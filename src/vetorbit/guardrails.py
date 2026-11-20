from vetorbit.models import SpacecraftState, SpacecraftCommand, CommandType, ValidationResult, PointingMode
from vetorbit.physics import predict_state_after_command


CONJUNCTION_DISTANCE_THRESHOLD_KM = 2.0
CONJUNCTION_PROBABILITY_THRESHOLD = 1e-4
CONJUNCTION_TIME_WINDOW_S = 900.0
LOW_BATTERY_THRESHOLD_PCT = 20.0
LOW_BATTERY_ECLIPSE_THRESHOLD_PCT = 30.0
SUN_EXCLUSION_ANGLE_DEG = 25.0
THERMAL_LIMIT_C = 65.0
MAX_DOWNLINK_BUFFER_MB = 1000.0


class DeterministicGuardrailEngine:
    def evaluate(self, state: SpacecraftState, cmd: SpacecraftCommand) -> ValidationResult:
        if cmd.cmd_type == CommandType.ENTER_SAFE_MODE:
            if state.pointing_mode == PointingMode.SAFE:
                return ValidationResult(
                    approved=False,
                    command_id=cmd.command_id,
                    veto_code="ERR_ALREADY_IN_SAFE_MODE",
                    reason="Spacecraft already in SAFE mode",
                    predicted_state=state,
                    safety_margins={}
                )
            predicted = predict_state_after_command(state, cmd)
            return ValidationResult(
                approved=True,
                command_id=cmd.command_id,
                veto_code=None,
                reason="SAFE_MODE_OVERRIDE: Emergency safe mode entry always permitted",
                predicted_state=predicted,
                safety_margins={"battery_margin_pct": predicted.battery_soc}
            )

        predicted = predict_state_after_command(state, cmd)

        if self._check_conjunction_risk(state, cmd):
            return ValidationResult(
                approved=False,
                command_id=cmd.command_id,
                veto_code="ERR_CONJUNCTION_RISK",
                reason="Active debris conjunction requires avoidance burn first",
                predicted_state=predicted,
                safety_margins={
                    "min_debris_distance_km": self._min_debris_distance(state),
                    "max_collision_probability": self._max_collision_probability(state)
                }
            )

        if self._check_low_battery_eclipse(state, predicted, cmd):
            return ValidationResult(
                approved=False,
                command_id=cmd.command_id,
                veto_code="ERR_LOW_BATTERY_ECLIPSE",
                reason="Insufficient battery margin for high-power command",
                predicted_state=predicted,
                safety_margins={"current_battery_soc": state.battery_soc, "predicted_battery_soc": predicted.battery_soc}
            )

        if self._check_sun_exclusion(cmd, predicted):
            return ValidationResult(
                approved=False,
                command_id=cmd.command_id,
                veto_code="ERR_SUN_EXCLUSION_VIOLATION",
                reason="Target sun angle violates exclusion zone",
                predicted_state=predicted,
                safety_margins={"target_sun_angle_deg": predicted.sun_angle_deg}
            )

        if self._check_thermal_limit(state, cmd):
            return ValidationResult(
                approved=False,
                command_id=cmd.command_id,
                veto_code="ERR_THERMAL_LIMIT",
                reason="Current bus temperature exceeds thermal limit",
                predicted_state=predicted,
                safety_margins={"current_bus_temp_c": state.bus_temp_c}
            )

        if self._check_propellant_depleted(predicted, cmd):
            return ValidationResult(
                approved=False,
                command_id=cmd.command_id,
                veto_code="ERR_PROPELLANT_DEPLETED",
                reason="Insufficient propellant for maneuver",
                predicted_state=predicted,
                safety_margins={"remaining_propellant_kg": predicted.propellant_kg}
            )

        if self._check_buffer_overflow(predicted, cmd):
            return ValidationResult(
                approved=False,
                command_id=cmd.command_id,
                veto_code="ERR_BUFFER_OVERFLOW",
                reason="Downlink buffer would exceed capacity",
                predicted_state=predicted,
                safety_margins={"predicted_buffer_mb": predicted.downlink_buffer_mb}
            )

        return ValidationResult(
            approved=True,
            command_id=cmd.command_id,
            veto_code=None,
            reason="All safety checks passed",
            predicted_state=predicted,
            safety_margins={
                "battery_margin_pct": predicted.battery_soc - LOW_BATTERY_THRESHOLD_PCT,
                "thermal_margin_c": THERMAL_LIMIT_C - predicted.bus_temp_c,
                "propellant_margin_kg": predicted.propellant_kg,
                "buffer_margin_mb": MAX_DOWNLINK_BUFFER_MB - predicted.downlink_buffer_mb
            }
        )

    def _check_conjunction_risk(self, state: SpacecraftState, cmd: SpacecraftCommand) -> bool:
        if cmd.cmd_type in (CommandType.CAPTURE_PAYLOAD, CommandType.DOWNLINK_DATA, CommandType.SLEW_TO_TARGET):
            for debris in state.tracked_debris:
                if (debris.relative_distance_km < CONJUNCTION_DISTANCE_THRESHOLD_KM or
                    (debris.collision_probability > CONJUNCTION_PROBABILITY_THRESHOLD and
                     debris.time_to_closest_approach_s < CONJUNCTION_TIME_WINDOW_S)):
                    return True
        return False

    def _check_low_battery_eclipse(self, current: SpacecraftState, predicted: SpacecraftState, cmd: SpacecraftCommand) -> bool:
        high_power_cmds = {CommandType.SLEW_TO_TARGET, CommandType.CAPTURE_PAYLOAD, CommandType.DOWNLINK_DATA}
        if cmd.cmd_type not in high_power_cmds:
            return False
        in_eclipse = current.in_eclipse or predicted.in_eclipse
        if in_eclipse:
            if current.battery_soc < LOW_BATTERY_ECLIPSE_THRESHOLD_PCT:
                return True
        else:
            if predicted.battery_soc < LOW_BATTERY_THRESHOLD_PCT:
                return True
        return False

    def _check_sun_exclusion(self, cmd: SpacecraftCommand, predicted: SpacecraftState) -> bool:
        if cmd.cmd_type in (CommandType.SLEW_TO_TARGET, CommandType.CAPTURE_PAYLOAD):
            target_angle = cmd.params.get("target_sun_angle_deg", predicted.sun_angle_deg)
            if target_angle < SUN_EXCLUSION_ANGLE_DEG:
                return True
        return False

    def _check_thermal_limit(self, current: SpacecraftState, cmd: SpacecraftCommand) -> bool:
        high_power_cmds = {CommandType.SLEW_TO_TARGET, CommandType.CAPTURE_PAYLOAD, CommandType.DOWNLINK_DATA}
        if cmd.cmd_type in high_power_cmds and current.bus_temp_c > THERMAL_LIMIT_C:
            return True
        return False

    def _check_propellant_depleted(self, predicted: SpacecraftState, cmd: SpacecraftCommand) -> bool:
        if cmd.cmd_type in (CommandType.FIRE_THRUSTER, CommandType.ORBIT_AVOIDANCE_BURN):
            return predicted.propellant_kg <= 0.0
        return False

    def _check_buffer_overflow(self, predicted: SpacecraftState, cmd: SpacecraftCommand) -> bool:
        if cmd.cmd_type == CommandType.CAPTURE_PAYLOAD:
            data_generated = cmd.params.get("data_mb", 100.0)
            return predicted.downlink_buffer_mb + data_generated > MAX_DOWNLINK_BUFFER_MB
        return False

    def _min_debris_distance(self, state: SpacecraftState) -> float:
        if not state.tracked_debris:
            return float('inf')
        return min(d.relative_distance_km for d in state.tracked_debris)

    def _max_collision_probability(self, state: SpacecraftState) -> float:
        if not state.tracked_debris:
            return 0.0
        return max(d.collision_probability for d in state.tracked_debris)