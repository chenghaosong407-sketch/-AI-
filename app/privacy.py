"""发送给模型前隐去手机号、身份证号(平台适配补充第3条)。姓名无法可靠识别,交给模型提醒。"""
from __future__ import annotations

import re

MASK = "[已隐去]"
_PATTERNS = [
    re.compile(r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"),  # 身份证
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),  # 手机号
    re.compile(r"(?<!\d)1[3-9]\d[ -]\d{4}[ -]\d{4}(?!\d)"),  # 带空格/横线的手机号
]


def mask_pii(text: str) -> tuple[str, int]:
    count = 0
    for pattern in _PATTERNS:
        text, n = pattern.subn(MASK, text)
        count += n
    return text, count
