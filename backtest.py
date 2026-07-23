"""
R-Multiple 回测引擎
- 入场: 信号发出的下一根K线开盘 (避免未来函数)
- 止损: max(B.high, C.high) * (1 + sl_buffer_pct)  / min(B.low, C.low) * (1 - sl_buffer_pct)
  默认 sl_buffer_pct = 0.02 (即 1.02 / 0.98)
- R = |entry - SL|
- 止盈: entry ± r_multiple * R
- 同一根K线同时触及SL和TP时, 保守假设SL先触发
- 时间止损: 持仓超过N根仍未触发时市价平
- 手续费: 入+出各 0.05% (合约 taker 参考)
"""
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from signals import detect_signals


@dataclass
class VariantConfig:
    name: str
    body_ratio: float = 0.5
    entanglement_tolerance: float = 0.0  # 0 = 严格; 0.001 = 0.1% 容差
    r_multiple: float = 2.0         # 止盈倍数 (TP = entry ± r_multiple * R)
    sl_buffer_pct: float = 0.02     # 止损缓冲百分比, 默认 2%
    time_stop_bars: int = 0         # 0 = 不启用
    breakeven_at_r: float = 0.0     # 0 = 不启用; e.g. 1.0 = 浮盈到1R时把SL上移到入场价
    fee_rate: float = 0.0005        # 单边 0.05%
    # 入场模式
    entry_mode: str = "next_bar_open"  # "next_bar_open" 或 "breakout_confirm"
    entry_wait_bars: int = 0           # breakout_confirm 时的等待窗口, 0 = 不限
    # 趋势过滤 (EMA): 多单只在 close > EMA, 空单只在 close < EMA. 0 = 不启用
    ema_filter_period: int = 0
    ema_filter_invert: bool = False  # True: 反转逻辑 (取逆势信号: 多单在 close<EMA, 空单在 close>EMA)
    # ATR 波动率过滤: 只在 ATR >= 历史 X 百分位时入场. 0 = 不启用
    atr_period: int = 0
    atr_min_percentile: float = 0.0  # 0.5 = 只取ATR>=中位数 (高波动期, 反转友好)
    # Regime 自适应: 用 ADX+EMA200偏离度 判定 trend/chop/transition, 各档应用不同过滤
    regime_mode: str = ""              # "" 关闭; "switch" 启用
    regime_adx_high: float = 25.0      # ADX > 此值 = 趋势
    regime_adx_low: float = 20.0       # ADX < 此值 = 震荡
    regime_ema_dist_trend: float = 0.03    # 离EMA200 > 3% = 趋势
    regime_ema_dist_chop: float = 0.015    # 离EMA200 < 1.5% = 震荡
    regime_skip_transition: bool = True    # 过渡期是否跳过


def _stop_loss_price(direction: str, sig: Dict[str, Any], buffer_pct: float) -> float:
    """
    多: SL = min(B.low, C.low) * (1 - buffer_pct)
    空: SL = max(B.high, C.high) * (1 + buffer_pct)
    """
    if direction == "long":
        extremity = min(sig["B_low"], sig["C_low"])
        return extremity * (1 - buffer_pct)
    extremity = max(sig["B_high"], sig["C_high"])
    return extremity * (1 + buffer_pct)


def _resolve_entry(bars, sig, cfg) -> Optional[Dict[str, Any]]:
    """根据 entry_mode 解析入场。返回 {entry, entry_idx, entry_date} 或 None (信号作废)。"""
    i = sig["index"]
    direction = sig["direction"]

    if cfg.entry_mode == "next_bar_open":
        if i + 1 >= len(bars):
            return None
        eb = bars[i + 1]
        return {"entry": eb["open"], "entry_idx": i + 1, "entry_date": eb["date"]}

    # breakout_confirm 模式: 等价格突破 max/min(B.close, C.close)
    # 突破之前如果价格反向触及SL, 信号作废
    sl_level = _stop_loss_price(direction, sig, cfg.sl_buffer_pct)
    if direction == "long":
        trigger = max(sig["B_close"], sig["C_close"])  # 多: 突破收盘价较高者
    else:
        trigger = min(sig["B_close"], sig["C_close"])  # 空: 跌破收盘价较低者

    max_wait = cfg.entry_wait_bars if cfg.entry_wait_bars > 0 else (len(bars) - i - 1)
    end_j = min(len(bars), i + 1 + max_wait)

    for j in range(i + 1, end_j):
        bar = bars[j]
        # 先看反向触发SL (作废)
        if direction == "long":
            # 反向作废: 价格跌到SL以下
            if bar["low"] <= sl_level:
                return None
            # 触发入场: 价格上破 trigger
            if bar["high"] >= trigger:
                # 限价单: 假设以 trigger 价成交
                # 处理 gap: 如果开盘已经超过 trigger, 取开盘价(更不利)
                entry_price = max(trigger, bar["open"]) if bar["open"] > trigger else trigger
                return {"entry": entry_price, "entry_idx": j, "entry_date": bar["date"]}
        else:
            if bar["high"] >= sl_level:
                return None
            if bar["low"] <= trigger:
                entry_price = min(trigger, bar["open"]) if bar["open"] < trigger else trigger
                return {"entry": entry_price, "entry_idx": j, "entry_date": bar["date"]}
    return None  # 等待窗口耗尽未突破


def _simulate_one(bars: List[Dict[str, Any]], sig: Dict[str, Any], cfg: VariantConfig) -> Optional[Dict[str, Any]]:
    direction = sig["direction"]
    er = _resolve_entry(bars, sig, cfg)
    if er is None:
        return None
    entry = er["entry"]
    entry_idx = er["entry_idx"]
    entry_date = er["entry_date"]

    sl = _stop_loss_price(direction, sig, cfg.sl_buffer_pct)
    if direction == "long" and sl >= entry:
        return None
    if direction == "short" and sl <= entry:
        return None

    r = abs(entry - sl)
    if r <= 0:
        return None
    tp = entry + cfg.r_multiple * r if direction == "long" else entry - cfg.r_multiple * r

    exit_price = None
    exit_reason = None
    exit_index = None
    moved_to_be = False

    # 持仓循环: 从入场K线的下一根开始扫描 (入场那根本身的 high/low 已被 trigger 用过)
    for j in range(entry_idx + 1, len(bars)):
        bar = bars[j]
        bars_held = j - entry_idx

        if direction == "long":
            if bar["low"] <= sl:
                exit_price = sl
                exit_reason = "SL" if not moved_to_be else "BE"
                exit_index = j
                break
            if bar["high"] >= tp:
                exit_price = tp
                exit_reason = "TP"
                exit_index = j
                break
            if cfg.breakeven_at_r > 0 and not moved_to_be:
                trigger_be = entry + cfg.breakeven_at_r * r
                if bar["high"] >= trigger_be:
                    sl = max(sl, entry)
                    moved_to_be = True
        else:
            if bar["high"] >= sl:
                exit_price = sl
                exit_reason = "SL" if not moved_to_be else "BE"
                exit_index = j
                break
            if bar["low"] <= tp:
                exit_price = tp
                exit_reason = "TP"
                exit_index = j
                break
            if cfg.breakeven_at_r > 0 and not moved_to_be:
                trigger_be = entry - cfg.breakeven_at_r * r
                if bar["low"] <= trigger_be:
                    sl = min(sl, entry)
                    moved_to_be = True

        if cfg.time_stop_bars > 0 and bars_held >= cfg.time_stop_bars:
            exit_price = bar["close"]
            exit_reason = "TIME"
            exit_index = j
            break

    if exit_price is None:
        exit_price = bars[-1]["close"]
        exit_reason = "EOD"
        exit_index = len(bars) - 1

    if direction == "long":
        gross_r = (exit_price - entry) / r
    else:
        gross_r = (entry - exit_price) / r

    fee_in_r = (entry * cfg.fee_rate + exit_price * cfg.fee_rate) / r
    net_r = gross_r - fee_in_r

    return {
        "entry_date": entry_date,
        "exit_date": bars[exit_index]["date"],
        "direction": direction,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "exit": exit_price,
        "exit_reason": exit_reason,
        "bars_held": exit_index - entry_idx,
        "gross_r": gross_r,
        "net_r": net_r,
        "win": net_r > 0,
    }


def _compute_ema(bars, period):
    """计算各根K线的EMA close"""
    if period <= 0:
        return None
    alpha = 2.0 / (period + 1)
    ema = [bars[0]["close"]]
    for i in range(1, len(bars)):
        ema.append(alpha * bars[i]["close"] + (1 - alpha) * ema[-1])
    return ema


def _compute_atr(bars, period=14):
    """14周期ATR (典型值)"""
    if period <= 0:
        return None
    trs = []
    for i in range(len(bars)):
        if i == 0:
            tr = bars[i]["high"] - bars[i]["low"]
        else:
            pc = bars[i-1]["close"]
            tr = max(bars[i]["high"] - bars[i]["low"],
                     abs(bars[i]["high"] - pc),
                     abs(bars[i]["low"] - pc))
        trs.append(tr)
    atr = []
    for i in range(len(bars)):
        if i < period:
            atr.append(sum(trs[:i+1]) / (i+1))
        else:
            atr.append(sum(trs[i-period+1:i+1]) / period)
    return atr


def _compute_adx(bars, period=14):
    """Wilder ADX(14) - 方向运动指数, 衡量趋势强度"""
    if len(bars) < period + 1:
        return [0.0] * len(bars)
    trs, plus_dms, minus_dms = [0.0], [0.0], [0.0]
    for i in range(1, len(bars)):
        h, l = bars[i]["high"], bars[i]["low"]
        pc, ph, pl = bars[i-1]["close"], bars[i-1]["high"], bars[i-1]["low"]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        up_move = h - ph
        dn_move = pl - l
        plus_dm = up_move if (up_move > dn_move and up_move > 0) else 0
        minus_dm = dn_move if (dn_move > up_move and dn_move > 0) else 0
        trs.append(tr); plus_dms.append(plus_dm); minus_dms.append(minus_dm)

    # Wilder 平滑
    def _wilder_smooth(arr, p):
        out = [0.0] * len(arr)
        if len(arr) <= p:
            return out
        out[p] = sum(arr[1:p+1])
        for i in range(p+1, len(arr)):
            out[i] = out[i-1] - (out[i-1] / p) + arr[i]
        return out

    sm_tr = _wilder_smooth(trs, period)
    sm_pdm = _wilder_smooth(plus_dms, period)
    sm_mdm = _wilder_smooth(minus_dms, period)

    plus_di, minus_di, dx_vals = [0.0] * len(bars), [0.0] * len(bars), [0.0] * len(bars)
    for i in range(period, len(bars)):
        if sm_tr[i] > 0:
            plus_di[i] = 100 * sm_pdm[i] / sm_tr[i]
            minus_di[i] = 100 * sm_mdm[i] / sm_tr[i]
            denom = plus_di[i] + minus_di[i]
            dx_vals[i] = 100 * abs(plus_di[i] - minus_di[i]) / denom if denom > 0 else 0

    adx = [0.0] * len(bars)
    # 初始 ADX = 前 period 个 DX 的平均
    if len(bars) >= 2 * period:
        adx[2*period - 1] = sum(dx_vals[period:2*period]) / period
        for i in range(2*period, len(bars)):
            adx[i] = (adx[i-1] * (period - 1) + dx_vals[i]) / period
    return adx


def run_backtest(bars: List[Dict[str, Any]], cfg: VariantConfig) -> Dict[str, Any]:
    signals = detect_signals(bars, cfg.body_ratio, cfg.entanglement_tolerance)

    # ============ Optimal Regime: 跳过强趋势, 其他用M2顺势 ============
    if cfg.regime_mode == "optimal":
        ema200 = _compute_ema(bars, 200)
        adx = _compute_adx(bars, 14)
        filtered = []
        for sig in signals:
            idx = sig["index"]
            close = bars[idx]["close"]
            ev = ema200[idx]
            dist = abs(close - ev) / ev if ev > 0 else 0
            is_trend = (adx[idx] > cfg.regime_adx_high) and (dist > cfg.regime_ema_dist_trend)
            if is_trend:
                continue  # 跳过强趋势期 (负 EV)
            # 其他时间 (震荡/过渡): 用 EMA-200 顺势过滤
            if (sig["direction"] == "long" and close > ev) or \
               (sig["direction"] == "short" and close < ev):
                filtered.append(sig)
        signals = filtered

    # ============ Regime 自适应切换 (旧逻辑) ============
    if cfg.regime_mode == "switch":
        ema200 = _compute_ema(bars, 200)
        adx = _compute_adx(bars, 14)
        atr = _compute_atr(bars, 14)
        # 预计算 ATR 中位数 (用于 chop 模式)
        valid_atr = sorted([a for a in atr if a is not None and a > 0])
        atr_median = valid_atr[len(valid_atr)//2] if valid_atr else 0

        filtered = []
        for sig in signals:
            idx = sig["index"]
            close = bars[idx]["close"]
            ema_val = ema200[idx]
            adx_val = adx[idx]
            dist = abs(close - ema_val) / ema_val if ema_val > 0 else 0

            is_trend = (adx_val > cfg.regime_adx_high) and (dist > cfg.regime_ema_dist_trend)
            is_chop  = (adx_val < cfg.regime_adx_low)  and (dist < cfg.regime_ema_dist_chop)

            if is_trend:
                # 趋势模式: M2 顺势 EMA-200 逻辑
                if sig["direction"] == "long" and close > ema_val:
                    filtered.append(sig)
                elif sig["direction"] == "short" and close < ema_val:
                    filtered.append(sig)
            elif is_chop:
                # 震荡模式: M9 反转 EMA-100 + 高 ATR
                ema100 = _compute_ema(bars, 100) if not hasattr(cfg, "_ema100_cache") else cfg._ema100_cache
                # 反转: 多在 close<EMA, 空在 close>EMA
                inverted_pass = ((sig["direction"] == "long" and close < ema100[idx]) or
                                  (sig["direction"] == "short" and close > ema100[idx]))
                atr_pass = atr[idx] >= atr_median
                if inverted_pass and atr_pass:
                    filtered.append(sig)
            else:
                # 过渡期
                if not cfg.regime_skip_transition:
                    filtered.append(sig)  # 不跳过则全收
        signals = filtered

    # EMA 过滤 (顺势 或 反转逻辑)
    if cfg.ema_filter_period > 0:
        ema = _compute_ema(bars, cfg.ema_filter_period)
        filtered = []
        for sig in signals:
            idx = sig["index"]
            close = bars[idx]["close"]
            # 标准逻辑: 多在EMA上, 空在EMA下
            normal_pass = ((sig["direction"] == "long" and close > ema[idx]) or
                           (sig["direction"] == "short" and close < ema[idx]))
            if cfg.ema_filter_invert:
                if not normal_pass:
                    filtered.append(sig)
            else:
                if normal_pass:
                    filtered.append(sig)
        signals = filtered

    # ATR 波动率过滤
    if cfg.atr_period > 0 and cfg.atr_min_percentile > 0:
        atr = _compute_atr(bars, cfg.atr_period)
        # 计算阈值: 从全序列分布取百分位
        valid = sorted([a for a in atr if a is not None and a > 0])
        if valid:
            idx_pct = min(len(valid) - 1, int(len(valid) * cfg.atr_min_percentile))
            threshold = valid[idx_pct]
            filtered = []
            for sig in signals:
                sig_idx = sig["index"]
                if atr[sig_idx] >= threshold:
                    filtered.append(sig)
            signals = filtered

    trades = []
    for sig in signals:
        t = _simulate_one(bars, sig, cfg)
        if t is not None:
            trades.append(t)
    return _summarize(trades, cfg)


def _summarize(trades, cfg: VariantConfig) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "variant": cfg.name, "n_trades": 0, "n_wins": 0, "n_losses": 0,
            "win_rate": 0, "avg_r": 0, "total_r": 0, "max_drawdown_r": 0,
            "longest_winning_streak": 0, "longest_losing_streak": 0, "trades": [],
        }
    wins = sum(1 for t in trades if t["win"])
    win_rate = wins / n
    total_r = sum(t["net_r"] for t in trades)
    avg_r = total_r / n

    equity, peak, max_dd = 0, 0, 0
    for t in trades:
        equity += t["net_r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    longest_win, longest_loss, cur_win, cur_loss = 0, 0, 0, 0
    for t in trades:
        if t["win"]:
            cur_win += 1; cur_loss = 0
            longest_win = max(longest_win, cur_win)
        else:
            cur_loss += 1; cur_win = 0
            longest_loss = max(longest_loss, cur_loss)

    return {
        "variant": cfg.name,
        "n_trades": n,
        "n_wins": wins,
        "n_losses": n - wins,
        "win_rate": round(win_rate, 4),
        "avg_r": round(avg_r, 3),
        "total_r": round(total_r, 2),
        "max_drawdown_r": round(max_dd, 2),
        "longest_winning_streak": longest_win,
        "longest_losing_streak": longest_loss,
        "trades": trades,
    }


if __name__ == "__main__":
    # 自检: 合成数据
    import random
    random.seed(42)
    bars = []
    price = 60000.0
    for d in range(500):
        drift = random.gauss(0, price * 0.005)
        if random.random() < 0.06:
            drift *= 4
        o = price
        c = max(1, price + drift)
        h = max(o, c) * (1 + abs(random.gauss(0, 0.003)))
        l = min(o, c) * (1 - abs(random.gauss(0, 0.003)))
        bars.append({"date": f"bar{d:03d}", "open": o, "high": h, "low": l, "close": c})
        price = c

    cfg = VariantConfig(name="baseline", body_ratio=0.5, r_multiple=2.0, sl_buffer_pct=0.02)
    result = run_backtest(bars, cfg)
    print(f"Variant: {result['variant']}")
    print(f"Trades: {result['n_trades']}, Wins: {result['n_wins']}, Win rate: {result['win_rate']*100:.1f}%")
    print(f"Total R: {result['total_r']}, Avg R: {result['avg_r']}, Max DD: {result['max_drawdown_r']}R")
