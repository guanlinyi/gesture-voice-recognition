"""21 个关键点的 210 维距离特征与只读参考匹配。"""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import numpy as np

BACKGROUND = "__background__"
FEATURE_VERSION = "hand-pairwise-distance-v1"
PAIRS = np.triu_indices(21, 1)


@dataclass
class Descriptor:
    space: str
    vector: np.ndarray
    rotation: object = None
    scale: float | None = None

def describe(landmarks, world_landmarks=None, width=640, height=480):
    if landmarks is None or len(landmarks) != 21:
        return None
    if world_landmarks is not None and len(world_landmarks) == 21:
        points = np.array([(p.x, p.y, p.z) for p in world_landmarks], dtype=float)
        space = 'world'
    else:
        points = np.array([(p.x * width, p.y * height, p.z * width) for p in landmarks], dtype=float)
        space = 'image'
    if not np.isfinite(points).all():
        return None
    scale = np.mean(np.linalg.norm(points[[5, 9, 13, 17]] - points[0], axis=1))
    if scale <= 1e-8:
        return None
    distances = np.linalg.norm(points[:, None] - points[None, :], axis=2)[PAIRS] / scale
    from ..learning.sampling import palm_rotation
    image = np.array([(p.x * width, p.y * height) for p in landmarks], dtype=float)
    image_scale = float(np.mean(np.linalg.norm(image[[5, 9, 13, 17]] - image[0], axis=1)) / math.hypot(width, height))
    return Descriptor(space, distances, palm_rotation(points), image_scale)


@dataclass
class Prediction:
    label: str | None
    distance: float | None
    reason: str
    confidence: float | None = None
    origin: str = 'reference'

class SampleLibrary:
    def __init__(self, threshold=.12, margin=.02):
        if not math.isfinite(threshold) or threshold <= 0 or not math.isfinite(margin) or margin < 0:
            raise ValueError('距离上限须为有限正数，类别差距须为有限非负数')
        self.threshold, self.margin = float(threshold), float(margin)
        self.demonstrations = []
        self.annotations = {}

    @property
    def labels(self):
        labels = {d['label'] for d in self.demonstrations}
        labels.update(a['label'] for a in self.annotations.values())
        return sorted(labels - {BACKGROUND})

    def predict(self, descriptor):
        if descriptor is None:
            return Prediction(None, None, 'no_hand')
        if descriptor.space not in ('world', 'image') or descriptor.vector.shape != (210,) or not np.isfinite(descriptor.vector).all():
            return Prediction(None, None, 'invalid_features')
        scores = {}
        for demonstration in self.demonstrations:
            vectors = [feature['vector'] for feature in demonstration['features'] if feature['space'] == descriptor.space]
            if not vectors:
                continue
            matrix = np.asarray(vectors, dtype=float)
            distance = float(np.sqrt(np.mean((matrix - descriptor.vector) ** 2, axis=1)).min())
            label = demonstration['label']
            scores[label] = min(scores.get(label, float('inf')), distance)
        if not scores:
            return Prediction(None, None, 'no_reference')
        ranked = sorted(scores.items(), key=lambda item: item[1])
        label, distance = ranked[0]
        if label == BACKGROUND:
            return Prediction(None, distance, 'background')
        if distance > self.threshold:
            return Prediction(None, distance, 'too_far')
        if len(ranked) > 1 and ranked[1][1] - distance < self.margin:
            return Prediction(None, distance, 'ambiguous')
        return Prediction(label, distance, 'matched')

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('schema') != 1 or data.get('feature_version') != FEATURE_VERSION:
            raise ValueError('样本库版本不兼容')
        library = cls(data['threshold'], data['margin'])
        for source, annotation in data.get('annotations', {}).items():
            if not isinstance(source, str) or not isinstance(annotation.get('label'), str) or not annotation['label'].strip():
                raise ValueError('人工标签格式无效')
            library.annotations[source] = annotation
        for demo in data['demonstrations']:
            if not isinstance(demo.get('label'), str) or not demo['label'].strip() or not demo.get('features'):
                raise ValueError('样本标签或特征缺失')
            for item in demo['features']:
                vector = np.asarray(item['vector'], dtype=float)
                if item['space'] not in ('world', 'image') or vector.shape != (210,) or not np.isfinite(vector).all():
                    raise ValueError('样本库特征无效')
            library.demonstrations.append(demo)
        return library
