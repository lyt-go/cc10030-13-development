"""增量对账批量入口的 HTTP 路由层测试。

环境未安装 httpx，无法启动 TestClient，这里直接调用路由处理函数并传入
Pydantic 载荷，验证路由注册、请求契约、错误码与服务编排是否一致。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.main import app
from app.routers import backfill as backfill_router
from app.routers.backfill_schemas import (
    ApplicationPayload,
    BoundaryRevisionPayload,
    ConfirmPayload,
    IssuePayload,
    SupplementBatchPayload,
)


@pytest.fixture(autouse=True)
def setup_data_dir():
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_api_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["backfill_reconcile", "region_boundary"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


JIA = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
YI = [[0.0, 1.0], [1.0, 1.0], [1.0, 2.0], [0.0, 2.0]]


def _seed_tree(suffix: str):
    from app.services.region_dim import RegionDimService

    region = RegionDimService()
    city, jia, yi = f"CITY-{suffix}", f"JIA-{suffix}", f"YI-{suffix}"
    region.ensure_region(city, "测试市", "市级", "", None)
    region.ensure_region(jia, "甲县", "县级", city, JIA)
    region.ensure_region(yi, "乙县", "县级", city, YI)
    return city, jia, yi


def test_routes_registered_on_app():
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
        # 新版 Starlette 以 _IncludedRouter 包装 include_router 的路由
        original = getattr(route, "original_router", None)
        for sub in getattr(original, "routes", []) or []:
            sub_path = getattr(sub, "path", None)
            if sub_path:
                paths.add(sub_path)
    assert "/api/backfill/supplement-batches" in paths
    assert "/api/backfill/applications/{app_id}/issue" in paths
    assert "/api/backfill/observations/{obs_key}" in paths
    assert "/api/backfill/regions/{region_code}/boundary-revisions" in paths
    assert "/api/backfill/carryovers/{carryover_id}/settle" in paths


def test_full_reconcile_flow_through_router_handlers():
    city, jia, yi = _seed_tree("G1")

    # 1. 批量提交补采记录
    submit = backfill_router.submit_supplement_batch(SupplementBatchPayload(
        batch_no="BATCH-G1", parent_region=city,
        start_date="2026-09-01", end_date="2026-09-07",
        points=[
            {"obs_key": "g1-jia", "lat": 0.5, "lon": 0.5},
            {"obs_key": "g1-yi", "lat": 0.5, "lon": 1.5},
            {"obs_key": "g1-cross", "lat": 2.5, "lon": 2.5},
        ],
    ))
    assert submit["ok"] is True
    assert submit["data"]["suspended_points"] == 1

    # 2. 甲县确认
    confirm = backfill_router.confirm_region_supplements(
        jia, ConfirmPayload(parent_region=city, region_code=jia))
    assert confirm["data"]["confirmed_count"] == 1

    # 3. 观测查重：p-jia 已计入甲县确认单
    lookup = backfill_router.lookup_observation("g1-jia")
    assert lookup["data"]["confirmation"]["region_code"] == jia

    # 4. 未知观测返回 404
    with pytest.raises(HTTPException) as exc:
        backfill_router.lookup_observation("ghost")
    assert exc.value.status_code == 404

    # 5. 市级申请 + confirmed_only 签发：101 条 + 乙县补单
    app_resp = backfill_router.create_application(ApplicationPayload(
        app_no="APP-G1", parent_region=city, baseline_total=100))
    app_id = app_resp["data"]["application_id"]
    issued = backfill_router.issue_application(app_id, IssuePayload(mode="confirmed_only"))
    assert issued["data"]["application"]["issued_total"] == 101
    assert len(issued["data"]["carryover_orders"]) == 1

    # 6. 重送同样记录：重复跳过，不新增差额
    resend = backfill_router.submit_supplement_batch(SupplementBatchPayload(
        batch_no="BATCH-G1-2", parent_region=city,
        start_date="2026-09-01", end_date="2026-09-07",
        points=[{"obs_key": "g1-jia", "lat": 0.5, "lon": 0.5}],
    ))
    assert resend["data"]["accepted_points"] == 0
    assert resend["data"]["duplicate_points"] == 1

    # 7. 重复签发幂等
    reissue = backfill_router.issue_application(app_id, IssuePayload(mode="confirmed_only"))
    assert reissue["data"]["idempotent"] is True
    assert reissue["data"]["application"]["issued_total"] == 101

    # 8. 父区按子区的对账汇总
    summary = backfill_router.parent_reconcile_summary(city)
    assert summary["data"]["total_records"] == 3


def test_invalid_merge_target_returns_400():
    city, jia, yi = _seed_tree("G2")
    backfill_router.submit_supplement_batch(SupplementBatchPayload(
        batch_no="BATCH-G2", parent_region=city,
        start_date="2026-09-01", end_date="2026-09-07",
        points=[{"obs_key": "g2-jia", "lat": 0.5, "lon": 0.5},
                {"obs_key": "g2-yi", "lat": 0.5, "lon": 1.5}],
    ))
    backfill_router.confirm_region_supplements(
        jia, ConfirmPayload(parent_region=city, region_code=jia))
    app_resp = backfill_router.create_application(ApplicationPayload(
        app_no="APP-G2", parent_region=city, baseline_total=100))
    with pytest.raises(HTTPException) as exc:
        backfill_router.issue_application(
            app_resp["data"]["application_id"],
            IssuePayload(mode="merge_pending", merge_target=yi))
    assert exc.value.status_code == 400


def test_carryover_settle_endpoint_reopens_next_batch():
    city, jia, yi = _seed_tree("G4")
    backfill_router.submit_supplement_batch(SupplementBatchPayload(
        batch_no="BATCH-G4", parent_region=city,
        start_date="2026-09-01", end_date="2026-09-07",
        points=[{"obs_key": "g4-jia", "lat": 0.5, "lon": 0.5},
                {"obs_key": "g4-yi", "lat": 0.5, "lon": 1.5}],
    ))
    backfill_router.confirm_region_supplements(
        jia, ConfirmPayload(parent_region=city, region_code=jia))
    app_resp = backfill_router.create_application(ApplicationPayload(
        app_no="APP-G4", parent_region=city, baseline_total=100))
    issued = backfill_router.issue_application(
        app_resp["data"]["application_id"], IssuePayload(mode="confirmed_only"))
    carryover_id = issued["data"]["carryover_orders"][0]["carryover_id"]

    # 结算补单后乙县点回到已确认，重复结算返回 400
    settled = backfill_router.settle_carryover(carryover_id, None)
    assert settled["data"]["confirmed_count"] == 1
    assert settled["data"]["region_code"] == yi
    with pytest.raises(HTTPException) as exc:
        backfill_router.settle_carryover(carryover_id, None)
    assert exc.value.status_code == 400

    # 次批申请把补回的点纳入，101 -> 102
    next_app = backfill_router.create_application(ApplicationPayload(
        app_no="APP-G4-NEXT", parent_region=city, baseline_total=101))
    next_issued = backfill_router.issue_application(
        next_app["data"]["application_id"], IssuePayload(mode="confirmed_only"))
    assert next_issued["data"]["application"]["issued_total"] == 102
    assert next_issued["data"]["carryover_orders"] == []


def test_boundary_revision_endpoint_effects_later_confirmation():
    city, jia, yi = _seed_tree("G3")
    backfill_router.submit_supplement_batch(SupplementBatchPayload(
        batch_no="BATCH-G3", parent_region=city,
        start_date="2026-09-01", end_date="2026-09-07",
        points=[{"obs_key": "g3-move", "lat": 0.65, "lon": 0.65}],
    ))
    # 勘正后该点由乙县接管
    backfill_router.apply_boundary_revision(
        yi, BoundaryRevisionPayload(
            polygon=[[0.0, 0.5], [1.0, 0.5], [1.0, 2.0], [0.0, 2.0]], note="西扩"))
    backfill_router.apply_boundary_revision(
        jia, BoundaryRevisionPayload(
            polygon=[[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]], note="东退"))
    confirm = backfill_router.confirm_region_supplements(
        jia, ConfirmPayload(parent_region=city, region_code=jia))
    assert confirm["data"]["region_changed"][0]["new_region"] == yi
