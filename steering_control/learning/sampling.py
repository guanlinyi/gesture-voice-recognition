"""按掌面方向、画面尺度和手形特征的覆盖度选取真实样例，不按停留时长分配。"""
from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class SamplingConfig:
    max_samples: int = 256
    angle_step: float = 10.0
    scale_step: float = .2
    feature_step: float = .01

    def __post_init__(self):
        if isinstance(self.max_samples, bool) or not isinstance(self.max_samples, int) or not 1 <= self.max_samples <= 4096:
            raise ValueError('参考样例上限须为 1–4096 的整数')
        if not math.isfinite(self.angle_step) or not 0 < self.angle_step <= 180:
            raise ValueError('参考方向间隔须在 0–180 度之间，且不为 0')
        for value in (self.scale_step, self.feature_step):
            if not math.isfinite(value) or value <= 0:
                raise ValueError('参考尺度和手形覆盖间隔须为正有限数')


def palm_rotation(points):
    """从掌部关节构建右手正交基；退化掌面不提供朝向观测。"""
    x = points[5] - points[17]
    length = np.linalg.norm(x)
    if length < 1e-8:
        return None
    x = x / length
    y = points[9] - points[0]
    y = y - np.dot(y, x) * x
    length = np.linalg.norm(y)
    if length < 1e-8:
        return None
    y = y / length
    return np.column_stack((x, y, np.cross(x, y)))


def select_coverage(values, config, priority_indices=None):
    """values 为 (原始帧编号, Descriptor)；返回下标及未覆盖程度。"""
    vectors = np.stack([np.asarray(descriptor.vector, dtype=float) for _, descriptor in values])
    if vectors.shape != (len(values), 210) or not np.isfinite(vectors).all():
        raise ValueError('手部特征格式无效')
    rotations = np.zeros((len(values), 9))
    rotation_valid = np.zeros(len(values), dtype=bool)
    scales = np.zeros(len(values))
    scale_valid = np.zeros(len(values), dtype=bool)
    for index, (_, descriptor) in enumerate(values):
        rotation = descriptor.rotation
        if rotation is not None:
            rotation = np.asarray(rotation, dtype=float)
            if rotation.shape == (3, 3) and np.isfinite(rotation).all():
                rotations[index] = rotation.ravel()
                rotation_valid[index] = True
        if descriptor.scale is not None and math.isfinite(descriptor.scale) and descriptor.scale > 0:
            scales[index], scale_valid[index] = math.log(descriptor.scale), True

    def gaps(index):
        feature_gap = np.sqrt(np.mean((vectors - vectors[index]) ** 2, axis=1)) / config.feature_step
        if rotation_valid[index]:
            cosines = np.clip((rotations @ rotations[index] - 1) / 2, -1, 1)
            angle_gap = np.where(rotation_valid, np.arccos(cosines) / math.radians(config.angle_step), 0)
        else:
            angle_gap = np.where(rotation_valid, 1e6, 0)
        if scale_valid[index]:
            scale_gap = np.where(scale_valid, np.abs(scales - scales[index]) / math.log1p(config.scale_step), 0)
        else:
            scale_gap = np.where(scale_valid, 1e6, 0)
        return np.maximum.reduce((feature_gap, angle_gap, scale_gap))

    priority_indices = set(priority_indices or ())
    priority = [index for index, (frame_index, _) in enumerate(values) if frame_index in priority_indices]
    selected = []
    nearest = np.full(len(values), float('inf'))

    def add(index):
        nonlocal nearest
        selected.append(index)
        nearest = np.minimum(nearest, gaps(index))
        nearest[selected] = 0

    # 固定原模型先推理完，再优先覆盖它的错例；不一边学习一边改变错例定义。
    if priority:
        while len(selected) < config.max_samples:
            index = priority[int(np.argmax(nearest[priority]))]
            if nearest[index] <= 1 + 1e-8:
                break
            add(index)
    else:
        add(0)
        # 纯覆盖模式保留不同的首尾状态。
        if len(values) > 1 and config.max_samples > 1 and nearest[-1] > 1:
            add(len(values) - 1)
    while len(selected) < min(config.max_samples, len(values)):
        index = int(np.argmax(nearest))
        if nearest[index] <= 1 + 1e-8:
            break
        add(index)
    selected.sort()
    remaining = float(np.max(nearest))
    return selected, dict(candidate_frames=len(values), selected_frames=len(selected),
                          source_frame_indices=[int(values[index][0]) for index in selected],
                          max_normalized_gap=remaining, coverage_complete=remaining <= 1 + 1e-8,
                          cap_reached=len(selected) == config.max_samples and remaining > 1 + 1e-8,
                          priority_frames=len(priority), selected_priority_frames=len(set(selected) & set(priority)),
                          priority_coverage_complete=not priority or bool(np.max(nearest[priority]) <= 1 + 1e-8),
                          orientation_frames=int(rotation_valid.sum()), scale_frames=int(scale_valid.sum()))
