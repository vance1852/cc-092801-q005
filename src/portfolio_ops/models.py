"""实验样本事件保藏中心调度领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
RISK_INDEXES = {"HUMIDITY", "INJURY", "CONGESTION", "HAZMAT", "SECONDARY", "CUSTOM"}
RESOURCE_KINDS = {"preservation-box", "tow-truck", "ambulance", "warning-kit", "evidence-kit", "rapid-response-team"}
CENTER_KINDS = {"road-section", "receiving-vault", "herbarium-room", "storage", "patrol-station"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class RiskIndexRecord:
    risk_index: str
    duty_date: str
    index_value: Decimal
    source_revision: str
    observed_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RiskIndexRecord":
        risk_index = required_text(raw.get("risk_index"), "risk_index", 16).upper()
        if risk_index not in RISK_INDEXES - {"CUSTOM"}:
            raise ValidationFailed("risk_index 必须是 HUMIDITY、INJURY、CONGESTION、HAZMAT 或 SECONDARY")
        observed_at = required_text(raw.get("observed_at"), "observed_at", 40)
        try:
            parse_utc(observed_at, "observed_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            risk_index=risk_index,
            duty_date=date_text(raw.get("duty_date"), "duty_date"),
            index_value=decimal_value(raw.get("index_value"), "index_value", minimum=Decimal("0.01")),
            source_revision=identifier(raw.get("source_revision"), "source_revision"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class ResponseCenter:
    center_id: str
    name: str
    kind: str
    timezone: str
    capacity_units: Decimal
    region: str = "default"

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResponseCenter":
        kind = required_text(raw.get("kind"), "kind", 24)
        if kind not in CENTER_KINDS:
            raise ValidationFailed("kind 不是受支持的设施类型")
        timezone = required_text(raw.get("timezone"), "timezone", 64)
        if "/" not in timezone and timezone != "UTC":
            raise ValidationFailed("timezone 必须是 IANA 时区或 UTC")
        return cls(
            center_id=identifier(raw.get("center_id"), "center_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            timezone=timezone,
            region=required_text(raw.get("region", "default"), "region", 64),
            capacity_units=decimal_value(
                raw.get("capacity_units"), "capacity_units", minimum=Decimal("0")
            ),
        )


@dataclass(frozen=True, slots=True)
class RoadCorridor:
    corridor_id: str
    origin_center_id: str
    destination_center_id: str
    preservation_resource_kind: str
    hourly_capacity: Decimal
    delay_basis_points: int
    response_minutes: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RoadCorridor":
        preservation_resource_kind = required_text(raw.get("preservation_resource_kind"), "preservation_resource_kind", 32)
        if preservation_resource_kind not in RESOURCE_KINDS:
            raise ValidationFailed("preservation_resource_kind 不是受支持的电源类型")
        loss = raw.get("delay_basis_points", 0)
        if isinstance(loss, bool) or not isinstance(loss, int) or not 0 <= loss <= 1000:
            raise ValidationFailed("delay_basis_points 必须是 0 到 1000 的整数")
        origin = identifier(raw.get("origin_center_id"), "origin_center_id")
        destination = identifier(raw.get("destination_center_id"), "destination_center_id")
        if origin == destination:
            raise ValidationFailed("转运路线起点和终点不能相同")
        return cls(
            corridor_id=identifier(raw.get("corridor_id"), "corridor_id"),
            origin_center_id=origin,
            destination_center_id=destination,
            preservation_resource_kind=preservation_resource_kind,
            hourly_capacity=decimal_value(
                raw.get("hourly_capacity"), "hourly_capacity", minimum=Decimal("0.001")
            ),
            delay_basis_points=loss,
            response_minutes=positive_integer(raw.get("response_minutes"), "response_minutes"),
        )


@dataclass(frozen=True, slots=True)
class PreservationResourceLot:
    preservation_resource_lot_id: str
    center_id: str
    preservation_resource_kind: str
    grade: str
    quantity_units: Decimal
    unit_cost_cny: Decimal
    received_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PreservationResourceLot":
        preservation_resource_kind = required_text(raw.get("preservation_resource_kind"), "preservation_resource_kind", 32)
        if preservation_resource_kind not in RESOURCE_KINDS:
            raise ValidationFailed("preservation_resource_kind 不是受支持的电源类型")
        received_at = required_text(raw.get("received_at"), "received_at", 40)
        try:
            parse_utc(received_at, "received_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        return cls(
            preservation_resource_lot_id=identifier(raw.get("preservation_resource_lot_id"), "preservation_resource_lot_id"),
            center_id=identifier(raw.get("center_id"), "center_id"),
            preservation_resource_kind=preservation_resource_kind,
            grade=required_text(raw.get("grade"), "grade", 32).upper(),
            quantity_units=decimal_value(
                raw.get("quantity_units"), "quantity_units", minimum=Decimal("0.001")
            ),
            unit_cost_cny=decimal_value(
                raw.get("unit_cost_cny"), "unit_cost_cny", minimum=Decimal("0")
            ),
            received_at=received_at,
        )


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    dispatch_id: str
    corridor_id: str
    specimen_event_id: str
    duty_date: str
    requested_units: Decimal
    priority: int
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "DispatchRequest":
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValidationFailed("priority 必须是 1 到 999 的整数")
        return cls(
            dispatch_id=identifier(raw.get("dispatch_id"), "dispatch_id"),
            corridor_id=identifier(raw.get("corridor_id"), "corridor_id"),
            specimen_event_id=identifier(raw.get("specimen_event_id"), "specimen_event_id"),
            duty_date=date_text(raw.get("duty_date"), "duty_date"),
            requested_units=decimal_value(
                raw.get("requested_units"), "requested_units", minimum=Decimal("0.001")
            ),
            priority=priority,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class ResponseScenario:
    scenario_id: str
    name: str
    risk_index_drop_percent: Decimal
    route_capacity_changes: Mapping[str, Decimal]
    demand_changes: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResponseScenario":
        route_changes = raw.get("route_capacity_changes", {})
        demand_changes = raw.get("demand_changes", {})
        if not isinstance(route_changes, Mapping) or not isinstance(demand_changes, Mapping):
            raise ValidationFailed("情景变化必须是对象")
        parsed_road_corridors = {
            identifier(key, "route_capacity_changes 键"): decimal_value(
                value, f"route_capacity_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in route_changes.items()
        }
        parsed_demand = {
            identifier(key, "demand_changes 键"): decimal_value(
                value, f"demand_changes.{key}", minimum=Decimal("-100"), maximum=Decimal("500")
            )
            for key, value in demand_changes.items()
        }
        return cls(
            scenario_id=identifier(raw.get("scenario_id"), "scenario_id"),
            name=required_text(raw.get("name"), "name"),
            risk_index_drop_percent=decimal_value(
                raw.get("risk_index_drop_percent", 0),
                "risk_index_drop_percent",
                minimum=Decimal("-500"),
                maximum=Decimal("100"),
            ),
            route_capacity_changes=parsed_road_corridors,
            demand_changes=parsed_demand,
        )


def timestamp_text(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        return utc_text(parse_utc(text, field))
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class ValidationScheme:
    """候选项目的跨中心验证方案：样本类型需求量、所需设备与工时要求。"""

    scheme_id: str
    name: str
    candidate_project_id: str
    protocol_version: str
    sample_requirements: tuple["SchemeSampleRequirement", ...]
    equipment_requirements: tuple["SchemeEquipmentRequirement", ...]
    duration_minutes: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ValidationScheme":
        samples = raw.get("sample_requirements")
        equipments = raw.get("equipment_requirements")
        if not isinstance(samples, list) or not samples:
            raise ValidationFailed("sample_requirements 至少包含一种样本类型")
        if not isinstance(equipments, list) or not equipments:
            raise ValidationFailed("equipment_requirements 至少包含一种设备")
        parsed_samples = tuple(
            SchemeSampleRequirement.from_dict(item, index)
            for index, item in enumerate(samples)
        )
        parsed_equipment = tuple(
            SchemeEquipmentRequirement.from_dict(item, index)
            for index, item in enumerate(equipments)
        )
        sample_keys = [item.sample_type for item in parsed_samples]
        if len(sample_keys) != len(set(sample_keys)):
            raise ValidationFailed("验证方案内样本类型不能重复")
        equipment_keys = [(item.equipment_kind, item.grade) for item in parsed_equipment]
        if len(equipment_keys) != len(set(equipment_keys)):
            raise ValidationFailed("验证方案内设备类型与等级组合不能重复")
        duration = raw.get("duration_minutes", 60)
        if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 24 * 60:
            raise ValidationFailed("duration_minutes 必须是 1 到 1440 的整数")
        return cls(
            scheme_id=identifier(raw.get("scheme_id"), "scheme_id"),
            name=required_text(raw.get("name"), "name"),
            candidate_project_id=identifier(raw.get("candidate_project_id"), "candidate_project_id"),
            protocol_version=identifier(raw.get("protocol_version"), "protocol_version"),
            sample_requirements=parsed_samples,
            equipment_requirements=parsed_equipment,
            duration_minutes=duration,
        )


@dataclass(frozen=True, slots=True)
class SchemeSampleRequirement:
    sample_type: str
    required_units: Decimal
    grade: str

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> "SchemeSampleRequirement":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"sample_requirements[{index}] 必须是对象")
        field = f"sample_requirements[{index}]"
        return cls(
            sample_type=required_text(raw.get("sample_type"), f"{field}.sample_type", 48),
            required_units=decimal_value(raw.get("required_units"), f"{field}.required_units", minimum=Decimal("0.001")),
            grade=required_text(raw.get("grade", "STANDARD"), f"{field}.grade", 32).upper(),
        )


@dataclass(frozen=True, slots=True)
class SchemeEquipmentRequirement:
    equipment_kind: str
    units: int
    grade: str

    @classmethod
    def from_dict(cls, raw: Any, index: int) -> "SchemeEquipmentRequirement":
        if not isinstance(raw, Mapping):
            raise ValidationFailed(f"equipment_requirements[{index}] 必须是对象")
        field = f"equipment_requirements[{index}]"
        return cls(
            equipment_kind=required_text(raw.get("equipment_kind"), f"{field}.equipment_kind", 48),
            units=positive_integer(raw.get("units", 1), f"{field}.units"),
            grade=required_text(raw.get("grade", "STANDARD"), f"{field}.grade", 32).upper(),
        )


@dataclass(frozen=True, slots=True)
class SampleCapabilityRecord:
    center_id: str
    sample_type: str
    daily_capacity_units: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SampleCapabilityRecord":
        return cls(
            center_id=identifier(raw.get("center_id"), "center_id"),
            sample_type=required_text(raw.get("sample_type"), "sample_type", 48),
            daily_capacity_units=decimal_value(
                raw.get("daily_capacity_units"), "daily_capacity_units", minimum=Decimal("0.001")
            ),
        )


EQUIPMENT_STATES = {"available", "maintenance", "retired"}


@dataclass(frozen=True, slots=True)
class EquipmentRecord:
    equipment_id: str
    center_id: str
    equipment_kind: str
    grade: str
    state: str
    unavailable_from: str | None
    unavailable_until: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EquipmentRecord":
        state = required_text(raw.get("state", "available"), "state", 16).lower()
        if state not in EQUIPMENT_STATES:
            raise ValidationFailed("state 必须是 available、maintenance 或 retired")
        unavailable_from = raw.get("unavailable_from")
        unavailable_until = raw.get("unavailable_until")
        start = None if unavailable_from is None else timestamp_text(unavailable_from, "unavailable_from")
        end = None if unavailable_until is None else timestamp_text(unavailable_until, "unavailable_until")
        if start is not None and end is not None and end <= start:
            raise ValidationFailed("unavailable_until 必须晚于 unavailable_from")
        return cls(
            equipment_id=identifier(raw.get("equipment_id"), "equipment_id"),
            center_id=identifier(raw.get("center_id"), "center_id"),
            equipment_kind=required_text(raw.get("equipment_kind"), "equipment_kind", 48),
            grade=required_text(raw.get("grade", "STANDARD"), "grade", 32).upper(),
            state=state,
            unavailable_from=start,
            unavailable_until=end,
        )


@dataclass(frozen=True, slots=True)
class CalendarWindowRecord:
    center_id: str
    opens_at: str
    closes_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CalendarWindowRecord":
        opens_at = timestamp_text(raw.get("opens_at"), "opens_at")
        closes_at = timestamp_text(raw.get("closes_at"), "closes_at")
        if closes_at <= opens_at:
            raise ValidationFailed("closes_at 必须晚于 opens_at")
        return cls(
            center_id=identifier(raw.get("center_id"), "center_id"),
            opens_at=opens_at,
            closes_at=closes_at,
        )


@dataclass(frozen=True, slots=True)
class ResourceBookingRecord:
    center_id: str
    starts_at: str
    ends_at: str
    units: Decimal
    reference_id: str
    sample_type: str | None
    equipment_kind: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResourceBookingRecord":
        sample_type = raw.get("sample_type")
        equipment_kind = raw.get("equipment_kind")
        if (sample_type is None or not str(sample_type).strip()) and (
            equipment_kind is None or not str(equipment_kind).strip()
        ):
            raise ValidationFailed("sample_type 与 equipment_kind 至少提供一个")
        starts_at = timestamp_text(raw.get("starts_at"), "starts_at")
        ends_at = timestamp_text(raw.get("ends_at"), "ends_at")
        if ends_at <= starts_at:
            raise ValidationFailed("ends_at 必须晚于 starts_at")
        return cls(
            center_id=identifier(raw.get("center_id"), "center_id"),
            starts_at=starts_at,
            ends_at=ends_at,
            units=decimal_value(raw.get("units", 1), "units", minimum=Decimal("0.001")),
            reference_id=identifier(raw.get("reference_id"), "reference_id"),
            sample_type=None if sample_type is None else required_text(sample_type, "sample_type", 48),
            equipment_kind=None if equipment_kind is None else required_text(equipment_kind, "equipment_kind", 48),
        )


@dataclass(frozen=True, slots=True)
class PrecheckQuery:
    candidate_project_id: str
    scheme_id: str
    target_center_id: str
    window_starts_at: str
    window_ends_at: str
    search_region: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PrecheckQuery":
        start = timestamp_text(raw.get("window_starts_at"), "window_starts_at")
        end = timestamp_text(raw.get("window_ends_at"), "window_ends_at")
        if end <= start:
            raise ValidationFailed("window_ends_at 必须晚于 window_starts_at")
        search_region = raw.get("search_region")
        return cls(
            candidate_project_id=identifier(raw.get("candidate_project_id"), "candidate_project_id"),
            scheme_id=identifier(raw.get("scheme_id"), "scheme_id"),
            target_center_id=identifier(raw.get("target_center_id"), "target_center_id"),
            window_starts_at=start,
            window_ends_at=end,
            search_region=None if search_region is None else required_text(search_region, "search_region", 64),
        )
