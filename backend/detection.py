"""
detection.py — Fault detection and time-to-stress forecasting

Consumes the simulator's to_dict() snapshot each tick and fires alerts.
Each alert is deduplicated: won't re-fire while condition is active.
"""

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ── Alert severity ─────────────────────────────────────────────────────────────
INFO     = "info"
WARNING  = "warning"
CRITICAL = "critical"

# ── Thresholds ─────────────────────────────────────────────────────────────────
MOISTURE_WARN        = 25.0
MOISTURE_CRITICAL    = 20.0
MOISTURE_TARGET      = 75.0   # target moisture after irrigation
FORECAST_WINDOW      = 10    # samples for linear regression
LEAK_FLOW_FACTOR     = 1.5   # flow > expected * this → suspect leak
BLOCKED_FLOW_FACTOR  = 0.3   # flow < expected * this → suspect blockage
EXPECTED_FLOW        = 0.0116  # L/s (from simulator.DRIP_RATE_L_PER_S)
TANK_WARN_PCT        = 20.0
SENSOR_DRIFT_THRESH  = 8.0   # moisture points — robot probe vs fixed sensor
FROZEN_SIM_MINUTES   = 30    # if reading unchanged for this long → fault

# ── Structures ─────────────────────────────────────────────────────────────────
@dataclass
class Alert:
    id: str
    severity: str
    title: str
    reason: str
    action: str
    urgency_minutes: Optional[float]
    timestamp: float = field(default_factory=time.time)
    sim_time_s: float = 0.0
    cleared: bool = False

    def to_dict(self) -> dict:
        return {
            "id":              self.id,
            "severity":        self.severity,
            "title":           self.title,
            "reason":          self.reason,
            "action":          self.action,
            "urgency_minutes": self.urgency_minutes,
            "timestamp":       self.timestamp,
            "sim_time_s":      self.sim_time_s,
        }


# ── Detector ───────────────────────────────────────────────────────────────────
class Detector:
    """
    Call ``update(state_dict)`` every sim tick.
    ``new_alerts`` is populated with any newly fired alerts each call.
    ``all_active_alerts`` holds everything currently active.
    """

    def __init__(self):
        # Rolling buffers per zone: (sim_time_s, moisture)
        self._moisture_history: List[deque] = [
            deque(maxlen=FORECAST_WINDOW) for _ in range(4)
        ]
        # Active alert IDs → Alert
        self._active: Dict[str, Alert] = {}
        # Recently cleared IDs → cleared_sim_time (5-min quiet period)
        self._cleared_at: Dict[str, float] = {}
        self.new_alerts: List[Alert] = []
        self.all_active_alerts: List[Alert] = []

        # Leak / blockage consecutive-tick counters
        self._leak_ticks: int = 0
        self._block_ticks: Dict[int, int] = {i: 0 for i in range(4)}

        # Frozen-reading tracker: zone → (last_value, first_seen_sim_time)
        self._frozen: Dict[int, tuple] = {}

    def update(self, state: dict, robot_probes: Optional[Dict[int, float]] = None) -> None:
        """
        state: output of Simulator.to_dict()
        robot_probes: {zone_id: probe_moisture} when robot has just probed
        """
        self.new_alerts = []
        sim_t = state["sim_time_s"]
        zones = state["zones"]
        weather = state["weather"]

        # Update moisture history
        for z in zones:
            self._moisture_history[z["id"]].append((sim_t, z["moisture"]))

        # Run all detectors
        self._detect_dry_zones(zones, sim_t)
        self._detect_forecast(zones, sim_t)
        self._detect_leak(state, sim_t)
        self._detect_blocked(state, sim_t)
        self._detect_empty_tank(state, sim_t)
        if robot_probes:
            self._detect_faulty_sensor(zones, robot_probes, sim_t)
        self._detect_frozen_sensor(zones, sim_t)
        self._clear_resolved(state, sim_t)

        self.all_active_alerts = list(self._active.values())

    # ── Detection rules ────────────────────────────────────────────────────────

    def _detect_dry_zones(self, zones, sim_t):
        for z in zones:
            m = z["moisture"]
            zid = z["id"]

            alert_id = f"dry_warn_{zid}"
            if m < MOISTURE_CRITICAL:
                self._fire(Alert(
                    id=alert_id,
                    severity=CRITICAL,
                    title=f"Zone {zid+1} is critically dry — plants under stress",
                    reason=f"Soil moisture is {m:.0f}%, below the 20% stress threshold.",
                    action=f"Water Zone {zid+1} immediately. Check drip line is running.",
                    urgency_minutes=0,
                    sim_time_s=sim_t,
                ), sim_t)
            elif m < MOISTURE_WARN:
                self._fire(Alert(
                    id=alert_id,
                    severity=WARNING,
                    title=f"Zone {zid+1} is getting dry — water soon",
                    reason=f"Soil moisture is {m:.0f}%, approaching the 25% warning level.",
                    action=f"Water Zone {zid+1} within the next hour.",
                    urgency_minutes=60,
                    sim_time_s=sim_t,
                ), sim_t)
            else:
                self._clear_alert(alert_id, sim_t)

    def _detect_forecast(self, zones, sim_t):
        for z in zones:
            zid = z["id"]
            buf = self._moisture_history[zid]
            if len(buf) < 3:
                continue

            slope = _linear_slope(list(buf))  # %/sim-second (negative = drying)
            if slope >= 0:
                self._clear_alert(f"forecast_{zid}", sim_t)
                continue

            current = z["moisture"]
            time_to_stress_s = (current - MOISTURE_CRITICAL) / abs(slope)
            time_to_stress_m = time_to_stress_s / 60.0

            if time_to_stress_m > 240:
                self._clear_alert(f"forecast_{zid}", sim_t)
                continue

            volume_est = _zone_water_volume(current, MOISTURE_TARGET)
            severity = CRITICAL if time_to_stress_m < 30 else WARNING

            self._fire(Alert(
                id=f"forecast_{zid}",
                severity=severity,
                title=f"Zone {zid+1} will hit stress in {time_to_stress_m:.0f} min",
                reason=(f"Moisture is {current:.0f}% and falling at "
                        f"{abs(slope)*60:.3f}%/min."),
                action=(f"Water Zone {zid+1} within {time_to_stress_m:.0f} minutes "
                        f"— about {volume_est:.0f} litres needed."),
                urgency_minutes=round(time_to_stress_m, 1),
                sim_time_s=sim_t,
            ), sim_t)

    def _detect_leak(self, state, sim_t):
        alert_id = "leak"
        pump_on  = state["pump_on"]
        flow     = state["pump_flow"]
        tank     = state["tank_level"]

        if not pump_on:
            self._leak_ticks = 0
            return

        high_flow  = flow > EXPECTED_FLOW * LEAK_FLOW_FACTOR
        # Check if any zone moisture is NOT rising despite pump running
        zones = state["zones"]
        moisture_flat = all(
            len(self._moisture_history[z["id"]]) < 2 or
            self._moisture_history[z["id"]][-1][1] - self._moisture_history[z["id"]][-2][1] < 0.3
            for z in zones
        )

        if high_flow and moisture_flat:
            self._leak_ticks += 1
        else:
            self._leak_ticks = max(0, self._leak_ticks - 1)

        if self._leak_ticks >= 3:
            loss_l_h = (flow - EXPECTED_FLOW) * 3600
            self._fire(Alert(
                id=alert_id,
                severity=CRITICAL,
                title="Possible water leak detected",
                reason=(f"Flow is {flow*60:.1f} L/min — {LEAK_FLOW_FACTOR}× higher than "
                        f"normal, but soil isn't getting wetter. Losing ~{loss_l_h:.0f} L/h."),
                action=("Check all drip line joints, especially Zone 4. "
                        "Turn off pump and inspect before the tank empties."),
                urgency_minutes=10,
                sim_time_s=sim_t,
            ), sim_t)
        else:
            self._clear_alert(alert_id, sim_t)

    def _detect_blocked(self, state, sim_t):
        zones   = state["zones"]
        pump_on = state["pump_on"]
        flow    = state["pump_flow"]

        if not pump_on:
            for zid in self._block_ticks:
                self._block_ticks[zid] = 0
            return

        low_flow = flow < EXPECTED_FLOW * BLOCKED_FLOW_FACTOR

        for z in zones:
            zid = z["id"]
            buf = self._moisture_history[zid]
            moisture_not_rising = (
                len(buf) >= 2 and
                buf[-1][1] - buf[-2][1] < 0.2
            )

            alert_id = f"blocked_{zid}"
            if low_flow and moisture_not_rising and z.get("drip_active", False):
                self._block_ticks[zid] += 1
            else:
                self._block_ticks[zid] = max(0, self._block_ticks[zid] - 1)

            if self._block_ticks[zid] >= 3:
                self._fire(Alert(
                    id=alert_id,
                    severity=WARNING,
                    title=f"Drip line blocked — Zone {zid+1}",
                    reason=(f"Pump is running but flow is very low "
                            f"({flow*60:.2f} L/min) and Zone {zid+1} moisture isn't rising."),
                    action=(f"Check the drip line emitters in Zone {zid+1} for blockage or kinking. "
                            f"The robot is on its way to spot-water."),
                    urgency_minutes=30,
                    sim_time_s=sim_t,
                ), sim_t)
            else:
                self._clear_alert(alert_id, sim_t)

    def _detect_empty_tank(self, state, sim_t):
        tank = state["tank_level"]
        alert_id = "empty_tank"

        if tank <= 0:
            self._fire(Alert(
                id=alert_id,
                severity=CRITICAL,
                title="Water tank is empty — irrigation stopped",
                reason="Tank level has reached 0%. The pump cannot run.",
                action="Refill the tank immediately. Check for leaks that may have drained it.",
                urgency_minutes=0,
                sim_time_s=sim_t,
            ), sim_t)
        elif tank < TANK_WARN_PCT:
            hours_left = (tank / 100.0 * 2000) / (EXPECTED_FLOW * 3600) if EXPECTED_FLOW > 0 else 99
            self._fire(Alert(
                id=alert_id,
                severity=WARNING,
                title=f"Tank low — about {hours_left:.1f} hours of water left",
                reason=f"Tank is at {tank:.0f}%. At current usage this runs out soon.",
                action="Arrange a refill or connect mains supply.",
                urgency_minutes=hours_left * 60,
                sim_time_s=sim_t,
            ), sim_t)
        else:
            self._clear_alert(alert_id, sim_t)

    def _detect_faulty_sensor(self, zones, probes: Dict[int, float], sim_t):
        for zid, probe_val in probes.items():
            fixed_val = zones[zid]["moisture"]
            delta = abs(probe_val - fixed_val)
            alert_id = f"faulty_sensor_{zid}"
            if delta > SENSOR_DRIFT_THRESH:
                self._fire(Alert(
                    id=alert_id,
                    severity=WARNING,
                    title=f"Zone {zid+1} sensor may be faulty",
                    reason=(f"Robot probe reads {probe_val:.1f}% but the fixed sensor "
                            f"shows {fixed_val:.1f}% — a {delta:.1f}-point difference."),
                    action=(f"Check the Zone {zid+1} moisture sensor mounting and wiring. "
                            f"Trust the robot probe reading for now."),
                    urgency_minutes=120,
                    sim_time_s=sim_t,
                ), sim_t)
            else:
                self._clear_alert(alert_id, sim_t)

    def _detect_frozen_sensor(self, zones, sim_t):
        for z in zones:
            zid = z["id"]
            val = z["moisture"]
            alert_id = f"frozen_sensor_{zid}"

            if zid not in self._frozen:
                self._frozen[zid] = (val, sim_t)
                continue

            last_val, first_seen = self._frozen[zid]
            if abs(val - last_val) > 0.5:
                self._frozen[zid] = (val, sim_t)   # value changed — reset
                self._clear_alert(alert_id, sim_t)
            elif (sim_t - first_seen) > FROZEN_SIM_MINUTES * 60:
                self._fire(Alert(
                    id=alert_id,
                    severity=WARNING,
                    title=f"Zone {zid+1} sensor reading hasn't changed in 30 min",
                    reason=(f"Sensor stuck at {val:.1f}% for more than 30 simulated minutes. "
                            f"This suggests a faulty or disconnected sensor."),
                    action=f"Inspect Zone {zid+1} sensor connection. Replace if needed.",
                    urgency_minutes=180,
                    sim_time_s=sim_t,
                ), sim_t)

    def _clear_resolved(self, state, sim_t):
        """Auto-clear critical dry alerts when moisture recovers."""
        zones = state["zones"]
        for z in zones:
            zid = z["id"]
            if z["moisture"] >= MOISTURE_WARN + 5:
                self._clear_alert(f"dry_warn_{zid}", sim_t)
            if z["moisture"] >= MOISTURE_WARN + 10:
                self._clear_alert(f"forecast_{zid}", sim_t)

    # ── Alert lifecycle ────────────────────────────────────────────────────────

    def _fire(self, alert: Alert, sim_t: float) -> None:
        """Fire an alert — respects deduplication and quiet period."""
        aid = alert.id
        # Already active → update but don't add to new_alerts again
        if aid in self._active:
            self._active[aid] = alert
            return
        # In quiet period after being cleared
        cleared_at = self._cleared_at.get(aid)
        if cleared_at and (sim_t - cleared_at) < 5 * 60:  # 5 sim-min quiet
            return
        self._active[aid] = alert
        self.new_alerts.append(alert)

    def _clear_alert(self, alert_id: str, sim_t: float) -> None:
        if alert_id in self._active:
            self._active[alert_id].cleared = True
            self._cleared_at[alert_id] = sim_t
            del self._active[alert_id]

    def get_all_active(self) -> List[dict]:
        return [a.to_dict() for a in self._active.values()]


# ── Math helpers ──────────────────────────────────────────────────────────────

def _linear_slope(points: list) -> float:
    """Least-squares slope of [(sim_time_s, moisture), ...] → %/s."""
    n = len(points)
    if n < 2:
        return 0.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    den = sum((x - x_mean) ** 2 for x in xs)
    return num / den if den != 0 else 0.0


def _zone_water_volume(current_moisture: float, target: float) -> float:
    """Rough litres needed to raise one zone from current_moisture to target."""
    zones = 4
    beds  = 6
    plants = 8
    # 1 % moisture ≈ 0.05 L per plant (rough calibration)
    delta = max(0, target - current_moisture)
    return delta * 0.05 * plants * beds
