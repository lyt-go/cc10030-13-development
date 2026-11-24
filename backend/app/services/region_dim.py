"""区域维度领域服务：维护行政与业务区域层级维度表，将网格索引归属到区域维度中生成区域聚合数据集。

在维度表之上额外承载三块增量对账能力：
1. 行政区树：按「区域编号 / 上级区域」构建父子树，支持向最小可核实子区落点；
2. 边界勘正单：边界修正以版本化勘正单登记，生效后核验按最新边界版本留痕；
3. 落点归属：射线法点面判定，迟到定位点按采集区间分配到最深层可核实子区，
   无法唯一归属时显式标记为跨县/归属不明，绝不默认摊派。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class RegionDimService(BaseModuleService):
    MODULE = "region_dim"
    ENTITY = "区域层级"
    OPERATION_TERM = "入湖"
    LIST_FIELDS = ["区域编号", "上级区域", "区域名称", "区域级别", "边界编码", "网格数量", "维度版本", "生效时间"]
    REQUIRED_FIELDS = ["区域编号", "上级区域", "区域名称"]
    STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线"]
    ACTION_RULES = {"提交校验": "待校验", "发布维度": "已发布", "标记下线": "已下线"}
    NEGATIVE_ACTIONS = []

    # -- 行政区树 --

    def get_region(self, region_code: str) -> dict[str, Any] | None:
        """按区域编号读取维度行。"""
        for row in store.rows(self.MODULE):
            if str(row.get("区域编号", "")) == str(region_code):
                return row
        return None

    def ensure_region(self, region_code: str, name: str, level: str,
                      parent_code: str, polygon: list[list[float]] | None = None,
                      boundary_version: str = "原始边界") -> dict[str, Any]:
        """登记或取回一个行政区节点，边界多边形等非列表字段直接挂在维度行上。"""
        existing = self.get_region(region_code)
        if existing is not None:
            return existing
        entry, _ = self.create_entry({
            "区域编号": region_code,
            # 维度表上级区域为必填，根节点用 ROOT 占位；parent_of 找不到时自然返回 None
            "上级区域": parent_code or "ROOT",
            "区域名称": name,
            "区域级别": level,
            "边界编码": region_code,
            "网格数量": 0,
            "维度版本": "v1",
            "生效时间": time.strftime("%Y-%m-%d", time.localtime()),
        })
        entry["边界多边形"] = polygon or []
        entry["边界版本"] = boundary_version
        entry["status"] = "已发布"
        entry["pending"] = False
        return entry

    def parent_of(self, region_code: str) -> dict[str, Any] | None:
        """返回直接父区维度行，顶层区域返回 None。"""
        region = self.get_region(region_code)
        if region is None:
            return None
        parent_ref = str(region.get("上级区域", "")).strip()
        if not parent_ref:
            return None
        parent = self.get_region(parent_ref)
        if parent is not None:
            return parent
        for row in store.rows(self.MODULE):
            if str(row.get("区域名称", "")) == parent_ref:
                return row
        return None

    def children_of(self, region_code: str) -> list[dict[str, Any]]:
        """返回直接子区（按区域编号或名称匹配上级引用）。"""
        region = self.get_region(region_code)
        refs = {str(region_code)}
        if region is not None:
            refs.add(str(region.get("区域名称", "")))
        return [
            row for row in store.rows(self.MODULE)
            if str(row.get("上级区域", "")) in refs and str(row.get("区域编号", "")) != str(region_code)
        ]

    def region_tree(self, root_code: str) -> dict[str, Any]:
        """以指定区域为根输出嵌套行政区树，叶子即最小可核实子区。"""
        root = self.get_region(root_code)
        if root is None:
            raise KeyError(f"区域 {root_code} 不存在")

        def build(node: dict[str, Any]) -> dict[str, Any]:
            children = [build(child) for child in self.children_of(str(node["区域编号"]))]
            return {
                "region_code": node["区域编号"],
                "region_name": node.get("区域名称", ""),
                "level": node.get("区域级别", ""),
                "boundary_version": node.get("边界版本", "原始边界"),
                "children": children,
            }

        return build(root)

    # -- 边界勘正单 --

    def _revisions_file(self):
        return self._data_dir("region_boundary") / "boundary_revisions.jsonl"

    def list_boundary_revisions(self, region_code: str | None = None) -> list[dict[str, Any]]:
        """读取边界勘正单台账，可按区域过滤。"""
        rows = self._read_jsonl(self._revisions_file())
        if region_code:
            rows = [r for r in rows if r.get("region_code") == region_code]
        return rows

    def apply_boundary_revision(self, region_code: str, polygon: list[list[float]],
                                effective_at: float | None = None,
                                note: str = "") -> dict[str, Any]:
        """登记并立即生效一份边界勘正单：历史版本留痕，维度行切换到新边界。"""
        region = self.get_region(region_code)
        if region is None:
            raise KeyError(f"区域 {region_code} 不存在")
        if not polygon or len(polygon) < 3:
            raise ValueError("边界多边形至少需要 3 个顶点")
        effective_at = effective_at or time.time()
        previous = list(self.list_boundary_revisions(region_code))
        revision_no = f"RV-{region_code}-{len(previous) + 1:03d}"
        revision = {
            "revision_id": revision_no,
            "region_code": region_code,
            "region_name": region.get("区域名称", ""),
            "polygon": polygon,
            "previous_version": region.get("边界版本", "原始边界"),
            "effective_at": effective_at,
            "note": note,
            "status": "已生效",
        }
        self._write_jsonl(self._revisions_file(), [revision])
        region["边界多边形"] = polygon
        region["边界版本"] = revision_no
        region["生效时间"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(effective_at))
        region["status"] = "已发布"
        region["pending"] = False
        return revision

    # -- 点面判定与最小子区归属 --

    @staticmethod
    def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
        """射线法判断点是否在多边形内，顶点按 [lat, lon] 顺序给出。"""
        if not polygon or len(polygon) < 3:
            return False
        inside = False
        n = len(polygon)
        j = n - 1
        for i in range(n):
            lat_i, lon_i = float(polygon[i][0]), float(polygon[i][1])
            lat_j, lon_j = float(polygon[j][0]), float(polygon[j][1])
            intersects = ((lat_i > lat) != (lat_j > lat)) and (
                lon < (lon_j - lon_i) * (lat - lat_i) / ((lat_j - lat_i) or 1e-12) + lon_i
            )
            if intersects:
                inside = not inside
            j = i
        return inside

    def _descendant_leaves(self, root_code: str) -> list[dict[str, Any]]:
        """深度优先收集根下全部叶子区域（最小可核实子区）。"""
        leaves: list[dict[str, Any]] = []

        def walk(code: str) -> None:
            children = self.children_of(code)
            if not children:
                region = self.get_region(code)
                if region is not None:
                    leaves.append(region)
                return
            for child in children:
                walk(str(child["区域编号"]))

        walk(root_code)
        return leaves

    def assign_point_to_region(self, point: dict[str, Any], parent_code: str) -> dict[str, Any]:
        """把一个迟到定位点分配到父区下最小可核实的子区。

        优先采信载荷显式指定的 region_code 并用当前边界复核；未指定或复核不通过时，
        对父区下全部叶子做射线法点面判定。多个子区同时命中视为边界争议，
        全部未命中视为跨县/归属不明，两种情况都不做默认摊派。
        """
        lat = point.get("lat")
        lon = point.get("lon")
        leaves = self._descendant_leaves(parent_code)
        hinted = str(point.get("region_code", "")).strip()

        if hinted:
            hinted_region = self.get_region(hinted)
            polygon = (hinted_region or {}).get("边界多边形") or []
            if hinted_region is not None and self.point_in_polygon(float(lat), float(lon), polygon):
                return {
                    "region_code": hinted,
                    "region_name": hinted_region.get("区域名称", ""),
                    "status": "已归属",
                    "boundary_version": hinted_region.get("边界版本", "原始边界"),
                    "reason": "显式归属且边界复核通过",
                }

        matches: list[dict[str, Any]] = []
        for leaf in leaves:
            polygon = leaf.get("边界多边形") or []
            if self.point_in_polygon(float(lat), float(lon), polygon):
                matches.append(leaf)

        if len(matches) == 1:
            leaf = matches[0]
            return {
                "region_code": leaf["区域编号"],
                "region_name": leaf.get("区域名称", ""),
                "status": "已归属",
                "boundary_version": leaf.get("边界版本", "原始边界"),
                "reason": "点面判定唯一命中最小子区",
            }
        if len(matches) > 1:
            codes = [str(m["区域编号"]) for m in matches]
            return {
                "region_code": None,
                "status": "边界争议",
                "boundary_version": [m.get("边界版本", "原始边界") for m in matches],
                "reason": f"同一观测同时命中 {('、'.join(codes))}，待边界勘正后核验",
            }
        reason = "显式归属未通过当前边界复核" if hinted else "父区下无任何子区边界包含该点"
        return {
            "region_code": None,
            "status": "归属不明",
            "boundary_version": None,
            "reason": f"{reason}，按跨县记录挂起待确认",
        }

    def aggregate_by_region(self, batch_key: str, region_id: int = 1) -> dict[str, Any]:
        """读取网格索引数据集，将网格按区域归属聚合生成区域维度汇总数据集。"""
        region = store.find(self.MODULE, region_id)
        if region is None:
            region = {"区域编号": "REGION-001", "区域名称": "东城区", "区域级别": "区级",
                      "边界编码": "110101", "上级区域": "北京市"}

        # 读取网格索引
        try:
            grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
            grids, _ = grid_svc.get_grid_index(batch_key)
        except Exception:
            grids_file = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
            if not grids_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的网格索引数据不存在")
            grids = self._read_jsonl(grids_file)

        if not grids:
            return {"batch_key": batch_key, "total_points_aggregated": 0}

        total_points = sum(g["point_count"] for g in grids)
        region_center_lat = sum(g["center_lat"] * g["point_count"] for g in grids) / max(total_points, 1)
        region_center_lon = sum(g["center_lon"] * g["point_count"] for g in grids) / max(total_points, 1)

        summary = {
            "batch_key": batch_key,
            "region_id": region_id,
            "region_name": region.get("区域名称", "未知"),
            "region_level": region.get("区域级别", "未知"),
            "boundary_code": region.get("边界编码", ""),
            "grid_count": len(grids),
            "total_points": total_points,
            "region_center_lat": round(region_center_lat, 6),
            "region_center_lon": round(region_center_lon, 6),
            "generated_at": time.time(),
        }

        output_file = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        written = self._write_jsonl(output_file, [summary])

        # 更新区域维度记录
        if region.get("id"):
            region["网格数量"] = len(grids)

        return {
            "batch_key": batch_key,
            "region_name": summary["region_name"],
            "grids_aggregated": len(grids),
            "total_points": total_points,
            "output_file": str(output_file),
        }

    def get_region_aggregates(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取批次的区域聚合数据集文件。"""
        file_path = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        data = self._read_jsonl(file_path)
        return data, str(file_path)
