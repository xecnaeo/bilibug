import json
from pathlib import Path

import httpx
import pytest

from bili_comments.client import BiliClient
from bili_comments.errors import AuthenticationRequiredError, RiskControlError

FIXTURES = Path(__file__).parent / "fixtures"
IMG = "7cd084941338484aae1ad9425b84077c"
SUB = "4932caff0ff746eab6f01bf08b70ac45"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def nav_payload(code: int = 0) -> dict:
    return {
        "code": code,
        "message": "账号未登录" if code else "0",
        "data": {
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB}.png",
            }
        },
    }


def test_video_contract_maps_metadata_pages_and_stats() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture("video_response.json"))

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    video = BiliClient(client=http, delay=0).get_video("BV1xx411c7mD")
    assert video.description == "测试简介"
    assert video.category_name == "单机游戏"
    assert video.pages[0].cid == 456
    assert video.pages[0].width == 1920
    assert video.stats.view == 100
    assert video.stats.like == 60


def test_video_optional_fields_default_when_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "aid": 123,
                    "bvid": "BV1xx411c7mD",
                    "title": "最小响应",
                    "owner": {"mid": 7, "name": "UP主"},
                },
            },
        )

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    video = BiliClient(client=http, delay=0).get_video("BV1xx411c7mD")
    assert video.description == ""
    assert video.pages == ()
    assert video.stats.view == 0


@pytest.mark.parametrize(("order", "mode"), [("hot", "3"), ("time", "2")])
def test_comment_contract_uses_cursor_order_and_typed_comment(order, mode) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json=nav_payload())
        return httpx.Response(200, json=fixture("comment_response.json"))

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    client = BiliClient(
        client=http, delay=0, unix_time=lambda: 1700000000, jitter=lambda: 0
    )
    page = client.get_comment_page(123, "cursor-1", order=order)
    assert page.next_cursor == "cursor-2"
    assert page.comments[0].message == "脱敏一级评论"
    assert page.comments[0].author_level == 5
    params = requests[-1].url.params
    assert json.loads(params["pagination_str"]) == {"offset": "cursor-1"}
    assert params["mode"] == mode
    assert "w_rid" in params


def test_subreply_contract_maps_parent_and_finishes_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture("subreply_response.json"))

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    page = BiliClient(client=http, delay=0).get_sub_reply_page(123, 1001, 1)
    assert page.next_page is None
    assert page.comments[0].root_rpid == 1001
    assert page.comments[0].parent_rpid == 1001
    assert page.comments[0].level == 1


def test_anonymous_nav_data_can_supply_wbi_keys() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/nav"):
            return httpx.Response(200, json=nav_payload(-101))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"replies": [], "cursor": {"is_end": True}}},
        )

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    assert BiliClient(client=http, delay=0).get_comment_page(123).comments == ()


@pytest.mark.parametrize(
    ("code", "error"),
    [(-101, AuthenticationRequiredError), (-352, RiskControlError), (-412, RiskControlError)],
)
def test_api_error_mapping(code, error) -> None:
    with pytest.raises(error):
        BiliClient._data({"code": code, "message": "blocked"})


def test_retries_429_uses_retry_after_then_backoff() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "4"}, request=request)
        if attempts == 2:
            return httpx.Response(500, request=request)
        return httpx.Response(200, json={"code": 0, "data": {}})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    client = BiliClient(
        client=http, delay=0, sleep=sleeps.append, jitter=lambda: 0
    )
    assert client._request_json("/test")["code"] == 0
    assert attempts == 3
    assert sleeps == [4, 2]


def test_http_412_maps_to_risk_control() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, request=request)

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    with pytest.raises(RiskControlError):
        BiliClient(client=http, delay=0)._request_json("/test")
