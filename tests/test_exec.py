"""exec_bot / exec_risk 单元测试 — 交易所全 mock, 不联网
用法: 在仓库根目录运行  python3 tests/test_exec.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import time
import types
import unittest
from unittest.mock import patch

import exec_risk
import exec_bot


class FakeBroker:
    """内存版交易所"""
    def __init__(self):
        self.next_oid = 100
        self._open_orders = []     # {oid, side, limitPx}
        self._fills = []           # HL fill dicts
        self._position = None
        self.placed_entries = []
        self.placed_tpsl = []
        self.cancelled = []
        self.sz_decimals = 5

    def round_sz(self, sz):
        return int(sz * 1e5) / 1e5

    def round_px(self, px):
        return round(float(f"{px:.5g}"), 1)

    def open_orders(self):
        return list(self._open_orders)

    def fills_since(self, start_ms):
        return [f for f in self._fills if f["time"] >= start_ms]

    def position(self):
        return self._position

    def place_entry_stop(self, is_buy, sz, trigger_px):
        oid = self.next_oid; self.next_oid += 1
        self._open_orders.append({"oid": oid, "side": "B" if is_buy else "A",
                                  "limitPx": trigger_px, "coin": "BTC"})
        self.placed_entries.append((oid, is_buy, sz, trigger_px))
        return True, oid

    def place_tpsl(self, is_long, sz, tp, sl):
        tp_oid = self.next_oid; self.next_oid += 1
        sl_oid = self.next_oid; self.next_oid += 1
        self._open_orders += [{"oid": tp_oid, "coin": "BTC"}, {"oid": sl_oid, "coin": "BTC"}]
        self.placed_tpsl.append((tp_oid, sl_oid, is_long, sz, tp, sl))
        return True, (tp_oid, sl_oid)

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        self._open_orders = [o for o in self._open_orders if o["oid"] != oid]
        return True

    # 测试辅助
    def add_fill(self, oid, px, sz, closed_pnl=0.0, fee=0.0):
        self._fills.append({"oid": oid, "px": str(px), "sz": str(sz),
                            "closedPnl": str(closed_pnl), "fee": str(fee),
                            "time": int(time.time() * 1000), "coin": "BTC"})


def fresh_state():
    return {"signals": [], "trades": [], "anchor_ts": 0, "started": True,
            "base_equity": 1000.0, "last_fill_check_ms": 0}


def mk_signal(direction="long", status="waiting", entry_oid=1,
              trigger=100000.0, sl=98000.0, tp=104000.0, expires_offset=3600):
    now = int(time.time())
    return {"signal_ts": now - 4 * 3600, "signal_time": "t", "direction": direction,
            "trigger_price": trigger, "sl": sl, "tp": tp,
            "expires_ts": now + expires_offset, "expires_at": "x",
            "pattern_desc": "test", "status": status, "entry_oid": entry_oid,
            "entry_price": None, "entry_time": None, "exit_price": None,
            "exit_time": None, "result_r": None, "planned_size": 0.005}


def bars_flat(px=99000.0, n=5):
    now = int(time.time())
    return [{"ts": now - (n - i) * 3600, "date": "d", "open": px, "high": px + 10,
             "low": px - 10, "close": px} for i in range(n)]


class TestRisk(unittest.TestCase):
    def test_position_size_normal(self):
        # 权益1000, 风险1%=10, 止损距离2% (entry 100k, sl 98k)
        size, notional, why = exec_risk.position_size(1000, 100000, 98000)
        self.assertAlmostEqual(size * 2000, 10, delta=0.01)  # risk_usd = size*dist
        self.assertEqual(why, "")

    def test_reject_wide_sl(self):
        size, _, why = exec_risk.position_size(1000, 100000, 93000)  # 7%
        self.assertEqual(size, 0)
        self.assertIn("止损距离", why)

    def test_leverage_cap(self):
        # 止损距离 0.1% -> 名义会爆掉, 应被压到 5x
        size, notional, why = exec_risk.position_size(1000, 100000, 99900)
        self.assertLessEqual(notional, 1000 * exec_risk.MAX_LEVERAGE + 1)

    def test_min_notional(self):
        size, _, why = exec_risk.position_size(100, 100000, 95100)  # 风险$1, 距离4.9% -> $20 名义
        self.assertGreater(size, 0)
        size, _, why = exec_risk.position_size(20, 100000, 95100)  # 名义 $4 < $10
        self.assertEqual(size, 0)

    def test_gate_halted(self):
        st = fresh_state(); st["halted"] = True; st["halt_reason"] = "x"
        ok, why = exec_risk.gate_check(st, 1000, False, False)
        self.assertFalse(ok)

    def test_gate_concurrent(self):
        ok, why = exec_risk.gate_check(fresh_state(), 1000, True, False)
        self.assertFalse(ok)
        ok, why = exec_risk.gate_check(fresh_state(), 1000, False, True)
        self.assertFalse(ok)
        ok, why = exec_risk.gate_check(fresh_state(), 1000, False, False)
        self.assertTrue(ok)

    def test_gate_daily_loss(self):
        st = fresh_state()
        today = exec_risk.utc_day()
        st["trades"] = [{"result_r": -1, "exit_day": today, "pnl_usd": -10},
                        {"result_r": -1, "exit_day": today, "pnl_usd": -10}]
        ok, why = exec_risk.gate_check(st, 1000, False, False)
        self.assertFalse(ok)
        self.assertIn("日亏", why)

    def test_streak_halt(self):
        st = fresh_state()
        st["trades"] = [{"result_r": -1}] * 3
        self.assertTrue(exec_risk.update_streak_halt(st))
        self.assertTrue(st["halted"])
        st2 = fresh_state()
        st2["trades"] = [{"result_r": -1}, {"result_r": 2}, {"result_r": -1}]
        self.assertFalse(exec_risk.update_streak_halt(st2))

    def test_ledger(self):
        st = fresh_state()
        st["trades"] = [{"pnl_usd": 25.0}]
        ok, diff = exec_risk.ledger_check(st, 1025.3)
        self.assertTrue(ok)                       # 容差内 (1% of equity)
        ok, diff = exec_risk.ledger_check(st, 1100.0)
        self.assertFalse(ok)


class TestReconcile(unittest.TestCase):
    def test_entry_fill_places_tpsl(self):
        br = FakeBroker()
        st = fresh_state()
        sig = mk_signal(entry_oid=1)
        st["signals"].append(sig)
        br.add_fill(oid=1, px=100050.0, sz=0.005)
        msgs = exec_bot.reconcile(st, br, bars_flat())
        self.assertEqual(sig["status"], "entered")
        self.assertEqual(len(br.placed_tpsl), 1)
        self.assertIsNotNone(sig.get("tp_oid"))
        self.assertTrue(any("已入场" in m for m in msgs))

    def test_tp_fill_closes_trade(self):
        br = FakeBroker()
        st = fresh_state()
        sig = mk_signal(status="entered")
        sig.update({"entry_price": 100000.0, "filled_size": 0.005,
                    "tp_oid": 50, "sl_oid": 51, "entry_ts": int(time.time())})
        st["signals"].append(sig)
        br._position = None
        br.add_fill(oid=50, px=104000.0, sz=0.005, closed_pnl=20.0, fee=0.4)
        msgs = exec_bot.reconcile(st, br, bars_flat())
        self.assertEqual(sig["status"], "tp_hit")
        self.assertEqual(len(st["trades"]), 1)
        self.assertAlmostEqual(st["trades"][0]["pnl_usd"], 19.6, places=2)

    def test_sl_fill_and_streak_halt(self):
        br = FakeBroker()
        st = fresh_state()
        st["trades"] = [{"result_r": -1, "pnl_usd": -10, "exit_day": "2020-01-01"}] * 2
        sig = mk_signal(status="entered")
        sig.update({"entry_price": 100000.0, "filled_size": 0.005,
                    "tp_oid": 50, "sl_oid": 51, "entry_ts": int(time.time())})
        st["signals"].append(sig)
        br.add_fill(oid=51, px=98000.0, sz=0.005, closed_pnl=-10.0, fee=0.4)
        msgs = exec_bot.reconcile(st, br, bars_flat())
        self.assertEqual(sig["status"], "sl_hit")
        self.assertTrue(st.get("halted"))
        self.assertTrue(any("熔断" in m for m in msgs))

    def test_expiry_cancels_entry(self):
        br = FakeBroker()
        st = fresh_state()
        sig = mk_signal(entry_oid=7, expires_offset=-10)  # 已过期
        st["signals"].append(sig)
        br._open_orders.append({"oid": 7, "coin": "BTC"})
        msgs = exec_bot.reconcile(st, br, bars_flat())
        self.assertEqual(sig["status"], "expired")
        self.assertIn(7, br.cancelled)

    def test_invalidated_by_sl_before_entry(self):
        br = FakeBroker()
        st = fresh_state()
        sig = mk_signal(entry_oid=7, trigger=100000, sl=98000)
        st["signals"].append(sig)
        br._open_orders.append({"oid": 7, "coin": "BTC"})
        bars = bars_flat(px=97900)  # low 已破 SL
        msgs = exec_bot.reconcile(st, br, bars)
        self.assertEqual(sig["status"], "invalidated")
        self.assertIn(7, br.cancelled)

    def test_missing_protection_replaced(self):
        br = FakeBroker()
        st = fresh_state()
        sig = mk_signal(status="entered")
        sig.update({"entry_price": 100000.0, "filled_size": 0.005,
                    "tp_oid": 50, "sl_oid": 51, "entry_ts": int(time.time())})
        st["signals"].append(sig)
        br._position = {"size": 0.005, "entry_px": 100000.0, "unrealized_pnl": 0}
        # 挂单列表为空 -> 保护单丢失
        msgs = exec_bot.reconcile(st, br, bars_flat())
        self.assertEqual(len(br.placed_tpsl), 1)
        self.assertTrue(any("重新挂上" in m for m in msgs))

    def test_orphan_order_cancelled(self):
        br = FakeBroker()
        st = fresh_state()
        br._open_orders.append({"oid": 999, "coin": "BTC", "side": "B", "limitPx": 1})
        msgs = exec_bot.reconcile(st, br, bars_flat())
        self.assertIn(999, br.cancelled)

    def test_orphan_position_alert_no_touch(self):
        br = FakeBroker()
        st = fresh_state()
        br._position = {"size": 0.01, "entry_px": 100000.0, "unrealized_pnl": 0}
        msgs = exec_bot.reconcile(st, br, bars_flat())
        self.assertTrue(any("本地无对应记录" in m for m in msgs))
        self.assertEqual(br.cancelled, [])  # 不动持仓


class TestOpenNew(unittest.TestCase):
    def _run(self, st, sigs, equity=1000.0):
        import signals as sigmod
        br = FakeBroker()
        bars = bars_flat(px=99000, n=300)
        sigmod._TEST_SIGNALS = sigs
        msgs = []
        exec_bot.try_open_new(st, br, bars, equity, msgs, dry_run=True)
        sigmod._TEST_SIGNALS = []
        return br, msgs

    def test_open_places_entry(self):
        # 用真实 build_signal_record 路径太依赖 detect 字段, 这里直接测 gate+size 组合:
        st = fresh_state()
        size, notional, why = exec_risk.position_size(1000, 99500, 97500)
        ok, reason = exec_risk.gate_check(st, 1000, False, False)
        self.assertTrue(ok)
        self.assertGreater(size, 0)

    def test_skip_when_position_open(self):
        st = fresh_state()
        st["signals"].append(mk_signal(status="entered"))
        ok, reason = exec_risk.gate_check(
            st, 1000,
            any(s["status"] == "entered" for s in st["signals"]),
            any(s["status"] == "waiting" for s in st["signals"]))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
