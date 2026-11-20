from vetorbit.models import SpacecraftState, DebrisObject, SpacecraftCommand, CommandType, PointingMode


ORBITAL_RATE_DEG_PER_S = 360.0 / 5820.0
ECLIPSE_START_DEG = 115.0
ECLIPSE_END_DEG = 245.0
SOLAR_CHARGING_RATE_PCT_PER_S = 0.05
BASE_AVIONICS_LOAD_PCT_PER_S = 0.01
SLEW_POWER_PCT_PER_S = 0.5
PAYLOAD_POWER_PCT_PER_S = 1.0
DOWNLINK_POWER_PCT_PER_S = 0.3
THERMAL_TIME_CONSTANT_S = 300.0
THERMAL_EQUILIBRIUM_SUN_C = 45.0
THERMAL_EQUILIBRIUM_ECLIPSE_C = -10.0
THRUSTER_DV_PER_KG = 300.0
AVOIDANCE_BURN_DV_M_S = 0.5
SLEW_DURATION_S = 30.0
PAYLOAD_DURATION_S = 60.0
DOWNLINK_RATE_MB_PER_S = 10.0


def _update_orbital_phase(state: SpacecraftState, dt_s: float) -> float:
    new_phase = (state.orbital_phase_deg + ORBITAL_RATE_DEG_PER_S * dt_s) % 360.0
    return new_phase


def _update_eclipse(state: SpacecraftState, new_phase: float) -> bool:
    in_eclipse = ECLIPSE_START_DEG <= new_phase <= ECLIPSE_END_DEG
    return in_eclipse


def _update_battery(state: SpacecraftState, dt_s: float, in_eclipse: bool, cmd: SpacecraftCommand | None = None) -> float:
    if in_eclipse:
        charge_rate = -BASE_AVIONICS_LOAD_PCT_PER_S
    else:
        charge_rate = SOLAR_CHARGING_RATE_PCT_PER_S - BASE_AVIONICS_LOAD_PCT_PER_S

    if cmd:
        if cmd.cmd_type == CommandType.SLEW_TO_TARGET:
            charge_rate -= SLEW_POWER_PCT_PER_S
        elif cmd.cmd_type == CommandType.CAPTURE_PAYLOAD:
            charge_rate -= PAYLOAD_POWER_PCT_PER_S
        elif cmd.cmd_type == CommandType.DOWNLINK_DATA:
            charge_rate -= DOWNLINK_POWER_PCT_PER_S

    new_soc = state.battery_soc + charge_rate * dt_s
    return max(0.0, min(100.0, new_soc))


def _update_thermal(state: SpacecraftState, dt_s: float, in_eclipse: float) -> float:
    target_temp = THERMAL_EQUILIBRIUM_ECLIPSE_C if in_eclipse else THERMAL_EQUILIBRIUM_SUN_C
    alpha = dt_s / THERMAL_TIME_CONSTANT_S
    new_temp = state.bus_temp_c + alpha * (target_temp - state.bus_temp_c)
    return new_temp


def _update_debris(state: SpacecraftState, dt_s: float) -> list[DebrisObject]:
    updated = []
    for debris in state.tracked_debris:
        new_distance = max(0.0, debris.relative_distance_km - debris.closing_velocity_kms * dt_s)
        new_tca = max(0.0, debris.time_to_closest_approach_s - dt_s)
        updated.append(DebrisObject(
            object_id=debris.object_id,
            relative_distance_km=new_distance,
            closing_velocity_kms=debris.closing_velocity_kms,
            time_to_closest_approach_s=new_tca,
            collision_probability=debris.collision_probability
        ))
    return updated


def _update_downlink_buffer(state: SpacecraftState, dt_s: float, cmd: SpacecraftCommand | None = None) -> float:
    new_buffer = state.downlink_buffer_mb
    if cmd and cmd.cmd_type == CommandType.DOWNLINK_DATA:
        rate = cmd.params.get("rate_mb_per_s", DOWNLINK_RATE_MB_PER_S)
        new_buffer = max(0.0, state.downlink_buffer_mb - rate * dt_s)
    return new_buffer


def _update_propellant(state: SpacecraftState, cmd: SpacecraftCommand | None = None) -> float:
    new_propellant = state.propellant_kg
    if cmd and cmd.cmd_type in (CommandType.FIRE_THRUSTER, CommandType.ORBIT_AVOIDANCE_BURN):
        dv = cmd.params.get("delta_v_m_s", AVOIDANCE_BURN_DV_M_S)
        prop_used = dv / THRUSTER_DV_PER_KG
        new_propellant = max(0.0, state.propellant_kg - prop_used)
    return new_propellant


def _predict_sun_angle(state: SpacecraftState, cmd: SpacecraftCommand | None = None) -> float:
    if cmd and cmd.cmd_type == CommandType.SLEW_TO_TARGET:
        return cmd.params.get("target_sun_angle_deg", state.sun_angle_deg)
    return state.sun_angle_deg


def _predict_pointing_mode(state: SpacecraftState, cmd: SpacecraftCommand | None = None) -> PointingMode:
    if cmd:
        if cmd.cmd_type == CommandType.SLEW_TO_TARGET:
            return PointingMode.TARGET
        elif cmd.cmd_type == CommandType.ENTER_SAFE_MODE:
            return PointingMode.SAFE
    return state.pointing_mode


def step_simulation(state: SpacecraftState, dt_s: float, cmd: SpacecraftCommand | None = None) -> SpacecraftState:
    new_phase = _update_orbital_phase(state, dt_s)
    in_eclipse = _update_eclipse(state, new_phase)
    new_battery = _update_battery(state, dt_s, in_eclipse, cmd)
    new_temp = _update_thermal(state, dt_s, in_eclipse)
    new_debris = _update_debris(state, dt_s)
    new_buffer = _update_downlink_buffer(state, dt_s, cmd)
    new_propellant = _update_propellant(state, cmd)
    new_sun_angle = _predict_sun_angle(state, cmd)
    new_pointing = _predict_pointing_mode(state, cmd)

    return SpacecraftState(
        timestamp_s=state.timestamp_s + dt_s,
        altitude_km=state.altitude_km,
        orbital_phase_deg=new_phase,
        in_eclipse=in_eclipse,
        battery_soc=new_battery,
        bus_temp_c=new_temp,
        sun_angle_deg=new_sun_angle,
        pointing_mode=new_pointing,
        downlink_buffer_mb=new_buffer,
        propellant_kg=new_propellant,
        tracked_debris=new_debris
    )


def predict_state_after_command(state: SpacecraftState, cmd: SpacecraftCommand) -> SpacecraftState:
    duration_map = {
        CommandType.SLEW_TO_TARGET: SLEW_DURATION_S,
        CommandType.FIRE_THRUSTER: 5.0,
        CommandType.ORBIT_AVOIDANCE_BURN: 10.0,
        CommandType.CAPTURE_PAYLOAD: PAYLOAD_DURATION_S,
        CommandType.DOWNLINK_DATA: state.downlink_buffer_mb / DOWNLINK_RATE_MB_PER_S if state.downlink_buffer_mb > 0 else 0.0,
        CommandType.ENTER_SAFE_MODE: 5.0,
    }
    duration = duration_map.get(cmd.cmd_type, 1.0)
    return step_simulation(state, duration, cmd)