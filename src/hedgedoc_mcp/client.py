"""HedgeDoc 1.x HTTP client.

HedgeDoc 1.x (the widely self-hosted version, as opposed to newer forks)
has NO API token system. Every write endpoint requires an authenticated
session, identified by the cookie name configured by the instance (normally
`connect.sid`) that Express issues on login. This module hides that detail
from callers (the MCP server, the CLI, and tests).

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

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote

import requests

from .realtime import (
    HedgeDocRealtimeSession,
    RealtimeAuthenticationError,
    RealtimeError,
)

PERMISSION_VALUES = frozenset({"freely", "editable", "limited", "locked", "protected", "private"})

DEFAULT_SESSION_COOKIE_NAME = "connect.sid"
_COOKIE_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class HedgeDocError(RuntimeError):
    """Raised for any HedgeDoc API failure (auth, network, not-found, etc.)."""


class SessionExpiredError(HedgeDocError):
    """Raised specifically when the stored session cookie is no longer valid.

    Callers should catch this and trigger a re-login (via `login()`) rather
    than treating it as a generic failure.
    """


def validate_session_cookie_name(cookie_name: str) -> str:
    """Return a valid HTTP cookie name, or raise ``HedgeDocError``."""
    if not _COOKIE_NAME_RE.fullmatch(cookie_name):
        raise HedgeDocError("Session cookie name must contain only RFC 6265 token characters.")
    return cookie_name


class PermissionChangeError(HedgeDocError):
    """Raised when a note exists but its requested permission was not applied."""

    def __init__(
        self,
        message: str,
        note: NoteResult | None = None,
        requested_permission: str | None = None,
    ):
        super().__init__(message)
        self.note = note
        self.requested_permission = requested_permission

    def recovery_details(self) -> dict:
        """Return machine-readable details after a create-then-mutate failure."""
        return {
            "created": self.note is not None,
            "note_id": self.note.note_id if self.note else None,
            "url": self.note.url if self.note else None,
            "requested_permission": self.requested_permission,
            "permission_verified": False,
            "error": str(self),
        }


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
    permission: str | None = None
    permission_verified: bool = False


def validate_permission(permission: str) -> str:
    """Return a valid HedgeDoc 1.11.1 permission value."""
    if permission not in PERMISSION_VALUES:
        allowed = ", ".join(sorted(PERMISSION_VALUES))
        raise HedgeDocError(f"Invalid permission '{permission}'. Allowed values: {allowed}.")
    return permission


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

    def __init__(
        self,
        base_url: str,
        timeout: int = 20,
        session_cookie_name: str = DEFAULT_SESSION_COOKIE_NAME,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session_cookie_name = validate_session_cookie_name(session_cookie_name)
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
        cookie = self._session.cookies.get(self.session_cookie_name)
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
        self._session.cookies.set(self.session_cookie_name, value, domain=domain)

    def get_session_cookie(self, encoded: bool = True) -> str | None:
        """Return the currently installed session cookie value."""
        raw = self._session.cookies.get(self.session_cookie_name)
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
        if permission is not None:
            validate_permission(permission)

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
                    requested_permission=permission,
                ) from exc
            result.permission = permission
            result.permission_verified = True
        return result

    def set_permission(self, note_id: str, permission: str) -> str:
        """Set and persist a note permission through HedgeDoc's realtime channel.

        Success means HedgeDoc emitted its post-database-update room broadcast
        and a subsequently requested refresh reported the requested value.
        """
        validate_permission(permission)
        try:
            with HedgeDocRealtimeSession(
                base_url=self.base_url,
                note_id=note_id,
                http_session=self._session,
                session_cookie_name=self.session_cookie_name,
                timeout=self.timeout,
            ) as realtime:
                realtime.set_permission(permission)
        except RealtimeAuthenticationError as exc:
            raise SessionExpiredError(str(exc)) from exc
        except RealtimeError as exc:
            raise PermissionChangeError(
                f"HedgeDoc did not confirm permission '{permission}' for note '{note_id}': {exc}",
                requested_permission=permission,
            ) from exc
        return permission

    def read_note(self, note_id: str) -> str:
        """Fetch the raw markdown content of a note. Public endpoint, no auth needed."""
        resp = self._session.get(f"{self.base_url}/{note_id}/download", timeout=self.timeout)
        if resp.status_code == 404:
            raise HedgeDocError(f"Note '{note_id}' not found.")
        resp.raise_for_status()
        return resp.text

    def update_note(self, note_id: str, content: str) -> None:
        """Raise because HedgeDoc 1.x has no supported HTTP update endpoint.

        The browser edits through its Socket.IO collaborative-editing
        protocol. ``POST /new/{alias}`` only creates a new alias; it returns
        HTTP 409 when the alias already exists and does not alter that note.
        """
        del note_id, content
        raise HedgeDocError(
            "Updating notes is unsupported: HedgeDoc 1.x has no HTTP update endpoint. "
            "POST /new/{alias} creates notes only and returns HTTP 409 for an existing alias. "
            "See docs/LIMITATIONS.md."
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
