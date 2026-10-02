"""贯通风险指数、转运路线、研究资源库存、调度申请和情景分析的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import CollectionLogisticsService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = CollectionLogisticsService(connection, FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (("plan", "planner"), ("dispatch", "dispatcher"), ("risk", "risk"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)
    for index, close in enumerate(("108", "105", "102", "100", "98", "96"), start=18):
        service.record_risk_record("plan", {"risk_index": "HUMIDITY", "duty_date": f"2026-09-{index}", "index_value": close, "source_revision": f"rev-{index}", "observed_at": f"2026-09-{index}T21:00:00Z"})
    service.create_facility("plan", {"center_id": "collection-east", "name": "北部实验样本事件保藏中心", "kind": "storage", "timezone": "Asia/Shanghai", "capacity_units": "500000"})
    service.create_facility("plan", {"center_id": "receiving-vault-b", "name": "沿海终端", "kind": "receiving-vault", "timezone": "Asia/Shanghai", "capacity_units": "800000"})
    service.create_route("plan", {"corridor_id": "transfer-east-1", "origin_center_id": "collection-east", "destination_center_id": "receiving-vault-b", "preservation_resource_kind": "preservation-box", "hourly_capacity": "100000", "delay_basis_points": 25, "response_minutes": 36})
    service.add_inventory_lot("dispatch", {"preservation_resource_lot_id": "lot-001", "center_id": "collection-east", "preservation_resource_kind": "preservation-box", "grade": "HUMIDITY", "quantity_units": "150000", "unit_cost_cny": "91.25", "received_at": "2026-09-24T06:00:00Z"})
    service.submit_dispatch("dispatch", {"dispatch_id": "nom-001", "corridor_id": "transfer-east-1", "specimen_event_id": "herbarium-room-east", "duty_date": "2026-09-25", "requested_units": "80000", "priority": 10, "idempotency_key": "nom-key-001"})
    allocation = service.allocate("dispatch", "transfer-east-1", "2026-09-25")
    deployment = service.dispatch_deployment("dispatch", "deployment-001", "nom-001", "lot-001", 2)
    service.create_scenario("plan", {"scenario_id": "storage-recovery", "name": "主干路恢复通行与实验样本事件需求回落", "risk_index_drop_percent": "9", "route_capacity_changes": {"transfer-east-1": "20"}, "demand_changes": {"collection-east:preservation-box": "-5"}})
    service.approve_scenario("risk", "storage-recovery", 1)
    scenario = service.run_scenario("plan", "storage-recovery", "2026-09-23")

    # 跨中心验证能力预检：同区域备选完全满足，目标中心样本产能与设备存在缺口
    service.create_facility("plan", {"center_id": "validation-east", "name": "东部验证中心", "kind": "storage", "timezone": "Asia/Shanghai", "region": "east", "capacity_units": "1000"})
    service.create_facility("plan", {"center_id": "validation-east-backup", "name": "东部验证备选中心", "kind": "storage", "timezone": "Asia/Shanghai", "region": "east", "capacity_units": "1000"})
    service.register_validation_scheme("plan", {
        "scheme_id": "scheme-ct-1",
        "name": "候选药 CT-77 跨中心验证方案",
        "candidate_project_id": "ct-77",
        "protocol_version": "v2.1",
        "sample_requirements": [{"sample_type": "PBMC", "required_units": "120", "grade": "A"}],
        "equipment_requirements": [{"equipment_kind": "flow-cytometer", "units": 2, "grade": "A"}],
        "duration_minutes": 120,
    })
    service.register_sample_capability("plan", {"center_id": "validation-east", "sample_type": "PBMC", "daily_capacity_units": "100"})
    service.register_equipment("plan", {"equipment_id": "veq-1", "center_id": "validation-east", "equipment_kind": "flow-cytometer", "grade": "A", "state": "available"})
    service.register_equipment("plan", {"equipment_id": "veq-2", "center_id": "validation-east", "equipment_kind": "flow-cytometer", "grade": "A", "state": "maintenance", "unavailable_from": "2026-09-26T00:00:00Z", "unavailable_until": "2026-09-26T12:00:00Z"})
    service.register_calendar_window("dispatch", {"center_id": "validation-east", "opens_at": "2026-09-26T00:00:00Z", "closes_at": "2026-09-27T00:00:00Z"})
    service.register_sample_capability("plan", {"center_id": "validation-east-backup", "sample_type": "PBMC", "daily_capacity_units": "200"})
    service.register_equipment("plan", {"equipment_id": "veqb-1", "center_id": "validation-east-backup", "equipment_kind": "flow-cytometer", "grade": "A", "state": "available"})
    service.register_equipment("plan", {"equipment_id": "veqb-2", "center_id": "validation-east-backup", "equipment_kind": "flow-cytometer", "grade": "A", "state": "available"})
    service.register_calendar_window("dispatch", {"center_id": "validation-east-backup", "opens_at": "2026-09-26T00:00:00Z", "closes_at": "2026-09-27T00:00:00Z"})
    precheck = service.precheck_validation("plan", {
        "candidate_project_id": "ct-77",
        "scheme_id": "scheme-ct-1",
        "target_center_id": "validation-east",
        "window_starts_at": "2026-09-26T02:00:00Z",
        "window_ends_at": "2026-09-26T06:00:00Z",
    })

    result = {"status": "ok", "index": service.risk_summary("HUMIDITY"), "plan_id": allocation["plan_id"], "deployment": deployment, "scenario_run_id": scenario["run_id"], "validation_precheck": {"conclusion": precheck["conclusion"], "gap_count": len(precheck["gaps"]), "top_alternative": precheck["alternatives"][0]["center_id"]}, "audit": service.audit_chain("audit"), "workspace": workspace.name}
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行实验样本事件保藏中心调度服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
