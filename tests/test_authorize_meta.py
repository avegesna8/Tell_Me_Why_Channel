import pytest

from automation.authorize_meta import exchange_credentials


class Response:
    def __init__(self, payload, ok=True, status_code=200):
        self.payload = payload
        self.ok = ok
        self.status_code = status_code

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


def settings():
    return {
        "META_APP_ID": "app",
        "META_APP_SECRET": "secret",
        "META_USER_ACCESS_TOKEN": "temporary",
        "META_PAGE_ID": "page-1",
        "META_INSTAGRAM_ACCOUNT_ID": "ig-1",
    }


def test_exchanges_and_validates_meta_credentials():
    session = Session([
        Response({"access_token": "long-user"}),
        Response({
            "id": "page-1",
            "name": "Tell Me Why",
            "access_token": "page-token",
            "instagram_business_account": {"id": "ig-1", "username": "tellmewhyexplained"},
        }),
    ])

    result = exchange_credentials(settings(), session=session)

    assert result == {
        "META_USER_ACCESS_TOKEN": "long-user",
        "META_PAGE_ACCESS_TOKEN": "page-token",
        "META_INSTAGRAM_ACCOUNT_ID": "ig-1",
    }
    assert session.calls[1][1]["headers"] == {"Authorization": "Bearer long-user"}
    assert "access_token" not in session.calls[1][1]["params"]


def test_rejects_different_instagram_account():
    session = Session([
        Response({"access_token": "long-user"}),
        Response({
            "id": "page-1",
            "access_token": "page-token",
            "instagram_business_account": {"id": "other"},
        }),
    ])

    with pytest.raises(RuntimeError, match="different Instagram"):
        exchange_credentials(settings(), session=session)


def test_reports_meta_error_without_echoing_credentials():
    session = Session([
        Response({"error": {"message": "Invalid OAuth access token."}}, ok=False, status_code=400),
    ])

    with pytest.raises(RuntimeError, match="Invalid OAuth access token") as error:
        exchange_credentials(settings(), session=session)
    assert "secret" not in str(error.value)
    assert "temporary" not in str(error.value)
