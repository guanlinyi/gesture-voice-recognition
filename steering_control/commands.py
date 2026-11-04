"""Explicit Chinese steering imperatives; whole utterances, never substrings."""
import re
import unicodedata


def normalize_text(text):
    if not isinstance(text, str):
        return ""
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


def parse_voice_command(text):
    if not isinstance(text, str) or "?" in unicodedata.normalize("NFKC", text):
        return None
    text = normalize_text(text)
    if any(word in text for word in ("不", "别", "取消", "等一下", "等会", "等等", "是否", "怎么", "如何", "吗", "么", "呢")):
        return None
    prefix = r"(?:请帮我|麻烦帮我|请你|麻烦你|帮我|请|麻烦|我想要|我想|我要|我需要)?"
    suffix = r"(?:操作一下|操作|一下|命令)?(?:吧)?"
    operations = (("HALF", r"(?:半折叠|半展开)"),
                  ("UNFOLD", r"展开"), ("FOLD", r"折叠"))
    for command, operation in operations:
        forms = (rf"(?:把|将)?方向盘(?:进行)?{operation}{suffix}",
                 rf"{operation}(?:一下)?方向盘{suffix}",
                 rf"对方向盘进行{operation}{suffix}")
        if any(re.fullmatch(prefix + form, text) for form in forms):
            return command
    return None
