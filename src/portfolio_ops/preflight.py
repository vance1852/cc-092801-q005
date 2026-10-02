"""跨中心验证能力预检的输入契约与纯函数评估。

预检汇总候选项目、验证方案在期望时间窗口内的样本类型、设备、研究资源与
连续排产时段需求，输出可执行 / 存在缺口 / 不可执行结论，并按满足度与最早
可用时间推荐同区域或其他区域的替代中心。本模块不接触 SQLite，也不持有任何
可变状态，因此可以在不扣减库存、不占用排期的前提下重复执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .clock import parse_utc, utc_text
from .errors import ValidationFailed
from .models import RESOURCE_KINDS, decimal_value, identifier, required_text
from .planning import quantize_volume


ZERO = Decimal("0")
ANY_GRADE = "ANY"
BLOCKING_GAP = "blocking"
CLOCK_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")
REGION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

FEASIBLE = "feasible"
HAS_GAP = "gap"
INFEASIBLE = "infeasible"


def region_text(value: object) -> str:
    result = required_text(value, "region", 32)
    if not REGION_NAME.fullmatch(result):
        raise ValidationFailed("region 只能包含字母、数字、下划线、点和连字符")
    return result


def clock_time(value: object, field: str) -> str:
    text = required_text(value, field, 8)
    match = CLOCK_TIME.fullmatch(text)
    if match is None:
        raise ValidationFailed(f"{field} 必须是 HH:MM 或 HH:MM:SS 本地时间")
    hour, minute, second = match.groups()
    normalized = f"{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"
    return normalized


def _minutes(value: str) -> int:
    parts = value.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    second = int(parts[2]) if len(parts) > 2 else 0
    return hour * 60 + minute + (1 if second >= 30 else 0)


def weekday_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 6:
        raise ValidationFailed("weekday 必须是 0（周一）到 6（周日）的整数")
    return value


@dataclass(frozen=True, slots=True)
class SampleCapability:
    sample_type: str
    grade: str
    capacity_units: Decimal
    active: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SampleCapability":
        return cls(
            sample_type=required_text(raw.get("sample_type"), "sample_type", 48).upper(),
            grade=required_text(raw.get("grade", ANY_GRADE), "grade", 32).upper(),
            capacity_units=decimal_value(raw.get("capacity_units"), "capacity_units", minimum=Decimal("0")),
            active=bool(raw.get("active", True)),
        )


@dataclass(frozen=True, slots=True)
class EquipmentCapability:
    equipment_kind: str
    grade: str
    capacity_units: Decimal
    active: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EquipmentCapability":
        return cls(
            equipment_kind=required_text(raw.get("equipment_kind"), "equipment_kind", 48).upper(),
            grade=required_text(raw.get("grade", ANY_GRADE), "grade", 32).upper(),
            capacity_units=decimal_value(raw.get("capacity_units"), "capacity_units", minimum=Decimal("0")),
            active=bool(raw.get("active", True)),
        )


@dataclass(frozen=True, slots=True)
class ScheduleWindow:
    weekday: int
    start_time: str
    end_time: str
    active: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ScheduleWindow":
        start_time = clock_time(raw.get("start_time"), "start_time")
        end_time = clock_time(raw.get("end_time"), "end_time")
        if _minutes(end_time) <= _minutes(start_time):
            raise ValidationFailed("end_time 必须晚于 start_time，不支持跨午夜窗口")
        active = bool(raw.get("active", True))
        return cls(weekday_value(raw.get("weekday")), start_time, end_time, active)


@dataclass(frozen=True, slots=True)
class CapabilityDemand:
    kind: str
    grade: str
    required_units: Decimal


@dataclass(frozen=True, slots=True)
class PrecheckRequest:
    precheck_id: str
    project_id: str
    protocol_id: str
    center_id: str
    window_start: datetime
    window_end: datetime
    samples: tuple[CapabilityDemand, ...]
    equipment: tuple[CapabilityDemand, ...]
    resources: tuple[CapabilityDemand, ...]
    required_minutes: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PrecheckRequest":
        window_start = parse_utc(required_text(raw.get("window_start"), "window_start", 40), "window_start")
        window_end = parse_utc(required_text(raw.get("window_end"), "window_end", 40), "window_end")
        if window_end <= window_start:
            raise ValidationFailed("window_end 必须晚于 window_start")
        required_minutes = raw.get("required_minutes", 0)
        if isinstance(required_minutes, bool) or not isinstance(required_minutes, int) or required_minutes < 0:
            raise ValidationFailed("required_minutes 必须是非负整数")
        if required_minutes and (window_end - window_start) < timedelta(minutes=required_minutes):
            raise ValidationFailed("期望时间窗口短于方案所需连续时长")
        return cls(
            precheck_id=identifier(raw.get("precheck_id"), "precheck_id"),
            project_id=identifier(raw.get("project_id"), "project_id"),
            protocol_id=identifier(raw.get("protocol_id"), "protocol_id"),
            center_id=identifier(raw.get("center_id") or raw.get("candidate_center_id"), "center_id"),
            window_start=window_start,
            window_end=window_end,
            samples=_capability_demands(raw.get("sample_requirements", []), "sample_requirements", "sample_type"),
            equipment=_capability_demands(raw.get("equipment_requirements", []), "equipment_requirements", "equipment_kind"),
            resources=tuple(sorted(_resource_demands(raw.get("resource_requirements", [])), key=lambda item: item.kind)),
            required_minutes=required_minutes,
        )


def _capability_demands(raw: object, field: str, kind_field: str) -> tuple[CapabilityDemand, ...]:
    if not isinstance(raw, list):
        raise ValidationFailed(f"{field} 必须是数组")
    merged: dict[tuple[str, str], Decimal] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"{field} 的每一项必须是对象")
        kind = required_text(item.get(kind_field), kind_field, 48).upper()
        grade = required_text(item.get("grade", ANY_GRADE), "grade", 32).upper()
        required_units = decimal_value(item.get("quantity_units"), f"{field}.quantity_units", minimum=Decimal("0.001"))
        key = (kind, grade)
        merged[key] = merged.get(key, ZERO) + required_units
    return tuple(
        CapabilityDemand(kind, grade, quantize_volume(required_units))
        for (kind, grade), required_units in sorted(merged.items())
    )


def _resource_demands(raw: object) -> list[CapabilityDemand]:
    if not isinstance(raw, list):
        raise ValidationFailed("resource_requirements 必须是数组")
    merged: dict[str, Decimal] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValidationFailed("resource_requirements 的每一项必须是对象")
        kind = required_text(item.get("preservation_resource_kind"), "preservation_resource_kind", 32)
        if kind not in RESOURCE_KINDS:
            raise ValidationFailed("preservation_resource_kind 不是受支持的研究资源类型")
        required_units = decimal_value(item.get("quantity_units"), "resource_requirements.quantity_units", minimum=Decimal("0.001"))
        merged[kind] = merged.get(kind, ZERO) + required_units
    return [CapabilityDemand(kind, "", quantize_volume(required_units)) for kind, required_units in sorted(merged.items())]


def _gap(dimension: str, code: str, message: str, *, blocking: bool, **details: object) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "code": code,
        "blocking": blocking,
        "severity": BLOCKING_GAP if blocking else "recoverable",
        "message": message,
        **details,
    }


def _capability_total(rows: Sequence[Mapping[str, Any]], kind_field: str, wanted: str, wanted_grade: str) -> Decimal:
    total = ZERO
    for row in rows:
        if not row.get("active") or row[kind_field] != wanted:
            continue
        grade = str(row.get("grade", ANY_GRADE))
        if wanted_grade == ANY_GRADE or grade == ANY_GRADE or grade == wanted_grade:
            total += Decimal(str(row["capacity_units"]))
    return quantize_volume(total)


def _iter_slots(windows: Sequence[Mapping[str, Any]], timezone_name: str, now: datetime, horizon_days: int):
    """展开从现在起每个工作日的本地排产窗口为 UTC 时间区间。"""
    zone = ZoneInfo(timezone_name)
    local_today = now.astimezone(zone).date()
    for offset in range(horizon_days + 1):
        local_day = local_today + timedelta(days=offset)
        for window in windows:
            if not window.get("active") or window["weekday"] != local_day.weekday():
                continue
            start_clock = divmod(_minutes(str(window["start_time"])), 60)
            end_clock = divmod(_minutes(str(window["end_time"])), 60)
            start_dt = datetime.combine(
                local_day, time(hour=start_clock[0], minute=start_clock[1]), tzinfo=zone
            ).astimezone(timezone.utc)
            end_dt = datetime.combine(
                local_day, time(hour=end_clock[0], minute=end_clock[1]), tzinfo=zone
            ).astimezone(timezone.utc)
            if end_dt <= now:
                continue
            yield start_dt, end_dt


def _schedule_fit(
    windows: Sequence[Mapping[str, Any]],
    timezone_name: str,
    now: datetime,
    window_start: datetime,
    window_end: datetime,
    required_minutes: int,
    horizon_days: int,
) -> tuple[datetime | None, datetime | None, bool]:
    """返回（期望窗口内最早可入场时间、未来任意窗口最早可入场时间、是否存在足够长的窗口）。

    入场时间不得早于当前时间；已经开始但尚未结束的窗口可以从当前时刻入场，
    只要剩余时长足以容纳方案所需连续时长。
    """
    if required_minutes <= 0:
        return None, None, True
    active_windows = [window for window in windows if window.get("active")]
    if not active_windows:
        return None, None, False
    duration = timedelta(minutes=required_minutes)
    inside: datetime | None = None
    future: datetime | None = None
    long_enough = False
    for slot_start, slot_end in _iter_slots(active_windows, timezone_name, now, horizon_days):
        if slot_end - slot_start < duration:
            continue
        long_enough = True
        entry = max(slot_start, now, window_start)
        if entry < slot_end and slot_end - entry >= duration and (future is None or entry < future):
            future = entry
        overlap_start = max(slot_start, now, window_start)
        overlap_end = min(slot_end, window_end)
        if overlap_end - overlap_start >= duration and (inside is None or overlap_start < inside):
            inside = overlap_start
    return inside, future, long_enough


def _evaluate_center(
    request: PrecheckRequest,
    *,
    center: Mapping[str, Any],
    sample_rows: Sequence[Mapping[str, Any]],
    equipment_rows: Sequence[Mapping[str, Any]],
    window_rows: Sequence[Mapping[str, Any]],
    inventory_rows: Sequence[Mapping[str, Any]],
    now: datetime,
    horizon_days: int,
) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    earliest: datetime | None = None

    if not center["active"]:
        gaps.append(_gap("center", "center_inactive", "中心已停用，不能安排跨中心验证", blocking=True))

    for demand in request.samples:
        available = _capability_total(sample_rows, "sample_type", demand.kind, demand.grade)
        label = demand.kind if demand.grade == ANY_GRADE else f"{demand.kind}/{demand.grade}"
        if available <= 0:
            gaps.append(_gap(
                "sample", "sample_type_unsupported",
                f"中心不具备样本类型 {label} 的处理能力",
                blocking=True, sample_type=demand.kind, grade=demand.grade,
                required_units=_text(demand.required_units), available_units="0.000",
            ))
        elif available < demand.required_units:
            gaps.append(_short_gap("sample", "sample_capacity_short", "样本类型", label, demand.required_units, available))

    for demand in request.equipment:
        available = _capability_total(equipment_rows, "equipment_kind", demand.kind, demand.grade)
        label = demand.kind if demand.grade == ANY_GRADE else f"{demand.kind}/{demand.grade}"
        if available <= 0:
            gaps.append(_gap(
                "equipment", "equipment_kind_unsupported",
                f"中心不具备设备 {label} 的验证能力",
                blocking=True, equipment_kind=demand.kind, grade=demand.grade,
                required_units=_text(demand.required_units), available_units="0.000",
            ))
        elif available < demand.required_units:
            gaps.append(_short_gap("equipment", "equipment_capacity_short", "设备", label, demand.required_units, available))

    inventory = {str(row["preservation_resource_kind"]): Decimal(str(row["available_units"])) for row in inventory_rows}
    for demand in request.resources:
        available = quantize_volume(inventory.get(demand.kind, ZERO))
        if available < demand.required_units:
            gaps.append(_short_gap("resource", "resource_short", "研究资源", demand.kind, demand.required_units, available))

    if request.required_minutes > 0:
        inside, future, long_enough = _schedule_fit(
            window_rows, str(center["timezone"]), now,
            request.window_start, request.window_end,
            request.required_minutes, horizon_days,
        )
        earliest = inside or future
        if not [window for window in window_rows if window.get("active")] or not long_enough or future is None:
            gaps.append(_gap(
                "schedule", "schedule_unavailable",
                "中心没有可容纳方案所需连续时长的排产窗口",
                blocking=True, required_minutes=request.required_minutes,
            ))
            earliest = None
        elif inside is None:
            gaps.append(_gap(
                "schedule", "schedule_conflict",
                "期望时间窗口内没有可用连续排产时段，需要推迟到最早可用窗口",
                blocking=False, required_minutes=request.required_minutes,
                earliest_available_at=utc_text(future),
            ))

    blocking = any(gap["blocking"] for gap in gaps)
    conclusion = INFEASIBLE if blocking else (HAS_GAP if gaps else FEASIBLE)
    return {"gaps": gaps, "conclusion": conclusion, "earliest_available_at": earliest}


def _short_gap(dimension: str, code: str, label: str, kind: str, required_units: Decimal, available: Decimal) -> dict[str, Any]:
    shortfall = quantize_volume(required_units - available)
    key_field = {
        "sample": "sample_type",
        "equipment": "equipment_kind",
        "resource": "preservation_resource_kind",
    }[dimension]
    return _gap(
        dimension, code,
        f"{label} {kind} 能力余量不足：需要 {_text(required_units)}，可用 {_text(available)}，缺口 {_text(shortfall)}",
        blocking=False,
        **{key_field: kind},
        required_units=_text(required_units),
        available_units=_text(available),
        shortfall_units=_text(shortfall),
    )


def _text(value: Decimal) -> str:
    return format(quantize_volume(value), "f")


def _satisfaction_percent(gaps: Sequence[Mapping[str, Any]]) -> int:
    penalty = 0
    for gap in gaps:
        code = str(gap["code"])
        if code in {"sample_capacity_short", "equipment_capacity_short", "resource_short"}:
            required_units = Decimal(str(gap["required_units"]))
            shortfall = Decimal(str(gap["shortfall_units"]))
            ratio_penalty = (shortfall / required_units * 100).to_integral_value(rounding=ROUND_CEILING)
            penalty += min(40, int(ratio_penalty))
        elif code == "schedule_conflict":
            penalty += 25
    return max(0, 100 - penalty)


def evaluate_precheck(
    request: PrecheckRequest,
    *,
    centers: Mapping[str, Mapping[str, Any]],
    regions: Mapping[str, str],
    samples_by_center: Mapping[str, Sequence[Mapping[str, Any]]],
    equipment_by_center: Mapping[str, Sequence[Mapping[str, Any]]],
    windows_by_center: Mapping[str, Sequence[Mapping[str, Any]]],
    inventory_by_center: Mapping[str, Sequence[Mapping[str, Any]]],
    now: datetime,
) -> dict[str, Any]:
    target = centers.get(request.center_id)
    if target is None:
        raise KeyError(request.center_id)
    horizon_days = max(14, (request.window_end.date() - now.date()).days + 7)
    target_region = regions.get(request.center_id)

    target_result = _evaluate_center(
        request,
        center=target,
        sample_rows=samples_by_center.get(request.center_id, ()),
        equipment_rows=equipment_by_center.get(request.center_id, ()),
        window_rows=windows_by_center.get(request.center_id, ()),
        inventory_rows=inventory_by_center.get(request.center_id, ()),
        now=now,
        horizon_days=horizon_days,
    )

    same_region: list[dict[str, Any]] = []
    other_region: list[dict[str, Any]] = []
    for center_id, center in sorted(centers.items()):
        if center_id == request.center_id or not center["active"]:
            continue
        candidate_region = regions.get(center_id)
        regional_pref = "same_region" if target_region and candidate_region == target_region else "other_region"
        result = _evaluate_center(
            request,
            center=center,
            sample_rows=samples_by_center.get(center_id, ()),
            equipment_rows=equipment_by_center.get(center_id, ()),
            window_rows=windows_by_center.get(center_id, ()),
            inventory_rows=inventory_by_center.get(center_id, ()),
            now=now,
            horizon_days=horizon_days,
        )
        # 阻断缺口在任何时间都无法化解，不作为替代中心推荐。
        if any(gap["blocking"] for gap in result["gaps"]):
            continue
        item = {
            "center_id": center_id,
            "name": center["name"],
            "region": candidate_region,
            "regional_pref": regional_pref,
            "conclusion": result["conclusion"],
            "satisfaction_percent": _satisfaction_percent(result["gaps"]),
            "earliest_available_at": None if result["earliest_available_at"] is None else utc_text(result["earliest_available_at"]),
            "gap_count": len(result["gaps"]),
            "gaps": result["gaps"],
        }
        (same_region if regional_pref == "same_region" else other_region).append(item)

    def _rank_key(item: Mapping[str, Any]) -> tuple[object, ...]:
        earliest = item["earliest_available_at"]
        return (-int(item["satisfaction_percent"]), earliest is None, earliest or "", item["center_id"])

    same_region.sort(key=_rank_key)
    other_region.sort(key=_rank_key)

    return {
        "precheck_id": request.precheck_id,
        "project_id": request.project_id,
        "protocol_id": request.protocol_id,
        "center_id": request.center_id,
        "region": target_region,
        "window_start": utc_text(request.window_start),
        "window_end": utc_text(request.window_end),
        "evaluated_at": utc_text(now),
        "required_minutes": request.required_minutes,
        "requirements": {
            "samples": [
                {"sample_type": demand.kind, "grade": demand.grade, "required_units": _text(demand.required_units)}
                for demand in request.samples
            ],
            "equipment": [
                {"equipment_kind": demand.kind, "grade": demand.grade, "required_units": _text(demand.required_units)}
                for demand in request.equipment
            ],
            "resources": [
                {"preservation_resource_kind": demand.kind, "required_units": _text(demand.required_units)}
                for demand in request.resources
            ],
        },
        "conclusion": target_result["conclusion"],
        "earliest_available_at": None if target_result["earliest_available_at"] is None else utc_text(target_result["earliest_available_at"]),
        "gap_count": len(target_result["gaps"]),
        "gaps": target_result["gaps"],
        "alternatives": {
            "same_region": same_region,
            "other_region": other_region,
            "eligible": len(same_region) + len(other_region),
            "evaluated": len([center for center_id, center in centers.items() if center_id != request.center_id and center["active"]]),
        },
    }
