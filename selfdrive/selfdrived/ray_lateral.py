from cereal import car, log
from openpilot.selfdrive.selfdrived.events import Events, ET
from openpilot.selfdrive.selfdrived.state import StateMachine

EventName = log.OnroadEvent.EventName

# These events operate the longitudinal channel only. All other safety events,
# including driver monitoring, steering faults and gear/parking-brake checks,
# pass through the ordinary state machine unchanged.
LONGITUDINAL_EVENTS = frozenset(
  (
    EventName.buttonEnable,
    EventName.buttonCancel,
    EventName.pcmEnable,
    EventName.pcmDisable,
    EventName.pedalPressed,
    EventName.preEnableStandstill,
    EventName.gasPressedOverride,
    EventName.wrongCarMode,
    EventName.wrongCruiseMode,
    EventName.resumeBlocked,
  )
)


def panda_lateral_ready(CP, pandas):
  if not CP.safetyConfigs or len(pandas) != len(CP.safetyConfigs):
    return False
  for index, config in enumerate(CP.safetyConfigs):
    panda = pandas[index]
    if (
      panda.safetyModel in (car.CarParams.SafetyModel.silent, car.CarParams.SafetyModel.noOutput)
      or panda.safetyModel != config.safetyModel
      or panda.safetyParam != config.safetyParam
      or panda.alternativeExperience != CP.alternativeExperience
      or panda.faults
      or panda.heartbeatLost
      or panda.safetyRxChecksInvalid
    ):
      return False
    if any(bus.busOff or bus.errorPassive for bus in (panda.canState0, panda.canState1, panda.canState2)):
      return False
  # controlsAllowed tracks longitudinal engagement in this fork. Never change
  # it or weaken firmware torque/rate checks to enable independent steering.
  return True


class RayLateralState:
  def __init__(self):
    self.state_machine = StateMachine()
    self.events = Events()
    self.enabled = False
    self.active = False
    # Ray starts with its lateral selection on. Do not engage while parked or
    # unhealthy; emit one enable request after the first healthy driving
    # state. An explicit LFA-off cancels this boot request permanently.
    self.selected_prev = True
    self.boot_enable_pending = True

  def update(self, selected, events, *, initialized, passive, can_valid, inputs_ok, monitoring_ok, panda_ok):
    # Before the first valid carState, latEnabled can still be its schema
    # default. That is not an explicit driver OFF and must not consume boot.
    if not can_valid:
      selected = self.selected_prev
    rising = selected and not self.selected_prev
    falling = not selected and self.selected_prev
    self.selected_prev = selected
    if not selected:
      self.boot_enable_pending = False

    self.events.clear()
    for name in events.names:
      if name not in LONGITUDINAL_EVENTS:
        self.events.add(name)

    if any(name in events.names for name in (EventName.driverDistracted3, EventName.driverUnresponsive3, EventName.tooDistracted)):
      # Terminal DM alerts in this fork are permanent alerts, not disable
      # events. Keep the driver-facing alert and explicitly end lateral control.
      self.events.add(EventName.tooDistracted)
      self.state_machine.state = log.SelfdriveState.OpenpilotState.disabled

    if not initialized:
      self.events.add(EventName.selfdriveInitializing)
    if passive:
      self.events.add(EventName.dashcamMode)
    if not can_valid:
      self.events.add(EventName.canError)
    if not inputs_ok or not monitoring_ok or not panda_ok:
      # A stale/disabled monitor or unhealthy CAN interface must fail closed,
      # including when no longitudinal controller is engaged.
      self.events.add(EventName.controlsMismatch)

    # Wait for every entry gate, including temporary model/calibration faults.
    # Consume the boot request only after engagement really succeeds. After
    # that, faults still require a deliberate switch cycle to re-engage.
    boot_ready = (selected and self.boot_enable_pending and initialized and not passive and can_valid and
                  inputs_ok and monitoring_ok and panda_ok and
                  not any(self.events.contains(kind) for kind in (ET.NO_ENTRY, ET.USER_DISABLE,
                                                                  ET.IMMEDIATE_DISABLE, ET.SOFT_DISABLE)))
    rising |= boot_ready

    if rising:
      self.events.add(EventName.buttonEnable)
    elif falling:
      self.events.add(EventName.buttonCancel)

    self.enabled, self.active = self.state_machine.update(self.events)
    if passive or not initialized:
      self.state_machine.state = log.SelfdriveState.OpenpilotState.disabled
      self.enabled = self.active = False
    if self.enabled:
      self.boot_enable_pending = False
    return self.enabled, self.active
