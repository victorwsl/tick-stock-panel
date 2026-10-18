"""离线历史信号回放；不生成尚未验收的收益曲线。"""
import hashlib
import html
import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.matrix import build_market_data_matrix
from app.indicators.zhixing import add_zhixing_lines
from app.strategy.personal.zhixing_pullback import META, compute_features


def _chart(frame: pl.DataFrame, signal_date: str, code: str) -> str:
    frame = add_zhixing_lines(frame).tail(150)
    columns = ('close', 'zx_white', 'zx_yellow')
    values = np.array([frame[c].to_numpy() for c in columns], dtype=float)
    low, high = np.nanmin(values)*.98, np.nanmax(values)*1.02
    width, height = 720, 230
    def x(i):
        return 40 + i*650/max(1,frame.height-1)
    def y(v):
        return 200-(v-low)/(high-low)*170
    paths=[]
    for col, color in zip(columns, ('#3b82f6','#64748b','#b08600'), strict=True):
        points=' '.join(f'{x(i):.1f},{y(v):.1f}' for i,v in enumerate(frame[col]) if v is not None)
        paths.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
    dates=[str(d) for d in frame['date']]
    mark=''
    if signal_date in dates:
        i=dates.index(signal_date)
        mark=f'<line x1="{x(i)}" x2="{x(i)}" y1="20" y2="200" stroke="#dc2626" stroke-dasharray="4"/><text x="{x(i)-20}" y="15" fill="#dc2626">信号</text>'
    return f'<section><h3>{html.escape(code)} · {signal_date}</h3><svg viewBox="0 0 {width} {height}" role="img" aria-label="收盘价、知行白黄线与入场信号">{"".join(paths)}{mark}<text x="40" y="225">{dates[0]}</text><text x="590" y="225">{dates[-1]}</text><text x="2" y="30">{high:.1f}</text><text x="2" y="200">{low:.1f}</text></svg></section>'


def review_strategy(data_dir: Path, start: date, end: date) -> dict:
    if start > end:
        raise ValueError('起始日期不能晚于结束日期')
    source = data_dir / 'kline_daily_enriched'
    panel = (pl.scan_parquet(str(source/'**/*.parquet'))
             .filter(pl.col('date') <= end)
             .select('symbol','date','open','high','low','close','volume')
             .sort(['symbol','date']).collect())
    if panel.is_empty() or panel.select(pl.struct('symbol','date').is_duplicated().any()).item():
        raise ValueError('历史为空或键重复，停止信号回放')
    source_end=panel['date'].max()
    if source_end < end:
        raise ValueError(f'数据仅到{source_end}，不能冒充覆盖到{end}')
    run_id=datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    output=data_dir/'research'/'zhixing'/run_id
    output.mkdir(parents=True, exist_ok=False)
    events=[]
    codes=sorted(panel['symbol'].unique().to_list())
    for offset in range(0,len(codes),256):
        batch=panel.filter(pl.col('symbol').is_in(codes[offset:offset+256]))
        market=build_market_data_matrix(batch)
        f=compute_features(market,{})
        for t,a in zip(*np.where(f['entry'] | f['trim_warning']),strict=True):
            day=market.timestamp_labels[t][:10]
            if day < str(start) or day > str(end):
                continue
            def number(key, values=f, row=t, column=a):
                value=float(values[key][row,column])
                return value if np.isfinite(value) else None
            events.append(dict(date=day,symbol=market.symbols[a],entry=bool(f['entry'][t,a]),
                trim_warning=bool(f['trim_warning'][t,a]),close=float(market.close[t,a]),
                white=number('white'),yellow=number('yellow'),j=number('j'),
                planned_stop=number('stop'),known_resistance=number('target'),
                planned_rr=number('planned_rr'),bias=number('bias'),volume_ratio=number('volume_ratio')))
    # 名称与行业仅用于复盘展示，不进入历史信号计算。
    names_path = data_dir / 'instruments' / 'instruments.parquet'
    names = dict(pl.read_parquet(names_path).select('symbol', 'name').iter_rows()) if names_path.exists() else {}
    industry_path = data_dir / 'ext_data' / 'personal_local_memberships' / 'part.parquet'
    industries = dict(pl.read_parquet(industry_path).select('symbol', 'industry').iter_rows()) if industry_path.exists() else {}
    for event in events:
        event['name'] = names.get(event['symbol']) or '名称未收录'
        event['industry'] = industries.get(event['symbol']) or '行业未分类'
    events.sort(key=lambda x:(x['date'],x['symbol']))
    event_frame=pl.DataFrame(events) if events else pl.DataFrame(schema={'date':pl.String,'symbol':pl.String,'entry':pl.Boolean,'trim_warning':pl.Boolean})
    event_frame.write_csv(output/'signals_and_warnings.csv')
    entries=[r for r in events if r['entry']]
    config=dict(strategy_id=META['id'],params={p['id']:p['default'] for p in META['params']},
                start=str(start),end=str(end),signal_time='close',earliest_execution='next_tradable_open',
                execution_performed=False,partial_exit_implemented=False,open_rr_recheck_implemented=False,
                source_start=str(panel['date'].min()),source_end=str(source_end),rows=panel.height,symbols=len(codes),
                entry_events=len(entries),warning_events=sum(r['trim_warning'] for r in events),
                strategy_sha256=hashlib.sha256((Path(__file__).resolve().parents[1]/'strategy/personal/zhixing_pullback.py').read_bytes()).hexdigest())
    (output/'manifest.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    examples=[]
    for event in entries:
        if event['symbol'] in [x['symbol'] for x in examples]:
            continue
        examples.append(event)
        if len(examples)>=8:
            break
    charts=''.join(_chart(panel.filter((pl.col('symbol')==r['symbol']) & (pl.col('date')<=date.fromisoformat(r['date']))),r['date'],r['name']+' · '+r['industry']) for r in examples)
    table=''.join(f"<tr><td>{r['date']}</td><td>{html.escape(r['name'])}</td><td>{html.escape(r['industry'])}</td><td>{r['close']:.2f}</td><td>{r['planned_rr']:.2f}</td><td>{r['bias']:.1%}</td><td>{r['j']:.1f}</td></tr>" for r in entries[:200])
    report=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>知行策略信号核验</title><style>body{{font:16px/1.8 system-ui,sans-serif;color:#20242c;background:#f5f6f7;margin:0}}main{{max-width:960px;margin:auto;padding:24px}}h1{{font-size:28px}}h2{{font-size:21px;margin-top:32px}}section,.card{{background:white;border:1px solid #ddd;border-radius:10px;padding:18px;margin:18px 0}}.notice{{border-left:4px solid #b45309;background:#fff8e8;padding:16px}}svg{{width:100%;height:auto}}table{{border-collapse:collapse;width:100%;white-space:nowrap}}th,td{{padding:8px;border-bottom:1px solid #ddd;text-align:right}}.scroll{{overflow:auto}}small{{color:#666}}</style><main><small>个人量化工作台 · 版本{META['id']}</small><h1>知行回调再转强：先核对信号</h1><div class="notice"><b>这是历史信号回放，不是收益回测。</b>尚未模拟半仓退出、按真实开盘价复算盈亏比或核验分红现金账本，因此不输出年化收益、胜率和账户净值，不据此决定实盘。</div><h2>本轮事实</h2><p>{start} 至 {end}，回放{len(codes):,}只证券；入场候选事件<b>{len(entries):,}</b>次，风险预警{config['warning_events']:,}次。同一股票可多次出现，不能当作已成交或已结束交易。数据起点{config['source_start']}，共{panel.height:,}条；使用起点以来的历史计算均线，不将预热计入研究期。</p><h2>冻结规则</h2><p>白线=EMA(EMA(C,10),10)；黄线=(MA14+MA28+MA57+MA114)/4。白线高于黄线，黄线不低于5个有效交易日前；近10日J曾≤20，当前J回升、收盘突破前一日高点且站上白线。距白线≤6%，计划止损距离≤8%，计划盈亏比≥2。</p><p>止损参考=前10日最低价×0.995；目标参考=前60日已知最高价。计划盈亏比=(参考目标−收盘)/(收盘−参考止损)，没有使用未来高点，也不代表真实可赚到该收益。下一开盘可能使计划失效，这项成交复算尚未实现。</p><p>近20日曾出现5日均量≥5天前60日均量的2倍；10日、20日上涨日总量均大于下跌日总量，涨跌按收盘与前收盘比较。60日最高/最低振幅&lt;80%。无新增因子打分；同分按代码顺序仅作工程基线。</p><p>跌破白线给整仓退出信号。放量阴线配合白线斜率走弱，或连续三日收跌且白线不升，记录半仓风险预警；本轮未把预警当成成交。</p><h2>图上逐个核对</h2><p>蓝=收盘，灰=白线，黄=黄线；红虚线=入场候选。仅取最早出现的8个不同代码，不按未来盈亏筛选；图截至信号当日，不展示未来走势。</p>{charts or '<p>本区间无入场候选，保留空结果，不为增加候选而调参。</p>'}<h2>候选明细</h2><p>展示前200条；全部候选与预警已另存CSV，保留证券代码便于核对。名称与行业为当前展示分类，不代表信号日历史分类，不参与选股计算。这里只是信号，尚无成交和交易盈亏。</p><div class="scroll"><table><thead><tr><th>信号日</th><th>公司名称</th><th>行业名称</th><th>收盘</th><th>计划盈亏比</th><th>白线乖离</th><th>J</th></tr></thead><tbody>{table}</tbody></table></div><h2>数据周期怎么选</h2><p><b>基础核验：2026-07-01至2026-10-16。</b>使用至少200个交易日的前缀预热，先核对日期、买点和反例，不以短窗口评选最佳收益。</p><p><b>现有本地全量：2024-01-02至2026-10-16。</b>2023年作为预热与早期规则检查；分年度、强弱市场检查，不把已查看的历史称为真正样本外。下一阶段补到2021年，数据从2020年起供预热，目标覆盖2021-01-04至2026-10-16。</p><h2>尚未通过的项目</h2><p>共用资金及完整账本验收、分批退出、开盘盈亏比复算、结构止损订单、最低佣金和完整历史税费、公司行动、历史ST与退市状态、浏览器真实视觉验收。当前股池不因缺当前名称而丢历史证券，但尚不能宣称无幸存者偏差。旧规则收益对照尚未运行。</p><small>策略参数在回放前冻结；所有结果保留。原始数据只读，结果位于隔离的个人研究目录。</small></main></html>'''
    (output/'report.html').write_text(report,encoding='utf-8')
    return {'output':str(output),'report':str(output/'report.html'),**config}
