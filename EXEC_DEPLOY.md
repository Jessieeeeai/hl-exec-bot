# F6 自动交易机器人 (Hyperliquid) — 部署指南

信号检测和纸面机器人完全同一套 F6 逻辑，区别是它会真的在 Hyperliquid 下单。
**默认 DRY_RUN 演习模式，不动真钱。** 确认演习输出正常后，改一个开关切实盘。

> 本机器人是**独立仓库**部署，和你现有的 btc-signal-bot 信号仓库完全隔离。
> 原仓库、原 workflow、原 TG 消息一切照旧，互不影响。

## 文件清单

| 文件 | 作用 |
|------|------|
| `exec_bot.py` | 主程序：检测信号 → 挂入场触发单 → 成交后挂 TP/SL → 记账播报 |
| `signals.py` 等 4 个 | F6 策略的冻结副本（快照 2026-07-23），与原仓库解耦 |
| `hl_broker.py` | Hyperliquid 适配层（下单/查询/取整） |
| `exec_risk.py` | 风控参数和熔断（所有可调参数都在文件开头） |
| `live_state.json` | 实盘状态账本（机器人自动生成和更新） |
| `.github/workflows/exec_bot.yml` | 每 5 分钟自动运行 |
| `tests/test_exec.py` | 单元测试（19 个用例） |

## 第 1 步：准备 Hyperliquid 账户

1. 打开 [app.hyperliquid.xyz](https://app.hyperliquid.xyz)，用钱包（如 MetaMask/Rabby）连接
2. 入金：从 Arbitrum 链转 USDC 进去（你说的小额试运行，$500–1000 就够）
3. 创建 API 钱包（机器人专用，**不是**你的主钱包私钥）：
   - 右上角菜单 → **More** → **API**
   - 点 **Generate** 生成一个 Agent Wallet，起个名字（如 `f6bot`），Authorize
   - **复制生成的私钥**（只显示一次）——这是给机器人的
   - 同时记下你**主账户的地址**（0x 开头，连接钱包的那个地址）

> API 钱包只能交易，不能提币，泄漏了最多是被乱下单，资金提不走。这也是为什么绝不要把主钱包私钥给机器人。

## 第 2 步：配置 GitHub Secrets

新仓库 hl-exec-bot → Settings → Secrets and variables → Actions：

**Secrets（共 5 个）：**

先把原仓库用的 `COINGLASS_API_KEY`、`TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` 在新仓库配一遍，再加两个新的：

| Name | 内容 |
|------|------|
| `HL_PRIVATE_KEY` | 第 1 步生成的 API 钱包私钥 |
| `HL_ACCOUNT_ADDRESS` | 你主账户的 0x 地址 |

**Variables（旁边的 Variables 标签，新增）：**

| Name | 内容 |
|------|------|
| `EXEC_MODE` | `dry_run`（先演习！确认没问题后改成 `live`） |

## 第 3 步：新建独立仓库并推送

1. GitHub 上新建一个 **Private** 仓库，比如 `hl-exec-bot`（和 btc-signal-bot 分开）
2. 在 Mac 终端：

```bash
cd "/Users/guoxiaoquandediannao/Desktop/交易/hl_exec_bot"

# workflow 文件挪到位 (我没法直接往 .github/workflows 里写)
mkdir -p .github/workflows
mv exec_bot.workflow.yml .github/workflows/exec_bot.yml

git init
git branch -M main
git add .
git commit -m "F6 Hyperliquid exec bot"
git remote add origin https://github.com/你的用户名/hl-exec-bot.git
git push -u origin main
```

3. Secrets 配在**新仓库**里（下面第 2 步的表格），COINGLASS_API_KEY / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 三个老的也要在新仓库再配一遍（Secrets 不跨仓库共享）

Actions 标签 → 「F6 自动交易机器人 (Hyperliquid)」→ Run workflow 手动跑一次确认成功。
首次运行会发 TG 启动消息并设锚点（历史信号不处理）。

## 第 4 步：演习 → 实盘

1. **演习期（建议至少跑到第一个完整信号走完）**：TG 里所有消息都带 🎬 [演习] 标记，
   检查触发价、仓位大小、TP/SL 是否符合预期
2. **切实盘**：Settings → Variables → 把 `EXEC_MODE` 改成 `live`，下一轮生效
3. 切换后第一条启动消息会显示 ⚡️ 实盘 + 你的真实权益

## 运行逻辑（一图流）

```
新 F6 信号 ──风控闸门──> 交易所挂入场触发单（突破价，交易所侧执行）
   │                        │
   │ 4h 内没突破/先打到SL    │ 价格突破触发
   ▼                        ▼
 撤单作废                市价入场 ──立刻──> 挂 TP 限价 + SL 市价触发（OCO）
                            │
                 TP 或 SL 成交 ──> 记账 + TG 播报 + 熔断检查
```

关键点：**止损单挂在交易所**，不依赖机器人在线。GitHub Actions 哪怕挂了一小时，
你的持仓也始终有 SL 保护。

## 风控参数（`exec_risk.py` 开头，改完 push 即生效）

| 参数 | 默认 | 说明 |
|------|------|------|
| `RISK_PCT` | 1% | 单笔风险占权益比例 |
| `MAX_LEVERAGE` | 5x | 名义仓位上限 |
| `MAX_CONCURRENT` | 1 | 同时最多 1 个仓位（含待触发） |
| `DAILY_LOSS_LIMIT_R` | 2R | 当日亏满 2R 停止开新单，UTC 0 点恢复 |
| `LOSS_STREAK_HALT` | 3 笔 | 连亏 3 笔熔断，**需手动解除** |
| `MAX_SL_DIST_PCT` | 5% | 止损距离超 5% 的信号放弃 |

## 紧急操作

- **立刻停止一切**：Actions → exec_bot workflow → 右上 ⋯ → Disable workflow。
  然后去 Hyperliquid 网页手动处理持仓（机器人停了 TP/SL 单还在交易所挂着）
- **解除熔断**：编辑仓库里的 `live_state.json`，删掉 `"halted": true` 和 `"halt_reason"` 两行，commit
- **手动出入金后**：机器人会报「账本对不上」并停止开单——把 `live_state.json` 里的
  `base_equity` 改成调整后的基准值即可恢复

## 已知边界（诚实清单）

1. **入场成交到挂上 TP/SL 之间有最多 ~1 分钟裸奔窗口**（同一轮内完成，通常几秒）。
   极端行情下这段时间没有止损保护，这是 5 分钟轮询架构的固有限制
2. GitHub Actions 的 5 分钟 cron 高峰期可能延迟到 8–15 分钟。入场和止损不受影响
   （都在交易所侧），受影响的只是新信号发现和作废撤单的及时性
3. 演习模式的成交按触发价理想成交，实盘会有滑点（入场消息里会报实际滑点）
4. 机器人只管自己开的仓。你手动开的仓它不碰，只会 TG 提醒你有「无主持仓」
