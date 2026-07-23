"""
Hyperliquid 交易所适配层 — 只做三件事:
  1) 下/撤单 (入场触发单、TP/SL 组合单、紧急平仓)
  2) 查询 (权益、持仓、挂单、成交)
  3) 价格/数量按交易所规则取整

所有方法在 DRY_RUN 模式下只打日志不发请求。
依赖: pip install hyperliquid-python-sdk eth-account
"""
import json
import time

from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants


class HLBroker:
    def __init__(self, private_key: str = None, account_address: str = None,
                 testnet: bool = False, dry_run: bool = True, coin: str = "BTC"):
        self.dry_run = dry_run
        self.coin = coin
        base_url = constants.TESTNET_API_URL if testnet else constants.MAINNET_API_URL
        self.info = Info(base_url, skip_ws=True)

        # 取整规则: perp 价格最多 5 位有效数字, 小数位 <= 6 - szDecimals
        meta = self.info.meta()
        self.sz_decimals = None
        for asset in meta["universe"]:
            if asset["name"] == coin:
                self.sz_decimals = asset["szDecimals"]
                self.max_leverage = asset.get("maxLeverage", 20)
                break
        if self.sz_decimals is None:
            raise RuntimeError(f"找不到品种 {coin} 的元数据")
        self.px_decimals = max(0, 6 - self.sz_decimals)

        self.exchange = None
        self.address = account_address
        if private_key and not dry_run:
            from eth_account import Account
            wallet = Account.from_key(private_key)
            # API wallet (agent) 模式: account_address 是主账户地址
            self.address = account_address or wallet.address
            self.exchange = Exchange(wallet, base_url,
                                     account_address=self.address)
        elif private_key and dry_run:
            # dry_run 也允许带地址查权益
            from eth_account import Account
            wallet = Account.from_key(private_key)
            self.address = account_address or wallet.address

    # ---------- 取整 ----------

    def round_px(self, px: float) -> float:
        """5 位有效数字 + 小数位限制"""
        sig = float(f"{px:.5g}")
        return round(sig, self.px_decimals)

    def round_sz(self, sz: float) -> float:
        factor = 10 ** self.sz_decimals
        return int(sz * factor) / factor  # 向下取整, 不放大仓位

    # ---------- 查询 ----------

    def equity(self) -> float:
        """账户总权益 (USDC)"""
        if not self.address:
            return 0.0
        st = self.info.user_state(self.address)
        return float(st["marginSummary"]["accountValue"])

    def position(self) -> dict:
        """当前 coin 的持仓; 无仓返回 None"""
        if not self.address:
            return None
        st = self.info.user_state(self.address)
        for p in st.get("assetPositions", []):
            pos = p["position"]
            if pos["coin"] == self.coin and float(pos["szi"]) != 0:
                return {
                    "size": float(pos["szi"]),      # 正=多, 负=空
                    "entry_px": float(pos["entryPx"]),
                    "unrealized_pnl": float(pos["unrealizedPnl"]),
                }
        return None

    def open_orders(self) -> list:
        if not self.address:
            return []
        oo = self.info.frontend_open_orders(self.address)
        return [o for o in oo if o.get("coin") == self.coin]

    def fills_since(self, start_ms: int) -> list:
        if not self.address:
            return []
        fills = self.info.user_fills_by_time(self.address, start_ms)
        return [f for f in fills if f.get("coin") == self.coin]

    def mid_price(self) -> float:
        mids = self.info.all_mids()
        return float(mids[self.coin])

    # ---------- 下单 ----------

    def _check(self, resp, what):
        """校验下单响应, 返回 (ok, oid or err)"""
        try:
            if resp.get("status") != "ok":
                return False, str(resp)
            statuses = resp["response"]["data"]["statuses"]
            st = statuses[0]
            if "error" in st:
                return False, st["error"]
            oid = (st.get("resting") or st.get("filled") or {}).get("oid")
            return True, oid
        except Exception as e:
            return False, f"{what} 响应解析失败: {e} / {resp}"

    def place_entry_stop(self, is_buy: bool, sz: float, trigger_px: float) -> tuple:
        """入场触发单: 价格突破 trigger_px 时市价入场 (交易所侧执行, 机器人离线也生效)"""
        sz = self.round_sz(sz)
        trigger_px = self.round_px(trigger_px)
        if self.dry_run:
            print(f"  [DRY_RUN] 入场触发单: {'买' if is_buy else '卖'} {sz} {self.coin} @ 触发 {trigger_px}")
            return True, -1
        # 突破入场方向上是"停损单"语义: tpsl="sl" + 非 reduce-only
        order_type = {"trigger": {"triggerPx": trigger_px, "isMarket": True, "tpsl": "sl"}}
        resp = self.exchange.order(self.coin, is_buy, sz, trigger_px, order_type,
                                   reduce_only=False)
        return self._check(resp, "入场触发单")

    def place_tpsl(self, is_long: bool, sz: float, tp_px: float, sl_px: float) -> tuple:
        """持仓后挂 TP/SL 组合 (positionTpsl 分组, 一个成交自动撤另一个)"""
        sz = self.round_sz(sz)
        tp_px = self.round_px(tp_px)
        sl_px = self.round_px(sl_px)
        close_is_buy = not is_long
        if self.dry_run:
            print(f"  [DRY_RUN] TP/SL: TP@{tp_px} SL@{sl_px} sz={sz}")
            return True, (-1, -1)
        # SL 市价触发保证成交; TP 限价触发拿更好价格
        slippage_px = self.round_px(sl_px * (1.05 if close_is_buy else 0.95))
        orders = [
            {"coin": self.coin, "is_buy": close_is_buy, "sz": sz, "limit_px": tp_px,
             "order_type": {"trigger": {"triggerPx": tp_px, "isMarket": False, "tpsl": "tp"}},
             "reduce_only": True},
            {"coin": self.coin, "is_buy": close_is_buy, "sz": sz, "limit_px": slippage_px,
             "order_type": {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
             "reduce_only": True},
        ]
        resp = self.exchange.bulk_orders(orders, grouping="positionTpsl")
        try:
            if resp.get("status") != "ok":
                return False, str(resp)
            statuses = resp["response"]["data"]["statuses"]
            oids = []
            for st in statuses:
                if "error" in st:
                    return False, st["error"]
                oids.append((st.get("resting") or st.get("filled") or {}).get("oid"))
            return True, tuple(oids)
        except Exception as e:
            return False, f"TPSL 响应解析失败: {e} / {resp}"

    def cancel_order(self, oid: int) -> bool:
        if self.dry_run:
            print(f"  [DRY_RUN] 撤单 oid={oid}")
            return True
        resp = self.exchange.cancel(self.coin, oid)
        return resp.get("status") == "ok"

    def cancel_all(self):
        for o in self.open_orders():
            self.cancel_order(o["oid"])

    def market_close_all(self) -> tuple:
        """紧急市价全平"""
        if self.dry_run:
            print(f"  [DRY_RUN] 市价全平 {self.coin}")
            return True, None
        resp = self.exchange.market_close(self.coin)
        return self._check(resp, "市价平仓")

    def set_leverage(self, lev: int):
        if self.dry_run:
            return
        self.exchange.update_leverage(min(lev, self.max_leverage), self.coin, is_cross=True)
