import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from vetorbit.models import (
    SpacecraftState,
    SpacecraftCommand,
    CommandType,
    ValidationResult,
    DebrisObject,
    PointingMode
)
from vetorbit.guardrails import DeterministicGuardrailEngine
from vetorbit.physics import step_simulation
from vetorbit.ledger import BlackboxLedger


SIMULATION_DT_S = 15.0
SIMULATION_INTERVAL_S = 1.0
AUTO_PLANNER_INTERVAL_S = 2.5

ISS_API_URL = "https://api.wheretheiss.at/v1/satellites/25544"
ISS_FALLBACK_ALTITUDE_KM = 420.6
ISS_ORBITAL_VELOCITY_KMS = 7.66

TEST_MODE = os.environ.get("VETORBIT_TEST_MODE") == "1"
TEST_DB_PATH = "file:vetorbit_test?mode=memory&cache=shared" if TEST_MODE else "vetorbit_blackbox.db"


class CommandRequest(BaseModel):
    cmd_type: CommandType
    params: dict = {}
    issued_by: str = "operator"


class AutoPlannerRequest(BaseModel):
    enabled: bool


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections[:]:
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()
engine = DeterministicGuardrailEngine()
ledger = BlackboxLedger(TEST_DB_PATH)


class SimulationState:
    def __init__(self):
        self.state = SpacecraftState(timestamp_s=time.time())
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.auto_planner_enabled = False
        self.auto_planner_task: Optional[asyncio.Task] = None
        self.thruster_active = False
        self.thruster_direction = 0.0
        self.thruster_end_time = 0.0

    def reset(self):
        self.state = SpacecraftState(timestamp_s=time.time())
        self.thruster_active = False


sim = SimulationState()


def get_current_state() -> SpacecraftState:
    return sim.state


def fallback_iss_telemetry() -> dict:
    return {
        "name": "ISS (ZARYA)",
        "id": 25544,
        "latitude": 8.42,
        "longitude": -67.35,
        "altitude": ISS_FALLBACK_ALTITUDE_KM,
        "velocity": ISS_ORBITAL_VELOCITY_KMS,
        "visibility": "daylight",
        "timestamp": int(time.time()),
    }


async def fetch_iss_telemetry() -> dict:
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(ISS_API_URL)
        response.raise_for_status()
        return response.json()


def log_telemetry_event(event_type: str, reason: str, details: dict, state: SpacecraftState) -> None:
    timestamp_utc = datetime.now(timezone.utc).isoformat()
    with ledger._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO decisions (
                timestamp_utc, command_id, cmd_type, cmd_params, issued_by,
                approved, veto_code, reason, safety_margins, spacecraft_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp_utc,
                f"EVT_{int(time.time() * 1000)}",
                event_type,
                json.dumps(details),
                "live_iss_sync",
                1,
                None,
                reason,
                json.dumps({}),
                state.model_dump_json()
            )
        )


async def simulation_loop():
    while sim.running:
        sim.state = step_simulation(sim.state, SIMULATION_DT_S)
        
        if sim.thruster_active and time.time() > sim.thruster_end_time:
            sim.thruster_active = False
        
        await broadcast_telemetry()
        await asyncio.sleep(SIMULATION_INTERVAL_S)


async def auto_planner_loop():
    command_types = [
        CommandType.CAPTURE_PAYLOAD,
        CommandType.SLEW_TO_TARGET,
        CommandType.DOWNLINK_DATA,
    ]
    
    while sim.auto_planner_enabled:
        await asyncio.sleep(AUTO_PLANNER_INTERVAL_S)
        
        if not sim.auto_planner_enabled:
            break
            
        has_debris = len(sim.state.tracked_debris) > 0
        debris_threat = False
        if has_debris:
            for debris in sim.state.tracked_debris:
                if (debris.relative_distance_km < 2.0 or
                    (debris.collision_probability > 1e-4 and debris.time_to_closest_approach_s < 900.0)):
                    debris_threat = True
                    break
        
        if debris_threat:
            cmd_type = CommandType.ORBIT_AVOIDANCE_BURN
            params = {"delta_v_m_s": 0.5}
        else:
            cmd_type = random.choice(command_types)
            params = {}
            if cmd_type == CommandType.SLEW_TO_TARGET:
                params = {"target_sun_angle_deg": random.uniform(30.0, 90.0)}
            elif cmd_type == CommandType.CAPTURE_PAYLOAD:
                params = {"data_mb": 100.0}
            elif cmd_type == CommandType.DOWNLINK_DATA:
                params = {"rate_mb_per_s": 10.0}
        
        cmd = SpacecraftCommand(
            command_id=f"AUTO_{int(time.time() * 1000)}",
            cmd_type=cmd_type,
            params=params,
            issued_by="autonomous_planner"
        )
        
        result = engine.evaluate(sim.state, cmd)
        
        if result.approved:
            sim.state = result.predicted_state if result.predicted_state else sim.state
            if cmd_type == CommandType.ORBIT_AVOIDANCE_BURN:
                sim.thruster_active = True
                sim.thruster_direction = sim.state.orbital_phase_deg + 90
                sim.thruster_end_time = time.time() + 2.0
        
        ledger.log_decision(cmd, result, sim.state)
        await broadcast_telemetry()


async def broadcast_telemetry():
    recent_logs = ledger.get_recent_logs(30)
    stats = ledger.get_stats()
    message = {
        "type": "telemetry",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "state": sim.state.model_dump(),
        "logs": recent_logs,
        "stats": stats,
        "thruster": {
            "active": sim.thruster_active,
            "direction": sim.thruster_direction,
        },
        "auto_planner": sim.auto_planner_enabled
    }
    await manager.broadcast(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not TEST_MODE:
        sim.running = True
        sim.task = asyncio.create_task(simulation_loop())
    yield
    if not TEST_MODE:
        sim.running = False
        sim.auto_planner_enabled = False
        if sim.task:
            sim.task.cancel()
            try:
                await sim.task
            except asyncio.CancelledError:
                pass
        if sim.auto_planner_task:
            sim.auto_planner_task.cancel()
            try:
                await sim.auto_planner_task
            except asyncio.CancelledError:
                pass


app = FastAPI(title="Vetorbit Mission Control", lifespan=lifespan)

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return {"message": "Vetorbit Mission Control - UI not found"}


@app.get("/api/state")
async def get_state():
    return {
        "state": sim.state.model_dump(),
        "stats": ledger.get_stats(),
        "thruster": {
            "active": sim.thruster_active,
            "direction": sim.thruster_direction,
        },
        "auto_planner": sim.auto_planner_enabled
    }


@app.post("/api/command")
async def execute_command(request: CommandRequest):
    cmd = SpacecraftCommand(
        command_id=f"CMD_{int(time.time() * 1000)}",
        cmd_type=request.cmd_type,
        params=request.params,
        issued_by=request.issued_by
    )

    result = engine.evaluate(sim.state, cmd)

    if result.approved:
        sim.state = result.predicted_state if result.predicted_state else sim.state
        if cmd.cmd_type == CommandType.ORBIT_AVOIDANCE_BURN:
            sim.thruster_active = True
            sim.thruster_direction = sim.state.orbital_phase_deg + 90
            sim.thruster_end_time = time.time() + 2.0

    ledger.log_decision(cmd, result, sim.state)
    await broadcast_telemetry()

    return result.model_dump()


@app.post("/api/auto-planner")
async def toggle_auto_planner(request: AutoPlannerRequest):
    sim.auto_planner_enabled = request.enabled
    if request.enabled and not sim.auto_planner_task:
        sim.auto_planner_task = asyncio.create_task(auto_planner_loop())
    elif not request.enabled and sim.auto_planner_task:
        sim.auto_planner_task.cancel()
        try:
            await sim.auto_planner_task
        except asyncio.CancelledError:
            pass
        sim.auto_planner_task = None
    await broadcast_telemetry()
    return {"auto_planner": sim.auto_planner_enabled}


@app.post("/api/scenario/live_iss")
async def sync_live_iss():
    source = "live"
    try:
        iss = await fetch_iss_telemetry()
    except Exception:
        source = "fallback"
        iss = fallback_iss_telemetry()

    altitude_km = float(iss.get("altitude", ISS_FALLBACK_ALTITUDE_KM))
    latitude = float(iss.get("latitude", 0.0))
    longitude = float(iss.get("longitude", 0.0))
    visibility = str(iss.get("visibility", "daylight"))
    velocity_kms = float(iss.get("velocity", ISS_ORBITAL_VELOCITY_KMS))

    orbital_phase_deg = (longitude + 180.0) % 360.0
    in_eclipse = visibility == "eclipsed"

    sim.state = SpacecraftState(
        timestamp_s=time.time(),
        altitude_km=altitude_km,
        orbital_phase_deg=orbital_phase_deg,
        in_eclipse=in_eclipse,
        battery_soc=sim.state.battery_soc,
        bus_temp_c=sim.state.bus_temp_c,
        sun_angle_deg=sim.state.sun_angle_deg,
        pointing_mode=sim.state.pointing_mode,
        downlink_buffer_mb=sim.state.downlink_buffer_mb,
        propellant_kg=sim.state.propellant_kg,
        tracked_debris=sim.state.tracked_debris
    )

    log_telemetry_event(
        "LIVE_ISS_SYNC",
        (
            f"Synced real-time ISS orbital telemetry "
            f"(source={source}, alt={altitude_km:.1f}km, phase={orbital_phase_deg:.1f}deg, "
            f"lat={latitude:.2f}, lon={longitude:.2f}, eclipse={in_eclipse})"
        ),
        {"source": source, "iss": iss},
        sim.state
    )
    await broadcast_telemetry()

    return {
        "status": "synced",
        "scenario": "live_iss",
        "source": source,
        "iss": iss,
        "latitude": latitude,
        "longitude": longitude,
        "altitude_km": altitude_km,
        "velocity_kms": velocity_kms,
        "visibility": visibility,
        "orbital_phase_deg": orbital_phase_deg,
        "in_eclipse": in_eclipse,
        "state": sim.state.model_dump()
    }


@app.post("/api/scenario/{scenario_name}")
async def inject_scenario(scenario_name: str):
    if scenario_name == "debris_threat":
        debris = DebrisObject(
            object_id="COSMOS-2251-FRAG",
            relative_distance_km=1.2,
            closing_velocity_kms=0.01,
            time_to_closest_approach_s=420.0,
            collision_probability=3.4e-4
        )
        sim.state = SpacecraftState(
            timestamp_s=sim.state.timestamp_s,
            altitude_km=sim.state.altitude_km,
            orbital_phase_deg=sim.state.orbital_phase_deg,
            in_eclipse=sim.state.in_eclipse,
            battery_soc=sim.state.battery_soc,
            bus_temp_c=sim.state.bus_temp_c,
            sun_angle_deg=sim.state.sun_angle_deg,
            pointing_mode=sim.state.pointing_mode,
            downlink_buffer_mb=sim.state.downlink_buffer_mb,
            propellant_kg=sim.state.propellant_kg,
            tracked_debris=[debris]
        )
        await broadcast_telemetry()
        return {"status": "injected", "scenario": "debris_threat", "debris": debris.model_dump()}

    elif scenario_name == "eclipse_low_battery":
        sim.state = SpacecraftState(
            timestamp_s=sim.state.timestamp_s,
            altitude_km=sim.state.altitude_km,
            orbital_phase_deg=130.0,
            in_eclipse=True,
            battery_soc=24.0,
            bus_temp_c=sim.state.bus_temp_c,
            sun_angle_deg=sim.state.sun_angle_deg,
            pointing_mode=sim.state.pointing_mode,
            downlink_buffer_mb=sim.state.downlink_buffer_mb,
            propellant_kg=sim.state.propellant_kg,
            tracked_debris=sim.state.tracked_debris
        )
        await broadcast_telemetry()
        return {"status": "injected", "scenario": "eclipse_low_battery"}

    elif scenario_name == "sun_glare":
        sim.state = SpacecraftState(
            timestamp_s=sim.state.timestamp_s,
            altitude_km=sim.state.altitude_km,
            orbital_phase_deg=sim.state.orbital_phase_deg,
            in_eclipse=sim.state.in_eclipse,
            battery_soc=sim.state.battery_soc,
            bus_temp_c=sim.state.bus_temp_c,
            sun_angle_deg=12.0,
            pointing_mode=sim.state.pointing_mode,
            downlink_buffer_mb=sim.state.downlink_buffer_mb,
            propellant_kg=sim.state.propellant_kg,
            tracked_debris=sim.state.tracked_debris
        )
        await broadcast_telemetry()
        return {"status": "injected", "scenario": "sun_glare"}

    elif scenario_name == "reset":
        sim.reset()
        await broadcast_telemetry()
        return {"status": "reset", "scenario": "reset"}

    else:
        raise HTTPException(status_code=404, detail=f"Unknown scenario: {scenario_name}")


@app.websocket("/ws/telemetry")
async def websocket_telemetry(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({
            "type": "telemetry",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "state": sim.state.model_dump(),
            "logs": ledger.get_recent_logs(30),
            "stats": ledger.get_stats(),
            "thruster": {"active": sim.thruster_active, "direction": sim.thruster_direction},
            "auto_planner": sim.auto_planner_enabled
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)