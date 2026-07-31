"""Unit tests for api.py — channels, retry policy, error mapping, redaction.

Runs against a real local aiohttp server (no response-mocking library), so the
tests exercise the actual wire behavior — headers, Basic auth, query params —
and stay compatible with any aiohttp version.
"""

from __future__ import annotations

import socket
from datetime import datetime

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.msedcl.api import (
    IST,
    MahaApiClient,
    MahaAuthError,
    MahaError,
    MahaNotFound,
    MahaServerError,
)
from tests.conftest import CNO, CONTACT_PAYLOAD, READING_PAYLOAD

CURRENT_PATH = f"/consappsmartmeterapi-2.1.0/002/GetCurrentReading/{CNO}"
CONTACT_PATH = "/App_Requests/getContactDetails"


class Recorder:
    """Catch-all handler: records every request, plays back queued responses."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.queues: dict[str, list[tuple]] = {}

    def queue(self, path: str, status: int = 200, text: str = "", json=None) -> None:
        self.queues.setdefault(path, []).append((status, text, json))

    async def handler(self, request: web.Request) -> web.Response:
        self.calls.append(
            {
                "path": request.path,
                "query": dict(request.query),
                "headers": dict(request.headers),
            }
        )
        queued = self.queues.get(request.path)
        if not queued:
            # Unqueued request = the test didn't expect it (e.g. a forbidden
            # retry). 418 is deliberately outside every handled-status branch.
            return web.Response(status=418, text="unexpected request")
        status, text, json_body = queued.pop(0)
        if json_body is not None:
            return web.json_response(json_body, status=status)
        return web.Response(status=status, text=text)


@pytest.fixture
async def api():
    recorder = Recorder()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", recorder.handler)
    server = TestServer(app)
    await server.start_server()
    session = aiohttp.ClientSession()
    client = MahaApiClient(
        session, "user", "secret", CNO,
        amisp="002", host=f"http://127.0.0.1:{server.port}",
    )
    yield client, recorder
    await session.close()
    await server.close()


async def test_smart_channel_sends_basic_auth_only(api):
    client, rec = api
    rec.queue(CURRENT_PATH, json=READING_PAYLOAD)
    assert await client.current_reading() == READING_PAYLOAD

    headers = rec.calls[0]["headers"]
    assert headers["Authorization"].startswith("Basic ")
    assert "Client-Os" not in headers
    assert "Client-Version" not in headers


async def test_standard_channel_sends_client_headers_no_auth(api):
    client, rec = api
    rec.queue(CONTACT_PATH, json=CONTACT_PAYLOAD)
    result = await client.contact_details()
    assert result["Consumer"]["BU"] == "0000"

    call = rec.calls[0]
    assert call["headers"]["Client-Os"] == "ANDROID"
    assert "Client-Version" in call["headers"]
    assert "Authorization" not in call["headers"]
    assert call["query"] == {"Con": CNO}


async def test_401_raises_auth_error_and_redacts_consumer_no(api):
    client, rec = api
    rec.queue(CURRENT_PATH, status=401)
    with pytest.raises(MahaAuthError) as err:
        await client.current_reading()
    assert CNO not in str(err.value)
    assert "<cno>" in str(err.value)


async def test_404_raises_not_found(api):
    client, rec = api
    rec.queue(CURRENT_PATH, status=404)
    with pytest.raises(MahaNotFound):
        await client.current_reading()


async def test_500_is_not_retried(api):
    """A 500 is deterministic bad-params/no-data — exactly one request."""
    client, rec = api
    rec.queue(CURRENT_PATH, status=500, text="oops")
    with pytest.raises(MahaServerError):
        await client.current_reading()
    assert len(rec.calls) == 1


async def test_503_is_retried_then_succeeds(api):
    client, rec = api
    rec.queue(CURRENT_PATH, status=503)
    rec.queue(CURRENT_PATH, json=READING_PAYLOAD)
    assert await client.current_reading() == READING_PAYLOAD
    assert len(rec.calls) == 2


async def test_connection_errors_exhaust_to_plain_maha_error():
    # A port that nothing listens on -> connection refused on every attempt.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    async with aiohttp.ClientSession() as session:
        client = MahaApiClient(
            session, "user", "secret", CNO,
            amisp="002", host=f"http://127.0.0.1:{port}", timeout=2,
        )
        with pytest.raises(MahaError) as err:
            await client.current_reading()
        assert not isinstance(err.value, (MahaAuthError, MahaServerError))


async def test_non_json_body_returned_as_text(api):
    client, rec = api
    rec.queue(CONTACT_PATH, text="MAINTENANCE")
    assert await client.contact_details() == "MAINTENANCE"


async def test_missing_amisp_rejected_before_network(api):
    client, rec = api
    client.amisp = ""
    with pytest.raises(MahaError):
        await client.current_reading()
    assert rec.calls == []


async def test_date_defaults_use_ist(api):
    client, rec = api
    month = datetime.now(IST).strftime("%Y%m")
    path = f"/consappsmartmeterapi-2.1.0/002/GetDailyConsumption/{CNO}/{month}"
    rec.queue(path, json=[])
    assert await client.daily_consumption() == []
    assert rec.calls[0]["path"] == path
