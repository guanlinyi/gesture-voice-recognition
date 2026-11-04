"""work 原模型读数与人工纠正样本分开，只有匹配明确的样本才覆盖。"""
from .features import Prediction

GESTURE_NAMES = {
    'fist': '握拳', 'palm': '手掌展开', 'one': '摆1字', 'victory': '剪刀手',
    'thumb_up': '点赞', 'thumb_down': '点踩', 'love': '爱你', 'ok': 'OK手势',
}


def reading_record(reading):
    if reading is None or isinstance(reading, dict):
        return reading
    return {key: getattr(reading, key) for key in ('key', 'score', 'margin', 'hand_present')}


def work_prediction(reading, config, raw=False):
    reading = reading_record(reading)
    if not reading or not reading['hand_present']:
        return Prediction(None, None, 'no_hand', origin='work')
    label = GESTURE_NAMES.get(reading['key'])
    valid = (reading['score'] >= config['gate']['min_score'] and
             reading['margin'] >= config['gate']['min_margin'])
    reason = 'work_model' if valid and label else 'work_uncertain'
    return Prediction(label if raw or valid else None, None, reason, reading['score'], 'work')


def corrected_prediction(library, descriptor, reading, config):
    base = work_prediction(reading, config)
    reference = library.predict(descriptor)
    if reference.reason == 'matched':
        return Prediction(reference.label, reference.distance, 'corrected', origin='correction')
    if reference.reason == 'background' and reference.distance <= library.threshold:
        return Prediction(None, reference.distance, 'background', origin='correction')
    return base
