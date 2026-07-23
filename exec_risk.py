"""
风控层 — 仓位计算 + 熔断闸门。
所有闸门在开新单前逐一检查, 任一不过则拒绝开单并给出原因。
"""
from datetime import datetime, timezone

# ===== 可调参数 (改这里, 不要在代码里散落魔法数字) =====
RISK_PCT = 0.01            # 单笔风险 = 权益 * 1%
MAX_LEVERAGE = 5           # 杠杆上限 (名义仓位/权益)
MIN_NOTIONAL_USD = 10.0    # HL 最小下单名义价值
MAX_CONCURRENT = 1         # 同时最多持有几个仓位 (O1: 单仓互斥)
DAILY_LOSS_LIMIT_R = 2.0   # 当日(UTC)累计亏损达 -2R 后当日停止开新单
LOSS_STREAK_HALT = 3       # 连亏 3 笔 → 熔断, 需要手动解除
MAX_SL_DIST_PCT = 0.05     # 止损距离 >5% 的信号放弃 (手册 O4)
EQUITY_FLOOR_USD = 50.0    # 权益低于此值拒绝开单 (防止打穿)


def position_size(equity: float, entry_px: float, sl_px: float) -> tuple:
    """返回 (size, notional, reason)。size=0 表示拒绝, reason 给原因。"""
    sl_dist = abs(entry_px - sl_px)
    if sl_dist <= 0:
        return 0.0, 0.0, "止损距离为 0"
    sl_dist_pct = sl_dist / entry_px
    if sl_dist_pct > MAX_SL_DIST_PCT:
        return 0.0, 0.0, f"止损距离 {sl_dist_pct:.1%} 超过上限 {MAX_SL_DIST_PCT:.0%}"

    risk_usd = equity * RISK_PCT
    size = risk_usd / sl_dist                 # 币数量
    notional = size * entry_px
    # 杠杆上限压制
    max_notional = equity * MAX_LEVERAGE
    if notional > max_notional:
        size = max_notional / entry_px
        notional = max_notional
    if notional < MIN_NOTIONAL_USD:
        return 0.0, 0.0, f"名义价值 ${notional:.2f} 低于交易所最小值 ${MIN_NOTIONAL_USD}"
    return size, notional, ""


def utc_day(ts: float = None) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def gate_check(state: dict, equity: float, open_position: bool,
               pending_entry: bool) -> tuple:
    """开新单前的闸门检查。返回 (allowed, reason)。"""
    if state.get("halted"):
        return False, f"熔断中: {state.get('halt_reason', '?')} (手动删除 halted 字段解除)"

    if equity < EQUITY_FLOOR_USD:
        return False, f"权益 ${equity:.2f} 低于下限 ${EQUITY_FLOOR_USD}"

    n_active = (1 if open_position else 0) + (1 if pending_entry else 0)
    if n_active >= MAX_CONCURRENT:
        return False, "已有持仓或待触发入场单 (单仓互斥)"

    # 当日亏损上限
    today = utc_day()
    day_r = sum(t.get("result_r", 0) for t in state.get("trades", [])
                if t.get("exit_day") == today)
    if day_r <= -DAILY_LOSS_LIMIT_R:
        return False, f"今日已亏 {day_r:.1f}R, 达到日亏上限, 明日 UTC 0 点自动恢复"

    return True, ""


def update_streak_halt(state: dict) -> bool:
    """平仓后调用: 检查连亏熔断。触发则置 halted 并返回 True。"""
    trades = state.get("trades", [])
    streak = 0
    for t in reversed(trades):
        if t.get("result_r", 0) < 0:
            streak += 1
        else:
            break
    if streak >= LOSS_STREAK_HALT:
        state["halted"] = True
        state["halt_reason"] = f"连亏 {streak} 笔 (阈值 {LOSS_STREAK_HALT})"
        return True
    return False


def ledger_check(state: dict, equity: float, tolerance_usd: float = None) -> tuple:
    """账本自洽 (手册 E1): 权益 ≈ 起始本金 + Σ已平仓盈亏。
    容差按权益 1% 或 $1 取大者 (资金费率/滑点会造成小偏移)。
    不符则返回 (False, 差额), 调用方应暂停开单+告警。"""
    base = state.get("base_equity")
    if base is None:
        return True, 0.0
    expected = base + sum(t.get("pnl_usd", 0) for t in state.get("trades", []))
    tol = tolerance_usd if tolerance_usd is not None else max(1.0, equity * 0.01)
    diff = equity - expected
    return abs(diff) <= tol, diff
