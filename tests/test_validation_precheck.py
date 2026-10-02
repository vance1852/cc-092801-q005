from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from portfolio_ops.api import JsonApplication
from portfolio_ops.clock import FrozenClock
from portfolio_ops.service import CollectionLogisticsService


WINDOW = ("2026-10-05T02:00:00Z", "2026-10-05T06:00:00Z")
FULL_DAY = ("2026-10-05T00:00:00Z", "2026-10-06T00:00:00Z")
LATER_DAY = ("2026-10-06T00:00:00Z", "2026-10-07T00:00:00Z")


class PrecheckFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc))
        self.service = CollectionLogisticsService(self.connection, self.clock)
        self.service.create_user("plan", "plan", "planner")
        self.service.create_user("dispatch", "dispatch", "dispatcher")
        self.service.create_user("risk", "risk", "risk")
        self.service.create_user("audit", "audit", "auditor")

    def tearDown(self) -> None:
        self.connection.close()

    def center(self, center_id: str, region: str, name: str | None = None) -> None:
        self.service.create_facility("plan", {
            "center_id": center_id,
            "name": name or center_id,
            "kind": "storage",
            "timezone": "Asia/Shanghai",
            "region": region,
            "capacity_units": "1000",
        })

    def scheme(self, *, sample_units: str = "120", equipment_units: int = 2) -> None:
        self.service.register_validation_scheme("plan", {
            "scheme_id": "sch-1",
            "name": "多中心流式验证方案",
            "candidate_project_id": "proj-1",
            "protocol_version": "v1.0",
            "sample_requirements": [
                {"sample_type": "PBMC", "required_units": sample_units, "grade": "A"},
            ],
            "equipment_requirements": [
                {"equipment_kind": "flow-cytometer", "units": equipment_units, "grade": "A"},
            ],
            "duration_minutes": 120,
        })

    def capability(self, center_id: str, units: str, sample_type: str = "PBMC") -> None:
        self.service.register_sample_capability("plan", {
            "center_id": center_id,
            "sample_type": sample_type,
            "daily_capacity_units": units,
        })

    def equipment(self, equipment_id: str, center_id: str, **changes: object) -> None:
        body = {
            "equipment_id": equipment_id,
            "center_id": center_id,
            "equipment_kind": "flow-cytometer",
            "grade": "A",
            "state": "available",
        }
        body.update(changes)
        self.service.register_equipment("plan", body)

    def window(self, center_id: str, span: tuple[str, str] = FULL_DAY) -> None:
        self.service.register_calendar_window("dispatch", {
            "center_id": center_id,
            "opens_at": span[0],
            "closes_at": span[1],
        })

    def booking(
        self,
        center_id: str,
        reference_id: str,
        *,
        span: tuple[str, str] = WINDOW,
        units: str = "1",
        sample_type: str | None = None,
        equipment_kind: str | None = None,
    ) -> None:
        self.service.register_booking("dispatch", {
            "center_id": center_id,
            "starts_at": span[0],
            "ends_at": span[1],
            "units": units,
            "reference_id": reference_id,
            "sample_type": sample_type,
            "equipment_kind": equipment_kind,
        })

    def fully_equipped(self, center_id: str) -> None:
        self.capability(center_id, "200")
        self.equipment(f"{center_id}-eq-1", center_id)
        self.equipment(f"{center_id}-eq-2", center_id)
        self.window(center_id)

    def precheck(self, target: str = "center-a", **changes: object):
        body = self._query(target)
        body.update(changes)
        return self.service.precheck_validation("plan", body)

    def _query(self, target: str = "center-a") -> dict[str, str]:
        return {
            "candidate_project_id": "proj-1",
            "scheme_id": "sch-1",
            "target_center_id": target,
            "window_starts_at": WINDOW[0],
            "window_ends_at": WINDOW[1],
        }


class FullySatisfiedTests(PrecheckFixture):
    def test_target_fully_feasible_is_executable_without_gaps(self) -> None:
        self.center("center-a", "east")
        self.center("center-b", "west")
        self.scheme()
        self.fully_equipped("center-a")
        self.fully_equipped("center-b")

        result = self.precheck()

        self.assertEqual(result["conclusion"], "executable")
        self.assertEqual(result["gaps"], [])
        self.assertTrue(result["target"]["fully_feasible"])
        self.assertEqual(result["target"]["satisfaction_score"], "100.00")
        self.assertEqual(result["target"]["requirements_met"], 3)
        self.assertEqual(result["target"]["earliest_available_at"], WINDOW[0])
        self.assertEqual(
            {(item["dimension"], item["key"]) for item in result["target"]["items"]},
            {("sample_type", "PBMC"), ("equipment", "flow-cytometer"), ("time_window", f"{WINDOW[0]}/{WINDOW[1]}")},
        )
        # 即使目标中心可执行，仍输出同区域与其他区域候选作为参照
        self.assertEqual(result["alternative_count"], 1)

    def test_precheck_does_not_consume_or_reserve_anything(self) -> None:
        self.center("center-a", "east")
        self.scheme()
        self.fully_equipped("center-a")
        audit_before = self.connection.execute("SELECT COUNT(*) FROM traffic_audit_events").fetchone()[0]

        result = self.precheck()
        self.assertEqual(result["conclusion"], "executable")

        # 预检不登记占用
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM validation_resource_bookings").fetchone()[0],
            0,
        )
        # 预检不写审计、不产生副作用
        audit_after = self.connection.execute("SELECT COUNT(*) FROM traffic_audit_events").fetchone()[0]
        self.assertEqual(audit_before, audit_after)
        self.assertTrue(self.service.audit_chain("audit")["valid"])


class PartialGapTests(PrecheckFixture):
    def test_gaps_explained_per_item_and_same_region_alternative_ranks_first(self) -> None:
        self.center("center-a", "east", "目标中心")
        self.center("center-east-b", "east", "同区域备选")
        self.center("center-west-b", "west", "其他区域备选")
        self.scheme()

        # 目标中心：样本产能差 20、一台设备维护中
        self.capability("center-a", "100")
        self.equipment("eq-a-1", "center-a")
        self.equipment(
            "eq-a-2",
            "center-a",
            state="maintenance",
            unavailable_from="2026-10-05T00:00:00Z",
            unavailable_until="2026-10-05T12:00:00Z",
        )
        self.window("center-a")

        # 同区域备选完全满足；其他区域备选只部分满足
        self.fully_equipped("center-east-b")
        self.capability("center-west-b", "110")
        self.equipment("eq-w-1", "center-west-b")
        self.window("center-west-b")

        result = self.precheck()

        self.assertEqual(result["conclusion"], "has_gaps")
        reasons = {(gap["dimension"], gap["reason"]) for gap in result["gaps"]}
        self.assertIn(("sample_type", "sample_capacity_shortfall"), reasons)
        self.assertIn(("equipment", "equipment_in_maintenance"), reasons)
        sample_gap = next(gap for gap in result["gaps"] if gap["dimension"] == "sample_type")
        self.assertEqual(sample_gap["required_units"], "120.000")
        self.assertEqual(sample_gap["available_units"], "100.000")
        self.assertEqual(sample_gap["shortfall_units"], "20.000")
        equipment_gap = next(gap for gap in result["gaps"] if gap["dimension"] == "equipment")
        self.assertEqual(equipment_gap["shortfall_units"], "1")
        self.assertEqual(equipment_gap["unavailable_equipment_ids"], ["eq-a-2"])

        # 同区域可完整执行的中心排在最前
        self.assertEqual(result["alternatives"][0]["center_id"], "center-east-b")
        self.assertTrue(result["alternatives"][0]["same_region"])
        self.assertTrue(result["alternatives"][0]["fully_feasible"])
        ordered = [row["center_id"] for row in result["alternatives"]]
        self.assertLess(ordered.index("center-east-b"), ordered.index("center-west-b"))
        self.assertFalse(result["alternatives"][-1]["same_region"])

    def test_existing_bookings_count_toward_committed_capacity(self) -> None:
        self.center("center-a", "east")
        self.scheme()
        # 账面产能 200，但窗口内已有 100 单位样本占用与 1 台设备占用
        self.capability("center-a", "200")
        self.equipment("eq-a-1", "center-a")
        self.equipment("eq-a-2", "center-a")
        self.window("center-a")
        self.booking("center-a", "book-1", units="100", sample_type="PBMC")
        self.booking("center-a", "book-2", units="1", equipment_kind="flow-cytometer")

        result = self.precheck()

        self.assertEqual(result["conclusion"], "has_gaps")
        sample_item = next(item for item in result["target"]["items"] if item["dimension"] == "sample_type")
        self.assertEqual(sample_item["committed_units"], "100.000")
        self.assertEqual(sample_item["available_units"], "100.000")
        equipment_item = next(item for item in result["target"]["items"] if item["dimension"] == "equipment")
        self.assertEqual(equipment_item["booked_units"], "1")
        self.assertEqual(equipment_item["available_units"], "1")
        # 设备占用在窗口结束时释放，可推断次日/占用后的最早可用时刻
        self.assertIsNotNone(result["target"]["earliest_available_at"])

    def test_no_matching_time_window_is_a_gap(self) -> None:
        self.center("center-a", "east")
        self.scheme()
        self.capability("center-a", "200")
        self.equipment("eq-a-1", "center-a")
        self.equipment("eq-a-2", "center-a")
        # 只开放次日，期望窗口当天不可用
        self.window("center-a", LATER_DAY)

        result = self.precheck()
        self.assertEqual(result["conclusion"], "has_gaps")
        gap = next(gap for gap in result["gaps"] if gap["dimension"] == "time_window")
        self.assertEqual(gap["reason"], "no_matching_time_window")
        # 次日窗口存在，最早可用时间落到次日开放时刻
        self.assertEqual(result["target"]["earliest_available_at"], LATER_DAY[0])


class NoAlternativeTests(PrecheckFixture):
    def test_unsupported_everywhere_is_not_executable_with_empty_alternatives(self) -> None:
        self.center("center-a", "east")
        self.scheme()
        # 目标中心既不支持该样本类型，也没有设备和排期
        result = self.precheck()

        self.assertEqual(result["conclusion"], "not_executable")
        reasons = {gap["reason"] for gap in result["gaps"]}
        self.assertIn("sample_type_unsupported", reasons)
        self.assertIn("equipment_unsupported", reasons)
        self.assertIn("no_matching_time_window", reasons)
        self.assertEqual(result["alternatives"], [])
        self.assertEqual(result["alternative_count"], 0)
        self.assertIsNone(result["target"]["earliest_available_at"])

    def test_capacity_that_can_never_recover_is_not_executable(self) -> None:
        self.center("center-a", "east")
        # 另一个区域的中心同样不支持该样本类型
        self.center("center-west", "west")
        self.scheme()
        # 日产能本身就低于需求，即使占用全部释放也无法满足
        self.capability("center-a", "100")
        self.equipment("eq-a-1", "center-a")
        self.equipment("eq-a-2", "center-a")
        self.window("center-a")

        result = self.precheck()
        self.assertEqual(result["conclusion"], "not_executable")
        self.assertIsNone(result["target"]["earliest_available_at"])
        self.assertFalse(any(row["fully_feasible"] for row in result["alternatives"]))


class ConcurrentDataTests(PrecheckFixture):
    def test_changes_after_first_precheck_are_reflected(self) -> None:
        self.center("center-a", "east")
        self.scheme()
        self.fully_equipped("center-a")

        first = self.precheck()
        self.assertEqual(first["conclusion"], "executable")
        first_revision = first["data_revision"]

        # 写入新的排期占用：窗口内一台流式细胞仪已被占用
        self.booking("center-a", "concurrent-book", units="1", equipment_kind="flow-cytometer")

        second = self.precheck()
        self.assertEqual(second["conclusion"], "has_gaps")
        self.assertNotEqual(second["data_revision"], first_revision)
        equipment_gap = next(gap for gap in second["gaps"] if gap["dimension"] == "equipment")
        self.assertEqual(equipment_gap["reason"], "equipment_unavailable_or_booked")
        self.assertEqual(equipment_gap["available_units"], "1")

        # 占用改期到期望窗口开始之前：不再与窗口重叠，结论恢复为可执行
        self.connection.execute(
            "UPDATE validation_resource_bookings SET starts_at=?,ends_at=? WHERE reference_id='concurrent-book'",
            ("2026-10-05T00:00:00Z", "2026-10-05T01:00:00Z"),
        )
        third = self.precheck()
        self.assertEqual(third["conclusion"], "executable")

    def test_uncommitted_writes_are_never_visible_and_committed_ones_are(self) -> None:
        from portfolio_ops.storage import transaction

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "concurrent.sqlite3"
            reader = sqlite3.connect(str(database), isolation_level=None)
            reader.row_factory = sqlite3.Row
            service = CollectionLogisticsService(reader, self.clock)
            service.create_user("plan", "plan", "planner")
            service.create_user("dispatch", "dispatch", "dispatcher")
            self._seed_on(service)

            writer = sqlite3.connect(str(database), isolation_level=None)
            writer.row_factory = sqlite3.Row
            writer.execute("PRAGMA busy_timeout=5000")

            self.assertEqual(service.precheck_validation("plan", self._query())["conclusion"], "executable")

            # 另一连接开始写入但尚未提交：预检仍读取已提交快照
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "INSERT INTO validation_resource_bookings(center_id,sample_type,equipment_kind,starts_at,ends_at,"
                "units,reference_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("center-a", None, "flow-cytometer", WINDOW[0], WINDOW[1], "1", "pending-book", "dispatch", "2026-10-02T09:00:00Z"),
            )
            try:
                during = service.precheck_validation("plan", self._query())
                self.assertEqual(during["conclusion"], "executable")
            finally:
                writer.rollback()
                writer.close()

            # 提交后再次预检立即反映最新排期
            writer = sqlite3.connect(str(database), isolation_level=None)
            writer.execute(
                "INSERT INTO validation_resource_bookings(center_id,sample_type,equipment_kind,starts_at,ends_at,"
                "units,reference_id,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("center-a", None, "flow-cytometer", WINDOW[0], WINDOW[1], "1", "committed-book", "dispatch", "2026-10-02T09:00:00Z"),
            )
            writer.commit()
            writer.close()

            after = service.precheck_validation("plan", self._query())
            self.assertEqual(after["conclusion"], "has_gaps")
            reader.close()

    def _seed_on(self, service: CollectionLogisticsService) -> None:
        service.create_facility("plan", {
            "center_id": "center-a", "name": "目标中心", "kind": "storage",
            "timezone": "Asia/Shanghai", "region": "east", "capacity_units": "1000",
        })
        service.register_validation_scheme("plan", {
            "scheme_id": "sch-1", "name": "方案", "candidate_project_id": "proj-1", "protocol_version": "v1.0",
            "sample_requirements": [{"sample_type": "PBMC", "required_units": "120", "grade": "A"}],
            "equipment_requirements": [{"equipment_kind": "flow-cytometer", "units": 2, "grade": "A"}],
            "duration_minutes": 120,
        })
        service.register_sample_capability("plan", {"center_id": "center-a", "sample_type": "PBMC", "daily_capacity_units": "200"})
        service.register_equipment("plan", {"equipment_id": "eq-1", "center_id": "center-a", "equipment_kind": "flow-cytometer", "grade": "A"})
        service.register_equipment("plan", {"equipment_id": "eq-2", "center_id": "center-a", "equipment_kind": "flow-cytometer", "grade": "A"})
        service.register_calendar_window("dispatch", {"center_id": "center-a", "opens_at": FULL_DAY[0], "closes_at": FULL_DAY[1]})


class PrecheckApiTests(PrecheckFixture):
    def test_http_endpoint_returns_structured_output(self) -> None:
        self.center("center-a", "east")
        self.scheme()
        self.fully_equipped("center-a")
        app = JsonApplication(self.service)

        response = app.handle(
            "POST",
            "/validation_prechecks",
            {"X-Actor-Id": "plan", "Content-Type": "application/json"},
            json.dumps({
                "candidate_project_id": "proj-1",
                "scheme_id": "sch-1",
                "target_center_id": "center-a",
                "window_starts_at": WINDOW[0],
                "window_ends_at": WINDOW[1],
            }).encode("utf-8"),
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["conclusion"], "executable")
        self.assertEqual(response.body["target"]["center_id"], "center-a")
        self.assertIn("data_revision", response.body)
        self.assertIn("alternatives", response.body)

    def test_missing_actor_is_rejected(self) -> None:
        app = JsonApplication(self.service)
        response = app.handle("POST", "/validation_prechecks", {}, b"{}")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_dispatcher_may_run_precheck_but_not_register_scheme(self) -> None:
        app = JsonApplication(self.service)
        scheme_body = json.dumps({
            "scheme_id": "sch-1", "name": "方案", "candidate_project_id": "proj-1", "protocol_version": "v1",
            "sample_requirements": [{"sample_type": "PBMC", "required_units": "1"}],
            "equipment_requirements": [{"equipment_kind": "flow-cytometer", "units": 1}],
        }).encode("utf-8")
        forbidden = app.handle("POST", "/validation_schemes", {"X-Actor-Id": "dispatch"}, scheme_body)
        self.assertEqual(forbidden.status, 403)

        # 调度角色同样可以执行只读预检
        self.center("center-a", "east")
        self.scheme()
        self.fully_equipped("center-a")
        ok = app.handle(
            "POST",
            "/validation_prechecks",
            {"X-Actor-Id": "dispatch", "Content-Type": "application/json"},
            json.dumps(self._query()).encode("utf-8"),
        )
        self.assertEqual(ok.status, 200)
        self.assertEqual(ok.body["conclusion"], "executable")

        # 空请求体得到参数校验错误，而不是权限错误
        response = app.handle("POST", "/validation_prechecks", {"X-Actor-Id": "dispatch"}, b"{}")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")


if __name__ == "__main__":
    unittest.main()
