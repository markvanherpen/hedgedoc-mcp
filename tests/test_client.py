"""Unit tests for hedgedoc_mcp.client — mocked HTTP, no real HedgeDoc needed."""

from __future__ import annotations

import pytest
import requests

from hedgedoc_mcp.client import (
    HedgeDocClient,
    HedgeDocError,
    PermissionChangeError,
    SessionExpiredError,
)


@pytest.fixture
def client():
    return HedgeDocClient("https://md.example.com")


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_login_success(client, mocker):
    mock_post = mocker.patch.object(client._session, "post")
    mock_post.return_value = _FakeResponse(status_code=302)
    client._session.cookies.set("connect.sid", "abc123", domain="md.example.com")

    cookie = client.login("user@example.com", "hunter2")
    assert cookie == "abc123"
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["data"] == {"email": "user@example.com", "password": "hunter2"}


def test_login_failure_raises(client, mocker):
    mocker.patch.object(client._session, "post", return_value=_FakeResponse(status_code=401))
    with pytest.raises(HedgeDocError, match="Login failed"):
        client.login("user@example.com", "wrong")


def test_set_session_cookie_decodes_urlencoded(client):
    client.set_session_cookie("s%3AaBc123")
    assert client.get_session_cookie(encoded=False) == "s:aBc123"


def test_set_session_cookie_accepts_raw(client):
    client.set_session_cookie("plain-value-no-percent")
    assert client.get_session_cookie(encoded=False) == "plain-value-no-percent"


def test_whoami_success(client, mocker):
    mocker.patch.object(
        client._session,
        "get",
        return_value=_FakeResponse(json_data={"status": "ok", "name": "kanishk"}),
    )
    data = client.whoami()
    assert data["name"] == "kanishk"


def test_whoami_expired_raises(client, mocker):
    mocker.patch.object(
        client._session, "get", return_value=_FakeResponse(json_data={"status": "forbidden"})
    )
    with pytest.raises(SessionExpiredError):
        client.whoami()


def test_create_note_success(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(
            status_code=302, headers={"Location": "https://md.example.com/abc123XYZ"}
        ),
    )
    result = client.create_note("# Hello")
    assert result.note_id == "abc123XYZ"
    assert result.url == "https://md.example.com/abc123XYZ"


def test_create_note_relative_location(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/relativeId"}),
    )
    result = client.create_note("# Hello")
    assert result.note_id == "relativeId"
    assert result.url == "https://md.example.com/relativeId"


def test_create_note_redirect_to_home_means_expired(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/"}),
    )
    with pytest.raises(SessionExpiredError):
        client.create_note("# Hello")


def test_create_note_non_redirect_raises(client, mocker):
    mocker.patch.object(client._session, "post", return_value=_FakeResponse(status_code=500))
    with pytest.raises(HedgeDocError, match="Unexpected status"):
        client.create_note("# Hello")


def test_create_note_with_alias_uses_alias_path(client, mocker):
    mock_post = mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(
            status_code=302, headers={"Location": "https://md.example.com/my-alias"}
        ),
    )
    client.create_note("# Hello", alias="my-alias")
    args, _ = mock_post.call_args
    assert args[0] == "https://md.example.com/new/my-alias"


def test_create_note_without_permission_preserves_server_default(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/abc"}),
    )
    set_permission = mocker.patch.object(client, "set_permission")

    client.create_note("# Hello")

    set_permission.assert_not_called()


def test_create_note_with_permission_applies_it(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/abc"}),
    )
    set_permission = mocker.patch.object(client, "set_permission", return_value="protected")

    result = client.create_note("# Handoff", permission="protected")

    assert result.note_id == "abc"
    set_permission.assert_called_once_with("abc", "protected")


def test_create_note_reports_url_when_permission_change_fails(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/abc"}),
    )
    mocker.patch.object(client, "set_permission", side_effect=HedgeDocError("denied"))

    with pytest.raises(PermissionChangeError, match=r"created at https://md.example.com/abc"):
        client.create_note("# Handoff", permission="protected")


@pytest.mark.parametrize(
    "permission", ["freely", "editable", "limited", "locked", "protected", "private"]
)
def test_set_permission_accepts_hedgedoc_values(client, mocker, permission):
    realtime = mocker.patch.object(
        client, "_realtime_permission", side_effect=[permission, permission]
    )

    assert client.set_permission("abc", permission) == permission
    assert realtime.call_args_list == [mocker.call("abc", requested=permission), mocker.call("abc")]


def test_set_permission_rejects_invalid_value_without_connecting(client, mocker):
    realtime = mocker.patch.object(client, "_realtime_permission")

    with pytest.raises(HedgeDocError, match="Invalid permission"):
        client.set_permission("abc", "public")

    realtime.assert_not_called()


def test_set_permission_emits_protocol_payload_and_reconnects(client, mocker):
    class FakeSocket:
        instances = []

        def __init__(self, **kwargs):
            self.handlers = {}
            self.connected = False
            self.emitted = []
            self.connect_args = None
            self.__class__.instances.append(self)

        def on(self, event):
            def register(callback):
                self.handlers[event] = callback
                return callback

            return register

        def connect(self, url, **kwargs):
            self.connected = True
            self.connect_args = (url, kwargs)
            persisted = "private" if self is self.__class__.instances[0] else "protected"
            self.handlers["refresh"]({"permission": persisted})

        def emit(self, event, payload):
            self.emitted.append((event, payload))
            self.handlers["permission"]({"permission": payload})

        def disconnect(self):
            self.connected = False

    mocker.patch("hedgedoc_mcp.client.socketio.Client", side_effect=FakeSocket)
    client._session.cookies.set("connect.sid", "signed-cookie", domain="md.example.com")

    assert client.set_permission("note/alias", "protected") == "protected"

    assert len(FakeSocket.instances) == 2
    mutation = FakeSocket.instances[0]
    assert mutation.connect_args[0] == "https://md.example.com?noteId=note%2Falias"
    assert mutation.connect_args[1]["socketio_path"] == "socket.io"
    assert mutation.connect_args[1]["headers"] == {"Cookie": "connect.sid=signed-cookie"}
    assert mutation.emitted == [("permission", "protected")]


def test_set_permission_does_not_report_success_without_server_event(client, mocker):
    class SilentSocket:
        connected = False

        def __init__(self, **kwargs):
            self.handlers = {}

        def on(self, event):
            def register(callback):
                self.handlers[event] = callback
                return callback

            return register

        def connect(self, url, **kwargs):
            self.connected = True
            self.handlers["refresh"]({"permission": "private"})

        def emit(self, event, payload):
            pass

        def disconnect(self):
            self.connected = False

    mocker.patch("hedgedoc_mcp.client.socketio.Client", side_effect=SilentSocket)
    client._session.cookies.set("connect.sid", "signed-cookie", domain="md.example.com")
    client.timeout = 0.01

    with pytest.raises(PermissionChangeError, match="did not confirm"):
        client.set_permission("abc", "protected")


def test_read_note_success(client, mocker):
    mocker.patch.object(
        client._session, "get", return_value=_FakeResponse(status_code=200, text="# Content")
    )
    assert client.read_note("abc") == "# Content"


def test_read_note_not_found_raises(client, mocker):
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(status_code=404))
    with pytest.raises(HedgeDocError, match="not found"):
        client.read_note("missing")


def test_note_info_success(client, mocker):
    mocker.patch.object(
        client._session,
        "get",
        return_value=_FakeResponse(
            status_code=200,
            json_data={
                "title": "My Note",
                "description": "desc",
                "viewcount": 5,
                "createtime": "2026-01-01T00:00:00Z",
                "updatetime": "2026-01-02T00:00:00Z",
            },
        ),
    )
    info = client.note_info("abc")
    assert info.title == "My Note"
    assert info.viewcount == 5


def test_history_requires_auth(client, mocker):
    mocker.patch.object(
        client._session, "get", return_value=_FakeResponse(json_data={"status": "forbidden"})
    )
    with pytest.raises(SessionExpiredError):
        client.history()


def test_history_success(client, mocker):
    mocker.patch.object(
        client._session,
        "get",
        return_value=_FakeResponse(json_data={"history": [{"id": "abc", "text": "Note 1"}]}),
    )
    history = client.history()
    assert len(history) == 1
    assert history[0]["text"] == "Note 1"


def test_status_no_auth_needed(client, mocker):
    mocker.patch.object(
        client._session,
        "get",
        return_value=_FakeResponse(json_data={"notesCount": 42, "onlineUsers": 3}),
    )
    data = client.status()
    assert data["notesCount"] == 42
