"""
robot.py — Greenhouse Guardian scout robot

State machine:
    PATROL → PROBE → SPOT_WATER → RETURN_TO_BASE

The robot:
  1. Patrols zones in priority order (driest first).
  2. Probes each zone with its own sensor — catches drifting fixed sensors.
  3. Spot-waters a zone when the drip line is blocked and moisture < 25%.
  4. Returns to base (Zone 0) after completing its task.

Movement: 2 sim-seconds per zone hop.
Position: 0.0–3.0 float (zone index), interpolated for smooth dashboard rendering.
"""

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ── States ────────────────────────────────────────────────────────────────────
PATROL      = "PATROL"
PROBE       = "PROBE"
SPOT_WATER  = "SPOT_WATER"
RETURN      = "RETURN_TO_BASE"

MOVE_TIME_SIM_S  = 2.0    # sim-seconds to travel one zone
PROBE_TIME_SIM_S = 5.0    # sim-seconds to take a reading
SPOT_WATER_L_MIN = 1.5    # litres per sim-minute when spot-watering
SPOT_WATER_DURATION = 10 * 60   # 10 sim-minutes
SPOT_WATER_CAPACITY = 20.0  # litres robot carries

MOISTURE_SPOT_THRESHOLD = 25.0  # start spot-watering below this


@dataclass
class RobotState:
    state:            str   = PATROL
    current_zone:     int   = 0       # zone the robot is in (0-indexed)
    target_zone:      int   = 0
    position:         float = 0.0     # 0.0–3.0 for SVG interpolation
    probe_reading:    Optional[float] = None
    spot_water_active: bool = False
    water_tank_l:     float = SPOT_WATER_CAPACITY
    action_label:     str   = "Patrolling"
    _state_timer_s:   float = field(default=0.0, repr=False)
    _spot_water_timer_s: float = field(default=0.0, repr=False)

    def to_dict(self) -> dict:
        return {
            "state":             self.state,
            "current_zone":      self.current_zone,
            "target_zone":       self.target_zone,
            "position":          round(self.position, 3),
            "probe_reading":     (round(self.probe_reading, 1)
                                  if self.probe_reading is not None else None),
            "spot_water_active": self.spot_water_active,
            "water_tank_l":      round(self.water_tank_l, 1),
            "action_label":      self.action_label,
        }


class Robot:
    """
    Call ``tick(sim_dt, sim_state_dict)`` each simulation tick.
    Returns (updated RobotState, probe_readings_dict, moisture_additions_dict).
    """

    def __init__(self):
        self.rs = RobotState()
        self._patrol_index: int = 0       # which zone in priority queue we're visiting
        self._priority_queue: List[int] = [0, 1, 2, 3]
        self._queue_refresh_timer: float = 0.0
        self._queue_refresh_interval: float = 60.0  # sim-seconds

        # Results published each tick
        self.probe_readings:    Dict[int, float] = {}
        self.moisture_additions: Dict[int, float] = {}  # zone → %moisture added

    def tick(self, sim_dt: float, state: dict) -> Tuple[dict, Dict, Dict]:
        """
        Advance robot by sim_dt seconds.
        Returns (robot_state_dict, probe_readings, moisture_additions).
        """
        self.probe_readings = {}
        self.moisture_additions = {}

        self._refresh_priority_queue(sim_dt, state)
        self._run_state_machine(sim_dt, state)

        return self.rs.to_dict(), self.probe_readings, self.moisture_additions

    # ── Priority queue ────────────────────────────────────────────────────────

    def _refresh_priority_queue(self, sim_dt: float, state: dict) -> None:
        self._queue_refresh_timer += sim_dt
        if self._queue_refresh_timer < self._queue_refresh_interval:
            return
        self._queue_refresh_timer = 0.0
        zones = state["zones"]
        # Sort by ascending moisture — driest first
        self._priority_queue = sorted(
            range(len(zones)),
            key=lambda i: zones[i]["moisture"]
        )

    # ── State machine ─────────────────────────────────────────────────────────

    def _run_state_machine(self, sim_dt: float, state: dict) -> None:
        rs = self.rs

        if rs.state == PATROL:
            self._do_patrol(sim_dt, state)
        elif rs.state == PROBE:
            self._do_probe(sim_dt, state)
        elif rs.state == SPOT_WATER:
            self._do_spot_water(sim_dt, state)
        elif rs.state == RETURN:
            self._do_return(sim_dt)

    def _do_patrol(self, sim_dt: float, state: dict) -> None:
        rs = self.rs
        target = self._priority_queue[self._patrol_index % len(self._priority_queue)]
        rs.target_zone = target
        rs.action_label = f"Moving to Zone {target + 1}"

        arrived = self._move_toward(target, sim_dt)
        if arrived:
            rs.state = PROBE
            rs._state_timer_s = 0.0
            rs.action_label = f"Probing Zone {target + 1} sensor"

    def _do_probe(self, sim_dt: float, state: dict) -> None:
        rs = self.rs
        rs._state_timer_s += sim_dt

        if rs._state_timer_s < PROBE_TIME_SIM_S:
            return  # still taking reading

        # Take probe reading
        zid = rs.current_zone
        zones = state["zones"]
        true_moisture = zones[zid]["moisture"]

        # Robot probe: slightly noisy but independent of fixed sensor
        probe_val = true_moisture + random.gauss(0, 1.5)
        probe_val = max(0, min(100, probe_val))
        rs.probe_reading = probe_val
        self.probe_readings[zid] = probe_val  # will be picked up by Detector

        # Check if spot-watering is needed
        zone_fault = zones[zid].get("fault", "")
        needs_spot = (
            zone_fault == "blocked_line" and
            true_moisture < MOISTURE_SPOT_THRESHOLD and
            rs.water_tank_l > 0
        )

        if needs_spot:
            rs.state = SPOT_WATER
            rs._state_timer_s = 0.0
            rs._spot_water_timer_s = 0.0
            rs.action_label = f"Spot-watering Zone {zid + 1} (line blocked)"
        else:
            # Move to next zone in queue
            self._patrol_index += 1
            rs.state = PATROL
            rs._state_timer_s = 0.0
            rs.action_label = "Patrolling"

    def _do_spot_water(self, sim_dt: float, state: dict) -> None:
        rs = self.rs
        rs._spot_water_timer_s += sim_dt
        rs.spot_water_active = True

        # Apply water to zone moisture
        zid = rs.current_zone
        flow_l = SPOT_WATER_L_MIN / 60.0 * sim_dt   # L per sim-second
        flow_l = min(flow_l, rs.water_tank_l)
        rs.water_tank_l -= flow_l

        # Convert litres to moisture %
        moisture_gain = flow_l * 0.4   # same calibration as simulator
        self.moisture_additions[zid] = moisture_gain

        rs.action_label = (f"Spot-watering Zone {zid + 1} — "
                           f"{rs.water_tank_l:.1f} L remaining")

        # Stop when done or tank empty
        if rs._spot_water_timer_s >= SPOT_WATER_DURATION or rs.water_tank_l <= 0:
            rs.spot_water_active = False
            rs.state = RETURN
            rs.target_zone = 0
            rs.action_label = "Returning to base to refill"

    def _do_return(self, sim_dt: float) -> None:
        rs = self.rs
        arrived = self._move_toward(0, sim_dt)
        if arrived:
            rs.water_tank_l = SPOT_WATER_CAPACITY   # refill at base
            rs.state = PATROL
            rs._state_timer_s = 0.0
            self._patrol_index = 0
            rs.action_label = "Patrolling"

    # ── Movement helper ───────────────────────────────────────────────────────

    def _move_toward(self, target: int, sim_dt: float) -> bool:
        """Move position toward target zone. Returns True when arrived."""
        rs = self.rs
        step = sim_dt / MOVE_TIME_SIM_S   # fraction of a zone per sim-second

        if rs.position < target:
            rs.position = min(target, rs.position + step)
        elif rs.position > target:
            rs.position = max(target, rs.position - step)

        if abs(rs.position - target) < 0.01:
            rs.position = float(target)
            rs.current_zone = target
            return True
        return False
