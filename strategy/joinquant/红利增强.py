# 克隆自聚宽文章：https://www.joinquant.com/post/71143
# 标题：红利策略一直有效
# 作者：YYrich

# 克隆自聚宽文章：https://www.joinquant.com/post/61657
# 标题：红利可以抄底吗
# 作者：wywy1995
# 优化：屌丝逆袭量化
# 优化时间：2026-4-28
# 优化内容：
# 1、修复股息率公式：bonus_amount_rmb/market_cap → bonus_amount_rmb/price
# 2、激活红利低波子策略：target_num[3,4]，等权分配替代 available_cash
# 3、新增固定止损模块，多时间点检查
# 4、新增盈利保护模块，从近期高点回撤超阈值时卖出
# 5、新增防御模式，熊市降仓 + 无候选时切换货币ETF
# 6、新增 T+1 保护：safe_sell 统一检查 closeable_amount
# 7、代码结构优化：辅助函数置顶，结构化日志前缀

import datetime

import pandas as pd
import talib
import talib as tb
from jqdata import *
from jqfactor import get_factor_values


# ==================== 辅助函数 ====================

def filter_paused_stock(stock_list):
    """过滤停牌股票"""
    current_data = get_current_data()
    return [stock for stock in stock_list if not current_data[stock].paused]

def filter_st_stock(stock_list):
    """过滤ST股票"""
    current_data = get_current_data()
    return [stock for stock in stock_list
            if not current_data[stock].is_st
            and 'ST' not in current_data[stock].name
            and '*' not in current_data[stock].name
            and '退' not in current_data[stock].name]

def filter_kcb_stock(stock_list):
    """过滤科创板(688/689)、北交所(8xx)、科创板申赎代码(4xx)"""
    return [stock for stock in stock_list
            if stock[0] not in ('4', '8') and stock[:2] != '68']

def filter_new_stock(context, stock_list):
    """过滤上市不足250天的新股"""
    yesterday = context.previous_date
    return [stock for stock in stock_list
            if yesterday - get_security_info(stock).start_date >= datetime.timedelta(days=250)]

def filter_negative_momentum(context, stock_list, days=60):
    """过滤过去N日涨幅为负的股票（避免价值陷阱），支持批量预取"""
    if not stock_list:
        return []
    try:
        df = get_price(stock_list, end_date=context.previous_date,
                       frequency='daily', fields=['close'], count=days,
                       panel=False, skip_paused=False)
        if df is None or df.empty:
            return stock_list  # 获取失败不过滤，安全默认
        # 计算每只股票的动量 = 最新收盘 / 最早收盘 - 1
        momentum = df.groupby('code')['close'].agg(
            first='first', last='last')
        momentum['return'] = momentum['last'] / momentum['first'] - 1
        passed = momentum[momentum['return'] > 0].index.tolist()
        removed = len(stock_list) - len(passed)
        if removed > 0:
            log.info(f"[动量过滤] 排除 {removed} 只过去{days}日涨幅为负的股票")
        return passed
    except:
        return stock_list  # 异常不过滤


def get_dividend_ratio_filter_list(context, stock_list, sort, p1, p2, threshold):
    """按股息率筛选（可选连续三年分红）"""
    time1 = context.previous_date
    lookback_days = 365 * 3 if g.enable_3year_dividend_filter else 365
    time0 = time1 - datetime.timedelta(days=lookback_days)
    interval = 1000
    list_len = len(stock_list)

    # 批量查询分红数据
    q = query(finance.STK_XR_XD.code, finance.STK_XR_XD.a_registration_date, finance.STK_XR_XD.bonus_amount_rmb
    ).filter(
        finance.STK_XR_XD.a_registration_date >= time0,
        finance.STK_XR_XD.a_registration_date <= time1,
        finance.STK_XR_XD.code.in_(stock_list[:min(list_len, interval)]))
    df = finance.run_query(q)
    if list_len > interval:
        df_num = list_len // interval
        for i in range(df_num):
            q = query(finance.STK_XR_XD.code, finance.STK_XR_XD.a_registration_date, finance.STK_XR_XD.bonus_amount_rmb
            ).filter(
                finance.STK_XR_XD.a_registration_date >= time0,
                finance.STK_XR_XD.a_registration_date <= time1,
                finance.STK_XR_XD.code.in_(stock_list[interval*(i+1):min(list_len,interval*(i+2))]))
            temp_df = finance.run_query(q)
            df = df.append(temp_df)

    if df.empty:
        return []

    df = df.fillna(0)
    df['year'] = pd.to_datetime(df['a_registration_date']).dt.year

    if g.enable_3year_dividend_filter:
        # 过滤：必须最近连续3年每年都有分红
        all_years = sorted(df['year'].unique())
        if len(all_years) < 3:
            return []
        latest_year = all_years[-1]
        required_years = {latest_year - 2, latest_year - 1, latest_year}

        # 每只股票检查是否覆盖 required_years
        stock_years = df.groupby('code')['year'].apply(set)
        qualified = [code for code, yrs in stock_years.items()
                     if required_years.issubset(yrs)]

        removed_3y = len(df['code'].unique()) - len(qualified)
        if removed_3y > 0:
            log.info(f"[三年分红] 排除 {removed_3y} 只不满足连续3年({min(required_years)}-{max(required_years)})的股票")
        if not qualified:
            return []

        # 仅用最近一年的分红计算股息率
        recent = df[(df['code'].isin(qualified)) & (df['year'] == latest_year)]
        dividend = recent.groupby('code')['bonus_amount_rmb'].sum()
    else:
        # 不过滤三年：查询期内全部分红累加（与原始一致）
        dividend = df.groupby('code')['bonus_amount_rmb'].sum()

    temp_list = list(dividend.index)
    q = query(valuation.code, valuation.market_cap).filter(valuation.code.in_(temp_list))
    cap = get_fundamentals(q, date=time1)
    cap = cap.set_index('code')
    result = pd.concat([dividend, cap], axis=1, sort=False)
    # 股息率 = 派息总额(万元)/10000(→亿元) / 总市值(亿元)
    result['dividend_ratio'] = (result['bonus_amount_rmb']/10000) / result['market_cap']
    result = result.sort_values(by=['dividend_ratio'], ascending=sort)
    result = result[int(p1*len(result)):int(p2*len(result))]
    result = result[result['dividend_ratio'] > threshold]
    return list(result.index)

def get_factor_filter_list(context, stock_list, jqfactor, sort, p1, p2):
    """因子排序筛选，因子获取失败时不过滤"""
    if not stock_list:
        return []
    try:
        yesterday = context.previous_date
        factor_data = get_factor_values(stock_list, jqfactor, end_date=yesterday, count=1)
        if jqfactor not in factor_data or factor_data[jqfactor].empty:
            log.warning(f"因子 {jqfactor} 获取失败，跳过该过滤")
            return stock_list
        score_list = factor_data[jqfactor].iloc[0].tolist()
        df = pd.DataFrame(columns=['code','score'])
        df['code'] = stock_list
        df['score'] = score_list
        df = df.dropna()
        if df.empty:
            return stock_list
        df.sort_values(by='score', ascending=sort, inplace=True)
        final_list = list(df.code)[int(p1*len(df)):int(p2*len(df))]
        return final_list
    except Exception as e:
        log.warning(f"因子 {jqfactor} 筛选异常: {e}，跳过该过滤")
        return stock_list

def get_stock_name(security):
    """获取证券名称，带异常处理"""
    try:
        return get_current_data()[security].name
    except:
        return "未知"


# ==================== 辅助卖出函数 ====================

def safe_sell(context, security, target_value=0):
    """安全卖出：检查 closeable_amount（T+1 保护），卖出前检查涨停"""
    current_data = get_current_data()
    if current_data[security].last_price >= current_data[security].high_limit:
        log.debug(f"🔒 {security} {get_stock_name(security)} 涨停，跳过卖出")
        return False
    pos = context.portfolio.positions.get(security)
    if pos and pos.total_amount > 0:
        if pos.closeable_amount <= 0:
            log.info(f"⏳ {security} {get_stock_name(security)} 当日买入不可卖出(T+1)")
            return False
    return order_target_value(security, target_value)


# ==================== 风控模块 ====================

def stop_loss_check(context):
    """[止损] 固定止损检查：当前价低于成本价 * 阈值时卖出"""
    if not g.enable_stop_loss:
        return
    current_data = get_current_data()
    for sec in list(context.portfolio.positions.keys()):
        pos = context.portfolio.positions[sec]
        if pos.total_amount <= 0 or pos.closeable_amount <= 0:
            continue
        cost = pos.avg_cost
        if cost <= 0:
            continue
        price = current_data[sec].last_price
        if price <= 0:
            continue
        if price <= cost * g.stop_loss_threshold:
            loss_pct = (price / cost - 1) * 100
            log.info(f"🚨 [止损] {sec} {get_stock_name(sec)} 亏损{loss_pct:.2f}%，卖出")
            safe_sell(context, sec)


def check_market_bearish(context):
    """[防御] 两级熊市检测。enable_market_defense=False 时始终返回0"""
    if not g.enable_market_defense:
        return 0
    benchmark = '000300.XSHG'
    try:
        hist = attribute_history(benchmark, 31, '1d', ['close'])
        if hist.empty or len(hist) < 31:
            return 0

        # 30日动量：用于严重熊市判断
        momentum_30 = hist['close'].iloc[-1] / hist['close'].iloc[-30]
        if momentum_30 < g.severe_bearish_threshold:
            return 2  # 严重熊市

        # 20日动量：用于轻度熊市判断
        momentum_20 = hist['close'].iloc[-1] / hist['close'].iloc[-20]
        if momentum_20 < g.market_bearish_threshold:
            return 1  # 轻度熊市

        return 0  # 正常
    except:
        return 0


#macd
def get_macd_M(stock_list,check_date):
    macd_list = {}
    if isinstance(check_date,str):
        check_date = datetime.datetime.strptime(check_date, "%Y-%m-%d %H:%M:%S")
    if isinstance(stock_list,str):
        stock_list = [stock_list]
    for stock in stock_list:
        array = get_bars(security=stock, 
                         count=500, 
                         unit='1M',
                         fields=['close'],
                         include_now=False,
                         end_dt=check_date, 
                         fq_ref_date=check_date)
        close_list = array['close']
        dif, dea, macd = tb.MACD(close_list, 
                                 fastperiod=12, 
                                 slowperiod=26, 
                                 signalperiod=9)
        last_dif = dif[-1]
        last_dea = dea[-1]
        last_macd = macd[-1]
        macd_dic = (last_dif, last_dea, last_macd*2)
        macd_list[stock] = macd_dic
    return macd_list
	
def check_macd_trend(context):
    """
    月线MACD红绿柱判断市场风格：
    True  = 红柱（牛市/成长风格）→ 多配HLJZ
    False = 绿柱（熊市/防御风格）→ 多配HLDB
    """
    yesterday = context.previous_date
    re_value = get_macd_M(['000300.XSHG'],yesterday)
    dif, dea, macd = re_value['000300.XSHG']
    today_sig =macd
    stock_list=context.portfolio.positions.keys()
    stock_list=list(stock_list)
    if today_sig >0:
        return True
    if today_sig <= 0:
        return False


def check_defensive_etf_available(context):
    """[防御] 检查防御ETF是否可交易"""
    data = get_current_data()
    etf = g.defensive_etf
    if data[etf].paused:
        return False
    if data[etf].last_price >= data[etf].high_limit:
        return False
    if data[etf].last_price <= data[etf].low_limit:
        return False
    return True


def monitor_drawdown(context):
    """[回撤监控] 组合层面回撤监控（仅日志告警）"""
    value = context.portfolio.total_value
    if value > g.max_portfolio_value:
        g.max_portfolio_value = value
    if g.max_portfolio_value > 0:
        drawdown = (g.max_portfolio_value - value) / g.max_portfolio_value
        if drawdown >= g.drawdown_warning_threshold:
            log.warning(f"⚠️ [回撤] 组合回撤 {drawdown:.2%}（当前{value:.0f} / 最高{g.max_portfolio_value:.0f}）")


# ==================== 初始化模块 ====================

def initialize(context):
    """初始化函数：系统设置、全局变量、调度任务"""
    # ---------- 系统设置 ----------
    set_option("avoid_future_data", True)
    set_option('use_real_price', True)
    set_order_cost(OrderCost(open_tax=0, close_tax=0.001,
                             open_commission=0.00015, close_commission=0.00015,
                             close_today_commission=0, min_commission=0), type='stock')

    set_benchmark('000015.XSHG')
    log.set_level('order', 'error')
    log.set_level('history', 'error')
    log.set_level('system', 'error')

    # ---------- 全局变量 ----------
    g.sell_list = []
    g.buy_df = []
    g.buy_value = 0
    #g.target_num = [1, 4]    # 红利低波1只 + 红利价值4只
    g.high_limit_list = []

    # ---------- 止损参数 ----------
    g.enable_stop_loss = False
    g.stop_loss_threshold = 0.93          # 固定止损阈值（亏损8%）
    g.stop_loss_check_times = ['09:45', '10:30', '13:30', '14:30']

    # ---------- 动量过滤参数 ----------
    g.enable_momentum_filter = False
    g.momentum_filter_days = 30         # 过去N日涨幅为负则不买

    # ---------- 三年分红过滤 ----------
    g.enable_3year_dividend_filter = False

    # ---------- 资金分配模式 ----------
    g.allocation_mode = 'equal_weight'  # 'equal_weight'=总资产等权 | 'cash_only'=仅用可用现金

    # ---------- 防御模式参数 ----------
    g.defensive_etf = "511880.XSHG"       # 银华日利（货币基金）
    g.enable_market_defense = False       # 防御检测总开关
    g.market_bearish_threshold = 0.95     # 沪深300 20日动量 < 0.95 → 轻度熊市（降仓）
    g.severe_bearish_threshold = 0.90     # 沪深300 30日动量 < 0.90 → 严重熊市（全仓防御）

    # ---------- 组合回撤监控 ----------
    g.max_portfolio_value = 0
    g.drawdown_warning_threshold = 0.05   # 组合回撤5%预警

    # ---------- 调度任务 ----------
    # 原有
    run_daily(prepare_stock_list, '09:01')
    run_monthly(get_stock_list, 1, '09:02')
    run_monthly(trade, 1, '09:30')
    run_daily(check_limit_up, '11:00')

    # 止损检查（多个时间点）
    for t in g.stop_loss_check_times:
        run_daily(stop_loss_check, time=t)
        log.info(f"已注册止损检查时间：{t}")

    # 组合回撤监控
    #run_daily(monitor_drawdown, time='14:30')

    log.info("========== 红利策略初始化完成 ==========")


# ==================== 核心业务模块 ====================

def prepare_stock_list(context):
    """准备昨日涨停列表"""
    g.high_limit_list = []
    g.hold_list = list(context.portfolio.positions)
    if len(g.hold_list) != 0:
        df = get_price(g.hold_list, end_date=context.previous_date, frequency='daily',
                       fields=['close','high_limit'], count=1, panel=False,
                       fill_paused=False, skip_paused=False).dropna()
        df = df[df['close'] == df['high_limit']]
        g.high_limit_list = list(df.code)


def get_stock_list(context):
    """月度选股：红利低波 + 红利价值"""
    g.buy_df = pd.DataFrame(index=[], columns=['name'])
    yesterday = str(context.previous_date)
    today = context.current_dt

    # ---------- MACD 月线择时：红柱多配成长，绿柱多配防御 ----------
    macd_bull = check_macd_trend(context)
    if macd_bull:
        bearish_target = [1, 4]  # 红柱：1只红利低波 + 4只红利价值（进攻）
        log.info(f"🔴 月线MACD红柱 → 进攻模式: HLDB={bearish_target[0]} HLJZ={bearish_target[1]}")
    else:
        bearish_target = [4, 1]  # 绿柱：4只红利低波 + 1只红利价值（防御）
        log.info(f"🟢 月线MACD绿柱 → 防御模式: HLDB={bearish_target[0]} HLJZ={bearish_target[1]}")

    # ---------- 初始过滤 ----------
    initial_list = get_all_securities('stock', today).index.tolist()
    initial_list = filter_new_stock(context, initial_list)
    initial_list = filter_kcb_stock(initial_list)
    initial_list = filter_st_stock(initial_list)
    initial_list = filter_paused_stock(initial_list)

    # 过滤过去N日涨幅为负的股票（避免价值陷阱）
    if g.enable_momentum_filter:
        initial_list = filter_negative_momentum(context, initial_list, g.momentum_filter_days)

    # ---------- 红利低波 ----------
    stock_list = initial_list
    stock_list = get_dividend_ratio_filter_list(context, stock_list, False, 0.00, 0.10, 0.03)
    stock_list = get_factor_filter_list(context, stock_list, 'beta', True, 0.00, 0.50)
    HLDB_list = stock_list[:min(bearish_target[0], len(stock_list))]

    # ---------- 红利价值 ----------
    stock_list = initial_list
    df = get_fundamentals(query(
            valuation.code,
        ).filter(
            valuation.code.in_(stock_list),
            valuation.pe_ratio.between(8, 50),
            indicator.inc_return.between(5, 100),
            indicator.inc_total_revenue_year_on_year.between(5, 100),
            indicator.inc_net_profit_year_on_year.between(10, 100),
        ))
    stock_list = list(df.code)
    stock_list = get_dividend_ratio_filter_list(context, stock_list, False, 0.00, 0.10, 0.03)
    HLJZ_list = stock_list[:min(bearish_target[1], len(stock_list))]

    # ---------- 合并候选 ----------
    target_list = list(set(HLDB_list).union(set(HLJZ_list)))
    g.sell_list = [s for s in g.hold_list if s not in target_list and s not in g.high_limit_list]
    buy_list = [s for s in target_list if s not in g.hold_list]

    # 缓存完整排名列表（涨停卖出后补票用）
    g.hljz_pool = HLJZ_list
    g.hldb_pool = HLDB_list

    # ---------- 资金分配 ----------
    if g.allocation_mode == 'equal_weight':
        # 等权：组合总资产平分给每只候选股
        total_val = context.portfolio.total_value
        if len(buy_list) > 0:
            g.buy_value = total_val / len(target_list) if len(target_list) > 0 else 0
            g.buy_df['name'] = [get_security_info(s, yesterday).display_name for s in buy_list]
            g.buy_df.index = buy_list
        else:
            g.buy_value = 0
    else:
        # cash_only：仅用可用现金+卖出回款，不动已有持仓
        value = context.portfolio.available_cash
        if len(g.sell_list) > 0:
            for s in g.sell_list:
                value += context.portfolio.positions[s].value
        if len(buy_list) > 0:
            g.buy_value = value / len(buy_list)
            g.buy_df['name'] = [get_security_info(s, yesterday).display_name for s in buy_list]
            g.buy_df.index = buy_list
        else:
            g.buy_value = 0

    # ---------- 防御模式：无候选时买入防御ETF ----------
    if len(buy_list) == 0 and len(g.sell_list) > 0:
        def_total = context.portfolio.total_value if g.allocation_mode == 'equal_weight' else (
            context.portfolio.available_cash + sum(
                context.portfolio.positions[s].value for s in g.sell_list))
        if check_defensive_etf_available(context):
            log.info(f"🛡️ [防御] 无红利候选股，切换至防御ETF {g.defensive_etf} {get_stock_name(g.defensive_etf)}")
            g.buy_df = pd.DataFrame(
                {'name': [get_stock_name(g.defensive_etf)]},
                index=[g.defensive_etf])
            g.buy_value = def_total
        else:
            log.info(f"⚠️ [防御] 防御ETF {g.defensive_etf} 不可交易，保持空仓")
            g.buy_value = 0

    # ---------- 盘前打印 ----------
    print('卖出', g.sell_list)
    print('———————————————————————————————————')
    print(f"红利低波({len(HLDB_list)}只)", g.buy_df)
    print('———————————————————————————————————')

def trade(context):
    """执行交易：卖出非目标股，买入目标股"""
    current_data = get_current_data()

    # 卖出
    for s in g.sell_list:
        safe_sell(context, s)

    # 买入 — 用 order_target_value，涨停/停牌则跳过
    buy_list = list(g.buy_df.index) if len(g.buy_df) > 0 else []
    if len(buy_list) == 0 or g.buy_value <= 0:
        return

    for s in buy_list:
        name = g.buy_df.loc[s, 'name'] if s in g.buy_df.index else get_stock_name(s)
        if current_data[s].last_price >= current_data[s].high_limit:
            log.info(f"🔒 {s} {name} 涨停，跳过买入")
            continue
        if current_data[s].paused:
            log.info(f"⏸️ {s} {name} 停牌，跳过买入")
            continue
        print(f'买入 {s} {name} 目标金额 {g.buy_value:.0f}')
        order_target_value(s, g.buy_value)


def check_limit_up(context):
    """昨日涨停股处理：重新选股，在目标池的保留，不在的卖出并调仓"""
    current_data = get_current_data()
    if not g.high_limit_list:
        return

    # 先检查是否有涨停打开，没有则跳过（避免每天跑选股）
    opened = [s for s in g.high_limit_list
              if current_data[s].last_price < current_data[s].high_limit]
    if not opened:
        for s in g.high_limit_list:
            print(f'{s} {get_stock_name(s)} 继续涨停，持有')
        return

    # 有涨停打开 → 重新选股
    get_stock_list(context)
    buy_codes = set(g.buy_df.index) if len(g.buy_df) > 0 else set()

    sold_total = 0
    for s in g.high_limit_list:
        if current_data[s].last_price < current_data[s].high_limit:
            if s in buy_codes:
                print(f'{s} {get_stock_name(s)} 涨停打开，仍在目标池，持有')
            else:
                val = context.portfolio.positions[s].total_amount * current_data[s].last_price
                if safe_sell(context, s):
                    sold_total += val
                    print(f'{s} {get_stock_name(s)} 涨停打开，不在目标池，卖出 {val:.0f}')
        else:
            print(f'{s} {get_stock_name(s)} 继续涨停，持有')
        print('———————————————————————————————————')

    # 卖出资金买入防御ETF
    if sold_total > 0 and check_defensive_etf_available(context):
        existing = 0
        if g.defensive_etf in context.portfolio.positions:
            existing = context.portfolio.positions[g.defensive_etf].total_amount * current_data[g.defensive_etf].last_price
        order_target_value(g.defensive_etf, existing + sold_total)
        print(f'🛡️ 买入防御ETF {g.defensive_etf} 金额 {sold_total:.0f}')
