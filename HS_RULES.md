# 头肩告警：现口径 + 服务器运维

> 逐条用户建议、每次改动的动机和每一轮重跑数据在 **[HS_REVIEW_NOTES.md](HS_REVIEW_NOTES.md)**（台账，按提出时间编号）。
> 这份文档只留两件事：**当前生效的口径**（对着代码，不重复台账故事）和**必须在服务器上做的操作**。
> 口径以 `git log -- strategies/head_shoulders.py` 为准，本文对齐 2026-11-16 的「全周期右肩当根即告警」口径（台账 #33）。

## 1. 当前形态口径

**点位与肩**

| 项 | 现口径 | 出处 |
|----|--------|------|
| 形态点位 | 肩、头、颈线拐点**一律按收盘价量**；蜡烛 high/low 只用于止盈止损摆动 | `strategies/head_shoulders.py:197` `_pivot_series` |
| 肩的取法 | `confirmed_shoulders=False`（默认）：两侧确认极值 **+ 单边尾部候选**，所以右肩可以是当前这根；`=True` 时只认两侧确认极值（慢口径，回测对比用） | `:79` `:124` `:225` |
| 触发时机 | **右肩收盘确定当根即告警/开仓**（`idx_r == last_idx`），全周期不延迟；慢口径才是 `last_idx == idx_r + pivot_order` | `:246` `_right_shoulder_confirmed` |
| 多候选取舍 | 肩形优先：两肩价差最小 → 最早合格右肩 → 左右腿最均衡 → 头高（**不再跟着 `confirmed_shoulders` 分叉**） | `:689` |
| 图上颈线 | 锚点同样取收盘价 | `core/hs_chart.py:120` |

**`_shape_ok` 的闸，按代码顺序**（`:284`）

1. `height > 0` 且 `height ≥ min_head_neck_points = 10` 个价点（`:62`，防小波动凑形态）
2. `height / price ≥ 0.008`
3. 颈线斜率 `|p1 − p2| ≤ 0.25 × height`（锚点落在单边急涨急跌远端时颈线会贴趋势）
4. 两肩突出：每肩离头 `≥ 0.2 × height`（`shoulder_prominence_ratio`，`:71`）
5. `|左肩 − 右肩| ≤ 0.5 × height`
6. 颈线锚点间隔：`n1 − idx_l ≥ gap`、`idx_r − n2 ≥ gap`，`gap` = **1m 用 3 根**（`shoulder_neck_bars`）、其余周期 1 根
7. 肩离颈余量：每肩到颈线 `≥ 0.1 × height`（`min_shoulder_neck_ratio`，`:73`；原 0.3× 已取消）
8. `strict_shape` 额外三条：`|左肩−右肩| ≤ 1.5% × 颈线价`；**左右腿跨度比 ≤ 2.5**（`max_shoulder_head_span_ratio`，双重/平台/三重头豁免）；颈线被收盘价穿透（容差 ±0.1×height）即拒
   → **严格档这一条换成 `|右肩−左肩| ÷ 左肩 ≤ 0.025`**（`strict_dev_threshold`，台账 #38），其余六条两档共用。
9. 平台头判定容差 `flat_head_tolerance_ratio = 0.1 × height`（`:76`，`68c08dc` 刚从 0.25 收紧——0.25 太宽会把普通上涨段误判成平台头，导致对称闸形同虚设）

**已移除**：130 分钟跨夜间隙闸、≤5 交易日跨度闸（台账 #15、#21）。

**周期参数**（`core/hs_monitor.py:52` `TF_CONFIGS`，`make_strategy` 在 `:62`）

| 周期 | pivot_order | min_span | max_span | lookback |
|------|-------------|----------|----------|----------|
| 1m | 8 | 25 | 110 | 500 |
| 5m | 6 | 20 | 110 | 400 |
| 30m | 4 | 10 | 110 | 300 |
| 1h | 3 | 8 | 110 | 250 |
| 2h | 3 | 6 | 110 | 200 |
| 1d | 2 | 8 | 110 | 220 |

**去重**：`pattern_id = kind:左肩时刻:头时刻:右肩时刻` + `kind-head:头时刻`，全部用 **K 线时刻**（`:611`）。窗口每根都在滑，相对下标每根都变，用下标会同一个头反复告警。

**胜率口径**（`scripts/replay_winrate.py`）：信号根收盘价进场，止盈 ±0.8×swing、止损 ∓0.2×swing，`HORIZON=120` 根未决记超时，同一根 both-touch 记亏损。
→ **平衡胜率 = 0.2 / (0.8 + 0.2) = 20%**。低于 20% 就是负期望，读结果时先拿这条线卡。

**收盘前抢跑**（11-16 加，`PROVISIONAL_DURATIONS` / `PROVISIONAL_WINDOWS` 在 `core/hs_monitor.py:43`）：14:45–15:00、22:30–23:00 这两个窗口里，把还开着的 30m/1h/2h 半截会话 K 也送去检测，右肩一露头就告警，免得等到收线已经买不到。三条硬约束：**只提醒不开仓**（抢跑走独立 `HsMonitorEngine`，`on_signal` 恒为 None，半截 K 也不喂 `broker.on_bar`）；**不碰正式路径**（独立引擎，`_traded_patterns` 分开，抢跑注册过的头不会把收线那条正式告警吃掉）；**按分钟节流**（半截 K 的内容每根 1m 才变一次，0.5s 的轮询里不重复审）。告警带 `extra.provisional=true`、kind 后缀 `·抢跑`，主告警列表照旧全部显示，App 另有「抢跑」标签页只看它们。

**严格档（第二优先级通道）**（11-17 加，同日二次改动去掉延迟确认；开关 `STRICT_SIGNALS` 在 `core/hs_monitor.py:52`，参数在 `strategies/head_shoulders.py:125`）：与常规档**同刻**（右肩收盘当根）出的第二套信号，走同一个 `_scan`、同一套候选，只多三道闸；两档各一套记账表（`_traded_patterns` / `_traded_strict`），互不吃对方的去重，常规档口径一个字没改。原方案的「右肩之后再连续 2 根收盘不创新极值」按用户指示**删掉**了——告警就在发现那根出。多出来的三道闸：

1. **肩差换算法**：`|右肩−左肩| ÷ 左肩 ≤ strict_dev_threshold = 0.025`（`:475`）替掉闸 8 的 `1.5% × 颈线`。注意**这条更宽**（头肩底颈线低于左肩、顶高于左肩，0.025×肩价 ≈ 那条的 1.6~1.7 倍），不是「严格档=常规档子集」。
2. **形态够长**：`idx_r − idx_l + 1 ≥ strict_min_form_bars = 8` 根。
3. **三段均量递减**（`strict_volume_gates=True`，`_volumes_ok` `:240`）：底 = `VolAvg(头腿) < VolAvg(左肩腿)` 且 `VolAvg(右肩腿) ≤ VolAvg(头腿)`，顶把 `<` / `≤` 对调；段起点取「上一个反弹拐点 → 该锚点」，均量是段内成交量均值；`strict_pulse_filter`（默认 **关**）可先剔掉 `> strict_pulse_multiple = 3×` 均量的脉冲根再取均值。

止盈止损**沿用老规则**（±0.8/∓0.2×swing），颈线只用来算目标，**不再当严格档的开仓条件**——所以严格档进场点会比颈线突破早，是「预判入场」，假信号更多，用户据此把仓位压到一半。

优先级与发射：严格档那条带 kind 后缀 `·严格`、reason 前缀 `严格`、`extra.strict=true`、`extra.pattern = "品种|周期|pattern_id"`（`core/hs_monitor.py:213`；必须带品种和周期，`pattern_id` 只有三根 K 线时刻，不同品种会同名）。两档同刻出时由 `tier_dets`（`core/hs_monitor.py:263`）**直接把同形态的常规档那条不发**，列表里自然只剩严格档；库里**已经存在**的同键常规档（典型是抢跑那半截 K 提前出的那条）仍由 `add_alert(supersedes=)` 打上 `superseded`（`core/alert_store.py:85`，**只标记不删**——它可能已推送、还挂着模拟盘单的 `alert_id`），`list_alerts` 默认不返回被顶掉的。`/api/alerts?strict=1` 单看这一档。抢跑通道不参与严格档（`core/hs_fanout.py:83`：半截 K 只出常规档那条提醒）。

模拟盘：`STRICT_LOT_RATIO = 0.5`（`core/paper_trade.py:42`）→ 预算 = 可用资金 × 50% 再开满（`:577`）。同形态时常规档那条不再发射，所以严格档先下这笔半仓，`_open` 的 (品种, 周期) 去重不会再把它挡掉（改造前是常规档先开满、严格档被「已有持仓」跳过）。

`HS_STRICT_SIGNALS=0` 让严格档整体静默（含告警与模拟盘），用于出对照。代价是策略侧每根多跑完整一遍（§2 有实测比值），不是白加的。

## 2. 反复踩过的坑

- **去重键用窗口相对下标 → 同一个头反复告警**（SH 30m 曾一个头出 4 条）。
- **回放预过滤必须跟触发口径一致**：`scripts/audit_killed_samples.py:42` 现在按 `strat.confirmed_shoulders` 分流——当根口径看 `i` 是否盖过前 `order` 根，慢口径看 `i - order` 是否两侧极值。改触发规则时这里必须跟着改，否则回测报 0 条。
- **当根告警的代价**：尾部候选只要盖过前 `pivot_order` 根就算右肩，而 11-13 实测「仅凭单边就成立」的候选里 57.1%（低点侧 70.1%）会在 `pivot_order` 根内被更极端的同向拐点盖掉——这部分告警属于抢跑后被证伪，只能靠胜率数据衡量。
- **loose / strict 是两次独立回放，改后不是改前的严格子集**：`滤掉 N 条` 是时间戳集合差，`strict − (loose ∩ strict)` 那部分是只在改后侧新出现的。别把「改后 ≤ 改前」当恒等式读。
- **平台头豁免容差不能太宽**：0.25×头高时 lu 上涨段有 ≥4 根收盘落在带内被误判成平台头，对称闸 2.5 直接形同虚设（台账 #31）。
- **严格档 ≠ 常规档的子集，两档条数只能按集合差读**：闸 1 的 `Dev ≤ 2.5%`（按左肩价）比常规档的 `1.5%×颈线` 宽，会放进常规档拒掉的形态；闸 2/3 又拒掉一批常规档收下的。实测（2026-11-17，生产 RDS 只读，60 天 × {PF, jd, eg} × {30m, 5m} 逐根滚动，`scripts/replay_strict_tier.py`）：常规档 113 条、严格档 34 条，其中**同形态**（=同刻把常规档那条让位掉）17 条、严格档独占 17 条 → 入库净条数 `113 − 17 + 34 = 130`（比只有常规档多 15%，其中 34 条带 `·严格`）。同一批数据上 `on_bar` 改前/改后/两档同实例三跑输出的 `(datetime, reason)` 序列**逐条一致**（等价性守住，别拿这个当改动风险的代理）。
- **严格档每根都跑完整一遍，策略侧成本 = 两遍**（11-17 去掉延迟确认之后就是这样，延迟版那层 O(pivot_order) 预筛没了）：本机实测（`_strict_cost.py`，30 天真实库逐根滚动，同一份窗口分别调 `on_bar` / `on_bar_strict`）eg 5m **6.40 → +6.43 ms**、jd 1m 5.51 → +5.52、PF 30m 3.27 → +3.50，即严格档那一跑与常规档几乎等价，**策略侧翻倍**。绝对值别直接搬：本机比 11-16 服务器口径（1m 2.7 / 5m 2.5 / 30m 3.6 ms）慢一截，用比值。按服务器那份结构推（130 键）：策略侧 `0.35s → ~0.7s`，取数侧 4.2s 的库往返不变 → 单轮增量 <0.4s，占现分片稳态 3.74s 的 ~10%；但这是 CPU 不是等待，3 片仍要同时占 3 核。**待线上复核**：`reports/monitor.log` 的 `本轮扫描 ... 用时` 与 `docker stats`。
- **加载窗不够会静默跳过周期**：`run_backtest.py:45` 的 `LOAD_DAYS` = 1m/5m 60 天、30m 90、1h 120、2h 150、1d 300。60 天窗里 SH 的 2h 只有约 107 根，低于阈值就不出数；中金所品种只有日盘，1h 也会缺。看到某周期整列缺失先按这个算，不是 bug。
- `datetime_end` 字段对 raw 周期（1m/3m/5m/10m/15m）其实是**起始**时刻，只有会话合成周期（30m/1h/2h/3h/4h）才是结束时刻。
  证据：`serial_to_rows` 用 K 线的 `datetime`（=起点）算 `start_utc`（`core/realtime_pipeline.py:238`），再把它塞进 `upsert_bars` 的 `datetime_end`（`:592`）。
  **读告警延迟时必须先减掉这根的长度**：App 里 1m 显示 `K线时间 10:01:00 / 发现时间 10:02:14`，看着像差 1 分 14 秒，实际是这根在 10:02:00 才收线，真实延迟 **14 秒**。
  剩下的十几秒来自 monitor 的**扫描串行度**（`poll_sec=0.5` 一圈，按水位把本轮有新根的品种逐个 `load_tf_df` + `on_bar`，排在后面的更晚出）。
  **1d 两头都不是**：库里存交易日当天 00:00（历史里还有 16:00 的 UTC 写法，`detect_clock` 就是为这个），收盘口径按当天 15:00 显示。
  App 卡片自 `K线收盘` 这一行起替用户做完换算（1m/5m 加一根长度、30m/1h/2h 原样、1d 归到当天 15:00），代价是 reason 里的 `右…(14:50)`（仍是库里的原始标签）与 `K线收盘 14:55` 在 1m/5m 上差一个周期——台账 #35 那条「右时刻 = K线时刻」的验收等式从此只在服务端成立，肉眼对 App 卡片别再套。
  实测（2026-11-16，生产 RDS 只读，6 个主连逐根滚动回放，`_hs_real_diff.py`）：向量化前 1m（lookback 500、pivot_order 8、strict 开）**中位 53 ms/根**，5m 49 ms、30m 48 ms → 130 品种 1m 一轮策略侧 ≈ 7s；向量化后 2.7 / 2.5 / 3.6 ms（**约 20×**，见 3 节）。
  取数侧另有硬成本：`load_tf_df` 单键 **32 ms**（`load_bars` 取 500 根的 MySQL 往返，`bars_to_strategy_df` 只占 1.6 ms）→ 130 键串行 ≈ **4.2s 纯 I/O**，现在它才是扫描的大头。
  11-16 已把扫描改成 **3 进程按品种分片**（`core/hs_fanout.py`，`HS_WORKERS` 可调，<2 或任何异常退回串行）：240 键一批实测串行中位 11.22s → 分片稳态 3.74s（**3.0×**，首轮含 worker 冷启动 8.55s）。能拿满 3× 是因为每键那 32 ms 的库往返本来就在等，分片把等待也并发了——不是 CPU 变快（策略侧只占扫描的 8%）。推论（上线后待实测）：一轮扫描的 CPU 总量不变，但同样多的核时被压进 1/3 的时间，分钟边界的脉冲会更尖、同时占 3 个核；看 `docker stats` 时别把这当成回归。
  合成随机游走的 345 ms/根是**上界不是量级**（极值密度比真实数据高）。**与「右肩当根告警」无关**（reason 里右肩时刻 = K 线时刻即口径成立）。
- **改动必须先 commit + push，再给服务器 pull 命令**：漏一次就白跑一轮回测（60 个作业全崩，台账 #29）。

## 3. 回测 / A/B：只在服务器上跑

分工：**生产 4C8G `116.62.103.114`**（i-bp1gio4o2xsynj0rsnoiZ）跑实时链路；**老 2C2G `120.26.26.72`** 是回测专用机。本机（Windows）只做 `py_compile` 和单品种轻量自检。休市日在生产机上跑回测可以接受（工作时段别跑，会抢实时链路的核）。

老机红线：**绝不 `docker compose up`**（一起手就拉起 ingest/monitor → 双份订阅、双份告警、双份模拟盘、天勤配额翻倍）。老机只用 `docker build` + `docker run -d --rm`，旧容器保持 Exited。

```bash
cd /opt/futures_quant
git stash push deploy/install_docker.sh   # 老机这个文件有本地改动，否则 pull 被中止
git pull
docker build -t futures-quant-api .       # 结尾空格+点不能少；不 build 则 docker run 秒退 can't open file
```

批量回测（`scripts/run_backtest.py`，默认 60 品种 × 60 天 × 6 周期，loose vs strict 双跑并出画廊）。生产机上最省事的是走一次性服务（`restart: "no"`，跑完自己退出，`--no-deps` 避免连带起点别的服务）：

```bash
cd /opt/futures_quant && git pull && docker compose build futures-backtest && docker compose run --rm --no-deps futures-backtest
```

老机没有 compose 在线，用手工容器——注意**重定向要写在容器内的 `sh -c` 里**，写在 `docker run -d` 后面只会建个空文件：

```bash
docker run -d --rm --name bt --env-file .env -v $PWD/reports:/app/reports -v $PWD/data:/app/data futures-quant-api sh -c "python -u scripts/run_backtest.py >> /app/reports/backtest.log 2>&1"
```

- 开关：`--days` `--workers`（默认 2，或 `BACKTEST_WORKERS`）`--tfs` `--symbols` `--outdir`；三个**只影响回测、实盘口径不变**的对比开关 `--pivot-override 300:1`、`--load-days N`、`--prominence 0.25`。`--allow-session-gap` 已随 130 分钟闸一起移除。
- 输出目录随开关变：默认 `reports/backtest/`，带 `--pivot-override`→`backtest_pivot/`，带 `--prominence`→`backtest_prom/`，带 `--outdir` 就以它为准。
- 产物：`summary.json`（`total` + `by_tf`）、`gallery_single.html`（命中，base64 自包含，678 张 ≈27MB）、`gallery_killed.html`（被滤）、逐周期 `reports/backtest/<label>/meta_<label>.json` 与 `killed/<label>/meta_killed.json`（元素键 = `file/symbol/when/reason`）。
- 进度看 `grep -a '\[backtest\]' reports/backtest.log | tail`；该 log 是 `>>` 追加，顶部残留上次失败行，**以最后一条表头为准**。

严格档两档计数（`scripts/replay_strict_tier.py`：同一段历史逐根滚动，两档各出一遍，输出「常规档 N / 严格档 M（占 x%，顶掉同键 K，独占 M−K）」+ 每周期样例）。`--days` 给短了会整片出 0——根数不足 `max_span + 2×pivot_order + 5` 的品种会被跳过并在汇总行里点名，看到 0 先读这行，不是没信号：

```bash
docker run -d --rm --name strict_tier --env-file .env -v $PWD/reports:/app/reports -v $PWD/data:/app/data futures-quant-api sh -c "python -u scripts/replay_strict_tier.py --days 60 --workers 2 >> /app/reports/strict_tier.log 2>&1"
```

- 对照：加 `-e HS_STRICT_SIGNALS=0` 重跑一次，严格档那几列应全 0、常规档列不动——这是验证开关真的管用。
- 本机只做轻量自检（≤3 品种 × ≤2 周期）；全市场 130 品种一律上服务器，2 机并行时 `--workers` 别超 2。

规则 A/B（`scripts/ab_hs_rules.py`，OLD 用类方法补丁还原旧口径）：`HS_AB_SHARD=k/n`、`HS_AB_TFS`、`HS_AB_SYMBOLS`、`HS_AB_OUT`、`HS_AB_DUMP=1`。命令一律**写成单行**，反斜杠续行粘进终端会被截断成 `-bash: ocker: command not found`。

```bash
docker run -d --rm --name ab_hs_0 --env-file .env -e HS_AB_SHARD=0/2 -e HS_AB_TFS=30m,1h,2h -e HS_AB_OUT=/app/reports/ab_hs_0.txt -v $PWD/reports:/app/reports futures-quant-api python -u scripts/ab_hs_rules.py
```

ETA：`awk -F'[()]' 'NF>2{gsub("s","",$2); s+=$2; n++} END{if(n)printf "已完成 %d 个，平均 %.0f 秒/个，两片并行剩余约 %.0f 分钟\n", n, s/n, (89-n)/2*s/n/60}' reports/ab_hs_0.txt reports/ab_hs_1.txt`——线性下限，**实际要 ×2**（两片抢 2 核 + 云盘）。

容器 `Up ... (unhealthy)` 是镜像 healthcheck 在探 `:8000/health`，跑脚本的容器不起 HTTP，**必红不是挂了**，别 stop。

实时链路 CPU 归因（2026-11-16 本机微基准：随机游走 + 400 根滑窗 + 5m 参数）：单根 `on_bar` 慢口径 **103.9 ms** vs 当根口径 **105.9 ms**——**切触发口径不省 CPU**（差 <2%，尾部候选只多一个 pivot）；关掉 `strict_shape` 才降到 62–66 ms（−40%），大头在 strict 的肩部主导性三重循环 + pandas 切片。
**这条已在 11-16 向量化拆掉**：`_range_aggs` 稀疏表接管区间 min/max（`pts.iloc[a:b].max()` 每次 20–30µs 的 pandas 切片 → O(1) numpy 查表），拐点扫描 / 颈线穿透 / 平台头计数改 numpy，`(li,hi)` 级只算一次的量（`neck_l`、`between_l`、`head_time`）从 `ri` 循环里提出去。真实库回放 1m **53.3 → 2.7 ms/根**、5m 49.0 → 2.5、30m 48.3 → 3.6；等价性用 144 个合成用例（pivot_order 3/4/8 × strict × confirmed × 60/300s × 含 NaN）+ 6 品种 × 3 周期真实回放（36 条信号、逐根 `last_trace`）**全部零偏差**。校验脚本 `_hs_equiv.py` / `_hs_real_diff.py`（基线副本 `git show 348ea03:strategies/head_shoulders.py > _hs_baseline.py` 现取）。
改造后 monitor 一轮 130 键的成本结构变了：策略侧 0.35s、取数侧 4.2s（32 ms/键的 MySQL 往返），**所以再压缩延迟要从「每键重取 500 根」下手，不是加进程分 CPU**。
**这句当天就被实测打了脸**：分片照样拿到 3.0×（见 2 节的 `_hs_fanout_diff.py` 数字），因为每键 32 ms 里大头是**等 RDS 回包**，多进程并发的正是这段等待，不是 CPU。真正没被分片解决的是「每键重取 500 根」本身——要再降一个量级仍得走增量取数 / 按键缓存 frame。
分片实现（`core/hs_fanout.py`）：按**品种**（crc32，`hash()` 每进程加盐不可用）分成 `HS_WORKERS`（默认 3）片，worker 只算形态并把纯数据的 `Detection` + 最后一根 OHLC 回传；告警落盘、App 推送、模拟盘开仓**全部留在父进程**（`alerts.json` 的锁是进程内的，TqApi 也不能跨进程）。任何一处对不上（worker 退出 / 超 `HS_WORKER_TIMEOUT=30s` 没回 / 结果序号或条数不符）就置 `broken`，本轮起整体退回串行兜底；`HS_WORKERS=1` 直接走串行。`HsFanout` 在构造时就 spawn，所以只在 `run_forever` 里 TqApi 和库都起来之后才建——启动失败外层每 15s 重建一次 `DbHsMonitor`，早建会每次漏 3 个子进程。
总耗时 ≈ 品种数 × 单根耗时，1m 每分钟全品种各算一次，60 品种 ≈ 6–9 秒/分钟 ≈ 4 核 3–4%。控制台掉到个位数百分比时先分清三种原因：负载本来就轻（午休 11:30–13:30、小节休息 10:15–10:30、收盘后没有新 1m 根）、回测容器已挪去老机、**还是数据不推进**（水位不动 → `_tick` 整根跳过，这种情况告警也一起没了，是坏消息）。查水位：`docker compose exec -T futures-monitor python -` 里跑 `load_watermarks(conn, HS_MONITOR_DURATIONS, list_symbols(conn, duration_sec=60))`，看 1m 的 `max` 有没有贴着 now。

## 4. 部署与看结果

- **代码是 `COPY` 进镜像的，`git pull` + `restart` 等于什么都没做。** 生产上必须：
  `docker compose up -d --build futures-api futures-ingest futures-monitor`
  服务名显式列全、**绝不带 `futures-backtest`**（一次性回测任务，裸 `up` 会把它点起来打满 CPU）。
- 验证真进容器：`docker compose exec futures-monitor grep -c <新函数名> /app/core/paper_trade.py`（`grep -c` 数匹配行数，只出现一次时期望值就是 `1`）。
- `docker compose logs futures-monitor`（和 ingest）**永远是空的，不是故障**——stdout 被重定向进 `reports/*.log`，用 `tail -n 40 reports/monitor.log`。
- 分片并发数走 `HS_WORKERS`（`.env` 或 compose `environment:`），改这个**只需 `docker compose up -d futures-monitor` 重建容器，不用 `--build`**；设成 1 等于关掉分片。生产是 4 核、ingest 常态吃掉 1 核，3 是当前的折中。
- 分片上线后的日志标记（11-16 11:48 部署、13:30 开盘实测）：`[monitor] 检测分片已启动 workers=3` → 启动行 `poll=0.5s workers=3（只读库，不订 K 线）` → 开盘后第一轮 `水位预热 … loaded=… in …s keys=… sum=…s max=…`，之后每次有键要算都打 `本轮扫描 pending=… keys=… sum=… 用时 …s`。`docker top futures-quant-monitor | grep -c spawn_main` 期望 3。**免责条款每次启动多打 3 行不是重启循环**，是 3 个 spawn 子进程各自 import tqsdk。午休/收盘后只有 `休市 …, 15s 后再检查`，此时看不到扫描行属正常。
  扫描行原本挂了 `scan_sec >= 1.0` 阈值，分片后单轮普遍 <1s 就**一条都不打**（13:30 那批就是这样，只能反推「没打=都很快」）；已改成每轮必打，并把 `pending=` 一起带上区分「算了几键」和「几键拿到了数据」。
- 画廊路由（只有生产机有 HTTP）：`/backtest`、`/backtest/killed`、`/backtest/pivot`、`/backtest/prom`、`/backtest/neck`。路由只是读 `reports/<变体>/gallery_*.html`，缺文件就 404 且 detail 写明缺哪个；在别的机器跑出来的要先 `scp -r` 到生产机 `/opt/futures_quant/reports/`，**到位后不用重启任何容器**。
- 生产 sshd 只认密钥（`PasswordAuthentication no`，报 `Permission denied (publickey...)` 且不问密码不是输错）。老机→生产机已互信。

## 5. 未闭环

- 台账里的待决策：#10 左肩≈颈线是否另加闸；#12/#13 是否单跑「仅 earliest-R」；5m 单周期细化（sym/conf 两轮里 5m 都是唯一改善的周期，但合计仍为负）。
- **合计仍贴着 20% 平衡线**（最近一轮 20.8%/+0.008），5m 与 1d 是稳定拖累——见台账 32 条。
- 数据修复：11-06～11-10 的 30m/1h/2h/3h/4h 会话周期回填
  `docker compose run --rm --no-deps futures-ingest python -u scripts/rebuild_session_klines.py --all --start 2026-11-06 --end 2026-11-10`，确认输出无误再加 `--execute`；1d 的 11-06 另修。
- **1d 又缺 11-14/11-15**（11-16 09:56 水位表 `dur=86400 max=2026-11-13 00:00`）。`rebuild_session_klines.py` 只合成 30m/1h/2h/3h/4h，不管 1d/5m；1d 用
  `docker compose run --rm --no-deps futures-ingest python -u scripts/repair_today_klines.py --day 2026-11-14 --durations 1d`（再 `--day 2026-11-15`）。该脚本**直接写库、无 dry-run**，且会再开一个 TqApi 会话占配额。
- **5m 与 1d 只有 89 个品种有数据，1m 有 130**（同一张水位表）。差额应是「历史回填写过的品种」多于「实时订阅的品种」——用 `grep "品种" reports/ingest.log | tail -3` 核 `list_commodity_mains` 返回的数量；若确实只有 89，那这 41 个品种的 5m/1d **永远不出告警**，要覆盖得补历史或加订阅。
- ingest 常态就是 ≈ 单核打满（`docker stats` 5 次采样 90.8/95.9/100.0/99.7/100.0%，4 核约 25%；monitor 则是分钟边界脉冲，同一批采样 27.0/0.7/23.0/1.0/21.3%，api 0.06%）。主因已定位：`serial_to_rows` 用 `klines.iloc[i]["col"]` 逐行取标量，**60 行的 1m 序列一次实测 25.8 ms**；按单核打满反推 ≈ 每秒 35–39 次调用（每 0.25s 圈约 9 条序列被 `is_changing` 判为变化），数字自洽。降载两档：向量化取列（输出逐行一致，实测 5.5 ms，→ 约 20% 单核）、或只解析水位之后的行（调用方本就按 watermark 过滤，可 <1 ms，→ 个位数百分比，但要连带核 `correction`/半截K 判定）。**不改告警语义**，且 ingest 是 `enable_monitor=False` 不跑策略——所以告警口径怎么改都不影响它，别把 CPU 变化算成规则改动的功劳。
- 开仓时点：一年基线和 30 天贴收盘（仅 2 条、竞价逆向选择）都**否定**了抢收盘跳空，规则未改。
- **3 进程分片已上生产并验收通过（11-16 13:30 开盘）**：预热 261 键 `sum=12.64s` → 墙钟 `4.7s` = **2.69×**；13:30/13:35 的告警轮**一条 `本轮扫描` 都没打**（阈值 1s）⇒ 单轮全部 <1s，改造前这批键是秒级起步。告警口径不变（`右(...) 时刻 = K线时刻`）。资源：monitor MEM 153.8→**479.8 MiB（+326）**、PIDS 9→37、CPU 1.77%，**ingest 20.47% 未被抢**。剩下两档：①每键重取 500 根 → 按键缓存 frame + 增量取数；②新扫描行（每轮必打）要等下次 `--build` 部署才生效。
