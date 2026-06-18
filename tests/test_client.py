import json

import httpx
import pytest

from bili_comments.client import BiliClient
from bili_comments.errors import AuthenticationRequiredError, RiskControlError

IMG = "7cd084941338484aae1ad9425b84077c"
SUB = "4932caff0ff746eab6f01bf08b70ac45"


def nav_response() -> httpx.Response:
    return httpx.Response(200, json={
        "code": 0,
        "data": {"wbi_img": {
            "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG}.png",
            "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB}.png",
        }},
    })


def test_comment_page_uses_cursor_and_parses_next_offset() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/nav"):
            return nav_response()
        return httpx.Response(200, json={
            "code": 0,
            "data": {
                "replies": [{"rpid": 1}],
                "cursor": {
                    "is_end": False,
                    "pagination_reply": {"next_offset": "cursor-2"},
                },
            },
        })

    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="https://api.bilibili.com")
    client = BiliClient(client=http, delay=0, unix_time=lambda: 1700000000)
    page = client.get_comment_page(123, "cursor-1")
    assert page.next_cursor == "cursor-2"
    params = requests[-1].url.params
    assert json.loads(params["pagination_str"]) == {"offset": "cursor-1"}
    assert params["mode"] == "3"
    assert "w_rid" in params


def test_anonymous_nav_data_can_supply_wbi_keys() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/nav"):
            response = nav_response()
            payload = response.json()
            payload["code"] = -101
            payload["message"] = "账号未登录"
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={
            "code": 0,
            "data": {"replies": [], "cursor": {"is_end": True}},
        })

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    client = BiliClient(client=http, delay=0)
    assert client.get_comment_page(123).replies == []


@pytest.mark.parametrize(
    ("code", "error"),
    [(-101, AuthenticationRequiredError), (-352, RiskControlError), (-412, RiskControlError)],
)
def test_api_error_mapping(code, error) -> None:
    with pytest.raises(error):
        BiliClient._data({"code": code, "message": "blocked"})


def test_retries_429_then_succeeds() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, request=request)
        return httpx.Response(200, json={"code": 0, "data": {}})

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    client = BiliClient(client=http, delay=0, sleep=sleeps.append)
    assert client._request_json("/test")["code"] == 0
    assert attempts == 3
    assert sleeps == [1, 2]


def test_http_412_maps_to_risk_control() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(412, request=request)

    http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.bilibili.com"
    )
    client = BiliClient(client=http, delay=0)
    with pytest.raises(RiskControlError):
        client._request_json("/test")
