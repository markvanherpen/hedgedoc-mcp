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


def test_update_tool_description_documents_bounded_conflict_behavior():
    tools = asyncio.run(server.list_tools())
    update_tool = next(tool for tool in tools if tool.name == "hedgedoc_update_note")

    assert "bounded" in update_tool.description
    assert "structured conflict" in update_tool.description
    assert "re-read the note" in update_tool.description
    assert "never retries" in update_tool.description


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
