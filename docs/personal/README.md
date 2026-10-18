# 个人量化工作台

本fork基于TSP `b63c17e`，用于盘后策略与因子研究，和生产隔离。**数据和知行信号已接通；完整交易回测尚未验收，当前收益不能作为实盘依据。**

只读这四份：

| 文档 | 用途 |
|---|---|
| 本页 | 启动、更新、当前状态与复现 |
| [系统与策略施工方案](07_轻量量化工作台_Agent施工方案.md) | 目标、迭代路径、知行策略规则与下一步 |
| [共同数据约定](00_共同接口与验收.md) | 时间、价格、候选与因子交接 |
| [回测施工与验收](02_交易回测施工说明.md) | 订单、现金、A股约束、逐笔复盘与验收样例 |

Agent先读根目录AGENTS、CONTRIBUTING及二开指南。历史选型、重复接入笔记和临时探查脚本已清理；上游使用与开发文档保留。

## 当前可用与缺口

截至2026-10-18，本机已导入：

| 数据 | 范围与限制 |
|---|---|
| 股票 | 4,901,682条，915交易日，2023-01-03至2026-10-16；窗口内5,725只，末日5,567只 |
| 指数 | 10只、9,120条；首页四指数可查 |
| ETF | 12只、40,045条，截止2026-10-16；固定研究样本，有选样与幸存者偏差 |
| 板块 | 31个申万一级行业、395个概念；展示当前快照，概念成员不能回填历史 |
| 财务 | 四表949,530条，报告期2014年末至2026中报；保留公告版本，页面及AI每期展示最新公告 |

价格保留原始价和前复权价；成交量为手、成交额为元。累计复权因子转换成单次事件比值后才交给TSP。历史股本采用当日快照，不用最新值回填。财报金额元、每股元/股、比率百分点；利润和现金流为年初累计口径，不当作单季度或TTM。缺字段保留为空。

财务日期错误已隔离并留在本地导入清单；历史原始版本完整性仍未知。股票一条daily_basic价格冲突经两项交叉核验后隔离该基本面行，未改原始行情。缺当前名称维表的历史证券补名称显示，不删除行情；历史ST、退市状态仍未完整还原。

知行v0提供信号与预警回放，基础区间2026-07-01至2026-10-16共53次入场候选，本地全量2024-01-02至2026-10-16共3,966次候选。它们不是已成交交易，不代表胜率或收益。报告在`data/personal_trial/research/zhixing/<run_id>/report.html`，单文件可离线查看，运行产物不进Git。

**下一项必须完成：** 修正同日收盘卖款支持开盘买入的资金时序，核对费用与公司行动，实现减半退出、结构止损及开盘盈亏比复算，再比较账户收益。旧信号回测的多标的指标平均也不等于共用资金账户。浏览器布局及窄屏交互尚未验收；多年市场环境和2021—2022年股票数据仍缺。

## 配置与启动

在fork根目录创建本机`.env`，不提交凭证：

```dotenv
HOST=127.0.0.1
PORT=8603
DATA_DIR=./data/personal_trial
PERSONAL_LOCAL_ONLY=true
SLQUANT_DATA_ROOT=<已有行情根目录>
```

在设置中将日K、复权因子和财务数据源选为`local_parquet`，关闭实时行情及矩阵自动预热。日常页面只读本地，不自动在线回退；本地模式不是操作系统网络沙箱。没有券商连接、自动下单、外部通知或自动AI任务。

```bash
# 重建锁定环境（首次）
cd backend
uv sync --frozen --extra dev --extra backtest --python 3.12
cd ../frontend
pnpm install --frozen-lockfile
pnpm build
cd ..

# 安装策略；重复执行不覆盖手工修改
backend/.venv/bin/python scripts/personal_trial.py install-strategy
# 使用已有派生库启动，Ctrl+C停止
backend/.venv/bin/python scripts/personal_trial.py serve
```

地址为 http://127.0.0.1:8603，仅运行服务的电脑可直接访问；其他设备的127.0.0.1指向其自身。端口占用会退出，不杀其他进程，不用会抢占端口的原dev.sh。

## 更新与回放

**先停试用服务，再导入，随后启动以刷新缓存。** 源目录只读，派生库及报告写入本fork的`DATA_DIR`。导入互斥，不能同时运行多个导入。

```bash
# 完整原始行情快照必须同时含daily.parquet与adj_factor.parquet
backend/.venv/bin/python scripts/personal_trial.py import-stock --stock-source <完整快照目录>
backend/.venv/bin/python scripts/personal_trial.py import-index
backend/.venv/bin/python scripts/personal_trial.py import-sectors
backend/.venv/bin/python scripts/personal_trial.py import-financial
backend/.venv/bin/python scripts/personal_trial.py import-etf --etf-source <含原始价和复权校验列的ETF研究文件>
backend/.venv/bin/python scripts/personal_trial.py import-regime

# 历史信号核验，不是账户回测；每次产生新目录
NUMBA_NUM_THREADS=4 backend/.venv/bin/python scripts/personal_trial.py review-strategy --start 2026-07-01 --end 2026-10-16
NUMBA_NUM_THREADS=4 backend/.venv/bin/python scripts/personal_trial.py review-strategy --start 2024-01-02 --end 2026-10-16
```

ETF研究文件须含原始OHLC、累计因子及qfq_close等字段；来源与已有缓存冲突则拒绝写入，不挑选有利版本。`--etf-source`可重复提供多个文件。

显式`sync-stock`/`sync-etf`才联网，需要`TUSHARE_TOKEN`或`TUSHARE_TOKEN_FILE`。本机目前没有配置有效补数凭证，不影响离线读取。**多年股票历史导入后不能用短窗口sync-stock重建；当前会拒绝覆盖股本和复权事件，增量同步仍待验收，不作为日常更新推荐命令。**

AI设置选择Codex CLI，当前工作台保存为`gpt-6.1-sol`、`xhigh`；每次调用传明确参数，失败不自动降级。复用本机已登录CLI，不复制auth.json，不修改Cindy全局设置。个人模式要求CLI支持`--ignore-user-config`。曾实测财务分析完整返回正文；文本调用通过不代表Agent工具协议、报告质量已验收。

## 活跃市值（0AMV）

市场环境页顶部和标的回放下方显示真实0AMV K线及知行白黄线。只读`SLQUANT_DATA_ROOT/raw/compass_0amv.parquet`，不复制到行情库，不把它当可交易指数；单位保留指南针原始刻度。本地已核对8,225条，1993-01-03至2026-10-16。

Windows数据先用已有导入流程校验，再原子替换上述规范文件，不能直接覆盖正在读取的文件。Panel每分钟重新读取，也可点“刷新本地数据”，无需重新导入或重启。仅收到收盘值时不能伪造OHLC；当前完整K线入口会拒绝无效记录。

回测的“0AMV开仓过滤”默认关闭，可选试探/确认或仅确认。按单日+4%启动、三日+6%确认、单日−2.3%防守计算完整历史状态；T日收盘门禁先作用于信号，再由引擎移至T+1开盘，缺当日数据禁止新仓。不强制退出、不实现试探期50%仓位上限；知行线仅展示，不参与这版门禁。旧环境分数过滤保留原T−1口径，两者同时选中取交集。

这是历史研究功能，不保证数据当时已同步到本机；源文件SHA256在图表API及研究验收记录中保留。完整交易回测原有未验收项仍有效，不能把门禁测试通过当成系统可实盘。

本轮验收：34项后端测试、5项前端测试通过；运行中的0AMV接口返回8,225条、最新2026-10-16为防守。2026-07-01至2026-10-16的固定10只工程样本，未择时10笔，试探/确认过滤及仅确认过滤均无买入信号。这是接线检查，样本来自已有候选，不能作为择时有效性证据；验收JSON在`data/personal_trial/research/amv/<run_id>/`。

## 提交前验证与后续维护

```bash
# 在backend目录运行；按改动范围补跑相邻模块
cd backend
.venv/bin/python -m pytest tests/test_personal_*.py tests/test_financial_analyzer_dates.py tests/test_vbt_cow_compat.py tests/test_ai_provider.py -q
cd ../frontend
pnpm build
cd ..
git diff --check
```

验收记录以本轮实际运行结果为准，不沿用旧笔记中的测试数量。数据核验JSON、信号CSV和HTML保存在`DATA_DIR`，不提交原始行情、凭证、日志、数据库或生成报告。

每轮只改一个假设，交付对照结果、逐笔反例、未通过项和可复现命令。规则冻结后用新增数据验证，不把看过的历史重新称为样本外。更新上游前用`scripts/upgrade_check.py`预检，并重点复核启动、仓库、指标、AI适配和回测接线。

本轮整理验收（2026-10-18）：247项受影响模块测试通过；TypeScript检查与Vite生产构建通过（本机无pnpm命令，直接调用已安装的tsc/vite入口）。新增模块Ruff通过，修改文件与上游基线比较不新增告警；未清理上游存量告警。保留5条Polars分组asof排序提示与Vite大包提示。真实浏览器视觉验收及完整账户回测仍未通过。新增ETF重叠缓存冲突、概念名称空白和知行缓存预热回归；测试包加标记以免被环境同名tests包遮蔽。
