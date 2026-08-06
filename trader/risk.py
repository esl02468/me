"""Risk rails, including prop-firm rule profiles.

Every account in config.json carries a rules profile. The engine refuses to
send an order that would violate the account's rules, and force-flattens
when a hard limit is hit. Numbers are per-firm: fill them in from YOUR
firm's current published rules — they change, and the copy in a config file
never overrides what the firm actually enforces.

IMPORTANT: many prop firms restrict or forbid fully automated trading
(and some forbid trade copying between firms). Automating a funded account
without checking your firm's automation policy can forfeit the account.
This module enforces loss limits; it cannot make automation allowed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PropRules:
    """Common prop-firm constraint set. None = rule not applicable."""

    max_contracts: int = 1
    daily_loss_limit: float | None = None      # $ — stop trading for the day
    trailing_drawdown: float | None = None     # $ below high-water mark
    flatten_by: str | None = "15:55"           # HH:MM exchange time (Chicago)
    max_trades_per_day: int | None = None
    allow_automation: bool = False             # set true ONLY after checking your firm's policy

    @classmethod
    def from_dict(cls, d: dict) -> "PropRules":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class AccountState:
    realized_today: float = 0.0
    high_water: float = 0.0
    equity: float = 0.0
    trades_today: int = 0
    halted: str | None = None  # reason, once tripped — stays tripped all day


@dataclass
class RiskManager:
    rules: PropRules
    state: AccountState = field(default_factory=AccountState)

    def record_fill(self, pnl: float) -> None:
        self.state.realized_today += pnl
        self.state.equity += pnl
        self.state.high_water = max(self.state.high_water, self.state.equity)
        self.state.trades_today += 1

    def pre_trade_check(self, qty: int, now: float | None = None) -> str | None:
        """Return a refusal reason, or None if the order may go out."""
        r, s = self.rules, self.state
        if s.halted:
            return f"halted: {s.halted}"
        if not r.allow_automation:
            return "automation not enabled for this account (allow_automation=false)"
        if qty > r.max_contracts:
            return f"qty {qty} > max_contracts {r.max_contracts}"
        if r.max_trades_per_day and s.trades_today >= r.max_trades_per_day:
            return "max trades per day reached"
        if r.daily_loss_limit is not None and s.realized_today <= -abs(r.daily_loss_limit):
            s.halted = "daily loss limit"
            return s.halted
        if (r.trailing_drawdown is not None
                and s.equity <= s.high_water - abs(r.trailing_drawdown)):
            s.halted = "trailing drawdown"
            return s.halted
        if r.flatten_by:
            hh, mm = map(int, r.flatten_by.split(":"))
            t = time.localtime(now or time.time())
            if (t.tm_hour, t.tm_min) >= (hh, mm):
                return f"past flatten_by {r.flatten_by}"
        return None

    def must_flatten(self, now: float | None = None) -> bool:
        r, s = self.rules, self.state
        if s.halted:
            return True
        if r.daily_loss_limit is not None and s.realized_today <= -abs(r.daily_loss_limit):
            s.halted = "daily loss limit"
            return True
        if (r.trailing_drawdown is not None
                and s.equity <= s.high_water - abs(r.trailing_drawdown)):
            s.halted = "trailing drawdown"
            return True
        if r.flatten_by:
            hh, mm = map(int, r.flatten_by.split(":"))
            t = time.localtime(now or time.time())
            if (t.tm_hour, t.tm_min) >= (hh, mm):
                return True
        return False
