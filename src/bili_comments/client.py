from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from .errors import (
    ApiError,
    AuthenticationRequiredError,
    RiskControlError,
    VideoNotFoundError,
)
from .wbi import key_from_url, sign_params

API_BASE = "https://api.bilibili.com"


@dataclass(frozen=True)
class Video:
    aid: int
    bvid: str
    title: str
    owner_mid: str
    owner_name: str


@dataclass(frozen=True)
class CommentPage:
    replies: list[dict[str, Any]]
    next_cursor: str | None


class BiliClient:
    def __init__(
        self,
        *,
        delay: float = 1.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        unix_time: Callable[[], float] = time.time,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._unix_time = unix_time
        self._last_request_at: float | None = None
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=API_BASE,
            timeout=httpx.Timeout(20.0),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.bilibili.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
            },
            follow_redirects=True,
        )
        self._wbi_keys: tuple[str, str] | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BiliClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _pace(self) -> None:
        if self._last_request_at is not None:
            remaining = self.delay - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)

    def _request_json(self, path: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            self._pace()
            try:
                response = self._client.get(path, params=params)
                self._last_request_at = self._clock()
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        self._sleep(2**attempt)
                        continue
                if response.status_code in {401, 403}:
                    raise AuthenticationRequiredError(
                        f"接口要求登录或拒绝匿名访问（HTTP {response.status_code}）"
                    )
                if response.status_code == 412:
                    raise RiskControlError("请求触发风控或验证码（HTTP 412）")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ApiError("B站接口返回了非对象 JSON")
                return payload
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self._last_request_at = self._clock()
                if attempt >= self.max_retries:
                    raise ApiError(f"网络请求失败：{exc}") from exc
                self._sleep(2**attempt)
            except (AuthenticationRequiredError, RiskControlError):
                raise
            except httpx.HTTPStatusError as exc:
                raise ApiError(f"B站接口返回 HTTP {exc.response.status_code}") from exc
            except ValueError as exc:
                raise ApiError("B站接口返回了无效 JSON") from exc
        raise AssertionError("retry loop exited unexpectedly")

    @staticmethod
    def _data(payload: dict[str, Any]) -> dict[str, Any]:
        code = int(payload.get("code", -1))
        message = str(payload.get("message") or payload.get("msg") or "未知错误")
        if code == 0:
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        if code == -101:
            raise AuthenticationRequiredError(f"接口要求登录：{message}", code=code)
        if code in {-352, -412}:
            raise RiskControlError(f"请求触发风控或验证码：{message}", code=code)
        if code in {-400, -404, 62002}:
            raise VideoNotFoundError(f"视频不存在或不可访问：{message}", code=code)
        raise ApiError(f"B站接口错误 {code}：{message}", code=code)

    def _get_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_keys is None:
            payload = self._request_json("/x/web-interface/nav")
            code = int(payload.get("code", -1))
            if code not in {0, -101}:
                self._data(payload)
            data = payload.get("data")
            data = data if isinstance(data, dict) else {}
            wbi_img = data.get("wbi_img")
            if not isinstance(wbi_img, dict):
                raise ApiError("B站接口未返回 WBI 密钥")
            img_url = str(wbi_img.get("img_url", ""))
            sub_url = str(wbi_img.get("sub_url", ""))
            if not img_url or not sub_url:
                raise ApiError("B站接口返回的 WBI 密钥不完整")
            self._wbi_keys = (key_from_url(img_url), key_from_url(sub_url))
        return self._wbi_keys

    def get_video(self, bvid: str) -> Video:
        data = self._data(self._request_json("/x/web-interface/view", {"bvid": bvid}))
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        try:
            return Video(
                aid=int(data["aid"]),
                bvid=str(data.get("bvid") or bvid),
                title=str(data.get("title") or ""),
                owner_mid=str(owner.get("mid") or ""),
                owner_name=str(owner.get("name") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError("视频元数据缺少必要字段") from exc

    def get_comment_page(self, aid: int, cursor: str = "") -> CommentPage:
        params: dict[str, object] = {
            "oid": aid,
            "type": 1,
            "mode": 3,
            "plat": 1,
            "pagination_str": json.dumps(
                {"offset": cursor}, ensure_ascii=False, separators=(",", ":")
            ),
        }
        img_key, sub_key = self._get_wbi_keys()
        signed = sign_params(
            params,
            img_key,
            sub_key,
            timestamp=int(self._unix_time()),
        )
        data = self._data(self._request_json("/x/v2/reply/wbi/main", signed))
        replies = data.get("replies")
        if replies is None:
            replies = []
        if not isinstance(replies, list):
            raise ApiError("评论接口返回了无效的评论列表")

        cursor_data = data.get("cursor") if isinstance(data.get("cursor"), dict) else {}
        is_end = bool(cursor_data.get("is_end", False))
        pagination = cursor_data.get("pagination_reply")
        pagination = pagination if isinstance(pagination, dict) else {}
        next_offset = pagination.get("next_offset")
        next_cursor = None if is_end or next_offset in (None, "") else str(next_offset)
        if not is_end and next_cursor is None and replies:
            raise ApiError("评论接口未返回下一页游标，无法安全继续")
        return CommentPage(replies=replies, next_cursor=next_cursor)
