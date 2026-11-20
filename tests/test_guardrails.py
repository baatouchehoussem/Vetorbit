import pytest
from vetorbit.models import (
    DebrisObject,
    SpacecraftState,
    CommandType,
    SpacecraftCommand,
    PointingMode,
)
from vetorbit.physics import step_simulation, predict_state_after_command
from vetorbit.guardrails import DeterministicGuardrailEngine


class TestOrbitalEclipseTransitions:
    def test_eclipse_entry_at_115_deg(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=110.0)
        new_state = step_simulation(state, 100.0)
        assert new_state.orbital_phase_deg >= 115.0
        assert new_state.in_eclipse is True

    def test_eclipse_exit_at_245_deg(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=240.0, in_eclipse=True)
        new_state = step_simulation(state, 100.0)
        assert new_state.orbital_phase_deg >= 245.0
        assert new_state.in_eclipse is False

    def test_orbital_phase_wraps_at_360(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=350.0)
        new_state = step_simulation(state, 200.0)
        assert 0.0 <= new_state.orbital_phase_deg < 360.0

    def test_eclipse_battery_drain(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=120.0, battery_soc=50.0, in_eclipse=True)
        new_state = step_simulation(state, 60.0)
        assert new_state.battery_soc < 50.0

    def test_sunlight_battery_charge(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=50.0, battery_soc=50.0, in_eclipse=False)
        new_state = step_simulation(state, 60.0)
        assert new_state.battery_soc > 50.0


class TestDebrisConjunctionVetoes:
    def setup_method(self):
        self.engine = DeterministicGuardrailEngine()

    def test_veto_payload_on_close_debris(self):
        debris = DebrisObject(
            object_id="DEBRIS_001",
            relative_distance_km=1.5,
            closing_velocity_kms=0.1,
            time_to_closest_approach_s=500.0,
            collision_probability=1e-5
        )
        state = SpacecraftState(timestamp_s=0.0, tracked_debris=[debris])
        cmd = SpacecraftCommand(command_id="CMD_001", cmd_type=CommandType.CAPTURE_PAYLOAD)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_CONJUNCTION_RISK"

    def test_veto_downlink_on_close_debris(self):
        debris = DebrisObject(
            object_id="DEBRIS_002",
            relative_distance_km=1.0,
            closing_velocity_kms=0.05,
            time_to_closest_approach_s=800.0,
            collision_probability=1e-6
        )
        state = SpacecraftState(timestamp_s=0.0, tracked_debris=[debris])
        cmd = SpacecraftCommand(command_id="CMD_002", cmd_type=CommandType.DOWNLINK_DATA)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_CONJUNCTION_RISK"

    def test_veto_slew_on_high_collision_probability(self):
        debris = DebrisObject(
            object_id="DEBRIS_003",
            relative_distance_km=5.0,
            closing_velocity_kms=0.2,
            time_to_closest_approach_s=600.0,
            collision_probability=5e-4
        )
        state = SpacecraftState(timestamp_s=0.0, tracked_debris=[debris])
        cmd = SpacecraftCommand(command_id="CMD_003", cmd_type=CommandType.SLEW_TO_TARGET)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_CONJUNCTION_RISK"

    def test_allow_avoidance_burn_during_conjunction(self):
        debris = DebrisObject(
            object_id="DEBRIS_004",
            relative_distance_km=1.0,
            closing_velocity_kms=0.1,
            time_to_closest_approach_s=300.0,
            collision_probability=1e-3
        )
        state = SpacecraftState(timestamp_s=0.0, tracked_debris=[debris], propellant_kg=50.0)
        cmd = SpacecraftCommand(command_id="CMD_004", cmd_type=CommandType.ORBIT_AVOIDANCE_BURN)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True

    def test_allow_payload_when_debris_far(self):
        debris = DebrisObject(
            object_id="DEBRIS_005",
            relative_distance_km=10.0,
            closing_velocity_kms=0.01,
            time_to_closest_approach_s=5000.0,
            collision_probability=1e-6
        )
        state = SpacecraftState(timestamp_s=0.0, tracked_debris=[debris])
        cmd = SpacecraftCommand(command_id="CMD_005", cmd_type=CommandType.CAPTURE_PAYLOAD)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True

    def test_conjunction_time_window_900s(self):
        debris = DebrisObject(
            object_id="DEBRIS_006",
            relative_distance_km=5.0,
            closing_velocity_kms=0.01,
            time_to_closest_approach_s=1000.0,
            collision_probability=5e-4
        )
        state = SpacecraftState(timestamp_s=0.0, tracked_debris=[debris])
        cmd = SpacecraftCommand(command_id="CMD_006", cmd_type=CommandType.CAPTURE_PAYLOAD)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True


class TestAvoidanceBurns:
    def setup_method(self):
        self.engine = DeterministicGuardrailEngine()

    def test_avoidance_burn_consumes_propellant(self):
        state = SpacecraftState(timestamp_s=0.0, propellant_kg=10.0)
        cmd = SpacecraftCommand(command_id="CMD_007", cmd_type=CommandType.ORBIT_AVOIDANCE_BURN)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True
        assert result.predicted_state is not None
        assert result.predicted_state.propellant_kg < 10.0

    def test_avoidance_burn_updates_debris_tca(self):
        debris = DebrisObject(
            object_id="DEBRIS_007",
            relative_distance_km=1.0,
            closing_velocity_kms=0.1,
            time_to_closest_approach_s=200.0,
            collision_probability=1e-3
        )
        state = SpacecraftState(timestamp_s=0.0, tracked_debris=[debris], propellant_kg=50.0)
        cmd = SpacecraftCommand(command_id="CMD_008", cmd_type=CommandType.ORBIT_AVOIDANCE_BURN)
        result = self.engine.evaluate(state, cmd)
        assert result.predicted_state is not None
        updated_debris = result.predicted_state.tracked_debris[0]
        assert updated_debris.time_to_closest_approach_s < 200.0


class TestBatteryMargins:
    def setup_method(self):
        self.engine = DeterministicGuardrailEngine()

    def test_veto_high_power_below_20pct(self):
        state = SpacecraftState(timestamp_s=0.0, battery_soc=15.0, in_eclipse=False)
        cmd = SpacecraftCommand(command_id="CMD_009", cmd_type=CommandType.SLEW_TO_TARGET)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_LOW_BATTERY_ECLIPSE"

    def test_veto_in_eclipse_below_30pct(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=120.0, battery_soc=25.0)
        cmd = SpacecraftCommand(command_id="CMD_010", cmd_type=CommandType.CAPTURE_PAYLOAD)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_LOW_BATTERY_ECLIPSE"

    def test_allow_high_power_above_30pct_in_eclipse(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=120.0, battery_soc=35.0)
        cmd = SpacecraftCommand(command_id="CMD_011", cmd_type=CommandType.SLEW_TO_TARGET)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True

    def test_allow_low_power_commands_at_low_battery(self):
        state = SpacecraftState(timestamp_s=0.0, battery_soc=10.0)
        cmd = SpacecraftCommand(command_id="CMD_012", cmd_type=CommandType.FIRE_THRUSTER)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True

    def test_entering_eclipse_predicts_low_battery(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=110.0, battery_soc=28.0, in_eclipse=False)
        cmd = SpacecraftCommand(command_id="CMD_013", cmd_type=CommandType.SLEW_TO_TARGET)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_LOW_BATTERY_ECLIPSE"


class TestSunExclusionAngles:
    def setup_method(self):
        self.engine = DeterministicGuardrailEngine()

    def test_veto_slew_below_25_deg(self):
        state = SpacecraftState(timestamp_s=0.0, sun_angle_deg=90.0)
        cmd = SpacecraftCommand(
            command_id="CMD_014",
            cmd_type=CommandType.SLEW_TO_TARGET,
            params={"target_sun_angle_deg": 20.0}
        )
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_SUN_EXCLUSION_VIOLATION"

    def test_veto_payload_below_25_deg(self):
        state = SpacecraftState(timestamp_s=0.0, sun_angle_deg=90.0)
        cmd = SpacecraftCommand(
            command_id="CMD_015",
            cmd_type=CommandType.CAPTURE_PAYLOAD,
            params={"target_sun_angle_deg": 10.0}
        )
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_SUN_EXCLUSION_VIOLATION"

    def test_allow_slew_at_25_deg(self):
        state = SpacecraftState(timestamp_s=0.0, sun_angle_deg=90.0)
        cmd = SpacecraftCommand(
            command_id="CMD_016",
            cmd_type=CommandType.SLEW_TO_TARGET,
            params={"target_sun_angle_deg": 25.0}
        )
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True

    def test_allow_slew_above_25_deg(self):
        state = SpacecraftState(timestamp_s=0.0, sun_angle_deg=90.0)
        cmd = SpacecraftCommand(
            command_id="CMD_017",
            cmd_type=CommandType.SLEW_TO_TARGET,
            params={"target_sun_angle_deg": 45.0}
        )
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True

    def test_other_commands_unaffected_by_sun_angle(self):
        state = SpacecraftState(timestamp_s=0.0, sun_angle_deg=10.0)
        cmd = SpacecraftCommand(command_id="CMD_018", cmd_type=CommandType.DOWNLINK_DATA)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True


class TestThermalLimits:
    def setup_method(self):
        self.engine = DeterministicGuardrailEngine()

    def test_veto_high_power_above_65c(self):
        state = SpacecraftState(timestamp_s=0.0, bus_temp_c=70.0)
        cmd = SpacecraftCommand(command_id="CMD_019", cmd_type=CommandType.SLEW_TO_TARGET)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_THERMAL_LIMIT"

    def test_veto_payload_above_65c(self):
        state = SpacecraftState(timestamp_s=0.0, bus_temp_c=66.0)
        cmd = SpacecraftCommand(command_id="CMD_020", cmd_type=CommandType.CAPTURE_PAYLOAD)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_THERMAL_LIMIT"

    def test_allow_at_65c(self):
        state = SpacecraftState(timestamp_s=0.0, bus_temp_c=65.0)
        cmd = SpacecraftCommand(command_id="CMD_021", cmd_type=CommandType.SLEW_TO_TARGET)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True

    def test_allow_low_power_above_65c(self):
        state = SpacecraftState(timestamp_s=0.0, bus_temp_c=70.0)
        cmd = SpacecraftCommand(command_id="CMD_022", cmd_type=CommandType.FIRE_THRUSTER)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True


class TestPropellantAndBufferLimits:
    def setup_method(self):
        self.engine = DeterministicGuardrailEngine()

    def test_veto_thruster_no_propellant(self):
        state = SpacecraftState(timestamp_s=0.0, propellant_kg=0.0)
        cmd = SpacecraftCommand(command_id="CMD_023", cmd_type=CommandType.FIRE_THRUSTER)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_PROPELLANT_DEPLETED"

    def test_veto_avoidance_no_propellant(self):
        state = SpacecraftState(timestamp_s=0.0, propellant_kg=0.0)
        cmd = SpacecraftCommand(command_id="CMD_024", cmd_type=CommandType.ORBIT_AVOIDANCE_BURN)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_PROPELLANT_DEPLETED"

    def test_veto_payload_buffer_overflow(self):
        state = SpacecraftState(timestamp_s=0.0, downlink_buffer_mb=950.0)
        cmd = SpacecraftCommand(
            command_id="CMD_025",
            cmd_type=CommandType.CAPTURE_PAYLOAD,
            params={"data_mb": 100.0}
        )
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_BUFFER_OVERFLOW"

    def test_allow_payload_within_buffer(self):
        state = SpacecraftState(timestamp_s=0.0, downlink_buffer_mb=800.0)
        cmd = SpacecraftCommand(
            command_id="CMD_026",
            cmd_type=CommandType.CAPTURE_PAYLOAD,
            params={"data_mb": 100.0}
        )
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True


class TestSafeModeOverride:
    def setup_method(self):
        self.engine = DeterministicGuardrailEngine()

    def test_always_approve_safe_mode(self):
        state = SpacecraftState(
            timestamp_s=0.0,
            battery_soc=5.0,
            bus_temp_c=80.0,
            propellant_kg=0.0,
            in_eclipse=True
        )
        cmd = SpacecraftCommand(command_id="CMD_027", cmd_type=CommandType.ENTER_SAFE_MODE)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is True
        assert result.veto_code is None
        assert "SAFE_MODE_OVERRIDE" in result.reason

    def test_veto_safe_mode_when_already_safe(self):
        state = SpacecraftState(timestamp_s=0.0, pointing_mode=PointingMode.SAFE)
        cmd = SpacecraftCommand(command_id="CMD_028", cmd_type=CommandType.ENTER_SAFE_MODE)
        result = self.engine.evaluate(state, cmd)
        assert result.approved is False
        assert result.veto_code == "ERR_ALREADY_IN_SAFE_MODE"

    def test_safe_mode_changes_pointing_mode(self):
        state = SpacecraftState(timestamp_s=0.0, pointing_mode=PointingMode.NADIR)
        cmd = SpacecraftCommand(command_id="CMD_029", cmd_type=CommandType.ENTER_SAFE_MODE)
        result = self.engine.evaluate(state, cmd)
        assert result.predicted_state is not None
        assert result.predicted_state.pointing_mode == PointingMode.SAFE


class TestPhysicsIntegration:
    def test_step_simulation_updates_timestamp(self):
        state = SpacecraftState(timestamp_s=100.0)
        new_state = step_simulation(state, 10.0)
        assert new_state.timestamp_s == 110.0

    def test_step_simulation_updates_orbital_phase(self):
        state = SpacecraftState(timestamp_s=0.0, orbital_phase_deg=0.0)
        new_state = step_simulation(state, 5820.0)
        assert abs(new_state.orbital_phase_deg - 0.0) < 1.0

    def test_predict_state_after_slew(self):
        state = SpacecraftState(timestamp_s=0.0, pointing_mode=PointingMode.NADIR, sun_angle_deg=90.0)
        cmd = SpacecraftCommand(
            command_id="CMD_030",
            cmd_type=CommandType.SLEW_TO_TARGET,
            params={"target_sun_angle_deg": 45.0}
        )
        predicted = predict_state_after_command(state, cmd)
        assert predicted.pointing_mode == PointingMode.TARGET
        assert predicted.sun_angle_deg == 45.0
        assert predicted.timestamp_s > state.timestamp_s

    def test_predict_state_after_downlink(self):
        state = SpacecraftState(timestamp_s=0.0, downlink_buffer_mb=100.0)
        cmd = SpacecraftCommand(command_id="CMD_031", cmd_type=CommandType.DOWNLINK_DATA)
        predicted = predict_state_after_command(state, cmd)
        assert predicted.downlink_buffer_mb == 0.0
        assert predicted.timestamp_s > state.timestamp_s


if __name__ == "__main__":
    pytest.main([__file__, "-v"])