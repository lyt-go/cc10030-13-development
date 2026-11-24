# 地理空间轨迹与网格聚合分析平台

面向定位点采集、坐标标准化、网格切分、轨迹分段驻留识别、空间关联聚合与区域维度产出的地理空间数据分析平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 定位源接入 | `geo_source` | 采集批次 | 批次编号、采集终端、定位方式 |
| 坐标标准化 | `coord_transform` | 转换规则 | 规则编号、源坐标系、目标坐标系 |
| 轨迹切分 | `track_segment` | 轨迹分段 | 分段编号、终端编号、起始时间 |
| 驻留识别 | `dwell_detect` | 驻留事件 | 事件编号、分段编号、中心坐标 |
| 网格索引 | `grid_index` | 网格单元 | 网格编号、网格层级、边界范围 |
| 围栏判定 | `geofence_rule` | 电子围栏 | 围栏编号、围栏形状、顶点序列 |
| 空间关联 | `spatial_join` | 关联任务 | 任务编号、左数据集、右数据集 |
| 兴趣点匹配 | `poi_match` | 匹配候选 | 候选编号、驻留事件、兴趣点编号 |
| 区域维度 | `region_dim` | 区域层级 | 区域编号、上级区域、区域名称 |
| 热力聚合 | `heatmap_aggregate` | 聚合窗口 | 窗口编号、网格层级、时间粒度 |
| 起止流量 | `od_matrix` | 流量单元 | 单元编号、起点网格、终点网格 |
| 路径还原 | `route_restore` | 还原路段 | 路段编号、分段编号、路段序列 |
| 密度估算 | `density_estimate` | 密度网格 | 估算编号、网格编号、带宽参数 |
| 轨迹质量 | `geo_quality` | 质量报告 | 报告编号、批次编号、漂移点数 |
| 点位归并 | `dedup_merge` | 归并批次 | 归并编号、批次编号、去重键列 |
| 分析快照 | `geo_snapshot` | 版本快照 | 快照编号、分析口径、网格层级 |
| 历史回补 | `geo_backfill` | 回补任务 | 回补编号、目标数据集、起始日期 |
| 结果导出 | `geo_export` | 导出任务 | 导出编号、快照编号、导出格式 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 历史回补·行政区树增量对账

历史回补工作台（`/geo_backfill`）在通用清单之外提供「行政区树补采增量对账」批量入口，
后端实现位于 `app/services/geo_backfill.py`、`app/services/region_dim.py`，
专用路由为 `app/routers/backfill.py`（前缀 `/api/backfill`），台账落
`data/backfill_reconcile/*.jsonl`、勘正单落 `data/region_boundary/`。

| 接口 | 作用 |
| --- | --- |
| `POST /api/backfill/regions` | 登记行政区树节点（市/县），叶子即最小可核实子区 |
| `GET /api/backfill/region-tree/{root_code}` | 输出嵌套行政区树 |
| `POST /api/backfill/regions/{code}/boundary-revisions` | 边界勘正单登记并立即生效，历史版本留痕 |
| `POST /api/backfill/supplement-batches` | 批量选择多条补采记录一次提交，逐点射线法落点，跨县/争议挂起不摊派 |
| `GET /api/backfill/supplement-records` | 按父区/子区/状态查询逐点清单 |
| `GET /api/backfill/observations/{obs_key}` | 观测查重：回查已计入哪次确认、申请或待次批补单 |
| `POST /api/backfill/regions/{code}/confirmations` | 子区按当前生效边界独立核验逐点清单 |
| `POST /api/backfill/applications` | 父区打开差额申请，快照当时可结算的逐点范围 |
| `POST /api/backfill/applications/{id}/issue` | 实际签发，支持两种口径（见下） |
| `POST /api/backfill/carryovers/{id}/settle` | 待次批补单对应子区核验后结算，点回到已确认并进入下一批申请 |
| `GET /api/backfill/regions/{parent}/summary` | 按子区分别给出待对账数据与差额 |

对账口径（同一观测全局唯一，任何路径只结算一次，重送/重发均幂等）：

- `confirmed_only`：只结算已确认子区的点，未确认点转「待次批」补单。
  以市级已登记 100、甲县确认 1 点、乙县待审 1 点为例，签发为 **101 条**并保留乙县补单。
- `merge_pending`：首次签发前把范围内未确认点并入另一个**已通过确认**的子区，
  上例可签发为 **102 条且无补单**；并入目标必须是本申请已确认的子区。
- 申请未签发期间，待审子区随勘正边界完成独立核验后再走 `confirmed_only`，
  同样得到 102 条、无补单；勘正改判的点只在新归属子区记一次增量，不会两县重复计入。
- 跨县/边界争议点始终挂起，不进申请范围、不生成差额；再次提交同一观测直接判重，
  返回其已计入的确认单/申请/补单，不多记差额。
- 待次批补单在子区核验后经 `settle` 结算（勘正边界会在结算时重新复核），
  点回到已确认并可纳入次批申请；补单只允许结算一次，同一观测全程只产生一次增量。

领域：数据工程
子方向：数据分析与数据应用
