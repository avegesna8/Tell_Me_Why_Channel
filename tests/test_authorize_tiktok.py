import json

import pytest

from automation.authorize_tiktok import begin, exchange


class Response:
    ok = True
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Session:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.payload)


def settings():
    return {
        "TIKTOK_CLIENT_KEY": "client-key",
        "TIKTOK_CLIENT_SECRET": "secret",
        "TIKTOK_REDIRECT_URI": "https://example.com/tiktok-callback.html",
    }


def test_begin_writes_private_state_and_builds_scoped_url(tmp_path):
    state_file = tmp_path / "state.json"
    url = begin(settings(), state_file)
    assert state_file.exists()
    assert "client_key=client-key" in url
    assert "user.info.basic%2Cvideo.upload" in url
    assert "redirect_uri=https%3A%2F%2Fexample.com%2Ftiktok-callback.html" in url


def test_exchange_validates_state_and_returns_tokens(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"state": "expected"}))
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps({"code": "one-time-code", "state": "expected"}))
    session = Session({
        "access_token": "access",
        "refresh_token": "refresh",
        "open_id": "account",
        "scope": "user.info.basic,video.upload",
    })

    result = exchange(settings(), result_file, state_file, session=session)

    assert result == {
        "TIKTOK_ACCESS_TOKEN": "access",
        "TIKTOK_REFRESH_TOKEN": "refresh",
        "TIKTOK_OPEN_ID": "account",
    }
    assert not state_file.exists()
    assert session.calls[0][1]["data"]["client_secret"] == "secret"


def test_exchange_rejects_wrong_state_before_network_call(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"state": "expected"}))
    result_file = tmp_path / "result.json"
    result_file.write_text(json.dumps({"code": "code", "state": "wrong"}))
    session = Session({})

    with pytest.raises(RuntimeError, match="state did not match"):
        exchange(settings(), result_file, state_file, session=session)
    assert session.calls == []
