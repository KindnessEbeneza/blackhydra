"""
server.py — Greenhouse Guardian FastAPI backend

Endpoints:
  GET  /            → serves frontend/index.html
  GET  /state       → full JSON snapshot
  POST /fault/{ft}  → inject: leak | blocked_line | empty_tank | heatwave | broken_sensor | clear
  POST /speed/{m}   → set speed multiplier (1 | 60 | 360 | 720)
  POST /demo        → start auto demo sequence
  WS   /ws          → broadcast state every 500 ms
  WS   /wokwi-serial→ receive Wokwi serial JSON, override sim sensor values
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Ensure backend package is importable when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.simulator import Simulator
from backend.detection import Detector
from backend.advisor   import Advisor
from backend.robot     import Robot

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("greenhouse")

# ── App & components ──────────────────────────────────────────────────────────
app = FastAPI(title="Greenhouse Guardian")

simulator = Simulator(speed=360.0)
detector  = Detector()
advisor   = Advisor()
robot     = Robot()

# Alert / tip history (last 200 entries kept for dashboard feed)
alert_history: list = []

# Connected WebSocket clients
clients: Set[WebSocket] = set()

# Demo sequence state
_demo_task: asyncio.Task | None = None

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# ── Static files & index ──────────────────────────────────────────────────────
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

@app.get("/")
async def serve_index():
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({"status": "Greenhouse Guardian API running. Frontend not found."})

# ── REST endpoints ────────────────────────────────────────────────────────────
@app.get("/state")
async def get_state():
    return _build_full_state()

@app.post("/fault/{fault_type}")
async def inject_fault(fault_type: str):
    valid = {"leak", "blocked_line", "empty_tank", "heatwave", "broken_sensor", "clear"}
    if fault_type not in valid:
        return JSONResponse({"error": f"Unknown fault. Valid: {valid}"}, status_code=400)
    simulator.inject_fault(fault_type)
    log.info(f"Fault injected: {fault_type}")
    return {"status": "ok", "fault": fault_type}

@app.post("/speed/{multiplier}")
async def set_speed(multiplier: float):
    simulator.set_speed(multiplier)
    log.info(f"Speed set to {multiplier}x")
    return {"status": "ok", "speed": simulator.speed}

@app.post("/demo")
async def start_demo():
    global _demo_task
    if _demo_task and not _demo_task.done():
        return {"status": "demo already running"}
    _demo_task = asyncio.create_task(_run_demo_sequence())
    return {"status": "demo started"}

# ── WebSocket: dashboard clients ──────────────────────────────────────────────
@app.websocket("/ws")
async def ws_dashboard(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    log.info(f"Dashboard client connected. Total: {len(clients)}")
    try:
        # Send current state immediately on connect
        await websocket.send_text(json.dumps(_build_full_state()))
        while True:
            # Keep connection alive — actual data is pushed from the sim loop
            await asyncio.sleep(30)
            await websocket.send_text(json.dumps({"ping": True}))
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        clients.discard(websocket)
        log.info(f"Dashboard client disconnected. Total: {len(clients)}")

# ── WebSocket: Wokwi serial bridge ───────────────────────────────────────────
@app.websocket("/wokwi-serial")
async def ws_wokwi(websocket: WebSocket):
    await websocket.accept()
    log.info("Wokwi serial bridge connected")
    try:
        while True:
            raw = await websocket.receive_text()
            raw = raw.strip()
            if not raw or raw.startswith("{\"status\""):
                continue
            try:
                data = json.loads(raw)
                simulator.apply_wokwi_reading(data)
            except json.JSONDecodeError:
                log.warning(f"Wokwi: bad JSON: {raw[:80]}")
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        log.info("Wokwi serial bridge disconnected — falling back to Python sim")
        simulator.state.wokwi_override = False

# ── Simulation loop ───────────────────────────────────────────────────────────
@app.on_event("startup")
async def start_sim_loop():
    asyncio.create_task(_sim_loop())
    log.info("Simulation loop started")

async def _sim_loop():
    """
    Runs at ~20 Hz real time.
    Each iteration advances the sim, runs detection + advisor + robot,
    then broadcasts full state to all dashboard clients every 500 ms.
    """
    tick_interval   = 0.05   # 50 ms real → 18 sim-s at 360x
    broadcast_every = 0.5    # seconds real time
    last_broadcast  = 0.0

    while True:
        t0 = time.monotonic()
        sim_state = simulator.tick(tick_interval)
        state_dict = simulator.to_dict()

        # Robot tick — returns (robot_dict, probes, moisture_additions)
        robot_dict, probes, moisture_adds = robot.tick(
            tick_interval * simulator.speed, state_dict
        )

        # Apply robot spot-watering back to sim
        for zid, gain in moisture_adds.items():
            sim_state.zones[zid].moisture = min(
                100.0, sim_state.zones[zid].moisture + gain
            )

        # Detection (pass robot probes for sensor verification)
        detector.update(state_dict, robot_probes=probes if probes else None)
        advisor.update(state_dict)

        # Collect new alerts into history
        for alert in detector.new_alerts:
            alert_history.append({"type": "detection", **alert.to_dict()})
            log.info(f"ALERT [{alert.severity}] {alert.title}")
        for tip in advisor.new_tips:
            alert_history.append({"type": "advice", **tip.to_dict()})
            log.info(f"TIP [{tip.severity}] {tip.title}")

        # Keep history bounded
        if len(alert_history) > 200:
            del alert_history[:-200]

        # Broadcast to dashboard clients at 2 Hz
        now = time.monotonic()
        if now - last_broadcast >= broadcast_every:
            last_broadcast = now
            payload = json.dumps(_build_full_state(state_dict, robot_dict))
            dead: Set[WebSocket] = set()
            for ws in clients:
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.add(ws)
            clients.difference_update(dead)

        # Sleep for remainder of tick interval
        elapsed = time.monotonic() - t0
        await asyncio.sleep(max(0, tick_interval - elapsed))


def _build_full_state(state_dict: dict | None = None,
                      robot_dict: dict | None = None) -> dict:
    if state_dict is None:
        state_dict = simulator.to_dict()
    if robot_dict is None:
        robot_dict = robot.rs.to_dict()
    return {
        **state_dict,
        "robot":   robot_dict,
        "alerts":  detector.get_all_active(),
        "tips":    advisor.get_all_active(),
        "history": alert_history[-50:],   # last 50 events for feed
    }

# ── Demo sequence ─────────────────────────────────────────────────────────────
async def _run_demo_sequence():
    """
    Automated 3-minute demo:
      0 s  — normal operation
      30 s — heatwave
      60 s — blocked line Zone 3
      90 s — leak Zone 4
      120 s— clear all faults, show final scoreboard
    """
    log.info("Demo sequence started")
    simulator.inject_fault("clear")
    simulator.set_speed(360.0)

    await asyncio.sleep(30)
    log.info("Demo: injecting heatwave")
    simulator.inject_fault("heatwave")

    await asyncio.sleep(30)
    log.info("Demo: injecting blocked_line")
    simulator.inject_fault("blocked_line")

    await asyncio.sleep(30)
    log.info("Demo: injecting leak")
    simulator.inject_fault("leak")

    await asyncio.sleep(30)
    log.info("Demo: clearing all faults")
    simulator.inject_fault("clear")
    log.info("Demo sequence complete")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.server:app", host="0.0.0.0", port=port, reload=False)
