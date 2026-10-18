// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { renderToStaticMarkup } from 'react-dom/server'
import { MiniCandlestick } from './stock-table/MiniCandlestick'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { EChartsCandlestick, type OHLC } from './EChartsCandlestick'

const chart = vi.hoisted(() => ({
  handlers: {} as Record<string, (event?: any) => void>,
  setOption: vi.fn(),
  on: vi.fn(),
  off: vi.fn(),
  dispatchAction: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
  getZr: () => ({ on: vi.fn(), off: vi.fn() }),
  getOption: () => ({ dataZoom: [{ start: 0, end: 100 }] }),
  containPixel: () => false,
  convertFromPixel: () => [0, 0],
}))

vi.mock('echarts', () => ({ init: () => chart }))

// 超过 COMPACT_THRESHOLD(60): 切标的后 dataZoom 一响就会走紧凑态转换, 进而重写 markPoint
const DAYS = Array.from({ length: 62 }, (_, i) =>
  new Date(Date.UTC(2024, 0, 1 + i)).toISOString().slice(0, 10))
const rows = (base: number): OHLC[] =>
  DAYS.map(date => ({ date, open: base, high: base + 1, low: base - 1, close: base, volume: 1 }))

let host: HTMLDivElement
let root: ReturnType<typeof createRoot>

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  chart.handlers = {}
  chart.setOption.mockClear()
  chart.on.mockImplementation((name: string, callback: (event?: any) => void) => {
    chart.handlers[name] = callback
  })
  host = document.createElement('div')
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.unstubAllGlobals()
})

/** 最后一次 setOption 里 K 线的 markPoint (买卖箭头来源) */
function lastMarkPoint(): string {
  const option = chart.setOption.mock.calls.at(-1)?.[0] as any
  const k = option?.series?.find((s: any) => s.name === 'K')
  return JSON.stringify(k?.markPoint ?? null)
}

/** 模拟竖虚线命中某根 K 线 (鼠标在数据区内移动时 echarts 派发的事件) */
async function hoverCandle(index: number) {
  await act(async () => { chart.handlers.updateAxisPointer?.({ axesInfo: [{ value: index }] }) })
}

it('切标的后响应 dataZoom 仍用当前标的的买卖标记, 不回退到上一只', async () => {
  const render = async (symbol: string, data: OHLC[], markerLabel: string, markerDate: string) => {
    await act(async () => root.render(
      <EChartsCandlestick
        data={data}
        markers={[{ date: markerDate, kind: 'buy', label: markerLabel }]}
        symbol={symbol}
        height={400}
        showInfoBar={false}
        visibleBars="all"
        activeIndicators={['vol']}
      />,
    ))
  }

  await render('600000', rows(10), 'A-BUY', DAYS[10])
  await render('000001', rows(100), 'B-SELL', DAYS[20])

  // 切换标的后图表实例被复用, dataZoom 监听仍是最初注册的那个; 首次 setOption 已是当前标的
  expect(lastMarkPoint()).toContain('B-SELL')

  await act(async () => { chart.handlers.dataZoom?.() })

  expect(lastMarkPoint()).toContain('B-SELL')
  expect(lastMarkPoint()).not.toContain('A-BUY')
})

it('切股后鼠标未离开图表, 竖虚线重新命中即恢复「至今/周期」', async () => {
  const render = async (symbol: string) => {
    await act(async () => root.render(
      <EChartsCandlestick data={rows(10)} symbol={symbol} height={400} visibleBars="all" />,
    ))
  }

  await render('600000')
  const surface = host.firstElementChild as HTMLElement
  await act(async () => { surface.dispatchEvent(new MouseEvent('mouseenter')) })
  await hoverCandle(30)
  expect(host.textContent).toContain('至今')

  // 切股: 上一只的悬停上下文作废, 清掉「至今/周期」
  await render('000001')
  expect(host.textContent).not.toContain('至今')

  // 鼠标没离开图表区 (只是切股), 竖虚线重新命中即恢复, 不靠 mouseenter
  await hoverCandle(20)
  expect(host.textContent).toContain('至今')
  expect(host.textContent).toContain('周期')

  // 移出图表区 → 重新收起
  await act(async () => { surface.dispatchEvent(new MouseEvent('mouseleave')) })
  expect(host.textContent).not.toContain('至今')
})


it('回放把触发与成交放到K线外不同的行，保留真实日期及退出原因', async () => {
  const markers = [
    { date: DAYS[10], kind: 'buy' as const, phase: 'signal' as const, label: '买信号' },
    { date: DAYS[11], kind: 'buy' as const, phase: 'fill' as const, label: '买入' },
    { date: DAYS[50], kind: 'sell' as const, phase: 'signal' as const, label: '触发·跌破白线' },
    { date: DAYS[51], kind: 'sell' as const, phase: 'fill' as const, label: '卖·跌破白线', detail: '延迟卖出<说明>' },
  ]
  await act(async () => root.render(
    <EChartsCandlestick data={rows(10)} markers={markers} height={520}
      showInfoBar={false} visibleBars="all" activeIndicators={['vol']} />,
  ))
  const option = chart.setOption.mock.calls.map(c => c[0]).find(o => o.grid)
  const candle = option.series.find((s: any) => s.name === 'K')
  // 色弱用户可通过空心/实心区分涨跌，不依赖红绿色辨认。
  expect(candle.itemStyle.color).toBe('transparent')
  expect(candle.itemStyle.color0).toBe('#089981')
  expect(candle.itemStyle.borderColor).toBe('#F23645')
  const points = candle.markPoint.data
  expect(points.map((p: any) => p.name)).toEqual(markers.map(m => m.date))
  expect(points.map((p: any) => p.symbol)).toEqual(['diamond', 'circle', 'diamond', 'circle'])
  expect(points[0].y).toBeGreaterThan(option.grid[0].top + option.grid[0].height)
  expect(points[1].y).toBeLessThan(points[0].y)
  expect(points[2].y).toBeLessThan(points[3].y)
  expect(points[3].y).toBeLessThan(option.grid[0].top)
  expect(points[3].label.formatter).toBe('卖·跌破白线')
  expect(points[3].label.position).toBe('left')
  expect(points[3].tooltip.formatter()).toContain('&lt;说明&gt;')
  await act(async () => { chart.handlers.dataZoom?.() })
  expect(lastMarkPoint()).toContain('卖·跌破白线')
  expect(lastMarkPoint()).not.toContain('arrow')

  // 用真实 ECharts 渲染同一个配置，避免只验证 mock 的配置对象。
  // jsdom 无 Canvas；SVG 只需文字宽度估计，不依赖像素绘制。
  const context = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    measureText: (text: string) => ({ width: text.length * 11 }),
  } as any)
  const real = await vi.importActual<typeof import('echarts')>('echarts')
  const rendered = real.init(null, undefined, { renderer: 'svg', ssr: true, width: 1000, height: 520 })
  try {
    rendered.setOption(option)
    const svg = rendered.renderToSVGString()
    expect(svg).toContain('卖·跌破白线')
    expect(svg).toContain('买信号')
    expect(svg).not.toContain('NaN')
  } finally { rendered.dispose(); context.mockRestore() }
})


it('列表迷你K线也使用空心阳线、实心青绿阴线', () => {
  const svg = renderToStaticMarkup(<MiniCandlestick rows={[
    { date: '2025-01-02', open: 10, close: 11, high: 12, low: 9 },
    { date: '2025-01-03', open: 11, close: 10, high: 12, low: 9 },
  ] as any} />)
  const doc = new DOMParser().parseFromString(svg, 'image/svg+xml')
  const bodies = doc.querySelectorAll('rect')
  expect(bodies[0].getAttribute('fill')).toBe('none')
  expect(bodies[0].getAttribute('stroke')).toBe('#F23645')
  expect(bodies[1].getAttribute('fill')).toBe('#089981')
})
