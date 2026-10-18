import { useSyncExternalStore } from 'react'
import type { StrategyBacktestResult } from './api'
import { fetchRunningTask, reportTaskFinished, reportTaskRunning } from './taskRegistry'

/**
 * 全局回测任务管理 (SSE 模式 + 任务缓存 + 重连支持)。
 *
 * 特性:
 * - 实时进度: EventSource 监听后端 SSE, 推送 day/total/equity
 * - 可取消: POST /strategy/cancel/{job_key}, 后端 cancel_event
 * - 切页/刷新保持: 后端按参数 hash 缓存任务, 重连不重启
 *   - 切页: 模块级 store 保持, EventSource 保持连接, 后续队列继续执行
 *   - 刷新: localStorage 存 job 参数, 刷新后重新连接到同一任务
 */

export interface BacktestProgress {
  day: number
  total: number
  date: string
  equity: number
}

export interface BacktestTask {
  id: number
  isPending: boolean
  result: StrategyBacktestResult | null
  progress: BacktestProgress | null
  error: string | null
  /** 连接中断、正在有界重连中 (UI 显示"连接中断，重试中") */
  reconnecting: boolean
}

// 连接断开后最多自动重连次数, 超过则放弃并进入可重试的错误态
const MAX_RECONNECT_ATTEMPTS = 5

let current: BacktestTask | null = null
const listeners = new Set<() => void>()
let taskSeq = 0
let eventSource: EventSource | null = null
let connectionSeq = 0

export interface BacktestBatchItem {
  name: string
  strategy_id: string
  status: 'queued' | 'running' | 'success' | 'error' | 'cancelled'
  error?: string
  history_id?: string
  history_error?: string
}

export interface BacktestBatch {
  running: boolean
  items: BacktestBatchItem[]
}

let batch: BacktestBatch | null = null
let batchParams: BacktestRunParams[] = []
let batchTaskId: number | null = null
const batchListeners = new Set<() => void>()

export function getBacktestBatch(): BacktestBatch | null { return batch }
export function subscribeBacktestBatch(fn: () => void) {
  batchListeners.add(fn)
  return () => { batchListeners.delete(fn) }
}
function emitBatch() { batchListeners.forEach(fn => fn()) }

export function useBacktestBatch(): BacktestBatch | null {
  return useSyncExternalStore(subscribeBacktestBatch, getBacktestBatch, getServerSnapshot)
}

export function startBacktestBatch(items: { name: string; params: BacktestRunParams }[]): void {
  if (!items.length) throw new Error('请至少选择一个策略')
  if (batch?.running) throw new Error('批量回测正在运行，请先停止当前队列')
  if (current?.isPending) throw new Error('已有回测正在运行，请等待完成或先停止')
  if (localStorage.getItem(RECONNECT_KEY)) throw new Error('上一项回测状态尚未确认，请先重连或停止当前任务')
  batchParams = items.map(item => structuredClone(item.params))
  batch = { running: true, items: items.map(item => ({ name: item.name, strategy_id: item.params.strategy_id, status: 'queued' })) }
  runNextBatchItem()
}

function runNextBatchItem() {
  if (!batch?.running) return
  const index = batch.items.findIndex(item => item.status === 'queued')
  if (index < 0) {
    batch = { ...batch, running: false }
    batchTaskId = null
    emitBatch()
    return
  }
  batch = { ...batch, items: batch.items.map((item, i) => i === index ? { ...item, status: 'running' } : item) }
  // beginBacktest synchronously publishes its pending state, so associate its id first.
  batchTaskId = taskSeq + 1
  beginBacktest(batchParams[index])
  emitBatch()
}

/** Close the old stream and registry before publishing a terminal state: listeners may start a new task. */
function completeTask(id: number, patch: Partial<BacktestTask>, outcome: 'success' | 'error' | 'cancelled' | 'unknown') {
  if (current?.id !== id || !current.isPending) return
  eventSource?.close()
  eventSource = null
  connectionSeq += 1
  if (outcome !== 'unknown') finishTaskRegistry()
  current = { ...current, ...patch, isPending: false, reconnecting: false }
  const activeBatch = batchTaskId === id && batch
  if (activeBatch) {
    const result = current.result
    const halt = outcome === 'unknown' || outcome === 'cancelled' || !activeBatch.running
    batch = {
      ...activeBatch,
      running: !halt,
      items: activeBatch.items.map(item => item.status === 'running'
        ? { ...item, status: outcome === 'success' ? 'success' : outcome === 'cancelled' ? 'cancelled' : 'error', error: current!.error ?? undefined, history_id: result?.history_id, history_error: result?.history_error }
        : halt && item.status === 'queued' ? { ...item, status: 'cancelled', error: '队列已停止，未运行' } : item),
    }
    batchTaskId = null
  }
  emit()
  if (activeBatch) {
    emitBatch()
    if (current?.id === id) runNextBatchItem()
  }
}


const RECONNECT_KEY = 'backtest_reconnect'

// 跨设备接回: 启动时上报注册表, 终态关闭; 其他设备 tryReconnect 无本地键时
// 从注册表回填。重连/SSE 逻辑本身零改动。
const TASK_KIND = 'backtest' as const

/** 终态: 关闭注册表项 (其他设备不再视为进行中) 并清本地重连键。 */
function finishTaskRegistry() {
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (qs) reportTaskFinished(TASK_KIND, { qs })
  localStorage.removeItem(RECONNECT_KEY)
}

function emit() {
  listeners.forEach(fn => fn())
}

function subscribe(fn: () => void) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function getSnapshot() {
  return current
}

function getServerSnapshot() {
  return null
}

/** 查询字符串构建 */
function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== '') sp.set(k, String(v))
  }
  return sp.toString()
}

/** 连接 SSE (新建或重连都用这个) */
async function connectSSE(url: string): Promise<void> {
  const id = current?.id ?? ++taskSeq
  const connection = ++connectionSeq
  const isActive = () => current?.id === id && current.isPending && connectionSeq === connection

  // 关闭旧连接
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  // 预检: 4xx 拒绝 (如分钟策略回测的分钟K覆盖守卫 400) 时 EventSource 只会无 data 地
  // onerror, 会被当成"连接中断"有界重试 — 先 fetch 一次把后端 detail 直接展示给用户。
  try {
    const probe = await fetch(url, { headers: { Accept: 'text/event-stream' } })
    if (!probe.ok) {
      let message = `回测请求失败 (${probe.status})`
      try {
        message = (await probe.json())?.detail ?? message
      } catch { /* ignore */ }
      await probe.body?.cancel().catch(() => {})
      if (isActive()) completeTask(id, { error: message }, probe.status >= 500 ? 'unknown' : 'error')
      return
    }
    await probe.body?.cancel().catch(() => {})
  } catch { /* 网络层异常: 交给下方 EventSource 的重连逻辑 */ }

  if (!isActive()) return
  const es = new EventSource(url)
  eventSource = es

  // 本次连接的重连计数 (EventSource 断开会自动重连并再次触发 onerror)
  let reconnectAttempts = 0

  const clearReconnecting = () => {
    if (isActive() && current?.reconnecting) {
      current = { ...current, reconnecting: false }
      emit()
    }
    reconnectAttempts = 0
  }

  es.onopen = () => {
    clearReconnecting()
  }

  es.addEventListener('progress', (e: MessageEvent) => {
    if (!isActive() || !current) return
    // 收到数据说明连接恢复正常
    reconnectAttempts = 0
    try {
      const prog = JSON.parse(e.data) as BacktestProgress
      current = { ...current, progress: prog, reconnecting: false }
      emit()
    } catch { /* ignore */ }
  })

  es.addEventListener('done', (e: MessageEvent) => {
    if (!isActive()) return
    try {
      const result = JSON.parse(e.data) as StrategyBacktestResult & { error?: string }
      const outcome = result.error ? (/取消|cancel/i.test(result.error) ? 'cancelled' : 'error') : 'success'
      completeTask(id, { result, error: result.error || null }, outcome)
    } catch {
      completeTask(id, { error: '结果解析失败，任务状态未知，请先重连确认' }, 'unknown')
    }
  })

  es.addEventListener('error', (e: MessageEvent) => {
    if (!isActive() || !current) return
    if (e.data) {
      let message = '回测出错'
      try { message = JSON.parse(e.data)?.message ?? message } catch { /* keep fallback */ }
      completeTask(id, { error: message }, /取消|cancel/i.test(message) ? 'cancelled' : 'error')
      return
    }
    reconnectAttempts += 1
    if (reconnectAttempts > MAX_RECONNECT_ATTEMPTS) {
      completeTask(id, { error: '连接中断，任务状态未知，请先重连确认' }, 'unknown')
      return
    }
    current = { ...current, reconnecting: true }
    emit()
  })
}

/** 启动一次 SSE 回测任务 */
export interface BacktestRunParams {
  strategy_id: string
  symbols?: string[] | null
  start?: string | null
  end?: string | null
  matching?: string
  entry_fill?: string
  exit_fill?: string
  fees_pct?: number
  commission_pct?: number
  stamp_tax_pct?: number
  slippage_bps?: number
  max_positions?: number
  max_exposure_pct?: number
  initial_capital?: number
  position_sizing?: string
  params?: Record<string, any> | null
  overrides?: Record<string, any> | null
  mode?: 'position' | 'full'
  holding_days?: number
  asset_type?: 'stock' | 'etf'
  minute_fill?: boolean
  regime_filter?: { states?: string[]; min_score?: number; amv?: string } | null
}

export function startBacktest(params: BacktestRunParams): void {
  if (batch?.running || current?.isPending) return
  if (localStorage.getItem(RECONNECT_KEY)) throw new Error('上一项回测状态尚未确认，请先重连或停止当前任务')
  beginBacktest(params)
}

function beginBacktest(params: BacktestRunParams): void {
  // 取消之前的任务状态
  if (eventSource) {
    eventSource.close()
    eventSource = null
  }

  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null, reconnecting: false }

  const qs = buildQuery({
    strategy_id: params.strategy_id,
    symbols: params.symbols?.join(','),
    start: params.start ?? undefined,
    end: params.end ?? undefined,
    matching: params.matching,
    entry_fill: params.entry_fill,
    exit_fill: params.exit_fill,
    fees_pct: params.fees_pct,
    commission_pct: params.commission_pct,
    stamp_tax_pct: params.stamp_tax_pct,
    slippage_bps: params.slippage_bps,
    max_positions: params.max_positions,
    max_exposure_pct: params.max_exposure_pct,
    initial_capital: params.initial_capital,
    position_sizing: params.position_sizing,
    params: params.params ? JSON.stringify(params.params) : undefined,
    overrides: params.overrides ? JSON.stringify(params.overrides) : undefined,
    mode: params.mode,
    holding_days: params.holding_days,
    asset_type: params.asset_type,
    minute_fill: params.minute_fill,
    regime_filter: params.regime_filter ? JSON.stringify(params.regime_filter) : undefined,
  })

  // 存 reconnect 信息 (刷新后用) + 上报注册表 (其他设备可接回)
  localStorage.setItem(RECONNECT_KEY, qs)
  reportTaskRunning(TASK_KIND, { qs }, params.strategy_id)
  emit()
  if (current?.id !== id || !current.isPending) return

  connectSSE(`/api/backtest/strategy/stream?${qs}`)
}

/** 停止当前回测任务 (调后端 cancel, 后端 cancel_event → 停止计算) */
export async function stopBacktest(): Promise<void> {
  if (batch?.running) {
    await stopBacktestBatch()
    return
  }
  await cancelCurrentTask()
}

export async function stopBacktestBatch(): Promise<void> {
  if (!batch?.running) return
  batch = { ...batch, running: false, items: batch.items.map(item => item.status === 'queued' ? { ...item, status: 'cancelled', error: '队列已停止，未运行' } : item) }
  emitBatch()
  await cancelCurrentTask()
}

async function cancelCurrentTask(): Promise<void> {
  const id = current?.id
  const qs = localStorage.getItem(RECONNECT_KEY)
  // Invalidate callbacks immediately, including a still-pending preflight fetch.
  connectionSeq += 1
  eventSource?.close()
  eventSource = null
  if (qs) {
    try {
      const response = await fetch('/api/backtest/strategy/cancel', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ qs }),
      })
      if (!response.ok) throw new Error('cancel failed')
    } catch {
      if (id !== undefined) completeTask(id, { error: '取消请求未确认，任务状态未知，请先重连确认' }, 'unknown')
      return
    }
  }
  if (id !== undefined && current?.id === id && current.isPending) {
    completeTask(id, { error: '已取消' }, 'cancelled')
  } else if (current?.id === id && localStorage.getItem(RECONNECT_KEY) === qs) {
    finishTaskRegistry()
    if (current) {
      current = { ...current, error: '已取消', reconnecting: false }
      emit()
    }
  }
}

/** 清除任务状态 (隐藏提示) */
export function clearBacktest(): void {
  if (current?.isPending || batch?.running) return
  current = null
  emit()
}

/** 换设备接回: 本地无键时从服务端注册表找 running 任务, 回填本地键并走原有重连。 */
async function adoptFromServer() {
  if (current && !current.error && !current.result) return // 本机已有活动任务
  const task = await fetchRunningTask(TASK_KIND)
  if (current?.isPending || batch?.running) return
  const qs = typeof task?.payload?.qs === 'string' ? task.payload.qs : ''
  if (!qs || localStorage.getItem(RECONNECT_KEY)) return
  localStorage.setItem(RECONNECT_KEY, qs)
  tryReconnect()
}

/** 恢复: 从 localStorage 读取 reconnect 信息, 重新连接 (刷新后调用);
 *  本地无键时异步尝试从服务端注册表接回 (换设备场景)。 */
export function tryReconnect(): boolean {
  if (current?.isPending || batch?.running) return false
  const qs = localStorage.getItem(RECONNECT_KEY)
  if (!qs) {
    void adoptFromServer()
    return false
  }
  // 有未完成的任务, 重连
  const id = ++taskSeq
  current = { id, isPending: true, result: null, progress: null, error: null, reconnecting: false }
  emit()
  connectSSE(`/api/backtest/strategy/stream?${qs}`)
  return true
}

/** React hook: 读取当前全局回测任务状态 */
export function useBacktestTask(): BacktestTask | null {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
