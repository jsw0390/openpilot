from cereal import car
from openpilot.selfdrive.selfdrived.events import Alert, AlertStatus, AlertSize, Priority, VisualAlert, AudibleAlert


class RayLateralAlerts:
  """Warn on loss of commanded steering, including controlsd-only suspension."""

  def __init__(self):
    self.controlling_prev = False
    self.moving_prev = False

  def update(self, CS, *, state_active, selected, soft_disabling,
             command_active, command_fresh, car_state_fresh, disengage_reason=''):
    moving = (CS.gearShifter == car.CarState.GearShifter.drive and
              abs(CS.vEgo) > 0.1 and not CS.standstill) if car_state_fresh else self.moving_prev
    controlling = state_active and command_active and command_fresh and car_state_fresh
    alerts = []
    if moving and self.controlling_prev and not controlling:
      alert = Alert(
        "핸들을 직접 조작하세요" if selected else "조향 보조 해제",
        disengage_reason or "조향 보조가 중단되었습니다",
        AlertStatus.critical if selected else AlertStatus.userPrompt, AlertSize.mid,
        Priority.HIGH if selected else Priority.MID, VisualAlert.steerRequired,
        AudibleAlert.warningImmediate if selected else AudibleAlert.prompt, 3.0)
      alert.alert_type = 'rayLateralLost'
      alerts.append(alert)
    elif moving and controlling and soft_disabling:
      alert = Alert("핸들을 잡으세요", "조향 보조 해제 예정",
                    AlertStatus.userPrompt, AlertSize.mid, Priority.HIGH,
                    VisualAlert.steerRequired, AudibleAlert.warningSoft, 0.2)
      alert.alert_type = 'rayLateralWillDisengage'
      alerts.append(alert)
    self.controlling_prev = controlling
    self.moving_prev = moving
    return alerts
