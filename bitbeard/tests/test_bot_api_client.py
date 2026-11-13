"""Tests for the Bot's authenticated proxy path to the API.

The Bot is a thin Frontend (ADR-0002): it authenticates as a Service
Account with the shared SERVICE_TOKEN and relays the acting Discord user
on every call, so the API can attribute the action in the Audit Ledger.
"""

import pytest

from bot.api_client import DISCORD_ID_HEADER, DISCORD_NAME_HEADER, ApiClient


class RecordingTransport:
    """Records every call; answers with a canned ApiResult."""

    def __init__(self, status_code=200, payload=None):
        self.calls = []
        self.status_code = status_code
        self.payload = payload

    def __call__(self, method, url, *, headers, params=None, json=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params,
                "json": json,
            }
        )
        from bot.api_client import ApiResult

        return ApiResult(self.status_code, self.payload)


@pytest.fixture
def transport():
    return RecordingTransport()


@pytest.fixture
def client(transport):
    return ApiClient("http://api.test/", "service-token", transport=transport)


def test_search_carries_the_service_token_and_discord_identity(client, transport):
    result = client.search("linux iso", discord_id="123456789", discord_name="Salty")

    assert result.status_code == 200
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "http://api.test/search"
    assert call["params"] == {"q": "linux iso"}
    assert call["headers"]["Authorization"] == "Bearer service-token"
    assert call["headers"][DISCORD_ID_HEADER] == "123456789"
    assert call["headers"][DISCORD_NAME_HEADER] == "Salty"


def test_submit_cancel_and_progress_relay_the_actor(client, transport):
    client.submit(
        "magnet:?xt=urn:btih:x", "Movie",
        discord_id="123456789", discord_name="Salty",
    )
    client.cancel(discord_id="123456789", discord_name="Salty")
    client.progress(discord_id="123456789", discord_name="Salty")

    methods = [call["method"] for call in transport.calls]
    urls = [call["url"] for call in transport.calls]
    assert methods == ["POST", "POST", "GET"]
    assert urls == [
        "http://api.test/download", "http://api.test/cancel",
        "http://api.test/progress",
    ]
    assert transport.calls[0]["json"] == {
        "magnet_link": "magnet:?xt=urn:btih:x", "category": "Movie",
    }
    for call in transport.calls:
        assert call["headers"]["Authorization"] == "Bearer service-token"
        assert call["headers"][DISCORD_ID_HEADER] == "123456789"


def test_an_unknown_display_name_simply_omits_the_name_header(client, transport):
    client.search("x", discord_id="123456789")

    headers = transport.calls[0]["headers"]
    assert DISCORD_ID_HEADER in headers
    assert DISCORD_NAME_HEADER not in headers


def test_an_unreachable_api_surfaces_as_a_503_result(transport):
    def dead_transport(method, url, *, headers, params=None, json=None):
        raise ConnectionError("api down")

    client = ApiClient("http://api.test", "service-token", transport=dead_transport)
    result = client.search("x", discord_id="123")

    assert result.status_code == 503
    assert "detail" in result.payload
