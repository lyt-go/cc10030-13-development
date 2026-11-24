"""历史回补增量对账批量入口路由。

按行政区树组织的补采批量提交、子区逐点核验、父区差额申请/签发、观测查重与
区域边界勘正入口都收敛在这里；通用资源 CRUD 仍走 resource.py。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.routers.backfill_schemas import (
    ApplicationPayload,
    BoundaryRevisionPayload,
    CarryoverSettlePayload,
    ConfirmPayload,
    IssuePayload,
    RegionNodePayload,
    SupplementBatchPayload,
)
from app.services.geo_backfill import GeoBackfillService
from app.services.region_dim import RegionDimService

router = APIRouter(prefix="/api/backfill", tags=["历史回补增量对账"])

backfill_service = GeoBackfillService()
region_service = RegionDimService()


@router.post("/supplement-batches")
def submit_supplement_batch(payload: SupplementBatchPayload) -> dict:
    """批量选择多条补采记录一次提交，按点逐条核验行政区归属。"""
    try:
        result = backfill_service.submit_supplement_batch(
            batch_no=payload.batch_no,
            parent_region=payload.parent_region,
            points=[p.model_dump() for p in payload.points],
            start_date=payload.start_date,
            end_date=payload.end_date,
            source=payload.source,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": f"补采批次 {payload.batch_no} 已批量提交", "data": result}


@router.get("/supplement-records")
def list_supplement_records(
    parent_region: str | None = Query(default=None),
    region_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict:
    """按父区/子区/状态查询补采逐点清单。"""
    items = backfill_service.list_supplement_records(parent_region, region_code, status)
    return {"ok": True, "data": {"items": items, "total": len(items)}}


@router.get("/observations/{obs_key}")
def lookup_observation(obs_key: str) -> dict:
    """回查同一观测已经计入哪次确认、哪次申请或哪张待次批补单。"""
    result = backfill_service.lookup_observation(obs_key)
    if not result["found"]:
        raise HTTPException(status_code=404, detail=f"观测 {obs_key} 尚未登记")
    return {"ok": True, "data": result}


@router.post("/regions/{region_code}/confirmations")
def confirm_region_supplements(region_code: str, payload: ConfirmPayload) -> dict:
    """子区对本区域逐点清单做独立核验确认。"""
    if payload.region_code != region_code:
        raise HTTPException(status_code=400, detail="路径与请求体中的子区编号不一致")
    try:
        result = backfill_service.confirm_region_supplements(
            parent_region=payload.parent_region,
            region_code=region_code,
            record_ids=payload.record_ids,
            operator=payload.operator,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": f"子区 {region_code} 核验完成", "data": result}


@router.post("/applications")
def create_application(payload: ApplicationPayload) -> dict:
    """父区打开增量对账申请，快照当时可结算的逐点范围。"""
    try:
        result = backfill_service.create_recon_application(
            app_no=payload.app_no,
            parent_region=payload.parent_region,
            baseline_total=payload.baseline_total,
            remark=payload.remark or "",
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": f"对账申请 {payload.app_no} 已创建", "data": result}


@router.post("/applications/{app_id}/issue")
def issue_application(app_id: str, payload: IssuePayload) -> dict:
    """实际签发申请；confirmed_only 留补单，merge_pending 首次签发前并入已通过子区。"""
    try:
        result = backfill_service.issue_application(
            app_id=app_id, mode=payload.mode, merge_target=payload.merge_target)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    message = "重复签发已幂等返回，未新增差额" if result.get("idempotent") else "申请已签发"
    return {"ok": True, "message": message, "data": result}


@router.get("/applications/{app_id}")
def get_application(app_id: str) -> dict:
    result = backfill_service.get_application(app_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"申请 {app_id} 不存在")
    return {"ok": True, "data": result}


@router.get("/regions/{parent_region}/summary")
def parent_reconcile_summary(parent_region: str) -> dict:
    """区域维度批量查看：按子区分别给出待对账数据与差额。"""
    return {"ok": True, "data": backfill_service.parent_reconcile_summary(parent_region)}


@router.post("/carryovers/{carryover_id}/settle")
def settle_carryover(carryover_id: str, payload: CarryoverSettlePayload | None = None) -> dict:
    """待次批补单对应子区完成核验后结算补单，使其可进入下一批申请。"""
    operator = payload.operator if payload else "子区核验员"
    try:
        result = backfill_service.settle_carryover(carryover_id, operator=operator)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": f"补单 {carryover_id} 已结算", "data": result}


@router.get("/region-tree/{root_code}")
def region_tree(root_code: str) -> dict:
    """输出行政区嵌套树（叶子为最小可核实子区）。"""
    try:
        tree = region_service.region_tree(root_code)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "data": tree}


@router.post("/regions")
def ensure_region(payload: RegionNodePayload) -> dict:
    """登记行政区树节点（市级根节点或县级子区），已存在则直接返回。"""
    entry = region_service.ensure_region(
        region_code=payload.region_code,
        name=payload.region_name,
        level=payload.region_level,
        parent_code=payload.parent_code or "",
        polygon=payload.polygon,
        boundary_version=payload.boundary_version,
    )
    return {"ok": True, "data": {"region_code": entry["区域编号"],
                                 "region_name": entry.get("区域名称"),
                                 "boundary_version": entry.get("边界版本")}}


@router.post("/regions/{region_code}/boundary-revisions")
def apply_boundary_revision(region_code: str, payload: BoundaryRevisionPayload) -> dict:
    """登记并生效一份边界勘正单，后续核验按新边界版本执行。"""
    try:
        result = region_service.apply_boundary_revision(
            region_code=region_code,
            polygon=payload.polygon,
            effective_at=payload.effective_at,
            note=payload.note or "",
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": f"勘正单 {result['revision_id']} 已生效", "data": result}
