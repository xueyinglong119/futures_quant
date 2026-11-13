# 头肩告警规则与服务器运维要点

这份文档记的是**当前生效的形态口径**、踩过的坑、以及全量回测/部署必须在服务器上跑的操作。
规则本身的演进以 git 为准（`git log -- strategies/head_shoulders.py`），这里只留结论和出处。

## 1. 当前形态口径（`573bd28` + `2a5ac6b`）

| 项 | 现口径 | 出处 |
|----|--------|------|
| 形态点位 | 肩、头、颈线拐点**一律按收盘价量**；蜡烛 high/low 只用于止盈止损摆动 | `strategies/head_shoulders.py:178` `_pivot_series` |
| 拐点判定 | 常规两侧 ±`pivot_order`；再加一根**单边尾部候选**（当前这根盖过前 `pivot_order` 根即算拐点） | `:182` `:194` `:206` `_with_tail_pivot` |
| 触发时机 | **右肩就是当前这根 K 线即告警**，不等破颈线、不再多走 `pivot_order` 根确认 | `:227` `_right_shoulder_confirmed` |
| 肩离颈 | 两肩各 ≥ **0.3×形态高度**（原 0.4×） | `:297` `:299` |
| 颈线拐点 | 离肩、离头**各至少 1 根**即可（原来贴肩的那根不算） | `:459` `gap = 1` |
| 其余质量闸 | 高度/价 ≥0.008、颈线斜率 ≤0.5h、头肩高差 ≥0.25h、两肩差 ≤0.5h；strict 下两肩差 ≤1.5%·颈线、斜率 ≤0.5%·颈线；跨度 ≤5 交易日；≤15m 周期相邻间隙 >130 分钟即拒 | `core/hs_monitor.py:62` `make_strategy`、`head_shoulders.py:612` |
| 周期参数 | 30m `pivot_order=4 / min_span=10 / max_span=110 / lookback=300`；1h `3/8/110/250`；2h `3/6/110/200`；1m `8/25/110/500`；5m `6/20/110/400`；1d `2/8/110/220` | `core/hs_monitor.py:52` `TF_CONFIGS` |
| 去重键 | 用 **K 线时刻**（`kind:左肩时刻:头时刻:右肩时刻` + `kind-head:头时刻`），不是窗口相对下标 | `head_shoulders.py:566` |
| 图上颈线 | 锚点同样取收盘价，与量化口径一致 | `core/hs_chart.py:119` `pick = "close"` |

胜率口径（`scripts/replay_winrate.py`）：信号根收盘价进场，止盈 ±0.8×swing、止损 ∓0.2×swing，`HORIZON=120` 根未决即超时，同一根内 both-touch 记亏损。

## 2. 已验证过的结论 / 坑

- **去重键用窗口相对下标会重复告警**。回放窗口每根都在滑，同一形态的相对下标每根都变，改前 SH 30m 一个头（1516，10-30 11:15）在 11-02 10:45 / 11-02 23:00 / 11-03 09:30 / 11-03 10:00 连出 4 条。改成时刻键后该品种 30m 由 10 条降到 5 条。
- **改前/改后的告警条数不能直接对比**，去重键变了；只能同一份回放里 A/B 两个变体并排比。
- **`replay_detail` 的预过滤必须跟着触发规则走**，否则「右肩当根即告警」这条在回测里会被旧预过滤（要求两侧拐点）整段滤掉，表现为「一条告警都没有」。现写法见 `scripts/audit_killed_samples.py:43`。
- **当根告警的代价**：只等 4 根就确认的旧口径，是在等右肩不被更快的极值推翻。实测仅凭左肩成立的高点候选里 **57.1%**（低点侧 70.1%）会在 4 根内被更极端的同向拐点盖掉——即改后有一部分告警属于「抢跑后被证伪」，这部分只能靠胜率数据裁决。
- **烧碱 1573 顶（30m）**：点位改收盘价后唯一卡它的就是肩离颈那道闸（11.9 vs 需 12.2，差 0.3），0.3× 能过、0.4× 过不了。当根告警口径下它的首次成立时点是 **2026-11-12 14:15**（右 1551 / 颈 1541），而不是旧口径的 11-13 09:30（右 1549 / 颈 1536）——先满足条件的那根拿到去重槽位。
- **加载窗不够会让周期被静默跳过**：60 天加载窗 ≈37 个交易日，SH 每天约 345 个 1m 分钟 → 30m 约 426 根、1h 约 213 根、**2h 约 107 根**，低于 `ab_hs_rules.py` 的 200 根下限就不出数；中金所品种只有日盘，1h 也会缺。看到某周期整列缺失先按这个算，别当成 bug。
- `datetime_end` 字段对 raw 周期（1m/3m/5m/10m/15m）其实是**起始**时刻，只有会话合成周期（30m/1h/2h/3h/4h）才是结束时刻。跨周期对齐时务必注意。

## 3. 全量回测 / A/B：只在服务器上跑

分工：**生产 4C8G `116.62.103.114`** 只跑实时链路；**老 2C2G `120.26.26.72` 的 `/opt/futures_quant` 是回测专用机**。本机（Windows）只做 `py_compile` 和单品种轻量自检，全量一律上服务器。

老机红线：**绝不 `docker compose up`**（新 compose 是四服务，一起手就拉起 ingest/monitor → 双份订阅、双份告警、双份模拟盘、天勤配额翻倍）。老机只用 `docker build` + `docker run -d --rm`，旧 api/ingest/monitor 容器保持 Exited。

```bash
cd /opt/futures_quant
git stash push deploy/install_docker.sh   # 该机这个文件有本地改动，否则 pull 被中止
git pull
docker build -t futures-quant-api .       # 结尾空格+点不能少；不 build 则 docker run 秒退 can't open file
```

规则 A/B（`scripts/ab_hs_rules.py`，OLD 用类方法补丁还原改前口径，NEW 走库内默认）：

```bash
docker run -d --rm --name ab_hs_0 --env-file .env -e HS_AB_SHARD=0/2 -e HS_AB_TFS=30m,1h,2h -e HS_AB_OUT=/app/reports/ab_hs_0.txt -v $PWD/reports:/app/reports futures-quant-api python -u scripts/ab_hs_rules.py
docker run -d --rm --name ab_hs_1 --env-file .env -e HS_AB_SHARD=1/2 -e HS_AB_TFS=30m,1h,2h -e HS_AB_OUT=/app/reports/ab_hs_1.txt -v $PWD/reports:/app/reports futures-quant-api python -u scripts/ab_hs_rules.py
```

- 环境变量：`HS_AB_SHARD=k/n` 分片、`HS_AB_TFS` 周期（默认 `30m,1h`）、`HS_AB_SYMBOLS` 指定品种、`HS_AB_OUT` 输出、`HS_AB_DUMP=1` 逐条列告警。
- 命令**写成单行，别用反斜杠续行**——粘进那台终端会被截断成 `-bash: ocker: command not found`。
- 输出：逐品种行 `30m OLD=a NEW=b 同x 新y 旧z 早N根 胜/负 w/l->w/l`（同=两口径都有的形态，新/旧=只有一边有，早=同形态改后提前几根），片尾各带「全品种」「仅监控 41 品种」合计。
- 进度/ETA：`awk -F'[()]' 'NF>2{gsub("s","",$2); s+=$2; n++} END{if(n)printf "已完成 %d 个，平均 %.0f 秒/个，两片并行剩余约 %.0f 分钟\n", n, s/n, (89-n)/2*s/n/60}' reports/ab_hs_0.txt reports/ab_hs_1.txt`。这个外推是**线性下限，实际要 ×2**（两片抢 2 核 + 云盘）。
- 容器 `Up ... (unhealthy)` 是镜像 healthcheck 在探 `:8000/health`，跑脚本的容器不起 HTTP，**必红，不是挂了**，别 stop。
- 拆片前先 `docker stats --no-stream` 看内存；2C2G 上两片并行约 112MiB/片，安全。

批量回测入口是 `scripts/run_backtest.py`，其中三个开关**只影响回测、实盘口径不变**：`--allow-session-gap`（放开 1m/5m 跨收盘断点）、`--pivot-override 300:1`（按周期改拐点确认根数）、`--load-days N`（覆盖各周期预热窗口，`run_backtest.py:45` 的默认表是 1m/5m=60、30m=90、1h=120、2h=150、1d=300）。

## 4. 部署与看结果

- **代码是 `COPY` 进镜像的，`git pull` + `restart` 等于什么都没做。** 生产上必须：
  `docker compose up -d --build futures-api futures-ingest futures-monitor`
  服务名显式列全、**绝不带 `futures-backtest`**（那是一次性回测任务，裸 `up` 会把它点起来打满 CPU）。
- 验证真进容器：`docker compose exec futures-monitor grep -c <新函数名> /app/core/paper_trade.py`（`grep -c` 数的是匹配行数，只出现一次时正确期望值就是 `1`）。
- `docker compose logs futures-monitor`（和 ingest）**永远是空的，不是故障**——stdout 被重定向进 `reports/*.log`，用 `tail -n 40 reports/monitor.log`。
- 回测画廊 `http://116.62.103.114:8000/backtest*` 只是读 `reports/<变体>/gallery_*.html`，文件不在就 404 且 detail 写明缺哪个；老机产物要先 scp 过去。更快是 scp 单个自包含 HTML 到本机用电脑浏览器打开（base64 内联，手机会卡）。scp 到位后**不用重启任何容器**。
- 生产 sshd 只认密钥（`PasswordAuthentication no`，报 `Permission denied (publickey...)` 且不问密码不是输错）。老机→生产机已互信，反向不通。

## 5. 未闭环事项

- **③ 肩离颈 0.3× 是保留还是整条去掉**：用户原话是「这个距离的限制也去掉」，当前实现保守取了 0.3×，等 `reports/ab_hs_0.txt` + `ab_hs_1.txt` 的胜率和条数再裁决。
- **①② 的全量裁决**：同样等上面两片数据（收盘价点位 / 右肩当根即告警）。
- **数据修复**：11-06～11-10 的 30m/1h/2h/3h/4h 会话周期需回填——
  `docker compose run --rm --no-deps futures-ingest python -u scripts/rebuild_session_klines.py --all --start 2026-11-06 --end 2026-11-10`，确认输出无误后加 `--execute`；1d 的 11-06 另修。
- **开仓时点**：一年基线和 30 天贴收盘（仅 2 条样本、竞价逆向选择）都**否定**了抢收盘跳空，strict 样本极小，规则未改。
