"""历史回补·行政区树增量对账场景测试。

覆盖题述全部规则：
- 批量提交逐点归属到最小可核实子区、重复提交可回查已计入的确认；
- 子区独立核验、边界勘正单生效后按新边界复核；
- 同一观测不能被两个县分别作为增量重复计入市级；
- 市级申请两种签发口径：只结算确认过的一县并留待次批补单（101 条），
  或首次签发前并入另一个已通过子区 / 待审子区核验后签发（102 条且无需补单）；
- 重复签发、重复提交均幂等，不多记差额。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_recon_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["geo_points", "checkpoints", "backfill_reconcile", "region_boundary",
                "region_aggregates"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


# 多边形顶点按服务约定的 [lat, lon] 顺序；甲乙两县按经度东西划界
# 甲县：lat∈[0,1]，lon∈[0,1]
JIA_POLYGON = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
# 乙县：lat∈[0,1]，lon∈[1,2]
YI_POLYGON = [[0.0, 1.0], [1.0, 1.0], [1.0, 2.0], [0.0, 2.0]]
# 勘正后乙县西扩至 lon=0.5
YI_REVISED_POLYGON = [[0.0, 0.5], [1.0, 0.5], [1.0, 2.0], [0.0, 2.0]]
# 勘正后甲县东界退至 lon=0.5（与乙县无缝衔接、无重叠）
JIA_REVISED_POLYGON = [[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]]


def _build_regions(suffix: str):
    """构建 市→甲乙两县 的行政区树并返回各区域编号。"""
    from app.services.region_dim import RegionDimService

    region = RegionDimService()
    city = f"CITY-{suffix}"
    jia = f"JIA-{suffix}"
    yi = f"YI-{suffix}"
    region.ensure_region(city, f"测试市{suffix}", "市级", "", None)
    region.ensure_region(jia, f"甲县{suffix}", "县级", city, JIA_POLYGON)
    region.ensure_region(yi, f"乙县{suffix}", "县级", city, YI_POLYGON)
    return region, city, jia, yi


def _point(key: str, lat: float, lon: float, region_code: str | None = None):
    return {"obs_key": key, "lat": lat, "lon": lon, "region_code": region_code,
            "collected_at": "2026-09-10T08:00:00"}


class TestSupplementBatchAndLedger:
    """批量补采提交、逐点归属与观测查重。"""

    def test_batch_submit_assigns_to_smallest_region_and_dedupes(self):
        from app.services.geo_backfill import GeoBackfillService

        _, city, jia, yi = _build_regions("B1")
        svc = GeoBackfillService()

        result = svc.submit_supplement_batch(
            batch_no="BATCH-B1", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[
                _point("p-jia", 0.5, 0.5),    # 甲县
                _point("p-yi", 0.5, 1.5),     # 乙县
                _point("p-cross", 2.5, 2.5),  # 两县边界外，跨县挂起
            ],
        )
        assert result["accepted_points"] == 3
        assert result["suspended_points"] == 1
        pending = {item["region_code"]: item["pending_count"]
                   for item in result["per_region_pending"]}
        assert pending == {jia: 1, yi: 1}

        # 跨县/归属不明记录挂起，不分摊给任何子区
        suspended_records = svc.list_supplement_records(city, status="挂起")
        assert len(suspended_records) == 1
        assert suspended_records[0]["region_code"] is None

        # 再次提交含同一观测的批次：已登记的点不再入账，并能回查其去向
        again = svc.submit_supplement_batch(
            batch_no="BATCH-B1-RESEND", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[_point("p-jia", 0.5, 0.5), _point("p-new", 0.8, 0.2)],
        )
        assert again["accepted_points"] == 1
        assert again["duplicate_points"] == 1
        dup = again["duplicates"][0]
        assert dup["obs_key"] == "p-jia"
        assert dup["already_region"] == jia
        assert dup["status"] == "重复跳过"

        lookup = svc.lookup_observation("p-jia")
        assert lookup["found"] is True
        assert lookup["batch_no"] == "BATCH-B1"

    def test_same_batch_no_cannot_be_submitted_twice(self):
        from app.services.geo_backfill import GeoBackfillService

        _, city, _, _ = _build_regions("B2")
        svc = GeoBackfillService()
        kwargs = dict(batch_no="BATCH-B2", parent_region=city,
                      start_date="2026-09-01", end_date="2026-09-07",
                      points=[_point("p1", 0.5, 0.5)])
        svc.submit_supplement_batch(**kwargs)
        with pytest.raises(ValueError):
            svc.submit_supplement_batch(**kwargs)

    def test_explicit_region_must_pass_boundary_recheck(self):
        from app.services.geo_backfill import GeoBackfillService

        _, city, jia, yi = _build_regions("B3")
        svc = GeoBackfillService()
        # 点实际在乙县，却声明归属甲县：声明被边界复核否决，按真实边界落乙县
        result = svc.submit_supplement_batch(
            batch_no="BATCH-B3", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[_point("p-wrong-claim", 0.5, 1.5, region_code=jia)],
        )
        record = svc.list_supplement_records(city, region_code=yi)
        assert len(record) == 1
        assert record[0]["status"] == "待核验"
        assert result["per_region_pending"][0]["region_code"] == yi


class TestRegionConfirmation:
    """子区独立核验与重复计入拦截。"""

    def test_confirmations_are_scoped_per_sub_region(self):
        from app.services.geo_backfill import GeoBackfillService

        _, city, jia, yi = _build_regions("C1")
        svc = GeoBackfillService()
        svc.submit_supplement_batch(
            batch_no="BATCH-C1", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[_point("p-jia", 0.5, 0.5), _point("p-yi", 0.5, 1.5)],
        )

        jia_result = svc.confirm_region_supplements(city, jia)
        assert jia_result["confirmed_count"] == 1
        assert jia_result["recon_data"]["confirmed_delta"] == 1
        assert jia_result["confirmation_id"].startswith(f"CF-{jia}-")

        # 乙县核验自己的点，不能把甲县已确认的观测再确认一遍
        yi_result = svc.confirm_region_supplements(city, yi)
        assert yi_result["confirmed_count"] == 1
        all_confirmed = svc.list_supplement_records(city, status="已确认")
        assert {r["obs_key"] for r in all_confirmed} == {"p-jia", "p-yi"}
        ledger = svc.lookup_observation("p-jia")
        assert ledger["confirmation"] is not None
        assert ledger["confirmation"]["region_code"] == jia


class TestCityApplicationIssue:
    """市级申请签发：101+补单 / 102 无补单 / 幂等重发。"""

    def _prepare_application(self, suffix: str, baseline: int = 100):
        from app.services.geo_backfill import GeoBackfillService

        _, city, jia, yi = _build_regions(suffix)
        svc = GeoBackfillService()
        svc.submit_supplement_batch(
            batch_no=f"BATCH-{suffix}", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[
                _point(f"{suffix}-jia", 0.5, 0.5),    # 甲县点
                _point(f"{suffix}-yi", 0.5, 1.5),     # 乙县点
                _point(f"{suffix}-cross", 2.5, 2.5),  # 跨县挂起点
            ],
        )
        # 申请打开时只有甲县已确认，乙县待审，跨县记录还在等待确认
        svc.confirm_region_supplements(city, jia)
        app = svc.create_recon_application(
            app_no=f"APP-{suffix}", parent_region=city, baseline_total=baseline)
        return svc, city, jia, yi, app

    def test_confirmed_only_issues_101_and_keeps_yi_carryover(self):
        svc, city, jia, yi, app = self._prepare_application("D1")

        issued = svc.issue_application(app["application_id"], mode="confirmed_only")
        application = issued["application"]
        # 题述口径一：市级 100 + 已确认甲县 1 = 101
        assert application["issued_total"] == 101
        assert application["delta_total"] == 1
        assert application["reconcile_check"]["balanced"] is True
        delta = {d["region_code"]: d for d in application["delta_by_region"]}
        assert set(delta) == {jia}
        assert delta[jia]["confirmed_delta"] == 1

        # 乙县待审点保留为待次批补单，跨县挂起点不进入申请也不进补单
        assert len(issued["carryover_orders"]) == 1
        carryover = issued["carryover_orders"][0]
        assert carryover["region_code"] == yi
        assert carryover["obs_keys"] == ["D1-yi"]
        assert len(issued["cross_region_pending"]) == 1
        assert issued["cross_region_pending"][0]["obs_key"] == "D1-cross"

        # 同一观测可回查：已计入哪次申请、哪张补单
        yi_lookup = svc.lookup_observation("D1-yi")
        assert yi_lookup["status"] == "待次批"
        assert yi_lookup["carryover"]["carryover_id"] == carryover["carryover_id"]
        jia_lookup = svc.lookup_observation("D1-jia")
        assert jia_lookup["status"] == "已签发"
        assert jia_lookup["application"]["application_id"] == app["application_id"]

    def test_reissue_same_application_is_idempotent(self):
        svc, _, _, _, app = self._prepare_application("D2")
        first = svc.issue_application(app["application_id"], mode="confirmed_only")
        second = svc.issue_application(app["application_id"], mode="confirmed_only")
        assert second["idempotent"] is True
        assert second["application"]["issued_total"] == first["application"]["issued_total"] == 101
        # 补单没有重复生成
        assert len(svc._load("carryover_orders")) == 1

    def test_merge_pending_before_first_issue_settles_102_without_carryover(self):
        svc, city, jia, yi, app = self._prepare_application("D3")

        # 题述口径二：首次签发前，把待审乙县点并入已通过的甲县
        issued = svc.issue_application(
            app["application_id"], mode="merge_pending", merge_target=jia)
        application = issued["application"]
        assert application["issued_total"] == 102
        assert application["delta_total"] == 2
        assert application["carryover_ids"] == []
        assert issued["carryover_orders"] == []
        delta = {d["region_code"]: d for d in application["delta_by_region"]}
        assert delta[jia]["confirmed_delta"] == 1
        assert delta[jia]["merged_delta"] == 1

        merged_lookup = svc.lookup_observation("D3-yi")
        assert merged_lookup["status"] == "已并入"
        assert merged_lookup["region_code"] == jia
        assert merged_lookup["application"]["application_id"] == app["application_id"]

        # 首次签发后不允许再开同范围申请：范围内的点都已结算，新申请无点可纳
        with pytest.raises(ValueError):
            svc.create_recon_application(app_no="APP-D3-X", parent_region=city,
                                         baseline_total=100)

    def test_merge_target_must_be_a_confirmed_region(self):
        svc, _, _, yi, app = self._prepare_application("D4")
        with pytest.raises(ValueError):
            svc.issue_application(app["application_id"], mode="merge_pending",
                                  merge_target=yi)

    def test_yi_verified_with_revised_boundary_before_issue_settles_102(self):
        """申请尚未签发期间，乙县点随勘正边界完成独立核验，之后正常 102 签发。"""
        from app.services.region_dim import RegionDimService

        svc, city, jia, yi, app = self._prepare_application("D5")
        region = RegionDimService()

        # 乙县边界勘正单生效；核验按新边界执行（乙县点仍在乙县新边界内）
        revision = region.apply_boundary_revision(
            yi, YI_REVISED_POLYGON, note="乙县西界勘正")
        assert revision["revision_id"].startswith(f"RV-{yi}-")
        assert region.get_region(yi)["边界版本"] == revision["revision_id"]

        yi_result = svc.confirm_region_supplements(city, yi)
        assert yi_result["confirmed_count"] == 1
        issued = svc.issue_application(app["application_id"], mode="confirmed_only")
        application = issued["application"]
        # 题述口径二的另一路径：甲乙都确认后签发 102，无需补单
        assert application["issued_total"] == 102
        assert application["delta_total"] == 2
        assert issued["carryover_orders"] == []
        assert application["mode"] == "confirmed_only"
        # 跨县点仍在等待确认，未被任何县计入
        assert len(issued["cross_region_pending"]) == 1


class TestBoundaryRevisionReassignment:
    """边界勘正把点从甲县改判给乙县时，不会在两个县各记一次增量。"""

    def test_revision_moves_point_without_double_count(self):
        from app.services.region_dim import RegionDimService
        from app.services.geo_backfill import GeoBackfillService

        region, city, jia, yi = _build_regions("E1")
        svc = GeoBackfillService()
        # 该点 lon=0.65，在原始边界下属于甲县
        svc.submit_supplement_batch(
            batch_no="BATCH-E1", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[_point("p-move", 0.65, 0.65)],
        )
        assert len(svc.list_supplement_records(city, region_code=jia)) == 1

        # 勘正：乙县西扩、甲县东退，该点唯一落入乙县，两县边界无重叠
        region.apply_boundary_revision(yi, YI_REVISED_POLYGON, note="乙县西界勘正")
        region.apply_boundary_revision(jia, JIA_REVISED_POLYGON, note="甲县东界退界")
        result = svc.confirm_region_supplements(city, jia)
        assert result["confirmed_count"] == 0
        assert len(result["region_changed"]) == 1
        assert result["region_changed"][0]["new_region"] == yi

        # 改判后只能由乙县确认；甲县台账里没有这个点的增量
        yi_result = svc.confirm_region_supplements(city, yi)
        assert yi_result["confirmed_count"] == 1
        app = svc.create_recon_application(app_no="APP-E1", parent_region=city,
                                           baseline_total=100)
        issued = svc.issue_application(app["application_id"])
        assert issued["application"]["delta_total"] == 1
        assert issued["application"]["issued_total"] == 101
        lookup = svc.lookup_observation("p-move")
        assert lookup["region_code"] == yi


class TestCarryoverSettlement:
    """待次批补单的结算闭环：次批申请能把补单点补回来，且不会重复计增量。"""

    def test_carryover_settles_into_next_application(self):
        from app.services.geo_backfill import GeoBackfillService

        _, city, jia, yi = _build_regions("G1")
        svc = GeoBackfillService()
        svc.submit_supplement_batch(
            batch_no="BATCH-G1", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[_point("g1-jia", 0.5, 0.5), _point("g1-yi", 0.5, 1.5)],
        )
        svc.confirm_region_supplements(city, jia)
        first_app = svc.create_recon_application(
            app_no="APP-G1-1", parent_region=city, baseline_total=100)
        first_issue = svc.issue_application(
            first_app["application_id"], mode="confirmed_only")
        assert first_issue["application"]["issued_total"] == 101
        carryover = first_issue["carryover_orders"][0]
        assert carryover["obs_keys"] == ["g1-yi"]

        # 乙县随后完成核验，结算补单：点回到已确认，补单标记解除
        settled = svc.settle_carryover(carryover["carryover_id"])
        assert settled["confirmed_count"] == 1
        assert settled["region_code"] == yi
        yi_record = svc.list_supplement_records(city, region_code=yi, status="已确认")
        assert len(yi_record) == 1
        assert yi_record[0]["carryover_id"] is None

        # 同一张补单不能重复结算
        with pytest.raises(ValueError):
            svc.settle_carryover(carryover["carryover_id"])

        # 次批申请以 101 为新基线，纳入补回的乙县点：签发 102，无新增补单
        second_app = svc.create_recon_application(
            app_no="APP-G1-2", parent_region=city, baseline_total=101)
        second_issue = svc.issue_application(
            second_app["application_id"], mode="confirmed_only")
        assert second_issue["application"]["issued_total"] == 102
        assert second_issue["application"]["delta_total"] == 1
        assert second_issue["carryover_orders"] == []

        # 乙县观测全程只计过一次增量
        lookup = svc.lookup_observation("g1-yi")
        assert lookup["status"] == "已签发"
        assert lookup["application"]["app_no"] == "APP-G1-2"
        assert lookup["carryover"] is None

    def test_carryover_settlement_respects_later_boundary_revision(self):
        from app.services.region_dim import RegionDimService
        from app.services.geo_backfill import GeoBackfillService

        region, city, jia, yi = _build_regions("G2")
        svc = GeoBackfillService()
        svc.submit_supplement_batch(
            batch_no="BATCH-G2", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[_point("g2-move", 0.65, 0.65)],
        )
        svc.confirm_region_supplements(city, jia)
        app = svc.create_recon_application(
            app_no="APP-G2-1", parent_region=city, baseline_total=100)
        issue1 = svc.issue_application(app["application_id"], mode="confirmed_only")
        assert issue1["application"]["issued_total"] == 101

        # 第二批乙县点未确认，留下待次批补单
        svc.submit_supplement_batch(
            batch_no="BATCH-G2-B", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[_point("g2-yi", 0.5, 1.5)],
        )
        app2 = svc.create_recon_application(
            app_no="APP-G2-2", parent_region=city, baseline_total=101)
        issue2 = svc.issue_application(app2["application_id"], mode="confirmed_only")
        assert issue2["application"]["issued_total"] == 101
        carryover = issue2["carryover_orders"][0]

        # 补单结算前乙县西扩、甲县东退，乙县点仍在乙县（正常确认路径）
        region.apply_boundary_revision(yi, YI_REVISED_POLYGON, note="乙县西界勘正")
        region.apply_boundary_revision(jia, JIA_REVISED_POLYGON, note="甲县东界退界")
        settled = svc.settle_carryover(carryover["carryover_id"])
        assert settled["confirmed_count"] == 1
        assert settled["confirmation_id"].startswith(f"CF-{yi}-")
        assert settled["region_changed"] == []

        # 未知补单报错
        with pytest.raises(KeyError):
            svc.settle_carryover("CO-NOT-EXIST")


class TestRegionTreeAndSummary:
    """行政区树与父区按子区的对账汇总。"""

    def test_region_tree_and_parent_summary(self):
        from app.services.region_dim import RegionDimService
        from app.services.geo_backfill import GeoBackfillService

        region, city, jia, yi = _build_regions("F1")
        svc = GeoBackfillService()
        node = region.region_tree(city)
        assert node["region_code"] == city
        assert {child["region_code"] for child in node["children"]} == {jia, yi}
        assert all(child["children"] == [] for child in node["children"])

        svc.submit_supplement_batch(
            batch_no="BATCH-F1", parent_region=city,
            start_date="2026-09-01", end_date="2026-09-07",
            points=[_point("p1", 0.3, 0.3), _point("p2", 0.4, 0.4),
                    _point("p3", 0.6, 1.6)],
        )
        svc.confirm_region_supplements(city, jia)
        summary = svc.parent_reconcile_summary(city)
        per_region = {item["region_code"]: item for item in summary["per_region"]}
        assert per_region[jia]["confirmed_count"] == 2
        assert per_region[yi]["pending_count"] == 1
        assert summary["total_records"] == 3

    def test_lookup_unknown_observation(self):
        from app.services.geo_backfill import GeoBackfillService

        svc = GeoBackfillService()
        result = svc.lookup_observation("never-existed")
        assert result["found"] is False
