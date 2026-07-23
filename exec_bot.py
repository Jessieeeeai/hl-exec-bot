"""
F6 自动交易机器人 — Hyperliquid 执行版
================================================
在 signal_bot.py (纸面信号跟踪) 基础上增加真实下单:

  信号形成  -> 交易所挂入场触发单 (突破价, 交易所侧执行, 机器人离线也生效)
  触发成交  -> 立刻挂 TP 限价 + SL 市价触发 (positionTpsl 组合, 自动 OCO)
  TP/SL 成交 -> 记账 + TG 播报
  信号过期/作废 -> 撤入场单

三种运行模式 (环境变量 EXEC_MODE):
  dry_run (默认) — 全流程模拟, 不连交易所下单, 只打日志+TG
  live           — 真实下单

安全机制:
  - 单仓互斥 / 日亏 2R 停开 / 连亏 3 笔熔断 (见 exec_risk.py)
  - 每轮先对账: 交易所的持仓/挂单/成交是唯一事实来源
  - 持仓若发现没有 TP/SL 保护单, 立刻补挂
  - 账本自洽校验失败 -> 暂停开单 + 告警

环境变量:
  COINGLASS_API_KEY   数据源
  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID   播报
  EXEC_MODE           dry_run | live
  HL_PRIVATE_KEY      Hyperliquid API 钱包私钥 (live 必需)
  HL_ACCOUNT_ADDRESS  主账户地址 (用 API 钱包时必需)
  HL_TESTNET          "1" = 用测试网
用法: python3 exec_bot.py
"""
import os
import sys
import json
import time
from datetime import datetime, timezone

# 复用纸面信号机器人的检测逻辑 (同一套 F6 参数, 保证信号一致)
from signal_bot import (CFG, fetch_btc_1h_bars, apply_f6_filter,
                        build_signal_record, update_signal_status, N_BARS)
from signals import detect_signals
from tg_notify import send_message

import exec_risk
from hl_broker import HLBroker

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_state.json")
COIN = "BTC"


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"signals": [], "trades": [], "anchor_ts": None, "started": False,
                "base_equity": None, "last_fill_check_ms": 0}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def tg(msg):
    try:
        send_message(msg)
        time.sleep(0.5)
    except Exception as e:
        print(f"  TG 发送失败: {e}")


# ---------- 信号检测 (锚点之后的新信号) ----------

def detect_new_signals(bars, state):
    anchor_ts = state.get("anchor_ts") or 0
    known_ts = {s["signal_ts"] for s in state["signals"]}
    raw = detect_signals(bars, CFG.body_ratio, CFG.entanglement_tolerance)
    out = []
    for sig in raw:
        sig_bar = bars[sig["index"]]
        if sig_bar["ts"] in known_ts or sig_bar["ts"] <= anchor_ts:
            continue
        if sig["index"] >= len(bars) - 1:   # 必须已收盘
            continue
        if not apply_f6_filter(bars, sig):
            continue
        out.append(build_signal_record(bars, sig))
    return out


# ---------- 对账: 交易所事实 -> 本地状态 ----------

def reconcile(state, broker, bars):
    """用交易所的成交/持仓/挂单推进本地信号状态。live 模式专用。"""
    msgs = []
    fills = broker.fills_since(state.get("last_fill_check_ms", 0) or
                               int((time.time() - 86400) * 1000))
    if fills:
        state["last_fill_check_ms"] = max(f["time"] for f in fills) + 1
    fills_by_oid = {}
    for f in fills:
        fills_by_oid.setdefault(f.get("oid"), []).append(f)

    open_oids = {o["oid"] for o in broker.open_orders()}
    pos = broker.position()

    for sig in state["signals"]:
        if sig["status"] not in ("waiting", "entered"):
            continue

        # ---- waiting: 入场触发单在场外等突破 ----
        if sig["status"] == "waiting":
            entry_fills = fills_by_oid.get(sig.get("entry_oid"), [])
            if entry_fills:
                tot_sz = sum(float(f["sz"]) for f in entry_fills)
                avg_px = sum(float(f["px"]) * float(f["sz"]) for f in entry_fills) / tot_sz
                sig["status"] = "entered"
                sig["entry_price"] = round(avg_px, 2)
                sig["entry_time"] = now_utc() + " UTC"
                sig["entry_ts"] = int(time.time())
                sig["filled_size"] = tot_sz
                slip = (avg_px - sig["trigger_price"]) / sig["trigger_price"]
                msgs.append(f"🟢 <b>已入场</b> {'做多' if sig['direction']=='long' else '做空'} "
                            f"{tot_sz} {COIN} @ <code>${avg_px:,.2f}</code>\n"
                            f"计划触发价 ${sig['trigger_price']:,.2f} | 滑点 {slip:+.3%}\n"
                            f"TP <code>${sig['tp']:,.2f}</code> | SL <code>${sig['sl']:,.2f}</code>")
                # 立刻挂保护单
                ok, oids = broker.place_tpsl(sig["direction"] == "long",
                                             tot_sz, sig["tp"], sig["sl"])
                if ok:
                    sig["tp_oid"], sig["sl_oid"] = oids
                else:
                    msgs.append(f"🚨 <b>TP/SL 挂单失败</b>: {oids}\n下轮自动重试; 若持续失败请手动处理!")
                continue  # 本轮刚入场, TP/SL 状态下一轮再查 (persist 前 pos 快照已过期)
            else:
                # 没成交: 检查过期 / 作废 (用 K 线判定, 和纸面同规则)
                nowts = time.time()
                invalidated = False
                for bar in [b for b in bars if b["ts"] > sig["signal_ts"]]:
                    if sig["direction"] == "long" and bar["low"] <= sig["sl"]:
                        invalidated = True
                        break
                    if sig["direction"] == "short" and bar["high"] >= sig["sl"]:
                        invalidated = True
                        break
                if invalidated or nowts > sig["expires_ts"]:
                    if sig.get("entry_oid") in open_oids:
                        broker.cancel_order(sig["entry_oid"])
                    # 撤单后再查一次: 防止撤单前一瞬间成交
                    time.sleep(1)
                    late = broker.fills_since(state["last_fill_check_ms"])
                    late_entry = [f for f in late if f.get("oid") == sig.get("entry_oid")]
                    if late_entry:
                        continue  # 下一轮会按 entered 处理
                    sig["status"] = "invalidated" if invalidated else "expired"
                    label = "先触发止损, 作废" if invalidated else "突破窗口已过, 作废"
                    msgs.append(f"⚪️ 信号取消 ({label}), 已撤入场单\n"
                                f"方向 {sig['direction']} | 触发价 ${sig['trigger_price']:,.2f}")

        # ---- entered: 等 TP / SL ----
        if sig["status"] == "entered":
            tp_fills = fills_by_oid.get(sig.get("tp_oid"), [])
            sl_fills = fills_by_oid.get(sig.get("sl_oid"), [])
            closed = tp_fills or sl_fills
            if closed:
                is_tp = bool(tp_fills)
                cf = tp_fills or sl_fills
                tot_sz = sum(float(f["sz"]) for f in cf)
                avg_px = sum(float(f["px"]) * float(f["sz"]) for f in cf) / tot_sz
                pnl = sum(float(f.get("closedPnl", 0)) for f in cf)
                fee = sum(float(f.get("fee", 0)) for f in cf)
                _close_trade(state, sig, "tp_hit" if is_tp else "sl_hit",
                             avg_px, pnl - fee, msgs)
            elif pos is None:
                # 持仓没了但没找到 TP/SL 成交 -> 可能手动平仓或数据延迟
                sig["status"] = "manual_closed"
                sig["exit_time"] = now_utc() + " UTC"
                msgs.append("⚠️ 持仓已不在但未匹配到 TP/SL 成交 — 按手动平仓处理, 请核对账户")
            else:
                # 持仓在, 检查保护单是否齐全; 不齐立刻补挂 (安全关键)
                missing = (sig.get("tp_oid") not in open_oids or
                           sig.get("sl_oid") not in open_oids)
                if missing and not (tp_fills or sl_fills):
                    ok, oids = broker.place_tpsl(sig["direction"] == "long",
                                                 abs(pos["size"]), sig["tp"], sig["sl"])
                    if ok:
                        sig["tp_oid"], sig["sl_oid"] = oids
                        msgs.append("🔧 检测到保护单缺失, 已重新挂上 TP/SL")
                    else:
                        msgs.append(f"🚨 *补挂 TP/SL 失败*: {oids} — 当前持仓无止损保护, 请立即人工检查!")

    # ---- 孤儿检查 ----
    active = [s for s in state["signals"] if s["status"] in ("waiting", "entered")]
    if pos is not None and not any(s["status"] == "entered" for s in active):
        msgs.append(f"⚠️ 交易所有 {pos['size']} {COIN} 持仓但本地无对应记录 — "
                    f"机器人不会动它, 请人工确认")
    known_oids = set()
    for s in active:
        known_oids |= {s.get("entry_oid"), s.get("tp_oid"), s.get("sl_oid")}
    for o in broker.open_orders():
        if o["oid"] not in known_oids:
            broker.cancel_order(o["oid"])
            msgs.append(f"🧹 撤销无主挂单 oid={o['oid']} ({o.get('side')} @ {o.get('limitPx')})")
    return msgs


def _close_trade(state, sig, status, exit_px, pnl_usd, msgs):
    sig["status"] = status
    sig["exit_price"] = round(exit_px, 2)
    sig["exit_time"] = now_utc() + " UTC"
    sig["result_r"] = CFG.r_multiple if status == "tp_hit" else -1.0
    trade = {
        "direction": sig["direction"],
        "entry_price": sig["entry_price"], "exit_price": sig["exit_price"],
        "size": sig.get("filled_size"),
        "pnl_usd": round(pnl_usd, 2), "result_r": sig["result_r"],
        "status": status, "exit_day": exec_risk.utc_day(),
        "exit_time": sig["exit_time"],
    }
    state["trades"].append(trade)
    emoji = "✅" if status == "tp_hit" else "❌"
    wins = sum(1 for t in state["trades"] if t["result_r"] > 0)
    total = len(state["trades"])
    total_pnl = sum(t["pnl_usd"] for t in state["trades"])
    msgs.append(f"{emoji} <b>{'止盈' if status=='tp_hit' else '止损'}平仓</b> "
                f"@ <code>${exit_px:,.2f}</code>\n"
                f"本笔盈亏 <code>${pnl_usd:+,.2f}</code>\n"
                f"累计: {total} 笔 | {wins} 胜 | 总盈亏 <code>${total_pnl:+,.2f}</code>")
    if exec_risk.update_streak_halt(state):
        msgs.append(f"🛑 <b>熔断触发</b>: {state['halt_reason']}\n"
                    f"机器人已暂停开新单 (持仓保护单不受影响)。\n"
                    f"确认想继续后, 删除 live_state.json 里的 halted 字段即可恢复。")


# ---------- 开新单 ----------

def try_open_new(state, broker, bars, equity, msgs, dry_run):
    if len(bars) < 250:
        return
    new_sigs = detect_new_signals(bars, state)
    if not new_sigs:
        return
    for rec in new_sigs:
        pos = None if dry_run else broker.position()
        pending = any(s["status"] == "waiting" for s in state["signals"])
        entered = any(s["status"] == "entered" for s in state["signals"]) or pos is not None
        allowed, reason = exec_risk.gate_check(state, equity, entered, pending)
        if not allowed:
            state["signals"].append({**rec, "status": "skipped", "skip_reason": reason})
            msgs.append(f"⏭ 新信号被风控拦截: {reason}\n"
                        f"({rec['direction']} @ 触发 ${rec['trigger_price']:,.2f})")
            continue
        size, notional, why = exec_risk.position_size(
            equity, rec["trigger_price"], rec["sl"])
        if size <= 0:
            state["signals"].append({**rec, "status": "skipped", "skip_reason": why})
            msgs.append(f"⏭ 新信号放弃: {why}")
            continue
        size = broker.round_sz(size)
        ok, oid = broker.place_entry_stop(rec["direction"] == "long",
                                          size, rec["trigger_price"])
        if not ok:
            msgs.append(f"🚨 入场触发单失败: {oid}")
            continue
        rec["entry_oid"] = oid
        rec["planned_size"] = size
        state["signals"].append(rec)
        risk_usd = equity * exec_risk.RISK_PCT
        msgs.append(
            f"📡 <b>新 F6 信号</b> {'🟩 做多' if rec['direction']=='long' else '🟥 做空'}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"形态: {rec['pattern_desc']}\n"
            f"入场触发: <code>${rec['trigger_price']:,.2f}</code> (已挂交易所触发单)\n"
            f"止损: <code>${rec['sl']:,.2f}</code> | 止盈: <code>${rec['tp']:,.2f}</code> (R={CFG.r_multiple})\n"
            f"仓位: {size} {COIN} (≈${notional:,.0f} 名义, 风险 ${risk_usd:,.2f})\n"
            f"有效期至: {rec['expires_at']}")


# ---------- 主流程 ----------

def main():
    mode = os.environ.get("EXEC_MODE", "dry_run").lower()
    dry_run = mode != "live"
    testnet = os.environ.get("HL_TESTNET") == "1"
    api_key = os.environ.get("COINGLASS_API_KEY")
    if not api_key:
        print("ERROR: 缺少 COINGLASS_API_KEY")
        sys.exit(1)

    print(f"[{now_utc()}] exec_bot 启动 | 模式={'DRY_RUN' if dry_run else 'LIVE'}"
          f"{' (testnet)' if testnet else ''}")

    broker = HLBroker(private_key=os.environ.get("HL_PRIVATE_KEY"),
                      account_address=os.environ.get("HL_ACCOUNT_ADDRESS"),
                      testnet=testnet, dry_run=dry_run, coin=COIN)
    state = load_state()
    msgs = []

    # 数据
    bars = fetch_btc_1h_bars(api_key, N_BARS)
    if len(bars) < 250:
        print(f"  数据不足 ({len(bars)} 根), 本轮跳过")  # E1 数据守门
        sys.exit(0)
    px_now = bars[-1]["close"]
    print(f"  {len(bars)} 根 1h K线 | BTC ${px_now:,.2f}")

    # 权益
    if dry_run:
        equity = float(os.environ.get("DRY_EQUITY", "1000"))
    else:
        equity = broker.equity()
    print(f"  权益: ${equity:,.2f}")

    # 首次运行: 锚点 + 记录本金
    if not state.get("started"):
        state["started"] = True
        state["anchor_ts"] = bars[-2]["ts"]
        state["base_equity"] = equity
        if not dry_run:
            broker.set_leverage(exec_risk.MAX_LEVERAGE)
        tg(f"🤖 <b>F6 自动交易机器人已启动</b> ({'DRY_RUN 演习' if dry_run else '⚡️ 实盘'})\n"
           f"━━━━━━━━━━━━━━━\n"
           f"交易所: Hyperliquid{' Testnet' if testnet else ''}\n"
           f"标的: {COIN}-PERP | 权益: <code>${equity:,.2f}</code>\n"
           f"单笔风险: {exec_risk.RISK_PCT:.0%} | 杠杆上限: {exec_risk.MAX_LEVERAGE}x\n"
           f"日亏上限: {exec_risk.DAILY_LOSS_LIMIT_R}R | 连亏熔断: {exec_risk.LOSS_STREAK_HALT} 笔\n"
           f"锚点: <code>{bars[-2]['date']} UTC</code> (之前的信号不处理)")
        save_state(state)
        return

    # 账本自洽 (仅 live)
    ledger_ok = True
    if not dry_run:
        ledger_ok, diff = exec_risk.ledger_check(state, equity)
        if not ledger_ok:
            msgs.append(f"🚨 <b>账本对不上</b>: 实际权益与记账差 <code>${diff:+,.2f}</code>\n"
                        f"本轮暂停开新单 (已有持仓的保护单不受影响)。\n"
                        f"若是手动出入金, 请把 live_state.json 的 base_equity 调整后恢复。")

    # 对账推进状态
    if dry_run:
        # 纸面模式: 用 K 线模拟成交 (与 signal_bot 相同规则)
        for sig in state["signals"]:
            if sig["status"] not in ("waiting", "entered"):
                continue
            old = sig["status"]
            if update_signal_status(bars, sig):
                if sig["status"] == "entered":
                    msgs.append(f"🟢 [演习] 已入场 {sig['direction']} @ ${sig['entry_price']:,.2f}")
                elif sig["status"] in ("tp_hit", "sl_hit"):
                    r = sig["result_r"]
                    pnl = equity * exec_risk.RISK_PCT * r
                    _close_trade(state, sig, sig["status"], sig["exit_price"], pnl, msgs)
                elif sig["status"] in ("expired", "invalidated"):
                    msgs.append(f"⚪️ [演习] 信号作废 ({sig['status']})")
    else:
        msgs.extend(reconcile(state, broker, bars))

    # 开新单
    if ledger_ok:
        try_open_new(state, broker, bars, equity, msgs, dry_run)

    for m in msgs:
        prefix = "🎬 [演习] " if dry_run and not m.startswith(("🎬", "📡")) else ""
        tg((("🎬 <b>[演习模式]</b>\n" if dry_run else "") + m) if m.startswith("📡") else prefix + m)

    save_state(state)
    n_active = sum(1 for s in state["signals"] if s["status"] in ("waiting", "entered"))
    print(f"  done. 活跃信号 {n_active} | 历史成交 {len(state['trades'])} 笔 | "
          f"{len(msgs)} 条播报")


if __name__ == "__main__":
    main()
