"""Tests for MCP-facing error behaviour."""

from __future__ import annotations

import asyncio
import json

from hedgedoc_mcp import server
from hedgedoc_mcp.client import (
    NoteResult,
    NoteUpdateError,
    NoteUpdateResult,
    PermissionChangeError,
    SessionExpiredError,
)


def _update_result(**overrides):
    values = {
        "note_id": "existing-alias",
        "updated": True,
        "no_op": False,
        "operation_submitted": True,
        "operation_acknowledged": True,
        "concurrency_detected": False,
        "check_received": True,
        "content_verified": True,
        "persistence_verified": True,
        "revision_before": 2,
        "revision_after": 3,
        "expected_content_sha256": "expected",
        "actual_content_sha256": "actual",
    }
    values.update(overrides)
    return NoteUpdateResult(**values)


def test_update_tool_returns_verified_result(mocker):
    client = mocker.Mock()
    client.update_note.return_value = _update_result()

    result = json.loads(
        server._dispatch(
            client,
            "hedgedoc_update_note",
            {"note_id": "existing-alias", "content": "# Replacement"},
        )
    )

    assert result["updated"] is True
    assert result["content_verified"] is True
    assert result["revision_after"] == 3


def test_update_tool_forwards_optional_content_fingerprint(mocker):
    client = mocker.Mock()
    client.update_note.return_value = _update_result()
    fingerprint = "a" * 64

    server._dispatch(
        client,
        "hedgedoc_update_note",
        {
            "note_id": "existing-alias",
            "content": "# Replacement",
            "expected_content_sha256": fingerprint,
        },
    )

    client.update_note.assert_called_once_with(
        "existing-alias", "# Replacement", expected_content_sha256=fingerprint
    )


def test_fingerprint_read_tool_returns_content_and_fingerprint(mocker):
    client = mocker.Mock()
    client.read_note_with_fingerprint.return_value.as_dict.return_value = {
        "content": "# Existing",
        "content_sha256": "a" * 64,
    }

    result = json.loads(
        server._dispatch(client, "hedgedoc_read_note_with_fingerprint", {"note_id": "abc"})
    )

    assert result == {"content": "# Existing", "content_sha256": "a" * 64}
    client.read_note.assert_not_called()


def test_update_tool_description_documents_bounded_conflict_behavior():
    tools = asyncio.run(server.list_tools())
    update_tool = next(tool for tool in tools if tool.name == "hedgedoc_update_note")

    assert "bounded" in update_tool.description
    assert "structured conflict" in update_tool.description
    assert "re-read the note" in update_tool.description
    assert "never retries" in update_tool.description
    assert "expected_content_sha256" in update_tool.description


def test_fingerprint_read_tool_schema_documents_conditional_update():
    tools = asyncio.run(server.list_tools())
    read_tool = next(tool for tool in tools if tool.name == "hedgedoc_read_note_with_fingerprint")
    update_tool = next(tool for tool in tools if tool.name == "hedgedoc_update_note")

    assert "content_sha256" in read_tool.description
    fingerprint_schema = update_tool.inputSchema["properties"]["expected_content_sha256"]
    assert fingerprint_schema["pattern"] == "^[0-9a-f]{64}$"


def test_update_failure_is_machine_readable(mocker):
    client = mocker.Mock()
    client.update_note.side_effect = NoteUpdateError(
        "Concurrent editing was detected; the operation may already be applied.",
        _update_result(updated=False, concurrency_detected=True, content_verified=False),
    )
    mocker.patch("hedgedoc_mcp.server._get_client", return_value=client)

    async def run_synchronously(function, *args):
        return function(*args)

    mocker.patch("hedgedoc_mcp.server.asyncio.to_thread", side_effect=run_synchronously)

    response = asyncio.run(
        server.call_tool(
            "hedgedoc_update_note",
            {"note_id": "existing-alias", "content": "# Replacement"},
        )
    )
    result = json.loads(response[0].text)

    assert result["updated"] is False
    assert result["concurrency_detected"] is True
    assert "may already be applied" in result["error"]


def test_session_expiry_never_retries_a_conditional_update(mocker):
    client = mocker.Mock()
    client.update_note.side_effect = SessionExpiredError("session expired after submission")
    get_client = mocker.patch("hedgedoc_mcp.server._get_client", return_value=client)
    reset_client = mocker.patch("hedgedoc_mcp.server._reset_client")

    async def run_synchronously(function, *args):
        return function(*args)

    mocker.patch("hedgedoc_mcp.server.asyncio.to_thread", side_effect=run_synchronously)
    response = asyncio.run(
        server.call_tool(
            "hedgedoc_update_note",
            {"note_id": "existing", "content": "# Replacement", "expected_content_sha256": "a" * 64},
        )
    )

    assert response[0].text == "Error: session expired after submission"
    client.update_note.assert_called_once()
    get_client.assert_called_once()
    reset_client.assert_not_called()


def test_create_tool_returns_explicit_permission_verification(mocker):
    client = mocker.Mock()
    client.create_note.return_value = NoteResult(
        note_id="abc",
        url="https://md.example.com/abc",
        permission="protected",
        permission_verified=True,
    )

    result = json.loads(
        server._dispatch(
            client,
            "hedgedoc_create_note",
            {"content": "# Handoff", "permission": "protected"},
        )
    )

    assert result == {
        "note_id": "abc",
        "url": "https://md.example.com/abc",
        "permission": "protected",
        "permission_verified": True,
    }


def test_create_tool_without_permission_does_not_guess_default(mocker):
    client = mocker.Mock()
    client.create_note.return_value = NoteResult(note_id="abc", url="https://md.example.com/abc")

    result = json.loads(
        server._dispatch(client, "hedgedoc_create_note", {"content": "# Private by default"})
    )

    assert result["permission"] is None
    assert result["permission_verified"] is False


def test_create_partial_failure_is_machine_readable(mocker):
    note = NoteResult(note_id="abc", url="https://md.example.com/abc")
    client = mocker.Mock()
    client.create_note.side_effect = PermissionChangeError(
        "permission mutation timed out",
        note=note,
        requested_permission="protected",
    )
    mocker.patch("hedgedoc_mcp.server._get_client", return_value=client)

    async def run_synchronously(function, *args):
        return function(*args)

    mocker.patch("hedgedoc_mcp.server.asyncio.to_thread", side_effect=run_synchronously)

    response = asyncio.run(
        server.call_tool(
            "hedgedoc_create_note",
            {"content": "# Handoff", "permission": "protected"},
        )
    )
    result = json.loads(response[0].text)

    assert result == {
        "created": True,
        "note_id": "abc",
        "url": "https://md.example.com/abc",
        "requested_permission": "protected",
        "permission_verified": False,
        "error": "permission mutation timed out",
    }


def test_set_permission_tool_reports_verified_success(mocker):
    client = mocker.Mock()
    client.set_permission.return_value = "protected"

    result = json.loads(
        server._dispatch(
            client,
            "hedgedoc_set_permission",
            {"note_id": "abc", "permission": "protected"},
        )
    )

    assert result == {
        "note_id": "abc",
        "permission": "protected",
        "permission_verified": True,
    }
