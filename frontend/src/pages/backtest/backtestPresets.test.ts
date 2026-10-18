import { describe, expect, it } from 'vitest'
import { batchBacktestConfig, researchBacktestPreset, researchConfigError } from './backtestPresets'

describe('research batch request isolation', () => {
  const base = { strategy_id: 'old', start: '2025-01-01', end: '2026-10-16', initial_capital: 500000,
    max_positions: 10, mode: 'full' as const, position_sizing: 'score_weight', minute_fill: true,
    params: { leaked: true }, overrides: { stop_loss: 0.05 }, symbols: ['600000.SH'], regime_filter: { amv: 'confirmed' }, stamp_tax_pct: 0.001 }
  it('uses each smallcap strategy constraints and keeps shared dates and capital', () => {
    const config = batchBacktestConfig(base, 'custom_joinquant_smallcap', { anchor_date: '2024-01-01' }, {})
    expect(config).toMatchObject({ max_positions: 3, mode: 'position', max_exposure_pct: 1,
      position_sizing: 'equal', minute_fill: false, entry_fill: 'open_t+1', exit_fill: 'open_t+1',
      symbols: null, regime_filter: null, overrides: {}, params: { anchor_date: '2024-01-01' },
      initial_capital: 500000, start: '2025-01-01', end: '2026-10-16' })
    expect(config.params).not.toHaveProperty('leaked')
    expect(base.max_positions).toBe(10)
  })
  it('does not mix dividend params or fixed stamp tax with current strategy', () => {
    const config = batchBacktestConfig(base, 'custom_dividend_enhanced_daily_local', {}, {})
    expect(config).toMatchObject({ max_positions: 5, params: {}, overrides: {}, commission_pct: 0.00015 })
    expect(config.stamp_tax_pct).toBeUndefined()
    expect(researchBacktestPreset('custom_graham_s0')?.max_positions).toBe(20)
  })
  it('keeps generic user settings while using the new strategy own params and overrides', () => {
    const config = batchBacktestConfig(base, 'custom_example', { lookback: 20 }, { stop_loss: 0.08 })
    expect(config.max_positions).toBe(10)
    expect(config.params).toEqual({ lookback: 20 })
    expect(config.overrides).toEqual({ stop_loss: 0.08 })
  })
})

// A saved form from another strategy must fail before starting an expensive run.
it('explains the stale 10-slot smallcap form and accepts its required baseline', () => {
  const wrong = { strategy_id: 'custom_joinquant_smallcap', max_positions: 10 }
  expect(researchConfigError(wrong)).toContain('持仓数量')
  expect(researchConfigError({ ...wrong, ...researchBacktestPreset(wrong.strategy_id) })).toBeNull()
})
