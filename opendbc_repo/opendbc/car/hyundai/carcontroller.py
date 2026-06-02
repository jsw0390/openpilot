import numpy as np
from opendbc.can import CANPacker
from opendbc.car import Bus, DT_CTRL, apply_driver_steer_torque_limits, common_fault_avoidance, make_tester_present_msg, structs, apply_std_steer_angle_limits
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.hyundai import hyundaicanfd, hyundaican
from opendbc.car.hyundai.carstate import CarState
from opendbc.car.hyundai.hyundaicanfd import CanBus
from opendbc.car.hyundai.values import HyundaiFlags, Buttons, CarControllerParams, CAR, CAN_GEARS, HyundaiExtFlags
from opendbc.car.interfaces import CarControllerBase
from opendbc.car.vehicle_model import VehicleModel

VisualAlert = structs.CarControl.HUDControl.VisualAlert
LongCtrlState = structs.CarControl.Actuators.LongControlState


from openpilot.common.params import Params

# EPS faults if you apply torque while the steering angle is above 90 degrees for more than 1 second
# All slightly below EPS thresholds to avoid fault
MAX_ANGLE = 85
MAX_ANGLE_FRAMES = 89
MAX_ANGLE_CONSECUTIVE_FRAMES = 2
# On Ray EV, button value 4 is the physical pause/resume button.
RAY_EV_ACTIVATE_BUTTON = Buttons.CANCEL
RAY_EV_DRIVER_PAUSE_RESUME_FRAMES = 50
RAY_EV_SPEED_SYNC_BLOCK_FRAMES = 80
RAY_EV_STATE_SYNC_WAIT_FRAMES = 40

vibrate_intervals = [
  (0.0, 0.5),
  (1.0, 1.5),
  #(2.5, 3.0),
  #(3.5, 4.0),
  (5.0, 5.5),
  (6.0, 6.5),
  (7.5, 8.0),
]

def process_hud_alert(enabled, fingerprint, hud_control):
  sys_warning = (hud_control.visualAlert in (VisualAlert.steerRequired, VisualAlert.ldw))

  # initialize to no line visible
  # TODO: this is not accurate for all cars
  sys_state = 1
  if hud_control.leftLaneVisible and hud_control.rightLaneVisible or sys_warning:  # HUD alert only display when LKAS status is active
    sys_state = 3 if enabled or sys_warning else 4
  elif hud_control.leftLaneVisible:
    sys_state = 5
  elif hud_control.rightLaneVisible:
    sys_state = 6

  # initialize to no warnings
  left_lane_warning = 0
  right_lane_warning = 0
  if hud_control.leftLaneDepart:
    left_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2
  if hud_control.rightLaneDepart:
    right_lane_warning = 1 if fingerprint in (CAR.GENESIS_G90, CAR.GENESIS_G80) else 2

  return sys_warning, sys_state, left_lane_warning, right_lane_warning

def rate_limit(x, x_last, lo, hi):
  return float(np.clip(x, x_last + lo, x_last + hi))

def apply_steer_angle_limits_physics(desired_sw_deg: float,
                                     last_sw_deg: float,
                                     v_ego: float,
                                     steering_sw_deg: float,
                                     lat_active: bool,
                                     wheelbase_m: float,
                                     steer_ratio: float,
                                     steer_sw_max_deg: float) -> float:
  max_lat_accel = 5.0   # m/s^2
  max_lat_jerk  = 4.0   # m/s^3
  max_sw_rate_deg_per_tick = 2.0   # ★ EPS 보호용 상한

  v = max(float(v_ego), 1.0)

  target_sw = float(np.clip(desired_sw_deg, -steer_sw_max_deg, steer_sw_max_deg))

  target_rw = target_sw / steer_ratio
  last_rw   = float(last_sw_deg) / steer_ratio

  # --- accel limit ---
  rw_max_rad = np.arctan((max_lat_accel * wheelbase_m) / (v * v))
  rw_max = float(np.degrees(rw_max_rad))

  # --- jerk -> rate limit ---
  sec2 = 1.2
  max_drw_dt = (max_lat_jerk * wheelbase_m) / (v * v * sec2)     # rad/s
  max_drw_per_tick = max_drw_dt * DT_CTRL                        # rad/tick
  max_drw_per_tick_deg = float(np.degrees(max_drw_per_tick))

  max_drw_per_tick_deg = min(
    max_drw_per_tick_deg,
    max_sw_rate_deg_per_tick / steer_ratio
  )
  err = abs(target_sw - last_sw_deg)
  if err > 20.0:
    max_drw_per_tick_deg *= 0.5
  
  # --- rate limit ---
  cmd_rw = rate_limit(target_rw, last_rw, -max_drw_per_tick_deg, max_drw_per_tick_deg)

  # --- accel clip ---
  cmd_rw = float(np.clip(cmd_rw, -rw_max, rw_max))

  if not lat_active:
    cmd_rw = float(steering_sw_deg) / steer_ratio

  cmd_sw = cmd_rw * steer_ratio
  return float(np.clip(cmd_sw, -steer_sw_max_deg, steer_sw_max_deg))
  
class CarController(CarControllerBase):
  def __init__(self, dbc_names, CP):
    super().__init__(dbc_names, CP)
    self.CAN = CanBus(CP)
    self.params = CarControllerParams(CP)
    self.packer = CANPacker(dbc_names[Bus.pt])
    self.angle_limit_counter = 0

    self.accel_last = 0
    self.apply_torque_last = 0
    self.car_fingerprint = CP.carFingerprint
    self.last_button_frame = 0

    self.hyundai_jerk = HyundaiJerk()
    self.speedCameraHapticEndFrame = 0
    self.hapticFeedbackWhenSpeedCamera = 0
    self.max_angle_frames = MAX_ANGLE_FRAMES
    self.blinking_signal = False # 1Hz
    self.blinking_frame = int(1.0 / DT_CTRL)
    self.soft_hold_mode = 2

    self.activateCruise = 0
    self.button_wait = 12
    self.cruise_buttons_msg_values = None
    self.cruise_buttons_msg_cnt = 0
    self.button_spamming_count = 0
    self.prev_clu_speed = 0
    self.button_spam1 = 8
    self.button_spam2 = 30
    self.button_spam3 = 1
    self.ray_ev_cruise_enabled_last = False
    self.ray_ev_estimated_cruise_speed = 0
    self.ray_ev_activate_retry = 0
    self.ray_ev_prev_cruise_button = Buttons.NONE
    self.ray_ev_pause_resume_frame = -(RAY_EV_DRIVER_PAUSE_RESUME_FRAMES + 1)
    self.ray_ev_speed_sync_block_frame = -(RAY_EV_SPEED_SYNC_BLOCK_FRAMES + 1)
    self.ray_ev_speed_bias_active = False

    self.apply_angle_last = 0
    self.lkas_max_torque = 0
    self.angle_max_torque = 250
    self.prev_abs_angle_error = 0.0
    self.recover_level = 1.0

    self.lkas11_active = False

    self.canfd_debug = 0
    self.MainMode_ACC_trigger = 0
    self.LFA_trigger = 0

    self.activeCarrot = 0
    self.camera_scc_params = Params().get_int("HyundaiCameraSCC")
    self.is_ldws_car = Params().get_bool("IsLdwsCar")
    self.enable_corner_radar = 0

    self.steerDeltaUpOrg = self.steerDeltaUp = self.steerDeltaUpLC = self.params.STEER_DELTA_UP
    self.steerDeltaDownOrg = self.steerDeltaDown = self.steerDeltaDownLC = self.params.STEER_DELTA_DOWN

  def update(self, CC, CS, now_nanos):

    if self.frame % 50 == 0:
      params = Params()
      self.max_angle_frames = params.get_int("MaxAngleFrames")
      steerMax = params.get_int("CustomSteerMax")
      steerDeltaUp = params.get_int("CustomSteerDeltaUp")
      steerDeltaDown = params.get_int("CustomSteerDeltaDown")
      steerDeltaUpLC = params.get_int("CustomSteerDeltaUpLC")
      steerDeltaDownLC = params.get_int("CustomSteerDeltaDownLC")
      if steerMax > 0:
        self.params.STEER_MAX = steerMax
      if steerDeltaUp > 0:
        self.steerDeltaUp = steerDeltaUp
        #self.params.ANGLE_TORQUE_UP_RATE = steerDeltaUp
      else:
        self.steerDeltaUp = self.steerDeltaUpOrg
      if steerDeltaDown > 0:
        self.steerDeltaDown = steerDeltaDown
        #self.params.ANGLE_TORQUE_DOWN_RATE = steerDeltaDown
      else:
        self.steerDeltaDown = self.steerDeltaDownOrg

      if steerDeltaUpLC > 0:
        self.steerDeltaUpLC = steerDeltaUpLC
      else:
        self.steerDeltaUpLC = self.steerDeltaUp
      if steerDeltaDownLC > 0:
        self.steerDeltaDownLC = steerDeltaDownLC
      else:
        self.steerDeltaDownLC = self.steerDeltaDown
        
      self.soft_hold_mode = 1 if params.get_int("AutoCruiseControl") > 1 else 2
      self.hapticFeedbackWhenSpeedCamera = int(params.get_int("HapticFeedbackWhenSpeedCamera"))

      self.button_spam1 = params.get_int("CruiseButtonTest1")
      self.button_spam2 = params.get_int("CruiseButtonTest2")
      self.button_spam3 = params.get_int("CruiseButtonTest3")
      self.speed_from_pcm = params.get_int("SpeedFromPCM")

      self.canfd_debug = params.get_int("CanfdDebug")
      self.camera_scc_params = params.get_int("HyundaiCameraSCC")
      self.enable_corner_radar = params.get_int("EnableCornerRadar")

    actuators = CC.actuators
    hud_control = CC.hudControl

    if hud_control.modelDesire in [3,4]:
      self.params.STEER_DELTA_UP = self.steerDeltaUpLC
      self.params.STEER_DELTA_DOWN = self.steerDeltaDownLC
    else:
      self.params.STEER_DELTA_UP = self.steerDeltaUp
      self.params.STEER_DELTA_DOWN = self.steerDeltaDown
    
    angle_control = self.CP.flags & HyundaiFlags.ANGLE_CONTROL

    # steering torque
    new_torque = int(round(actuators.torque * self.params.STEER_MAX))
    apply_torque = apply_driver_steer_torque_limits(new_torque, self.apply_torque_last, CS.out.steeringTorque, self.params)

    # >90 degree steering fault prevention
    self.angle_limit_counter, apply_steer_req = common_fault_avoidance(abs(CS.out.steeringAngleDeg) >= MAX_ANGLE, CC.latActive,
                                                                       self.angle_limit_counter, self.max_angle_frames,
                                                                       MAX_ANGLE_CONSECUTIVE_FRAMES)

    #apply_angle = apply_std_steer_angle_limits(actuators.steeringAngleDeg, self.apply_angle_last, CS.out.vEgoRaw, 
    #                                           CS.out.steeringAngleDeg, CC.latActive, self.params.ANGLE_LIMITS)

    apply_angle = apply_steer_angle_limits_physics(
      actuators.steeringAngleDeg,
      self.apply_angle_last,
      CS.out.vEgoRaw,
      CS.out.steeringAngleDeg,
      CC.latActive,
      self.CP.wheelbase,
      self.CP.steerRatio,
      self.params.ANGLE_LIMITS.STEER_ANGLE_MAX
    )

    
    if angle_control:
      apply_steer_req = CC.latActive

    def _clip(x, lo, hi):
      return min(max(x, lo), hi)

    def _scale01(x, lo, hi):
      return _clip((x - lo) / (hi - lo), 0.0, 1.0)

    angle_error = apply_angle - CS.out.steeringAngleDeg
    abs_angle_error = abs(angle_error)

    error_delta = self.prev_abs_angle_error - abs_angle_error

    if CS.out.steeringPressed:
      # Driver touched the wheel, gradually yield.
      self.lkas_max_torque = max(self.lkas_max_torque - 20, 25)
      self.recover_level = 0.0

    else:
      target_torque = self.angle_max_torque

      max_steering_tq = self.params.STEER_DRIVER_ALLOWANCE * 0.7
      rate_ratio = max(20, max_steering_tq - abs(CS.out.steeringTorque)) / max_steering_tq
      rate_up = self.params.ANGLE_TORQUE_UP_RATE * rate_ratio
      rate_down = self.params.ANGLE_TORQUE_DOWN_RATE * rate_ratio

      recover_level = self.recover_level

      # error_delta > 0 means actual steering angle and apply_angle are getting closer.
      recover_factor = 0.0
      if error_delta > 0.02:
        recover_factor = _scale01(error_delta, 0.02, 0.30)

      # Normal recovery is slow.
      # If angle error is decreasing, recover faster.
      recover_rate = 0.005 + recover_factor * 0.035
      recover_level = _clip(recover_level + recover_rate, 0.0, 1.0)
      self.recover_level = recover_level

      # While recovering, limit available torque.
      # recover_level = 0.0 -> 30%
      # recover_level = 1.0 -> 100%
      target_torque *= 0.3 + recover_level * 0.7

      # If angle error is already converging, allow torque to come back a little faster.
      rate_up *= 1.0 + recover_factor * 0.5

      if self.lkas_max_torque > target_torque:
        self.lkas_max_torque = max(self.lkas_max_torque - rate_down, target_torque)
      else:
        self.lkas_max_torque = min(self.lkas_max_torque + rate_up, target_torque)

    self.prev_abs_angle_error = abs_angle_error

    if not CC.latActive:
      apply_torque = 0
      self.lkas_max_torque = 0

    self.apply_angle_last = apply_angle

    # Hold torque with induced temporary fault when cutting the actuation bit
    torque_fault = CC.latActive and not apply_steer_req

    self.apply_torque_last = apply_torque

    # accel + longitudinal
    accel = float(np.clip(actuators.accel, CarControllerParams.ACCEL_MIN, CarControllerParams.ACCEL_MAX))
    stopping = actuators.longControlState == LongCtrlState.stopping
    set_speed_in_units = hud_control.setSpeed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH)

    # HUD messages
    sys_warning, sys_state, left_lane_warning, right_lane_warning = process_hud_alert(CC.enabled, self.car_fingerprint,
                                                                                      hud_control)

    active_speed_decel = hud_control.activeCarrot == 3 and self.activeCarrot != 3 # 3: Speed Decel
    self.activeCarrot = hud_control.activeCarrot
    if active_speed_decel and self.speedCameraHapticEndFrame < 0: # 과속카메라 감속시작      
      self.speedCameraHapticEndFrame = self.frame + (8.0 / DT_CTRL)  #8초간 켜줌.
    elif not active_speed_decel:
      self.speedCameraHapticEndFrame = -1

    if 0 <= self.speedCameraHapticEndFrame - self.frame < int(8.0 / DT_CTRL) and self.hapticFeedbackWhenSpeedCamera > 0:
      t = (self.frame - (self.speedCameraHapticEndFrame - int(8.0 / DT_CTRL))) * DT_CTRL

      for start, end in vibrate_intervals:
        if start <= t < end:
          left_lane_warning = right_lane_warning = self.hapticFeedbackWhenSpeedCamera
          break

    if self.frame >= self.speedCameraHapticEndFrame:
      self.speedCameraHapticEndFrame = -1

    if self.frame % self.blinking_frame == 0:
      self.blinking_signal = True
    elif self.frame % self.blinking_frame == self.blinking_frame / 2:
      self.blinking_signal = False



    can_sends = []

    # *** common hyundai stuff ***

    # tester present - w/ no response (keeps relevant ECU disabled)
    if self.frame % 100 == 0 and not (self.CP.flags & HyundaiFlags.CANFD_CAMERA_SCC) and self.CP.openpilotLongitudinalControl:
      # for longitudinal control, either radar or ADAS driving ECU
      addr, bus = 0x7d0, self.CAN.ECAN if self.CP.flags & HyundaiFlags.CANFD else 0
      if self.CP.flags & HyundaiFlags.CANFD_HDA2.value:
        addr, bus = 0x730, self.CAN.ECAN
      can_sends.append(make_tester_present_msg(addr, bus, suppress_response=True))

      # for blinkers
      if self.CP.flags & HyundaiFlags.ENABLE_BLINKERS:
        can_sends.append(make_tester_present_msg(0x7b1, self.CAN.ECAN, suppress_response=True))

    camera_scc = self.CP.flags & HyundaiFlags.CAMERA_SCC
    # CAN-FD platforms
    if self.CP.flags & HyundaiFlags.CANFD:
      hda2 = self.CP.flags & HyundaiFlags.CANFD_HDA2
      hda2_long = hda2 and self.CP.openpilotLongitudinalControl
      # steering control
      if camera_scc:
        can_sends.extend(hyundaicanfd.create_steering_messages_camera_scc(self.frame, self.packer, self.CP, self.CAN, CC, apply_steer_req, apply_torque, CS, apply_angle, self.lkas_max_torque, angle_control))
      else:
        can_sends.extend(hyundaicanfd.create_steering_messages(self.packer, self.CP, self.CAN, CC.enabled, apply_steer_req, apply_torque, apply_angle, self.lkas_max_torque, angle_control))
              
      # prevent LFA from activating on HDA2 by sending "no lane lines detected" to ADAS ECU
      if self.frame % 5 == 0 and hda2 and not camera_scc:
        can_sends.extend(hyundaicanfd.create_suppress_lfa(self.packer, self.CAN, CS))

      # LFA and HDA icons
      if self.frame % 5 == 0 and (not hda2 or hda2_long or camera_scc):
        can_sends.extend(hyundaicanfd.create_lfahda_cluster(self.packer, CS, self.CAN, CC.longActive, CC.latActive))
        if not camera_scc:
          can_sends.extend(hyundaicanfd.create_lfa_icon_non_camera_scc(self.packer, CS, self.CAN, CC))

      # blinkers
      if hda2 and self.CP.flags & HyundaiFlags.ENABLE_BLINKERS:
        can_sends.extend(hyundaicanfd.create_spas_messages(self.packer, self.CAN, self.frame, CC.leftBlinker, CC.rightBlinker))

      if self.camera_scc_params in [2, 3]:
        self.canfd_toggle_adas(CC, CS)
      if self.CP.openpilotLongitudinalControl:
        self.hyundai_jerk.make_jerk(self.CP, CS, accel, actuators, hud_control)
        self.hyundai_jerk.check_carrot_cruise(CC, CS, hud_control, stopping, accel, actuators.aTarget)

        if True: #not camera_scc:
          can_sends.extend(hyundaicanfd.create_ccnc_messages(self.CP, self.packer, self.CAN, self.frame, CC, CS, hud_control, apply_angle, left_lane_warning, right_lane_warning, self.enable_corner_radar, stopping, self.canfd_debug))
          if hda2:
            can_sends.extend(hyundaicanfd.create_adrv_messages(self.CP, self.packer, self.CAN, self.frame))
          else:
            can_sends.extend(hyundaicanfd.create_fca_warning_light(self.CP, self.packer, self.CAN, self.frame))
        if self.frame % 2 == 0:
          if self.CP.flags & HyundaiFlags.CAMERA_SCC.value:
            msg = hyundaicanfd.create_acc_control_scc2(self.packer, self.CAN, CC.enabled, self.accel_last, accel, stopping, CC.cruiseControl.override,
                                                             set_speed_in_units, hud_control, self.hyundai_jerk, CS)
            if msg is not None:
              can_sends.append(msg)
            can_sends.extend(hyundaicanfd.create_tcs_messages(self.packer, self.CAN, CS)) # for sorento SCC radar...
          else:
            can_sends.append(hyundaicanfd.create_acc_control(self.packer, self.CAN, CC.enabled, self.accel_last, accel, stopping, CC.cruiseControl.override,
                                                             set_speed_in_units, hud_control, self.hyundai_jerk.jerk_u, self.hyundai_jerk.jerk_l, CS))
          self.accel_last = accel
      else:
        # button presses
        if self.camera_scc_params == 3: # camera scc but stock long
          send_button = self.make_spam_button(CC, CS)
          can_sends.extend(hyundaicanfd.forward_button_message(self.packer, self.CAN, self.frame, CS, send_button, self.MainMode_ACC_trigger, self.LFA_trigger))
        else:
          can_sends.extend(self.create_button_messages(CC, CS, use_clu11=False))
    else:
      if CS.lkas11 is not None:
        if self.lkas11_active:
          can_sends.append(hyundaican.create_lkas11(self.packer, self.frame, self.CP, apply_torque, apply_steer_req,
                                                    torque_fault, CS.lkas11, sys_warning, sys_state, CC.enabled,
                                                    hud_control.leftLaneVisible, hud_control.rightLaneVisible,
                                                    left_lane_warning, right_lane_warning, self.is_ldws_car))
        self.lkas11_active = True

      if not self.CP.openpilotLongitudinalControl or self.CP.carFingerprint == CAR.KIA_RAY_EV:
        can_sends.extend(self.create_button_messages(CC, CS, use_clu11=True))
      if self.CP.carFingerprint in CAN_GEARS["send_mdps12"] and CS.mdps12 is not None:  # send mdps12 to LKAS to prevent LKAS error
        can_sends.append(hyundaican.create_mdps12(self.packer, self.frame, CS.mdps12))

      casper_opt = self.CP.carFingerprint in (CAR.HYUNDAI_CASPER_EV)
      if self.frame % 2 == 0 and self.CP.openpilotLongitudinalControl:
        self.hyundai_jerk.make_jerk(self.CP, CS, accel, actuators, hud_control)
        self.hyundai_jerk.check_carrot_cruise(CC, CS, hud_control, stopping, accel, actuators.aTarget)
        #jerk = 3.0 if actuators.longControlState == LongCtrlState.pid else 1.0
        use_fca = self.CP.flags & HyundaiFlags.USE_FCA.value
        if camera_scc:
          
          can_sends.extend(hyundaican.create_acc_commands_scc(self.packer, CC.enabled, accel, self.hyundai_jerk, int(self.frame / 2),
                                                          hud_control, set_speed_in_units, stopping,
                                                          CC.cruiseControl.override, casper_opt, CS, self.soft_hold_mode))
        else:
          can_sends.extend(hyundaican.create_acc_commands(self.packer, CC.enabled, accel, self.hyundai_jerk, int(self.frame / 2),
                                                hud_control, set_speed_in_units, stopping,
                                                CC.cruiseControl.override, use_fca, self.CP, CS, self.soft_hold_mode))


      # 20 Hz LFA MFA message
      if self.frame % 5 == 0 and self.CP.flags & HyundaiFlags.SEND_LFA.value:
        can_sends.append(hyundaican.create_lfahda_mfc(self.packer, CC, self.blinking_signal))

      # 5 Hz ACC options
      if self.frame % 20 == 0 and self.CP.openpilotLongitudinalControl:
        if camera_scc:
          if CS.scc13 is not None:
            if casper_opt:
              #can_sends.append(hyundaican.create_acc_opt_copy(CS, self.packer))
              pass
          pass
        else:
          can_sends.extend(hyundaican.create_acc_opt(self.packer, self.CP))

      # 2 Hz front radar options
      if self.frame % 50 == 0 and self.CP.openpilotLongitudinalControl and not camera_scc:
        can_sends.append(hyundaican.create_frt_radar_opt(self.packer))

    new_actuators = actuators.as_builder()
    new_actuators.torque = apply_torque / self.params.STEER_MAX
    new_actuators.torqueOutputCan = apply_torque
    new_actuators.steeringAngleDeg = float(apply_angle)
    new_actuators.accel = accel

    self.frame += 1
    return new_actuators, can_sends


  def create_button_messages(self, CC: structs.CarControl, CS: CarState, use_clu11: bool):
    can_sends = []
    if CS.out.brakePressed or CS.out.brakeHoldActive:
      return can_sends
    ray_ev_op_long = self.CP.carFingerprint == CAR.KIA_RAY_EV and self.CP.openpilotLongitudinalControl
    if ray_ev_op_long:
      cancel_request = CS.out.activateCruise < 0
    else:
      cancel_request = CC.cruiseControl.cancel or CS.out.activateCruise < 0

    if use_clu11:
      if cancel_request:
        can_sends.append(hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.CANCEL, self.CP))
      elif False: #CC.cruiseControl.resume:
        # send resume at a max freq of 10Hz
        if (self.frame - self.last_button_frame) * DT_CTRL > 0.1:
          # send 25 messages at a time to increases the likelihood of resume being accepted
          can_sends.extend([hyundaican.create_clu11(self.packer, self.frame, CS.clu11, Buttons.RES_ACCEL, self.CP)] * 25)
          if (self.frame - self.last_button_frame) * DT_CTRL >= 0.15:
            self.last_button_frame = self.frame

      if self.last_button_frame != self.frame:
        send_button = self.make_spam_button(CC, CS)
        if send_button > 0:
          can_sends.append(hyundaican.create_clu11_button(self.packer, self.frame, CS.clu11, send_button, self.CP))

    else:

      # carrot.. 왜 alt_cruise_button는 값이 리스트일까?, 그리고 왜? 빈데이터가 들어오는것일까?
      if CS.cruise_buttons_msg is not None and self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
        try:
          cruise_buttons_msg_values = {key: value[0] for key, value in CS.cruise_buttons_msg.items()}
        except: # IndexError:
          #print("IndexError....")
          cruise_buttons_msg_values = None
          self.cruise_buttons_msg_cnt += 1
        if cruise_buttons_msg_values is not None:
          self.cruise_buttons_msg_values = cruise_buttons_msg_values
          self.cruise_buttons_msg_cnt = 0

      if (self.frame - self.last_button_frame) * DT_CTRL > 0.25:
        # cruise cancel
        if cancel_request:
          if (self.frame - self.last_button_frame) * DT_CTRL > 0.1:
            print("cruiseControl.cancel222222")
            if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
              #can_sends.append(hyundaicanfd.create_acc_cancel(self.packer, self.CP, self.CAN, CS.scc_control))
              if self.cruise_buttons_msg_values is not None:
                can_sends.append(hyundaicanfd.alt_cruise_buttons(self.packer, self.CP, self.CAN, Buttons.CANCEL, self.cruise_buttons_msg_values, self.cruise_buttons_msg_cnt))

            else:
              for _ in range(20):
                can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.CANCEL))
            self.last_button_frame = self.frame

        # cruise standstill resume
        elif False: #CC.cruiseControl.resume:
          if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS:
            # TODO: resume for alt button cars
            pass
          else:
            for _ in range(20):
              can_sends.append(hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, Buttons.RES_ACCEL))
            self.last_button_frame = self.frame

      ## button 스패밍을 안했을때...
      if self.last_button_frame != self.frame:
        dat = self.canfd_speed_control_pcm(CC, CS, self.cruise_buttons_msg_values)
        if dat is not None:
          for _ in range(self.button_spam3):
            can_sends.append(dat)
          self.cruise_buttons_msg_cnt += 1

    return can_sends

  def canfd_toggle_adas(self, CC, CS):
    trigger_min = -200
    trigger_start = 6
    self.MainMode_ACC_trigger = max(trigger_min, self.MainMode_ACC_trigger - 1)
    self.LFA_trigger = max(trigger_min, self.LFA_trigger - 1)
    if self.MainMode_ACC_trigger == trigger_min and self.LFA_trigger == trigger_min:
      if CC.enabled and not CS.MainMode_ACC and CS.out.vEgo > 3.:
        self.MainMode_ACC_trigger = trigger_start
      elif CC.latActive and CS.LFA_ICON == 0:
        self.LFA_trigger = trigger_start

  def canfd_speed_control_pcm(self, CC, CS, cruise_buttons_msg_values):

    alt_buttons = True if self.CP.flags & HyundaiFlags.CANFD_ALT_BUTTONS else False

    if alt_buttons and cruise_buttons_msg_values is None:
      return None

    send_button = self.make_spam_button(CC, CS)
    if send_button > 0:
      if alt_buttons:
        return hyundaicanfd.alt_cruise_buttons(self.packer, self.CP, self.CAN, send_button, cruise_buttons_msg_values, self.cruise_buttons_msg_cnt)
      else:
        return hyundaicanfd.create_buttons(self.packer, self.CP, self.CAN, CS.buttons_counter+1, send_button)

    return None

  def _ray_ev_cruise_state_from_gear(self, CS):
    try:
      gear_step = int(CS.out.gearStep)
    except Exception:
      return None

    # Ray EV ELECT_GEAR step doubles as a stock cluster display state:
    # 7 means cruise set, while 0~4 are D/regen/i-Pedal after cruise release.
    if gear_step == 7:
      return True
    if 0 <= gear_step <= 4:
      return False
    return None

  def _ray_ev_speed_tracking_target(self, target, current, v_ego_kph, hud_control):
    if target <= 0:
      self.ray_ev_speed_bias_active = False
      return target

    speed_error = v_ego_kph - target

    # Once actual speed reaches the comma target, stop using the temporary low
    # target. The normal button logic below will then sync the vehicle cruise
    # setpoint back to the comma target if our Ray EV estimate is still off.
    if speed_error <= 2.0:
      self.ray_ev_speed_bias_active = False
      return target
    if not self.ray_ev_speed_bias_active and speed_error <= 3.0:
      return target

    # If the stock cruise setpoint is still above the comma target, let the
    # normal SET button matching bring it down first. When that setpoint reaches
    # target but the car is still too fast, temporarily bias lower to encourage
    # smooth deceleration, then restore to target at the settle point above.
    if current > target + 1:
      self.ray_ev_speed_bias_active = True
      return target

    lead_close = (
      hud_control.leadVisible and hud_control.leadDistance > 0.0 and
      (
        hud_control.leadRelSpeed < -0.5 or
        hud_control.leadDistance < max(18.0, v_ego_kph / 3.6 * 1.2)
      )
    )
    bias_max = 8 if lead_close else 5
    decel_bias = int(np.clip(round(speed_error * 0.5), 1, bias_max))
    self.ray_ev_speed_bias_active = True
    return max(30, target - decel_bias)

  def make_spam_button(self, CC, CS):
    hud_control = CC.hudControl
    set_speed_in_units = hud_control.setSpeed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH)
    target = int(set_speed_in_units+0.5)
    button_target = target
    current = int(CS.out.cruiseState.speed * (CV.MS_TO_KPH if CS.is_metric else CV.MS_TO_MPH) + 0.5)
    v_ego_kph = CS.out.vEgo * CV.MS_TO_KPH
    ray_ev_activation_target = target if 0 < target <= 160 else v_ego_kph
    ray_ev_activation_estimate = min(160, max(30, int(ray_ev_activation_target + 0.5)))
    is_ray_ev = self.CP.carFingerprint == CAR.KIA_RAY_EV
    ray_ev_cruise_state = self._ray_ev_cruise_state_from_gear(CS) if is_ray_ev else None
    physical_button = CS.cruise_buttons[-1] if len(CS.cruise_buttons) else Buttons.NONE
    physical_button_edge = physical_button != self.ray_ev_prev_cruise_button and physical_button != Buttons.NONE
    self.ray_ev_prev_cruise_button = physical_button
    if is_ray_ev and physical_button_edge and physical_button == Buttons.CANCEL:
      self.ray_ev_pause_resume_frame = self.frame
    if is_ray_ev and physical_button_edge and physical_button in (Buttons.RES_ACCEL, Buttons.SET_DECEL):
      self.ray_ev_speed_sync_block_frame = self.frame
    if is_ray_ev and not CC.enabled:
      self.ray_ev_activate_retry = 0
      if ray_ev_cruise_state is not True:
        self.ray_ev_cruise_enabled_last = False
    if is_ray_ev and CC.enabled and physical_button_edge and physical_button in (Buttons.RES_ACCEL, Buttons.SET_DECEL):
      if self.ray_ev_estimated_cruise_speed <= 0:
        self.ray_ev_estimated_cruise_speed = min(160, max(30, int((current if current > 0 else v_ego_kph) + 0.5)))
      if physical_button == Buttons.RES_ACCEL:
        self.ray_ev_estimated_cruise_speed = min(160, self.ray_ev_estimated_cruise_speed + 1)
      elif physical_button == Buttons.SET_DECEL:
        self.ray_ev_estimated_cruise_speed = max(30, self.ray_ev_estimated_cruise_speed - 1)
      self.ray_ev_cruise_enabled_last = True
    if is_ray_ev:
      ray_ev_cruise_active = (
        CS.out.cruiseState.enabled or
        ray_ev_cruise_state is True or
        (ray_ev_cruise_state is None and self.ray_ev_cruise_enabled_last and CC.enabled and self.ray_ev_activate_retry <= 0 and v_ego_kph > 10.0)
      )
    else:
      ray_ev_cruise_active = CS.out.cruiseState.enabled
    ray_ev_using_estimate = False

    if is_ray_ev:
      if not ray_ev_cruise_active:
        self.ray_ev_cruise_enabled_last = False
        self.ray_ev_estimated_cruise_speed = 0
        self.ray_ev_speed_bias_active = False
      elif not self.ray_ev_cruise_enabled_last or self.ray_ev_estimated_cruise_speed <= 0:
        self.ray_ev_estimated_cruise_speed = min(160, max(30, int(v_ego_kph + 0.5)))

      if ray_ev_cruise_active and current <= 0:
        ray_ev_using_estimate = True
        if self.ray_ev_estimated_cruise_speed <= 0:
          self.ray_ev_estimated_cruise_speed = min(160, max(30, int(v_ego_kph + 0.5)))
        current = self.ray_ev_estimated_cruise_speed

      self.ray_ev_cruise_enabled_last = ray_ev_cruise_active
      self.activateCruise = 0
      if ray_ev_cruise_active:
        button_target = self._ray_ev_speed_tracking_target(target, current, v_ego_kph, hud_control)

    send_button = 0
    activate_cruise = False
    resume_button = Buttons.RES_ACCEL
    ray_ev_activation_requested = is_ray_ev and CS.out.activateCruise == 2
    ray_ev_activation_allowed = v_ego_kph > 10.0 or (v_ego_kph <= 0.5 and hud_control.leadVisible)
    ray_ev_recent_driver_pause_resume = (
      is_ray_ev and
      (self.frame - self.ray_ev_pause_resume_frame) <= RAY_EV_DRIVER_PAUSE_RESUME_FRAMES
    )
    ray_ev_speed_sync_blocked = (
      is_ray_ev and
      (self.frame - self.ray_ev_speed_sync_block_frame) <= RAY_EV_SPEED_SYNC_BLOCK_FRAMES
    )
    ray_ev_driver_pause_resume = (
      ray_ev_activation_requested and
      ray_ev_recent_driver_pause_resume
    )
    ray_ev_state_sync = False

    if ray_ev_driver_pause_resume:
      self.ray_ev_activate_retry = 0
      self.ray_ev_cruise_enabled_last = True
      self.ray_ev_estimated_cruise_speed = ray_ev_activation_estimate
      self.activateCruise = 1
      self.button_spamming_count = 0
      self.prev_clu_speed = current
      return 0

    if CC.enabled:
      if ray_ev_activation_requested and ray_ev_activation_allowed:
        self.ray_ev_activate_retry = max(self.ray_ev_activate_retry, 12)
        send_button = RAY_EV_ACTIVATE_BUTTON
        activate_cruise = True
        self.activateCruise = 1
      elif is_ray_ev and self.ray_ev_activate_retry > 0 and ray_ev_activation_allowed and not ray_ev_cruise_active:
        send_button = RAY_EV_ACTIVATE_BUTTON
        activate_cruise = True
        self.activateCruise = 1
        self.ray_ev_activate_retry -= 1
      # If the cluster has returned to a regen/i-Pedal step while comma is enabled,
      # the stock cruise is paused; press the pause/resume button to realign it.
      elif is_ray_ev and ray_ev_cruise_state is False and CS.out.activateCruise >= 0 and not ray_ev_recent_driver_pause_resume and ray_ev_activation_allowed:
        send_button = RAY_EV_ACTIVATE_BUTTON
        ray_ev_state_sync = True
        self.activateCruise = 1
      elif not CS.out.cruiseState.enabled and not ray_ev_cruise_active:
        if ray_ev_activation_requested and ray_ev_activation_allowed:
          self.ray_ev_activate_retry = max(self.ray_ev_activate_retry, 12)
          send_button = RAY_EV_ACTIVATE_BUTTON
          activate_cruise = True
          self.activateCruise = 1
        elif (hud_control.leadVisible or v_ego_kph > 10.0) and not is_ray_ev and self.activateCruise == 0:
          send_button = resume_button
          activate_cruise = self.activateCruise == 0
          self.activateCruise = 1
      elif CC.cruiseControl.resume and not is_ray_ev:
        send_button = resume_button
      elif not ray_ev_speed_sync_blocked and button_target < current and current>= 31 and self.speed_from_pcm != 1:
        if is_ray_ev:
          self.activateCruise = 0
        send_button = Buttons.SET_DECEL
      elif not ray_ev_speed_sync_blocked and button_target > current and current < 160 and self.speed_from_pcm != 1:
        if is_ray_ev:
          self.activateCruise = 0
        lead_blocking = (
          is_ray_ev and hud_control.leadVisible and hud_control.leadDistance > 0.0 and
          (
            (hud_control.leadRelSpeed < -1.0 and hud_control.leadDistance < max(22.0, CS.out.vEgo * 2.0)) or
            hud_control.leadDistance < max(12.0, CS.out.vEgo * 1.0)
          )
        )
        if not lead_blocking:
          send_button = Buttons.RES_ACCEL
    # If comma is off but the cluster is still in cruise step 7, pause stock cruise.
    elif is_ray_ev and ray_ev_cruise_state is True and not ray_ev_recent_driver_pause_resume and CS.out.activateCruise <= 0:
      send_button = RAY_EV_ACTIVATE_BUTTON
      ray_ev_state_sync = True
    elif CS.out.activateCruise and (not is_ray_ev or CS.out.activateCruise == 2): #CC.cruiseControl.activate:
      if (hud_control.leadVisible or v_ego_kph > 10.0) and self.activateCruise == 0:
        self.activateCruise = 1
        send_button = RAY_EV_ACTIVATE_BUTTON if is_ray_ev else resume_button
        activate_cruise = True

    if CS.out.brakePressed:
      self.activateCruise = 0
      self.ray_ev_activate_retry = 0
      self.ray_ev_cruise_enabled_last = False
      send_button = 0
    elif CS.out.gasPressed:
      self.activateCruise = 0
      self.ray_ev_activate_retry = 0
      send_button = 0

    if send_button == 0:
      self.button_spamming_count = 0
      self.prev_clu_speed = current
      return 0

    speed_diff = self.prev_clu_speed - current
    spamming_max = 1 if is_ray_ev else self.button_spam1
    if CS.cruise_buttons[-1] != Buttons.NONE:
      self.last_button_frame = self.frame
      self.button_wait = self.button_spam2
      self.button_spamming_count = 0
    elif abs(self.button_spamming_count) >= spamming_max or abs(speed_diff) > 0:
      self.last_button_frame = self.frame
      self.button_wait = self.button_spam2 if abs(self.button_spamming_count) >= spamming_max else 7
      self.button_spamming_count = 0

    self.prev_clu_speed = current
    send_button_allowed = (self.frame - self.last_button_frame) > self.button_wait
    #CC.debugTextCC = "{} speed_diff={:.1f},{:.0f}/{:.0f}, button={}, button_wait={}, count={}".format(
    #  send_button_allowed, speed_diff, target, current, send_button, self.button_wait, self.button_spamming_count)

    if send_button_allowed or activate_cruise or (CC.cruiseControl.resume and self.frame % 2 == 0):
      if is_ray_ev and ray_ev_state_sync and send_button == RAY_EV_ACTIVATE_BUTTON:
        self.last_button_frame = self.frame
        self.button_wait = RAY_EV_STATE_SYNC_WAIT_FRAMES
        self.ray_ev_speed_sync_block_frame = self.frame
        self.button_spamming_count = 0
        if CC.enabled:
          self.ray_ev_cruise_enabled_last = True
          if self.ray_ev_estimated_cruise_speed <= 0:
            self.ray_ev_estimated_cruise_speed = ray_ev_activation_estimate
        else:
          self.ray_ev_cruise_enabled_last = False
          self.ray_ev_estimated_cruise_speed = 0
          self.ray_ev_speed_bias_active = False
        return send_button
      if is_ray_ev and activate_cruise and send_button == RAY_EV_ACTIVATE_BUTTON:
        self.ray_ev_speed_sync_block_frame = self.frame
        if self.ray_ev_activate_retry <= 0:
          self.ray_ev_cruise_enabled_last = True
        self.ray_ev_estimated_cruise_speed = ray_ev_activation_estimate
        self.button_spamming_count = 0
      elif is_ray_ev and ray_ev_using_estimate:
        self.button_spamming_count = self.button_spamming_count + 1 if send_button == Buttons.RES_ACCEL else self.button_spamming_count - 1
        if send_button == Buttons.RES_ACCEL:
          self.ray_ev_estimated_cruise_speed = min(160, self.ray_ev_estimated_cruise_speed + 1)
        elif send_button == Buttons.SET_DECEL:
          self.ray_ev_estimated_cruise_speed = max(30, self.ray_ev_estimated_cruise_speed - 1)
      else:
        self.button_spamming_count = self.button_spamming_count + 1 if send_button == Buttons.RES_ACCEL else self.button_spamming_count - 1
      return send_button
    else:
      self.button_spamming_count = 0
    return 0

from openpilot.common.filter_simple import MyMovingAverage
class HyundaiJerk:
  def __init__(self):
    self.params = Params()
    self.jerk = 0.0
    self.jerk_u = self.jerk_l = 0.0
    self.cb_upper = self.cb_lower = 0.0
    self.jerk_u_min = 0.5
    self.carrot_cruise = 1
    self.carrot_cruise_accel = 0.0

  def check_carrot_cruise(self, CC, CS, hud_control, stopping, accel, a_target):
    carrot_cruise_decel = self.params.get_float("CarrotCruiseDecel")
    carrot_cruise_atc_decel = self.params.get_float("CarrotCruiseAtcDecel")
    if carrot_cruise_atc_decel >= 0 and 0 < hud_control.atcDistance < 500:
      carrot_cruise_decel = max(carrot_cruise_decel, carrot_cruise_atc_decel)
    self.carrot_cruise = 0
    if CS.out.carrotCruise > 0 and not CC.cruiseControl.override:
      if CS.softHoldActive == 0 and not stopping:
        if CS.out.vEgo > 10/3.6:
          if carrot_cruise_decel < 0:
            if (a_target > -0.1 or accel > -0.1):
              self.carrot_cruise = 1
              self.carrot_cruise_accel = 0.0
          else:
            self.carrot_cruise = 2
            carrot_cruise = min(accel, -carrot_cruise_decel * 0.01)
            self.carrot_cruise_accel = max(carrot_cruise, self.carrot_cruise_accel - 1.0 * DT_CTRL) #  점진적으로 줄임.
    if self.carrot_cruise == 0:
      self.carrot_cruise_accel = CS.out.aEgo
    
  def make_jerk(self, CP, CS, accel, actuators, hud_control):
    if actuators.longControlState == LongCtrlState.stopping:
      self.jerk = self.jerk_u_min / 2 - CS.out.aEgo
    else:
      jerk = actuators.jerk if actuators.longControlState == LongCtrlState.pid else 0.0
      #a_error = actuators.aTarget - CS.out.aEgo
      self.jerk = jerk# + a_error

    jerk_max_l = 5.0
    jerk_max_u = jerk_max_l
    if actuators.longControlState == LongCtrlState.off:
      self.jerk_u = jerk_max_u
      self.jerk_l = jerk_max_l
      self.cb_upper = self.cb_lower = 0.0
    else:
      if CP.flags & HyundaiFlags.CANFD:
        self.jerk_u = min(max(self.jerk_u_min, self.jerk * 2.0), jerk_max_u)
        self.jerk_l = min(max(1.0, -self.jerk * 4.0), jerk_max_l)
        self.cb_upper = self.cb_lower = 0.0
      else:
        self.jerk_u = min(max(self.jerk_u_min, self.jerk * 2.0), jerk_max_u)
        self.jerk_l = min(max(1.0, -self.jerk * 4.0), jerk_max_l)
        self.cb_upper = np.clip(0.9 + accel * 0.2, 0, 1.2)
        self.cb_lower = np.clip(0.8 + accel * 0.2, 0, 1.2)
