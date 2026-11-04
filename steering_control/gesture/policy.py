"""OK 准备和工作手势的稳定停留门控。"""
from dataclasses import replace
from ..hardware.gestures.geometry import is_ok_geometry
from ..hardware.gestures.types import GestureReading
from . import gate as two_stage
WORK_LABELS = {"握拳": "fist", "摆1字": "one", "手掌展开": "palm"}

class ModelPolicy:
    """返回允许触发的动作与流程状态；界面识别应使用门控前判断。"""

    def __init__(self, kind, config, timing=None, labels=(), reference_ready_priority=False):
        self.kind = kind
        self.reference_ready_priority = bool(reference_ready_priority)
        self.label_keys = dict(WORK_LABELS)
        for label in sorted(set(labels) - set(self.label_keys)):
            if kind != 'ok' or label != 'OK手势':
                self.label_keys[label] = 'custom:' + label
        timing = two_stage.Timing(**timing) if timing else two_stage.Timing(
            min_score=config['gate']['min_score'], min_margin=config['gate']['min_margin'])
        commands = {key: index for index, key in enumerate(self.label_keys.values(), start=1)}
        self.gate = two_stage.TwoStageGate(commands=commands, timing=timing)

    def apply(self, prediction, reading, points, width, height, now):
        if self.kind == 'three':
            return (prediction if prediction.label in self.label_keys else replace(prediction, label=None)), ''
        ready = bool(is_ok_geometry(points, width, height) or prediction.label == 'OK手势')
        if self.reference_ready_priority and prediction.origin == 'correction' and prediction.label in self.label_keys:
            # 此模型的明确工作手势参考优先，避免几何 OK 误报阻断已经学会的动作。
            ready = False
        corrected = prediction.origin == 'correction' and prediction.label is not None
        gated = GestureReading(self.label_keys.get(prediction.label, 'unknown'),
                               1.0 if corrected else reading.score,
                               1.0 if corrected else reading.margin, reading.hand_present, now)
        self.gate.update(gated, ready=ready)
        permitted = (self.gate.phase == 'active' and not ready
                     and prediction.label in self.label_keys and self.gate.progress >= 1)
        status = self.gate.status
        key = self.label_keys.get(prediction.label, '')
        if key.startswith('custom:'):
            status = status.replace(key, prediction.label)
        return (prediction if permitted else replace(prediction, label=None)), status
