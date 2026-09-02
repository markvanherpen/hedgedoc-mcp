"""HedgeDoc 1.x HTTP client.

HedgeDoc 1.x (the widely self-hosted version, as opposed to newer forks)
has NO API token system. Every write endpoint requires an authenticated
session, identified by the `connect.sid` cookie that Express issues on
login. This module wraps that reality behind a clean interface so callers
(the MCP server, the CLI, tests) never touch raw cookies directly.

Endpoints used (from HedgeDoc's own OpenAPI spec, version 1.11.1):
    POST /login                  -- email+password login, sets connect.sid
    POST /new                    -- create a note, body = raw markdown
    POST /new/{alias}            -- create a note with a custom slug (needs FreeURL)
    GET  /{note}/download        -- raw markdown content (public, no auth needed)
    GET  /{note}/info            -- title, created/updated, viewcount (public)
    GET  /{note}/revision        -- list of revisions
    GET  /me                     -- verify session / get user info
    GET  /history                -- recently viewed/pinned notes (auth required)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from urllib.parse import quote, unquote

import requests
import socketio

PERMISSION_VALUES = frozenset({"freely", "editable", "limited", "locked", "protected", "private"})


class HedgeDocError(RuntimeError):
    """Raised for any HedgeDoc API failure (auth, network, not-found, etc.)."""


class SessionExpiredError(HedgeDocError):
    """Raised specifically when the stored session cookie is no longer valid.

    Callers should catch this and trigger a re-login (via `login()`) rather
    than treating it as a generic failure.
    """


class PermissionChangeError(HedgeDocError):
    """Raised when a note exists but its requested permission was not applied."""

    def __init__(self, message: str, note: NoteResult | None = None):
        super().__init__(message)
        self.note = note


@dataclass
class NoteInfo:
    title: str
    description: str | None
    viewcount: int
    createtime: str
    updatetime: str


@dataclass
class NoteResult:
    note_id: str
    url: str


class HedgeDocClient:
    """Thin wrapper around a HedgeDoc 1.x instance.

    Usage:
        client = HedgeDocClient(base_url="https://md.example.com")
        client.set_session_cookie(cookie_value)          # from a prior login
        # ...or...
        client.login(email="you@example.com", password="secret")

        result = client.create_note("# Hello\\n\\nWorld")
        content = client.read_note(result.note_id)
    """

    def __init__(self, base_url: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    # -- Auth -----------------------------------------------------------

    def login(self, email: str, password: str) -> str:
        """Log in with email/password. Returns the raw (decoded) session cookie.

        Raises HedgeDocError if the instance has no local email/password
        auth enabled, or the credentials are wrong.
        """
        resp = self._session.post(
            f"{self.base_url}/login",
            data={"email": email, "password": password},
            allow_redirects=False,
            timeout=self.timeout,
        )
        cookie = self._session.cookies.get("connect.sid")
        if not cookie:
            raise HedgeDocError(
                f"Login failed (HTTP {resp.status_code}). Check that email/password "
                "auth is enabled on this HedgeDoc instance (CMD_EMAIL=true) and that "
                "the credentials are correct."
            )
        return cookie

    def set_session_cookie(self, cookie_value: str) -> None:
        """Install a previously-obtained session cookie.

        Accepts either the raw value or a URL-encoded value (as captured
        from a browser's cookie jar or `cookies.txt`) -- it is unquoted
        automatically if needed.
        """
        value = unquote(cookie_value)
        domain = self.base_url.split("//", 1)[-1].split("/", 1)[0]
        self._session.cookies.set("connect.sid", value, domain=domain)

    def get_session_cookie(self, encoded: bool = True) -> str | None:
        """Return the currently installed session cookie value."""
        raw = self._session.cookies.get("connect.sid")
        if raw is None:
            return None
        return quote(raw, safe="") if encoded else raw

    def whoami(self) -> dict:
        """Verify the session and return the logged-in user's profile.

        Raises SessionExpiredError if not authenticated.
        """
        resp = self._session.get(f"{self.base_url}/me", timeout=self.timeout)
        data = resp.json()
        if data.get("status") == "forbidden":
            raise SessionExpiredError(
                "Session cookie is missing, expired, or invalid. Call login() again."
            )
        return data

    # -- Notes ------------------------------------------------------------

    def create_note(
        self, content: str, alias: str | None = None, permission: str | None = None
    ) -> NoteResult:
        """Create a new note. Returns its ID and full URL.

        If the instance requires login for note creation (the common case)
        and the session is invalid, raises SessionExpiredError.
        """
        path = f"/new/{alias}" if alias else "/new"
        resp = self._session.post(
            f"{self.base_url}{path}",
            data=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown"},
            allow_redirects=False,
            timeout=self.timeout,
        )
        if resp.status_code != 302:
            raise HedgeDocError(f"Unexpected status {resp.status_code} creating note.")

        location = resp.headers.get("Location", "")
        if location in ("", "/", self.base_url, f"{self.base_url}/"):
            raise SessionExpiredError(
                "Note creation redirected to the home page instead of a new note. "
                "This usually means the session is expired or anonymous note "
                "creation is disabled on this instance. Call login() again."
            )

        note_url = location if location.startswith("http") else f"{self.base_url}{location}"
        note_id = note_url.rstrip("/").rsplit("/", 1)[-1]
        result = NoteResult(note_id=note_id, url=note_url)
        if permission is not None:
            try:
                self.set_permission(note_id, permission)
            except HedgeDocError as exc:
                raise PermissionChangeError(
                    f"Note was created at {note_url}, but permission '{permission}' "
                    f"was not confirmed. The note may still have its server-default "
                    f"permission; inspect it before sharing. {exc}",
                    note=result,
                ) from exc
        return result

    def get_permission(self, note_id: str) -> str:
        """Read a note's persisted permission through HedgeDoc's realtime state."""
        return self._realtime_permission(note_id)

    def set_permission(self, note_id: str, permission: str) -> str:
        """Set and persist a note permission through HedgeDoc's realtime channel.

        HedgeDoc accepts this operation only from the note owner. Its server does
        not acknowledge denials or database errors, so success is recognized from
        the post-update broadcast and then verified using a fresh connection.
        """
        if permission not in PERMISSION_VALUES:
            allowed = ", ".join(sorted(PERMISSION_VALUES))
            raise HedgeDocError(f"Invalid permission '{permission}'. Allowed values: {allowed}.")

        observed = self._realtime_permission(note_id, requested=permission)
        if observed != permission:
            raise PermissionChangeError(
                f"HedgeDoc did not confirm permission '{permission}' for note '{note_id}'."
            )

        persisted = self._realtime_permission(note_id)
        if persisted != permission:
            raise PermissionChangeError(
                f"HedgeDoc reported permission '{observed}', but a fresh connection read "
                f"'{persisted}' for note '{note_id}'."
            )
        return persisted

    def _realtime_permission(self, note_id: str, requested: str | None = None) -> str:
        state: dict[str, str | None] = {"permission": None, "error": None}
        ready = threading.Event()
        changed = threading.Event()
        client = socketio.Client(http_session=self._session, reconnection=False)

        @client.on("refresh")
        def on_refresh(data):
            permission = data.get("permission") if isinstance(data, dict) else None
            if permission in PERMISSION_VALUES:
                state["permission"] = permission
                ready.set()

        @client.on("permission")
        def on_permission(data):
            permission = data.get("permission") if isinstance(data, dict) else None
            if permission in PERMISSION_VALUES:
                state["permission"] = permission
                changed.set()

        @client.on("info")
        def on_info(data):
            code = data.get("code") if isinstance(data, dict) else None
            state["error"] = f"HedgeDoc realtime connection failed (HTTP {code or 'unknown'})."
            ready.set()
            changed.set()

        try:
            client.connect(
                f"{self.base_url}?noteId={quote(note_id, safe='')}",
                socketio_path=self._socketio_path(),
                headers={"Cookie": self._session_cookie_header()},
                wait_timeout=self.timeout,
            )
            if not ready.wait(self.timeout):
                raise HedgeDocError(f"Timed out reading permission for note '{note_id}'.")
            if state["error"]:
                raise HedgeDocError(state["error"])

            if requested is not None:
                client.emit("permission", requested)
                if not changed.wait(self.timeout):
                    raise PermissionChangeError(
                        f"HedgeDoc did not confirm permission '{requested}' for note '{note_id}'; "
                        "the request may have been denied or failed."
                    )
                if state["error"]:
                    raise HedgeDocError(state["error"])
            return str(state["permission"])
        except socketio.exceptions.ConnectionError as exc:
            if "AUTH failed" in str(exc):
                raise SessionExpiredError(
                    "Session cookie is missing, expired, or invalid. Call login() again."
                ) from exc
            raise HedgeDocError(f"Could not connect to HedgeDoc realtime service: {exc}") from exc
        finally:
            if client.connected:
                client.disconnect()

    def _socketio_path(self) -> str:
        from urllib.parse import urlsplit

        url_path = urlsplit(self.base_url).path.strip("/")
        return f"{url_path}/socket.io" if url_path else "socket.io"

    def _session_cookie_header(self) -> str:
        cookie = self.get_session_cookie(encoded=False)
        if not cookie:
            raise SessionExpiredError(
                "Session cookie is missing. Permission operations require authentication."
            )
        return f"connect.sid={cookie}"

    def read_note(self, note_id: str) -> str:
        """Fetch the raw markdown content of a note. Public endpoint, no auth needed."""
        resp = self._session.get(f"{self.base_url}/{note_id}/download", timeout=self.timeout)
        if resp.status_code == 404:
            raise HedgeDocError(f"Note '{note_id}' not found.")
        resp.raise_for_status()
        return resp.text

    def update_note(self, note_id: str, content: str) -> None:
        """Overwrite a note's content.

        HedgeDoc 1.x has no REST PATCH/PUT endpoint for note content --
        real-time edits happen over the Socket.IO/realtime channel used by
        the web editor. As a practical workaround for automation, this
        recreates the note at the same alias when FreeURL mode is enabled
        (POST /new/{alias} overwrites an existing note at that alias).
        If the note was NOT created with a custom alias, this will fail --
        see docs/LIMITATIONS.md for the full explanation and alternatives.
        """
        resp = self._session.post(
            f"{self.base_url}/new/{note_id}",
            data=content.encode("utf-8"),
            headers={"Content-Type": "text/markdown"},
            allow_redirects=False,
            timeout=self.timeout,
        )
        if resp.status_code != 302:
            raise HedgeDocError(
                f"Could not update note '{note_id}' (HTTP {resp.status_code}). "
                "In-place updates via HTTP only work for notes created with a "
                "custom alias under FreeURL mode. See docs/LIMITATIONS.md."
            )
        location = resp.headers.get("Location", "")
        if location in ("", "/", self.base_url, f"{self.base_url}/"):
            raise SessionExpiredError(
                "Note update redirected to the home page instead of the note. "
                "This usually means the session is expired. Call login() again."
            )

    def note_info(self, note_id: str) -> NoteInfo:
        """Fetch metadata: title, description, viewcount, timestamps."""
        resp = self._session.get(f"{self.base_url}/{note_id}/info", timeout=self.timeout)
        if resp.status_code == 404:
            raise HedgeDocError(f"Note '{note_id}' not found.")
        resp.raise_for_status()
        data = resp.json()
        return NoteInfo(
            title=data.get("title", "Untitled"),
            description=data.get("description"),
            viewcount=data.get("viewcount", 0),
            createtime=data.get("createtime", ""),
            updatetime=data.get("updatetime", ""),
        )

    def history(self) -> list[dict]:
        """List the logged-in user's recently viewed/pinned notes. Requires auth."""
        resp = self._session.get(f"{self.base_url}/history", timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("status") == "forbidden":
            raise SessionExpiredError("Session expired. Call login() again.")
        return data.get("history", [])

    def status(self) -> dict:
        """Instance-wide stats (note count, online users, etc). No auth needed."""
        resp = self._session.get(f"{self.base_url}/status", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()
