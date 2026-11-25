"""ALLOWED_CLIENTS: only the listed addresses may talk to the service at all."""
import asyncio
import sys
import types

import pytest

from deid import cli
from deid.jobs import JobStore
from deid.web.app import create_app


def ask(app, client, method="GET", path="/", headers=()):
    """Status and body for a request from the given peer address. A POST's body must not be asked for."""
    sent, asked = [], []

    async def receive():
        if method == "POST":
            raise AssertionError("the request body was read")
        if asked:
            await asyncio.Event().wait()  # nothing more is coming; the client stays connected
        asked.append(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "scheme": "http", "server": ("test", 80),
             "client": (client, 1) if client else None, "root_path": "", "headers": list(headers)}
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")


def app_for(settings, allowed):
    settings.allowed_clients = allowed
    return create_app(settings, JobStore(settings))


def test_without_a_list_every_address_is_served(settings):
    assert ask(app_for(settings, ""), "203.0.113.9")[0] == 200


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/jobs/nope", "/api/config"])
def test_an_address_off_the_list_gets_nothing_not_even_the_page(settings, path):
    app = app_for(settings, "127.0.0.1, 192.0.2.166")
    status, body = ask(app, "192.0.2.167", path=path)
    assert status == 403 and "192.0.2.167" in body.decode("utf-8")
    assert ask(app, "192.0.2.166", path=path)[0] != 403
    assert ask(app, "127.0.0.1", path=path)[0] != 403


def test_an_upload_from_an_address_off_the_list_is_refused_before_its_body_is_read(settings):
    app = app_for(settings, "192.0.2.166")
    form = [(b"content-type", b"multipart/form-data; boundary=x"), (b"content-length", b"1000")]
    assert ask(app, "192.0.2.167", "POST", "/api/jobs", form)[0] == 403


def test_the_server_itself_is_not_let_in_unless_listed(settings):
    assert ask(app_for(settings, "192.0.2.166"), "127.0.0.1")[0] == 403


def test_a_range_lets_in_every_address_inside_it_and_none_outside(settings):
    app = app_for(settings, "192.0.2.0/24")
    assert ask(app, "192.0.2.9")[0] == 200 and ask(app, "198.51.100.9")[0] == 403


def test_an_ipv4_address_arriving_in_ipv6_form_is_the_same_address(settings):
    app = app_for(settings, "192.0.2.166")
    assert ask(app, "::ffff:192.0.2.166")[0] == 200 and ask(app, "::ffff:192.0.2.167")[0] == 403


@pytest.mark.parametrize("client", [None, "unix-socket", ""])
def test_a_request_whose_address_is_unknown_is_refused_when_a_list_is_set(settings, client):
    assert ask(app_for(settings, "192.0.2.166"), client)[0] == 403


def test_a_forwarded_for_header_does_not_change_who_the_client_is(settings):
    app = app_for(settings, "192.0.2.166")
    assert ask(app, "10.9.9.9", headers=[(b"x-forwarded-for", b"192.0.2.166")])[0] == 403


@pytest.mark.parametrize("bad", ["192.0.2.166, office", "192.0.2.300", "192.0.2.1/40", ","])
def test_a_list_that_cannot_be_read_stops_the_service_from_starting(settings, bad):
    with pytest.raises(ValueError, match="ALLOWED_CLIENTS"):
        app_for(settings, bad)


def test_the_server_takes_the_client_address_from_the_connection_only(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setitem(sys.modules, "uvicorn", types.SimpleNamespace(run=lambda app, **kw: seen.update(kw)))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    cli.serve(types.SimpleNamespace(host=None, port=None))
    assert seen["proxy_headers"] is False
