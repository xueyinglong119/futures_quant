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

## 2. 反复踩过的坑

- **去重键用窗口相对下标 → 同一个头反复告警**（SH 30m 曾一个头出 4 条）。
- **回放预过滤必须跟触发口径一致**：`scripts/audit_killed_samples.py:42` 现在按 `strat.confirmed_shoulders` 分流——当根口径看 `i` 是否盖过前 `order` 根，慢口径看 `i - order` 是否两侧极值。改触发规则时这里必须跟着改，否则回测报 0 条。
- **当根告警的代价**：尾部候选只要盖过前 `pivot_order` 根就算右肩，而 11-13 实测「仅凭单边就成立」的候选里 57.1%（低点侧 70.1%）会在 `pivot_order` 根内被更极端的同向拐点盖掉——这部分告警属于抢跑后被证伪，只能靠胜率数据衡量。
- **loose / strict 是两次独立回放，改后不是改前的严格子集**：`滤掉 N 条` 是时间戳集合差，`strict − (loose ∩ strict)` 那部分是只在改后侧新出现的。别把「改后 ≤ 改前」当恒等式读。
- **平台头豁免容差不能太宽**：0.25×头高时 lu 上涨段有 ≥4 根收盘落在带内被误判成平台头，对称闸 2.5 直接形同虚设（台账 #31）。
- **加载窗不够会静默跳过周期**：`run_backtest.py:45` 的 `LOAD_DAYS` = 1m/5m 60 天、30m 90、1h 120、2h 150、1d 300。60 天窗里 SH 的 2h 只有约 107 根，低于阈值就不出数；中金所品种只有日盘，1h 也会缺。看到某周期整列缺失先按这个算，不是 bug。
- `datetime_end` 字段对 raw 周期（1m/3m/5m/10m/15m）其实是**起始**时刻，只有会话合成周期（30m/1h/2h/3h/4h）才是结束时刻。
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

规则 A/B（`scripts/ab_hs_rules.py`，OLD 用类方法补丁还原旧口径）：`HS_AB_SHARD=k/n`、`HS_AB_TFS`、`HS_AB_SYMBOLS`、`HS_AB_OUT`、`HS_AB_DUMP=1`。命令一律**写成单行**，反斜杠续行粘进终端会被截断成 `-bash: ocker: command not found`。

```bash
docker run -d --rm --name ab_hs_0 --env-file .env -e HS_AB_SHARD=0/2 -e HS_AB_TFS=30m,1h,2h -e HS_AB_OUT=/app/reports/ab_hs_0.txt -v $PWD/reports:/app/reports futures-quant-api python -u scripts/ab_hs_rules.py
```

ETA：`awk -F'[()]' 'NF>2{gsub("s","",$2); s+=$2; n++} END{if(n)printf "已完成 %d 个，平均 %.0f 秒/个，两片并行剩余约 %.0f 分钟\n", n, s/n, (89-n)/2*s/n/60}' reports/ab_hs_0.txt reports/ab_hs_1.txt`——线性下限，**实际要 ×2**（两片抢 2 核 + 云盘）。

容器 `Up ... (unhealthy)` 是镜像 healthcheck 在探 `:8000/health`，跑脚本的容器不起 HTTP，**必红不是挂了**，别 stop。

## 4. 部署与看结果

- **代码是 `COPY` 进镜像的，`git pull` + `restart` 等于什么都没做。** 生产上必须：
  `docker compose up -d --build futures-api futures-ingest futures-monitor`
  服务名显式列全、**绝不带 `futures-backtest`**（一次性回测任务，裸 `up` 会把它点起来打满 CPU）。
- 验证真进容器：`docker compose exec futures-monitor grep -c <新函数名> /app/core/paper_trade.py`（`grep -c` 数匹配行数，只出现一次时期望值就是 `1`）。
- `docker compose logs futures-monitor`（和 ingest）**永远是空的，不是故障**——stdout 被重定向进 `reports/*.log`，用 `tail -n 40 reports/monitor.log`。
- 画廊路由（只有生产机有 HTTP）：`/backtest`、`/backtest/killed`、`/backtest/pivot`、`/backtest/prom`、`/backtest/neck`。路由只是读 `reports/<变体>/gallery_*.html`，缺文件就 404 且 detail 写明缺哪个；在别的机器跑出来的要先 `scp -r` 到生产机 `/opt/futures_quant/reports/`，**到位后不用重启任何容器**。
- 生产 sshd 只认密钥（`PasswordAuthentication no`，报 `Permission denied (publickey...)` 且不问密码不是输错）。老机→生产机已互信。

## 5. 未闭环

- 台账里的待决策：#10 左肩≈颈线是否另加闸；#12/#13 是否单跑「仅 earliest-R」；5m 单周期细化（sym/conf 两轮里 5m 都是唯一改善的周期，但合计仍为负）。
- **合计仍贴着 20% 平衡线**（最近一轮 20.8%/+0.008），5m 与 1d 是稳定拖累——见台账 32 条。
- 数据修复：11-06～11-10 的 30m/1h/2h/3h/4h 会话周期回填
  `docker compose run --rm --no-deps futures-ingest python -u scripts/rebuild_session_klines.py --all --start 2026-11-06 --end 2026-11-10`，确认输出无误再加 `--execute`；1d 的 11-06 另修。
- 开仓时点：一年基线和 30 天贴收盘（仅 2 条、竞价逆向选择）都**否定**了抢收盘跳空，规则未改。
