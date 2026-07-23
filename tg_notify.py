"""
Telegram 消息推送模块 (HTML 格式 - 比 Markdown 鲁棒)
"""
import os
import json
import html as html_module
import urllib.request
import urllib.parse
import urllib.error


def _esc(s) -> str:
    """HTML 转义"""
    return html_module.escape(str(s), quote=False)


def _chat_ids(chat_id=None):
    """解析目标 chat_id 列表。
    TELEGRAM_CHAT_ID 支持逗号分隔多个; 另外可选 TELEGRAM_GROUP_CHAT_ID 追加群,
    两者都支持逗号分隔, 自动去重。这样加群时只需新增 GROUP 密钥, 不动原私聊密钥。"""
    raw = chat_id if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID") or ""
    extra = "" if chat_id is not None else (os.environ.get("TELEGRAM_GROUP_CHAT_ID") or "")
    ids, seen = [], set()
    for part in str(raw).split(",") + str(extra).split(","):
        c = part.strip()
        if c and c not in seen:
            seen.add(c)
            ids.append(c)
    return ids


def _post_message(text, bot_token, one_chat_id, parse_mode):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": one_chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                return True
            print(f"[TG ERROR chat={one_chat_id}] {result}")
            return False
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()
        except Exception:
            pass
        print(f"[TG HTTP {e.code} chat={one_chat_id}] {body[:200]}")
        return False
    except Exception as e:
        print(f"[TG FAIL chat={one_chat_id}] {type(e).__name__}: {e}")
        return False


def send_message(text: str, bot_token: str = None, chat_id: str = None,
                  parse_mode: str = "HTML") -> bool:
    """发一条消息到 TG 的一个或多个目标 (私聊/群, 逗号分隔)。任一成功即返回 True。"""
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    targets = _chat_ids(chat_id)
    if not bot_token or not targets:
        print(f"[WARN] 缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID, 转 stdout:\n{text}\n")
        return False
    ok_any = False
    for cid in targets:
        if _post_message(text, bot_token, cid, parse_mode):
            ok_any = True
    return ok_any


def _post_photo(photo_bytes, caption, bot_token, one_chat_id, parse_mode):
    boundary = "----tgFormBoundary7d93a1c2e4"
    parts = []
    for k, v in (("chat_id", one_chat_id), ("caption", caption[:1024]),
                 ("parse_mode", parse_mode)):
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        )
    parts.append(
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
         f"filename=\"report.png\"\r\nContent-Type: image/png\r\n\r\n").encode()
        + photo_bytes + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            if result.get("ok"):
                return True
            print(f"[TG PHOTO ERROR chat={one_chat_id}] {result}")
            return False
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode()
        except Exception:
            pass
        print(f"[TG PHOTO HTTP {e.code} chat={one_chat_id}] {body_txt[:200]}")
        return False
    except Exception as e:
        print(f"[TG PHOTO FAIL chat={one_chat_id}] {type(e).__name__}: {e}")
        return False


def send_photo(photo_bytes: bytes, caption: str = "", bot_token: str = None,
               chat_id: str = None, parse_mode: str = "HTML") -> bool:
    """发一张图 + caption 到一个或多个 TG 目标 (私聊/群, 逗号分隔)。任一成功即 True。
    TG caption 上限 1024 字符, 超出会被截断。"""
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    targets = _chat_ids(chat_id)
    if not bot_token or not targets:
        print(f"[WARN] 缺少 TG 配置, 图片未发, caption 转 stdout:\n{caption}\n")
        return False
    ok_any = False
    for cid in targets:
        if _post_photo(photo_bytes, caption, bot_token, cid, parse_mode):
            ok_any = True
    return ok_any


# ============ 消息模板 (HTML 格式) ============

def msg_signal_formed(signal_no: int, sig: dict, account_pct: float = 0.02) -> str:
    direction = "📈 做多" if sig["direction"] == "long" else "📉 做空"
    side = "Long" if sig["direction"] == "long" else "Short"
    trigger = sig["trigger_price"]
    sl = sig["sl"]
    tp = sig["tp"]
    sl_pct = abs(trigger - sl) / trigger * 100
    tp_pct = abs(tp - trigger) / trigger * 100
    expires = sig.get("expires_at", "+3小时")

    return (
        f"🔔 <b>新信号 #{signal_no:03d}</b> — F6 策略\n"
        f"━━━━━━━━━━━━━━━\n"
        f"方向: {direction} ({side})\n"
        f"形态: {_esc(sig['pattern_desc'])}\n"
        f"时间: <code>{_esc(sig['signal_time'])}</code>\n\n"
        f"📍 <b>入场触发位</b>: <code>${trigger:,.2f}</code> (限价单)\n"
        f"🛑 <b>止损</b>: <code>${sl:,.2f}</code> ({sl_pct:.2f}%)\n"
        f"🎯 <b>止盈</b>: <code>${tp:,.2f}</code> (+{tp_pct:.2f}%, 2R)\n\n"
        f"⚙️ 仓位: 风险每笔账户 <b>{account_pct*100:.1f}%</b>\n"
        f"⏱ 突破窗口: {_esc(expires)} 前有效\n\n"
        f"<i>等待价格突破触发位...</i>"
    )


def msg_entered(signal_no: int, sig: dict, fill_price: float, fill_time: str) -> str:
    direction = "📈 做多" if sig["direction"] == "long" else "📉 做空"
    return (
        f"✅ <b>入场成功 #{signal_no:03d}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"方向: {direction}\n"
        f"成交价: <code>${fill_price:,.2f}</code>\n"
        f"成交时间: <code>{_esc(fill_time)}</code>\n\n"
        f"持仓中, 等待 TP/SL 触发..."
    )


def msg_tp_hit(signal_no: int, sig: dict, exit_price: float, exit_time: str,
                hold_hours: float, stats: dict) -> str:
    return (
        f"🎯 <b>止盈触发 #{signal_no:03d}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"出场: <code>${exit_price:,.2f}</code>\n"
        f"盈亏: <b>+2.00R</b> ✅\n"
        f"持仓: {hold_hours:.1f} 小时\n\n"
        f"📊 战绩: {stats['wins']}胜 {stats['losses']}败 / "
        f"胜率 {stats['win_rate']*100:.1f}% / 累计 <b>+{stats['total_r']:.2f}R</b>"
    )


def msg_sl_hit(signal_no: int, sig: dict, exit_price: float, exit_time: str,
                hold_hours: float, stats: dict) -> str:
    return (
        f"🛑 <b>止损触发 #{signal_no:03d}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"出场: <code>${exit_price:,.2f}</code>\n"
        f"盈亏: <b>-1.00R</b> ❌\n"
        f"持仓: {hold_hours:.1f} 小时\n\n"
        f"📊 战绩: {stats['wins']}胜 {stats['losses']}败 / "
        f"胜率 {stats['win_rate']*100:.1f}% / 累计 <b>{stats['total_r']:+.2f}R</b>"
    )


def msg_expired(signal_no: int, sig: dict) -> str:
    return (
        f"⚠️ <b>信号作废 #{signal_no:03d}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"原因: 3 小时内未触发突破\n"
        f"信号形成时间: <code>{_esc(sig['signal_time'])}</code>\n\n"
        f"<i>未入场, 不计入战绩</i>"
    )


def msg_invalidated_by_sl(signal_no: int, sig: dict) -> str:
    return (
        f"⚠️ <b>信号作废 #{signal_no:03d}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"原因: 突破前价格反向触及止损位\n"
        f"信号形成时间: <code>{_esc(sig['signal_time'])}</code>\n\n"
        f"<i>未入场, 不计入战绩</i>"
    )


def msg_final_report(stats: dict) -> str:
    expected_wr = 0.467
    expected_avg_r = 0.36
    actual_wr = stats["win_rate"]
    actual_avg_r = stats["total_r"] / stats["completed"] if stats["completed"] else 0
    diff_assess = "✅ 符合预期" if abs(actual_wr - expected_wr) < 0.1 else "⚠️ 偏离预期"

    return (
        f"📊 <b>F6 实盘战报 — 10 信号完成</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"期间: <code>{_esc(stats['period'])}</code>\n\n"
        f"总信号: {stats['total_signals']}\n"
        f"完成交易: {stats['completed']}\n"
        f"作废: {stats['expired']}\n\n"
        f"🏆 胜利: {stats['wins']} ({actual_wr*100:.1f}%)\n"
        f"💀 失败: {stats['losses']}\n"
        f"💰 累计 R: <b>{stats['total_r']:+.2f}R</b>\n"
        f"📈 单笔均 R: <b>{actual_avg_r:+.3f}R</b>\n\n"
        f"最大连胜: {stats['max_win_streak']}\n"
        f"最大连败: {stats['max_loss_streak']}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"<b>vs 3 年回测预期</b>:\n"
        f"  胜率: 实盘 {actual_wr*100:.1f}% vs 预期 46.7%\n"
        f"  均 R: 实盘 {actual_avg_r:+.3f}R vs 预期 +0.36R\n"
        f"  评估: {diff_assess}"
    )


if __name__ == "__main__":
    test = {
        "direction": "long",
        "pattern_desc": "看涨反转 (急跌后底部缠绕)",
        "signal_time": "2026-05-10 14:00 UTC",
        "trigger_price": 103450,
        "sl": 101200,
        "tp": 107950,
        "expires_at": "2026-05-10 17:00 UTC",
    }
    print(msg_signal_formed(1, test))
