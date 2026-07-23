# F6 → Hyperliquid 自动交易机器人（独立仓库）

与信号机器人仓库完全隔离：本仓库是自包含的，含 F6 策略的冻结副本
（signals.py / backtest.py / signal_bot.py / tg_notify.py，快照日期 2026-07-23）。
原信号仓库怎么跑还怎么跑，两边互不影响。

部署看 EXEC_DEPLOY.md。默认 DRY_RUN 演习模式。

注意：如果以后你更新了原仓库的 F6 策略代码，这里的副本不会自动跟着变——
这是刻意的（实盘策略冻结），要同步时手动覆盖这 4 个文件再 push。
