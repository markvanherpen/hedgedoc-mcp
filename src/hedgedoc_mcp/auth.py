"""Configuration and credential handling for hedgedoc-mcp.

All configuration comes from environment variables so the server works
identically whether it's launched by Claude Code, Codex, Hermes, or a
bare `hedgedoc-mcp` CLI invocation -- no code changes needed per agent.

Environment variables:
    HEDGEDOC_URL              Base URL of your HedgeDoc instance (required)
    HEDGEDOC_SESSION_COOKIE   A pre-obtained session cookie value (optional)
    HEDGEDOC_SESSION_COOKIE_NAME
                              Session cookie name (optional; default: connect.sid)
    HEDGEDOC_SESSION_COOKIE_FILE
                              Optional protected local cache for automatically
                              renewed session cookies. The file must be owned
                              by the current user and mode 0600.
    HEDGEDOC_EMAIL            Login email (optional, used for auto re-login)
    HEDGEDOC_PASSWORD         Login password (optional, used for auto re-login)

At least one of (HEDGEDOC_SESSION_COOKIE) or (HEDGEDOC_EMAIL + HEDGEDOC_PASSWORD)
must be set. If both are set, the session cookie is tried first and the
client transparently falls back to a fresh login if it has expired.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .client import (
    DEFAULT_SESSION_COOKIE_NAME,
    HedgeDocClient,
    HedgeDocError,
    SessionExpiredError,
    validate_session_cookie_name,
)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass
class Config:
    base_url: str
    session_cookie: str | None
    email: str | None
    password: str | None
    session_cookie_name: str = DEFAULT_SESSION_COOKIE_NAME
    session_cookie_file: Path | None = None

    @classmethod
    def from_env(cls) -> Config:
        base_url = os.environ.get("HEDGEDOC_URL", "").strip()
        if not base_url:
            raise ConfigError(
                "HEDGEDOC_URL is not set. Point it at your HedgeDoc instance, "
                "e.g. HEDGEDOC_URL=https://md.example.com"
            )

        cookie = os.environ.get("HEDGEDOC_SESSION_COOKIE", "").strip() or None
        cookie_file = _session_cookie_file_from_env()
        if not cookie and cookie_file and cookie_file.exists():
            cookie = _read_session_cookie_file(cookie_file)
        cookie_name = os.environ.get(
            "HEDGEDOC_SESSION_COOKIE_NAME", DEFAULT_SESSION_COOKIE_NAME
        ).strip()
        email = os.environ.get("HEDGEDOC_EMAIL", "").strip() or None
        password = os.environ.get("HEDGEDOC_PASSWORD", "").strip() or None

        if not cookie and not (email and password):
            raise ConfigError(
                "No credentials available. Set either HEDGEDOC_SESSION_COOKIE, "
                "or both HEDGEDOC_EMAIL and HEDGEDOC_PASSWORD."
            )

        try:
            validate_session_cookie_name(cookie_name)
        except HedgeDocError as e:
            raise ConfigError(f"Invalid HEDGEDOC_SESSION_COOKIE_NAME: {e}") from e

        return cls(
            base_url=base_url,
            session_cookie=cookie,
            email=email,
            password=password,
            session_cookie_name=cookie_name,
            session_cookie_file=cookie_file,
        )


def _session_cookie_file_from_env() -> Path | None:
    raw = os.environ.get("HEDGEDOC_SESSION_COOKIE_FILE", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ConfigError("HEDGEDOC_SESSION_COOKIE_FILE must be an absolute path")
    return path


def _read_session_cookie_file(path: Path) -> str:
    try:
        info = path.lstat()
    except OSError as error:
        raise ConfigError(f"Cannot inspect HEDGEDOC_SESSION_COOKIE_FILE: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError("HEDGEDOC_SESSION_COOKIE_FILE must be a regular non-symlink file")
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ConfigError("HEDGEDOC_SESSION_COOKIE_FILE must be owned by the current user with mode 0600")
    cookie = path.read_text(encoding="utf-8").strip()
    if not cookie:
        raise ConfigError("HEDGEDOC_SESSION_COOKIE_FILE is empty")
    return cookie


def _write_session_cookie_file(path: Path, cookie: str) -> None:
    """Atomically retain an auto-renewed cookie without exposing it to logs."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        # Validate the existing target before replacing it; never follow a link.
        _read_session_cookie_file(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(cookie + "\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def build_client(config: Config) -> HedgeDocClient:
    """Build a HedgeDocClient, authenticated, with auto-retry-on-expiry.

    Tries the session cookie first (fast path, no login round-trip).
    Falls back to email/password login if the cookie is missing or expired.
    """
    client = HedgeDocClient(config.base_url, session_cookie_name=config.session_cookie_name)

    if config.session_cookie:
        client.set_session_cookie(config.session_cookie)
        try:
            client.whoami()
            return client  # cookie still valid
        except SessionExpiredError:
            pass  # fall through to login below

    if config.email and config.password:
        cookie = client.login(config.email, config.password)
        if config.session_cookie_file:
            _write_session_cookie_file(config.session_cookie_file, cookie)
        return client

    raise ConfigError(
        "Session cookie was expired/invalid and no HEDGEDOC_EMAIL/HEDGEDOC_PASSWORD "
        "were provided to re-authenticate. Update HEDGEDOC_SESSION_COOKIE or set "
        "login credentials for automatic refresh."
    )


# -- CLI helper for obtaining a session cookie -----------------------------

ENV_COOKIE_LINE = re.compile(r"^HEDGEDOC_SESSION_COOKIE=.*$", re.MULTILINE)
ENV_COOKIE_NAME_LINE = re.compile(r"^HEDGEDOC_SESSION_COOKIE_NAME=.*$", re.MULTILINE)


def cli_login() -> None:
    """Entry point for `hedgedoc-mcp-login`.

    Logs in interactively (or via env vars / flags) and prints the resulting
    session cookie, optionally writing it into a .env file for reuse.
    """
    parser = argparse.ArgumentParser(
        prog="hedgedoc-mcp-login",
        description="Log in to a HedgeDoc instance and obtain a session cookie.",
    )
    parser.add_argument("--url", default=os.environ.get("HEDGEDOC_URL"), help="HedgeDoc base URL")
    parser.add_argument("--email", default=os.environ.get("HEDGEDOC_EMAIL"), help="Login email")
    parser.add_argument(
        "--password", default=os.environ.get("HEDGEDOC_PASSWORD"), help="Login password"
    )
    parser.add_argument(
        "--session-cookie-name",
        default=os.environ.get("HEDGEDOC_SESSION_COOKIE_NAME", DEFAULT_SESSION_COOKIE_NAME),
        help="HedgeDoc session cookie name (default: connect.sid)",
    )
    parser.add_argument(
        "--write-env",
        metavar="PATH",
        help="Write/update HEDGEDOC_SESSION_COOKIE in this .env file",
    )
    args = parser.parse_args()

    if not args.url:
        print("Error: --url or HEDGEDOC_URL is required", file=sys.stderr)
        sys.exit(1)
    if not args.email or not args.password:
        print(
            "Error: --email/--password or HEDGEDOC_EMAIL/HEDGEDOC_PASSWORD required",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        client = HedgeDocClient(args.url, session_cookie_name=args.session_cookie_name)
    except HedgeDocError as e:
        print(f"Error: invalid session cookie name: {e}", file=sys.stderr)
        sys.exit(1)
    client.login(args.email, args.password)
    cookie = client.get_session_cookie(encoded=True)

    print(f"HEDGEDOC_SESSION_COOKIE={cookie}")
    if args.session_cookie_name != DEFAULT_SESSION_COOKIE_NAME:
        print(f"HEDGEDOC_SESSION_COOKIE_NAME={args.session_cookie_name}")

    if args.write_env:
        path = Path(args.write_env)
        text = path.read_text() if path.exists() else ""
        new_line = f"HEDGEDOC_SESSION_COOKIE={cookie}"
        if ENV_COOKIE_LINE.search(text):
            text = ENV_COOKIE_LINE.sub(new_line, text)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"{new_line}\n"
        if args.session_cookie_name != DEFAULT_SESSION_COOKIE_NAME:
            name_line = f"HEDGEDOC_SESSION_COOKIE_NAME={args.session_cookie_name}"
            if ENV_COOKIE_NAME_LINE.search(text):
                text = ENV_COOKIE_NAME_LINE.sub(name_line, text)
            else:
                if text and not text.endswith("\n"):
                    text += "\n"
                text += f"{name_line}\n"
        path.write_text(text)
        print(f"Written to {path}", file=sys.stderr)
