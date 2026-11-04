"""准备后进入连续工作会话；离开画面或长期无工作手势时退出。"""
from dataclasses import dataclass
import math

from steering_control.hardware.gestures.types import GestureReading


@dataclass(frozen=True)
class Timing:
    ready_hold: float = .15
    select_hold: float = .2
    idle_timeout: float = 3.0
    ready_release: float = .15
    lost_hold: float = .15
    max_gap: float = .35
    min_score: float = .70
    min_margin: float = .15

    def __post_init__(self):
        for name in ('ready_hold', 'select_hold', 'idle_timeout', 'ready_release', 'lost_hold', 'max_gap'):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} 必须为有限正数')
        for name in ('min_score', 'min_margin'):
            if not math.isfinite(getattr(self, name)) or not 0 <= getattr(self, name) <= 1:
                raise ValueError(f'{name} 必须在 0 到 1 之间')


class TwoStageGate:
    def __init__(self, commands=None, timing=None):
        self.commands = {'fist': 1, 'one': 2, 'palm': 3} if commands is None else dict(commands)
        self.timing = timing or Timing()
        self.last_t = None
        self.outputs = 0
        self._ready_since = self._not_ready_since = None
        self._ready_latched = False
        self._enter('ready', '等待准备动作')

    def _enter(self, phase, status):
        self.phase, self.status = phase, status
        self.progress = 0.0
        self._lost_since = None
        self._candidate_since = self._last_work_at = None
        self._candidate = self._last_emitted = None

    def cancel(self):
        self._enter('ready', '已退出，重新做准备动作即可进入')

    def _new_ready(self, now, ready):
        """一次稳定 OK 只算一次；退出后按住旧 OK 不会自动反复进入。"""
        if not ready:
            self._ready_since = None
            if self._not_ready_since is None:
                self._not_ready_since = now
            if now - self._not_ready_since >= self.timing.ready_release:
                self._ready_latched = False
            return False
        self._not_ready_since = None
        if self._ready_latched:
            return False
        if self._ready_since is None:
            self._ready_since = now
        if now - self._ready_since >= self.timing.ready_hold:
            self._ready_latched = True
            return True
        return False

    @property
    def remaining(self):
        if self.phase != 'active' or self.last_t is None:
            return 0.0
        return max(0.0, self.timing.idle_timeout - (self.last_t - self._last_work_at))

    def _forget_candidate(self):
        self._candidate = self._candidate_since = None
        self.progress = 0.0

    def update(self, reading: GestureReading, ready: bool = False):
        now, cfg = reading.t, self.timing
        if not math.isfinite(now):
            self.last_t = None
            self._ready_since = self._not_ready_since = None
            self._enter('ready', '时间无效，请重新准备')
            return None
        if self.last_t is not None and (now < self.last_t or now - self.last_t > cfg.max_gap):
            self._ready_since = self._not_ready_since = None
            self._enter('ready', '帧时间不连续，请重新准备')
        self.last_t = now
        ready = bool(ready and reading.hand_present)
        new_ready = self._new_ready(now, ready)

        # 旧许可到期后，工作手势不能恢复它；新的准备动作可以立即重新进入。
        if self.phase == 'active' and now - self._last_work_at >= cfg.idle_timeout:
            self._enter('ready', '无工作手势，已退出，请重新准备')

        if self.phase == 'ready':
            if new_ready:
                self._enter('active', '准备好，请做工作手势')
                self._last_work_at = now
            elif ready and not self._ready_latched:
                self.progress = min(1.0, (now - self._ready_since) / cfg.ready_hold)
                self.status = '确认准备动作'
            else:
                self.progress = 0.0
                self.status = '等待准备动作'
            return None

        if not reading.hand_present:
            self._forget_candidate()
            if self._lost_since is None:
                self._lost_since = now
            self.status = '未见手，暂停输出'
            if now - self._lost_since >= cfg.lost_hold:
                self._enter('ready', '手已离开，已退出，请重新准备')
            return None
        self._lost_since = None
        # OK 几何命中时，即使原模型误判为 palm，也不拿它续期或输出 3。
        valid = (
            not ready and reading.key in self.commands
            and math.isfinite(reading.score) and math.isfinite(reading.margin)
            and reading.score >= cfg.min_score and reading.margin >= cfg.min_margin
        )
        if not valid:
            self._forget_candidate()
            self.status = '准备好，请做工作手势' if ready else '工作中：等待有效手势'
            return None

        self._last_work_at = now
        if self._candidate != reading.key:
            self._candidate, self._candidate_since = reading.key, now
        self.progress = min(1.0, (now - self._candidate_since) / cfg.select_hold)
        label = {'fist': '握拳', 'one': '食指', 'palm': '张掌'}.get(reading.key, reading.key)
        if now - self._candidate_since < cfg.select_hold:
            self.status = f'确认{label}…'
            return None
        number = self.commands[reading.key]
        self.status = f'工作中：{label}（{number}）'
        if self._last_emitted == reading.key:
            return None
        self._last_emitted = reading.key
        self.outputs += 1
        return number
