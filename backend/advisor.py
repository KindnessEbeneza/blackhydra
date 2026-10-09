"""
advisor.py — Tomato-specific agronomic advice rules

Fires plain-language tips based on environmental conditions.
Each rule fires at most once per condition onset — same deduplication
pattern as detection.py.
"""

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


INFO     = "info"
WARNING  = "warning"
CRITICAL = "critical"


@dataclass
class Tip:
    id: str
    severity: str
    title: str
    reason: str
    action: str
    timestamp: float = field(default_factory=time.time)
    sim_time_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id":         self.id,
            "severity":   self.severity,
            "title":      self.title,
            "reason":     self.reason,
            "action":     self.action,
            "timestamp":  self.timestamp,
            "sim_time_s": self.sim_time_s,
        }


class Advisor:
    """
    Call ``update(state_dict)`` each tick.
    Access ``new_tips`` and ``all_active_tips`` after each call.
    """

    def __init__(self):
        self._active:     Dict[str, Tip] = {}
        self._cleared_at: Dict[str, float] = {}
        self.new_tips:        List[Tip] = []
        self.all_active_tips: List[Tip] = []

    def update(self, state: dict) -> None:
        self.new_tips = []
        sim_t   = state["sim_time_s"]
        weather = state["weather"]
        T       = weather["temp"]
        RH      = weather["humidity"]
        VPD     = weather["vpd"]
        hour    = state["sim_hour"]

        self._rule_heat(T, sim_t)
        self._rule_chill(T, hour, sim_t)
        self._rule_humidity(RH, hour, sim_t)
        self._rule_vpd(VPD, sim_t)
        self._rule_tank_low(state, sim_t)
        self._auto_clear(state, sim_t)

        self.all_active_tips = list(self._active.values())

    # ── Rules ─────────────────────────────────────────────────────────────────

    def _rule_heat(self, temp: float, sim_t: float) -> None:
        aid = "heat_stress"
        if temp > 32.0:
            self._fire(Tip(
                id=aid,
                severity=WARNING,
                title="It's getting hot in the greenhouse",
                reason=(f"Temperature is {temp:.1f}°C — above 32°C tomato flowers "
                        f"can drop and fruit set suffers."),
                action=("Open the side vents and roll down the shade net. "
                        "Water early in the morning, not midday."),
                sim_time_s=sim_t,
            ), sim_t)
        else:
            self._clear("heat_stress", sim_t)

    def _rule_chill(self, temp: float, hour: float, sim_t: float) -> None:
        aid = "chill_risk"
        is_evening = hour >= 16.0 or hour < 6.0
        if temp < 13.0:
            severity = CRITICAL if temp < 10.0 else WARNING
            self._fire(Tip(
                id=aid,
                severity=severity,
                title="Cold night risk — protect your crop",
                reason=(f"Temperature is {temp:.1f}°C. Below 13°C tomatoes "
                        f"suffer chilling damage: slow growth and blossom drop."),
                action=("Close all vents before sunset. "
                        "If you have a heater, turn it on tonight."),
                sim_time_s=sim_t,
            ), sim_t)
        else:
            self._clear(aid, sim_t)

    def _rule_humidity(self, rh: float, hour: float, sim_t: float) -> None:
        aid = "fungal_risk"
        if rh > 85.0:
            severity = CRITICAL if rh > 92.0 else WARNING
            self._fire(Tip(
                id=aid,
                severity=severity,
                title="High humidity — fungal risk (botrytis, blight)",
                reason=(f"Humidity is {rh:.0f}%. Above 85%, especially at night, "
                        f"creates ideal conditions for grey mould and late blight."),
                action=("Ventilate well in the morning. "
                        "Avoid watering in the evening. "
                        "Remove any leaves touching the soil."),
                sim_time_s=sim_t,
            ), sim_t)
        else:
            self._clear(aid, sim_t)

    def _rule_vpd(self, vpd: float, sim_t: float) -> None:
        aid = "high_vpd"
        if vpd > 1.5:
            self._fire(Tip(
                id=aid,
                severity=WARNING,
                title="Air is very dry — plants losing water fast",
                reason=(f"VPD is {vpd:.2f} kPa (above 1.5). "
                        f"Tomatoes are transpiring hard and may wilt even if soil is wet."),
                action=("Mist the paths between beds at midday. "
                        "Use shade cloth to reduce air temperature."),
                sim_time_s=sim_t,
            ), sim_t)
        elif vpd < 0.3:
            self._fire(Tip(
                id="low_vpd",
                severity=INFO,
                title="Air is very humid — low evapotranspiration",
                reason=f"VPD is {vpd:.2f} kPa. Plants aren't transpiring much.",
                action="You can reduce watering slightly today.",
                sim_time_s=sim_t,
            ), sim_t)
        else:
            self._clear(aid, sim_t)
            self._clear("low_vpd", sim_t)

    def _rule_tank_low(self, state: dict, sim_t: float) -> None:
        # Tank advice handled separately from detection alerts for proactive messaging
        tank = state["tank_level"]
        aid  = "tank_advisory"
        if 10 < tank <= 20:
            self._fire(Tip(
                id=aid,
                severity=WARNING,
                title="Plan a tank refill soon",
                reason=f"Tank is at {tank:.0f}% — enough for roughly a day of watering.",
                action="Arrange a water delivery or connect the mains supply.",
                sim_time_s=sim_t,
            ), sim_t)
        else:
            self._clear(aid, sim_t)

    # ── Lifecycle helpers ─────────────────────────────────────────────────────

    def _fire(self, tip: Tip, sim_t: float) -> None:
        aid = tip.id
        if aid in self._active:
            self._active[aid] = tip   # refresh content, no new alert
            return
        cleared_at = self._cleared_at.get(aid)
        if cleared_at and (sim_t - cleared_at) < 5 * 60:
            return
        self._active[aid] = tip
        self.new_tips.append(tip)

    def _clear(self, aid: str, sim_t: float) -> None:
        if aid in self._active:
            self._cleared_at[aid] = sim_t
            del self._active[aid]

    def _auto_clear(self, state: dict, sim_t: float) -> None:
        T  = state["weather"]["temp"]
        RH = state["weather"]["humidity"]
        if T <= 31.0:
            self._clear("heat_stress",  sim_t)
        if T >= 14.0:
            self._clear("chill_risk",   sim_t)
        if RH <= 83.0:
            self._clear("fungal_risk",  sim_t)

    def get_all_active(self) -> List[dict]:
        return [t.to_dict() for t in self._active.values()]
