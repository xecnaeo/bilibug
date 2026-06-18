from __future__ import annotations

import json
import logging
import random
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from .errors import (
    ApiError,
    AuthenticationRequiredError,
    RiskControlError,
    VideoNotFoundError,
)
from .models import Comment, CommentPage, SubReplyPage, Video, VideoPage, VideoStats
from .wbi import key_from_url, sign_params

API_BASE = "https://api.bilibili.com"
ORDER_MODES = {"hot": 3, "time": 2}
logger = logging.getLogger(__name__)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(default if value is None or value == "" else value)
    except (TypeError, ValueError):
        return default


class BilibiliWebSource:
    name = "bilibili-web"

    def __init__(
        self,
        *,
        delay: float = 1.0,
        max_retries: int = 3,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        unix_time: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self._sleep = sleep
        self._clock = clock
        self._unix_time = unix_time
        self._jitter = jitter
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

    def __enter__(self) -> BilibiliWebSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _pace(self) -> None:
        if self._last_request_at is not None:
            remaining = self.delay - (self._clock() - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            value = response.headers.get("Retry-After")
            if value:
                try:
                    return max(0.0, float(value))
                except ValueError:
                    try:
                        retry_at = parsedate_to_datetime(value)
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=timezone.utc)
                        return max(
                            0.0,
                            (retry_at - datetime.now(timezone.utc)).total_seconds(),
                        )
                    except (TypeError, ValueError):
                        pass
        return float(2**attempt) + self._jitter() * 0.25

    def _request_json(
        self, path: str, params: dict[str, object] | None = None
    ) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            self._pace()
            started = self._clock()
            try:
                response = self._client.get(path, params=params)
                self._last_request_at = self._clock()
                logger.debug(
                    "request path=%s status=%s attempt=%s elapsed_ms=%d",
                    path,
                    response.status_code,
                    attempt + 1,
                    int((self._last_request_at - started) * 1000),
                )
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.max_retries:
                        self._sleep(self._retry_delay(attempt, response))
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
                logger.warning("request failed path=%s attempt=%s", path, attempt + 1)
                if attempt >= self.max_retries:
                    raise ApiError(f"网络请求失败：{exc}") from exc
                self._sleep(self._retry_delay(attempt))
            except (AuthenticationRequiredError, RiskControlError):
                raise
            except httpx.HTTPStatusError as exc:
                raise ApiError(f"B站接口返回 HTTP {exc.response.status_code}") from exc
            except ValueError as exc:
                raise ApiError("B站接口返回了无效 JSON") from exc
        raise AssertionError("retry loop exited unexpectedly")

    @staticmethod
    def _data(payload: dict[str, Any]) -> dict[str, Any]:
        code = _integer(payload.get("code"), -1)
        message = str(payload.get("message") or payload.get("msg") or "未知错误")
        if code == 0:
            return dict(_mapping(payload.get("data")))
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
            code = _integer(payload.get("code"), -1)
            if code not in {0, -101}:
                self._data(payload)
            wbi_img = _mapping(_mapping(payload.get("data")).get("wbi_img"))
            img_url = str(wbi_img.get("img_url", ""))
            sub_url = str(wbi_img.get("sub_url", ""))
            if not img_url or not sub_url:
                raise ApiError("B站接口返回的 WBI 密钥不完整")
            self._wbi_keys = (key_from_url(img_url), key_from_url(sub_url))
        return self._wbi_keys

    def get_video(self, bvid: str) -> Video:
        data = self._data(self._request_json("/x/web-interface/view", {"bvid": bvid}))
        owner = _mapping(data.get("owner"))
        stats = _mapping(data.get("stat"))
        pages: list[VideoPage] = []
        for item in data.get("pages") or []:
            if not isinstance(item, dict) or not item.get("cid"):
                continue
            dimension = _mapping(item.get("dimension"))
            pages.append(
                VideoPage(
                    cid=_integer(item.get("cid")),
                    page=_integer(item.get("page")),
                    title=str(item.get("part") or ""),
                    duration=_integer(item.get("duration")),
                    width=_integer(dimension.get("width")),
                    height=_integer(dimension.get("height")),
                    rotate=_integer(dimension.get("rotate")),
                )
            )
        try:
            return Video(
                aid=int(data["aid"]),
                bvid=str(data.get("bvid") or bvid),
                title=str(data.get("title") or ""),
                owner_mid=str(owner.get("mid") or ""),
                owner_name=str(owner.get("name") or ""),
                description=str(data.get("desc") or ""),
                cover_url=str(data.get("pic") or ""),
                category_id=_integer(data.get("tid_v2") or data.get("tid")),
                category_name=str(data.get("tname_v2") or data.get("tname") or ""),
                published_at=_integer(data.get("pubdate")),
                created_at=_integer(data.get("ctime")),
                duration=_integer(data.get("duration")),
                copyright=_integer(data.get("copyright")),
                state=_integer(data.get("state")),
                pages=tuple(pages),
                stats=VideoStats(
                    view=_integer(stats.get("view")),
                    danmaku=_integer(stats.get("danmaku")),
                    reply=_integer(stats.get("reply")),
                    favorite=_integer(stats.get("favorite")),
                    coin=_integer(stats.get("coin")),
                    share=_integer(stats.get("share")),
                    like=_integer(stats.get("like")),
                    current_rank=_integer(stats.get("now_rank")),
                    historical_rank=_integer(stats.get("his_rank")),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ApiError("视频元数据缺少必要字段") from exc

    @staticmethod
    def _pin_map(data: Mapping[str, Any]) -> dict[int, str]:
        pins: dict[int, str] = {}
        top = _mapping(data.get("top"))
        for pin_type in ("upper", "admin", "vote"):
            item = _mapping(top.get(pin_type))
            if item.get("rpid"):
                pins[_integer(item.get("rpid"))] = pin_type
        for item in data.get("top_replies") or []:
            if isinstance(item, dict) and item.get("rpid"):
                pins.setdefault(_integer(item.get("rpid")), "top")
        return pins

    @staticmethod
    def _parse_comment(
        reply: Mapping[str, Any], *, sort_rank: int, pin_type: str = ""
    ) -> Comment:
        member = _mapping(reply.get("member"))
        content = _mapping(reply.get("content"))
        level_info = _mapping(member.get("level_info"))
        root_rpid = _integer(reply.get("root") or reply.get("root_str"))
        parent_rpid = _integer(reply.get("parent") or reply.get("parent_str"))
        return Comment(
            rpid=int(reply["rpid"]),
            message=str(content.get("message") or ""),
            ctime=_integer(reply.get("ctime")),
            like_count=_integer(reply.get("like")),
            reply_count=_integer(reply.get("rcount", reply.get("count", 0))),
            author_mid=str(member.get("mid") or reply.get("mid_str") or ""),
            author_name=str(member.get("uname") or ""),
            author_level=(
                _integer(level_info.get("current_level"))
                if level_info.get("current_level") is not None
                else None
            ),
            root_rpid=root_rpid,
            parent_rpid=parent_rpid,
            level=1 if root_rpid else 0,
            state=_integer(reply.get("state")),
            pin_type=pin_type,
            sort_rank=sort_rank,
        )

    def get_comment_page(
        self, aid: int, cursor: str = "", *, order: str = "hot"
    ) -> CommentPage:
        if order not in ORDER_MODES:
            raise ValueError(f"unsupported comment order: {order}")
        params: dict[str, object] = {
            "oid": aid,
            "type": 1,
            "mode": ORDER_MODES[order],
            "plat": 1,
            "pagination_str": json.dumps(
                {"offset": cursor}, ensure_ascii=False, separators=(",", ":")
            ),
        }
        img_key, sub_key = self._get_wbi_keys()
        signed = sign_params(
            params, img_key, sub_key, timestamp=int(self._unix_time())
        )
        data = self._data(self._request_json("/x/v2/reply/wbi/main", signed))
        raw_replies = data.get("replies") or []
        if not isinstance(raw_replies, list):
            raise ApiError("评论接口返回了无效的评论列表")

        pins = self._pin_map(data)
        combined: list[Mapping[str, Any]] = []
        if not cursor:
            combined.extend(
                item for item in data.get("top_replies") or [] if isinstance(item, dict)
            )
        combined.extend(item for item in raw_replies if isinstance(item, dict))
        seen: set[int] = set()
        comments: list[Comment] = []
        for item in combined:
            rpid = _integer(item.get("rpid"))
            if not rpid or rpid in seen:
                continue
            seen.add(rpid)
            comments.append(
                self._parse_comment(
                    item, sort_rank=len(comments), pin_type=pins.get(rpid, "")
                )
            )

        cursor_data = _mapping(data.get("cursor"))
        is_end = bool(cursor_data.get("is_end", False))
        pagination = _mapping(cursor_data.get("pagination_reply"))
        next_offset = pagination.get("next_offset")
        next_cursor = None if is_end or next_offset in (None, "") else str(next_offset)
        if not is_end and next_cursor is None and comments:
            raise ApiError("评论接口未返回下一页游标，无法安全继续")
        return CommentPage(
            comments=tuple(comments),
            next_cursor=next_cursor,
            all_count=_integer(cursor_data.get("all_count")),
        )

    def get_sub_reply_page(
        self, aid: int, root_rpid: int, page: int
    ) -> SubReplyPage:
        data = self._data(
            self._request_json(
                "/x/v2/reply/reply",
                {"type": 1, "oid": aid, "root": root_rpid, "pn": page, "ps": 20},
            )
        )
        raw_replies = data.get("replies") or []
        if not isinstance(raw_replies, list):
            raise ApiError("楼中楼接口返回了无效的评论列表")
        page_data = _mapping(data.get("page"))
        page_num = _integer(page_data.get("num"), page)
        page_size = max(1, _integer(page_data.get("size"), 20))
        total_count = _integer(page_data.get("count"))
        next_page = page_num + 1 if page_num * page_size < total_count else None
        comments = tuple(
            self._parse_comment(item, sort_rank=(page_num - 1) * page_size + index)
            for index, item in enumerate(raw_replies)
            if isinstance(item, dict)
        )
        return SubReplyPage(
            comments=comments, next_page=next_page, total_count=total_count
        )


# Backward-compatible name retained for callers of v0.1.
BiliClient = BilibiliWebSource
