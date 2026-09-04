"""Protocol-focused tests for the reusable HedgeDoc realtime session."""

from __future__ import annotations

import pytest
import requests

from hedgedoc_mcp.realtime import (
    HedgeDocRealtimeSession,
    RealtimeConflictError,
    RealtimeError,
    RealtimeProtocolError,
    RealtimeTimeoutError,
    build_replacement_operation,
    utf16_length,
)


class FakeSocket:
    def __init__(
        self,
        *,
        initial_permission="private",
        initial_document="Version: A",
        initial_revision=0,
        initial_clients=None,
        info_code=None,
        silent=False,
    ):
        self.handlers = {}
        self.connected = False
        self.connect_args = None
        self.emitted = []
        self.initial_permission = initial_permission
        self.initial_document = initial_document
        self.initial_revision = initial_revision
        self.initial_clients = initial_clients or {}
        self.info_code = info_code
        self.silent = silent
        self.disconnect_count = 0
        self.sid = "self"

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
            self.handlers["doc"](
                {
                    "str": self.initial_document,
                    "revision": self.initial_revision,
                    "clients": self.initial_clients,
                }
            )
            self.handlers["refresh"](
                {
                    "permission": self.initial_permission,
                    "title": "Note",
                    "docmaxlength": 100000,
                }
            )

    def emit(self, event, payload=None):
        self.emitted.append((event, payload))
        if self.silent:
            return
        if event == "permission":
            self.handlers["permission"]({"permission": payload})
        elif event == "refresh":
            requested = next(value for name, value in self.emitted if name == "permission")
            self.handlers["refresh"]({"permission": requested, "title": "Note"})
        elif event == "operation":
            revision, _operation, _selection = payload
            self.handlers["ack"](revision + 1)
            self.handlers["check"]({"updatetime": 1})

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
    assert realtime.document == "Version: A"
    assert realtime.revision == 0
    assert realtime.clients == {}
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


@pytest.mark.parametrize(
    ("current", "replacement", "operation"),
    [
        ("", "new", ["new"]),
        ("old", "", [-3]),
        ("old", "new", ["new", -3]),
        ("😀", "replacement", ["replacement", -2]),
    ],
)
def test_build_replacement_operation_uses_exact_hedgedoc_wire_format(
    current, replacement, operation
):
    assert build_replacement_operation(current, replacement) == operation


def test_utf16_length_uses_javascript_units():
    assert utf16_length("A😀B") == 4


def test_replacement_requires_ack_and_post_update_check():
    fake = FakeSocket(initial_document="A", initial_revision=7)
    realtime = make_realtime(fake)
    realtime.connect()

    result = realtime.replace_document("B")

    assert fake.emitted[-1] == ("operation", (7, ["B", -1], None))
    assert result.revision_before == 7
    assert result.revision_after == 8
    assert result.check_received is True


def test_realtime_session_allows_only_one_replacement_attempt():
    fake = FakeSocket(initial_document="A")
    realtime = make_realtime(fake)
    realtime.connect()

    realtime.replace_document("B")

    with pytest.raises(RealtimeProtocolError, match="reconnect before retrying"):
        realtime.replace_document("C")
    assert [event for event, _payload in fake.emitted].count("operation") == 1


def test_existing_editor_refuses_before_operation():
    fake = FakeSocket(initial_clients={"other": {"name": "Editor"}})
    realtime = make_realtime(fake)
    realtime.connect()

    with pytest.raises(RealtimeConflictError) as raised:
        realtime.replace_document("B")

    assert raised.value.result.operation_submitted is False
    assert not any(event == "operation" for event, _payload in fake.emitted)


def test_peer_operation_during_attempt_is_conflict_after_ack_and_check():
    fake = FakeSocket(initial_document="A", silent=True)
    realtime = make_realtime(fake)
    realtime.connect()

    def emit(event, payload=None):
        fake.emitted.append((event, payload))
        if event == "operation":
            fake.handlers["operation"]("peer", 1, ["x"], None)
            fake.handlers["ack"](2)
            fake.handlers["check"]({"updatetime": 1})

    fake.emit = emit

    with pytest.raises(RealtimeConflictError) as raised:
        realtime.replace_document("B")

    assert raised.value.result.operation_acknowledged is True
    assert raised.value.result.concurrency_detected is True
    assert raised.value.result.check_received is True


def test_ack_revision_greater_than_expected_is_conflict():
    fake = FakeSocket(initial_document="A", initial_revision=3, silent=True)
    realtime = make_realtime(fake)
    realtime.connect()

    def emit(event, payload=None):
        fake.emitted.append((event, payload))
        if event == "operation":
            fake.handlers["ack"](5)
            fake.handlers["check"]({"updatetime": 1})

    fake.emit = emit

    with pytest.raises(RealtimeConflictError) as raised:
        realtime.replace_document("B")

    assert raised.value.result.revision_before == 3
    assert raised.value.result.revision_after == 5


def test_unauthorized_silent_operation_times_out():
    fake = FakeSocket(initial_document="A", silent=True)
    realtime = make_realtime(fake)
    realtime.connect()

    with pytest.raises(RealtimeTimeoutError, match="text-operation acknowledgement"):
        realtime.replace_document("B")

    progress = realtime.update_progress()
    assert progress.operation_submitted is True
    assert progress.operation_acknowledged is False


@pytest.mark.parametrize(
    "doc",
    [
        None,
        {"str": "A", "revision": "0", "clients": {}},
        {"str": "A", "revision": 0, "clients": []},
    ],
)
def test_malformed_or_missing_doc_state_fails_join(doc):
    fake = FakeSocket()

    def connect(url, **kwargs):
        fake.connected = True
        fake.connect_args = (url, kwargs)
        if doc is not None:
            fake.handlers["doc"](doc)
        fake.handlers["refresh"]({"permission": "private", "docmaxlength": 100000})

    fake.connect = connect
    realtime = make_realtime(fake)

    with pytest.raises((RealtimeProtocolError, RealtimeTimeoutError)):
        realtime.connect()
