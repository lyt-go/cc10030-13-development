"""历史回补增量对账专用接口契约。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SupplementPoint(BaseModel):
    """单条迟到定位点；region_code 可显式声明归属，服务端仍按当前边界复核。"""

    lat: float
    lon: float
    obs_key: str | None = None
    point_id: str | int | None = None
    region_code: str | None = None
    collected_at: str | None = None
    timestamp: float | None = None


class SupplementBatchPayload(BaseModel):
    batch_no: str
    parent_region: str
    start_date: str
    end_date: str
    source: str = "人工补采"
    points: list[SupplementPoint] = Field(min_length=1)


class ConfirmPayload(BaseModel):
    parent_region: str
    region_code: str
    record_ids: list[str] | None = None
    operator: str = "子区核验员"


class ApplicationPayload(BaseModel):
    app_no: str
    parent_region: str
    baseline_total: int
    remark: str | None = ""


class IssuePayload(BaseModel):
    mode: str = "confirmed_only"
    merge_target: str | None = None


class CarryoverSettlePayload(BaseModel):
    operator: str = "子区核验员"


class BoundaryRevisionPayload(BaseModel):
    polygon: list[list[float]] = Field(min_length=3)
    effective_at: float | None = None
    note: str | None = ""


class RegionNodePayload(BaseModel):
    region_code: str
    region_name: str
    region_level: str
    parent_code: str | None = ""
    polygon: list[list[float]] | None = None
    boundary_version: str = "原始边界"


class ReconcileResult(BaseModel):
    ok: bool = True
    message: str = ""
    data: dict[str, Any] | None = None
