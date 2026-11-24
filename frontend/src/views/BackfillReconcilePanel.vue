<template>
  <section class="recon-panel" data-testid="backfill-recon-panel">
    <header class="recon-head">
      <h3>行政区树补采增量对账</h3>
      <p>
        批量提交补采定位点，按点逐条核验到最小可核实子区；子区独立确认后由父区批量结算差额，
        同一观测全局只计一次，重送自动判重。
      </p>
    </header>

    <div v-if="message" class="recon-message" :class="messageKind" data-testid="recon-message">
      {{ message }}
    </div>

    <!-- 第一步：批量补采提交 -->
    <fieldset class="recon-block">
      <legend>① 批量提交补采记录</legend>
      <div class="form-grid">
        <label>
          <span>补采批次号</span>
          <input v-model="form.batchNo" data-testid="batch-no" placeholder="如 SB-202609-01" />
        </label>
        <label>
          <span>父级行政区编号</span>
          <input v-model="form.parentRegion" data-testid="parent-region" placeholder="如 CITY-1" />
        </label>
        <label>
          <span>采集区间起</span>
          <input v-model="form.startDate" data-testid="start-date" placeholder="2026-09-01" />
        </label>
        <label>
          <span>采集区间止</span>
          <input v-model="form.endDate" data-testid="end-date" placeholder="2026-09-07" />
        </label>
      </div>
      <label class="full-line">
        <span>逐点清单（每行一条：观测键,纬度,经度[,声明子区]）</span>
        <textarea
          v-model="form.pointsText"
          data-testid="points-text"
          rows="5"
          placeholder="obs-jia,0.5,0.5&#10;obs-yi,0.5,1.5&#10;obs-cross,2.5,2.5"
        ></textarea>
      </label>
      <div class="block-actions">
        <button class="btn primary" type="button" data-testid="submit-batch" :disabled="busy" @click="onSubmitBatch">
          一次批量提交
        </button>
      </div>
      <table v-if="submitResult" class="recon-table" data-testid="submit-result">
        <tbody>
          <tr><th>提交点数</th><td>{{ submitResult.submitted_points }}</td></tr>
          <tr><th>新入账</th><td>{{ submitResult.accepted_points }}</td></tr>
          <tr><th>重复跳过</th><td>{{ submitResult.duplicate_points }}</td></tr>
          <tr><th>跨县/归属不明挂起</th><td>{{ submitResult.suspended_points }}</td></tr>
        </tbody>
      </table>
      <ul v-if="submitResult && submitResult.duplicates.length" class="recon-list">
        <li v-for="dup in submitResult.duplicates" :key="dup.obs_key">
          观测 {{ dup.obs_key }} 已计入 {{ dup.already_region ?? '未归属' }}
          （确认单 {{ dup.confirmation_id ?? '尚无' }}），本次不重复入账
        </li>
      </ul>
      <ul v-if="submitResult && submitResult.suspended.length" class="recon-list warn">
        <li v-for="item in submitResult.suspended" :key="item.record_id">
          {{ item.obs_key }}：{{ item.reason }}
        </li>
      </ul>
    </fieldset>

    <!-- 第二步：子区独立核验 -->
    <fieldset class="recon-block">
      <legend>② 子区逐点核验确认</legend>
      <div class="form-grid">
        <label>
          <span>父级行政区编号</span>
          <input v-model="confirmForm.parentRegion" data-testid="confirm-parent" placeholder="CITY-1" />
        </label>
        <label>
          <span>核验子区编号</span>
          <input v-model="confirmForm.regionCode" data-testid="confirm-region" placeholder="JIA-1" />
        </label>
      </div>
      <div class="block-actions">
        <button class="btn primary" type="button" data-testid="confirm-region-btn" :disabled="busy" @click="onConfirmRegion">
          按当前生效边界核验
        </button>
        <button class="btn" type="button" data-testid="refresh-summary" :disabled="busy" @click="onRefreshSummary">
          刷新子区待对账数据
        </button>
      </div>
      <table v-if="summary" class="recon-table" data-testid="region-summary">
        <thead>
          <tr>
            <th>子区</th><th>待核验</th><th>已确认</th><th>挂起</th>
            <th>已签发</th><th>已并入</th><th>待次批</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="bucket in summary.per_region" :key="bucket.region_code">
            <td>{{ bucket.region_name }}（{{ bucket.region_code }}）</td>
            <td>{{ bucket.pending_count }}</td>
            <td>{{ bucket.confirmed_count }}</td>
            <td>{{ bucket.suspended_count }}</td>
            <td>{{ bucket.signed_count }}</td>
            <td>{{ bucket.merged_count }}</td>
            <td>{{ bucket.next_batch_count }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="summary && summary.open_carryovers.length" class="carryover-box">
        <h4>待次批补单（子区核验后可结算，进入下一批申请）</h4>
        <table class="recon-table" data-testid="carryover-list">
          <thead>
            <tr><th>补单编号</th><th>来源申请</th><th>子区</th><th>观测</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="order in summary.open_carryovers" :key="order.carryover_id">
              <td>{{ order.carryover_id }}</td>
              <td>{{ order.source_app_no }}</td>
              <td>{{ order.region_name ?? order.region_code }}</td>
              <td>{{ order.obs_keys.join('、') }}</td>
              <td>
                <button
                  class="btn primary"
                  type="button"
                  :data-testid="`settle-${order.carryover_id}`"
                  :disabled="busy"
                  @click="onSettleCarryover(order.carryover_id)"
                >
                  核验并结算补单
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </fieldset>

    <!-- 第三步：父区差额申请与签发 -->
    <fieldset class="recon-block">
      <legend>③ 父区差额申请与签发</legend>
      <div class="form-grid">
        <label>
          <span>申请编号</span>
          <input v-model="appForm.appNo" data-testid="app-no" placeholder="APP-001" />
        </label>
        <label>
          <span>父级行政区编号</span>
          <input v-model="appForm.parentRegion" data-testid="app-parent" placeholder="CITY-1" />
        </label>
        <label>
          <span>已登记总数（基线）</span>
          <input v-model.number="appForm.baselineTotal" data-testid="baseline-total" type="number" min="0" />
        </label>
        <label>
          <span>并入目标子区（仅并入口径）</span>
          <input v-model="appForm.mergeTarget" data-testid="merge-target" placeholder="如 JIA-1，可留空" />
        </label>
      </div>
      <div class="block-actions">
        <button class="btn" type="button" data-testid="create-app" :disabled="busy" @click="onCreateApplication">
          打开对账申请
        </button>
        <button class="btn primary" type="button" data-testid="issue-confirmed-only" :disabled="busy" @click="onIssueConfirmedOnly">
          只结算确认子区（未确认留待次批）
        </button>
        <button class="btn primary" type="button" data-testid="issue-merge" :disabled="busy" @click="onIssueMerge">
          首次签发前并入已通过子区
        </button>
      </div>
      <table v-if="issueResult" class="recon-table" data-testid="issue-result">
        <tbody>
          <tr><th>申请编号</th><td>{{ issueResult.application.application_id }}</td></tr>
          <tr><th>基线总数</th><td>{{ issueResult.application.baseline_total }}</td></tr>
          <tr><th>本次增量</th><td>{{ issueResult.application.delta_total }}</td></tr>
          <tr><th>签发后总数</th><td data-testid="issued-total">{{ issueResult.application.issued_total }}</td></tr>
          <tr><th>待次批补单</th><td>{{ issueResult.carryover_orders.length }} 张</td></tr>
        </tbody>
      </table>
      <ul v-if="issueResult && issueResult.carryover_orders.length" class="recon-list warn">
        <li v-for="order in issueResult.carryover_orders" :key="order.carryover_id">
          补单 {{ order.carryover_id }}：{{ order.region_code }} 的 {{ order.obs_keys.join('、') }} 留待次批
        </li>
      </ul>
    </fieldset>

    <!-- 第四步：观测查重 -->
    <fieldset class="recon-block">
      <legend>④ 观测查重：已计入哪次确认</legend>
      <div class="form-grid">
        <label>
          <span>观测键</span>
          <input v-model="lookupKey" data-testid="lookup-key" placeholder="如 obs-jia" />
        </label>
      </div>
      <div class="block-actions">
        <button class="btn" type="button" data-testid="lookup-btn" :disabled="busy" @click="onLookup">
          回查观测去向
        </button>
      </div>
      <table v-if="lookupResult" class="recon-table" data-testid="lookup-result">
        <tbody>
          <tr><th>当前状态</th><td>{{ lookupResult.status }}</td></tr>
          <tr><th>所属子区</th><td>{{ lookupResult.region_code ?? '—' }}</td></tr>
          <tr><th>来源批次</th><td>{{ lookupResult.batch_no }}</td></tr>
          <tr><th>确认单</th><td>{{ lookupResult.confirmation?.confirmation_id ?? '—' }}</td></tr>
          <tr>
            <th>计入申请</th>
            <td>
              {{ lookupResult.application ? `${lookupResult.application.app_no}（签发 ${lookupResult.application.issued_total} 条）` : '—' }}
            </td>
          </tr>
          <tr><th>待次批补单</th><td>{{ lookupResult.carryover?.carryover_id ?? '—' }}</td></tr>
        </tbody>
      </table>
    </fieldset>
  </section>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'

import {
  confirmRegion,
  createApplication,
  issueApplication,
  lookupObservation,
  parentReconcileSummary,
  settleCarryover,
  submitSupplementBatch,
  type ReconSummary,
  type SupplementPoint,
} from '@/api/backfill-reconcile'

const busy = ref(false)
const message = ref('')
const messageKind = ref<'ok' | 'err'>('ok')

const form = reactive({
  batchNo: '',
  parentRegion: '',
  startDate: '',
  endDate: '',
  pointsText: '',
})
const confirmForm = reactive({ parentRegion: '', regionCode: '' })
const appForm = reactive({
  appNo: '',
  parentRegion: '',
  baselineTotal: 100,
  mergeTarget: '',
})
const lookupKey = ref('')

const submitResult = ref<Awaited<ReturnType<typeof submitSupplementBatch>> | null>(null)
const summary = ref<ReconSummary | null>(null)
const createdApplicationId = ref('')
const issueResult = ref<Awaited<ReturnType<typeof issueApplication>> | null>(null)
const lookupResult = ref<Awaited<ReturnType<typeof lookupObservation>> | null>(null)

function notify(text: string, kind: 'ok' | 'err' = 'ok') {
  message.value = text
  messageKind.value = kind
}

function parsePoints(text: string): SupplementPoint[] {
  const points: SupplementPoint[] = []
  for (const line of text.split('\n')) {
    const parts = line.split(',').map((item) => item.trim()).filter(Boolean)
    if (!parts.length) continue
    if (parts.length < 3) {
      throw new Error(`行「${line}」至少需要 观测键,纬度,经度 三段`)
    }
    const lat = Number(parts[1])
    const lon = Number(parts[2])
    if (Number.isNaN(lat) || Number.isNaN(lon)) {
      throw new Error(`行「${line}」的经纬度不是数字`)
    }
    points.push({
      obs_key: parts[0],
      lat,
      lon,
      region_code: parts[3] || undefined,
    })
  }
  if (!points.length) throw new Error('请至少填写一条补采定位点')
  return points
}

async function onSubmitBatch() {
  busy.value = true
  try {
    const points = parsePoints(form.pointsText)
    submitResult.value = await submitSupplementBatch({
      batch_no: form.batchNo,
      parent_region: form.parentRegion,
      start_date: form.startDate,
      end_date: form.endDate,
      points,
    })
    notify(
      `批次 ${form.batchNo} 已提交：新入账 ${submitResult.value.accepted_points} 条，` +
        `重复 ${submitResult.value.duplicate_points} 条，挂起 ${submitResult.value.suspended_points} 条`,
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '批量提交失败', 'err')
  } finally {
    busy.value = false
  }
}

async function onConfirmRegion() {
  busy.value = true
  try {
    const result = await confirmRegion(confirmForm.parentRegion, confirmForm.regionCode)
    if (result.confirmation_id) {
      notify(`子区 ${confirmForm.regionCode} 已确认 ${result.confirmed_count} 条增量（${result.confirmation_id}）`)
    } else {
      notify(`子区 ${confirmForm.regionCode} 本次没有可确认的待核验点`)
    }
    if (result.region_changed.length) {
      notify(
        `边界勘正改判 ${result.region_changed.length} 条：${result.region_changed
          .map((item) => `${item.record_id}→${item.new_region}`)
          .join('；')}`,
      )
    }
    await onRefreshSummary()
  } catch (error) {
    notify(error instanceof Error ? error.message : '子区核验失败', 'err')
  } finally {
    busy.value = false
  }
}

async function onRefreshSummary() {
  if (!confirmForm.parentRegion) {
    notify('请先填写父级行政区编号', 'err')
    return
  }
  busy.value = true
  try {
    summary.value = await parentReconcileSummary(confirmForm.parentRegion)
  } catch (error) {
    notify(error instanceof Error ? error.message : '汇总读取失败', 'err')
  } finally {
    busy.value = false
  }
}

async function onSettleCarryover(carryoverId: string) {
  busy.value = true
  try {
    const result = await settleCarryover(carryoverId)
    notify(
      `补单 ${carryoverId} 已结算：确认 ${result.confirmed_count} 条` +
        (result.region_changed.length ? `，勘正改判 ${result.region_changed.length} 条` : ''),
    )
    await onRefreshSummary()
  } catch (error) {
    notify(error instanceof Error ? error.message : '补单结算失败', 'err')
  } finally {
    busy.value = false
  }
}

async function onCreateApplication() {
  busy.value = true
  try {
    const app = await createApplication({
      app_no: appForm.appNo,
      parent_region: appForm.parentRegion,
      baseline_total: appForm.baselineTotal,
    })
    createdApplicationId.value = app.application_id
    notify(`申请 ${app.app_no} 已打开，锁定逐点范围 ${app.scope_record_ids.length} 条`)
  } catch (error) {
    notify(error instanceof Error ? error.message : '申请创建失败', 'err')
  } finally {
    busy.value = false
  }
}

async function onIssueConfirmedOnly() {
  busy.value = true
  try {
    if (!createdApplicationId.value) throw new Error('请先打开对账申请')
    issueResult.value = await issueApplication(createdApplicationId.value, 'confirmed_only')
    notify(
      issueResult.value.idempotent
        ? '申请此前已签发，本次为幂等返回，未新增差额'
        : `签发完成：总数 ${issueResult.value.application.issued_total} 条，` +
            `补单 ${issueResult.value.carryover_orders.length} 张`,
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '签发失败', 'err')
  } finally {
    busy.value = false
  }
}

async function onIssueMerge() {
  busy.value = true
  try {
    if (!createdApplicationId.value) throw new Error('请先打开对账申请')
    if (!appForm.mergeTarget) throw new Error('并入口径必须指定已通过确认的目标子区')
    issueResult.value = await issueApplication(
      createdApplicationId.value,
      'merge_pending',
      appForm.mergeTarget,
    )
    notify(`并入签发完成：总数 ${issueResult.value.application.issued_total} 条，无待次批补单`)
  } catch (error) {
    notify(error instanceof Error ? error.message : '并入签发失败', 'err')
  } finally {
    busy.value = false
  }
}

async function onLookup() {
  busy.value = true
  try {
    lookupResult.value = await lookupObservation(lookupKey.value.trim())
  } catch (error) {
    notify(error instanceof Error ? error.message : '观测回查失败', 'err')
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.recon-panel {
  margin: 16px 0 24px;
  border: 1px solid #d8dee6;
  border-radius: 10px;
  padding: 16px 18px;
  background: #fafbfc;
}
.recon-head h3 {
  margin: 0 0 6px;
}
.recon-head p {
  margin: 0 0 12px;
  color: #5b6675;
  font-size: 13px;
}
.recon-block {
  border: 1px solid #e3e8ef;
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
  background: #fff;
}
.recon-block legend {
  padding: 0 6px;
  font-weight: 600;
  color: #2f3a4a;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 10px 14px;
}
.form-grid label,
.full-line {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
  color: #465263;
}
.full-line {
  margin-top: 10px;
}
textarea,
input {
  border: 1px solid #c8d0db;
  border-radius: 6px;
  padding: 7px 9px;
  font: inherit;
}
textarea {
  resize: vertical;
}
.block-actions {
  margin-top: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}
.btn {
  border: 1px solid #c8d0db;
  background: #fff;
  border-radius: 6px;
  padding: 7px 14px;
  cursor: pointer;
}
.btn.primary {
  background: #2563eb;
  border-color: #2563eb;
  color: #fff;
}
.btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.recon-table {
  width: 100%;
  margin-top: 12px;
  border-collapse: collapse;
  font-size: 13px;
}
.recon-table th,
.recon-table td {
  border: 1px solid #e3e8ef;
  padding: 6px 9px;
  text-align: left;
}
.recon-table thead th {
  background: #f3f6fa;
}
.recon-list {
  margin: 10px 0 0;
  padding-left: 18px;
  font-size: 13px;
  color: #334155;
}
.recon-list.warn {
  color: #b45309;
}
.carryover-box {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px dashed #d8dee6;
}
.carryover-box h4 {
  margin: 0 0 6px;
  font-size: 13px;
  color: #2f3a4a;
}
.recon-message {
  margin-bottom: 12px;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
}
.recon-message.ok {
  background: #ecfdf3;
  color: #166534;
  border: 1px solid #a7f3d0;
}
.recon-message.err {
  background: #fef2f2;
  color: #991b1b;
  border: 1px solid #fecaca;
}
</style>
