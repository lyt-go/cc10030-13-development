"""历史回补领域服务。

保留两条既有能力：
- backfill_late_data / verify_backfill_complete：对迟到定位数据按日期区间追加回补到批次文件。

新增「行政区树增量对账」批量入口，围绕补采记录、逐点观测台账、子区确认、
市级差额申请四类对象运转：
1. submit_supplement_batch：批量选择多条补采记录一次提交，按点逐条核验行政区归属，
   同一观测全局唯一，重复提交可回查它已计入哪次确认/申请；
2. confirm_region_supplements：子区按当前生效边界（含勘正单）独立核验逐点清单；
3. create_recon_application / issue_application：父区按子区分别生成差额清单，
   增量回对原先登记总数；支持「只结算确认过的子区+待次批补单」与
   「首次签发前并入另一个已通过子区」两种结算口径；
4. 同一观测在任何路径下都只会被结算一次，重复签发/重复提交不多记差额。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.region_dim import RegionDimService
from app.store import store


class GeoBackfillService(BaseModuleService):
    MODULE = "geo_backfill"
    ENTITY = "回补任务"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["回补编号", "目标数据集", "起始日期", "截止日期", "回补范围", "并行度", "覆盖方式", "回补水位"]
    REQUIRED_FIELDS = ["回补编号", "目标数据集", "起始日期"]
    STATUS_ORDER = ["草稿", "待回补", "回补中", "已完成", "已取消"]
    ACTION_RULES = {"启动回补": "回补中", "标记完成": "已完成", "取消回补": "已取消"}
    NEGATIVE_ACTIONS = []

    # 观测台账/补采/确认/申请/补单的状态口径
    ST_ASSIGNED = "待核验"
    ST_SUSPENDED = "挂起"
    ST_DUPLICATE = "重复跳过"
    ST_CONFIRMED = "已确认"
    ST_MERGED = "已并入"
    ST_SIGNED = "已签发"
    ST_NEXT_BATCH = "待次批"

    # -- 持久化帮助器：全部落 JSONL，进程重启后仍可对账 --

    def _recon_dir(self):
        return self._data_dir("backfill_reconcile")

    def _table_file(self, table: str):
        return self._recon_dir() / f"{table}.jsonl"

    def _load(self, table: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self._table_file(table))

    def _append(self, table: str, rows: list[dict[str, Any]]) -> None:
        if rows:
            self._write_jsonl(self._table_file(table), rows)

    def _replace(self, table: str, rows: list[dict[str, Any]]) -> None:
        path = self._table_file(table)
        path.parent.mkdir(parents=True, exist_ok=True)
        import json
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def _next_seq(self, rows: list[dict[str, Any]], key: str, prefix: str) -> str:
        used = {str(r.get(key, "")) for r in rows}
        seq = 1
        while f"{prefix}{seq:03d}" in used:
            seq += 1
        return f"{prefix}{seq:03d}"

    @staticmethod
    def _obs_key(point: dict[str, Any]) -> str:
        """同一观测的稳定身份键：优先 obs_key，其次 point_id，再次坐标+时间指纹。"""
        for field in ("obs_key", "point_id"):
            value = point.get(field)
            if value not in (None, ""):
                return str(value)
        raw = f"{point.get('lat')}|{point.get('lon')}|{point.get('collected_at') or point.get('timestamp')}"
        return "fp:" + raw

    def _find_ledger(self, obs_key: str) -> dict[str, Any] | None:
        for row in self._load("observation_ledger"):
            if row.get("obs_key") == obs_key:
                return row
        return None

    def _find_record(self, record_id: str) -> dict[str, Any] | None:
        for row in self._load("supplement_records"):
            if row.get("record_id") == record_id:
                return row
        return None

    # -- 批量补采提交：按点逐条核验归属 --

    def submit_supplement_batch(self, batch_no: str, parent_region: str,
                                points: list[dict[str, Any]],
                                start_date: str, end_date: str,
                                source: str = "人工补采") -> dict[str, Any]:
        """批量提交补采定位点，逐点分配到父区下最小可核实子区并登记观测台账。

        - 已在台账中的观测不会二次入账，返回其已计入的确认/申请位置；
        - 边界争议或归属不明的点挂起为跨县待确认，不摊派给任何子区；
        - 同步在 geo_backfill 模块登记一条批次任务，供通用工作台列表展示。
        """
        if not points:
            raise ValueError("补采批次至少包含一条定位点")
        region_svc = RegionDimService()
        if region_svc.get_region(parent_region) is None:
            raise KeyError(f"父级行政区 {parent_region} 不存在")

        records = self._load("supplement_records")
        ledger = self._load("observation_ledger")
        if any(r.get("batch_no") == batch_no for r in records):
            raise ValueError(f"补采批次 {batch_no} 已提交，请勿重复建单")

        new_records: list[dict[str, Any]] = []
        new_ledger_rows: list[dict[str, Any]] = []
        per_region: dict[str, dict[str, Any]] = {}
        duplicates: list[dict[str, Any]] = []
        suspended: list[dict[str, Any]] = []
        now = time.time()
        seq = len(records)

        for point in points:
            obs_key = self._obs_key(point)
            existing = next((r for r in ledger if r.get("obs_key") == obs_key), None)
            if existing is not None:
                dup = {
                    "obs_key": obs_key,
                    "status": self.ST_DUPLICATE,
                    "already_region": existing.get("region_code"),
                    "already_status": existing.get("status"),
                    "confirmation_id": existing.get("confirmation_id"),
                    "application_id": existing.get("application_id"),
                    "carryover_id": existing.get("carryover_id"),
                }
                duplicates.append(dup)
                continue

            assignment = region_svc.assign_point_to_region(point, parent_region)
            seq += 1
            record_id = f"SR-{batch_no}-{seq:03d}"
            status = self.ST_ASSIGNED if assignment["status"] == "已归属" else self.ST_SUSPENDED
            record = {
                "record_id": record_id,
                "batch_no": batch_no,
                "source": source,
                "parent_region": parent_region,
                "region_code": assignment.get("region_code"),
                "region_name": assignment.get("region_name"),
                "obs_key": obs_key,
                "lat": point.get("lat"),
                "lon": point.get("lon"),
                "collected_at": point.get("collected_at") or point.get("timestamp"),
                "date_range": f"{start_date}~{end_date}",
                "boundary_version": assignment.get("boundary_version"),
                "assign_reason": assignment.get("reason"),
                "status": status,
                "confirmation_id": None,
                "application_id": None,
                "carryover_id": None,
                "submitted_at": now,
            }
            new_records.append(record)
            new_ledger_rows.append({
                "obs_key": obs_key,
                "current_record_id": record_id,
                "batch_no": batch_no,
                "parent_region": parent_region,
                "region_code": assignment.get("region_code"),
                "status": status,
                "boundary_version": assignment.get("boundary_version"),
                "confirmation_id": None,
                "application_id": None,
                "carryover_id": None,
                "history": [{
                    "at": now,
                    "action": "批量提交",
                    "detail": assignment.get("reason"),
                    "region_code": assignment.get("region_code"),
                }],
                "updated_at": now,
            })

            if status == self.ST_SUSPENDED:
                suspended.append({"record_id": record_id, "obs_key": obs_key,
                                  "reason": assignment.get("reason")})
                continue

            bucket = per_region.setdefault(assignment["region_code"], {
                "region_code": assignment["region_code"],
                "region_name": assignment["region_name"],
                "pending_count": 0,
                "obs_keys": [],
            })
            bucket["pending_count"] += 1
            bucket["obs_keys"].append(obs_key)

        self._append("supplement_records", new_records)
        self._append("observation_ledger", new_ledger_rows)

        entry, _ = self.create_entry({
            "回补编号": batch_no,
            "目标数据集": parent_region,
            "起始日期": start_date,
            "截止日期": end_date,
            "回补范围": f"{start_date}~{end_date}",
            "并行度": "1",
            "覆盖方式": "increment",
            "回补水位": f"新增{len(new_records)}条/重复{len(duplicates)}条/挂起{len(suspended)}条",
        })
        if entry is not None:
            entry["status"] = self.ST_SUSPENDED if suspended and not per_region else self.ST_ASSIGNED

        return {
            "batch_no": batch_no,
            "parent_region": parent_region,
            "submitted_points": len(points),
            "accepted_points": len(new_records),
            "duplicate_points": len(duplicates),
            "suspended_points": len(suspended),
            "per_region_pending": list(per_region.values()),
            "duplicates": duplicates,
            "suspended": suspended,
        }

    def lookup_observation(self, obs_key: str) -> dict[str, Any]:
        """回查一个观测已计入哪次确认、哪次申请或哪张待次批补单。"""
        ledger_row = self._find_ledger(str(obs_key))
        if ledger_row is None:
            return {"obs_key": str(obs_key), "found": False}
        confirmation = None
        if ledger_row.get("confirmation_id"):
            confirmation = next(
                (c for c in self._load("confirmations")
                 if c.get("confirmation_id") == ledger_row["confirmation_id"]), None)
        application = None
        if ledger_row.get("application_id"):
            application = next(
                (a for a in self._load("applications")
                 if a.get("application_id") == ledger_row["application_id"]), None)
        carryover = None
        if ledger_row.get("carryover_id"):
            carryover = next(
                (c for c in self._load("carryover_orders")
                 if c.get("carryover_id") == ledger_row["carryover_id"]), None)
        return {
            "obs_key": str(obs_key),
            "found": True,
            "status": ledger_row.get("status"),
            "region_code": ledger_row.get("region_code"),
            "batch_no": ledger_row.get("batch_no"),
            "confirmation": confirmation,
            "application": application,
            "carryover": carryover,
            "history": ledger_row.get("history", []),
        }

    def list_supplement_records(self, parent_region: str | None = None,
                                region_code: str | None = None,
                                status: str | None = None) -> list[dict[str, Any]]:
        """多条件查询补采逐点清单（父区/子区/状态）。"""
        rows = self._load("supplement_records")
        if parent_region:
            rows = [r for r in rows if r.get("parent_region") == parent_region]
        if region_code:
            rows = [r for r in rows if r.get("region_code") == region_code]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return rows

    # -- 子区独立核验 --

    def confirm_region_supplements(self, parent_region: str, region_code: str,
                                   record_ids: list[str] | None = None,
                                   operator: str = "子区核验员") -> dict[str, Any]:
        """子区对本区域待核验逐点清单做独立确认，按当前生效边界重新落点。

        边界勘正已生效时按新边界复核：仍在本区域则确认；被勘正到其他子区的点
        做归属变更而不是确认；落入争议/不明的点挂起。已结算或已并入的观测
        不允许再被其他子区确认，保证同一观测不会被两个县重复计增量。
        """
        region_svc = RegionDimService()
        region = region_svc.get_region(region_code)
        if region is None:
            raise KeyError(f"子区 {region_code} 不存在")

        records = self._load("supplement_records")
        ledger = self._load("observation_ledger")
        targets = [
            r for r in records
            if r.get("parent_region") == parent_region
            and r.get("region_code") == region_code
            and r.get("status") == self.ST_ASSIGNED
            and (record_ids is None or r.get("record_id") in record_ids)
        ]
        if record_ids:
            found_ids = {r["record_id"] for r in targets}
            missing = [rid for rid in record_ids if rid not in found_ids]
            if missing:
                raise ValueError(f"记录 {missing} 不属于 {region_code} 当前待核验清单")

        confirmed_keys: list[str] = []
        changed: list[dict[str, Any]] = []
        suspended: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        now = time.time()

        for record in targets:
            ledger_row = next(r for r in ledger if r["obs_key"] == record["obs_key"])
            if ledger_row["status"] in (self.ST_SIGNED, self.ST_MERGED, self.ST_NEXT_BATCH):
                blocked.append({"record_id": record["record_id"], "obs_key": record["obs_key"],
                                "reason": f"观测已按{ledger_row['status']}处理，禁止重复确认"})
                continue
            reassignment = region_svc.assign_point_to_region(record, parent_region)
            if reassignment["status"] != "已归属":
                record["status"] = self.ST_SUSPENDED
                record["assign_reason"] = reassignment.get("reason")
                ledger_row["status"] = self.ST_SUSPENDED
                ledger_row["region_code"] = None
                ledger_row["history"].append({
                    "at": now, "action": "核验挂起",
                    "detail": reassignment.get("reason"), "region_code": None})
                suspended.append({"record_id": record["record_id"],
                                  "reason": reassignment.get("reason")})
                continue
            if reassignment["region_code"] != region_code:
                record["region_code"] = reassignment["region_code"]
                record["region_name"] = reassignment["region_name"]
                record["boundary_version"] = reassignment["boundary_version"]
                record["assign_reason"] = "边界勘正后归属变更：" + reassignment.get("reason", "")
                ledger_row["region_code"] = reassignment["region_code"]
                ledger_row["boundary_version"] = reassignment["boundary_version"]
                ledger_row["history"].append({
                    "at": now, "action": "归属变更",
                    "detail": record["assign_reason"],
                    "region_code": reassignment["region_code"]})
                changed.append({"record_id": record["record_id"],
                                "obs_key": record["obs_key"],
                                "new_region": reassignment["region_code"]})
                continue

            record["status"] = self.ST_CONFIRMED
            record["boundary_version"] = reassignment["boundary_version"]
            ledger_row["status"] = self.ST_CONFIRMED
            ledger_row["boundary_version"] = reassignment["boundary_version"]
            confirmed_keys.append(record["obs_key"])
            ledger_row["history"].append({
                "at": now, "action": "子区确认",
                "detail": f"{operator} 按 {reassignment['boundary_version']} 核验通过",
                "region_code": region_code})

        confirmation_id = None
        if confirmed_keys:
            confirmations = self._load("confirmations")
            confirmation_id = self._next_seq(confirmations, "confirmation_id",
                                              f"CF-{region_code}-")
            confirmation = {
                "confirmation_id": confirmation_id,
                "parent_region": parent_region,
                "region_code": region_code,
                "region_name": region.get("区域名称", ""),
                "boundary_version": region.get("边界版本", "原始边界"),
                "record_ids": [r["record_id"] for r in records
                               if r["obs_key"] in confirmed_keys and r["status"] == self.ST_CONFIRMED],
                "obs_keys": confirmed_keys,
                "confirmed_count": len(confirmed_keys),
                "delta": len(confirmed_keys),
                "operator": operator,
                "confirmed_at": now,
            }
            self._append("confirmations", [confirmation])
            for record in records:
                if record["obs_key"] in confirmed_keys and record["status"] == self.ST_CONFIRMED:
                    record["confirmation_id"] = confirmation_id
            for row in ledger:
                if row["obs_key"] in confirmed_keys and row["status"] == self.ST_CONFIRMED:
                    row["confirmation_id"] = confirmation_id
                    row["updated_at"] = now

        self._replace("supplement_records", records)
        self._replace("observation_ledger", ledger)

        return {
            "parent_region": parent_region,
            "region_code": region_code,
            "confirmation_id": confirmation_id,
            "confirmed_count": len(confirmed_keys),
            "delta": len(confirmed_keys),
            "region_changed": changed,
            "suspended": suspended,
            "blocked": blocked,
            "recon_data": {
                "region_code": region_code,
                "region_name": region.get("区域名称", ""),
                "confirmed_delta": len(confirmed_keys),
                "pending_left": sum(
                    1 for r in records
                    if r.get("parent_region") == parent_region
                    and r.get("region_code") == region_code
                    and r.get("status") == self.ST_ASSIGNED),
            },
        }

    # -- 待次批补单结算 --

    def settle_carryover(self, carryover_id: str,
                         operator: str = "子区核验员") -> dict[str, Any]:
        """待次批补单对应的子区完成核验后结算补单。

        补单逐点按当前生效边界（含期间生效的勘正单）重新复核：
        - 仍归属原子区：转「已确认」并解除补单标记，下一批父区申请可正常纳入；
        - 勘正改判到其他子区：做归属变更，回到待核验状态由新子区确认；
        - 边界争议/归属不明：转挂起等待确认。
        补单只允许结算一次，重复结算直接报错。
        """
        region_svc = RegionDimService()
        carryovers = self._load("carryover_orders")
        carryover = next((c for c in carryovers if c.get("carryover_id") == carryover_id), None)
        if carryover is None:
            raise KeyError(f"待次批补单 {carryover_id} 不存在")
        if carryover.get("status") != self.ST_NEXT_BATCH:
            raise ValueError(f"补单 {carryover_id} 已结算，不能重复处理")

        parent_region = carryover["parent_region"]
        region_code = carryover["region_code"]
        region = region_svc.get_region(region_code)
        records = self._load("supplement_records")
        ledger = self._load("observation_ledger")
        targets = [
            r for r in records
            if r.get("carryover_id") == carryover_id and r.get("status") == self.ST_NEXT_BATCH
        ]
        now = time.time()
        confirmed_keys: list[str] = []
        changed: list[dict[str, Any]] = []
        suspended: list[dict[str, Any]] = []

        for record in targets:
            ledger_row = next(r for r in ledger if r["obs_key"] == record["obs_key"])
            reassignment = region_svc.assign_point_to_region(record, parent_region)
            # 无论结果如何都先解除补单占用，避免观测被永久锁在待次批
            record["carryover_id"] = None
            ledger_row["carryover_id"] = None

            if reassignment["status"] != "已归属":
                record["status"] = self.ST_SUSPENDED
                record["assign_reason"] = reassignment.get("reason")
                ledger_row["status"] = self.ST_SUSPENDED
                ledger_row["region_code"] = None
                ledger_row["history"].append({
                    "at": now, "action": "补单核验挂起",
                    "detail": reassignment.get("reason"), "region_code": None})
                suspended.append({"record_id": record["record_id"],
                                  "reason": reassignment.get("reason")})
                continue
            if reassignment["region_code"] != region_code:
                record["region_code"] = reassignment["region_code"]
                record["region_name"] = reassignment["region_name"]
                record["boundary_version"] = reassignment["boundary_version"]
                record["status"] = self.ST_ASSIGNED
                record["assign_reason"] = "补单核验时边界勘正改判：" + reassignment.get("reason", "")
                ledger_row["region_code"] = reassignment["region_code"]
                ledger_row["boundary_version"] = reassignment["boundary_version"]
                ledger_row["status"] = self.ST_ASSIGNED
                ledger_row["history"].append({
                    "at": now, "action": "补单归属变更",
                    "detail": record["assign_reason"],
                    "region_code": reassignment["region_code"]})
                changed.append({"record_id": record["record_id"],
                                "obs_key": record["obs_key"],
                                "new_region": reassignment["region_code"]})
                continue

            record["status"] = self.ST_CONFIRMED
            record["boundary_version"] = reassignment["boundary_version"]
            ledger_row["status"] = self.ST_CONFIRMED
            ledger_row["boundary_version"] = reassignment["boundary_version"]
            confirmed_keys.append(record["obs_key"])
            ledger_row["history"].append({
                "at": now, "action": "补单子区确认",
                "detail": f"{operator} 按 {reassignment['boundary_version']} 核验通过",
                "region_code": region_code})

        confirmation_id = None
        if confirmed_keys:
            confirmations = self._load("confirmations")
            confirmation_id = self._next_seq(confirmations, "confirmation_id",
                                              f"CF-{region_code}-")
            confirmation = {
                "confirmation_id": confirmation_id,
                "parent_region": parent_region,
                "region_code": region_code,
                "region_name": (region or {}).get("区域名称", ""),
                "boundary_version": (region or {}).get("边界版本", "原始边界"),
                "record_ids": [r["record_id"] for r in records
                               if r["obs_key"] in confirmed_keys and r["status"] == self.ST_CONFIRMED],
                "obs_keys": confirmed_keys,
                "confirmed_count": len(confirmed_keys),
                "delta": len(confirmed_keys),
                "operator": operator,
                "source_carryover_id": carryover_id,
                "confirmed_at": now,
            }
            self._append("confirmations", [confirmation])
            for record in records:
                if record["obs_key"] in confirmed_keys and record["status"] == self.ST_CONFIRMED:
                    record["confirmation_id"] = confirmation_id
            for row in ledger:
                if row["obs_key"] in confirmed_keys and row["status"] == self.ST_CONFIRMED:
                    row["confirmation_id"] = confirmation_id
                    row["updated_at"] = now

        carryover["status"] = "已结算"
        carryover["settled_at"] = now
        carryover["settled_confirmation_id"] = confirmation_id
        carryover["settled_count"] = len(confirmed_keys)
        carryover["changed_count"] = len(changed)
        carryover["suspended_count"] = len(suspended)
        self._replace("carryover_orders", carryovers)
        self._replace("supplement_records", records)
        self._replace("observation_ledger", ledger)

        return {
            "carryover_id": carryover_id,
            "region_code": region_code,
            "confirmation_id": confirmation_id,
            "confirmed_count": len(confirmed_keys),
            "region_changed": changed,
            "suspended": suspended,
        }

    # -- 父区差额申请与签发 --

    def create_recon_application(self, app_no: str, parent_region: str,
                                 baseline_total: int, remark: str = "") -> dict[str, Any]:
        """父区（如市级）打开增量对账申请，快照当时可结算的逐点范围。

        挂起的跨县/争议记录不进入申请范围；已签发或已待次批的观测不重复纳入。
        """
        applications = self._load("applications")
        if any(a.get("app_no") == app_no for a in applications):
            raise ValueError(f"对账申请 {app_no} 已存在")
        records = self._load("supplement_records")
        scope = [
            r for r in records
            if r.get("parent_region") == parent_region
            and r.get("status") in (self.ST_ASSIGNED, self.ST_CONFIRMED)
        ]
        if not scope:
            raise ValueError("当前没有可纳入对账申请的补采记录")
        now = time.time()
        application_id = f"APP-{app_no}"
        application = {
            "application_id": application_id,
            "app_no": app_no,
            "parent_region": parent_region,
            "baseline_total": baseline_total,
            "scope_record_ids": [r["record_id"] for r in scope],
            "scope_snapshot": [
                {"record_id": r["record_id"], "obs_key": r["obs_key"],
                 "region_code": r["region_code"], "status": r["status"]}
                for r in scope
            ],
            "status": "待签发",
            "mode": None,
            "remark": remark,
            "created_at": now,
            "issued_at": None,
            "delta_by_region": [],
            "delta_total": 0,
            "issued_total": None,
            "carryover_ids": [],
        }
        self._append("applications", [application])

        entry, _ = self.create_entry({
            "回补编号": app_no,
            "目标数据集": parent_region,
            "起始日期": time.strftime("%Y-%m-%d", time.localtime(now)),
            "截止日期": time.strftime("%Y-%m-%d", time.localtime(now)),
            "回补范围": f"基线{baseline_total}条+申请增量",
            "并行度": "1",
            "覆盖方式": "reconcile",
            "回补水位": f"待签发，范围{len(scope)}点",
        })
        if entry is not None:
            entry["status"] = "待签发"
            application["store_entry_id"] = entry["id"]
            self._replace("applications", applications + [application])
        return application

    def get_application(self, app_id: str) -> dict[str, Any] | None:
        return next((a for a in self._load("applications")
                     if a.get("application_id") == app_id or a.get("app_no") == app_id), None)

    def issue_application(self, app_id: str, mode: str = "confirmed_only",
                          merge_target: str | None = None) -> dict[str, Any]:
        """实际签发父区申请，按确认采用的逐点范围结算增量。

        mode=confirmed_only：只结算已确认子区的点，未确认的在范围点转待次批补单；
        mode=merge_pending：首次签发前把范围内未确认点并入另一个已通过的子区一并结算。
        同一申请重复签发直接返回首次结果，不重复写台账、不多记差额。
        """
        if mode not in ("confirmed_only", "merge_pending"):
            raise ValueError("mode 仅支持 confirmed_only / merge_pending")
        application = self.get_application(app_id)
        if application is None:
            raise KeyError(f"对账申请 {app_id} 不存在")
        if application["status"] == "已签发":
            return {"idempotent": True, "application": application,
                    "message": "申请已签发，重复请求未新增差额"}

        records = self._load("supplement_records")
        ledger = self._load("observation_ledger")
        carryovers = self._load("carryover_orders")
        scope_ids = set(application["scope_record_ids"])
        scope_records = [r for r in records if r["record_id"] in scope_ids]

        merge_target_region = None
        if mode == "merge_pending":
            if not merge_target:
                raise ValueError("merge_pending 模式必须指定 merge_target 子区")
            confirmed_regions = {
                r["region_code"] for r in scope_records if r["status"] == self.ST_CONFIRMED
            }
            if merge_target not in confirmed_regions:
                raise ValueError(f"并入目标 {merge_target} 不是本申请中已通过确认的子区")
            merge_target_region = merge_target

        now = time.time()
        delta_by_region: dict[str, dict[str, Any]] = {}
        carryover_rows: list[dict[str, Any]] = []
        pending_by_region: dict[str, list[dict[str, Any]]] = {}

        for record in scope_records:
            ledger_row = next(r for r in ledger if r["obs_key"] == record["obs_key"])
            region_code = record["region_code"]
            if record["status"] == self.ST_CONFIRMED:
                bucket = delta_by_region.setdefault(region_code, {
                    "region_code": region_code,
                    "region_name": record.get("region_name"),
                    "confirmed_delta": 0, "merged_delta": 0, "obs_keys": []})
                bucket["confirmed_delta"] += 1
                bucket["obs_keys"].append(record["obs_key"])
                record["status"] = self.ST_SIGNED
                ledger_row["status"] = self.ST_SIGNED
                ledger_row["application_id"] = application["application_id"]
                ledger_row["history"].append({
                    "at": now, "action": "申请签发",
                    "detail": f"计入 {application['app_no']} 已确认增量",
                    "region_code": region_code})
            elif record["status"] == self.ST_ASSIGNED:
                if mode == "merge_pending":
                    bucket = delta_by_region.setdefault(merge_target, {
                        "region_code": merge_target,
                        "region_name": next(
                            (r.get("region_name") for r in scope_records
                             if r.get("region_code") == merge_target), merge_target),
                        "confirmed_delta": 0, "merged_delta": 0, "obs_keys": []})
                    bucket["merged_delta"] += 1
                    bucket["obs_keys"].append(record["obs_key"])
                    record["region_code"] = merge_target
                    record["status"] = self.ST_MERGED
                    ledger_row["region_code"] = merge_target
                    ledger_row["status"] = self.ST_MERGED
                    ledger_row["application_id"] = application["application_id"]
                    ledger_row["history"].append({
                        "at": now, "action": "并入结算",
                        "detail": f"首次签发前由 {region_code} 并入 {merge_target}",
                        "region_code": merge_target})
                else:
                    pending_by_region.setdefault(region_code, []).append(record)
            # 挂起点本就不在 scope_snapshot 中，不会走到这里

        if mode == "confirmed_only" and pending_by_region:
            for region_code, pending_records in pending_by_region.items():
                carryover_id = self._next_seq(carryovers + carryover_rows, "carryover_id",
                                              f"CO-{application['app_no']}-")
                carryover = {
                    "carryover_id": carryover_id,
                    "source_application_id": application["application_id"],
                    "source_app_no": application["app_no"],
                    "parent_region": application["parent_region"],
                    "region_code": region_code,
                    "region_name": pending_records[0].get("region_name"),
                    "record_ids": [r["record_id"] for r in pending_records],
                    "obs_keys": [r["obs_key"] for r in pending_records],
                    "reason": "子区尚未完成确认，留待次批结算",
                    "status": self.ST_NEXT_BATCH,
                    "created_at": now,
                }
                carryover_rows.append(carryover)
                for record in pending_records:
                    record["status"] = self.ST_NEXT_BATCH
                    record["carryover_id"] = carryover_id
                    ledger_row = next(r for r in ledger if r["obs_key"] == record["obs_key"])
                    ledger_row["status"] = self.ST_NEXT_BATCH
                    ledger_row["carryover_id"] = carryover_id
                    ledger_row["history"].append({
                        "at": now, "action": "转待次批",
                        "detail": f"计入补单 {carryover_id}", "region_code": region_code})

        delta_total = sum(b["confirmed_delta"] + b["merged_delta"]
                          for b in delta_by_region.values())
        issued_total = application["baseline_total"] + delta_total
        application.update({
            "status": "已签发",
            "mode": mode,
            "issued_at": now,
            "delta_by_region": list(delta_by_region.values()),
            "delta_total": delta_total,
            "issued_total": issued_total,
            "carryover_ids": [c["carryover_id"] for c in carryover_rows],
            "reconcile_check": {
                "baseline_total": application["baseline_total"],
                "delta_total": delta_total,
                "issued_total": issued_total,
                "balanced": issued_total == application["baseline_total"] + delta_total,
            },
        })

        for record in records:
            if record["record_id"] in scope_ids and record["status"] in (
                    self.ST_SIGNED, self.ST_MERGED):
                record["application_id"] = application["application_id"]
        for row in ledger:
            row["updated_at"] = now

        self._append("carryover_orders", carryover_rows)
        self._replace("supplement_records", records)
        self._replace("observation_ledger", ledger)
        all_apps = self._load("applications")
        for app_row in all_apps:
            if app_row["application_id"] == application["application_id"]:
                app_row.update(application)
        self._replace("applications", all_apps)

        if application.get("store_entry_id"):
            entry = store.find(self.MODULE, application["store_entry_id"])
            if entry is not None:
                entry["status"] = "已完成"
                entry["pending"] = False
                entry["回补水位"] = (
                    f"已签发{issued_total}条（基线{application['baseline_total']}+增量{delta_total}）"
                    f"/补单{len(carryover_rows)}张")

        cross_region_pending = [
            {"record_id": r["record_id"], "obs_key": r["obs_key"],
             "reason": r.get("assign_reason")}
            for r in records
            if r.get("parent_region") == application["parent_region"]
            and r.get("status") == self.ST_SUSPENDED
        ]
        return {
            "idempotent": False,
            "application": application,
            "carryover_orders": carryover_rows,
            "cross_region_pending": cross_region_pending,
        }

    def parent_reconcile_summary(self, parent_region: str) -> dict[str, Any]:
        """父区视角：按子区分别给出待对账数据与差额，并回对总数。"""
        records = self._load("supplement_records")
        rows = [r for r in records if r.get("parent_region") == parent_region]
        per_region: dict[str, dict[str, Any]] = {}
        for row in rows:
            code = row.get("region_code") or "UNASSIGNED"
            bucket = per_region.setdefault(code, {
                "region_code": code,
                "region_name": row.get("region_name") or "跨县/归属不明",
                "pending_count": 0, "confirmed_count": 0, "suspended_count": 0,
                "signed_count": 0, "merged_count": 0, "next_batch_count": 0})
            key_map = {
                self.ST_ASSIGNED: "pending_count",
                self.ST_CONFIRMED: "confirmed_count",
                self.ST_SUSPENDED: "suspended_count",
                self.ST_SIGNED: "signed_count",
                self.ST_MERGED: "merged_count",
                self.ST_NEXT_BATCH: "next_batch_count",
            }
            field = key_map.get(row.get("status"))
            if field:
                bucket[field] += 1
        applications = [a for a in self._load("applications")
                        if a.get("parent_region") == parent_region]
        return {
            "parent_region": parent_region,
            "total_records": len(rows),
            "per_region": list(per_region.values()),
            "applications": applications,
            "open_carryovers": [c for c in self._load("carryover_orders")
                                if c.get("parent_region") == parent_region
                                and c.get("status") == self.ST_NEXT_BATCH],
        }

    # -- 既有文件级回补能力（保持兼容） --

    def backfill_late_data(self, batch_key: str, late_point_count: int = 15,
                            start_date: str = "2026-09-01",
                            end_date: str = "2026-09-07") -> dict[str, Any]:
        """为目标采集批次生成迟到数据并追加回补到已有定位点数据集文件中。"""
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在")

        existing = self._read_jsonl(points_file)
        existing_count = len(existing)

        # 生成迟到数据
        late_points = self._generate_sample_points(count=late_point_count)
        for p in late_points:
            p["batch_key"] = batch_key
            p["is_backfill"] = True
            p["backfill_date_range"] = f"{start_date}~{end_date}"

        # 追加写入
        written = self._write_jsonl(points_file, late_points)

        # 记录回补水位
        self._write_checkpoint(f"backfill_{batch_key}", {
            "batch_key": batch_key,
            "existing_count": existing_count,
            "backfill_count": written,
            "total_after_backfill": existing_count + written,
            "start_date": start_date,
            "end_date": end_date,
            "backfilled_at": time.time(),
        })

        # 创建回补记录
        entry, _ = self.create_entry({
            "回补编号": f"BF-{batch_key}",
            "目标数据集": batch_key,
            "起始日期": start_date,
            "截止日期": end_date,
            "回补范围": f"{start_date}~{end_date}",
            "并行度": "1",
            "覆盖方式": "append",
            "回补水位": f"已回补{written}/{existing_count+written}条",
        })
        if entry:
            self.run_action(entry["id"], "标记完成")

        return {
            "batch_key": batch_key,
            "existing_points": existing_count,
            "backfill_points": written,
            "total_after": existing_count + written,
            "date_range": f"{start_date}~{end_date}",
        }

    def verify_backfill_complete(self, batch_key: str) -> dict[str, Any]:
        """校验指定批次的历史回补完成状态，读取数据文件和检查点返回补全情况。"""
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        current_count = len(self._read_jsonl(points_file)) if points_file.exists() else 0
        cp = self._read_checkpoint(f"backfill_{batch_key}")
        return {
            "batch_key": batch_key,
            "current_total": current_count,
            "original_count": cp.get("existing_count", 0) if cp else 0,
            "backfill_count": cp.get("backfill_count", 0) if cp else 0,
            "is_complete": current_count == (cp.get("total_after_backfill", 0)) if cp else False,
        }
