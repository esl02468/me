"""Reversal-likelihood ranking for chart levels.

For each level (user + auto) this measures how price has actually treated
the level *today*, then blends in structural context:

  empirical   touch episodes at the level -> bounce rate (rejection within
              the next 15 bars) vs. breaks (close beyond the band)
  kind        prior weight by level type (PDH/PDL > OR > VWAP > prev close)
  confluence  other levels stacked within 0.5 ATR
  bias        a support is likelier to hold when bias isn't bearish,
              a resistance when bias isn't bullish
  broken      levels already violated today lose credibility

Output is a 0-100 *confidence score* — a transparent heuristic, not a true
probability — with HIGH / MODERATE / LOW labels and a rank.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bias import BiasResult, atr
from .data import Candle, Session

LOOKAHEAD_BARS = 15  # bars after a touch in which a bounce must develop
BOUNCE_ATR = 1.0     # move away >= 1 ATR counts as a bounce
TOL_ATR = 0.3        # band half-width around the level
BREAK_TOL_MULT = 2.0  # close beyond tol*this = break

KIND_PRIOR = {
    "PDH": 15, "PDL": 15,
    "OR high": 12, "OR low": 12,
    "VWAP": 10,
    "Prev close": 8,
}
USER_PRIOR = 12       # user-drawn S/R is assumed deliberate
UNTESTED_EMPIRICAL = 15  # neutral prior when the level was never touched today


@dataclass
class LevelScore:
    price: float
    label: str
    kind: str
    user: bool
    touches: int = 0
    bounces: int = 0
    breaks: int = 0
    score: float = 0.0
    rating: str = "LOW"
    rank: int = 0
    distance: float | None = None  # points from current price (+above/-below)
    components: dict[str, float] = field(default_factory=dict)

    @property
    def bounce_rate(self) -> float | None:
        """Fraction of today's touches that bounced; None if never tested."""
        return self.bounces / self.touches if self.touches else None


def _touch_episodes(candles: list[Candle], level: float, tol: float) -> list[int]:
    """Indices where a touch episode *starts* (consecutive touching bars grouped)."""
    starts = []
    in_touch = False
    for i, c in enumerate(candles):
        touching = c.low <= level + tol and c.high >= level - tol
        if touching and not in_touch:
            starts.append(i)
        in_touch = touching
    return starts


def _classify_episode(
    candles: list[Candle], start: int, level: float, tol: float, a: float
) -> str:
    """'bounce', 'break', or 'fade' (drifted off without conviction)."""
    approach_above = start > 0 and candles[start - 1].close > level
    end = min(start + LOOKAHEAD_BARS, len(candles))
    for c in candles[start:end]:
        # Break: close beyond the far side of the band.
        if approach_above and c.close < level - tol * BREAK_TOL_MULT:
            return "break"
        if not approach_above and c.close > level + tol * BREAK_TOL_MULT:
            return "break"
        # Bounce: rejected back toward the approach side by >= 1 ATR.
        if approach_above and c.close > level + BOUNCE_ATR * a:
            return "bounce"
        if not approach_above and c.close < level - BOUNCE_ATR * a:
            return "bounce"
    return "fade"


def _rating(score: float) -> str:
    if score >= 70:
        return "HIGH"
    if score >= 45:
        return "MODERATE"
    return "LOW"


def rank_levels(
    session: Session,
    levels: list[dict],
    bias: BiasResult | None = None,
) -> list[LevelScore]:
    """Score and rank levels by reversal likelihood.

    `levels` entries: {"price": float, "label": str, "kind": str}, where
    kind == "auto" marks computed levels; anything else is user-drawn.
    """
    candles = session.candles
    price = session.last
    a = atr(candles) or 5.0
    tol = max(TOL_ATR * a, 1.0)

    out: list[LevelScore] = []
    prices = [float(l["price"]) for l in levels]

    for lv in levels:
        p = float(lv["price"])
        label = str(lv.get("label", ""))
        user = lv.get("kind") != "auto"
        ls = LevelScore(price=p, label=label, kind=str(lv.get("kind", "")), user=user)
        if price is not None:
            ls.distance = round(p - price, 2)

        # --- empirical behaviour today ---
        episodes = _touch_episodes(candles, p, tol)
        ls.touches = len(episodes)
        for s in episodes:
            outcome = _classify_episode(candles, s, p, tol, a)
            if outcome == "bounce":
                ls.bounces += 1
            elif outcome == "break":
                ls.breaks += 1
        if ls.touches:
            empirical = 40.0 * (ls.bounces / ls.touches)
        else:
            empirical = UNTESTED_EMPIRICAL

        # --- structural components ---
        prior = USER_PRIOR if user else KIND_PRIOR.get(label, 8)
        confluence = min(16.0, 8.0 * sum(
            1 for q in prices if q != p and abs(q - p) <= 0.5 * a
        ))
        broken_pen = -12.0 * min(ls.breaks, 2)

        align = 0.0
        if bias is not None and price is not None:
            is_support = p < price
            if is_support:
                align = 8.0 if bias.score >= 1 else (-8.0 if bias.score <= -3 else 0.0)
            else:
                align = 8.0 if bias.score <= -1 else (-8.0 if bias.score >= 3 else 0.0)

        ls.components = {
            "empirical": round(empirical, 1),
            "kind_prior": prior,
            "confluence": confluence,
            "bias_align": align,
            "broken_penalty": broken_pen,
        }
        ls.score = round(max(0.0, min(100.0, empirical + prior + confluence + align + broken_pen + 20.0)), 1)
        ls.rating = _rating(ls.score)
        out.append(ls)

    out.sort(key=lambda l: l.score, reverse=True)
    for i, ls in enumerate(out, 1):
        ls.rank = i
    return out


def filter_proven(scored: list[LevelScore], min_rate: float = 0.51) -> list[LevelScore]:
    """Keep only levels proven right more than `min_rate` of the time today.

    A level counts as "right" when a touch bounced. Untested levels are
    excluded — no touches means no evidence either way.
    """
    return [l for l in scored if l.bounce_rate is not None and l.bounce_rate > min_rate]
