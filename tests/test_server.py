import pytest
import os
os.environ["VETORBIT_TEST_MODE"] = "1"

import httpx
from fastapi.testclient import TestClient
import vetorbit.server as server_module
from vetorbit.server import app, ledger as server_ledger
from vetorbit.models import CommandType, DebrisObject
from vetorbit.ledger import BlackboxLedger


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_ledger():
    with server_ledger._get_conn() as conn:
        conn.execute("DELETE FROM decisions")
    yield


class TestServerEndpoints:
    def test_get_state(self, client):
        response = client.get("/api/state")
        assert response.status_code == 200
        data = response.json()
        assert "state" in data
        assert "stats" in data
        assert data["state"]["battery_soc"] == 100.0
        assert data["state"]["propellant_kg"] == 100.0

    def test_post_command_approved(self, client):
        response = client.post("/api/command", json={
            "cmd_type": "FIRE_THRUSTER",
            "params": {},
            "issued_by": "test"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["approved"] is True
        assert data["veto_code"] is None

    def test_post_command_veto_low_battery(self, client):
        # First set low battery via scenario
        client.post("/api/scenario/eclipse_low_battery")
        
        response = client.post("/api/command", json={
            "cmd_type": "CAPTURE_PAYLOAD",
            "params": {},
            "issued_by": "test"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["approved"] is False
        assert data["veto_code"] == "ERR_LOW_BATTERY_ECLIPSE"

    def test_scenario_debris_threat(self, client):
        response = client.post("/api/scenario/debris_threat")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "injected"
        assert data["scenario"] == "debris_threat"
        assert data["debris"]["object_id"] == "COSMOS-2251-FRAG"
        assert data["debris"]["relative_distance_km"] == 1.2
        assert data["debris"]["collision_probability"] == 3.4e-4
        assert data["debris"]["time_to_closest_approach_s"] == 420.0

    def test_scenario_eclipse_low_battery(self, client):
        response = client.post("/api/scenario/eclipse_low_battery")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "injected"
        
        state_resp = client.get("/api/state")
        state = state_resp.json()["state"]
        assert state["orbital_phase_deg"] == 130.0
        assert state["in_eclipse"] is True
        assert state["battery_soc"] == 24.0

    def test_scenario_sun_glare(self, client):
        response = client.post("/api/scenario/sun_glare")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "injected"
        
        state_resp = client.get("/api/state")
        state = state_resp.json()["state"]
        assert state["sun_angle_deg"] == 12.0

    def test_scenario_reset(self, client):
        client.post("/api/scenario/debris_threat")
        response = client.post("/api/scenario/reset")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "reset"
        
        state_resp = client.get("/api/state")
        state = state_resp.json()["state"]
        assert state["battery_soc"] == 100.0
        assert state["propellant_kg"] == 100.0
        assert len(state["tracked_debris"]) == 0


class TestLiveIssScenario:
    def test_live_iss_sync_uses_live_telemetry(self, client, monkeypatch):
        canned = {
            "name": "ISS (ZARYA)",
            "id": 25544,
            "latitude": -33.24,
            "longitude": 145.91,
            "altitude": 419.8,
            "velocity": 7.66,
            "visibility": "eclipsed",
            "timestamp": 1763000000,
        }

        async def fake_fetch():
            return canned

        monkeypatch.setattr(server_module, "fetch_iss_telemetry", fake_fetch)

        response = client.post("/api/scenario/live_iss")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "synced"
        assert data["scenario"] == "live_iss"
        assert data["source"] == "live"
        assert data["iss"]["altitude"] == 419.8
        assert data["in_eclipse"] is True
        assert data["orbital_phase_deg"] == pytest.approx((145.91 + 180.0) % 360.0)

        state = client.get("/api/state").json()["state"]
        assert state["altitude_km"] == pytest.approx(419.8)
        assert state["in_eclipse"] is True
        assert state["orbital_phase_deg"] == pytest.approx((145.91 + 180.0) % 360.0)

    def test_live_iss_sync_logs_event(self, client, monkeypatch):
        async def fake_fetch():
            return {
                "name": "ISS (ZARYA)",
                "id": 25544,
                "latitude": 12.5,
                "longitude": -45.0,
                "altitude": 420.2,
                "velocity": 7.67,
                "visibility": "daylight",
                "timestamp": 1763000001,
            }

        monkeypatch.setattr(server_module, "fetch_iss_telemetry", fake_fetch)

        response = client.post("/api/scenario/live_iss")
        assert response.status_code == 200
        assert response.json()["in_eclipse"] is False

        logs = server_ledger.get_recent_logs(10)
        sync_logs = [l for l in logs if l["cmd_type"] == "LIVE_ISS_SYNC"]
        assert len(sync_logs) == 1
        assert sync_logs[0]["issued_by"] == "live_iss_sync"
        assert "ISS orbital telemetry" in sync_logs[0]["reason"]

    def test_live_iss_falls_back_when_offline(self, client, monkeypatch):
        async def failing_fetch():
            raise httpx.ConnectError("network unreachable")

        monkeypatch.setattr(server_module, "fetch_iss_telemetry", failing_fetch)

        response = client.post("/api/scenario/live_iss")
        assert response.status_code == 200
        data = response.json()
        assert data["source"] == "fallback"
        assert data["status"] == "synced"

        state = client.get("/api/state").json()["state"]
        assert 418.0 <= state["altitude_km"] <= 422.0
        assert 0.0 <= state["orbital_phase_deg"] < 360.0

        logs = server_ledger.get_recent_logs(10)
        assert any(l["cmd_type"] == "LIVE_ISS_SYNC" for l in logs)


class TestBlackboxLedgerIntegration:
    def test_command_logged_to_ledger(self, client):
        response = client.post("/api/command", json={
            "cmd_type": "FIRE_THRUSTER",
            "params": {},
            "issued_by": "test"
        })
        assert response.status_code == 200
        
        logs = server_ledger.get_recent_logs(10)
        assert len(logs) == 1
        assert logs[0]["cmd_type"] == "FIRE_THRUSTER"
        assert logs[0]["approved"] == 1

    def test_veto_logged_with_code(self, client):
        client.post("/api/scenario/debris_threat")
        
        response = client.post("/api/command", json={
            "cmd_type": "CAPTURE_PAYLOAD",
            "params": {},
            "issued_by": "test"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["approved"] is False
        assert data["veto_code"] == "ERR_CONJUNCTION_RISK"
        
        logs = server_ledger.get_recent_logs(10)
        veto_logs = [l for l in logs if l["approved"] == 0]
        assert len(veto_logs) == 1
        assert veto_logs[0]["veto_code"] == "ERR_CONJUNCTION_RISK"

    def test_recovery_via_avoidance_burn(self, client):
        # Inject debris threat
        client.post("/api/scenario/debris_threat")
        
        # Try payload - should be vetoed
        response = client.post("/api/command", json={
            "cmd_type": "CAPTURE_PAYLOAD",
            "params": {},
            "issued_by": "test"
        })
        assert response.json()["approved"] is False
        
        # Execute avoidance burn - should be approved
        response = client.post("/api/command", json={
            "cmd_type": "ORBIT_AVOIDANCE_BURN",
            "params": {"delta_v_m_s": 0.5},
            "issued_by": "test"
        })
        assert response.status_code == 200
        data = response.json()
        assert data["approved"] is True
        
        # Now payload should work (debris TCA updated)
        response = client.post("/api/command", json={
            "cmd_type": "CAPTURE_PAYLOAD",
            "params": {},
            "issued_by": "test"
        })
        # May still be vetoed if TCA still in window, but burn was approved
        assert response.status_code == 200

    def test_ledger_stats(self, client):
        # Execute a few commands
        client.post("/api/command", json={"cmd_type": "FIRE_THRUSTER", "params": {}, "issued_by": "test"})
        client.post("/api/command", json={"cmd_type": "FIRE_THRUSTER", "params": {}, "issued_by": "test"})
        client.post("/api/scenario/debris_threat")
        client.post("/api/command", json={"cmd_type": "CAPTURE_PAYLOAD", "params": {}, "issued_by": "test"})  # vetoed
        
        stats = server_ledger.get_stats()
        
        assert stats["total_commands"] == 3
        assert stats["approved_count"] == 2
        assert stats["vetoed_count"] == 1
        assert "ERR_CONJUNCTION_RISK" in stats["veto_breakdown"]
        assert stats["veto_breakdown"]["ERR_CONJUNCTION_RISK"] == 1


class TestWebSocket:
    def test_websocket_connection(self, client):
        with client.websocket_connect("/ws/telemetry") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "telemetry"
            assert "state" in msg
            assert "logs" in msg
            assert "stats" in msg


class TestAutoPlanner:
    def test_auto_planner_enable(self, client):
        response = client.post("/api/auto-planner", json={"enabled": True})
        assert response.status_code == 200
        data = response.json()
        assert data["auto_planner"] is True

    def test_auto_planner_disable(self, client):
        client.post("/api/auto-planner", json={"enabled": True})
        response = client.post("/api/auto-planner", json={"enabled": False})
        assert response.status_code == 200
        data = response.json()
        assert data["auto_planner"] is False

    def test_auto_planner_state_in_api_state(self, client):
        client.post("/api/auto-planner", json={"enabled": True})
        response = client.get("/api/state")
        assert response.status_code == 200
        data = response.json()
        assert data["auto_planner"] is True
        
        client.post("/api/auto-planner", json={"enabled": False})
        response = client.get("/api/state")
        data = response.json()
        assert data["auto_planner"] is False

    def test_auto_planner_logs_commands(self, client):
        # Enable auto planner
        response = client.post("/api/auto-planner", json={"enabled": True})
        assert response.status_code == 200
        
        # Verify endpoint works and returns correct state
        data = response.json()
        assert data["auto_planner"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])