"""
两线反转信号检测器
对应Pine Script: 两线反转指标加密大漂亮v2

只负责"形态识别", 不计算止损 (止损放在 backtest.py 里, 按 variant 的 buffer 决定)。
信号里携带 B、C 两根K线的极值 (B_low/B_high/C_low/C_high), 供下游算止损用。
"""
from typing import List, Dict, Any


def typical_price(bar: Dict[str, float]) -> float:
    return (bar["high"] + bar["low"] + bar["close"]) / 3.0


def body_ratio(bar: Dict[str, float]) -> float:
    body = abs(bar["close"] - bar["open"])
    rng = abs(bar["high"] - bar["low"])
    return body / rng if rng > 0 else 0.0


def body_overlap(b: Dict[str, float], c: Dict[str, float]):
    """B、C两根K线实体的重叠区间。返回(low, high, size)。无重叠时size<=0。"""
    b_lo, b_hi = min(b["open"], b["close"]), max(b["open"], b["close"])
    c_lo, c_hi = min(c["open"], c["close"]), max(c["open"], c["close"])
    lo = max(b_lo, c_lo)
    hi = min(b_hi, c_hi)
    return lo, hi, hi - lo


def detect_signals(bars: List[Dict[str, Any]],
                    body_ratio_threshold: float = 0.5,
                    entanglement_tolerance: float = 0.0) -> List[Dict[str, Any]]:
    """
    扫描K线序列, 输出所有反转信号。
    bars: 升序时间排列的K线列表, 每根含 date/open/high/low/close
    body_ratio_threshold: 实体最小占比 (默认 50%)
    entanglement_tolerance: 缠绕检查的容差 (默认 0%, 即严格)。
                            tpB/tpC 可以超出对方实体上下沿这个百分比仍算通过
    """
    signals = []
    for i in range(2, len(bars)):
        A, B, C = bars[i - 2], bars[i - 1], bars[i]
        br_B = body_ratio(B)
        br_C = body_ratio(C)
        if br_B < body_ratio_threshold or br_C < body_ratio_threshold:
            continue

        tp_B = typical_price(B)
        tp_C = typical_price(C)

        c_lo, c_hi = min(C["open"], C["close"]), max(C["open"], C["close"])
        b_lo, b_hi = min(B["open"], B["close"]), max(B["open"], B["close"])
        # 容差版: 允许 tpB/tpC 超出对方实体上下沿 entanglement_tolerance 比例
        tol = entanglement_tolerance
        in_range = (
            c_lo * (1 - tol) <= tp_B <= c_hi * (1 + tol) and
            b_lo * (1 - tol) <= tp_C <= b_hi * (1 + tol)
        )
        if not in_range:
            continue

        overlap_lo, overlap_hi, overlap_size = body_overlap(B, C)
        if overlap_size <= 0:
            continue

        common = {
            "index": i,
            "date": C["date"],
            "entry_ref": C["close"],
            "B_low": B["low"], "B_high": B["high"],
            "C_low": C["low"], "C_high": C["high"],
            "B_close": B["close"], "C_close": C["close"],
            "B_open": B["open"], "C_open": C["open"],
            "overlap_lo": overlap_lo, "overlap_hi": overlap_hi, "overlap_size": overlap_size,
            "body_ratio_B": br_B, "body_ratio_C": br_C,
        }

        # 看涨: B 的典型价 < A 的最低 (急跌后底部缠绕)
        if tp_B < A["low"]:
            signals.append({**common, "direction": "long"})
        # 看跌: B 的典型价 > A 的最高 (急涨后顶部缠绕)
        elif tp_B > A["high"]:
            signals.append({**common, "direction": "short"})
    return signals


if __name__ == "__main__":
    # 自检: 构造一个明确的看涨形态
    test_bars = [
        {"date": "2025-01-01 00:00", "open": 100, "high": 102, "low": 98,  "close": 101},
        {"date": "2025-01-01 01:00", "open": 100, "high": 101, "low": 95,  "close": 95.5},  # A 急跌
        {"date": "2025-01-01 02:00", "open": 94,  "high": 95,  "low": 92,  "close": 92.5},  # B 继续跌
        {"date": "2025-01-01 03:00", "open": 92.5,"high": 95,  "low": 92,  "close": 94.5},  # C 反弹缠绕
    ]
    sigs = detect_signals(test_bars, 0.3)
    print(f"Detected {len(sigs)} signals")
    for s in sigs:
        print(s)
