// @vitest-environment node
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./taskRegistry', () => ({
  fetchRunningTask: vi.fn(async () => null),
  reportTaskFinished: vi.fn(),
  reportTaskRunning: vi.fn(),
}))

class MockEventSource {
  static instances: MockEventSource[] = []
  closed = false
  onopen: (() => void) | null = null
  handlers = new Map<string, (event: { data?: string }) => void>()
  constructor(public url: string) { MockEventSource.instances.push(this) }
  addEventListener(name: string, callback: (event: { data?: string }) => void) { this.handlers.set(name, callback) }
  close() { this.closed = true }
  send(name: string, data?: unknown) {
    this.handlers.get(name)?.({ data: data === undefined ? undefined : JSON.stringify(data) })
  }
}

let task: typeof import('./backtestTask')
const entries = () => ['first', 'second', 'third'].map(name => ({ name, params: { strategy_id: name } }))
const flush = async () => { await vi.waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0)) }
const statuses = () => task.getBacktestBatch()?.items.map(item => item.status)

beforeEach(async () => {
  vi.resetModules()
  const values = new Map<string, string>()
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value) },
    removeItem: (key: string) => { values.delete(key) },
  })
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, body: { cancel: async () => {} } })))
  task = await import('./backtestTask')
})

describe('sequential backtest batch', () => {
  it('waits for each terminal event, saves each history id and ignores old late events', async () => {
    task.startBacktestBatch(entries())
    await flush()
    expect(MockEventSource.instances).toHaveLength(1)
    expect(statuses()).toEqual(['running', 'queued', 'queued'])
    const first = MockEventSource.instances[0]
    first.send('progress', { day: 1, total: 10 })
    expect(MockEventSource.instances).toHaveLength(1)
    first.send('done', { history_id: 'h1' })
    await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(2))
    expect(first.closed).toBe(true)
    expect(new URL(MockEventSource.instances[1].url, 'http://test').searchParams.get('strategy_id')).toBe('second')
    expect(new URLSearchParams(localStorage.getItem('backtest_reconnect')!).get('strategy_id')).toBe('second')
    first.send('done', { history_id: 'stale' })
    first.send('error', { message: 'stale' })
    expect(statuses()).toEqual(['success', 'running', 'queued'])
    expect(MockEventSource.instances[1].closed).toBe(false)
    MockEventSource.instances[1].send('done', { history_id: 'h2' })
    await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(3))
    MockEventSource.instances[2].send('done', { history_id: 'h3' })
    expect(task.getBacktestBatch()?.running).toBe(false)
    expect(task.getBacktestBatch()?.items.map(item => item.history_id)).toEqual(['h1', 'h2', 'h3'])
    expect(localStorage.getItem('backtest_reconnect')).toBeNull()
  })

  it('continues explicit failures and preserves history save errors without reporting result.error as success', async () => {
    task.startBacktestBatch(entries())
    await flush()
    MockEventSource.instances[0].send('done', { error: 'not enough data' })
    await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(2))
    expect(task.getBacktestBatch()?.items[0]).toMatchObject({ status: 'error', error: 'not enough data' })
    MockEventSource.instances[1].send('error', { message: 'strategy failed' })
    await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(3))
    MockEventSource.instances[2].send('done', { history_error: 'disk full' })
    expect(task.getBacktestBatch()?.items[2]).toMatchObject({ status: 'success', history_error: 'disk full' })
  })

  it('stops remaining items on user cancellation and closes the active stream', async () => {
    task.startBacktestBatch(entries())
    await flush()
    await task.stopBacktestBatch()
    expect(statuses()).toEqual(['cancelled', 'cancelled', 'cancelled'])
    expect(task.getBacktestBatch()?.running).toBe(false)
    expect(MockEventSource.instances[0].closed).toBe(true)
    expect(fetch).toHaveBeenCalledWith('/api/backtest/strategy/cancel', expect.objectContaining({ method: 'POST' }))
    MockEventSource.instances[0].send('done', { history_id: 'late' })
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('retains module state without subscribers and does not reconnect or replace an active batch', async () => {
    const notify = vi.fn()
    const unsubscribe = task.subscribeBacktestBatch(notify)
    task.startBacktestBatch(entries())
    await flush()
    unsubscribe()
    task.tryReconnect()
    task.startBacktest({ strategy_id: 'replacement' })
    task.clearBacktest()
    MockEventSource.instances[0].send('done', { history_id: 'h1' })
    await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(2))
    expect(statuses()).toEqual(['success', 'running', 'queued'])
    expect(new URLSearchParams(localStorage.getItem('backtest_reconnect')!).get('strategy_id')).toBe('second')
  })

  it('stops on unknown connection state and retains the current reconnect key', async () => {
    task.startBacktestBatch(entries())
    await flush()
    for (let n = 0; n < 6; n += 1) MockEventSource.instances[0].send('error')
    expect(statuses()).toEqual(['error', 'cancelled', 'cancelled'])
    expect(task.getBacktestBatch()?.running).toBe(false)
    expect(localStorage.getItem('backtest_reconnect')).not.toBeNull()
    expect(() => task.startBacktestBatch(entries())).toThrow('状态尚未确认')
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('refresh reconnects only the active task, without restoring queued items', async () => {
    task.startBacktestBatch(entries())
    await flush()
    vi.resetModules()
    task = await import('./backtestTask')
    expect(task.getBacktestBatch()).toBeNull()
    expect(task.tryReconnect()).toBe(true)
    await vi.waitFor(() => expect(MockEventSource.instances).toHaveLength(2))
    MockEventSource.instances[1].send('done', { history_id: 'h1' })
    expect(MockEventSource.instances).toHaveLength(2)
  })

  it('does not open a stale EventSource when cancelled during preflight', async () => {
    let resolveProbe!: (value: Response) => void
    vi.mocked(fetch).mockImplementationOnce(() => new Promise(resolve => { resolveProbe = resolve }) as Promise<Response>)
    task.startBacktestBatch(entries())
    await task.stopBacktestBatch()
    resolveProbe({ ok: true, body: { cancel: async () => {} } } as unknown as Response)
    await new Promise(resolve => setTimeout(resolve, 0))
    expect(MockEventSource.instances).toHaveLength(0)
    expect(statuses()).toEqual(['cancelled', 'cancelled', 'cancelled'])
  })

  it('stops if backend reports cancellation', async () => {
    task.startBacktestBatch(entries())
    await flush()
    MockEventSource.instances[0].send('error', { message: '已取消' })
    expect(statuses()).toEqual(['cancelled', 'cancelled', 'cancelled'])
    expect(task.getBacktestBatch()?.running).toBe(false)
  })

  it('lets a synchronous subscriber stop the queue at a task boundary', async () => {
    task.startBacktestBatch(entries())
    await flush()
    task.subscribeBacktestBatch(() => {
      const state = task.getBacktestBatch()
      if (state?.running && state.items[0].status === 'success') void task.stopBacktestBatch()
    })
    MockEventSource.instances[0].send('done', { history_id: 'h1' })
    expect(statuses()).toEqual(['success', 'cancelled', 'cancelled'])
    expect(localStorage.getItem('backtest_reconnect')).toBeNull()
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('continues a rejected request but halts on an ambiguous server error', async () => {
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false, status: 400, json: async () => ({ detail: 'invalid params' }) } as Response)
    task.startBacktestBatch(entries())
    await flush()
    expect(statuses()).toEqual(['error', 'running', 'queued'])
    expect(task.getBacktestBatch()?.items[0].error).toBe('invalid params')
    await task.stopBacktestBatch()
    vi.mocked(fetch).mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ detail: 'server unavailable' }) } as Response)
    task.startBacktestBatch(entries())
    await vi.waitFor(() => expect(task.getBacktestBatch()?.running).toBe(false))
    expect(statuses()).toEqual(['error', 'cancelled', 'cancelled'])
    expect(localStorage.getItem('backtest_reconnect')).not.toBeNull()
  })

  it('keeps the reconnect key and stops the queue when cancellation is not acknowledged', async () => {
    task.startBacktestBatch(entries())
    await flush()
    vi.mocked(fetch).mockRejectedValueOnce(new Error('offline'))
    await task.stopBacktestBatch()
    expect(statuses()).toEqual(['error', 'cancelled', 'cancelled'])
    expect(task.getBacktestBatch()?.items[0].error).toContain('任务状态未知')
    expect(localStorage.getItem('backtest_reconnect')).not.toBeNull()
  })


  it('reports rejected starts instead of silently ignoring user actions', async () => {
    expect(() => task.startBacktestBatch([])).toThrow('至少选择一个')
    task.startBacktest({ strategy_id: 'single' })
    expect(() => task.startBacktestBatch(entries())).toThrow('已有回测正在运行')
    await task.stopBacktest()
    task.startBacktestBatch(entries())
    expect(() => task.startBacktestBatch(entries())).toThrow('批量回测正在运行')
  })


  it('allows explicitly stopping an unknown task before starting a fresh queue', async () => {
    task.startBacktestBatch(entries())
    await flush()
    for (let n = 0; n < 6; n += 1) MockEventSource.instances[0].send('error')
    await task.stopBacktest()
    expect(localStorage.getItem('backtest_reconnect')).toBeNull()
    expect(() => task.startBacktestBatch(entries())).not.toThrow()
    expect(statuses()).toEqual(['running', 'queued', 'queued'])
  })


  it('rejects a single run while the preceding task remains unknown', async () => {
    task.startBacktest({ strategy_id: 'unknown' })
    await flush()
    for (let n = 0; n < 6; n += 1) MockEventSource.instances[0].send('error')
    const reconnectKey = localStorage.getItem('backtest_reconnect')
    expect(() => task.startBacktest({ strategy_id: 'replacement' })).toThrow('状态尚未确认')
    expect(localStorage.getItem('backtest_reconnect')).toBe(reconnectKey)
    expect(MockEventSource.instances).toHaveLength(1)
    await task.stopBacktest()
    expect(() => task.startBacktest({ strategy_id: 'replacement' })).not.toThrow()
  })

})
