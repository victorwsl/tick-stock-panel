import { useEffect, useMemo, useRef } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { X } from 'lucide-react'
import { StockPanel } from '@/components/StockPanel'
import { NavPager, NavWrapToast } from '@/components/NavPager'
import type { ChartMarker } from '@/components/EChartsCandlestick'
import type { StrategyBacktestResult, StrategyBacktestTrade } from '@/lib/api'
import { navItemKey, type NavItem } from '@/lib/listNav'
import { useListNav } from '@/lib/useListNav'
import { fmtPct, fmtPrice, priceColorClass } from '@/lib/format'
import { boardTag } from '@/lib/board'
import { useDialogBackdrop } from '@/lib/useDialogBackdrop'
import { cnSignal } from '@/lib/signals'
import { useCustomSignalNames } from '@/lib/useCustomSignalNames'

interface Props {
  /** 选中的标的 (null = 关闭) */
  symbol: string | null
  /** 回测结果, 用于取该标的所有交易与聚合统计 */
  result: StrategyBacktestResult | null
  /** 回测有效区间 (图默认展示范围) */
  periodStart: string
  periodEnd: string
  /** 有序候选列表 (策略分析全部标的): 提供后支持左右键/顶栏按钮切标的 */
  navList?: NavItem[]
  /** 切标的回调: 收到目标 symbol/name, 由调用方更新选中状态 */
  onNavigate?: (symbol: string, name?: string) => void
  onClose: () => void
}

/** 「策略分析」行的标的级K线弹窗 (单笔回放见 TradeKlineModal) */
export function PicksSymbolKlineModal({ symbol, result, periodStart, periodEnd, navList, onNavigate, onClose }: Props) {
  const backdrop = useDialogBackdrop(onClose)
  const signalNames = useCustomSignalNames()

  // onClose 只有 ESC 用; onNavigate 由 useListNav 内部承接 (支持内联 lambda)
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  const nav = useListNav<NavItem>({
    items: navList ?? [],
    keyOf: navItemKey,
    currentKey: symbol,
    onNavigate: n => onNavigate?.(n.symbol, n.name),
    wrapHints: { head: '已到榜首', tail: '已到末尾' },
  })

  useEffect(() => {
    if (!symbol) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [symbol])

  const view = useMemo(() => {
    if (!symbol || !result) return { trades: [] as StrategyBacktestTrade[], stat: null, name: '' }
    const trades = result.trades.filter(t => t.symbol === symbol)
    return {
      trades,
      stat: result.per_symbol_stats.find(p => p.symbol === symbol) ?? null,
      name: trades.find(t => t.name)?.name ?? '',
    }
  }, [symbol, result])
  const { trades, stat, name } = view

  const markers = useMemo<ChartMarker[]>(() => {
    const reasons: Record<string, string> = {
      stop_loss: '止损', take_profit: '止盈', trailing_stop: '移动止损',
      trailing_take_profit: '回撤止盈', max_hold: '持有到期',
      pending_exit: '待卖', end: '期末退出', signal: '退出信号',
    }
    const signalName = (id: string | null | undefined, fallback: string) => {
      if (id === 'zx_below_white') return '跌破白线'
      if (id === 'zx_pullback_restrength') return '回调再转强'
      return id ? cnSignal(id, signalNames) : fallback
    }
    // 同日同方向同阶段合并；信号日期缺失时不能用成交日倒推。
    const groups = new Map<string, ChartMarker & { count: number; details: string[]; reasons: Set<string> }>()
    const add = (date: string | null | undefined, kind: 'buy' | 'sell', phase: 'signal' | 'fill', reason: string, detail: string) => {
      if (!date) return
      const day = String(date).slice(0, 10)
      const key = `${day}:${kind}:${phase}`
      const old = groups.get(key)
      if (old) { old.count++; old.details.push(detail); old.reasons.add(reason); return }
      groups.set(key, { date: day, kind, phase, count: 1, details: [detail], reasons: new Set([reason]) })
    }
    for (const t of trades) {
      const entry = signalName(t.entry_signal_id, '入场信号')
      const exit = t.exit_reason === 'signal'
        ? signalName(t.exit_signal_id, '退出信号')
        : reasons[t.exit_reason] ?? t.exit_reason
      add(t.entry_signal_date, 'buy', 'signal', entry, `买入信号：${entry}`)
      add(t.entry_date, 'buy', 'fill', '', `买入成交 ${fmtPrice(t.entry_price)}；信号日 ${t.entry_signal_date || '未记录'}`)
      // 期末退出是回测边界操作，不冒充策略退出触发；待卖仅画已记录的触发。
      if (t.exit_reason !== 'end') add(t.exit_signal_date, 'sell', 'signal', exit, `退出触发：${exit}`)
      if (t.exit_reason === 'pending_exit') continue
      add(t.exit_date, 'sell', 'fill', exit, `卖出成交 ${fmtPrice(t.exit_price)}；原因：${exit}；触发日 ${t.exit_signal_date || '未记录'}${t.blocked_exit_days ? `；受阻${t.blocked_exit_days}天` : ''}`)
    }
    return [...groups.values()].map(({ count, details, reasons, ...m }) => {
      const reason = [...reasons].filter(Boolean).join('、')
      const label = m.phase === 'signal'
        ? (m.kind === 'buy' ? '买信号' : `触发·${reason}`)
        : (m.kind === 'buy' ? '买入' : `卖·${reason}`)
      return { ...m, label: label + (count > 1 ? ` ×${count}` : ''), detail: [m.date, ...details].join('\n') }
    }).sort((a, b) => a.date.localeCompare(b.date))
  }, [trades, signalNames])

  const tag = symbol ? boardTag(symbol) : ''
  const dateRange = useMemo(() => ({ start: periodStart, end: periodEnd }), [periodStart, periodEnd])

  return (
    <AnimatePresence>
      {symbol && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            {...backdrop}
          />
          <motion.div
            initial={{ opacity: 0, scale: 0.95, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 8 }}
            transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
            className="relative flex max-h-[94vh] w-[92vw] max-w-[1120px] flex-col overflow-hidden rounded-card border border-border bg-base shadow-2xl"
          >
            <div className="border-b border-border px-5 py-3">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-sm font-semibold text-foreground">{symbol}</span>
                    {name && <span className="truncate text-sm text-foreground">{name}</span>}
                    {tag && (
                      <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">
                        {tag}
                      </span>
                    )}
                    <span className="rounded border border-accent/30 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent">标的回放</span>

                    {/* 切标的: 上一只 / n·N / 下一只 */}
                    <NavPager nav={nav} prevLabel="上一只" nextLabel="下一只" />
                  </div>
                  <div className="mt-1 text-[11px] text-muted">
                    回测 {periodStart} ~ {periodEnd} · {trades.length} 笔交易
                  </div>
                  <div className="mt-1 text-[10px] text-muted/70">
                    图下买入、图上卖出 · 空心菱形为信号/触发，实心圆为成交 · 卖出旁注明原因，悬停看日期与价格 · 同日多笔合并
                  </div>
                </div>

                <div className="flex shrink-0 items-center gap-3 text-xs">
                  {stat && (
                    <div className="flex items-center gap-3">
                      <div className="text-right">
                        <div className="text-muted">总收益</div>
                        <div className={`num font-semibold ${priceColorClass(stat.total_return)}`}>{fmtPct(stat.total_return)}</div>
                      </div>
                      <div className="text-right">
                        <div className="text-muted">胜率</div>
                        <div className="num text-secondary">{fmtPct(stat.win_rate)}</div>
                      </div>
                      <div className="text-right">
                        <div className="text-muted">最佳 / 最差</div>
                        <div className="num">
                          <span className="text-bull">{fmtPct(stat.best)}</span>
                          <span className="text-muted"> / </span>
                          <span className="text-bear">{fmtPct(stat.worst)}</span>
                        </div>
                      </div>
                    </div>
                  )}
                  <button
                    onClick={onClose}
                    className="rounded-btn p-1 text-secondary transition-colors hover:bg-elevated hover:text-foreground"
                    aria-label="关闭标的回放"
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>
            </div>

            <div className="flex-1 overflow-auto p-4">
              <StockPanel
                symbol={symbol}
                height={520}
                dateRange={dateRange}
                markers={markers}
                showLimitMarkers={false}
                showMarkerToggle={false}
                showIntraday={false}
                visibleBars="all"
              />
            </div>

            {/* 首↔尾循环弱提示 */}
            <NavWrapToast message={nav.wrapMsg} />
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  )
}
