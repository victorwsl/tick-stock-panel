// @vitest-environment jsdom
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import { expect, it } from 'vitest'
import { ReviewSortHeader, sortReviewRows } from './reviewSort'
import { buildTradeKeyMap, buildTradesNavItems } from './tradeNav'
import type { StrategyBacktestTrade } from '@/lib/api'

const trades = [
  { symbol: 'A', pnl_amount: -100, pnl_pct: -.2, duration: 2, position_pct: .1, entry_score: 2 },
  { symbol: 'B', pnl_amount: 1000, pnl_pct: .1, duration: 20, position_pct: .3, entry_score: 10 },
  { symbol: 'C', pnl_amount: 100, pnl_pct: .3, duration: 5, position_pct: .2, entry_score: null },
] as StrategyBacktestTrade[]

it('先按数值排序再取TopK，回放与排序后表格同序，原始结果不变', () => {
  const sorted = sortReviewRows(trades, { key: 'pnl_amount', direction: 'desc' }, 2)
  expect(sorted.map(t => t.symbol)).toEqual(['B', 'C'])
  expect(buildTradesNavItems(sorted, buildTradeKeyMap(trades)).map(t => t.trade.symbol)).toEqual(['B', 'C'])
  expect(trades.map(t => t.symbol)).toEqual(['A', 'B', 'C'])
  expect(sortReviewRows(trades, { key: 'pnl_pct', direction: 'desc' }).map(t => t.symbol)).toEqual(['C', 'B', 'A'])
  expect(sortReviewRows(trades, { key: 'duration', direction: 'asc' }).map(t => t.symbol)).toEqual(['A', 'C', 'B'])
  expect(sortReviewRows(trades, { key: 'position_pct', direction: 'desc' })[0].symbol).toBe('B')
})

it('缺失分数在升降序下都排最后，同值顺序稳定；空表和未排序保留正常行为', () => {
  expect(sortReviewRows(trades, { key: 'entry_score', direction: 'asc' }).map(t => t.symbol)).toEqual(['A', 'B', 'C'])
  expect(sortReviewRows(trades, { key: 'entry_score', direction: 'desc' }).map(t => t.symbol)).toEqual(['B', 'A', 'C'])
  expect(sortReviewRows([{ value: NaN }, { value: 2 }, { value: 2 }], { key: 'value', direction: 'desc' }).map(t => t.value)).toEqual([2, 2, NaN])
  expect(sortReviewRows([], null, 10)).toEqual([])
  expect(sortReviewRows(trades, null)).toEqual(trades)
})

it('列头支持键盘可达按钮和排序方向提示', async () => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })
  const host = document.createElement('div'), root = createRoot(host)
  let selected = ''
  try {
    await act(async () => root.render(<table><thead><tr><ReviewSortHeader column="duration"
      sort={{ key: 'duration', direction: 'desc' }} onSort={key => { selected = key }}>
      持仓天数
    </ReviewSortHeader></tr></thead></table>))
    expect(host.querySelector('th')?.getAttribute('aria-sort')).toBe('descending')
    await act(async () => host.querySelector('button')!.click())
    expect(selected).toBe('duration')
  } finally { await act(async () => root.unmount()) }
})
