import type { BacktestRunParams } from '@/lib/backtestTask'

// These are the already documented research baselines, not strategy signal rules.
export function researchBacktestPreset(id: string): Partial<BacktestRunParams> | null {
  const common: Partial<BacktestRunParams> = {
    asset_type: 'stock', mode: 'position', entry_fill: 'open_t+1', exit_fill: 'open_t+1',
    matching: 'open_t+1', minute_fill: false, position_sizing: 'equal',
    max_exposure_pct: 1, symbols: null, regime_filter: null, overrides: {},
  }
  if (id === 'custom_joinquant_smallcap') return {
    ...common, max_positions: 3, commission_pct: 0.0003, stamp_tax_pct: 0.001, slippage_bps: 10,
  }
  if (id.startsWith('custom_graham_')) return {
    ...common, max_positions: 20, commission_pct: 0.0002, stamp_tax_pct: undefined, slippage_bps: 10,
  }
  if (id === 'custom_dividend_enhanced_daily_local') return {
    ...common, max_positions: 5, commission_pct: 0.00015, stamp_tax_pct: undefined, slippage_bps: 10,
  }
  if (id === 'personal_zhixing_early_v0') return {
    entry_fill: 'open_t+1', exit_fill: 'open_t+1', minute_fill: false,
  }
  return null
}

export function batchBacktestConfig(
  base: BacktestRunParams,
  id: string,
  params: Record<string, unknown>,
  overrides: Record<string, unknown>,
): BacktestRunParams {
  return { ...base, strategy_id: id, params: { ...params }, overrides: { ...overrides }, ...researchBacktestPreset(id) }
}

export function researchPresetLabel(id: string): string {
  if (id === 'custom_joinquant_smallcap') return '3只 · 等额 · 满仓上限 · 佣金万3'
  if (id.startsWith('custom_graham_')) return '20槽 · 等权 · 满仓上限 · 佣金万2'
  if (id === 'custom_dividend_enhanced_daily_local') return '5只 · 等额 · 满仓上限 · 佣金万1.5'
  if (id === 'custom_dividend_enhanced_strict') return '原版输入未对齐，暂不可回测'
  if (id === 'personal_zhixing_early_v0') return '日线 · 次日开盘 · 三日验证'
  return '使用左侧账户与范围设置'
}

export function researchConfigError(config: BacktestRunParams): string | null {
  if (config.strategy_id === 'custom_dividend_enhanced_strict') return '红利原版输入尚未对齐，请使用日频本地研究版。'
  const preset = researchBacktestPreset(config.strategy_id)
  if (!preset) return null
  const fields: [keyof BacktestRunParams, string][] = [
    ['max_positions', '持仓数量'], ['mode', '仓位模拟'], ['position_sizing', '等额分配'],
    ['entry_fill', '次日开盘买入'], ['exit_fill', '次日开盘卖出'], ['minute_fill', '关闭分钟细化'],
  ]
  const errors = fields.filter(([key]) => preset[key] != null && config[key] !== preset[key]).map(([, label]) => label)
  if (preset.symbols === null && config.symbols?.length) errors.push('完整策略股票池')
  if (preset.regime_filter === null && config.regime_filter) errors.push('关闭额外择时')
  if (config.strategy_id === 'custom_joinquant_smallcap' && config.max_exposure_pct !== 1) errors.push('100%仓位上限')
  return errors.length ? `当前配置不匹配：${errors.join('、')}。请恢复研究基线后运行。` : null
}
