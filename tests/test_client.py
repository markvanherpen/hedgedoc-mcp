"""Unit tests for hedgedoc_mcp.client — mocked HTTP, no real HedgeDoc needed."""

from __future__ import annotations

import pytest
import requests

from hedgedoc_mcp.client import (
    HedgeDocClient,
    HedgeDocError,
    NoteUpdateError,
    PermissionChangeError,
    SessionExpiredError,
    content_sha256,
)
from hedgedoc_mcp.realtime import RealtimeConflictError, RealtimeTimeoutError, RealtimeUpdateResult


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


def test_login_success_with_custom_session_cookie_name(mocker):
    client = HedgeDocClient("https://md.example.com", session_cookie_name="hedgedoc.sid")
    mocker.patch.object(client._session, "post", return_value=_FakeResponse(status_code=302))
    client._session.cookies.set("hedgedoc.sid", "abc123", domain="md.example.com")

    assert client.login("user@example.com", "hunter2") == "abc123"


def test_login_rejects_wrong_session_cookie_name(client, mocker):
    mocker.patch.object(client._session, "post", return_value=_FakeResponse(status_code=302))
    client._session.cookies.set("hedgedoc.sid", "abc123", domain="md.example.com")

    with pytest.raises(HedgeDocError, match="Login failed"):
        client.login("user@example.com", "hunter2")


def test_login_failure_raises(client, mocker):
    mocker.patch.object(client._session, "post", return_value=_FakeResponse(status_code=401))
    with pytest.raises(HedgeDocError, match="Login failed"):
        client.login("user@example.com", "wrong")


def test_login_does_not_accept_a_rate_limit_cookie(client, mocker):
    mocker.patch.object(client._session, "post", return_value=_FakeResponse(status_code=429))
    client._session.cookies.set("connect.sid", "rate-limit-cookie", domain="md.example.com")

    with pytest.raises(HedgeDocError, match="HTTP 429"):
        client.login("user@example.com", "password")


def test_set_session_cookie_decodes_urlencoded(client):
    client.set_session_cookie("s%3AaBc123")
    assert client.get_session_cookie(encoded=False) == "s:aBc123"


def test_set_session_cookie_accepts_raw(client):
    client.set_session_cookie("plain-value-no-percent")
    assert client.get_session_cookie(encoded=False) == "plain-value-no-percent"


def test_set_session_cookie_uses_custom_cookie_name():
    client = HedgeDocClient("https://md.example.com", session_cookie_name="hedgedoc.sid")
    client.set_session_cookie("s%3AaBc123")

    assert client.get_session_cookie(encoded=False) == "s:aBc123"
    assert client._session.cookies.get("connect.sid") is None


@pytest.mark.parametrize("cookie_name", ["", "contains space", "connect/sid"])
def test_rejects_invalid_session_cookie_name(cookie_name):
    with pytest.raises(HedgeDocError, match="cookie name"):
        HedgeDocClient("https://md.example.com", session_cookie_name=cookie_name)


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


def test_creating_existing_alias_conflicts_and_preserves_original_content(client, mocker):
    notes = {}

    def post(url, data, **_kwargs):
        alias = url.rsplit("/", 1)[-1]
        if alias in notes:
            return _FakeResponse(status_code=409)
        notes[alias] = data.decode("utf-8")
        return _FakeResponse(status_code=302, headers={"Location": f"/{alias}"})

    def get(url, **_kwargs):
        note_id = url.removesuffix("/download").rsplit("/", 1)[-1]
        return _FakeResponse(status_code=200, text=notes[note_id])

    mocker.patch.object(client._session, "post", side_effect=post)
    mocker.patch.object(client._session, "get", side_effect=get)

    client.create_note("# Original", alias="existing-alias")
    with pytest.raises(HedgeDocError, match="Unexpected status 409"):
        client.create_note("# Replacement", alias="existing-alias")

    assert client.read_note("existing-alias") == "# Original"


def _mock_realtime(mocker, *, document="A", revision=4, clients=None, maximum=100000):
    realtime = mocker.Mock()
    realtime.document = document
    realtime.revision = revision
    realtime.clients = clients or {}
    realtime.doc_state = {"str": document, "revision": revision, "clients": realtime.clients}
    realtime.refresh_state = {"docmaxlength": maximum, "permission": "private"}
    realtime.replace_document.return_value = RealtimeUpdateResult(revision, revision + 1)
    realtime.update_progress.return_value = RealtimeUpdateResult(revision, revision)
    realtime_class = mocker.patch("hedgedoc_mcp.client.HedgeDocRealtimeSession")
    realtime_class.return_value.__enter__.return_value = realtime
    return realtime, realtime_class


def test_update_note_replaces_and_verifies_content(client, mocker):
    realtime, _class = _mock_realtime(mocker)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="B"))

    result = client.update_note("abc", "B")

    realtime.replace_document.assert_called_once_with("B")
    assert result.updated is True
    assert result.no_op is False
    assert result.operation_acknowledged is True
    assert result.check_received is True
    assert result.content_verified is True
    assert result.persistence_verified is True
    assert result.revision_before == 4
    assert result.revision_after == 5
    assert result.condition_matched is None
    assert result.expected_previous_content_sha256 is None


def test_read_note_with_fingerprint_preserves_string_read_contract(client, mocker):
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="# Content"))

    assert client.read_note("abc") == "# Content"
    read_result = client.read_note_with_fingerprint("abc")

    assert read_result.content == "# Content"
    assert read_result.content_sha256 == content_sha256("# Content")


def test_update_note_refuses_stale_content_fingerprint_before_submission(client, mocker):
    realtime, _class = _mock_realtime(mocker, document="newer", revision=8)

    with pytest.raises(NoteUpdateError, match="fingerprint did not match") as raised:
        client.update_note(
            "abc",
            "replacement",
            expected_content_sha256=content_sha256("older"),
        )

    realtime.replace_document.assert_not_called()
    details = raised.value.recovery_details()
    assert details["operation_submitted"] is False
    assert details["expected_previous_content_sha256"] == content_sha256("older")
    assert details["actual_content_sha256"] == content_sha256("newer")
    assert details["condition_matched"] is False


def test_update_note_rejects_invalid_content_fingerprint_before_connecting(client, mocker):
    realtime_class = mocker.patch("hedgedoc_mcp.client.HedgeDocRealtimeSession")

    with pytest.raises(HedgeDocError, match="64-character"):
        client.update_note("abc", "replacement", expected_content_sha256="not-a-fingerprint")

    realtime_class.assert_not_called()


def test_update_note_allows_matching_content_fingerprint(client, mocker):
    realtime, _class = _mock_realtime(mocker, document="current")
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="replacement"))

    result = client.update_note(
        "abc",
        "replacement",
        expected_content_sha256=content_sha256("current"),
    )

    realtime.replace_document.assert_called_once_with("replacement")
    assert result.updated is True
    assert result.condition_matched is True
    assert result.expected_previous_content_sha256 == content_sha256("current")


@pytest.mark.parametrize(("current", "replacement"), [("", "B"), ("A", "")])
def test_update_note_handles_empty_documents(client, mocker, current, replacement):
    realtime, _class = _mock_realtime(mocker, document=current)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text=replacement))

    result = client.update_note("abc", replacement)

    realtime.replace_document.assert_called_once_with(replacement)
    assert result.updated is True


def test_update_note_identical_content_is_verified_noop(client, mocker):
    realtime, _class = _mock_realtime(mocker, document="same", revision=9)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="same"))

    result = client.update_note("abc", "same")

    realtime.replace_document.assert_not_called()
    assert result.updated is False
    assert result.no_op is True
    assert result.operation_submitted is False
    assert result.content_verified is True
    assert result.persistence_verified is True
    assert result.revision_before == result.revision_after == 9


def test_conditional_update_identical_content_is_verified_noop(client, mocker):
    realtime, _class = _mock_realtime(mocker, document="same", revision=9)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="same"))
    fingerprint = content_sha256("same")

    result = client.update_note("abc", "same", expected_content_sha256=fingerprint)

    realtime.replace_document.assert_not_called()
    assert result.no_op is True
    assert result.content_verified is True
    assert result.persistence_verified is True
    assert result.condition_matched is True
    assert result.expected_previous_content_sha256 == fingerprint


def test_update_note_readback_mismatch_is_machine_readable(client, mocker):
    _realtime, _class = _mock_realtime(mocker)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="not B"))

    with pytest.raises(NoteUpdateError, match="did not match") as raised:
        client.update_note("abc", "B")

    details = raised.value.recovery_details()
    assert details["operation_acknowledged"] is True
    assert details["check_received"] is True
    assert details["content_verified"] is False
    assert details["persistence_verified"] is False
    assert details["expected_content_sha256"] == content_sha256("B")
    assert details["actual_content_sha256"] == content_sha256("not B")
    assert "B" not in details.values()


def test_update_note_reports_initial_editor_conflict_without_submission(client, mocker):
    realtime, _class = _mock_realtime(mocker)
    protocol = RealtimeUpdateResult(
        4,
        4,
        operation_submitted=False,
        operation_acknowledged=False,
        concurrency_detected=True,
        check_received=False,
    )
    realtime.replace_document.side_effect = RealtimeConflictError("editor connected", protocol)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="A"))

    with pytest.raises(NoteUpdateError) as raised:
        client.update_note("abc", "B")

    details = raised.value.recovery_details()
    assert details["operation_submitted"] is False
    assert details["concurrency_detected"] is True


def test_update_note_reports_post_submission_conflict_and_final_hash(client, mocker):
    realtime, _class = _mock_realtime(mocker)
    protocol = RealtimeUpdateResult(4, 6, concurrency_detected=True)
    realtime.replace_document.side_effect = RealtimeConflictError(
        "may already have transformed and applied", protocol
    )
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="merged"))

    with pytest.raises(NoteUpdateError, match="transformed") as raised:
        client.update_note("abc", "B", expected_content_sha256=content_sha256("A"))

    details = raised.value.recovery_details()
    assert details["operation_submitted"] is True
    assert details["operation_acknowledged"] is True
    assert details["concurrency_detected"] is True
    assert details["revision_after"] == 6
    assert details["actual_content_sha256"] == content_sha256("merged")
    assert details["condition_matched"] is True
    assert details["expected_previous_content_sha256"] == content_sha256("A")


def test_update_note_conflict_reports_matching_persisted_content(client, mocker):
    realtime, _class = _mock_realtime(mocker)
    protocol = RealtimeUpdateResult(4, 6, concurrency_detected=True)
    realtime.replace_document.side_effect = RealtimeConflictError("conflict", protocol)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="B"))

    with pytest.raises(NoteUpdateError) as raised:
        client.update_note("abc", "B")

    details = raised.value.recovery_details()
    assert details["updated"] is False
    assert details["concurrency_detected"] is True
    assert details["content_verified"] is True
    assert details["persistence_verified"] is True


def test_update_note_reports_authorization_ack_timeout(client, mocker):
    realtime, _class = _mock_realtime(mocker)
    realtime.replace_document.side_effect = RealtimeTimeoutError(
        "Timed out waiting for the text-operation acknowledgement."
    )
    realtime.update_progress.return_value = RealtimeUpdateResult(
        4,
        4,
        operation_submitted=True,
        operation_acknowledged=False,
        check_received=False,
    )
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="A"))

    with pytest.raises(NoteUpdateError, match="acknowledgement") as raised:
        client.update_note("abc", "B", expected_content_sha256=content_sha256("A"))

    assert raised.value.result.operation_submitted is True
    assert raised.value.result.operation_acknowledged is False
    assert raised.value.result.condition_matched is True
    assert raised.value.result.expected_previous_content_sha256 == content_sha256("A")


def test_update_note_rejects_document_over_limit_before_submission(client, mocker):
    realtime, _class = _mock_realtime(mocker, maximum=2)

    with pytest.raises(NoteUpdateError, match="document limit") as raised:
        client.update_note("abc", "😀x")

    realtime.replace_document.assert_not_called()
    assert raised.value.result.operation_submitted is False


def test_update_note_allows_oversized_document_to_shrink(client, mocker):
    realtime, _class = _mock_realtime(mocker, document="oversized", maximum=2)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="less"))

    result = client.update_note("abc", "less")

    realtime.replace_document.assert_called_once_with("less")
    assert result.updated is True


def test_update_note_context_cleans_up_after_success_and_failure(client, mocker):
    realtime, realtime_class = _mock_realtime(mocker)
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(text="B"))
    client.update_note("abc", "B")
    realtime_class.return_value.__exit__.assert_called_once()

    realtime.replace_document.side_effect = RealtimeTimeoutError("no ack")
    realtime.update_progress.return_value = RealtimeUpdateResult(4, 4)
    with pytest.raises(NoteUpdateError):
        client.update_note("abc", "C")
    assert realtime_class.return_value.__exit__.call_count == 2


def test_create_note_without_permission_preserves_server_default(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/abc"}),
    )
    set_permission = mocker.patch.object(client, "set_permission")

    result = client.create_note("# Hello")

    set_permission.assert_not_called()
    assert result.permission is None
    assert result.permission_verified is False


def test_create_note_with_permission_applies_it(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/abc"}),
    )
    set_permission = mocker.patch.object(client, "set_permission", return_value="protected")

    result = client.create_note("# Handoff", permission="protected")

    assert result.note_id == "abc"
    assert result.permission == "protected"
    assert result.permission_verified is True
    set_permission.assert_called_once_with("abc", "protected")


def test_create_alias_note_with_permission_applies_it(client, mocker):
    post = mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/handoff"}),
    )
    set_permission = mocker.patch.object(client, "set_permission", return_value="protected")

    result = client.create_note("# Handoff", alias="handoff", permission="protected")

    assert post.call_args.args[0] == "https://md.example.com/new/handoff"
    assert result.note_id == "handoff"
    assert result.permission_verified is True
    set_permission.assert_called_once_with("handoff", "protected")


def test_create_note_rejects_invalid_permission_before_creation(client, mocker):
    post = mocker.patch.object(client._session, "post")

    with pytest.raises(HedgeDocError, match="Invalid permission 'public'"):
        client.create_note("# Handoff", permission="public")

    post.assert_not_called()


def test_create_note_reports_url_when_permission_change_fails(client, mocker):
    mocker.patch.object(
        client._session,
        "post",
        return_value=_FakeResponse(status_code=302, headers={"Location": "/abc"}),
    )
    mocker.patch.object(client, "set_permission", side_effect=HedgeDocError("denied"))

    with pytest.raises(
        PermissionChangeError, match=r"created at https://md.example.com/abc"
    ) as raised:
        client.create_note("# Handoff", permission="protected")

    assert raised.value.recovery_details() == {
        "created": True,
        "note_id": "abc",
        "url": "https://md.example.com/abc",
        "requested_permission": "protected",
        "permission_verified": False,
        "error": str(raised.value),
    }


@pytest.mark.parametrize(
    "permission", ["freely", "editable", "limited", "locked", "protected", "private"]
)
def test_set_permission_accepts_hedgedoc_values(client, mocker, permission):
    realtime_class = mocker.patch("hedgedoc_mcp.client.HedgeDocRealtimeSession")

    assert client.set_permission("abc", permission) == permission
    realtime_class.return_value.__enter__.return_value.set_permission.assert_called_once_with(
        permission
    )


def test_set_permission_rejects_invalid_value_without_connecting(client, mocker):
    realtime_class = mocker.patch("hedgedoc_mcp.client.HedgeDocRealtimeSession")

    with pytest.raises(HedgeDocError, match="Invalid permission"):
        client.set_permission("abc", "public")

    realtime_class.assert_not_called()


def test_read_note_success(client, mocker):
    mocker.patch.object(
        client._session, "get", return_value=_FakeResponse(status_code=200, text="# Content")
    )
    assert client.read_note("abc") == "# Content"


def test_read_note_not_found_raises(client, mocker):
    mocker.patch.object(client._session, "get", return_value=_FakeResponse(status_code=404))
    with pytest.raises(HedgeDocError, match="not found"):
        client.read_note("missing")


def test_read_note_html_forbidden_page_triggers_session_recovery(client, mocker):
    mocker.patch.object(
        client._session,
        "get",
        side_effect=[
            _FakeResponse(
                status_code=200,
                text="<!DOCTYPE html><html><body>Sign In</body></html>",
                headers={"Content-Type": "text/html"},
            ),
            _FakeResponse(json_data={"status": "forbidden"}),
        ],
    )
    with pytest.raises(SessionExpiredError):
        client.read_note("abc")


def test_read_note_html_with_valid_auth_is_protocol_error(client, mocker):
    mocker.patch.object(
        client._session,
        "get",
        side_effect=[
            _FakeResponse(status_code=200, text="<html>unexpected</html>"),
            _FakeResponse(json_data={"status": "ok", "name": "agent-home"}),
        ],
    )
    with pytest.raises(HedgeDocError, match="unexpected HTML"):
        client.read_note("abc")


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
