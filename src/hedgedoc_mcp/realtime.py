"""Reusable Socket.IO transport for a joined HedgeDoc 1.x note.

This implements metadata events plus one bounded text replacement. It is not a
continuous collaborative editor or a generic Operational Transform client.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import requests
import socketio


class RealtimeError(RuntimeError):
    """Base error raised by the HedgeDoc realtime transport."""


class RealtimeAuthenticationError(RealtimeError):
    """The HTTP session could not authenticate the Socket.IO connection."""


class RealtimeTimeoutError(RealtimeError):
    """HedgeDoc did not emit the expected event before the timeout."""


class RealtimeProtocolError(RealtimeError):
    """HedgeDoc emitted malformed or incomplete realtime state."""


class RealtimeConflictError(RealtimeError):
    """A replacement overlapped another realtime operation."""

    def __init__(self, message: str, result: RealtimeUpdateResult):
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class RealtimeUpdateResult:
    """Protocol outcome for one submitted replacement operation."""

    revision_before: int
    revision_after: int
    operation_submitted: bool = True
    operation_acknowledged: bool = True
    concurrency_detected: bool = False
    check_received: bool = True


def utf16_length(value: str) -> int:
    """Return JavaScript string length, measured in UTF-16 code units."""
    return len(value.encode("utf-16-le")) // 2


def build_replacement_operation(current: str, replacement: str) -> list[int | str]:
    """Build HedgeDoc's canonical insert/delete operation for full replacement."""
    operation: list[int | str] = []
    if replacement:
        operation.append(replacement)
    current_length = utf16_length(current)
    if current_length:
        operation.append(-current_length)
    return operation


class HedgeDocRealtimeSession:
    """A short-lived authenticated Socket.IO connection joined to one note."""

    def __init__(
        self,
        base_url: str,
        note_id: str,
        http_session: requests.Session,
        session_cookie_name: str,
        timeout: float,
        client_factory: Callable[..., Any] = socketio.Client,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.note_id = note_id
        self.http_session = http_session
        self.session_cookie_name = session_cookie_name
        self.timeout = timeout
        self._doc_ready = threading.Event()
        self._refresh_ready = threading.Event()
        self._permission_broadcast = threading.Event()
        self._refresh_confirmation = threading.Event()
        self._operation_ack = threading.Event()
        self._peer_operation = threading.Event()
        self._post_update_check = threading.Event()
        self._error: RealtimeError | None = None
        self._expected_permission: str | None = None
        self._confirming_refresh = False
        self._waiting_for_update = False
        self._operation_submitted = False
        self._update_attempted = False
        self._update_revision_before: int | None = None
        self._ack_revision: int | None = None
        self.doc_state: dict[str, Any] | None = None
        self.refresh_state: dict[str, Any] | None = None
        self.client = client_factory(http_session=http_session, reconnection=False)
        self._register_handlers()

    @property
    def current_permission(self) -> str | None:
        if not self.refresh_state:
            return None
        permission = self.refresh_state.get("permission")
        return permission if isinstance(permission, str) else None

    @property
    def document(self) -> str:
        if not self.doc_state:
            raise RealtimeProtocolError("HedgeDoc did not provide initial document state.")
        return self.doc_state["str"]

    @property
    def revision(self) -> int:
        if not self.doc_state:
            raise RealtimeProtocolError("HedgeDoc did not provide an initial OT revision.")
        return self.doc_state["revision"]

    @property
    def clients(self) -> dict[str, Any]:
        if not self.doc_state:
            raise RealtimeProtocolError("HedgeDoc did not provide the connected OT clients.")
        return self.doc_state["clients"]

    def update_progress(self) -> RealtimeUpdateResult:
        """Describe the current one-shot update attempt after success or failure."""
        revision_before = (
            self._update_revision_before
            if self._update_revision_before is not None
            else self.revision
        )
        acknowledged = self._ack_revision is not None
        return RealtimeUpdateResult(
            revision_before=revision_before,
            revision_after=self._ack_revision if acknowledged else revision_before,
            operation_submitted=self._operation_submitted,
            operation_acknowledged=acknowledged,
            concurrency_detected=self._peer_operation.is_set()
            or (acknowledged and self._ack_revision != revision_before + 1),
            check_received=self._post_update_check.is_set(),
        )

    def _register_handlers(self) -> None:
        @self.client.on("doc")
        def on_doc(data: Any) -> None:
            if not (
                isinstance(data, dict)
                and isinstance(data.get("str"), str)
                and isinstance(data.get("revision"), int)
                and not isinstance(data.get("revision"), bool)
                and data["revision"] >= 0
                and isinstance(data.get("clients"), dict)
            ):
                self._error = RealtimeProtocolError(
                    "HedgeDoc emitted malformed initial document state."
                )
            else:
                self.doc_state = data
            self._doc_ready.set()

        @self.client.on("refresh")
        def on_refresh(data: Any) -> None:
            if not isinstance(data, dict):
                self._error = RealtimeProtocolError("HedgeDoc emitted malformed refresh state.")
                self._refresh_ready.set()
                return
            self.refresh_state = data
            self._refresh_ready.set()
            if self._confirming_refresh and data.get("permission") == self._expected_permission:
                self._refresh_confirmation.set()

        @self.client.on("ack")
        def on_ack(revision: Any) -> None:
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
                self._error = RealtimeProtocolError(
                    "HedgeDoc emitted a malformed OT acknowledgement."
                )
            else:
                self._ack_revision = revision
            self._operation_ack.set()

        @self.client.on("operation")
        def on_operation(*_args: Any) -> None:
            if self._waiting_for_update:
                self._peer_operation.set()

        @self.client.on("check")
        def on_check(_data: Any) -> None:
            if self._waiting_for_update and self._operation_ack.is_set():
                self._post_update_check.set()

        @self.client.on("permission")
        def on_permission(data: Any) -> None:
            if isinstance(data, dict) and data.get("permission") == self._expected_permission:
                self._permission_broadcast.set()

        @self.client.on("info")
        def on_info(data: Any) -> None:
            code = data.get("code") if isinstance(data, dict) else None
            self._error = RealtimeError(
                f"HedgeDoc realtime operation failed (HTTP {code or 'unknown'})."
            )
            self._doc_ready.set()
            self._refresh_ready.set()
            self._permission_broadcast.set()
            self._refresh_confirmation.set()
            self._operation_ack.set()
            self._post_update_check.set()

    def connect(self) -> None:
        cookie = self.http_session.cookies.get(self.session_cookie_name)
        if not cookie:
            raise RealtimeAuthenticationError(
                "Session cookie is missing. Realtime operations require authentication."
            )

        try:
            self.client.connect(
                f"{self.base_url}?noteId={quote(self.note_id, safe='')}",
                socketio_path=self.socketio_path,
                headers={"Cookie": f"{self.session_cookie_name}={cookie}"},
                wait_timeout=self.timeout,
            )
        except socketio.exceptions.ConnectionError as exc:
            if "AUTH failed" in str(exc):
                raise RealtimeAuthenticationError(
                    "Session cookie is expired, invalid, or was rejected by HedgeDoc."
                ) from exc
            raise RealtimeError(f"Could not connect to HedgeDoc realtime service: {exc}") from exc

        self._wait(self._doc_ready, f"receiving document state for note '{self.note_id}'")
        self._wait(self._refresh_ready, f"receiving metadata for note '{self.note_id}'")

    @property
    def socketio_path(self) -> str:
        base_path = urlsplit(self.base_url).path.strip("/")
        return f"{base_path}/socket.io" if base_path else "socket.io"

    def set_permission(self, permission: str) -> None:
        """Mutate permission and confirm broadcast plus requested refresh state."""
        self._expected_permission = permission
        self._permission_broadcast.clear()
        self._refresh_confirmation.clear()
        self._confirming_refresh = False

        self.client.emit("permission", permission)
        self._wait(
            self._permission_broadcast,
            f"the permission '{permission}' database-update broadcast",
        )

        self._confirming_refresh = True
        self.client.emit("refresh")
        self._wait(
            self._refresh_confirmation,
            f"refresh confirmation of permission '{permission}'",
        )

    def replace_document(self, replacement: str) -> RealtimeUpdateResult:
        """Submit exactly one full-document operation and await ack plus DB-update signal."""
        if self._update_attempted:
            raise RealtimeProtocolError(
                "This realtime session already attempted a replacement; reconnect before retrying."
            )
        self._update_attempted = True
        current = self.document
        revision_before = self.revision
        own_client_id = getattr(self.client, "sid", None)
        other_clients = [client_id for client_id in self.clients if client_id != own_client_id]
        if other_clients:
            raise RealtimeConflictError(
                "Replacement refused because another OT client was already connected.",
                RealtimeUpdateResult(
                    revision_before=revision_before,
                    revision_after=revision_before,
                    operation_submitted=False,
                    operation_acknowledged=False,
                    concurrency_detected=True,
                    check_received=False,
                ),
            )

        operation = build_replacement_operation(current, replacement)
        if not operation:
            raise RealtimeProtocolError("A no-op replacement must not be submitted to HedgeDoc.")

        self._operation_ack.clear()
        self._peer_operation.clear()
        self._post_update_check.clear()
        self._ack_revision = None
        self._operation_submitted = False
        self._update_revision_before = revision_before
        self._waiting_for_update = True
        try:
            self._operation_submitted = True
            self.client.emit("operation", (revision_before, operation, None))
            self._wait(self._operation_ack, "the text-operation acknowledgement")
            revision_after = self._ack_revision
            if revision_after is None:
                raise RealtimeProtocolError("HedgeDoc acknowledged without an OT revision.")
            self._wait(self._post_update_check, "the post-update persistence signal")

            concurrency_detected = (
                self._peer_operation.is_set() or revision_after != revision_before + 1
            )
            result = RealtimeUpdateResult(
                revision_before=revision_before,
                revision_after=revision_after,
                concurrency_detected=concurrency_detected,
            )
            if concurrency_detected:
                raise RealtimeConflictError(
                    "Concurrent editing was detected after the replacement was submitted; "
                    "HedgeDoc may already have transformed and applied the operation.",
                    result,
                )
            return result
        finally:
            self._waiting_for_update = False

    def _wait(self, event: threading.Event, description: str) -> None:
        if not event.wait(self.timeout):
            raise RealtimeTimeoutError(f"Timed out waiting for {description}.")
        if self._error:
            raise self._error

    def close(self) -> None:
        if self.client.connected:
            self.client.disconnect()

    def __enter__(self) -> HedgeDocRealtimeSession:
        try:
            self.connect()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
