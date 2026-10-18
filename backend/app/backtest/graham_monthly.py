"""Panel研究组合资金路径：Graham月调保留持仓、聚宽五日清仓重买。

复用Panel结果/统计接口及仓库已有DistributionLedger，原始价记账。
买入成交价与权重只依赖开盘及此前信息，不用日内高低/收盘审批开盘订单。
"""
from __future__ import annotations

from datetime import date

import numpy as np

from app.backtest.engine import BacktestEngine, TradeRecord
from app.backtest.graham_distributions import DistributionLedger
from app.services.personal_graham import snapshot


def iso(day):
    return f'{day[:4]}-{day[4:6]}-{day[6:8]}'


def summarize_trades(records):
    """重叠的减仓批次按净盈亏/分摊成本汇总，期末未平仓不计胜率。"""
    result = []
    for code in sorted({r['symbol'] for r in records}):
        rows = [r for r in records if r['symbol'] == code]
        closed = [r for r in rows if r['exit_reason'] != 'open_position']
        cost = sum(r['entry_value'] for r in rows)
        result.append({'symbol': code, 'n_trades': len(closed),
            'total_return': sum(r['pnl_amount'] for r in rows)/cost if cost else 0.,
            'win_rate': sum(r['pnl_pct'] > 0 for r in closed)/len(closed) if closed else 0.,
            'best': max(r['pnl_pct'] for r in rows), 'worst': min(r['pnl_pct'] for r in rows)})
    return result


def simulate(engine, raw, targets, config, progress_cb=None, cancel_event=None, *,
             roundtrip=False, order_log=None, positions_log=None,
             dividend_core=False, attribution=snapshot):
    if roundtrip and dividend_core:
        raise ValueError('不能同时启用两种月调执行规则')
    start, end = str(config.start).replace('-', ''), str(config.end).replace('-', '')
    days = [d for d in raw['dates'] if start <= d <= end]
    if not days or end > raw['daily'].trade_date.max():
        raise ValueError('请求区间超出本地原始日线覆盖，请先同步数据或缩短区间')
    available_days = set(raw['daily'].trade_date)
    if any(d not in available_days for d in days):
        raise ValueError('请求区间有整日市场行情缺失，请先同步完整数据')
    next_day = dict(zip(raw['dates'][:-1], raw['dates'][1:], strict=True))
    plans = {next_day[d]: (d, f) for d, f in targets.items() if d in next_day}
    if roundtrip:
        # 只读取各执行日开盘报价选择未停牌代理前三，避免为未入选股票装载全程OHLC。
        opening = raw['daily'].loc[raw['daily'].trade_date.isin(plans), ['trade_date', 'ts_code', 'open']]
        open_codes = {d:set(f.loc[np.isfinite(f.open) & f.open.gt(0), 'ts_code'])
                      for d, f in opening.groupby('trade_date', sort=False)}
        plans = {d:(signal, f.loc[f.index.isin(open_codes.get(d, set()))].head(3))
                 for d, (signal, f) in plans.items()}
    codes = set().union(*(set(f.index) for _, f in plans.values())) if plans else set()
    capacity = raw['daily'].loc[raw['daily'].ts_code.isin(codes), ['ts_code','trade_date','amount20']].set_index(['ts_code','trade_date']).amount20
    daily = raw['daily'][raw['daily'].ts_code.isin(codes) & raw['daily'].trade_date.between(start, end)].copy()
    adj = raw['adj'][raw['adj'].ts_code.isin(codes)]
    daily = daily.merge(adj[['ts_code', 'trade_date', 'adj_factor']], on=['ts_code', 'trade_date'], how='left', validate='one_to_one')
    limits = raw['limits'][raw['limits'].ts_code.isin(codes)]
    daily = daily.merge(limits[['ts_code', 'trade_date', 'up_limit', 'down_limit']], on=['ts_code', 'trade_date'], how='left', validate='one_to_one')
    bars = {d: f.set_index('ts_code').to_dict('index') for d, f in daily.groupby('trade_date', sort=False)}
    positions, pending, journals, fills, trades, curves, drawdowns = {}, {}, [], [], [], [], []
    ledger = DistributionLedger(raw['dividends'], journals, factor_decimals=raw.get('factor_decimals'),
                                fractional_policy=raw.get('fractional_policy', 'reject'))
    cash = float(config.initial_capital)
    peak = cash
    stocks = raw['stocks'].drop_duplicates('ts_code').set_index('ts_code')
    execution = {'buy_limit_up': 0, 'sell_limit_down': 0, 'buy_suspended': 0,
                 'sell_suspended': 0, 'buy_cash': 0, 'buy_lot_size': 0, 'pending_exit': 0}
    commission = config.commission_pct if config.commission_pct is not None else config.fees_pct
    slippage = float(config.slippage_bps) / 10000
    if not 0 <= slippage < .1 or not 0 <= commission < .1:
        raise ValueError('费用或滑点超出研究范围')
    fees_total = 0.
    order_log = order_log if order_log is not None else []
    positions_log = positions_log if positions_log is not None else []

    def reject(code, side, day, reason):
        order_log.append({'date': iso(day), 'symbol': code, 'side': side, 'status': 'rejected', 'reason': reason})

    def fee(value, side, day):
        # 缺账户单据时采用显式研究情景：最低佣金5元、过户费按历史分界。
        stamp = (config.stamp_tax_pct if config.stamp_tax_pct is not None
                 else (.0005 if day >= '20230828' else .001)) if side == 'sell' else 0.
        transfer = .00001 if day >= '20220429' else .00002
        return max(5., value * commission) + value * (stamp + transfer + slippage)

    def lot(code, quantity, selling=False, held=0):
        # 科创板最低200股、递增1股；其余沪深A股按100股。
        star = code.startswith(('688', '689')) and code.endswith('.SH')
        minimum, step = (200, 1) if star else (100, 100)
        quantity = max(0, int(np.floor(quantity)))
        if selling and quantity >= held:
            return int(held)  # 全部卖出允许一次性清掉不足最低申报量的余额。
        quantity = quantity // step * step
        return quantity if quantity >= minimum else 0

    def sell_capacity(code, quantity, signal_day, today):
        amount = float(capacity.get((code, signal_day), 0.))
        quote = today.get(code)
        cap = amount * .01 / quote['open'] if quote and quote['open'] > 0 else 0
        cap = max(0, cap - sold_today.get(code, 0))
        return lot(code, min(quantity, cap), selling=True, held=positions[code]['qty'])

    def market_value(today=None, at_open=False):
        total = 0.
        for code, p in positions.items():
            quote = (today or {}).get(code)
            mark = quote.get('open') if at_open and quote else p['mark']
            if not mark or not np.isfinite(mark) or mark <= 0:
                mark = p['mark']
            total += (p['qty'] + p.get('pending_qty', 0)) * mark
        return total

    def allowed(code, side, today):
        row = today.get(code)
        if row is None or not np.isfinite(row['open']) or row['open'] <= 0:
            execution[side + '_suspended'] += 1
            reject(code, side, current_day, '开盘报价缺失或停牌代理')
            return False
        bound = row['up_limit' if side == 'buy' else 'down_limit']
        if not np.isfinite(bound):
            raise ValueError(f'缺少开盘涨跌停基准：{code} {row["trade_date"]}')
        # 保守：开盘在限制价不假设能排到队；不读取随后是否开板。
        blocked = row['open'] >= bound - .005 if side == 'buy' else row['open'] <= bound + .005
        if blocked:
            execution['buy_limit_up' if side == 'buy' else 'sell_limit_down'] += 1
            reject(code, side, current_day, '开盘涨停' if side == 'buy' else '开盘跌停')
        return not blocked

    def close_part(code, quantity, day, signal_day, today, reason):
        nonlocal cash, fees_total
        p = positions[code]
        if quantity <= 0 or p['entry_day'] >= day or not allowed(code, 'sell', today):
            return False
        price = today[code]['open']
        ratio = quantity / (p['qty'] + p.get('pending_qty', 0))
        cost = p['cost'] * ratio
        dividend = p['proceeds'] * ratio
        charge = fee(quantity * price, 'sell', day)
        proceeds = quantity * price - charge
        cash += proceeds
        fees_total += charge
        p['cost'] -= cost
        p['proceeds'] -= dividend
        p['qty'] -= quantity
        sold_today[code] = sold_today.get(code, 0) + quantity
        pnl = proceeds + dividend - cost
        trade = TradeRecord(symbol=code, name=p['name'], entry_date=iso(p['entry_day']),
                            exit_date=iso(day), entry_price=p['entry_price'], exit_price=price,
                            pnl_pct=pnl/cost if cost else 0., duration=raw['dates'].index(day)-raw['dates'].index(p['entry_day']),
                            exit_reason=reason, shares=quantity, lots=quantity/100,
                            entry_value=cost, exit_value=proceeds+dividend, pnl_amount=pnl,
                            position_pct=cost/config.initial_capital, entry_score=p['entry_attr']['score'],
                            entry_signal_date=iso(p['entry_attr']['date']), exit_signal_date=iso(signal_day),
                            entry_signal_id=('dividend_selected' if dividend_core else 'jq_smallcap_selected' if roundtrip else f'graham_{config.strategy_id.split("_")[-1]}_selected'),
                            exit_signal_id=('dividend_monthly_removed' if dividend_core else 'jq_five_session_exit' if roundtrip else 'graham_monthly_removed') if reason == 'signal' else None,
                            blocked_exit_days=p.get('blocked', 0))
        trades.append({'record': trade, 'entry_attribution': p['entry_attr'], 'entry_fills': list(p['fills']),
                       'exit_attribution': {'date': iso(signal_day), 'reason': reason},
                       'pnl_decomposition': {'price_and_costs': proceeds-cost, 'dividend_net': dividend}})
        fills.append({'date': iso(day), 'signal_date': iso(signal_day), 'symbol': code,
                      'side': 'sell', 'price': price, 'quantity': quantity, 'fee': charge, 'reason': reason})
        journals.append({'trade_date': day, 'event': 'sell', 'cash_delta': proceeds, 'ts_code': code})
        if p['qty'] == 0 and p.get('pending_qty', 0) == 0:
            del positions[code]
        return True

    for i, day in enumerate(days):
        if cancel_event is not None and cancel_event.is_set():
            raise ValueError('cancelled')
        current_day = day
        today = bars.get(day, {})
        sold_today = {}
        for code in positions:
            delisted = stocks.at[code, 'delist_date'] if code in stocks.index else None
            if delisted is not None and str(delisted) not in ('<NA>', 'nan', 'None', 'NaT') and str(delisted) <= day:
                raise ValueError(f'持仓已退市但缺清算/转板回收账本：{code} {day}，禁止以停牌前价格冻结净值')
        ledger.open(day, positions, today)
        # 先按已知除权日调整成本展示，再使用当日开盘；股数变化仍由权益登记账本处理。
        for p in positions.values():
            total_qty = p['qty'] + p.get('pending_qty', 0)
            if total_qty > 0:
                p['entry_price'] = p['cost'] / total_qty
        for code in list(pending):
            if code not in positions:
                del pending[code]
                continue
            signal_day = pending[code]
            p = positions[code]
            quantity = sell_capacity(code, p['qty'], signal_day, today)
            if quantity and close_part(code, quantity, day, signal_day, today, 'signal'):
                if code not in positions or positions[code]['qty'] == 0:
                    del pending[code]
            elif code in positions:
                positions[code]['blocked'] = positions[code].get('blocked', 0) + 1

        if day in plans:
            signal_day, target = plans[day]
            if roundtrip:
                # 先筛开盘停牌代理、取前三，再清仓；涨停不视为停牌，不向第四名递补。
                active = [code for code in target.index if code in today
                          and np.isfinite(today[code]['open']) and today[code]['open'] > 0]
                target = target.loc[active].head(3)
                for code in list(positions):
                    quantity = sell_capacity(code, positions[code]['qty'], signal_day, today)
                    if quantity:
                        close_part(code, quantity, day, signal_day, today, 'signal')
                    else:
                        reject(code, 'sell', day, '容量或最小申报数量不足，留到下一周期')
                # 原代码按卖出后的空名额分配可用现金，循环中预算固定。
                vacancy = max(0, 3-len(positions))
                budget = cash / vacancy if vacancy else 0.
                desired = {}
                for code in target.index:
                    held = positions.get(code, {}).get('qty', 0)
                    desired[code] = held + lot(code, (budget-5) / (today[code]['open']*(1+commission+slippage+.00002)))
            elif dividend_core:
                # 盘前净值只用既有估值；新票分预算，保留旧仓不重置权重。
                nav_before = cash + ledger.receivable + market_value()
                budget = nav_before * config.max_exposure_pct / len(target) if len(target) else 0.
                desired = {}
                for code in target.index:
                    old = positions.get(code)
                    if old:
                        desired[code] = old['qty'] + old.get('pending_qty', 0)
                        continue
                    quote = today.get(code)
                    if quote and np.isfinite(quote['open']) and quote['open'] > 0:
                        desired[code] = lot(code, (budget-5)/(quote['open']*(1+commission+slippage+.00002)))
                for code in list(positions):
                    if code not in target.index:
                        close_part(code, positions[code]['qty'], day, signal_day, today, 'signal')
                # 原月调未成交卖单不跨日追卖；也不递补目标之外的股票。
            else:
                # 权重基准在调仓开盘确定，只用当时报价和前收，不能用今日收盘净值。
                nav_open = cash + ledger.receivable + market_value(today, at_open=True)
                desired = {}
                for code, _row in target.iterrows():
                    q = today.get(code)
                    if q is None or not np.isfinite(q['open']) or q['open'] <= 0:
                        desired[code] = positions.get(code, {}).get('qty', 0)
                        execution['buy_suspended'] += 1
                        continue
                    budget = nav_open * config.max_exposure_pct / 20
                    # 保留费用预算；真实金额稍后按精确费用再次核验。
                    desired[code] = lot(code, (budget - 5) / (q['open'] * (1+commission+slippage+.00002)))
                for code in list(positions):
                    if code not in desired:
                        pending.setdefault(code, signal_day)
                    elif code in pending:
                        # 已发出的退出单不因后来重入选静默取消。
                        desired[code] = 0
                    p = positions[code]
                    quantity = max(0, p['qty'] - desired.get(code, 0))
                    if quantity:
                        # 部分减仓受1%历史成交额容量约束，剩余持仓保留。
                        quantity = sell_capacity(code, quantity, signal_day, today)
                        reason = 'signal' if code not in desired else 'rebalance'
                        if quantity and close_part(code, quantity, day, signal_day, today, reason) and code not in positions:
                            pending.pop(code, None)
            # 只买当月原始Top20；买单不跨日追买。
            for code, row in target.iterrows():
                if dividend_core and code in positions:
                    continue
                if roundtrip and len(positions) >= 3:
                    reject(code, 'buy', day, '卖出后没有空名额')
                    continue
                if code in pending or not allowed(code, 'buy', today):
                    continue
                old = positions.get(code)
                held = old['qty'] + old.get('pending_qty', 0) if old else 0
                quantity = max(0, desired.get(code, held) - held)
                price = today[code]['open']
                if not np.isfinite(row['amount20']) or row['amount20'] <= 0:
                    reject(code, 'buy', day, '信号日历史成交容量缺失')
                    continue
                quantity = lot(code, quantity if dividend_core else min(quantity, row['amount20']*.01/price))
                if quantity <= 0:
                    reject(code, 'buy', day, '目标资金、容量或最小申报数量不足')
                    continue
                charge = fee(quantity*price, 'buy', day)
                while quantity > 0 and quantity*price+charge > cash + 1e-7:
                    quantity = lot(code, quantity - (1 if code.startswith(('688', '689')) else 100))
                    charge = fee(quantity*price, 'buy', day)
                if quantity <= 0:
                    reject(code, 'buy', day, '可用现金不足')
                    execution['buy_cash'] += 1
                    continue
                cost = quantity*price+charge
                cash -= cost
                fees_total += charge
                attr = attribution(row)
                fill = {'date': iso(day), 'signal_date': iso(signal_day), 'symbol': code, 'side': 'buy',
                        'price': price, 'quantity': quantity, 'fee': charge, 'attribution': attr}
                fills.append(fill)
                journals.append({'trade_date': day, 'event': 'buy', 'cash_delta': -cost, 'ts_code': code})
                if old:
                    old['cost'] += cost
                    old['qty'] += quantity
                    old['fills'].append(fill)
                    old['entry_price'] = old['cost']/(old['qty']+old.get('pending_qty', 0))
                else:
                    positions[code] = {'qty': quantity, 'cost': cost, 'proceeds': 0., 'pending_qty': 0,
                                       'name': row['name'], 'entry_day': day, 'entry_price': cost/quantity if roundtrip else price,
                                       'entry_attr': attr, 'fills': [fill], 'mark': price,
                                       'adj_factor': today[code]['adj_factor'], 'last_bar_date': day,
                                       'position_id': f'{code}:{day}'}
        for code, p in positions.items():
            if code in today:
                p['mark'] = today[code]['close']
        for code, p in positions.items():
            positions_log.append({'date': iso(day), 'symbol': code, 'quantity': p['qty'],
                'pending_quantity': p.get('pending_qty', 0), 'mark': p['mark'], 'cost': p['cost']})
        ledger.record_close(day, positions)
        cash += ledger.pay_close(day)
        equity = cash + ledger.receivable + market_value()
        if cash < -1e-6:
            raise ValueError('Graham现金账户透支')
        peak = max(peak, equity)
        curves.append({'date': iso(day), 'value': equity, 'cash': cash, 'receivable': ledger.receivable,
                       'positions': len(positions), 'exposure': market_value()/equity if equity else 0.})
        drawdowns.append({'date': iso(day), 'value': equity/peak-1})
        if progress_cb is not None and i % 20 == 0:
            progress_cb({'day': i+1, 'total': len(days), 'date': iso(day), 'equity': equity})
    stats = BacktestEngine._calc_portfolio_stats(curves, [t['record'] for t in trades], config.initial_capital,
                                                include_monte_carlo=False)
    span = (date.fromisoformat(iso(days[-1])) - config.start).days
    stats['annual_return'] = (curves[-1]['value']/config.initial_capital)**(365.25/max(span,1))-1
    stats['max_drawdown'] = min((r['value'] for r in drawdowns), default=0.)
    stats['execution'] = execution
    stats['fractional_policy'] = raw.get('fractional_policy', 'reject')
    stats['fractional_share_discards'] = sum(j.get('event') == 'fractional_share_discard' for j in journals)
    stats['factor_decimals'] = raw.get('factor_decimals')
    stats['factor_precision_matches'] = sum(j.get('event') == 'factor_precision_match' for j in journals)
    stats['fees_total'] = fees_total
    stats['pending_exit_positions'] = len(pending)
    stats['unrealized_positions'] = len(positions)
    stats['selection'] = {'strategy_matches': sum(len(f) for f in targets.values()),
                          'entry_candidates': sum(len(f) for f in targets.values())}
    # 期末只估值，不能凭空画出一笔卖出成交。冻结持仓与应收全部保留在净值。
    for code, p in positions.items():
        quantity = p['qty'] + p.get('pending_qty', 0)
        value = quantity*p['mark']+p['proceeds']
        cost = p['cost']
        record = TradeRecord(symbol=code, name=p['name'], entry_date=iso(p['entry_day']), exit_date=iso(days[-1]),
                             entry_price=p['entry_price'], exit_price=p['mark'], pnl_pct=value/cost-1 if cost else 0.,
                             duration=raw['dates'].index(days[-1])-raw['dates'].index(p['entry_day']),
                             exit_reason='open_position', shares=quantity, lots=quantity/100, entry_value=cost,
                             exit_value=value, pnl_amount=value-cost, entry_score=p['entry_attr']['score'],
                             entry_signal_date=iso(p['entry_attr']['date']))
        trades.append({'record': record, 'entry_attribution': p['entry_attr'], 'entry_fills': list(p['fills']),
                       'exit_attribution': {'reason': '期末估值，未卖出'},
                       'pnl_decomposition': {'price_and_costs': quantity*p['mark']-cost, 'dividend_net': p['proceeds']}})
    return stats, curves, drawdowns, trades, fills, journals
