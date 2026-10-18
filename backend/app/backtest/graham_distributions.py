"""Explicit cash/share entitlements for the isolated daily formula study.

20% dividend tax is reserved permanently: a conservative scenario, not a
broker's final holding-period tax bill. No adjusted prices enter cash flows.
Unsupported or unexplained actions fail loudly rather than freezing holdings.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import numpy as np
import pandas as pd


class DistributionLedger:
    def __init__(self, events, journal, *, factor_decimals=None, fractional_policy="reject"):
        self.journal = journal
        self.factor_decimals = factor_decimals
        self.fractional_policy = fractional_policy
        if fractional_policy not in {"reject", "floor"}:
            raise ValueError("fractional_policy must be reject or floor")
        self.claims = []
        self.pending_cash = []
        self.by_record = {}
        self.by_code = {}
        if events is None or events.empty:
            return
        e = events[events.div_proc.eq('实施')].copy()
        normalized=[]
        for x in e.to_dict('records'):
            for c in ('record_date', 'ex_date', 'pay_date', 'div_listdate'):
                x[c] = str(x[c]) if pd.notna(x.get(c)) else ''
            for c in ('cash_div_tax', 'stk_div', 'stk_bo_rate'):
                x[c] = float(x.get(c) or 0) if pd.notna(x.get(c)) else 0.
            if len(x['ex_date']) != 8:
                continue
            normalized.append({k:x[k] for k in ('ts_code','record_date','ex_date','pay_date','div_listdate','cash_div_tax','stk_div','stk_bo_rate')})
        if not normalized:return
        # Provider can repeat one implemented event under several report dates
        # or proposal announcements. Deduplicate ECONOMIC entitlements, not all
        # metadata, and never sum repeated implementation records.
        e=pd.DataFrame(normalized).drop_duplicates()
        e['ambiguous']=e.duplicated(['ts_code','ex_date'],keep=False)
        for x in e.to_dict('records'):
            self.by_record.setdefault(x['record_date'], []).append(x)
            self.by_code.setdefault(x['ts_code'], []).append(x)

    def record_close(self, date, positions):
        for e in self.by_record.get(date, []):
            p = positions.get(e['ts_code'])
            if p is None:
                continue
            if e['ambiguous']:
                raise ValueError(f'Ambiguous held distribution: {e["ts_code"]} {e["ex_date"]}')
            qty = p['qty'] + p.get('pending_qty', 0)
            if qty:
                self.claims.append(dict(event=e, position=p, record_qty=qty, applied=False, listed=False))

    def open(self, date, positions, today):
        for claim in self.claims:
            e, p = claim['event'], claim['position']
            if not claim['applied'] and e['ex_date'] <= date:
                # Accrual is on ex-date; spendable cash arrives after pay-date
                # trading. Ownership is frozen at record-date close.
                gross = claim['record_qty'] * e['cash_div_tax']
                tax = .2 * (gross + claim['record_qty'] * e['stk_bo_rate'])
                net = gross - tax
                if net and (len(e['pay_date']) != 8 or e['pay_date'] < e['ex_date']):
                    raise ValueError('Missing/invalid cash payment date')
                new_shares = claim['record_qty'] * e['stk_div']
                remainder = 0.
                if not np.isclose(new_shares, round(new_shares), rtol=0, atol=1e-7):
                    if self.fractional_policy == 'reject':
                        raise ValueError('Fractional stock distribution requires registrar allocation')
                    allocated = int(np.floor(new_shares))
                    remainder = new_shares - allocated
                    new_shares = allocated
                else:
                    new_shares = int(round(new_shares))
                if new_shares and (len(e['div_listdate']) != 8 or e['div_listdate'] < e['ex_date']):
                    raise ValueError('Missing/invalid stock listing date')
                if remainder:
                    self.journal.append(dict(trade_date=date, ts_code=e['ts_code'], position_id=p['position_id'],
                        event='fractional_share_discard', record_qty=claim['record_qty'], stock_rate=e['stk_div'],
                        allocated_qty=new_shares, discarded_qty=remainder,
                        cash_delta=0., receivable_delta=0., qty_delta=0, pending_delta=0))
                p['pending_qty'] = p.get('pending_qty', 0) + new_shares
                p['proceeds'] += net
                if net:
                    self.pending_cash.append(dict(amount=net, pay_date=e['pay_date'], position_id=p['position_id']))
                claim.update(applied=True, new_shares=new_shares)
                if e['ts_code'] not in today:
                    p['mark'] = (p['mark']-e['cash_div_tax'])/(1+e['stk_div'])
                self.journal.append(dict(trade_date=date, ts_code=e['ts_code'], position_id=p['position_id'],
                                         event='distribution_accrual', cash_delta=0., receivable_delta=net,
                                         qty_delta=0, pending_delta=new_shares, gross_dividend=gross, tax_reserve=tax))
            if claim['applied'] and not claim['listed'] and e['div_listdate'] <= date:
                qty = claim['new_shares']
                p['pending_qty'] -= qty
                p['qty'] += qty
                claim['listed'] = True
                if qty:
                    self.journal.append(dict(trade_date=date, ts_code=e['ts_code'], position_id=p['position_id'],
                                             event='stock_listing', cash_delta=0., receivable_delta=0., qty_delta=qty, pending_delta=-qty))
        for code, p in positions.items():
            row = today.get(code)
            if row is None:
                continue
            changed = not np.isclose(row['adj_factor'], p['adj_factor'], rtol=1e-10)
            if changed and self.factor_decimals is not None:
                # 混用3/4位小数的输入按声明精度比较；现金和股数始终用原始权益值。
                unit = Decimal(1).scaleb(-self.factor_decimals)
                previous = Decimal(str(p['adj_factor'])).quantize(unit, rounding=ROUND_HALF_UP)
                current = Decimal(str(row['adj_factor'])).quantize(unit, rounding=ROUND_HALF_UP)
                if previous == current:
                    self.journal.append(dict(trade_date=date, ts_code=code, position_id=p['position_id'],
                        event='factor_precision_match', previous_factor=float(p['adj_factor']),
                        factor=float(row['adj_factor']), factor_decimals=self.factor_decimals,
                        cash_delta=0., receivable_delta=0., qty_delta=0, pending_delta=0))
                    p['adj_factor'] = row['adj_factor']
                    changed = False
            if changed:
                explained = any(p['last_bar_date'] < e['ex_date'] <= date for e in self.by_code.get(code, []))
                if not explained:
                    raise ValueError(f'Unexplained adjustment: {code} {date}')
                p['adj_factor'] = row['adj_factor']
            p['last_bar_date'] = date

    def pay_close(self, date):
        paid = 0.
        remaining = []
        for c in self.pending_cash:
            if c['pay_date'] <= date:
                paid += c['amount']
                self.journal.append(dict(trade_date=date, position_id=c['position_id'], event='dividend_payment',
                                         cash_delta=c['amount'], receivable_delta=-c['amount'], qty_delta=0, pending_delta=0))
            else:
                remaining.append(c)
        self.pending_cash = remaining
        return paid

    @property
    def receivable(self):
        return sum(c['amount'] for c in self.pending_cash)
