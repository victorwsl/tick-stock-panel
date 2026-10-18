"""聚宽复现的自包含核账报告，全部图形使用内联SVG。"""
from __future__ import annotations

import html
import json


def report(path, spec, stats, curve, dd, benchmark, trades, rejections):
    def esc(v):
        return html.escape(str(v))

    def chart(title, series, percent=False):
        values = [v for _, items in series for _, v in items]
        if not values:
            return ''
        lo, hi = min(values), max(values)
        span = hi-lo if hi>lo else max(abs(hi)*.01, .01)
        lo, hi = lo-span*.06, hi+span*.06
        dates = sorted({d for _, items in series for d, _ in items})
        colors = ['#ef8b7e','#60a5fa','#45b5b5']
        parts = []
        for color, (label, items) in zip(colors, series, strict=False):
            points = ' '.join(f'{65+865*dates.index(d)/max(len(dates)-1,1):.2f},{22+218*(hi-v)/(hi-lo):.2f}' for d,v in items)
            parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{points}"/><text x="{75+series.index((label,items))*240}" y="18" style="fill:{color}">{esc(label)}</text>')
        for v in (lo,(lo+hi)/2,hi):
            y=22+218*(hi-v)/(hi-lo)
            label=f'{v:.1%}' if percent else f'{v:.3f}'
            parts.append(f'<line x1="65" x2="930" y1="{y}" y2="{y}" stroke="#344151"/><text x="2" y="{y+4}">{label}</text>')
        parts.append(f'<text x="65" y="266">{dates[0]}</text><text x="840" y="266">{dates[-1]}</text>')
        return f'<section><h2>{title}</h2><svg role="img" aria-label="{title}" viewBox="0 0 960 280">'+''.join(parts)+'</svg></section>'

    nav = [(r['date'],r['value']/spec['config']['initial_capital']) for r in curve]
    bmap={r['date']:r['value'] for r in benchmark}
    common=[r['date'] for r in curve if r['date'] in bmap]
    bench=[(d,bmap[d]/bmap[common[0]]) for d in common] if common else []
    # 账户从初始本金算，基准从区间首日收盘归一；首日开盘已有仓位时两者起点不同，明确披露。
    charts=chart('账户净值与沪深300（基准首日收盘归一）',[('账户 / 初始本金',nav),('沪深300',bench)])
    charts+=chart('账户最大回撤轨迹',[('相对此前最高净值',[(r['date'],r['value']) for r in dd])],True)
    charts+=chart('仓位与可用现金比例',[('股票仓位',[(r['date'],r['exposure']) for r in curve]),('可用现金 / 净值',[(r['date'],r['cash']/r['value']) for r in curve])],True)
    def table(headers, rows):
        return '<div class="scroll"><table><thead><tr>'+''.join(f'<th>{esc(h)}</th>' for h in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join(f'<td>{esc(x)}</td>' for x in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    trade_rows=[]
    for t in trades:
        a=t['entry_attribution']
        trade_rows.append([t['symbol'],t.get('name',''),t['entry_signal_date'],t['entry_date'],
            t['exit_date'],a.get('rank'),f"{a['market_cap']/1e8:.3f}",t.get('shares'),
            f"{t['entry_price']:.3f}",f"{t['exit_price']:.3f}",f"{t['pnl_pct']:.2%}",
            f"{t['pnl_amount']:.2f}",'期末估值，未卖出' if t['exit_reason']=='open_position' else '五日周期清仓'])
    text=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>聚宽小市值·本地复现核账</title>
<style>body{{margin:0;background:#111820;color:#dbe4ef;font:15px/1.7 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:24px 16px}}h1{{font-size:26px}}h2{{font-size:19px;margin-top:28px}}p,li{{max-width:90ch}}section{{border-top:1px solid #344151;margin-top:20px}}svg{{width:100%;min-width:600px;display:block}}svg text{{fill:#aebccc;font-size:12px}}.scroll,section{{overflow-x:auto}}table{{border-collapse:collapse;white-space:nowrap;width:100%;font-size:12px}}td,th{{padding:7px 10px;text-align:right;border-bottom:1px solid #344151}}th{{color:#93afc3}}details{{margin:24px 0}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}</style><main>
<h1>聚宽20—30亿小市值：日频本地复现</h1><p>时段 {esc(spec['config']['start'])} 至 {esc(spec['config']['end'])}，{len(curve)}个交易日。仅为回顾性研究，不是样本外证明。固定调仓锚点 {esc(spec['anchor_date'])}，每5个交易日先卖再买，仍入选也重新成交；未卖出占用资金和名额。</p>
<p>区间收益 <b>{stats['total_return']:.2%}</b>；最大回撤 <b>{abs(stats['max_drawdown']):.2%}</b>；已平仓批次 {stats['n_trades']}；未平仓 {stats['unrealized_positions']}；成交费用合计 {stats['fees_total']:.2f}元。</p>
<p>基准使用沪深300首日收盘起点；策略首日可能开盘成交，首日时点存在差异，不能把首日超额视为严格同步收益。交易价格和账本均为原始价，分红单独计入。</p>
<h2>实现差异与数据限制</h2><ul>{''.join('<li>'+esc(w)+'</li>' for w in spec['warnings'])}</ul>{charts}
<h2>完整交易记录（含期末未平仓）</h2><p>成本包含分摊买入费用，实际买入价及加仓明细在Panel回放和fills.json；未来财务、当日收盘及当日成交量不参与开盘选股。</p>
{table(['代码','名称','市值信号日','买入日','卖出／估值日','排名','市值亿元','股数','成本','退出／估值价','净收益率','盈亏元','原因'],trade_rows)}
<h2>未成交记录</h2>{table(['日期','股票','方向','原因'],[[r['date'],r['symbol'],r['side'],r['reason']] for r in rejections])}
<details><summary>冻结规则与费用配置</summary><pre>{esc(json.dumps(spec,ensure_ascii=False,indent=2))}</pre></details>
<p>同目录保留manifest、逐日净值、逐日持仓、现金账本、完整排序、成交明细及CSV。本报告的图和表全部内嵌，可离线查看。</p></main></html>'''
    path.write_text(text,encoding='utf-8')
