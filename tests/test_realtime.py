"""Protocol-focused tests for the reusable HedgeDoc realtime session."""

from __future__ import annotations

import pytest
import requests

from hedgedoc_mcp.realtime import (
    HedgeDocRealtimeSession,
    RealtimeError,
    RealtimeTimeoutError,
)


class FakeSocket:
    def __init__(self, *, initial_permission="private", info_code=None, silent=False):
        self.handlers = {}
        self.connected = False
        self.connect_args = None
        self.emitted = []
        self.initial_permission = initial_permission
        self.info_code = info_code
        self.silent = silent
        self.disconnect_count = 0

    def on(self, event):
        def register(callback):
            self.handlers[event] = callback
            return callback

        return register

    def connect(self, url, **kwargs):
        self.connected = True
        self.connect_args = (url, kwargs)
        if self.info_code:
            self.handlers["info"]({"code": self.info_code})
        else:
            self.handlers["refresh"]({"permission": self.initial_permission, "title": "Note"})

    def emit(self, event, payload=None):
        self.emitted.append((event, payload))
        if self.silent:
            return
        if event == "permission":
            self.handlers["permission"]({"permission": payload})
        elif event == "refresh":
            requested = next(value for name, value in self.emitted if name == "permission")
            self.handlers["refresh"]({"permission": requested, "title": "Note"})

    def disconnect(self):
        self.disconnect_count += 1
        self.connected = False


def make_realtime(fake, *, base_url="https://md.example.com", cookie_name="connect.sid"):
    http_session = requests.Session()
    http_session.cookies.set(cookie_name, "signed-cookie", domain="md.example.com")
    realtime = HedgeDocRealtimeSession(
        base_url=base_url,
        note_id="note/alias",
        http_session=http_session,
        session_cookie_name=cookie_name,
        timeout=0.01,
        client_factory=lambda **_kwargs: fake,
    )
    return realtime


def test_connect_joins_note_and_records_initial_refresh():
    fake = FakeSocket()
    realtime = make_realtime(fake)

    realtime.connect()

    assert realtime.current_permission == "private"
    assert realtime.refresh_state["title"] == "Note"
    assert fake.connect_args[0] == "https://md.example.com?noteId=note%2Falias"
    assert fake.connect_args[1]["socketio_path"] == "socket.io"


def test_custom_cookie_name_and_subpath_are_used():
    fake = FakeSocket()
    realtime = make_realtime(
        fake,
        base_url="https://md.example.com/hedgedoc",
        cookie_name="custom.sid",
    )

    realtime.connect()

    assert fake.connect_args[1]["socketio_path"] == "hedgedoc/socket.io"
    assert fake.connect_args[1]["headers"] == {"Cookie": "custom.sid=signed-cookie"}


def test_permission_requires_matching_broadcast_and_requested_refresh():
    fake = FakeSocket()
    realtime = make_realtime(fake)
    realtime.connect()

    realtime.set_permission("protected")

    assert fake.emitted == [("permission", "protected"), ("refresh", None)]
    assert realtime.current_permission == "protected"


def test_unrelated_permission_broadcast_does_not_confirm_request():
    fake = FakeSocket(silent=True)
    realtime = make_realtime(fake)
    realtime.connect()
    original_emit = fake.emit

    def emit_unrelated(event, payload=None):
        original_emit(event, payload)
        if event == "permission":
            fake.handlers["permission"]({"permission": "limited"})

    fake.emit = emit_unrelated

    with pytest.raises(RealtimeTimeoutError, match="database-update broadcast"):
        realtime.set_permission("protected")

    assert ("refresh", None) not in fake.emitted


def test_matching_broadcast_without_refresh_confirmation_times_out():
    fake = FakeSocket(silent=True)
    realtime = make_realtime(fake)
    realtime.connect()
    original_emit = fake.emit

    def emit_broadcast_only(event, payload=None):
        original_emit(event, payload)
        if event == "permission":
            fake.handlers["permission"]({"permission": payload})

    fake.emit = emit_broadcast_only

    with pytest.raises(RealtimeTimeoutError, match="refresh confirmation"):
        realtime.set_permission("protected")


def test_silent_server_denial_times_out():
    fake = FakeSocket(silent=True)
    realtime = make_realtime(fake)
    realtime.connect()

    with pytest.raises(RealtimeTimeoutError, match="database-update broadcast"):
        realtime.set_permission("protected")


@pytest.mark.parametrize("code", [403, 404, 500])
def test_info_error_during_join_is_reported_and_connection_is_cleaned_up(code):
    fake = FakeSocket(info_code=code)
    realtime = make_realtime(fake)

    with pytest.raises(RealtimeError, match=rf"HTTP {code}"):
        with realtime:
            pass

    assert fake.disconnect_count == 1
    assert fake.connected is False


def test_context_manager_disconnects_after_success():
    fake = FakeSocket()
    realtime = make_realtime(fake)

    with realtime:
        realtime.set_permission("protected")

    assert fake.disconnect_count == 1
    assert fake.connected is False
