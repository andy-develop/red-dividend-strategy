# 红利低波 · 情绪极值策略监控（每日 8:00 自动更新）

中证红利低波动全收益指数（H20269）v7.8 策略的实时监控产品：
**新手漏斗式仪表盘**：「① 今天做什么（人话结论+对应ETF+金额）→ ② 市场情绪温度计 → ③ 我的仓位（本金输入自动换算）→ ④ 回测证据/术语/买卖点（默认折叠）」，每天北京时间 8:00 由 GitHub Actions 自动更新并覆盖推送到 HSK 文件托管。

## 目录结构

```
repo/
├── backtest/engine.py        # 统一回测引擎（信号 px / 收益 TR / T+1 / 滑点 / 融资成本）
├── backtest/scan_sensitivity.py  # 过拟合体检
├── backtest/h20269_daily.csv / h30269_daily.csv  # 行情源（本地回测用）
├── backtest/sensitivity.json # 参数敏感性 + walk-forward 结果
└── product/
    ├── index.html              # 对外展示页（由模板+数据生成，推送此文件）
├── index_template.html     # 页面模板（占位符 __PAYLOAD__，注入今日快照+回测数据）
├── update.py               # 每日更新脚本：抓行情 → 统一回测引擎 → 生成 index.html
├── backtest_data.json      # 回测数据（日频全量净值/回撤抽样展示 + 历史买卖点，每日滚动重生成）
├── requirements.txt        # 依赖锁定（pandas / numpy）
├── tests/test_engine.py    # 统一引擎最小单元测试（16 用例）
├── .github/workflows/daily.yml
└── README.md
```

回测引擎在本仓库 `backtest/engine.py`（唯一实现，update.py 与回测共用，杜绝双实现漂移）；
`backtest/scan_sensitivity.py` 产出过拟合体检（参数敏感性 + walk-forward）→ `backtest/sensitivity.json`。

## 本地运行

```bash
pip install -r requirements.txt
python3 -m unittest tests.test_engine -v   # 引擎单测（16 用例，验证指标/状态机/T+1/净值复算/抄底闭环）
python3 update.py                          # 在线抓行情 → 生成 index.html + backtest_data.json
python3 -m http.server 8000                # 本地预览 http://localhost:8000/index.html
```

## 统一回测引擎（backtest/engine.py，v7.8 口径）

- **信号与收益分离**：全部信号（布林/KDJ/RSI/63日动量/顶背离）在**价格指数 H30269** 上计算；收益用**全收益 H20269**（分红再投）。`px` 不再是死代码。
- **T+1 撮合**：T 日收盘确认信号 → **T+1 收盘成交**（数据无开盘价，以 T+1 收盘近似，如实披露）；单边**滑点 5bp**。
- **杠杆真实成本**：125%/150% 仓位隐含 25%/50% 融资，按**年化 7% 按交易日计息**（每日计提）。
- **日频全量**：净值/回撤/夏普全部在日频全量数据上计算（每交易日一点），不抽稀；页面图表抽样仅为展示。
- **费用**：单边 max(金额×万1, 5元)；抄底胜率按含滑点成交价 FIFO 配对。
- 指标：布林 20 日均线±2σ（ddof=0）、周线 KDJ(9,3,3)（ISO 周重采样、J=3K−2D）、周线 RSI(14) Wilder、日线 RSI(14)（顶背离）、近 63 日动量（up63/dn63，超卖阈值 −20%）。

**回测结果（v7.8，2016-09-08 ~ 2026-09-08，初始 10 万）**：
策略 **+156.2% / 夏普 0.62 / 最大回撤 −31.0%**；买入持有 +134.3% / 0.62 / −27.5%。59 笔；抄底 29 档胜率 72.4%（21 盈/8 亏，平均 +3.4%）。v7.8 = v7.7 将超卖跌幅阈值 20%→15%（敏感性唯一稳健增益点），并修复引擎 warm-up 与周线重采样（见 HANDOFF 第 13 节）。
（v7.6 的 +328.5%/0.97/−27.6% 基于"同日收盘成交+零成本杠杆+TR 信号"，不可复现且系统性乐观，已废弃。）

## 每日更新逻辑（update.py）

1. **抓行情**：中证指数官网 `index-perf` 接口，抓 H20269（全收益）与 H30269（价格）最近 3800 个自然日收盘价（覆盖回测起点、MA200、周线 KDJ 收敛、63 日动量与状态机重放余量；接口限流自动退避重试）。
2. **算指标 + 状态机重放 + 净值核算**：全部复用 `backtest/engine.py`（单一实现）。
3. **触发预告**：计算距「超卖 2-of-4 / 超买三维极值 / 动能消失」还需的跌幅/涨幅缺口，页面展示"今天如果再跌 X% 将触发…"。
4. **回测数据每日滚动重生成**：`backtest_data.json` 由引擎按最新行情重算（不冻结），指标用日频全量。
5. **生成自包含 index.html**：内嵌今日快照 + 回测数据，覆盖写入。

## GitHub Actions 定时发布

仓库需配置（GitHub 仓库 Settings → Secrets and variables → Actions）：

| 配置项 | 值 | 说明 |
|---|---|---|
| Secret `HSK_API_KEY` | `ph_key_…` | HSK 文件托管 API Key（用户持有） |
| Secret `HSK_RESOURCE_ID` | `1788920564682150822` | 覆盖推送同一资源（经 `secrets.HSK_RESOURCE_ID` 引用；该账号 Actions variables 端点不可用，故走 Secret） |

当前已发布资源：

- 公网地址：`https://945q5w.gicp.fun`
- 资源 ID：`1788920564682150822`
- 更新模式：`hsk-cli +host index.html --resource-id 1788920564682150822 --format json`（update 后 CLI 可能返回 `claimed:false/pending`，属正常现象，实测内容即时生效、URL 持续可访问，无需处理）

**启用定时任务**：把 `product/` 下全部文件推到 GitHub 仓库（保持目录结构），Actions 即按 cron（UTC 00:00 = 北京 08:00）自动运行；也可在 Actions 页手动 `workflow_dispatch` 立即触发。
注意：workflow 为 `contents: read`，**不回写仓库**，`index.html` 的仓库副本与线上发布版随每次更新存在差异（线上为准）；如需仓库副本同步需改为回写 job。

## 数据口径与免责声明

- 回测：2016-09-08 ~ 最新交易日（10 年，2427+ 交易日），初始 10 万，费用单边 max(万1, 5元)，滑点单边 5bp，融资年化 7% 按交易日计息，年化 252 交易日，夏普无风险利率 0。
- 超卖买点＝2-of-4 共振：J<1 / 跌破布林下轨 / dn63≤−20% / 周线 RSI<35 任意 2 个 → 加仓 125%（再触发至 150%）。
- A 态清仓＝超买三维极值：J>95 且 触及/突破上轨 且 up63≥20% → 离场 0%；离场后 60 自然日强制回补。
- 临时仓（125/150%）卖出＝**动能消失确认**，任一触发即了结回 100%：J 从>90跌破80 / RSI 从>70跌破65 / 10日顶背离（收盘创新高且日线 RSI 未新高）。
- 到期兜底：每档 25% 独立 60 自然日到期强制卖出。
- **过拟合体检**（`backtest/sensitivity.json`）：关键参数（J_LOW/J_HIGH/Y_DOWN/X_UP/RSI_OS/HOLD_DAYS/滑点/融资利率）±扰动，除 X_UP 外均位于"参数平原"（夏普波动 ≤0.05）；walk-forward 三区间夏普 0.45/0.56/0.75，无区间崩溃。完整 PBO / Deflated Sharpe 未实现，属剩余披露项。
- 行情源为中证官网官方日度数据，Actions 环境若被 WAF 拦截需调整抓取源；当前无多源兜底（剩余披露项）。
- 仅供策略验证与监控参考，不构成投资建议。125%/150% 超配隐含融资，实际执行需评估融资成本。
