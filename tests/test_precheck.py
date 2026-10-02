from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from http.server import ThreadingHTTPServer
from pathlib import Path

from portfolio_ops.api import JsonApplication, make_handler
from portfolio_ops.clock import FrozenClock
from portfolio_ops.errors import Forbidden
from portfolio_ops.service import CollectionLogisticsService
from portfolio_ops.storage import connect as connect_storage


FROZEN_NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)


class PrecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        # 使用与生产 HTTP 服务相同的 connect（check_same_thread=False、WAL），
        # 以便覆盖 ThreadingHTTPServer 多线程并发边界。
        self.connection = connect_storage(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.service = CollectionLogisticsService(self.connection, FrozenClock(FROZEN_NOW))
        for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        # 目标中心 c-east-1 与同区域备选 c-east-2、跨区域备选 c-west-1。
        for center_id, tz in (("c-east-1", "Asia/Shanghai"), ("c-east-2", "Asia/Shanghai"), ("c-west-1", "UTC")):
            self.service.create_facility("plan", {
                "center_id": center_id, "name": center_id, "kind": "storage",
                "timezone": tz, "capacity_units": "1000",
            })
        self.service.assign_center_region("plan", "c-east-1", "east")
        self.service.assign_center_region("plan", "c-east-2", "east")
        self.service.assign_center_region("plan", "c-west-1", "west")
        # 周五 09:00-18:00（北京时间）= UTC 01:00-10:00；c-west-1 周五 09:00-18:00 UTC。
        for center_id, tz in (("c-east-1", None), ("c-east-2", None), ("c-west-1", None)):
            self.service.add_schedule_window("plan", {
                "center_id": center_id, "weekday": 4, "start_time": "09:00", "end_time": "18:00",
            })

    def tearDown(self) -> None:
        self.connection.close()

    def _wide_friday_window(self, center_id: str, timezone_local: bool = True) -> None:
        # 周五 08:00-22:00 本地；Asia/Shanghai 下为 UTC 00:00-14:00。
        self.service.add_schedule_window("plan", {
            "center_id": center_id, "weekday": 4, "start_time": "08:00", "end_time": "22:00",
        })

    def _capabilities(self, center_id: str, sample: str, equipment: str) -> None:
        self.service.register_sample_capability("plan", {
            "center_id": center_id, "sample_type": "PLASMA", "capacity_units": sample,
        })
        self.service.register_equipment_capability("plan", {
            "center_id": center_id, "equipment_kind": "SEQUENCER", "capacity_units": equipment,
        })

    def _inventory(self, lot_id: str, center_id: str, quantity: str) -> None:
        self.service.add_inventory_lot("dispatch", {
            "preservation_resource_lot_id": lot_id, "center_id": center_id,
            "preservation_resource_kind": "preservation-box", "grade": "A",
            "quantity_units": quantity, "unit_cost_cny": "12.50",
            "received_at": "2026-10-01T00:00:00Z",
        })

    def _request(self, **overrides):
        request = {
            "precheck_id": "pc-1",
            "project_id": "proj-onc-1",
            "protocol_id": "proto-pk-1",
            "center_id": "c-east-1",
            "window_start": "2026-10-02T01:00:00Z",
            "window_end": "2026-10-02T14:00:00Z",
            "required_minutes": 240,
            "sample_requirements": [{"sample_type": "PLASMA", "quantity_units": "100"}],
            "equipment_requirements": [{"equipment_kind": "SEQUENCER", "quantity_units": "2"}],
            "resource_requirements": [{"preservation_resource_kind": "preservation-box", "quantity_units": "80"}],
        }
        request.update(overrides)
        return request

    def test_fully_satisfied_center_is_feasible_and_recommends_nothing_required(self) -> None:
        self._wide_friday_window("c-east-1")
        self._capabilities("c-east-1", "120", "3")
        self._inventory("lot-1", "c-east-1", "100")
        result = self.service.run_precheck("plan", self._request())
        self.assertEqual(result["conclusion"], "feasible")
        self.assertEqual(result["gap_count"], 0)
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["region"], "east")
        # 期望窗口与北京周五窗口重叠，最早入场为当前冻结时刻 08:00Z。
        self.assertEqual(result["earliest_available_at"], "2026-10-02T08:00:00Z")
        self.assertEqual(result["requirements"]["samples"][0]["required_units"], "100.000")

    def test_partial_gap_explains_each_shortfall_and_ranks_alternatives(self) -> None:
        # 目标中心：样本/设备/库存全部不足，且期望窗口已无足够剩余时段。
        self._capabilities("c-east-1", "60", "1")
        self._inventory("lot-1", "c-east-1", "50")
        # 同区域备选完全满足（更宽的排产窗口）；跨区域备选库存不足。
        self._wide_friday_window("c-east-2")
        self._capabilities("c-east-2", "200", "4")
        self._inventory("lot-2", "c-east-2", "200")
        self._capabilities("c-west-1", "200", "4")
        self._inventory("lot-3", "c-west-1", "60")
        result = self.service.run_precheck("plan", self._request())
        self.assertEqual(result["conclusion"], "gap")
        codes = {gap["code"] for gap in result["gaps"]}
        self.assertEqual(
            codes,
            {"sample_capacity_short", "equipment_capacity_short", "resource_short", "schedule_conflict"},
        )
        sample_gap = next(gap for gap in result["gaps"] if gap["code"] == "sample_capacity_short")
        self.assertFalse(sample_gap["blocking"])
        self.assertEqual(sample_gap["required_units"], "100.000")
        self.assertEqual(sample_gap["available_units"], "60.000")
        self.assertEqual(sample_gap["shortfall_units"], "40.000")
        self.assertIn("PLASMA", sample_gap["message"])

        same_region = result["alternatives"]["same_region"]
        other_region = result["alternatives"]["other_region"]
        self.assertEqual([item["center_id"] for item in same_region], ["c-east-2"])
        self.assertEqual(same_region[0]["regional_pref"], "same_region")
        self.assertEqual(same_region[0]["satisfaction_percent"], 100)
        self.assertEqual([item["center_id"] for item in other_region], ["c-west-1"])
        self.assertEqual(other_region[0]["regional_pref"], "other_region")
        self.assertLess(other_region[0]["satisfaction_percent"], 100)
        self.assertEqual(result["alternatives"]["eligible"], 2)
        self.assertEqual(result["alternatives"]["evaluated"], 2)

    def test_infeasible_when_sample_type_unsupported_and_no_alternative_center(self) -> None:
        self._wide_friday_window("c-east-1")
        self._capabilities("c-east-1", "200", "4")
        self._inventory("lot-1", "c-east-1", "200")
        # 所有备选中心都缺少所需样本类型。
        self._capabilities("c-east-2", "200", "4")
        self._inventory("lot-2", "c-east-2", "200")
        self._capabilities("c-west-1", "200", "4")
        self._inventory("lot-3", "c-west-1", "200")
        result = self.service.run_precheck("plan", self._request(
            precheck_id="pc-bone",
            sample_requirements=[{"sample_type": "BONE_MARROW", "quantity_units": "10"}],
        ))
        self.assertEqual(result["conclusion"], "infeasible")
        blocking = [gap for gap in result["gaps"] if gap["blocking"]]
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]["code"], "sample_type_unsupported")
        self.assertEqual(blocking[0]["dimension"], "sample")
        self.assertEqual(result["alternatives"]["same_region"], [])
        self.assertEqual(result["alternatives"]["other_region"], [])
        self.assertEqual(result["alternatives"]["eligible"], 0)

    def test_precheck_never_consumes_inventory_or_writes_audit(self) -> None:
        self._wide_friday_window("c-east-1")
        self._capabilities("c-east-1", "200", "4")
        self._inventory("lot-1", "c-east-1", "200")
        request = self._request()
        before = self.connection.execute(
            "SELECT available_units FROM preservation_resource_lots WHERE preservation_resource_lot_id='lot-1'"
        ).fetchone()["available_units"]
        audit_before = self.connection.execute("SELECT count(*) AS c FROM traffic_audit_events").fetchone()["c"]
        for _ in range(3):
            result = self.service.run_precheck("plan", request)
            self.assertEqual(result["conclusion"], "feasible")
        after = self.connection.execute(
            "SELECT available_units FROM preservation_resource_lots WHERE preservation_resource_lot_id='lot-1'"
        ).fetchone()["available_units"]
        audit_after = self.connection.execute("SELECT count(*) AS c FROM traffic_audit_events").fetchone()["c"]
        self.assertEqual(before, after)
        self.assertEqual(audit_before, audit_after)

    def test_concurrent_updates_are_reflected_on_next_precheck(self) -> None:
        self._capabilities("c-east-1", "60", "1")
        self._inventory("lot-1", "c-east-1", "50")
        request = self._request()
        first = self.service.run_precheck("plan", request)
        self.assertEqual(first["conclusion"], "gap")
        self.assertIn("sample_capacity_short", {gap["code"] for gap in first["gaps"]})

        # 另一会话/事务提交新的能力与库存（并发数据更新）。
        self.service.register_sample_capability("plan", {
            "center_id": "c-east-1", "sample_type": "PLASMA", "capacity_units": "500",
        })
        self.service.register_equipment_capability("plan", {
            "center_id": "c-east-1", "equipment_kind": "SEQUENCER", "capacity_units": "6",
        })
        self._inventory("lot-9", "c-east-1", "500")
        second = self.service.run_precheck("plan", request)
        # 样本/设备/库存缺口消除；期望窗口（01:00-10:00Z，now=08:00Z）剩余 2 小时
        # 不足以容纳 4 小时连续时长，结论仍为排期缺口，但缺口只剩排期一项。
        self.assertEqual(second["conclusion"], "gap")
        self.assertEqual([gap["code"] for gap in second["gaps"]], ["schedule_conflict"])
        self.assertEqual(second["earliest_available_at"], "2026-10-09T01:00:00Z")

        # 排期数据更新（补一个周六更长窗口）后，结论变为可执行。
        self.service.add_schedule_window("plan", {
            "center_id": "c-east-1", "weekday": 5, "start_time": "09:00", "end_time": "20:00",
        })
        third = self.service.run_precheck("plan", self._request(
            window_start="2026-10-02T01:00:00Z", window_end="2026-10-03T12:00:00Z",
        ))
        self.assertEqual(third["conclusion"], "feasible")
        self.assertEqual(third["gap_count"], 0)

    def test_separate_connection_commit_is_visible_to_next_precheck_only(self) -> None:
        # 文件库 + WAL：预检服务连接 A 与数据维护连接 B 分离。
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "precheck.sqlite3"
            reader = CollectionLogisticsService(connect_storage(db_path), FrozenClock(FROZEN_NOW))
            writer = CollectionLogisticsService(connect_storage(db_path), FrozenClock(FROZEN_NOW))
            for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher")):
                writer.create_user(user_id, user_id, role)
            writer.create_facility("plan", {
                "center_id": "c-east-1", "name": "c-east-1", "kind": "storage",
                "timezone": "Asia/Shanghai", "capacity_units": "1000",
            })
            writer.assign_center_region("plan", "c-east-1", "east")
            writer.register_sample_capability("plan", {
                "center_id": "c-east-1", "sample_type": "PLASMA", "capacity_units": "60",
            })
            writer.register_equipment_capability("plan", {
                "center_id": "c-east-1", "equipment_kind": "SEQUENCER", "capacity_units": "1",
            })
            writer.add_schedule_window("plan", {
                "center_id": "c-east-1", "weekday": 4, "start_time": "08:00", "end_time": "22:00",
            })
            writer.add_inventory_lot("dispatch", {
                "preservation_resource_lot_id": "lot-1", "center_id": "c-east-1",
                "preservation_resource_kind": "preservation-box", "grade": "A",
                "quantity_units": "50", "unit_cost_cny": "12.50",
                "received_at": "2026-10-01T00:00:00Z",
            })
            request = self._request()
            first = reader.run_precheck("plan", request)
            self.assertEqual(first["conclusion"], "gap")

            # 快照边界：预检读事务进行期间，另一连接提交的更新在事务内不可见，
            # 回滚后再次预检才能读到最新数据。
            reader.connection.execute("BEGIN")
            stale_capacity = reader.connection.execute(
                "SELECT capacity_units FROM center_sample_capabilities "
                "WHERE center_id='c-east-1' AND sample_type='PLASMA'"
            ).fetchone()["capacity_units"]
            writer.register_sample_capability("plan", {
                "center_id": "c-east-1", "sample_type": "PLASMA", "capacity_units": "500",
            })
            writer.register_equipment_capability("plan", {
                "center_id": "c-east-1", "equipment_kind": "SEQUENCER", "capacity_units": "6",
            })
            writer.add_inventory_lot("dispatch", {
                "preservation_resource_lot_id": "lot-2", "center_id": "c-east-1",
                "preservation_resource_kind": "preservation-box", "grade": "A",
                "quantity_units": "500", "unit_cost_cny": "12.50",
                "received_at": "2026-10-02T00:00:00Z",
            })
            still_stale = reader.connection.execute(
                "SELECT capacity_units FROM center_sample_capabilities "
                "WHERE center_id='c-east-1' AND sample_type='PLASMA'"
            ).fetchone()["capacity_units"]
            self.assertEqual(stale_capacity, still_stale)
            reader.connection.execute("ROLLBACK")

            second = reader.run_precheck("plan", request)
            self.assertEqual(second["conclusion"], "feasible")
            self.assertEqual(second["gap_count"], 0)
            reader.connection.close()
            writer.connection.close()

    def test_concurrent_http_requests_do_not_corrupt_read_snapshot(self) -> None:
        # ThreadingHTTPServer 多线程共享同一连接：预检只读事务与写请求并发，
        # 必须全部成功，且库存最终值与预检过程中的只读保证不被破坏。
        self._wide_friday_window("c-east-1")
        self._capabilities("c-east-1", "200", "4")
        self._inventory("lot-1", "c-east-1", "200")
        app = JsonApplication(self.service)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        errors: list[Exception] = []

        def post(path: str, payload: dict, actor: str = "plan") -> None:
            request_obj = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Actor-Id": actor},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request_obj, timeout=5) as response:
                    assert response.status in (200, 201)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = []
        for index in range(12):
            threads.append(threading.Thread(target=post, args=("/validation_prechecks", self._request(precheck_id=f"pc-{index}"))))
        for index in range(4):
            threads.append(threading.Thread(target=post, args=(
                "/inventory/lots",
                {
                    "preservation_resource_lot_id": f"lot-c-{index}", "center_id": "c-east-1",
                    "preservation_resource_kind": "preservation-box", "grade": "A",
                    "quantity_units": "10", "unit_cost_cny": "1",
                    "received_at": f"2026-10-02T0{index}:00:00Z",
                },
                "dispatch",
            )))
        for worker in threads:
            worker.start()
        for worker in threads:
            worker.join()
        server.shutdown()
        server.server_close()
        self.assertEqual(errors, [])
        # 预检未扣减既有批次；新增批次来自显式写请求。
        self.assertEqual(Decimal(self.service.inventory_lot("lot-1")["available_units"]), Decimal("200"))

    def test_schedule_unavailable_is_blocking_when_no_window_long_enough(self) -> None:
        self._capabilities("c-east-1", "200", "4")
        self._inventory("lot-1", "c-east-1", "200")
        # 把窗口缩短为 1 小时，但方案需要 4 小时连续时长。
        self.connection.execute(
            "UPDATE center_schedule_windows SET end_time='10:00' WHERE center_id='c-east-1'"
        )
        result = self.service.run_precheck("plan", self._request(required_minutes=240))
        self.assertEqual(result["conclusion"], "infeasible")
        self.assertEqual(result["gaps"][0]["code"], "schedule_unavailable")
        self.assertTrue(result["gaps"][0]["blocking"])

    def test_permission_and_http_boundary(self) -> None:
        self._wide_friday_window("c-east-1")
        self._capabilities("c-east-1", "200", "4")
        self._inventory("lot-1", "c-east-1", "200")
        with self.assertRaises(Forbidden):
            self.service.run_precheck("audit", self._request())
        app = JsonApplication(self.service)
        response = app.handle(
            "POST", "/validation_prechecks", {"X-Actor-Id": "plan"},
            body=__import__("json").dumps(self._request()).encode("utf-8"),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["conclusion"], "feasible")
        self.assertEqual(response.body["precheck_id"], "pc-1")
        # 目录登记接口同样可用，并复用审计链。
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])


if __name__ == "__main__":
    unittest.main()
