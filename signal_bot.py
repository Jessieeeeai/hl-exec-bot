"""
F6 实时信号机器人 — 每次运行:
1) 拉最新 BTC 1h K线 (300 根足够 ADX/EMA-200 计算)
2) 应用 F6 策略检测信号
3) 跟踪每个信号的生命周期: waiting -> entered -> tp_hit/sl_hit/expired
4) 状态变化时推送 TG
5) 10 个信号全部完成后,发战报

用法: python3 signal_bot.py
环境变量:
  COINGLASS_API_KEY  - 数据 API key
  TELEGRAM_BOT_TOKEN - TG bot token
  TELEGRAM_CHAT_ID   - 接收消息的 chat_id
"""
import os
import sys
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta

from signals import detect_signals
from backtest import VariantConfig, _compute_ema, _compute_adx
from tg_notify import (send_message, msg_signal_formed, msg_entered,
                        msg_tp_hit, msg_sl_hit, msg_expired,
                        msg_invalidated_by_sl, msg_final_report)


# F6 策略配置 (与 variants.py 的 F6 一致)
CFG = VariantConfig(
    name="F6_live",
    body_ratio=0.5,
    entanglement_tolerance=0.005,  # T5: 0.5% 容差, 提升信号利用率
    r_multiple=2.0,
    sl_buffer_pct=0.02,
    entry_mode="breakout_confirm",
    entry_wait_bars=3,
    regime_mode="optimal",
    regime_adx_high=25,
    regime_ema_dist_trend=0.02,
)

MAX_SIGNALS = 10  # 收 10 个就出战报
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
N_BARS = 300       # 拉多少根 1h K线 (>200 才能算 EMA-200)
COINGLASS_BASE = "https://open-api-v4.coinglass.com"


# ========== 数据拉取 ==========

def fetch_btc_1h_bars(api_key: str, n: int = 300):
    """拉最近 n 根 BTC 1h K线"""
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (n + 5) * 3600 * 1000
    params = {
        "exchange": "Binance",
        "symbol": "BTCUSDT",
        "interval": "1h",
        "limit": 1000,
        "start_time": start_ms,
        "end_time": end_ms,
    }
    url = f"{COINGLASS_BASE}/api/futures/price/history?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "CG-API-KEY": api_key,
        "accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode())
    items = raw.get("data") or raw.get("result") or []
    bars = []
    for it in items:
        if isinstance(it, dict):
            ts = int(it.get("time") or it.get("t") or it.get("timestamp"))
            o = float(it.get("open") or it.get("o"))
            h = float(it.get("high") or it.get("h"))
            l = float(it.get("low") or it.get("l"))
            c = float(it.get("close") or it.get("c"))
        else:
            ts, o, h, l, c = int(it[0]), float(it[1]), float(it[2]), float(it[3]), float(it[4])
        if ts > 1e12:
            ts //= 1000
        bars.append({
            "date": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
            "ts": ts, "open": o, "high": h, "low": l, "close": c,
        })
    bars.sort(key=lambda x: x["ts"])
    return bars


# ========== 状态管理 ==========

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"signals": [], "first_signal_date": None, "final_sent": False,
                "anchor_ts": None, "started": False}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ========== 信号检测 & 跟踪 ==========

def apply_f6_filter(bars, sig):
    """对单个信号判定 F6 是否接受 (regime + 顺势 EMA-200)"""
    idx = sig["index"]
    ema200 = _compute_ema(bars, 200)
    adx = _compute_adx(bars, 14)
    close = bars[idx]["close"]
    ev = ema200[idx]
    dist = abs(close - ev) / ev if ev > 0 else 0
    # 强趋势期跳过
    if adx[idx] > CFG.regime_adx_high and dist > CFG.regime_ema_dist_trend:
        return False
    # 顺势 EMA-200
    if sig["direction"] == "long" and close > ev:
        return True
    if sig["direction"] == "short" and close < ev:
        return True
    return False


def build_signal_record(bars, sig):
    """从 raw signal 构造跟踪记录"""
    direction = sig["direction"]
    # SL: B/C 极值 ± 缓冲
    if direction == "long":
        extremity = min(sig["B_low"], sig["C_low"])
        sl = extremity * (1 - CFG.sl_buffer_pct)
        trigger = max(sig["B_close"], sig["C_close"])  # 突破上沿
    else:
        extremity = max(sig["B_high"], sig["C_high"])
        sl = extremity * (1 + CFG.sl_buffer_pct)
        trigger = min(sig["B_close"], sig["C_close"])  # 跌破下沿
    r = abs(trigger - sl)
    if direction == "long":
        tp = trigger + CFG.r_multiple * r
    else:
        tp = trigger - CFG.r_multiple * r

    sig_bar = bars[sig["index"]]
    expires_ts = sig_bar["ts"] + (CFG.entry_wait_bars + 1) * 3600  # 信号K线+3h
    expires_str = datetime.utcfromtimestamp(expires_ts).strftime("%Y-%m-%d %H:%M UTC")

    pattern = ("看涨反转 (急跌后底部缠绕)" if direction == "long"
               else "看跌反转 (急涨后顶部缠绕)")

    return {
        "signal_time": sig_bar["date"] + " UTC",
        "signal_ts": sig_bar["ts"],
        "direction": direction,
        "trigger_price": round(trigger, 2),
        "sl": round(sl, 2),
        "tp": round(tp, 2),
        "expires_at": expires_str,
        "expires_ts": expires_ts,
        "pattern_desc": pattern,
        "status": "waiting",
        "entry_price": None,
        "entry_time": None,
        "exit_price": None,
        "exit_time": None,
        "result_r": None,
    }


def detect_new_signals(bars, state):
    """扫描 bars 找 F6 信号, 跟 state 比对找新增。只要 anchor_ts 之后的"""
    anchor_ts = state.get("anchor_ts") or 0
    known_ts = {s["signal_ts"] for s in state["signals"]}
    raw_signals = detect_signals(bars, CFG.body_ratio, CFG.entanglement_tolerance)
    new = []
    for sig in raw_signals:
        sig_bar = bars[sig["index"]]
        if sig_bar["ts"] in known_ts:
            continue
        # 锚点之前的信号忽略 (避免首次部署时把历史信号全推一遍)
        if sig_bar["ts"] <= anchor_ts:
            continue
        # 信号 K 线必须已收盘
        if sig["index"] >= len(bars) - 1:
            continue
        if not apply_f6_filter(bars, sig):
            continue
        rec = build_signal_record(bars, sig)
        new.append(rec)
    return new


def update_signal_status(bars, sig_rec):
    """对一个 waiting/entered 信号, 看后续 K 线推进它的状态。"""
    sig_ts = sig_rec["signal_ts"]
    # 找 signal 之后的 bars
    after = [b for b in bars if b["ts"] > sig_ts]
    if not after:
        return False  # 还没下一根

    changed = False
    direction = sig_rec["direction"]
    trigger = sig_rec["trigger_price"]
    sl = sig_rec["sl"]
    tp = sig_rec["tp"]

    if sig_rec["status"] == "waiting":
        # 在 expires_ts 之前等突破或 SL 反向触发
        for bar in after:
            if bar["ts"] > sig_rec["expires_ts"]:
                # 窗口过了, 作废
                sig_rec["status"] = "expired"
                changed = True
                break
            if direction == "long":
                # 反向: 价格触及 SL (没入场就先挂掉)
                if bar["low"] <= sl:
                    sig_rec["status"] = "invalidated"
                    changed = True
                    break
                # 突破: 价格上破 trigger
                if bar["high"] >= trigger:
                    sig_rec["status"] = "entered"
                    sig_rec["entry_price"] = trigger
                    sig_rec["entry_time"] = bar["date"] + " UTC"
                    sig_rec["entry_ts"] = bar["ts"]
                    changed = True
                    break
            else:
                if bar["high"] >= sl:
                    sig_rec["status"] = "invalidated"
                    changed = True
                    break
                if bar["low"] <= trigger:
                    sig_rec["status"] = "entered"
                    sig_rec["entry_price"] = trigger
                    sig_rec["entry_time"] = bar["date"] + " UTC"
                    sig_rec["entry_ts"] = bar["ts"]
                    changed = True
                    break

    if sig_rec["status"] == "entered":
        entry_ts = sig_rec.get("entry_ts", sig_ts)
        for bar in after:
            if bar["ts"] <= entry_ts:
                continue
            # 检查 TP/SL (保守: 同根 K 线同时触发按 SL)
            if direction == "long":
                if bar["low"] <= sl:
                    sig_rec["status"] = "sl_hit"
                    sig_rec["exit_price"] = sl
                    sig_rec["exit_time"] = bar["date"] + " UTC"
                    sig_rec["exit_ts"] = bar["ts"]
                    sig_rec["result_r"] = -1.0
                    changed = True
                    break
                if bar["high"] >= tp:
                    sig_rec["status"] = "tp_hit"
                    sig_rec["exit_price"] = tp
                    sig_rec["exit_time"] = bar["date"] + " UTC"
                    sig_rec["exit_ts"] = bar["ts"]
                    sig_rec["result_r"] = 2.0
                    changed = True
                    break
            else:
                if bar["high"] >= sl:
                    sig_rec["status"] = "sl_hit"
                    sig_rec["exit_price"] = sl
                    sig_rec["exit_time"] = bar["date"] + " UTC"
                    sig_rec["exit_ts"] = bar["ts"]
                    sig_rec["result_r"] = -1.0
                    changed = True
                    break
                if bar["low"] <= tp:
                    sig_rec["status"] = "tp_hit"
                    sig_rec["exit_price"] = tp
                    sig_rec["exit_time"] = bar["date"] + " UTC"
                    sig_rec["exit_ts"] = bar["ts"]
                    sig_rec["result_r"] = 2.0
                    changed = True
                    break

    return changed


# ========== 战绩统计 ==========

def compute_stats(state):
    completed = [s for s in state["signals"] if s["status"] in ("tp_hit", "sl_hit")]
    expired = [s for s in state["signals"] if s["status"] in ("expired", "invalidated")]
    wins = sum(1 for s in completed if s["status"] == "tp_hit")
    losses = sum(1 for s in completed if s["status"] == "sl_hit")
    total_r = sum(s["result_r"] for s in completed if s["result_r"] is not None)

    # 连胜连败
    streak_win = streak_loss = max_win = max_loss = 0
    for s in completed:
        if s["status"] == "tp_hit":
            streak_win += 1; streak_loss = 0
            max_win = max(max_win, streak_win)
        else:
            streak_loss += 1; streak_win = 0
            max_loss = max(max_loss, streak_loss)

    return {
        "total_signals": len(state["signals"]),
        "completed": len(completed),
        "wins": wins,
        "losses": losses,
        "expired": len(expired),
        "win_rate": wins / len(completed) if completed else 0,
        "total_r": total_r,
        "max_win_streak": max_win,
        "max_loss_streak": max_loss,
    }


# ========== 主流程 ==========

def main():
    api_key = os.environ.get("COINGLASS_API_KEY")
    if not api_key:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("COINGLASS_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    if not api_key:
        print("ERROR: 缺少 COINGLASS_API_KEY")
        sys.exit(1)

    print(f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] 开始检查...")
    state = load_state()

    # 1) 拉数据
    try:
        bars = fetch_btc_1h_bars(api_key, N_BARS)
    except Exception as e:
        print(f"  拉数据失败: {e}")
        sys.exit(1)
    if len(bars) < 250:
        print(f"  数据不足 {len(bars)} 根, 退出")
        sys.exit(1)
    print(f"  拉到 {len(bars)} 根 1h K线, 最新: {bars[-1]['date']} = ${bars[-1]['close']:.2f}")

    # 1.5) 首次运行: 设置锚点 + 发欢迎消息, 不处理任何历史信号
    if not state.get("started"):
        state["anchor_ts"] = bars[-2]["ts"]  # 倒数第二根 (最新还在 forming)
        state["started"] = True
        latest_close = bars[-1]["close"]
        welcome = (
            f"🤖 *F6 信号机器人已启动*\n"
            f"━━━━━━━━━━━━━━━\n"
            f"策略: F6 (Regime + EMA-200 顺势 + 突破入场)\n"
            f"标的: BTCUSDT 1h\n"
            f"当前 BTC: `${latest_close:,.2f}`\n"
            f"锚点时间: `{bars[-2]['date']} UTC`\n\n"
            f"⏱ 每 5 分钟检查一次\n"
            f"🎯 收满 10 个信号自动发战报\n\n"
            f"_等待第一个 F6 信号..._"
        )
        send_message(welcome)
        save_state(state)
        print(f"  首次启动, 锚点 {bars[-2]['date']}, 已发欢迎消息")
        return

    # 2) 检查现有信号的状态变化
    new_messages = []
    for i, sig in enumerate(state["signals"], 1):
        if sig["status"] in ("tp_hit", "sl_hit", "expired", "invalidated"):
            continue
        old_status = sig["status"]
        if update_signal_status(bars, sig):
            new_status = sig["status"]
            print(f"  信号 #{i:03d}: {old_status} -> {new_status}")
            stats = compute_stats(state)
            if new_status == "entered":
                new_messages.append(msg_entered(i, sig, sig["entry_price"], sig["entry_time"]))
            elif new_status == "tp_hit":
                hold = (sig["exit_ts"] - sig["entry_ts"]) / 3600
                new_messages.append(msg_tp_hit(i, sig, sig["exit_price"], sig["exit_time"], hold, stats))
            elif new_status == "sl_hit":
                hold = (sig["exit_ts"] - sig["entry_ts"]) / 3600
                new_messages.append(msg_sl_hit(i, sig, sig["exit_price"], sig["exit_time"], hold, stats))
            elif new_status == "expired":
                new_messages.append(msg_expired(i, sig))
            elif new_status == "invalidated":
                new_messages.append(msg_invalidated_by_sl(i, sig))

    # 3) 如果还没收满 10 个, 检测新信号
    if len(state["signals"]) < MAX_SIGNALS:
        new_sigs = detect_new_signals(bars, state)
        for sig_rec in new_sigs:
            if len(state["signals"]) >= MAX_SIGNALS:
                break
            state["signals"].append(sig_rec)
            if state["first_signal_date"] is None:
                state["first_signal_date"] = sig_rec["signal_time"]
            n = len(state["signals"])
            print(f"  新信号 #{n:03d}: {sig_rec['direction']} @ {sig_rec['signal_time']}")
            new_messages.append(msg_signal_formed(n, sig_rec))
            # 立刻尝试推进它的状态 (可能上个小时已经突破)
            update_signal_status(bars, sig_rec)

    # 4) 发 TG
    for msg in new_messages:
        send_message(msg)
        time.sleep(0.5)  # 避免 TG 限流

    # 5) 战报判定
    stats = compute_stats(state)
    if (stats["completed"] + stats["expired"]) >= MAX_SIGNALS and not state["final_sent"]:
        period_end = datetime.utcnow().strftime("%Y-%m-%d")
        period = f"{state['first_signal_date'][:10] if state['first_signal_date'] else '?'} ~ {period_end}"
        stats["period"] = period
        send_message(msg_final_report(stats))
        state["final_sent"] = True
        print("  战报已发送!")

    save_state(state)
    print(f"  done. 当前 {len(state['signals'])} 信号, "
          f"{stats['wins']}胜 {stats['losses']}败 {stats['expired']}作废, R={stats['total_r']:+.2f}")


if __name__ == "__main__":
    main()
