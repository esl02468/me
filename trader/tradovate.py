"""Minimal Tradovate REST client (stdlib only).

Talks to the DEMO environment by default. The live URL is only used when
TRADOVATE_LIVE=YES_I_UNDERSTAND is set in the environment — an explicit,
typed acknowledgment, not a config flag you can flip by accident.

Auth uses the credential fields from your Tradovate API access settings
(API access is a paid add-on on funded accounts; the demo works with a
free practice account + API key).
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

DEMO_URL = "https://demo.tradovateapi.com/v1"
LIVE_URL = "https://live.tradovateapi.com/v1"


class TradovateError(RuntimeError):
    pass


class TradovateClient:
    def __init__(self, config: dict):
        self.cfg = config
        live_ack = os.environ.get("TRADOVATE_LIVE", "") == "YES_I_UNDERSTAND"
        self.base = LIVE_URL if (config.get("live") and live_ack) else DEMO_URL
        self.token: str | None = None
        self.token_expiry = 0.0

    # -- plumbing ---------------------------------------------------------
    def _call(self, path: str, payload: dict | None = None, method: str = "POST") -> dict:
        url = f"{self.base}/{path}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise TradovateError(f"{path}: HTTP {e.code} {e.read().decode()[:200]}") from e

    # -- auth -------------------------------------------------------------
    def authenticate(self) -> None:
        body = {
            "name": self.cfg["username"],
            "password": self.cfg["password"],
            "appId": self.cfg.get("app_id", "nq-toolkit"),
            "appVersion": "1.0",
            "cid": self.cfg["cid"],
            "sec": self.cfg["secret"],
        }
        out = self._call("auth/accesstokenrequest", body)
        if "accessToken" not in out:
            raise TradovateError(f"auth failed: {out}")
        self.token = out["accessToken"]
        self.token_expiry = time.time() + 60 * 60  # tokens last ~80 min; refresh at 60

    def ensure_auth(self) -> None:
        if not self.token or time.time() > self.token_expiry:
            self.authenticate()

    # -- account & orders -------------------------------------------------
    def accounts(self) -> list[dict]:
        self.ensure_auth()
        return self._call("account/list", method="GET", payload=None)

    def place_market(self, account_id: int, symbol: str, qty: int, side: str) -> dict:
        """side: 'Buy' | 'Sell'."""
        self.ensure_auth()
        return self._call("order/placeorder", {
            "accountId": account_id,
            "action": side,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,   # required disclosure for automated orders
        })

    def place_bracket(
        self, account_id: int, symbol: str, qty: int, side: str,
        take_profit: float, stop_loss: float,
    ) -> dict:
        """Market entry + OCO take-profit/stop via orderstrategy."""
        self.ensure_auth()
        brackets = [{
            "qty": qty,
            "profitTarget": take_profit,
            "stopLoss": stop_loss,
            "trailingStop": False,
        }]
        return self._call("orderstrategy/startorderstrategy", {
            "accountId": account_id,
            "symbol": symbol,
            "action": side,
            "orderStrategyTypeId": 2,  # bracket
            "params": json.dumps({"entryVersion": {"orderQty": qty, "orderType": "Market"},
                                   "brackets": brackets}),
        })

    def flatten(self, account_id: int, symbol: str) -> dict:
        self.ensure_auth()
        return self._call("order/liquidateposition", {
            "accountId": account_id, "symbol": symbol, "admin": False,
        })
