"""跨中心验证能力预检的确定性计算。

预检只读：汇总验证方案在期望时间窗口内对样本类型、设备与排期的全部需求，
逐项判定目标中心是否满足，并为存在缺口的需求寻找同区域/其他区域的替代中心。
所有函数都是纯函数，便于离线验收与并发快照测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .planning import decimal_text, quantize_volume


ZERO = Decimal("0")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

# 预检结论
EXECUTABLE = "executable"
HAS_GAPS = "has_gaps"
NOT_EXECUTABLE = "not_executable"

# 缺口/需求维度
SAMPLE_DIMENSION = "sample_type"
EQUIPMENT_DIMENSION = "equipment"
WINDOW_DIMENSION = "time_window"


@dataclass(frozen=True, slots=True)
class Interval:
    starts_at: str
    ends_at: str

    def overlaps(self, other: "Interval") -> bool:
        return self.starts_at < other.ends_at and other.starts_at < self.ends_at

    def contains(self, other: "Interval") -> bool:
        return self.starts_at <= other.starts_at and other.ends_at <= self.ends_at


@dataclass(frozen=True, slots=True)
class EquipmentItem:
    equipment_id: str
    equipment_kind: str
    grade: str
    state: str
    unavailable_from: str | None
    unavailable_until: str | None

    def available_during(self, window: Interval) -> bool:
        if self.state != "available":
            return False
        if self.unavailable_from is None or self.unavailable_until is None:
            return True
        return not self.overlaps_unavailable(window)

    def overlaps_unavailable(self, window: Interval) -> bool:
        if self.unavailable_from is None or self.unavailable_until is None:
            return False
        return window.overlaps(Interval(self.unavailable_from, self.unavailable_until))


@dataclass(frozen=True, slots=True)
class Booking:
    starts_at: str
    ends_at: str
    units: Decimal
    sample_type: str | None
    equipment_kind: str | None


@dataclass(frozen=True, slots=True)
class CenterSnapshot:
    """单个中心在预检时点的只读能力快照。"""

    center_id: str
    name: str
    region: str
    active: bool
    sample_capacity: Mapping[str, Decimal]
    equipment: Sequence[EquipmentItem]
    windows: Sequence[Interval]
    bookings: Sequence[Booking]

    def bookings_for_sample(self, sample_type: str, window: Interval) -> Decimal:
        total = ZERO
        for booking in self.bookings:
            if booking.sample_type == sample_type and window.overlaps(
                Interval(booking.starts_at, booking.ends_at)
            ):
                total += booking.units
        return quantize_volume(total)

    def has_matching_window(self, window: Interval) -> bool:
        return any(calendar.contains(window) for calendar in self.windows)

    def available_equipment(self, equipment_kind: str, grade: str, window: Interval) -> list[EquipmentItem]:
        return [
            item
            for item in self.equipment
            if item.equipment_kind == equipment_kind
            and item.grade == grade
            and item.available_during(window)
        ]

    def equipment_pool(self, equipment_kind: str, grade: str, window: Interval) -> "EquipmentPool":
        matching = [
            item
            for item in self.equipment
            if item.equipment_kind == equipment_kind and item.grade == grade
        ]
        non_retired = [item for item in matching if item.state != "retired"]
        physical_available = self.available_equipment(equipment_kind, grade, window)
        committed = 0
        for booking in self.bookings:
            if booking.equipment_kind == equipment_kind and window.overlaps(
                Interval(booking.starts_at, booking.ends_at)
            ):
                committed += max(1, int(booking.units))
        free = max(0, len(physical_available) - committed)
        return EquipmentPool(
            matching=matching,
            non_retired=non_retired,
            physical_available=physical_available,
            committed=committed,
            free=free,
        )


@dataclass(frozen=True, slots=True)
class EquipmentPool:
    matching: Sequence[EquipmentItem]
    non_retired: Sequence[EquipmentItem]
    physical_available: Sequence[EquipmentItem]
    committed: int
    free: int

    def slot_releases(self, window: Interval) -> list[str | None]:
        """窗口内被占用槽位预计释放的时刻（每槽位一项，None 表示维护结束时间未知）。"""

        releases: list[str | None] = []
        for item in self.non_retired:
            if item.state == "maintenance" and item.overlaps_unavailable(window):
                releases.append(item.unavailable_until)
        return releases

    def booking_releases(self, center: CenterSnapshot, window: Interval) -> list[str]:
        ends: list[str] = []
        for booking in center.bookings:
            if booking.equipment_kind in {item.equipment_kind for item in self.matching} and window.overlaps(
                Interval(booking.starts_at, booking.ends_at)
            ):
                ends.extend([booking.ends_at] * max(1, int(booking.units)))
        return ends


@dataclass(frozen=True, slots=True)
class SampleRequirement:
    sample_type: str
    required_units: Decimal
    grade: str


@dataclass(frozen=True, slots=True)
class EquipmentRequirement:
    equipment_kind: str
    units: int
    grade: str


def evaluate_center(
    center: CenterSnapshot,
    samples: Sequence[SampleRequirement],
    equipments: Sequence[EquipmentRequirement],
    window: Interval,
    *,
    is_target: bool,
    duration_minutes: int = 60,
) -> dict[str, Any]:
    """评估单个中心，返回逐项需求判定与满足度评分。"""

    items: list[dict[str, Any]] = []
    satisfied_weight = 0
    total_weight = 0

    for requirement in samples:
        total_weight += 1
        detail: dict[str, Any] = {
            "dimension": SAMPLE_DIMENSION,
            "key": requirement.sample_type,
            "grade": requirement.grade,
            "required_units": decimal_text(quantize_volume(requirement.required_units)),
        }
        if not center.active:
            detail.update(met=False, reason="center_inactive", available_units="0.000")
        else:
            capacity = center.sample_capacity.get(requirement.sample_type)
            if capacity is None:
                detail.update(
                    met=False,
                    reason="sample_type_unsupported",
                    available_units="0.000",
                )
            else:
                committed = center.bookings_for_sample(requirement.sample_type, window)
                spare = quantize_volume(capacity - committed)
                available = max(ZERO, spare)
                detail.update(
                    available_units=decimal_text(available),
                    committed_units=decimal_text(committed),
                )
                if spare >= requirement.required_units:
                    detail.update(met=True, reason=None)
                    satisfied_weight += 1
                else:
                    detail.update(
                        met=False,
                        reason="sample_capacity_shortfall",
                        shortfall_units=decimal_text(
                            quantize_volume(requirement.required_units - spare)
                        ),
                    )
        items.append(detail)

    for requirement in equipments:
        total_weight += 1
        detail = {
            "dimension": EQUIPMENT_DIMENSION,
            "key": requirement.equipment_kind,
            "grade": requirement.grade,
            "required_units": str(requirement.units),
        }
        if not center.active:
            detail.update(met=False, reason="center_inactive", available_units="0")
        else:
            pool = center.equipment_pool(requirement.equipment_kind, requirement.grade, window)
            detail["available_units"] = str(pool.free)
            if not pool.matching:
                detail.update(met=False, reason="equipment_unsupported")
            elif pool.free < requirement.units:
                under_maintenance = [
                    item
                    for item in pool.non_retired
                    if item.state == "maintenance" and item.overlaps_unavailable(window)
                ]
                if under_maintenance:
                    reason = "equipment_in_maintenance"
                elif pool.committed > 0:
                    reason = "equipment_unavailable_or_booked"
                else:
                    reason = "equipment_capacity_shortfall"
                detail.update(
                    met=False,
                    reason=reason,
                    shortfall_units=str(requirement.units - pool.free),
                    booked_units=str(pool.committed),
                    unavailable_equipment_ids=sorted(
                        item.equipment_id for item in pool.matching if item.state != "available"
                    ),
                )
            else:
                detail.update(met=True, reason=None)
                satisfied_weight += 1
        items.append(detail)

    # 排期时间窗口
    total_weight += 1
    window_item: dict[str, Any] = {
        "dimension": WINDOW_DIMENSION,
        "key": f"{window.starts_at}/{window.ends_at}",
        "required_units": "1",
        "available_units": "1",
    }
    if not center.active:
        window_item.update(met=False, reason="center_inactive")
    elif center.has_matching_window(window):
        window_item.update(met=True, reason=None)
        satisfied_weight += 1
    else:
        window_item.update(met=False, reason="no_matching_time_window", available_units="0")
    items.append(window_item)

    score = Decimal(100) if total_weight == 0 else (
        Decimal(satisfied_weight * 100) / Decimal(total_weight)
    ).quantize(Decimal("0.01"))
    earliest = earliest_available(center, samples, equipments, window, duration_minutes)
    return {
        "center_id": center.center_id,
        "name": center.name,
        "region": center.region,
        "is_target": is_target,
        "satisfaction_score": decimal_text(score),
        "requirements_met": satisfied_weight,
        "requirements_total": total_weight,
        "earliest_available_at": earliest,
        "items": items,
        "fully_feasible": satisfied_weight == total_weight,
    }


def earliest_available(
    center: CenterSnapshot,
    samples: Sequence[SampleRequirement],
    equipments: Sequence[EquipmentRequirement],
    window: Interval,
    duration_minutes: int = 60,
) -> str | None:
    """中心最早可完整执行方案的时刻。

    以期望窗口起点为基准：样本产能缺口/设备维护在既有占用或维护结束后可能释放，
    排期取“已登记开放窗口中第一个能容纳方案时长的起点”。
    无法从当前数据推断出可用时刻（例如不支持该样本类型/设备）时返回 None。
    """

    if not center.active:
        return None
    candidates: list[str] = []
    supported = True

    for requirement in samples:
        if requirement.sample_type not in center.sample_capacity:
            supported = False
            break
        capacity = center.sample_capacity[requirement.sample_type]
        committed = center.bookings_for_sample(requirement.sample_type, window)
        if capacity < requirement.required_units:
            # 即使既有占用全部释放，日产能仍不足
            supported = False
            break
        if capacity - committed >= requirement.required_units:
            candidates.append(window.starts_at)
        else:
            freed = [
                booking.ends_at
                for booking in center.bookings
                if booking.sample_type == requirement.sample_type
                and window.overlaps(Interval(booking.starts_at, booking.ends_at))
            ]
            if not freed:
                supported = False
                break
            candidates.append(max(freed))

    if not supported:
        return None

    for requirement in equipments:
        pool = center.equipment_pool(requirement.equipment_kind, requirement.grade, window)
        if not pool.matching:
            return None
        if pool.free >= requirement.units:
            candidates.append(window.starts_at)
            continue
        needed = requirement.units - pool.free
        if len(pool.non_retired) < requirement.units:
            return None
        releases = sorted(
            release
            for release in (*pool.slot_releases(window), *pool.booking_releases(center, window))
            if release is not None
        )
        # 存在结束时间未知的维护槽位时，无法承诺最早可用时刻
        if len(releases) < needed or any(release is None for release in pool.slot_releases(window)):
            return None
        candidates.append(releases[needed - 1])

    if not center.windows:
        return None
    resource_ready = max(candidates, default=window.starts_at)
    ready = _parse(resource_ready)
    duration = timedelta(minutes=duration_minutes)
    for calendar in sorted(center.windows, key=lambda item: item.starts_at):
        start = max(_parse(calendar.starts_at), ready)
        if start + duration <= _parse(calendar.ends_at):
            return _format(start)
    return None


def rank_alternatives(
    target: CenterSnapshot,
    evaluations: Sequence[Mapping[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """按同区域优先、满足度降序、最早可用时间升序排列替代中心。"""

    rows = [dict(row) for row in evaluations if row["center_id"] != target.center_id]
    for row in rows:
        row["same_region"] = row["region"] == target.region

    def sort_key(row: Mapping[str, Any]) -> tuple[int, Decimal, str, str]:
        earliest = row.get("earliest_available_at")
        return (
            0 if row["same_region"] else 1,
            -Decimal(row["satisfaction_score"]),
            "￿" if earliest is None else earliest,
            row["center_id"],
        )

    rows.sort(key=sort_key)
    return rows[:limit]


def build_precheck_result(
    *,
    query: Mapping[str, Any],
    target_center: CenterSnapshot,
    other_centers: Sequence[CenterSnapshot],
    samples: Sequence[SampleRequirement],
    equipments: Sequence[EquipmentRequirement],
    window: Interval,
    duration_minutes: int,
    evaluated_at: str,
    data_revision: str,
) -> dict[str, Any]:
    target_result = evaluate_center(
        target_center, samples, equipments, window, is_target=True, duration_minutes=duration_minutes
    )
    other_results = [
        evaluate_center(
            center, samples, equipments, window, is_target=False, duration_minutes=duration_minutes
        )
        for center in sorted(other_centers, key=lambda item: item.center_id)
    ]

    hard_reasons = {
        "sample_type_unsupported",
        "equipment_unsupported",
        "center_inactive",
    }

    if target_result["fully_feasible"]:
        conclusion = EXECUTABLE
    else:
        rescue_centers = [row for row in other_results if row["fully_feasible"]]
        if rescue_centers:
            # 目标中心在期望窗口内有缺口，但存在可完整执行的替代中心
            conclusion = HAS_GAPS
        elif target_result.get("earliest_available_at") is not None and not any(
            item["reason"] in hard_reasons for item in target_result["items"]
        ):
            # 仅存在可等待恢复的软缺口（产能占用、维护、排期未开放）
            conclusion = HAS_GAPS
        else:
            conclusion = NOT_EXECUTABLE

    alternatives = rank_alternatives(target_center, other_results)
    gaps = [
        {
            "dimension": item["dimension"],
            "key": item["key"],
            "grade": item.get("grade"),
            "required_units": item["required_units"],
            "available_units": item.get("available_units"),
            "shortfall_units": item.get("shortfall_units"),
            "booked_units": item.get("booked_units"),
            "unavailable_equipment_ids": item.get("unavailable_equipment_ids"),
            "reason": item["reason"],
        }
        for item in target_result["items"]
        if not item["met"]
    ]
    return {
        "candidate_project_id": query["candidate_project_id"],
        "scheme_id": query["scheme_id"],
        "target_center_id": target_center.center_id,
        "window": {"starts_at": window.starts_at, "ends_at": window.ends_at},
        "evaluated_at": evaluated_at,
        "data_revision": data_revision,
        "conclusion": conclusion,
        "target": target_result,
        "gaps": gaps,
        "alternatives": alternatives,
        "alternative_count": len(alternatives),
    }
