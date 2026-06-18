from bili_comments.wbi import key_from_url, mixin_key, sign_params


def test_key_from_url() -> None:
    assert key_from_url("https://i0.hdslb.com/bfs/wbi/abc123.png") == "abc123"


def test_sign_params_is_stable_and_filters_special_characters() -> None:
    img = "7cd084941338484aae1ad9425b84077c"
    sub = "4932caff0ff746eab6f01bf08b70ac45"
    first = sign_params({"foo": "a!b", "bar": 2}, img, sub, timestamp=1700000000)
    second = sign_params({"bar": 2, "foo": "ab"}, img, sub, timestamp=1700000000)
    assert first == second
    assert first["wts"] == 1700000000
    assert len(str(first["w_rid"])) == 32
    assert len(mixin_key(img, sub)) == 32
