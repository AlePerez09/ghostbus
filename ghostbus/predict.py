"""How Ghost Bus predicts arrivals, in one place (used by the app AND by the accuracy auditor,
so the accuracy we publish is the accuracy riders actually get).

The idea, in plain words:
  1. If we can see the bus, start from how late it is right now.
  2. Buses drift toward how late they *usually* are at your stop at this hour (learned from history).
     The farther away the bus is, the more we lean on that history.
  3. Give a time WINDOW, not a single time. It's narrow when the bus is close and widens with distance.
  4. If this exact trip often never shows up, say so ("ghost risk").
"""
import math
from dataclasses import dataclass, field

DEFAULT_PROFILE = {"n": 0, "p10": -60.0, "p50": 90.0, "p90": 420.0}   # when we have no history yet
TRUST_DECAY_MIN = 25          # after ~25 min of travel, current delay counts ~37%, history ~63%
MIN_HALF_WIDTH_S = 45
GROWTH_PER_MIN_S = 6          # window widens ~6 s per minute of travel left


@dataclass
class Prediction:
    delay_s: float                 # most likely delay at the stop
    lo_s: float                    # early end of the window (delay seconds)
    hi_s: float                    # late end of the window
    confidence: str                # "very likely" | "likely" | "rough" | "may not come"
    why: list = field(default_factory=list)   # 1–3 short plain-language sentences
    ghost_risk: float | None = None


def _mins(s):
    m = round(abs(s) / 60)
    return f"{m} min" if m != 1 else "1 min"


def lateness_phrase(delay_s):
    if abs(delay_s) < 60:
        return "on time"
    return f"{_mins(delay_s)} {'late' if delay_s > 0 else 'early'}"


def predict(*, live: bool, current_delay_s: float | None, minutes_away: float | None, stops_away: int | None,
            profile: dict | None, ghost_risk: float | None = None, ghost_days: tuple | None = None,
            started: bool = True) -> Prediction:
    prof = profile if profile and profile.get("n", 0) >= 5 else None
    p10, p50, p90 = ((prof["p10"], prof["p50"], prof["p90"]) if prof
                     else (DEFAULT_PROFILE["p10"], DEFAULT_PROFILE["p50"], DEFAULT_PROFILE["p90"]))
    why = []

    if live and current_delay_s is not None:
        m = max(0.0, minutes_away or 0.0)
        w = math.exp(-m / TRUST_DECAY_MIN)                 # how much we trust "right now"
        center = w * current_delay_s + (1 - w) * p50
        spread = max(60.0, (p90 - p10) / 2)
        half = MIN_HALF_WIDTH_S + (1 - w) * spread + GROWTH_PER_MIN_S * m
        lo, hi = center - 0.8 * half, center + 1.2 * half     # buses run late more often than early
        where = ("almost here" if not stops_away or stops_away <= 0
                 else "1 stop away" if stops_away == 1 else f"{stops_away} stops away")
        why.append(f"The bus is {where} and running {lateness_phrase(current_delay_s)}.")
        drift = center - current_delay_s
        if prof and abs(drift) >= 60 and w < 0.95:
            why.append(f"Buses here usually {'lose' if drift > 0 else 'make up'} about {_mins(drift)} "
                       f"by your stop at this hour, so we adjusted.")
    else:
        center, lo, hi = p50, p10, max(p90, p10 + 120)
        if prof:
            why.append(f"The bus hasn't {'reported in' if started else 'started its trip'} yet. At this hour it's "
                       f"usually {lateness_phrase(p50)} at this stop.")
        else:
            why.append("The bus hasn't started its trip yet, and we don't have much history for this stop, "
                       "so this is based on the schedule.")

    width_min = (hi - lo) / 60
    if ghost_risk is not None and ghost_risk >= 0.3:
        confidence = "may not come"
    elif live and width_min <= 4:
        confidence = "very likely"
    elif width_min <= 9:
        confidence = "likely"
    else:
        confidence = "rough"
    if ghost_risk is not None and ghost_risk >= 0.15 and ghost_days:
        missed, total = ghost_days
        why.append(f"Heads up: this trip didn't show up on {missed} of the last {total} days.")
    return Prediction(delay_s=center, lo_s=lo, hi_s=hi, confidence=confidence, why=why, ghost_risk=ghost_risk)


def horizon_band(minutes_away: float) -> int:
    """Group predictions by how far ahead they were made: 0–5, 5–10, 10–20, 20+ minutes."""
    return 5 if minutes_away <= 5 else 10 if minutes_away <= 10 else 20 if minutes_away <= 20 else 30
