from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from urllib.parse import urlencode, urlparse

MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)
FILTER_CHARS = "!'()*"


def key_from_url(url: str) -> str:
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    return filename.split(".", 1)[0]


def mixin_key(img_key: str, sub_key: str) -> str:
    original = img_key + sub_key
    if len(original) < 64:
        raise ValueError("WBI keys are shorter than expected")
    return "".join(original[index] for index in MIXIN_KEY_ENC_TAB)[:32]


def sign_params(
    params: Mapping[str, object],
    img_key: str,
    sub_key: str,
    *,
    timestamp: int | None = None,
) -> dict[str, object]:
    unsigned: dict[str, object] = dict(params)
    unsigned["wts"] = int(time.time()) if timestamp is None else timestamp
    cleaned = {
        key: "".join(char for char in str(value) if char not in FILTER_CHARS)
        for key, value in sorted(unsigned.items())
    }
    query = urlencode(cleaned)
    cleaned["w_rid"] = hashlib.md5(
        (query + mixin_key(img_key, sub_key)).encode("utf-8")
    ).hexdigest()
    cleaned["wts"] = unsigned["wts"]
    return cleaned
