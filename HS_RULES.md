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
2. `height / price ≥ 0.008`（**严格档可选** `HS_STRICT_HEAD_BOOST=1` 换成「头比肩 ≥0.3%」：顶 `(head−左/右肩)/肩`、底 `(左/右肩−head)/head`；默认关，A/B 后再决定，用户 11-22 口径）
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
11-18 起**两档并排出数**：同一次取数上跑 `on_bar` 与 `on_bar_strict` 两遍（`replay_detail(..., strict_tier=True)`，回测里强制打开 `strict_signals`，不受生产 `HS_STRICT_SIGNALS` 静默），输出 `逐周期并排` 每档一行 + `合计` 两行。开关 `--days`（默认 60）`--tfs` `--symbols`（缺省全主力）`--workers`；根数不足 `_min_bars` 的键**会被点名**，不再静默返回空。

**两档胜率实测**（2026-11-18 老机 2C2G 全主力，`--workers 2`；`p` 是两比例 z 检验，我按脚本给的胜/已决自己算的，不是脚本打印的）：

| 周期 | 窗口 | 常规档 | 严格档 | 差 | p |
|------|------|--------|--------|-----|-----|
| 5m | 60d | 21.6%（已决 1563，+0.016） | 23.2%（560，+0.032） | +1.6pp | 0.44 |
| 30m | 60d | 23.9%（335，+0.039） | 25.8%（124，+0.058） | +1.9pp | 0.67 |
| 1h | 60d | 23.0%（122，+0.030） | **15.6%（45，−0.044）** | −7.4pp | 0.30 |
| 2h | 300d | 22.8%（1012，+0.028） | **18.7%（449，−0.013）** | −4.1pp | 0.077 |
| 1d | 300d | 25.6%（320，+0.056） | 21.7%（138，+0.017） | −3.9pp | 0.38 |
| 60d 合计 | 60d | 22.1%（2020，+0.021） | 23.2%（729，+0.032） | — | — |

- 按频率分组（把上行的已决数合并算，**混了 60d 与 300d 两个窗口，只是方向性参考**）：**≤30m 严格 23.7%（+0.037）对常规 22.0%（+0.020），差 +1.7pp、p=0.37 → 不显著**；**≥1h 严格 19.1%（−0.009）对常规 23.5%（+0.035），差 −4.3pp、p=0.029 → 唯一过 5% 线的对比**。2h/1d 那份的合计行没从服务器带回，23.5%/19.1% 是我按 2h+1d 两行合并算的。
- 结论只有一条硬支撑：**长周期（≥1h）上严格档不占优，甚至略负**；而它恰恰在长周期最不像常规档的子集（独占占比 1h 57%、2h 51%、1d 57%）——新 Dev 闸放进来的那批宽肩形态，在长周期是**更早、更差**的入场。短周期两档都贴着 20% 线上方一点，看不出差别。
- 1m 没跑（老机时间成本）；1h 严格档只有 45 笔已决，单独看没有意义，别看表里 −7.4pp 就当结论。

**收盘前抢跑**（11-16 加，`PROVISIONAL_DURATIONS` / `PROVISIONAL_WINDOWS` 在 `core/hs_monitor.py:43`）：14:45–15:00、22:30–23:00 这两个窗口里，把还开着的 30m/1h/2h 半截会话 K 也送去检测，右肩一露头就告警，免得等到收线已经买不到。**11-20 用户裁决扩到三个通道**：常规档、严格档、三重顶/底都在半截 K 上算（`detect_one` 不再对 `prov` 跳过严格/三重），同形态时严格档顶掉常规档、三重独立追加，与收线路径同序。三条硬约束不变：**只提醒不开仓**（抢跑走独立 `HsMonitorEngine`，`on_signal` 恒为 None，半截 K 也不喂 `broker.on_bar`；`api` 的手动开仓对 `extra.provisional` 一律 400）；**不碰正式路径**（独立引擎，`_traded_patterns` / `_traded_strict` 分开，抢跑注册过的形态不会把收线那条正式告警吃掉）；**按分钟节流**（半截 K 的内容每根 1m 才变一次，0.5s 的轮询里不重复审）。告警带 `extra.provisional=true`、kind 后缀 `·抢跑`（严格档叠加成 `·抢跑·严格`）。App：「抢跑」页只看 `provisional=true`；「严格」页按 `strict=1` 取且**排除 provisional**（`core/alert_store.py` 的 `list_alerts`）——严格页只列已收线的严格档，半截 K 上的严格档归抢跑页；三重本来就不带 `extra.strict`，也只进抢跑页。飞书：抢跑里的严格档/三重照样推送（`emit_alert` 的 `strict or triple` 分支不分是否抢跑），常规档抢跑仍只进 App。

**严格档（第二优先级通道）**（11-17 加，同日二次改动去掉延迟确认；开关 `STRICT_SIGNALS` 在 `core/hs_monitor.py:52`，参数在 `strategies/head_shoulders.py:125`）：与常规档**同刻**（右肩收盘当根）出的第二套信号，走同一个 `_scan`、同一套候选，只多三道闸；两档各一套记账表（`_traded_patterns` / `_traded_strict`），互不吃对方的去重，常规档口径一个字没改。原方案的「右肩之后再连续 2 根收盘不创新极值」按用户指示**删掉**了——告警就在发现那根出。多出来的三道闸：

1. **肩差换算法**：`|右肩−左肩| ÷ 左肩 ≤ strict_dev_threshold = 0.025`（`:475`）替掉闸 8 的 `1.5% × 颈线`。注意**这条更宽**（头肩底颈线低于左肩、顶高于左肩，0.025×肩价 ≈ 那条的 1.6~1.7 倍），不是「严格档=常规档子集」。
2. **形态够长**：`idx_r − idx_l + 1 ≥ strict_min_form_bars = 8` 根。
3. **三段均量递减**（`strict_volume_gates=True`，`_volumes_ok` `:240`）：底 = `VolAvg(头腿) < VolAvg(左肩腿)` 且 `VolAvg(右肩腿) ≤ VolAvg(头腿)`，顶把 `<` / `≤` 对调；段起点取「上一个反弹拐点 → 该锚点」，均量是段内成交量均值；`strict_pulse_filter`（默认 **关**）可先剔掉 `> strict_pulse_multiple = 3×` 均量的脉冲根再取均值。**严格档可选** `HS_STRICT_VOL_TRIPLE=1`：改用三重同款「第三腿最弱」——顶 `v1>v3 且 v2>v3`、底 `v1<v3 且 v2<v3`（用户 11-22 裁决；默认关，A/B 后决定）。

止盈止损**沿用老规则**（±0.8/∓0.2×swing），颈线只用来算目标，**不再当严格档的开仓条件**——所以严格档进场点会比颈线突破早，是「预判入场」，假信号更多，用户据此把仓位压到一半。

**开仓来源（11-22 用户裁决）**：只有**严格档和超严格**开模拟盘；常规档、三重、抢跑都只告警/推送。落地在 `deliver(trade=...)`：`db_monitor` 与串行 `evaluate_df` 里严格档/超严格传 `True`，常规档与三重传 `False`；`prov_engine.on_signal` 本来就是 `None`，抢跑不受影响。

优先级与发射：严格档那条带 kind 后缀 `·严格`、reason 前缀 `严格`、`extra.strict=true`、`extra.pattern = "品种|周期|pattern_id"`（`core/hs_monitor.py:213`；必须带品种和周期，`pattern_id` 只有三根 K 线时刻，不同品种会同名）。两档同刻出时由 `tier_dets`（`core/hs_monitor.py:263`）**直接把同形态的常规档那条不发**，列表里自然只剩严格档；库里**已经存在**的同键常规档（典型是抢跑那半截 K 提前出的那条）仍由 `add_alert(supersedes=)` 打上 `superseded`（`core/alert_store.py:85`，**只标记不删**——它可能已推送、还挂着模拟盘单的 `alert_id`），`list_alerts` 默认不返回被顶掉的。`/api/alerts?strict=1` 单看这一档，App 另有独立的「严格」标签页（`mobile/App.tsx` 的 `MODE_QUERY`，与「抢跑」页同一套 mode 机制）；主「告警」页照旧显示全部（含带 `·严格` 的那条）。抢跑通道不参与严格档（`core/hs_fanout.py:83`：半截 K 只出常规档那条提醒）。

模拟盘：`STRICT_LOT_RATIO = 0.5`（`core/paper_trade.py:42`）→ 预算 = 可用资金 × 50% 再开满（`:577`）。同形态时常规档那条不再发射，所以严格档先下这笔半仓，`_open` 的 (品种, 周期) 去重不会再把它挡掉（改造前是常规档先开满、严格档被「已有持仓」跳过）。

`HS_STRICT_SIGNALS=0` 让严格档整体静默（含告警与模拟盘），用于出对照。代价是策略侧每根多跑完整一遍（§2 有实测比值），不是白加的。

### 三重顶 / 三重底通道（2026-11-18）

第三条独立通道，与头肩两条**同根各自记账、两条都发**（用户裁决③）。代码 `strategies/triple_pattern.py`：`TriplePatternStrategy` **继承** `HeadShouldersStrategy` 只覆写 `_scan`，所以点位口径（收盘价量）、拐点确认、尾拐点触发用的是同一份实现，`detail` 形状也与头肩一致——图表、手动开仓、模拟盘三处不用改协议。**触发与头肩同一口径：第三个极值那根收盘确认即出，不等颈线突破、不补推旧形态**（11-18 用户先按图例做过突破触发，同日傍晚拍板改回第三脚当根：「不是突破颈线，是第三个顶确认了就告警」）。开关 `HS_TRIPLE_SIGNALS`（默认开，`core/hs_monitor.py:56`），工厂 `make_triple_strategy`（`:102`）。

**硬条件（全 AND）**

| 项 | 现口径 | 备注 |
|----|--------|------|
| 三个相邻极值 | P1/P2/P3 必须是**连续三个拐点**（`main_pivots[-3:]`），且 P3 落在当根（尾拐点口径） | 图例定义是「三个相邻的低点/高位」。中间夹着别的拐点（台阶走势）直接不算——11-18 画廊里大量错图就是跳点组合出来的。P3 不是当根就返回（旧形态不补推，11-17 铁律） |
| 触发时点 | 第三脚那根收盘确认即告警，**不等颈线突破** | 11-18 用户拍板：「不是突破颈线，是第三个顶确认了就告警」——与头肩 `confirmed_shoulders=False` 的尾拐点口径一致，发现时点不整体后移一根 |
| 三顶等高 | `Dev(P2,P1) ≤ 0.01`、`Dev(P3,P1) ≤ 0.01` 只是粗筛（11-18 由 2.5% 收紧到 1%）；真闸是 `max(P)−min(P) ≤ 0.25 × height`（`triple_head_tolerance_ratio`，与头肩三重头同一把尺） | 11-18 画廊裁决：只按价格卡会把 DCE.j 2h 的 2222→2190→2184（一路走低 1.7%）、尿素 1668/1663/1700（上台阶）都收下。等高关系必须按形态高度算 |
| 颈线水平 | `Dev(S2,S1) ≤ 0.01` + `|S2−S1| ≤ 0.25 × height` | 回调点方向与头肩一致：底取**最高**反弹高点、顶取**最低**回落低点（`lowest=not bottom`）。旧代码写成 `lowest=bottom` 是取反的 bug，把颈线钉在弱反弹上，虚线与图对不上 |
| 区间极值 | `[P1, P3]` 内任何收盘低于 `min(P)`（顶为高于 `max(P)`）超过同一 0.25×height，即拒 | 三个极值必须是整个区间的最低/最高点；区间里还藏着更低的底，就不是三重底 |
| 形态够长 | `idx3 − idx1 + 1 ≥ min_form_bars`，且 ≤ `max_span_bars`(110) | 规格的 `MIN_FORM_CANDLE=10` 是全局值，短周期 10 根只十几分钟，工厂按 `max(10, 该周期 min_span)` 取下限 |
| 量能 | **第三极值那条腿最弱**：顶缩量（`V1>V3` 且 `V2>V3`）、底放量（`V1<V3` 且 `V2<V3`）。三口径满足**任一**即过：① 段内同向根均量（顶取上涨根、底取下跌根）② 三个极值当根单量 ③ 当根单量逐次排（顶 `V1>V2>V3`、底 `V1<V2<V3`）**且**当根 MACD 柱逐次下降 | 11-20 用户重定（#52）。段起点 = P1 之前最近一个反向拐点（不超一个形态长）；均量前先剔 `> 3×` 段均的单根天量（`PULSE_MULTIPLE=3`，本通道默认开；头肩那侧的 `strict_pulse_filter` 仍默认关）。**旧口径「三段递减、顶底同向」已废**：底的镜像这次真翻正了，且段均量不再把砸出颈线的反向放量算进推动量。MACD 为通达信口径 `2×(DIF−DEA)`，12/26/9，`adjust=False` 递归 EMA，只从 P1 往前多取 250 根算 |
| 形态高度三件套 | `height ≥ 10` 价点、`height ÷ 峰值 ≥ 0.008`、三个极值逐个离颈线 `≥ 0.2 × height` | 用户裁决④=复用头肩同款三件套。`height = |(S1+S2)/2 − 极值|`，极值取 `min(P)`（顶 `max(P)`） |

**软标记**：`TimeDev = abs(T1−T2) ÷ max(T1,T2)`（T1=P1→P2、T2=P2→P3 的根数），`TIME_DEV_THRESHOLD=0.5` 只写进 reason（「时间对称 / 时间不对称」）和 `detail.time_dev`，**不拦信号、不改手数**（`ENABLE_TIME_SYM_FILTER=False` → `time_filter=False`）；要当闸用得显式传 `time_filter=True`。

**触发与去重**：信号根 = P3 那根（`main_pivots[-3:]` 里 P3 必须等于当根下标），不等突破、不补推旧形态。多组候选理论上一次只取 `[-3:]` 这一组；`pattern_id = triple-{top|bottom}:P1时刻:P2时刻:P3时刻`，另记 `triple-{kind}-p2:P2时刻`；同一形态只发一次，与头肩的 `top:` / `bottom:` 命名空间不撞，两条通道同一根各发各的。回放 `scripts/replay_triple.py` 走默认 `replay_detail`——信号必在极值根，默认的跳根优化成立。

**资金口径**（裁决①②）：**第三脚收盘即告警并开模拟盘单**。止盈止损**仍用 ±0.8 / ∓0.2 × 形态高度**（形态高度 = `|(S1+S2)/2 − 极值|`，进 `detail.swing`，`replay_winrate.simulate_outcomes` 与手动开仓原样能用）。第三脚入场在颈线外侧，目标用颈线（`_register_pending(..., peak, neck, ...)`），不是规格的「下一根开盘」——那会把发现时点整体后移一根，跟 11-13 起的全套口径打架。量度目标 `颈线 ∓ height` 只进 reason/detail 作参考（`detail.measured`），**不参与判断、不参与止盈止损**。规格的 `max(P1,P2,P3)+1tick` 结构止损**没采用**（盈亏比 1:2.3，20% 平衡线失去意义）。规格的「形态失效」不进平仓条件：出场仍只有止盈、形态止损、浮亏 1000 三条既有闸。

**发射顺序**：串行 `evaluate_df` 走 `tier_dets(严格, 常规) + [三重]`（`core/hs_monitor.py:547`）；分片走 `KeyResult.det_triple`（`core/hs_fanout.py:42`、`:86`），**11-20 起抢跑那半截 K 也喂三重**（走抢跑引擎的独立三重实例）；父进程在 `core/db_monitor.py:256` 把它追加到头肩之后，抢跑路径同样追加在 `_deliver_provisional` 里。日志标签 `三重告警`（`alert_tag`，`core/hs_monitor.py:305`）。三重不带 `extra.strict`，所以只出现在 App「告警」页与「抢跑」页，不进「严格」页；`chart.png` 对三重画 `P1/P2/P3`（底画 `L1/L2/L3`）、标题 `TRIPLE TOP/BOTTOM`（`core/hs_chart.py:96`）；看板形态名取 `detail.pattern_kind`（`core/paper_trade.py:363`），不会再标成头肩。

**为什么头肩补不了这个洞**：`_shape_ok` 要求两肩各离头 `≥ 0.2 × height`，纯等高三重的"头"比"肩"只高 0 点 → 一律被拒（自检第 10 项直接坐实）。所以这条是**新增覆盖面**，不是重复告警。反过来「同根两条都响」只剩一条**窄带**：设两端顶 5000、中间顶 `5000+d`、颈线 4800，三重③等高要 `d ≤ 0.25×(200+d)` ⇒ `d ≤ 66`，头肩要 `d ≥ 0.2×(200+d)` ⇒ `d ≥ 50`，只有 `d ∈ [50, 66]`（约 1%~1.3% 价差）那一段两条通道同时认（自检第 10b 项按 d=60 造，`pattern_id` 不冲突）。11-18 等高闸从「价格 2.5%」收到「0.25×height」之前这条带宽得多——旧用例的 d=100 现在先被③拒。**全市场撞头肩同根的比例因此预计很低，要以 `scripts/replay_triple.py` 的新基线为准。**

**自检与已测数据**：11-18 首次出画廊后被用户整页否决（「没有一个对的」），当日按用户给的标准图例重写形状闸（相邻极值 + 等高按形态高度 + 区间极值），触发先做过突破口径、傍晚拍板**回到第三脚当根**、粗筛收到 1%。那批 `_triple_selftest.py` 112 项是上一轮的本机临时脚本（未入库，现工作区已无此文件），新增的「相邻极值 / 区间极值」闸会改掉部分构造样例，本次未重跑——验证只做了 `py_compile`。本机轻量回放（2 品种 × {30m,1h} × 30 天）只出 1 条三重底是旧口径的数据，作废。新形状闸的全市场条数与胜率还没跑，命令见 §3，出画廊后按「三脚等高 / 信号落在第三脚 / 颈线只在两回调点之间」目视验。

**CPU 代价（本机微基准，随机游走 400 根滑窗、每根重扫一次）**：1m 头肩 9.27 ms + 三重 1.48 ms、5m 13.34 + 1.39、30m 29.98 + 1.50 → 这条通道只加 **5%~16%**，不是严格档那种"策略侧翻倍"。原因：三重的 `Dev(P2,P1)` / `Dev(P3,P1)` 在候选循环最外层就 `continue` 掉绝大多数组合，而头肩的开销在 `_best_break` 那三层 `li/hi/ri` 循环加 strict 主导性判定。

### 监控品种范围（2026-11-19 起剔掉低流动性品种）

名单只有一个来源：`core/cont_symbols.py::list_commodity_mains` = TqSdk 的 CONT 主连 ∩ 商品交易所白名单（`CZCE/DCE/GFEX/INE/SHFE`，不含中金所）**− `ILLIQUID_PRODUCTS`**。ingest 采集、monitor 检测、`kline_audit`、`repair_today_klines`、`monitor_all_mains_hs` 全部走这一个函数，所以改这里等于同时改覆盖面、扫描量和采集量。

**11-19 用户裁决**：截图点名「粳米这类非常不活跃的期货不要放进去观察」。口径给的是**近 90 日 1d 日均成交量 < 5000 手**（同期全市场中位数 16.2 万手），命中 11 个：

| 剔除 | 日均成交量 | 日均持仓量 |
|---|---|---|
| 粳稻 JR、普麦 PM、早籼稻 RI、强麦 WH、动力煤 ZC | **0 手** | 0 |
| 线材 wr | 21 手 | 47 |
| 胶合板 bb | 320 手 | 164 |
| 纤维板 fb | 1280 手 | 8221 |
| 粳米 rr | 2941 手 | 20615 |
| 菜籽 RS | 3727 手 | 9144 |
| 原木 lg | 3950 手 | 12170 |

那五个零成交的品种 K 线照走、水位照前进，但**没有对手盘**，画出来的形态纯是噪声——而它们每个周期都白占一个检测键。

**两个口径别混**（11-19 部署实测）：线上实时名单是 TqSdk 当下返回的商品主连，ingest 启动行 `81 → 70`（序列数 `243 → 210` = 70×3，正好减 11）；本地自检按「库里有 ≥20 根 1d 数据的品种」算是 `87 → 76`。两者差 6 个是**库里有历史、但天勤现在不再返回主连**的品种——它们本来就不在采集与检测范围里，所以看日志核数一律按 **70** 这个口径。

**为什么是硬编码产品码而不是每日按量动态重算**：这批不会突然变活跃（动力煤那类是政策性停牌，不是量小），硬名单可复核、不引入新数据源，也不会某天因为一次缩量把活跃品种踢出去。代价是新增的低流动性品种不会自动被剔。**`db_monitor._symbols` 那条「TqApi 查不到就退回 `list_symbols(conn)`」的兜底路径也套了 `drop_illiquid`**，否则一降级就把这 11 个捞回来。

**大小写按交易所惯例**（CZCE 大写、SHFE/DCE 小写），`_illiquid_selftest.py` 专门钉了一条"串了不生效"，就是为了防止哪天出现同名不同所。相邻易混的品种逐个核过不误伤：菜粕 RM、晚籼稻 LR、玻璃 FG、橡胶 ru、沥青 bu、20 号胶 nr、沪银 ag 全留。

**副作用**：①这 11 个品种**从此不再采 K 线**，库里历史不删；要恢复某个品种，从集合里删那一行、`--build futures-ingest futures-monitor`，还得用 `rebuild_session_klines.py` / `repair_today_klines.py` 把停采期间的历史补回来，否则新根接不上。②`scripts/sync_all_main_klines.py` 用的是 `list_main_continuous`（全量含中金所），**历史回填不受影响**。③回测与审计脚本同样吃这个名单，所以 **11-19 之前那批「全市场 88/130 品种」的数字与新口径不可直接比**，跨这个日期对比要先按同一名单重跑。

### 告警推送通道：为什么现在是飞书（2026-11-18）

**现象与根因**：App 只在活着的时候有通知——通知栏那条是 `mobile/App.tsx` 里 15 秒一轮的 `/api/alerts` 轮询 + 本地 `scheduleNotificationAsync` 发出来的，**不是远程推送**。进程被系统杀掉（国产 ROM 很积极）就没有轮询，也就没有通知。远程推送这条路一直是死的：`mobile/android/app/google-services.json` 不存在、gradle 也没挂 google-services 插件 → `getExpoPushTokenAsync` 每次必抛 → `/api/devices/register` 从没被调用 → `data/alerts.json` 的 `devices` 恒空 → 服务端 `send_alert_push` 恒回 `{"sent": 0, "detail": "no_devices"}`（`reports/monitor.log` 里每行 `App推送结果:` 都能看到）。App 里那句「通知已开启」是 `setupPush` 的 catch 分支在**掩盖**这个异常，已改成实话（「通知仅在 App 存活时推送，彻底关闭 App 后收不到」）。

**为什么不接厂商通道**：小米/vivo/OPPO 的推送要求**企业开发者账号 + 应用已上架**（极光官方口径；华为个人账号可、魅族/FCM 个人可），而上架本身还要软著 + ICP + 工信部 APP 备案 + 隐私政策页，个人主体短期走不通。所以 11-18 用户裁决：**改走飞书群机器人 webhook**，亮屏、横幅、响铃交给飞书 App 自己的保活。

**超严格（11-21 加，独立通道，默认关）**：`HS_SUPER_STRICT=1` 才启用；触发仍是右肩收盘当根，在严格档候选上再套用户六条（当根可判 1/2/3/4/6，规则 5 需要右肩后 3 根、只能后验，不挡发射）。**只告警、飞书照推、不开模拟盘**（`deliver(trade=False)`），kind 后缀 `·超严格`，`extra.super_strict=true`；App 的「严格」页按 `extra.strict` 取，不会混进来，主告警页能看到。飞书关键词 `严格` 是 `超严格` 的子串，已覆盖。A/B 脚本 `scripts/super_strict_ab.py`；开之前先跑 A/B，别直接写 `.env`。

**推送范围（11-19、11-21 各扩过一次）**：11-18 首版**只发严格档**；11-19 用户加了一条「有三重形态的也上飞书」；11-21 又加「抢跑的告警也加入到飞书」。现口径是 **严格档 + 三重顶/底 + 抢跑（三个档位在抢跑窗口里的半截 K 提醒）**都进飞书，只有收线后的常规档仍只进 App 列表。判定用 `emit_alert` 的 `strict` / `triple` / `provisional` 三个显式标记，不是猜 kind 字符串——三重那条在 `detect_triple` 里打 `triple=True`，**不带** `·严格` 后缀也不写 `extra.strict`，所以 App 的「严格」标签页不会混进三重。⚠️ 三重的量级和严格档不是一个数量级：旧口径全市场 60 天三重 **8205 条**（1m 4841 / 5m 2718 / 30m 506 / 1h 140）对同期严格档 1227 条，新口径（1% 粗筛 +  textbook 图形闸）收紧后还没重跑（任务 #24）。限流本身打不到（100 次/分钟），**真上线后如果 1m/5m 的三重把群刷满**，收窄办法是给飞书加周期门槛（`emit_alert` 那个 `if strict or triple` 上 and 一个 `duration_sec >= 1800`），一行。

**现口径**：代码 `core/feishu_notify.py`，接线在 `core/hs_monitor.py` 那个已有的 `_push` 后台线程里、`strict or triple or provisional` 为真时追加一发，失败只打 `飞书推送结果/失败` 日志，不影响告警落库与模拟盘。签名按官方口径 `base64(HmacSHA256(key="{timestamp}\n{secret}", msg=b""))`——**key 和 msg 的位置与常规用法相反**，写反了服务端报字段校验失败，自检里专门钉了这条。**11-20 22:00 实测撞限流**：OI 1h、RM 1h、m 1h 同一秒连发，飞书回 `code 11232 frequency limited`，后几张被拒且手机无任何提示。**11-21 修法**：发送全局串行 + 最小间隔 0.6s，撞 11232 退避重试两次（1s、2s），三条同批的严格档现在会依次送达（日志仍是每张一行 `飞书推送结果`）。**11-21 加了墙钟与卡片身份**：`飞书推送结果 11-21 22:30:12 头肩底·抢跑 KQ.m@INE.sc 2026-11-21T22:00:00: {...}`——监控日志没有逐行时间，之前 13 条 `11232` 只能靠行号在两版镜像之间猜，现在可以直接对时刻。

**配置**（全部 `.env`，缺 `FEISHU_WEBHOOK_URL` 整条静默跳过、回 `no_webhook`）：`FEISHU_WEBHOOK_URL` 机器人地址、`FEISHU_WEBHOOK_SECRET` 只在机器人开了「签名校验」时填、`HS_PUBLIC_BASE` 填手机能访问到的服务地址（如 `http://<公网IP>:8000`）就在卡片上加「看形态图」按钮指向 `/api/alerts/{id}/chart.png`、`HS_FEISHU_NOTIFY=0` 关掉。⚠️ 这几个值在**发送时**现读，不在 import 时读：本模块可能比 `config.settings` 的 `load_dotenv()` 更早被 import，模块级求值会把地址读成空、整条通道静默失效。

**卡片**：红头标题「品种 周期 头肩底·严格」（kind 已带「·严格」，标题不重复；三重则是「品种 周期 三重底」这种原 kind，抢跑会把 kind 变成「头肩底·抢跑」「三重底·抢跑」「头肩底·抢跑·严格」），正文 = 加粗 reason + `K线 <bar_time>` + 有 `detail.measured`（只有三重通道带）时补「量度目标 X · 形态高度 Y」、否则只补「形态高度 Y」；底部 note 是 品种 · 周期 · `detail.pattern_kind`。自检 `_feishu_selftest.py` **22 项**（签名向量与四种写反/错分隔符、五种卡片排布、`disabled`/`no_webhook`/非 0 code/网络异常四个发送分支、以及 `emit_alert` 接线：常规档不碰飞书、严格档两路按序、**三重两路按序**、**常规档抢跑不进飞书**、`push=False` 全静默、Expo 抛异常飞书照发、飞书自己抛异常不冒到行情循环）。**11-21 口径**：判定条件变成 `strict or triple or provisional`，也就是**抢跑窗口里三个档位全部推飞书**，同一形态会有「抢跑」+「收线」两张卡（两个引擎各记各的账，属设计如此）。要退回只推严格/三重，把分支改回 `strict or triple`；要留抢跑但不要收线重复，改 `not provisional` 会把抢跑也一起关掉——没有现成开关，别想当然。

**送达后的手机侧边界（11-18 小米机实测）**：webhook 机器人发出去的是**普通群消息**，拿不到 full-screen intent，所以「亮屏」完全由系统全局开关（设置搜「亮屏」→「锁屏来通知时亮屏」，且**息屏显示 AOD 开着时通知只画在 AOD 小块里、不点亮整屏**）和该 App 通知渠道的三项权限（允许通知 / 锁屏通知 / 悬浮通知）决定。已实测到的一条坑：**MIUI 通知归组后只有第一条会亮屏**，连着发第二条不亮——划走旧通知再单发就亮（当时误判成"通道坏了"，别重复排查）。这条对真实使用有影响的场景是**同一批收线时刻多品种同时出严格档**（会话 K 线对齐，30m/1h 那几档会挤在同一分钟），此时只有第一条亮屏；要不要把一轮内的多条**聚合成一张卡片**再发（代价是推送要攒一个几秒的小窗口，落库与模拟盘不受影响）**待用户拍板**，现口径仍是一条一卡片。

### 告警页手动开仓（2026-11-18）

App 每条告警展开后有一块「手动开仓（模拟盘）」：价位自己填、**手数自己填**（1~8，初值 1），**买入 = long / 卖出 = short**。它是**模拟盘**单，不是真盘下单——本仓库没有任何实盘下单通道。

- 止盈止损**沿用老规则**：距离仍取这条告警的形态高度 `detail.swing` 的 0.8 / 0.2，只是起点换成用户填的价位。swing/extreme/head 全部由服务端从告警 `extra.detail` 读，App 传不进任何金额口径。
- 价位按最小变动价位取整后成交；`paper_trade.price_tick` 是这次新加的列（`ensure_schema` 自动 `ALTER`，老行是 NULL 就按原样不取整，等新自动单跑过就有值）。
- 合约数字（乘数 / 保证金比例 / 手续费）来自**同品种最近一笔模拟单**里引擎自己算过的那份（`_spec_from_history`）。原因：API 进程没有天勤连接，为一次点击开一条会多占一份配额。所以从没跑过自动单的品种会直接被拒（"取不到保证金和手续费标准"）。
- 手数由 App 传（`ManualOpenBody.lots`，`ge=1 le=8`），服务端 `manual_open` 再 `max(1, min(lots, MAX_LOTS))` 兜一层；保证金、手续费、扣本金全部 `× lots`，资金校验是 `(每手保证金 + 每手手续费) × 手数`。**App 侧先按 1~8 拦一道中文提示**（`MAX_MANUAL_LOTS`，与服务端同值 8）——不拦就会撞到 pydantic 的 422，那个 detail 是结构化列表、不是中文句子。
- 拒单口径四条：该品种该周期**已有持仓**、价位**偏离最新入库价 > 5%**（`MANUAL_PRICE_DEV`，防手滑）、**可用资金不够这几手**（可用 = 本金 + min(0, 浮动亏损) − 已占保证金，与自动单同一式）、抢跑提醒**不开仓**（那根 K 线还没收线）。
- 写库后由监控进程接手：`check_loss_caps` 每 ≤10s 会 `_sync_from_db()` 把库里的持仓与本金读回内存，于是这笔手动单照样吃止盈、止损和浮亏 1000 实时止损。**这与看板手动平仓是同一套「API 直接写库 + 监控同步」机制**，代价同样是那 ≤10s 窗口内两处写本金可能互相覆盖（现状已存在，未新增风险面）。
- **反向影响要记住**：手动单占了 (品种, 周期) 这个坑，同一周期随后的自动信号会被「该周期已有持仓」挡掉，直到这笔平掉；本金也被它的保证金吃掉。这不是 bug，是同一条去重口径的必然结果。
- 自检：`_manual_open_selftest.py`（23 项：取整、TP/SL、手数封顶、保证金/手续费、四类拒单、失败回滚）与 `_manual_api_selftest.py`（10 项：从告警取参、抢跑拒单、404、周期标签回退、缺 swing 拒单）全过，均为假游标，不连库。

## 2. 反复踩过的坑

- **去重键用窗口相对下标 → 同一个头反复告警**（SH 30m 曾一个头出 4 条）。
- **回放预过滤必须跟触发口径一致**：`scripts/audit_killed_samples.py:42` 现在按 `strat.confirmed_shoulders` 分流——当根口径看 `i` 是否盖过前 `order` 根，慢口径看 `i - order` 是否两侧极值。改触发规则时这里必须跟着改，否则回测报 0 条。
- **当根告警的代价**：尾部候选只要盖过前 `pivot_order` 根就算右肩，而 11-13 实测「仅凭单边就成立」的候选里 57.1%（低点侧 70.1%）会在 `pivot_order` 根内被更极端的同向拐点盖掉——这部分告警属于抢跑后被证伪，只能靠胜率数据衡量。
- **严格档必须按周期分组读**（§1 实测）：≥1h 严格档 19.1% / −0.009，比常规档低 4.3pp（p=0.029）；≤30m 两档没差别（p=0.37）。把两档或全周期混在一起算，5m 那 1563 笔会把长周期的负期望稀释掉——这正是「合计 23.2% 看着比 22.1% 好」的假象来源。
- **loose / strict 是两次独立回放，改后不是改前的严格子集**：`滤掉 N 条` 是时间戳集合差，`strict − (loose ∩ strict)` 那部分是只在改后侧新出现的。别把「改后 ≤ 改前」当恒等式读。
- **平台头豁免容差不能太宽**：0.25×头高时 lu 上涨段有 ≥4 根收盘落在带内被误判成平台头，对称闸 2.5 直接形同虚设（台账 #31）。
- **严格档 ≠ 常规档的子集，两档条数只能按集合差读**：闸 1 的 `Dev ≤ 2.5%`（按左肩价）比常规档的 `1.5%×颈线` 宽，会放进常规档拒掉的形态；闸 2/3 又拒掉一批常规档收下的。**全市场实测**（2026-11-18，老机 2C2G，`scripts/replay_strict_tier.py --days 60 --workers 2`，88 主连 × 6 周期 = 528 作业，**墙钟 4.3 小时**）：

  | 周期 | 常规档 | 严格档 | 占常规档 | 同形态让位 | 严格档独占 | 独占/严格 |
  |------|--------|--------|----------|------------|------------|-----------|
  | 1m | 1524 | 471 | 30.9% | 352 | 119 | 25% |
  | 5m | 1576 | 568 | 36.0% | 330 | 238 | 42% |
  | 30m | 352 | 135 | 38.4% | 57 | 78 | 58% |
  | 1h | 146 | 53 | 36.3% | 23 | 30 | 57% |
  | **小计（1m~1h）** | **3598** | **1227** | **34.1%** | **762** | **465** | **38%** |
  | 2h / 1d | — | — | — | — | — | 60 天窗根数不足，看下面 300 天那张 |

  **300 天窗**（`--days 300 --tfs 7200,86400`，88 主连 × 2 周期 = 176 作业，**墙钟约 15 分钟**；根数少，成本比 1m 低两个数量级）：

  | 周期 | 常规档 | 严格档 | 占常规档 | 同形态让位 | 严格档独占 | 独占/严格 |
  |------|--------|--------|----------|------------|------------|-----------|
  | 2h | 1046 | 470 | 44.9% | 230 | 240 | 51% |
  | 1d | 364 | 156 | 42.9% | 67 | 89 | 57% |
  | **小计** | **1410** | **626** | **44.4%** | **297** | **329** | **53%** |

  → 两张表**窗口不同，不能相加**（60 天 vs 300 天）。60 天那张的入库净条数 `3598 − 762 + 1227 = 4063`（比只有常规档多 **12.9%**），其中 30.2% 带 `·严格`；按约 37 个交易日算，全市场 ≈ 常规档 1.1 条/品种/日、严格档 0.38 条/品种/日。**周期越长，严格档越不是常规档的子集**（独占占比 1m 25% → 5m 42% → 30m/1h 57% → 2h 51% → 1d 57%；严格档占比也从 31% 一路涨到 44%）：低频段那批被旧对称闸拒掉的宽肩差形态，正是新 Dev 式放进来的。同一批数据上 `on_bar` 改前/改后/两档同实例三跑 `(datetime, reason)` **逐条一致**（3 品种 × 2 周期子样本 113 条，等价性守住）。
- **2h/1d 在 60 天窗里出不了数**：`_min_bars` = `max_span + 2×pivot_order + 5`，2h/1d 要 121/119 根，而 60 天只有 58/28 根 ⇒ **88 个品种整片被跳过，0 不是没信号**。已按 `--days 300` 补齐（见上面那张 300 天表），1h 也有 9 个品种被跳（中金所只有日盘），30m 跳 1 个（SO 只有 20 根）；300 天窗里 SO 的 2h/1d 仍只有 7/4 根，照旧跳过——**个别品种缺长周期是数据本身，不是 bug**。
- **严格档每根都跑完整一遍，策略侧成本 = 两遍**（11-17 去掉延迟确认之后就是这样，延迟版那层 O(pivot_order) 预筛没了）：本机实测（`_strict_cost.py`，30 天真实库逐根滚动，同一份窗口分别调 `on_bar` / `on_bar_strict`）eg 5m **6.40 → +6.43 ms**、jd 1m 5.51 → +5.52、PF 30m 3.27 → +3.50，即严格档那一跑与常规档几乎等价，**策略侧翻倍**。绝对值别直接搬：本机比 11-16 服务器口径（1m 2.7 / 5m 2.5 / 30m 3.6 ms）慢一截，用比值。按服务器那份结构推（130 键）：策略侧 `0.35s → ~0.7s`，取数侧 4.2s 的库往返不变 → 单轮增量 <0.4s，占现分片稳态 3.74s 的 ~10%；但这是 CPU 不是等待，3 片仍要同时占 3 核。**待线上复核**：`reports/monitor.log` 的 `本轮扫描 ... 用时` 与 `docker stats`。
- **加载窗不够会静默跳过周期**：`run_backtest.py:45` 的 `LOAD_DAYS` = 1m/5m 60 天、30m 90、1h 120、2h 150、1d 300。60 天窗里 SH 的 2h 只有约 107 根，低于阈值就不出数；中金所品种只有日盘，1h 也会缺。看到某周期整列缺失先按这个算，不是 bug。
- **夜盘的会话周期到 01:00 / 02:30（11-20 修，台账 #56）**：`utils/trading_session.py` 的夜盘段原来只定义 21:00-23:00，于是有色（CU AL ZN PB NI SN SS BC，夜盘到 01:00）与贵金属·原油（AU AG SC，到 02:30）**后半夜的成交在 30m/1h/2h/3h/4h 里一根都没有**（1m/5m 一直有，所以洞只在会话周期上）。现在夜盘段 = 当晚 21:00-23:59 + 次日凌晨 00:00-02:29，并且**一夜=一组**：合成时把「当晚的分钟 + 次日凌晨的分钟」放进同一个池子（`core/session_rebuild.py:aggregate_symbol_session`、`core/session_klines.py:_sync_day_bars`）——按自然日分组的话，跨零点那段会留在次日那一组里没人认领（次日的夜盘筛选只认 21:00 之后），那半小时到三个半小时就彻底消失。
  落根：30m 每半小时一根（23:30、次日 00:00/00:30/01:00，贵金属原油再到 01:30/02:00/02:30）、1h 整点一根、2h 两根（23:00、01:00）+ 贵金属原油第三根 02:30（90 分钟）。**3h/4h 不排固定钟点**：整夜按 180/240 分钟顺序切段、末段不足也算一根、收线时刻由实际分钟线推——120 分钟的短夜盘结果与改前**逐字节一致**，只有长夜盘才多落根（固定钟点要么给短夜盘贴错标签，要么把后半夜并进次日日盘）。
  标签日期：凌晨那几根标注在**次日**（`2026-11-20 00:00` 是 11-19 那一夜的 K），跨零点的 2h 根 `datetime_start` 仍在当晚 23:00。
  实盘侧靠 `get_all_session_trigger_minutes()` 新增的 23:29 / 23:59 / 00:29 / 00:59 / 01:29 / 01:59 / 02:29 触发刷新；`session_bar_open`（`core/realtime_pipeline.py:115`）的半截闸门自动生效——标注时刻没越过墙钟就不当真 K。审计夜盘窗本来就是 前一日 20:55~当日 02:40，覆盖得到，不用改。
  **影响与待办**：这 11 个品种的夜盘会话根数从每晚 4 根（30m）涨到最多 11 根；历史要用 `scripts/rebuild_session_klines.py --symbols … --execute` 重刷，**重刷后吃过这 11 个品种会话周期的回测基线全部要重跑**（#52 那两趟包含它们）；生产侧要等非交易时段 `--build` 部署才生效。
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
- **这两个脚本现在都出画廊**（11-18 加）：严格档落 `reports/backtest_strict/` → `/backtest/strict`，三重落 `reports/backtest_triple/` → `/backtest/triple`。卡片标题带归属（`[严格档独占]` / `[同形态让位]`、`[三重独占]` / `[同根有头肩]`），要判断这两条通道值不值得留，看独占那批画出来是什么东西，光看计数看不出。渲染统一在主进程做（不是 worker），因为要按时间全局截断：`--gallery-max` 默认 800 张（base64 单文件约 30MB，再多浏览器打不开），超了只丢最早的并在副标题点名；`--no-gallery` 退回纯数字。画图逻辑与 `run_backtest.py` 共用 `build_hits_gallery.render_hits`，别再各抄一份。
  ⚠️ **画廊目录是硬编码的，`replay_triple.py` 和 `replay_strict_tier.py` 都没有 `--outdir`**（参数只有 `--days/--symbols/--tfs/--workers/--samples/--gallery-max/--no-gallery`），而且每次跑先 `shutil.rmtree` 那个目录 ⇒ **同一个脚本的两趟（如 `triple_tier` 60 天全周期 与 `triple_long` 300 天 2h/1d）画廊必然互相覆盖，后跑的删掉先跑的**。只有数字能并存（log 是 `>>` 追加）。所以：要么先把短窗那趟看完再跑长窗，要么跑完立刻 `cp reports/backtest_triple/gallery_single.html reports/triple_60d_gallery.html` 留档（留的那份路由不认，只能自己开文件）。

三重顶/底计数与胜率（`scripts/replay_triple.py`：同一段历史逐根滚动，三重与常规档头肩各跑一遍，输出每周期「三重 N 条（撞头肩同根 M）」+ 胜率行 + 每周期最近几条样例）。`--days` 短了会整片 0，根数不足一样点名：

```bash
docker run -d --rm --name triple_tier --env-file .env -v $PWD/reports:/app/reports -v $PWD/data:/app/data futures-quant-api sh -c "python -u scripts/replay_triple.py --days 60 --workers 2 >> /app/reports/triple_tier.log 2>&1"
```

- `撞头肩同根` = 那笔形态头肩本来也报（两条通道同刻各发一条，模拟盘只有一个坑，先到的头肩拿到开仓权）；没撞上的才是净新增覆盖面，判断"要不要留这条"就盯这个数。
- 对照：`-e HS_TRIPLE_SIGNALS=0` 只关掉生产侧发射，**回测脚本不读这个开关**（`make_triple_strategy` 直接建实例），要出对照就 `--tfs` 逐周期比，或改跑 `replay_strict_tier.py` 的常规档列。
- `-e HS_TRIPLE_TREND=1` 打开**趋势方向闸**（默认关）：顶要求 P1 收盘高于等长前一段（`P3−P1` 根之前）、底要求低于，用来拦「暴跌/暴涨后箱体里那组等高反弹/回调」——它们其实是相反方向的颈线，不是三重反转。开关在 `make_triple_strategy` 里读，回测加 `-e` 即可，实盘未开。**11-21 A/B 已否决**：300d 2h/1d 从 47 条 21.3% 掉到 31 条 16.1%（−0.039），拦掉 16 条里 5 条是赢的；保留代码但别再开。
- `-e HS_TRIPLE_EXTREME=1` 打开**区间位置闸**（默认关）：顶的三个极值必须是回看 `max_span_bars` 根内的最高、底必须是最低（容差仍 `0.25×height`）。比趋势闸直接——箱体离前高/前低太远就直接拒，不靠单点收盘猜方向。
- `--symbols` 限定品种、`--tfs` 限定周期（**这里是秒不是标签**，`--tfs 1800,3600`）；300 天窗口（2h/1d）单独一趟，别和 60 天混在同一个 `--days` 里读：

```bash
docker run -d --rm --name triple_long --env-file .env -v $PWD/reports:/app/reports -v $PWD/data:/app/data futures-quant-api sh -c "python -u scripts/replay_triple.py --days 300 --tfs 7200,86400 --workers 2 >> /app/reports/triple_long.log 2>&1"
```

进度看 `tail -n 30 reports/triple_tier.log`（逐键打「进度 N/M 已用 Xs」，没有 `[tag]` 前缀可 grep）；结果在末尾三段：逐周期计数 → 合计 → 每周期最近样例。

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
- 手动开仓（§1）只需要 `futures-api`（接口与 `manual_open`）和 `futures-monitor`（同步接手这笔）两个镜像新；`paper_trade.price_tick` 那列由 `ensure_schema` 在第一次调用时自己 `ALTER`，**不用手工 DDL**。核一下：`docker compose exec futures-api grep -c "def manual_open" /app/core/paper_trade.py` 期望 `1`。
- 三重通道（§1）落在 `futures-monitor`（检测、发射、模拟盘）和 `futures-api`（`chart.png` 画 P1/P2/P3）两个镜像里，`docker compose up -d --build futures-api futures-monitor` 就够，ingest 不涉及。上线后核两件事：`docker compose exec futures-monitor grep -c "def detect_triple" /app/core/hs_monitor.py` 期望 `1`；日志里应出现 `[monitor] 三重告警 …` 这种行（没有不代表坏，代表还没遇到形态）。
- **改品种名单（§1 监控品种范围）必须重建 `futures-ingest`**，不只是 monitor：`list_commodity_mains` 同时喂采集（`core/realtime_pipeline.py:304`）和检测（`core/db_monitor.py:131`），api 侧没调用。所以 11-19 这次要 `docker compose up -d --build futures-api futures-ingest futures-monitor`（把之前"ingest 不涉及"那句忘掉，那是三重通道那次的情况）。核两件事：`docker compose exec futures-monitor grep -c "ILLIQUID_PRODUCTS" /app/core/cont_symbols.py` 期望 `2`（定义 + 过滤各一行）；启动后 `grep 品种 reports/ingest.log | tail -3` 看订阅数量，应从 `81` 掉到 **`70`**（掉 11 个才对，没掉就是镜像没新；**11-19 部署实测就是这样**，最后一行 `[pipeline] 品种 70，先订 1m,5m,1d （210 序列）`）。**别在交易时段重启 ingest**——重启窗口里的 1m 根会缺，得回头补历史。
- **单独 build 某服务时只能写 `futures-api`（11-20 踩坑）**：compose 里只有 `futures-api` 有 `build: .`，`futures-ingest`/`futures-monitor`/`futures-backtest` 都只写 `image: futures-quant-api:latest` 共用同一个镜像，所以 `docker compose build futures-ingest` 是**空操作**、镜像一点没变，而 `up -d --build <多服务>` 会顺带触发那唯一一次 build，所以这个坑只在"想只更新镜像、不重启在线容器"时才会踩到。一次性补数容器（`compose run --rm --no-deps futures-ingest ...`）取的正是新 tag，因此**补数可以在重启在线容器之前做**。闸要用 ASCII 标记核，别用中文 grep 参数（宿主机传参编码可能不匹配）：`docker compose run --rm --no-deps futures-ingest grep -c bounded /app/core/session_rebuild.py` 期望 `2`（旧代码 `0`）。
- 飞书推送（§1，**严格档 + 三重 + 抢跑**）只在 `futures-monitor` 一个进程里发射，`docker compose up -d --build futures-api futures-monitor` 就带上了（api 侧没改动，带上是为了同批镜像）。**机器人地址要写进生产机的 `.env`**，改 `.env` 本身不需要 `--build`，但 `docker compose up -d futures-monitor` 得跑一次才会把新变量注入容器。核三件事：
  `docker compose exec futures-monitor grep -c "def send_alert_feishu" /app/core/feishu_notify.py` 期望 `1`；
  `docker compose exec futures-monitor grep -c "if strict or triple or provisional" /app/core/hs_monitor.py` 期望 `1`（`1` = 三重与抢跑都已进飞书；回 `0` 说明镜像还是 11-20 那版）；
  `docker compose exec futures-monitor printenv FEISHU_WEBHOOK_URL` 应有值（只看有没有，别把值贴进聊天记录）；
  超严格（默认关）要开时：`.env` 加 `HS_SUPER_STRICT=1` 再 `up -d futures-monitor`（改开关不用 `--build`；代码要 `--build`），核 `docker compose exec futures-monitor grep -c "def detect_super" /app/core/hs_monitor.py` 期望 `1`、`grep -c "if strict or triple or provisional or super_strict" /app/core/hs_monitor.py` 期望 `1`；日志出现 `[monitor] 超严格告警 …` 或 reason 里 `超严格头肩底右肩确认` 才算接到；
  群里该收到卡片了就手动造一条（**会在群里发一条测试卡片**）：
  `docker compose exec futures-monitor python -c "from core.alert_store import Alert; from core.feishu_notify import send_alert_feishu; print(send_alert_feishu(Alert(id='t1', symbol='KQ.m@CZCE.PF', kind='头肩底·严格', reason='[短纤 · 30分钟] 推送链路自测', bar_time='2026-11-18T14:30:00', created_at='2026-11-18 14:30:01', extra={'name': '短纤', 'duration': '30分钟', 'strict': True, 'detail': {'swing': 17.0}})))"`
  回 `{'sent': 1}` 即通（**11-18 已实测通过**：手机飞书收到卡片）；回 `no_webhook` = 变量没进容器；回 `{'sent': 0, 'code': 11232...}` 之类 = 地址对但被拒，最常见是机器人「安全设置」勾了**自定义关键词**而消息没命中——**11-19 起三重也上飞书，只勾 `严格` 这一个关键词会把三重卡片全拒掉**（三重 kind 是「三重顶/三重底」，整张卡没有「严格」二字）。**11-21 起抢跑也推，关键词还要加 `抢跑`**（常规抢跑卡是「头肩底·抢跑」，整卡没有「严格」「三重」字样）。所以：首选勾**签名校验**并把 `FEISHU_WEBHOOK_SECRET` 填进 `.env`；用自定义关键词的话必须加到三个（飞书是"命中任一即放行"），即 `严格` + `三重` + `抢跑`。另外注意手动自测那条用的是严格档文案，**它通不代表三重/抢跑也通**，按上面这条核过关键词才算完。之后真告警看 `grep 飞书 reports/monitor.log | tail`。
- 验证真进容器：`docker compose exec futures-monitor grep -c <新函数名> /app/core/paper_trade.py`（`grep -c` 数匹配行数，只出现一次时期望值就是 `1`）。
- `docker compose logs futures-monitor`（和 ingest）**永远是空的，不是故障**——stdout 被重定向进 `reports/*.log`，用 `tail -n 40 reports/monitor.log`。
- 分片并发数走 `HS_WORKERS`（`.env` 或 compose `environment:`），改这个**只需 `docker compose up -d futures-monitor` 重建容器，不用 `--build`**；设成 1 等于关掉分片。生产是 4 核、ingest 常态吃掉 1 核，3 是当前的折中。
- 分片上线后的日志标记（11-16 11:48 部署、13:30 开盘实测）：`[monitor] 检测分片已启动 workers=3` → 启动行 `poll=0.5s workers=3（只读库，不订 K 线）` → 开盘后第一轮 `水位预热 … loaded=… in …s keys=… sum=…s max=…`，之后每次有键要算都打 `本轮扫描 pending=… keys=… sum=… 用时 …s`。`docker top futures-quant-monitor | grep -c spawn_main` 期望 3。**免责条款每次启动多打 3 行不是重启循环**，是 3 个 spawn 子进程各自 import tqsdk。午休/收盘后只有 `休市 …, 15s 后再检查`，此时看不到扫描行属正常。
  扫描行原本挂了 `scan_sec >= 1.0` 阈值，分片后单轮普遍 <1s 就**一条都不打**（13:30 那批就是这样，只能反推「没打=都很快」）；已改成每轮必打，并把 `pending=` 一起带上区分「算了几键」和「几键拿到了数据」。
- 画廊路由（只有生产机有 HTTP）：`/backtest`、`/backtest/killed`、`/backtest/pivot`、`/backtest/prom`、`/backtest/neck`、`/backtest/strict`（严格档）、`/backtest/triple`（三重顶底）。路由只是读 `reports/<变体>/gallery_*.html`，缺文件就 404 且 detail 写明缺哪个；在别的机器跑出来的要先 `scp -r` 到生产机 `/opt/futures_quant/reports/`，**到位后不用重启任何容器**。
- 生产 sshd 只认密钥（`PasswordAuthentication no`，报 `Permission denied (publickey...)` 且不问密码不是输错）。老机→生产机已互信。
- 本地打包若 gradle 启动就炸 `Cannot find JAR 'kotlin-compiler-embeddable-2.0.21.jar' required by module 'gradle-declarative-dsl-core'`：是 `~/.gradle/wrapper/dists/gradle-8.13-bin/<hash>/` 那份解压缺了这一个 jar（同一 URL 的两个 hash 目录里，另一份是完整的）。把缺的 jar 从完整那份 `cp` 过去就好，**不用重下 130MB**（11-18 实测：`ap7pdhv…` 缺 / `5xuhj0r…` 完整，补一个文件后 `assembleRelease` 正常出包）。

## 5. 未闭环

- **飞书推送（§1）严格档已上线（11-18 实测 `{'sent': 1}` + 手机收到卡片）；11-19 把三重也加了进来；11-20 夜盘已随 `808fc9a` 部署（api+monitor 重建，容器内标记 2/0/1 核过，ingest 未动）**。部署时一次带上了三件事：三重新口径（`17c1e4f` + `9a0fd8b`）、三重进飞书、严格档/三重进抢跑。部署后还剩两件要盯：①**确认机器人安全设置不是只有 `严格` 一个关键词**，否则三重卡片全被拒（飞书拒收只打在 `飞书推送结果:` 那行日志里，手机上没有任何提示，容易以为"没出形态"）；②等第一条真三重/抢跑档，`grep -E "三重告警|抢跑告警|飞书" reports/monitor.log | tail`。还差两件旧账：`mobile/App.tsx` 的推送状态文案要下次出 APK 才生效；Expo/FCM 那条远程推送通道保持"永久不通"（小米机要 FCM 本身也不可靠，台账 #46 记了理由，别再当 bug 排查）。**11-20 起抢跑里的严格档/三重也进飞书**（见 §1 抢跑段），仍只有 App 活着时才有通知的只剩「常规档」和「常规档抢跑」两类。
- **三重顶/底（§1 新通道）还剩两件**：①全市场条数、撞头肩同根率、逐周期胜率还没跑（命令在 §3，老机 2 核两片，别在生产机工作时段跑）；②生产镜像**已于 11-20 夜盘随 `808fc9a` 部署**（api+monitor 重建，ingest 未动）；③App 头部副标题已改成「头肩 / 三重形态 · 实时通知」，但 RN 的 JS 打进 APK，**这行文案要下次出包才生效**——不影响功能（三重告警本身是数据驱动，旧包照收照显示）。
- ~~**三重在 5m 上可能太密**~~：这条是旧口径（第三脚当根、价格 2.5% 等高、无突破、无相邻约束）测出来的 133 条，已作废——11-18 用户整页否决后按图例重写（台账 #47）。新口径条数会大幅下降，等生产上重跑 `/backtest/triple` 再看密度与胜率。
- **主连换月会污染跨换月的形态（11-20 实证，未裁决）**：`KQ.m@` 主连跟的是当下最活跃合约，换月那天门后的价格直接跳到另一只合约上，**形态窗口里的几个极值就成了两个合约的比价**。沪银实证：11-09 起主连才从旧月切到 `ag2702`，两者价差稳定 33~38 点，把同一组三顶的 `Dev(P3,P1)` 从同合约的 **1.002%（超 1% 阈、拒）** 压成混合口径的 **0.740%（阈内、过）**——判据结论被换月翻转。三重窗口最长 110 根 2h ≈ 37 个交易日，头肩同理，**凡是历史样本里跨过换月日的形态，等高数字都不可直接信**。是否加「跨换月闸」（窗口内出现持仓量跳变即不出信号）= 台账 #51 待决策 E，等用户拍板，**现口径未动**。另外：单月合约数据库里只有 `SHFE.ag2702` 一只，且 1m 断在 11-07 02:29，按单合约复核历史形态要先补数。
- 台账里的待决策：#10 左肩≈颈线是否另加闸；#12/#13 是否单跑「仅 earliest-R」；5m 单周期细化（sym/conf 两轮里 5m 都是唯一改善的周期，但合计仍为负）。
- **合计仍贴着 20% 平衡线**（最近一轮 20.8%/+0.008），5m 与 1d 是稳定拖累——见台账 32 条。
- **数据修复（11-20 二次复核，含根因）：会话周期 30m/1h/2h/3h/4h 自 11-06 起大面积缺失**。全市场逐日 `COUNT(DISTINCT symbol)` + 落库时刻逐根（只读，`_session_gap_check.py`）：11-02～11-05 六个周期基本齐（87，仅 1d 在 11-05 已先掉到 79）；**缺的交易日是 11-06（只剩 8 个品种）、11-09、11-10、11-12、11-13、11-16、11-17（只剩 11 个），只有 11-11 全周期正常（79），11-18 起恢复满覆盖**；同期 1m/5m 一直正常（79～87）。**11-07/08/14/15 是周六/周日，不是缺失**（11-07 那 13 个是周五夜盘拖到周六凌晨 02:29 的品种），第一版把它们算进空洞、又把范围截到 11-14，两处都已更正。落库是北京时（30m 落在 09:30…15:00、2h 落在 11:15/15:00/23:00），不是时区假象。后果是**那 7 个交易日 30m/1h/2h 的实时告警等于没跑**，且任何吃这段历史的回测（含 11-20 凌晨那趟 60 天）条数都偏低、不可与新名单基线混用。
  **根因（11-20 定位并已修）**：`grep -i session reports/ingest.log` 只命中英文摘要 `session_ok=… session_bad=0`，看着天天 PASS——但 `session_audit_window`（`core/kline_audit.py:88`）把校验窗钉死在"刚结束那一段时段"（day 08:55~15:05 / morning 08:55~11:35 / night 前一日 20:55~当日 02:40，最长 7 小时），**窗口只往前走、永不回看历史**，对已成型的空洞天生失明；且 `session_ok` 把"无差异"与"已修复"合并计数（`:450`/`:456` 都 +1），摘要里看不出写了多少。真正制造空洞的是修复路径自身的不对称：`_fix_session` → `rebuild_symbol_session_klines(execute=True)` **删除带 = `[start-1d, end+1d]` 并立即 commit，写回却只有按 `[start, end]` 裁剪 1m 重算的那几根** ⇒ 每次窄窗修复都顺手抹掉前一交易日全天 + 当日夜盘的会话K，而没人回来补（`grep -a -A20 Traceback reports/ingest.log` 空 ⇒ 修复没崩过，就是删多写少）。
  **修法（`core/session_rebuild.py:204-218`）**：删除带改为**由本次真正重算出来的那批 row 的 `datetime_end` 跨度推出**（`min(ends) ~ max(ends)`），且 `rows` 为空时**一根都不删**；不带 `start/end` 的全量重建仍保持"清空再全写"（那条本来就对称）。四条调用方（审计 `_fix_session`、ingest 启动 `_bootstrap_one_symbol`、`rebuild_session_klines.py`、`sync_all_main_klines.py`）共用这一个函数，一处改全部治好。本地只读取证（monkeypatch 掉写函数 + `COUNT(*)`，`_fixband_check.py`）：拿 11-19 日窗（邻日 11-18 有数据）跑，**旧口径 30m 命中 29 根、只写回 8 根 ⇒ 21 根删了补不回；新口径命中 8 根、写回 8 根，零外溢**。2h 同理 8→2 变 2→2。
  **补数因此才安全**：`rebuild_session_klines.py --all --start A --end B --execute` 在修复前会连 A 前一天、B 后一天一起删，**必须先部署这次修复再补**。11-20 的 dry-run（11-06~11-14，130 品种，9.1s）已确认 **1m 在这段完全够用**：ag `1m=3480 → 30m 72 / 1h 36 / 2h 18 / 3h 18 / 4h 12`（= 6 个交易日 × 每日 12/6/3/3/2 根），全市场合计 `30m 5668 / 1h 2838 / 2h 1419 / 3h 1419 / 4h 894`。1d 的 11-06/07 另用 `repair_today_klines.py --day …` 修。
  **11-20 已补完并只读复核**：EXECUTE 窗 `--start 2026-11-06 --end 2026-11-17`，21.7s，`30m 7396 / 1h 3702 / 2h 1851 / 3h 1851 / 4h 1166`。逐日覆盖（主连）回到 11-06=87、11-09~11=79、11-12~11-18=80、11-19=75、11-20=69，**11-02~11-05 仍是 87 ⇒ 11-05 没被删除带走**；ag 主连 2h 11-02~11-19 逐日 3 根连续，三顶 12843/12762/12748、两颈 12367/12453 与内存重建逐根一致。**拿补好的库内序列复跑三重检测（60 日 113 根 2h，常规档与严格档都是 0 条）** ⇒ 数据洞只是我第一次判错的原因，这个三重顶按现口径仍不出，拒因是量能递增 + 右回调点死锁（台账 #51 的 B/C）。**任何吃过 11-06~11-17 空洞的回测/回放数字（含 11-20 凌晨那趟 60 天）现在都要重跑才可比。** 闭环还差：在线 ingest 容器仍是旧代码，非交易时段跑一次 §4 那条 `up -d --build`。
- **1d 又缺 11-14/11-15**（11-16 09:56 水位表 `dur=86400 max=2026-11-13 00:00`）。`rebuild_session_klines.py` 只合成 30m/1h/2h/3h/4h，不管 1d/5m；1d 用
  `docker compose run --rm --no-deps futures-ingest python -u scripts/repair_today_klines.py --day 2026-11-14 --durations 1d`（再 `--day 2026-11-15`）。该脚本**直接写库、无 dry-run**，且会再开一个 TqApi 会话占配额。
- **5m 与 1d 只有 89 个品种有数据，1m 有 130**（同一张水位表）。差额应是「历史回填写过的品种」多于「实时订阅的品种」。**11-19 实测（只读 SQL，`COUNT(DISTINCT symbol)`）已对上**：1m 130、30m 130、5m 89、1d 89（近 90 日 88、其中 ≥20 根 87），而 TqSdk 当下返回的商品主连只有 **81**，剔掉 11 个低流动性品种后实时订阅 **70**（§1 监控品种范围）⇒ **130−70=60 个品种不会再有新告警**，其中 `89−81=8` 个是「库里有 5m/1d 历史、但天勤已不返回主连」（多半是摘牌/换码），具体是哪 8 个要拿 `SELECT DISTINCT symbol FROM kline_bars WHERE duration_sec=86400` 与 ingest 启动时的名单求差集才定得下。要覆盖得补历史或加订阅。
- ingest 常态就是 ≈ 单核打满（`docker stats` 5 次采样 90.8/95.9/100.0/99.7/100.0%，4 核约 25%；monitor 则是分钟边界脉冲，同一批采样 27.0/0.7/23.0/1.0/21.3%，api 0.06%）。主因已定位：`serial_to_rows` 用 `klines.iloc[i]["col"]` 逐行取标量，**60 行的 1m 序列一次实测 25.8 ms**；按单核打满反推 ≈ 每秒 35–39 次调用（每 0.25s 圈约 9 条序列被 `is_changing` 判为变化），数字自洽。降载两档：向量化取列（输出逐行一致，实测 5.5 ms，→ 约 20% 单核）、或只解析水位之后的行（调用方本就按 watermark 过滤，可 <1 ms，→ 个位数百分比，但要连带核 `correction`/半截K 判定）。**不改告警语义**，且 ingest 是 `enable_monitor=False` 不跑策略——所以告警口径怎么改都不影响它，别把 CPU 变化算成规则改动的功劳。
- 开仓时点：一年基线和 30 天贴收盘（仅 2 条、竞价逆向选择）都**否定**了抢收盘跳空，规则未改。
- **3 进程分片已上生产并验收通过（11-16 13:30 开盘）**：预热 261 键 `sum=12.64s` → 墙钟 `4.7s` = **2.69×**；13:30/13:35 的告警轮**一条 `本轮扫描` 都没打**（阈值 1s）⇒ 单轮全部 <1s，改造前这批键是秒级起步。告警口径不变（`右(...) 时刻 = K线时刻`）。资源：monitor MEM 153.8→**479.8 MiB（+326）**、PIDS 9→37、CPU 1.77%，**ingest 20.47% 未被抢**。剩下两档：①每键重取 500 根 → 按键缓存 frame + 增量取数；②新扫描行（每轮必打）要等下次 `--build` 部署才生效。
