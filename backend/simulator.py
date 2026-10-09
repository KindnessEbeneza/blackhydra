"""
simulator.py — Greenhouse Guardian physics engine

Drives a 24-hour greenhouse day with:
  - 4 drip zones × 6 beds each
  - Weather cycle (temperature, humidity, light)
  - Soil moisture ODE (ET-based drying + drip irrigation)
  - Fault injection (leak, blocked_line, empty_tank, heatwave, broken_sensor)
  - Twin 'dumb timer' greenhouse running in parallel for comparison
  - Speed multiplier (360x default → 1 day ≈ 4 min real time)
"""

import math
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional

# ── Constants ─────────────────────────────────────────────────────────────────
ZONES = 4
BEDS_PER_ZONE = 6
PLANTS_PER_BED = 8                # mature tomato plants
LITRES_PER_PLANT_PER_DAY = 2.0   # ~2 L/plant/day at peak
TANK_CAPACITY_LITRES = 2000.0

# Drip rate: litres per sim-second when active
# 4 zones × 6 beds × 8 plants × 2 L/day ÷ 86400 s/day × headroom factor
DRIP_RATE_L_PER_S = (ZONES * BEDS_PER_ZONE * PLANTS_PER_BED *
                     LITRES_PER_PLANT_PER_DAY / 86400.0) * 6

# Moisture units: 0–100 %.  One "moisture unit" ≈ 0.05 L of water per plant.
MOISTURE_PER_LITRE = 0.4   # % moisture per litre added per zone

# Thresholds
MOISTURE_WARN      = 25.0   # water now
MOISTURE_CRITICAL  = 20.0   # plant stress
MOISTURE_TARGET    = 75.0   # stop watering
MOISTURE_FIELD_CAP = 100.0  # excess = drainage

TANK_WARN_PCT      = 20.0   # warn at 20 %
EXPECTED_FLOW_L_S  = DRIP_RATE_L_PER_S  # L/s when pump is on


# ── Zone state ────────────────────────────────────────────────────────────────
@dataclass
class ZoneState:
    id: int
    moisture: float = 60.0       # %
    drip_active: bool = False
    fault: str = ""              # "leak" | "blocked_line" | ""
    last_watered_sim_s: float = 0.0
    # Timer twin fields
    timer_moisture: float = 60.0
    timer_drip_active: bool = False


# ── Weather state ─────────────────────────────────────────────────────────────
@dataclass
class WeatherState:
    sim_hour: float = 7.0        # 0–24 simulated hour of day
    temp: float = 22.0           # °C
    humidity: float = 65.0       # %
    light: float = 400.0         # 0–1000 arbitrary lux proxy
    vpd: float = 0.8             # kPa


# ── Full greenhouse state ─────────────────────────────────────────────────────
@dataclass
class GreenhouseState:
    zones: List[ZoneState] = field(
        default_factory=lambda: [ZoneState(id=i) for i in range(ZONES)]
    )
    weather: WeatherState = field(default_factory=WeatherState)
    tank_level: float = 85.0     # %
    pump_on: bool = False
    pump_flow: float = 0.0       # L/s
    sim_time_s: float = 7 * 3600  # seconds since midnight

    # Fault flags (set by server / Wokwi bridge)
    active_fault: str = ""        # global fault name

    # Scoreboard — smart system
    smart_water_used_l: float = 0.0
    smart_stress_hours: float = 0.0
    smart_waste_l: float = 0.0

    # Scoreboard — timer twin
    timer_water_used_l: float = 0.0
    timer_stress_hours: float = 0.0
    timer_waste_l: float = 0.0

    # Wokwi override flag — when True, sensor values come from Wokwi
    wokwi_override: bool = False


# ── Simulator class ───────────────────────────────────────────────────────────
class Simulator:
    """Tick-based greenhouse simulator.

    Call ``tick(real_dt)`` from an asyncio loop. Pass ``speed`` to control
    how many simulated seconds advance per real second.
    """

    def __init__(self, speed: float = 360.0):
        self.state = GreenhouseState()
        self.speed = speed          # sim-seconds per real-second
        self._last_real_time = time.monotonic()
        # Timer twin schedule: water each zone for 20 min at 07:00 and 15:00
        self._timer_schedule = [7 * 3600, 15 * 3600]   # sim-seconds
        self._timer_zone_duration = 20 * 60             # 20 min per zone
        self._sim_day_number = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def tick(self, real_dt: float) -> GreenhouseState:
        """Advance simulation by real_dt real seconds."""
        sim_dt = real_dt * self.speed
        s = self.state

        # Advance sim clock
        s.sim_time_s += sim_dt
        # Wrap at 24 h and count days
        if s.sim_time_s >= 86400:
            s.sim_time_s -= 86400
            self._sim_day_number += 1

        self._update_weather(s, sim_dt)
        self._update_smart_irrigation(s, sim_dt)
        self._update_timer_irrigation(s, sim_dt)
        self._apply_fault(s, sim_dt)
        self._update_pump_flow(s)
        self._update_scoreboard(s, sim_dt)

        return s

    def inject_fault(self, fault: str) -> None:
        """Inject a fault globally and into a specific zone where relevant."""
        self.state.active_fault = fault
        if fault == "leak":
            self.state.zones[3].fault = "leak"
        elif fault == "blocked_line":
            self.state.zones[2].fault = "blocked_line"
        elif fault == "heatwave":
            # weather will be overridden by _apply_fault
            pass
        elif fault in ("empty_tank", "broken_sensor"):
            pass
        # "clear" resets everything
        if fault == "clear":
            self.state.active_fault = ""
            for z in self.state.zones:
                z.fault = ""

    def set_speed(self, speed: float) -> None:
        self.speed = max(1.0, min(speed, 1440.0))

    def apply_wokwi_reading(self, data: dict) -> None:
        """Override sensor values from Wokwi serial JSON."""
        s = self.state
        s.wokwi_override = True
        zones = data.get("z", [])
        for i, m in enumerate(zones[:ZONES]):
            s.zones[i].moisture = float(m)
        if "t" in data:
            s.weather.temp = float(data["t"])
        if "h" in data:
            s.weather.humidity = float(data["h"])
        if "l" in data:
            s.weather.light = float(data["l"])
        if "flow" in data:
            s.pump_flow = float(data["flow"])
        if "tank" in data:
            s.tank_level = float(data["tank"])
        if "fault" in data and data["fault"]:
            self.inject_fault(data["fault"])

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _update_weather(self, s: GreenhouseState, sim_dt: float) -> None:
        if s.wokwi_override:
            return   # Wokwi drives weather

        hour = s.sim_time_s / 3600.0

        # Temperature: min 18 °C at 06:00, max 34 °C at 14:00
        s.weather.temp = 26.0 + 8.0 * math.sin((hour - 10.0) * math.pi / 12.0)
        s.weather.temp += random.gauss(0, 0.15)

        # Humidity: inversely correlated
        s.weather.humidity = 72.0 - 14.0 * math.sin((hour - 10.0) * math.pi / 12.0)
        s.weather.humidity = _clamp(s.weather.humidity + random.gauss(0, 0.5), 20, 100)

        # Light: trapezoid 06:00–19:00
        if hour < 6.0 or hour > 19.0:
            s.weather.light = 0.0
        elif hour < 11.0:
            s.weather.light = 900.0 * (hour - 6.0) / 5.0
        elif hour <= 15.0:
            s.weather.light = 900.0
        else:
            s.weather.light = 900.0 * (19.0 - hour) / 4.0
        s.weather.light += random.gauss(0, 10)
        s.weather.light = max(0, s.weather.light)

        # VPD (kPa): simplified Tetens
        T = s.weather.temp
        RH = s.weather.humidity
        es = 0.6108 * math.exp(17.27 * T / (T + 237.3))
        s.weather.vpd = round(es * (1.0 - RH / 100.0), 3)

    def _et_rate(self, s: GreenhouseState) -> float:
        """Evapotranspiration rate in moisture-% per sim-second."""
        light_factor = s.weather.light / 900.0
        temp_factor  = max(0, (s.weather.temp - 10.0) / 20.0)
        # Peak ~0.004 %/s → ~345 %/day; scaled so a full day dries ~30 % without irrigation
        base = 0.0028 * light_factor * temp_factor
        return max(0, base + random.gauss(0, 0.0001))

    def _update_smart_irrigation(self, s: GreenhouseState, sim_dt: float) -> None:
        """Smart system: water zone when moisture < 30%, stop at 75%."""
        if s.wokwi_override:
            return

        et = self._et_rate(s)
        any_drip = False

        for z in s.zones:
            # Dry soil passively
            z.moisture -= et * sim_dt
            z.moisture = max(0, z.moisture)

            # Decide drip
            if z.moisture < 30.0:
                z.drip_active = True
            elif z.moisture >= MOISTURE_TARGET:
                z.drip_active = False

            # Apply drip (if not blocked / empty tank)
            if z.drip_active and s.tank_level > 0 and z.fault != "blocked_line":
                flow_l = DRIP_RATE_L_PER_S * sim_dt
                gain = flow_l * MOISTURE_PER_LITRE
                new_m = z.moisture + gain
                if new_m > MOISTURE_FIELD_CAP:
                    s.smart_waste_l += (new_m - MOISTURE_FIELD_CAP) / MOISTURE_PER_LITRE
                    new_m = MOISTURE_FIELD_CAP
                z.moisture = new_m
                s.smart_water_used_l += flow_l
                s.tank_level -= (flow_l / TANK_CAPACITY_LITRES) * 100.0
                s.tank_level = max(0, s.tank_level)
                any_drip = True
                z.last_watered_sim_s = s.sim_time_s

        s.pump_on = any_drip

    def _update_timer_irrigation(self, s: GreenhouseState, sim_dt: float) -> None:
        """Timer twin: fixed schedule 07:00 + 15:00, 20 min per zone."""
        et = self._et_rate(s)
        t = s.sim_time_s

        for z in s.zones:
            # Dry soil
            z.timer_moisture -= et * sim_dt
            z.timer_moisture = max(0, z.timer_moisture)

            # Check if this zone should be watered now
            watering = False
            for sched_start in self._timer_schedule:
                zone_start = sched_start + z.id * self._timer_zone_duration
                zone_end   = zone_start + self._timer_zone_duration
                if zone_start <= t < zone_end:
                    watering = True
                    break

            z.timer_drip_active = watering

            if watering:
                flow_l = DRIP_RATE_L_PER_S * sim_dt
                gain   = flow_l * MOISTURE_PER_LITRE
                new_m  = z.timer_moisture + gain
                if new_m > MOISTURE_FIELD_CAP:
                    s.timer_waste_l += (new_m - MOISTURE_FIELD_CAP) / MOISTURE_PER_LITRE
                    new_m = MOISTURE_FIELD_CAP
                z.timer_moisture = new_m
                s.timer_water_used_l += flow_l

    def _apply_fault(self, s: GreenhouseState, sim_dt: float) -> None:
        fault = s.active_fault
        if not fault or fault == "clear":
            return

        if fault == "leak":
            # Tank drains ~12 L/h extra
            extra_drain_l = 12.0 / 3600.0 * sim_dt
            s.tank_level -= (extra_drain_l / TANK_CAPACITY_LITRES) * 100.0
            s.tank_level = max(0, s.tank_level)
            s.pump_flow = DRIP_RATE_L_PER_S * 3.5   # anomalously high

        elif fault == "blocked_line":
            # Zone 2 line is blocked — drip_active but no moisture gain (handled in smart loop)
            s.zones[2].fault = "blocked_line"

        elif fault == "empty_tank":
            s.tank_level = 0.0
            s.pump_on    = False

        elif fault == "heatwave":
            s.weather.temp     = 38.5 + random.gauss(0, 0.5)
            s.weather.humidity = 28.0 + random.gauss(0, 1.5)
            # Extra ET
            for z in s.zones:
                z.moisture -= 0.008 * sim_dt
                z.moisture  = max(0, z.moisture)

        elif fault == "broken_sensor":
            # Zone 0 sensor appears stuck — value frozen by robot detection
            pass   # Robot probe comparison handles detection

    def _update_pump_flow(self, s: GreenhouseState) -> None:
        if s.wokwi_override:
            return
        if s.active_fault == "leak":
            s.pump_flow = DRIP_RATE_L_PER_S * 3.5 + random.gauss(0, 0.05)
        elif s.active_fault == "blocked_line":
            s.pump_flow = DRIP_RATE_L_PER_S * 0.1 + random.gauss(0, 0.02)
        elif s.pump_on:
            s.pump_flow = DRIP_RATE_L_PER_S + random.gauss(0, 0.03)
        else:
            s.pump_flow = 0.0

    def _update_scoreboard(self, s: GreenhouseState, sim_dt: float) -> None:
        sim_hours = sim_dt / 3600.0
        for z in s.zones:
            if z.moisture < MOISTURE_CRITICAL:
                s.smart_stress_hours += sim_hours
            if z.timer_moisture < MOISTURE_CRITICAL:
                s.timer_stress_hours += sim_hours

    # ── Serialisation ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        s = self.state
        hour = s.sim_time_s / 3600.0
        return {
            "sim_time_s":   round(s.sim_time_s, 1),
            "sim_hour":     round(hour % 24, 2),
            "sim_day":      self._sim_day_number,
            "speed":        self.speed,
            "active_fault": s.active_fault,
            "wokwi_active": s.wokwi_override,
            "zones": [
                {
                    "id":           z.id,
                    "moisture":     round(z.moisture, 1),
                    "drip_active":  z.drip_active,
                    "fault":        z.fault,
                    "last_watered": round(z.last_watered_sim_s, 0),
                    "timer_moisture": round(z.timer_moisture, 1),
                }
                for z in s.zones
            ],
            "weather": {
                "temp":     round(s.weather.temp, 1),
                "humidity": round(s.weather.humidity, 1),
                "light":    round(s.weather.light, 0),
                "vpd":      round(s.weather.vpd, 3),
            },
            "tank_level": round(s.tank_level, 1),
            "pump_on":    s.pump_on,
            "pump_flow":  round(s.pump_flow, 3),
            "scoreboard": {
                "smart": {
                    "water_used_l":  round(s.smart_water_used_l, 1),
                    "stress_hours":  round(s.smart_stress_hours, 2),
                    "waste_l":       round(s.smart_waste_l, 1),
                },
                "timer": {
                    "water_used_l":  round(s.timer_water_used_l, 1),
                    "stress_hours":  round(s.timer_stress_hours, 2),
                    "waste_l":       round(s.timer_waste_l, 1),
                },
            },
        }


# ── Utility ───────────────────────────────────────────────────────────────────
def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ── Headless smoke test ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sim = Simulator(speed=3600.0)   # 1 day in ~24 real seconds
    print("Running 1 sim-day headless test...")
    steps = int(86400 / 1.0)        # 1-second real ticks
    for i in range(steps):
        sim.tick(1.0)
        if i % 3600 == 0:
            d = sim.to_dict()
            zones_m = [z["moisture"] for z in d["zones"]]
            print(f"  Day {d['sim_day']} {d['sim_hour']:05.2f}h  "
                  f"zones={zones_m}  "
                  f"smart={d['scoreboard']['smart']['water_used_l']:.0f}L  "
                  f"timer={d['scoreboard']['timer']['water_used_l']:.0f}L")

    d = sim.to_dict()
    smart_w = d["scoreboard"]["smart"]["water_used_l"]
    timer_w = d["scoreboard"]["timer"]["water_used_l"]
    saving  = (timer_w - smart_w) / timer_w * 100 if timer_w > 0 else 0
    print(f"\nFinal: smart={smart_w:.0f}L  timer={timer_w:.0f}L  "
          f"saving={saving:.0f}%  "
          f"smart_stress={d['scoreboard']['smart']['stress_hours']:.2f}h")
