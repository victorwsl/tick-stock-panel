import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { EChartsOption } from 'echarts'
import { Download, RotateCw } from 'lucide-react'
import { api, type FactorResearchDetail, type FactorResearchRow } from '@/lib/api'
import { QK } from '@/lib/queryKeys'
import { useChartTheme } from '@/lib/theme'
import { useECharts } from '../backtest/charts/useECharts'

const CONTROL = 'rounded-input border border-border bg-surface px-2.5 py-1.5 text-xs text-foreground focus:outline-none focus:ring-2 focus:ring-accent/40'
const PERIOD_NAMES: Record<string, string> = { '2025H1_frozen': '窗口一 · 2025年1—4月', '2025H2_frozen': '窗口二 · 2025年7—10月' }
const format = (v: number | null | undefined, percent = false) => v == null ? '—' : percent ? `${v >= 0 ? '+' : ''}${(v * 100).toFixed(2)}pp` : v.toFixed(3)
const method = (id: string) => id.startsWith('soft5_') ? '基线95%＋因子5%' : id.startsWith('raw_') ? '原尺度等权' : '归一化等权'

function ResultChart({ result, mode }: { result: FactorResearchDetail; mode: 'ic' | 'nav' | 'drawdown' }) {
  const ct = useChartTheme()
  const option = useMemo<EChartsOption>(() => {
    const dates = mode === 'ic' ? result.ic.map(r => r.trade_date) : [...new Set(result.curves.map(r => r.trade_date))].sort()
    const variants = [...new Set(result.curves.map(r => r.variant_id))]
    return {
      animation: false,
      grid: { left: 52, right: 16, top: mode === 'ic' ? 14 : 35, bottom: 30 },
      tooltip: { trigger: 'axis', backgroundColor: ct.tooltipBg, textStyle: { color: ct.tooltipText } },
      legend: { show: mode !== 'ic', textStyle: { color: ct.text, fontSize: 11 } },
      xAxis: { type: 'category', data: dates, axisLabel: { color: ct.text, fontSize: 10, formatter: (v: string) => `${v.slice(4, 6)}-${v.slice(6)}` }, axisLine: { lineStyle: { color: ct.border } } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: ct.text, fontSize: 10, formatter: (v: number) => mode === 'drawdown' ? `${(v * 100).toFixed(0)}%` : v.toFixed(2) }, splitLine: { lineStyle: { color: ct.grid } } },
      series: mode === 'ic' ? [{ name: 'Rank IC', type: 'bar', data: result.ic.map(r => r.rank_ic), itemStyle: { color: '#3B82F6' } }] : variants.map(id => {
        const lookup = new Map(result.curves.filter(r => r.variant_id === id).map(r => [r.trade_date, r]))
        return { name: id === 'baseline' ? '原排名' : '加入5%因子', type: 'line', symbol: 'none', data: dates.map(d => lookup.get(d)?.[mode] ?? null), lineStyle: { width: 1.5 }, itemStyle: { color: id === 'baseline' ? ct.text : '#3B82F6' } }
      }),
    }
  }, [result, mode, ct])
  const ref = useECharts(option, [mode, result])
  return <div ref={ref} className="h-44 w-full" role="img" aria-label={mode === 'ic' ? '逐日Rank IC图' : mode === 'nav' ? '研究组合净值图' : '研究组合回撤图'} />
}

export function FactorValidation() {
  const client = useQueryClient()
  const [selectedStudy, setStudy] = useState('')
  const [horizon, setHorizon] = useState(10)
  const [k, setK] = useState(3)
  const [cohort, setCohort] = useState('own_mature')
  const [period, setPeriod] = useState('2025H2_frozen')
  const [factor, setFactor] = useState('low_vol')
  const [search, setSearch] = useState('')
  const studies = useQuery({ queryKey: QK.factorResearchStudies, queryFn: api.factorResearchStudies })
  const id = selectedStudy || studies.data?.studies[0]?.id || ''
  const summary = useQuery({ queryKey: QK.factorResearchSummary(id, horizon, k, cohort), queryFn: () => api.factorResearchSummary(id, horizon, k, cohort), enabled: !!id })
  const detail = useQuery({ queryKey: QK.factorResearchDetail(id, factor, period, horizon, k, cohort), queryFn: () => api.factorResearchDetail(id, factor, period, horizon, k, cohort), enabled: !!id })
  const verify = useMutation({ mutationFn: () => api.factorResearchVerify(id), onSuccess: () => { void client.invalidateQueries({ queryKey: QK.factorResearchStudies }) } })
  const check = studies.data?.studies.find(s => s.id === id)?.verification
  const rows = (summary.data?.rows ?? []).filter(r => r.period === period && `${r.name} ${r.id} ${r.formula}`.toLowerCase().includes(search.toLowerCase()))
  const mature = summary.data?.maturity.find(r => r.period === period && r.horizon === horizon)
  const selected = rows.find(r => r.id === factor && r.variant_id.startsWith('soft5_'))
  const download = () => {
    const cols: (keyof FactorResearchRow)[] = ['name', 'formula', 'variant_id', 'period', 'rank_ic', 'uplift', 'paired_dates', 'nonoverlap_intervals', 'p_holm']
    const text = '\uFEFF' + [['因子', '公式', '评分方法', '窗口', 'Rank IC', '收益增量(小数)', '配对日', '不重叠区间', 'Holm p值'].join(','), ...rows.map(r => cols.map(c => `"${String(r[c] ?? '').replaceAll('"', '""')}"`).join(','))].join('\n')
    const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a'); a.href = url; a.download = `因子验证_${horizon}日_Top${k}.csv`; a.click(); URL.revokeObjectURL(url)
  }
  return <div className="flex h-full min-h-0 flex-col gap-3 overflow-y-auto">
    <div className="flex flex-wrap items-center gap-2">
      <select aria-label="因子实验" className={`${CONTROL} max-w-full`} value={id} onChange={e => { setStudy(e.target.value); verify.reset() }} disabled={!studies.data?.studies.length}>
        {!id && <option value="">尚未导入实验</option>}
        {studies.data?.studies.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
      </select>
      <button type="button" disabled={!id || verify.isPending} onClick={() => verify.mutate()} className={`${CONTROL} inline-flex items-center gap-1.5 disabled:opacity-50`}><RotateCw className="h-3.5 w-3.5" />{verify.isPending ? 'Panel复算中…' : 'Panel独立复算'}</button>
      <button type="button" onClick={download} disabled={!rows.length} className={`${CONTROL} inline-flex items-center gap-1.5 disabled:opacity-50`}><Download className="h-3.5 w-3.5" />导出表格</button>
    </div>
    {(studies.isError || summary.isError || verify.isError) && <div role="alert" className="text-sm text-danger">{String((studies.error || summary.error || verify.error)?.message)} <button type="button" className="underline" onClick={() => { void studies.refetch(); void summary.refetch(); verify.reset() }}>重试读取</button></div>}
    {studies.isLoading && <p className="text-sm text-muted">正在读取因子实验…</p>}
    {!studies.isLoading && !studies.isError && !id && <p className="text-sm text-secondary">尚无冻结实验。先用个人因子导入入口载入经过哈希校验的研究包，再在这里复算和查看。</p>}
    {id && <>
      <p className="text-xs leading-relaxed text-secondary">{check?.status === 'passed' ? `Panel统计对账通过：${check.ic_comparisons.toLocaleString()}条IC、${check.ranking_comparisons.toLocaleString()}组排名增量；最大IC差${check.max_ic_delta.toExponential(1)}。` : '尚未完成Panel独立统计复算。'} 此状态不代表因子已样本外有效，也不代表账户回测已通过。</p>
      <div className="flex flex-wrap gap-2">
        <select aria-label="收益观察期限" value={horizon} onChange={e => setHorizon(Number(e.target.value))} className={CONTROL}>{[10,20,60,120,250].map(h => <option key={h} value={h}>{h}交易日</option>)}</select>
        <select aria-label="候选数量" value={k} onChange={e => setK(Number(e.target.value))} className={CONTROL}>{[1,3,5].map(n => <option key={n} value={n}>Top{n}</option>)}</select>
        <select aria-label="样本窗口" value={period} onChange={e => setPeriod(e.target.value)} className={CONTROL}>{(summary.data?.study.periods ?? ['2025H1_frozen', '2025H2_frozen']).map(p => <option key={p} value={p}>{PERIOD_NAMES[p] ?? p}</option>)}</select>
        <select aria-label="成熟样本口径" value={cohort} onChange={e => setCohort(e.target.value)} className={CONTROL}><option value="own_mature">各期限成熟样本</option><option value="common_250d_mature">250日历成熟共同日期</option></select>
        <input aria-label="搜索验证因子" placeholder="搜索因子、组合或公式" value={search} onChange={e => setSearch(e.target.value)} className={CONTROL} />
      </div>
      <p className="text-xs text-muted">信号收盘后确定，次日开盘入场；基准399303。增量单位pp＝百分点，未扣费。{mature && `本窗${mature.signal_dates}个信号日，${mature.mature_dates}日历已成熟，${mature.valid_rows.toLocaleString()}个有效股票日。`}</p>
      <div className="grid min-h-0 gap-4 xl:grid-cols-[minmax(0,1.6fr)_minmax(320px,1fr)]">
        <div className="min-w-0 overflow-x-auto rounded-card border border-border bg-surface/80">
          {summary.isLoading ? <p className="p-4 text-sm text-muted">正在读取当前期限的验证数据…</p> : rows.length === 0 ? <p className="p-4 text-sm text-muted">没有匹配的因子，请修改搜索条件。</p> : <table className="w-full min-w-[730px] text-xs tabular-nums">
            <thead className="bg-elevated text-left text-secondary"><tr>{['因子 / 评分方法', 'Rank IC', 'TopK增量', '配对日 / 不重叠段', '证据', '查看'].map(t => <th key={t} className="px-3 py-2.5 font-medium">{t}</th>)}</tr></thead>
            <tbody>{rows.map(r => <tr key={r.variant_id} className={`border-t border-border/70 ${r.id === factor ? 'bg-accent/5' : ''}`}>
              <td className="max-w-64 px-3 py-2.5"><div className="font-medium text-foreground">{r.name}</div><div className="mt-1 text-muted">{method(r.variant_id)}</div><div className="mt-1 max-w-64 truncate text-muted" title={r.formula}>{r.formula}</div></td>
              <td className="px-3 py-2.5 text-foreground">{format(r.rank_ic)}</td><td className="px-3 py-2.5 font-medium text-foreground">{format(r.uplift, true)}</td>
              <td className="px-3 py-2.5 text-secondary">{r.paired_dates} / {r.nonoverlap_intervals}</td>
              <td className="px-3 py-2.5 text-secondary">{r.uplift == null ? '有效样本不足' : r.inference === 'descriptive_only_short_history' ? '仅描述统计' : r.p_holm != null && r.p_holm < .05 ? '本窗修正检验通过' : r.uplift > 0 ? '本窗正增量，待验证' : '本窗未改善排名'}</td>
              <td className="px-3 py-2.5"><button type="button" disabled={!r.variant_id.startsWith('soft5_')} onClick={() => setFactor(r.id)} className="rounded-btn px-2 py-1 text-accent hover:bg-accent/10 focus:outline-none focus:ring-2 focus:ring-accent/40 disabled:text-muted" aria-label={`查看${r.name}图表`}>{r.variant_id.startsWith('soft5_') ? '图表' : '仅增量'}</button></td>
            </tr>)}</tbody>
          </table>}
        </div>
        <section className="min-w-0" aria-label="因子图表详情">
          <h2 className="text-sm font-semibold text-foreground">{selected?.name ?? factor} · {horizon}日 Rank IC</h2>
          <p className="mt-1 text-xs text-secondary">因子原值的候选内预测相关，与5%融合后的TopK增量分别评价。</p>
          {detail.isLoading && <p className="py-4 text-xs text-muted">图表加载中…</p>}
          {detail.isError && <p role="alert" className="py-4 text-xs text-danger">图表读取失败：{detail.error.message} <button type="button" onClick={() => { void detail.refetch() }} className="underline">重试</button></p>}
          {detail.data && <>
            {detail.data.ic.length ? <ResultChart result={detail.data} mode="ic" /> : <p className="py-4 text-xs text-muted">当前样本没有有效IC。</p>}
            <h3 className="mt-4 text-sm font-medium text-foreground">研究净值与回撤</h3>
            <p className="mt-1 text-xs leading-relaxed text-secondary">{detail.data.curve_note}</p>
            {detail.data.curves.length > 0 && <><ResultChart result={detail.data} mode="nav" /><ResultChart result={detail.data} mode="drawdown" /></>}
          </>}
        </section>
      </div>
      {summary.data && <details className="pb-3 text-xs leading-relaxed text-secondary"><summary className="cursor-pointer font-medium text-foreground">定义、样本与缺失说明</summary><p className="mt-2">{summary.data.study.baseline}。{summary.data.study.label_contract}。{summary.data.study.fusion}。</p>{summary.data.study.limitations.map(s => <p key={s} className="mt-1">{s}</p>)}<p className="mt-1">本表为冻结候选研究；Panel“检验”和“因子库”保留原有常见因子及各自口径。原尺度/归一化CH排序仅显示排名增量，不复用组合原值IC。</p><p className="mt-1">H日检验用H−1阶HAC；不足2H配对日只报描述统计。Top3以21因子×5期限做Holm修正，其他TopK和等权消融只作描述。</p>{summary.data.label_audit.filter(r => r.horizon === horizon && r.status !== 'valid').map(r => <p key={r.status} className="mt-1">{({ immature: '观察期未走完', benchmark_missing: '基准缺价', entry_open_missing: '入场开盘缺价', endpoint_close_missing: '终点收盘缺价' } as Record<string, string>)[r.status] ?? '其他缺失'}：{r.rows}个股票日（两窗合计）。</p>)}</details>}
    </>}
  </div>
}
