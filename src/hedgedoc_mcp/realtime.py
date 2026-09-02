"""Reusable Socket.IO transport for a joined HedgeDoc 1.x note.

This module deliberately implements note connection and metadata events only.
It does not implement HedgeDoc's Operational Transform protocol.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
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
        self._ready = threading.Event()
        self._permission_broadcast = threading.Event()
        self._refresh_confirmation = threading.Event()
        self._error: RealtimeError | None = None
        self._expected_permission: str | None = None
        self._confirming_refresh = False
        self.refresh_state: dict[str, Any] | None = None
        self.client = client_factory(http_session=http_session, reconnection=False)
        self._register_handlers()

    @property
    def current_permission(self) -> str | None:
        if not self.refresh_state:
            return None
        permission = self.refresh_state.get("permission")
        return permission if isinstance(permission, str) else None

    def _register_handlers(self) -> None:
        @self.client.on("refresh")
        def on_refresh(data: Any) -> None:
            if not isinstance(data, dict):
                return
            self.refresh_state = data
            self._ready.set()
            if self._confirming_refresh and data.get("permission") == self._expected_permission:
                self._refresh_confirmation.set()

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
            self._ready.set()
            self._permission_broadcast.set()
            self._refresh_confirmation.set()

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

        self._wait(self._ready, f"joining note '{self.note_id}'")

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
