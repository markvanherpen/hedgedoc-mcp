# Architecture

This project has four layers, with the client and transport independently usable:

```
┌─────────────────────────────────────────────┐
│  MCP layer (server.py)                       │
│  7 tools exposed via stdio JSON-RPC           │
│  Used by: Claude Code, Codex, Hermes, etc.    │
└───────────────────┬───────────────────────────┘
                     │
┌────────────────────▼───────────────────────────┐
│  Config/auth layer (auth.py)                     │
│  Env var parsing + cookie-first,                 │
│  login-fallback authentication strategy          │
└────────────────────┬───────────────────────────┘
                     │
┌────────────────────▼───────────────────────────┐
│  Client layer (client.py)                        │
│  HTTP endpoints plus permission orchestration    │
│  Zero MCP/env dependencies -- usable standalone  │
└─────────────────────────────────────────────────┘
                     │
┌────────────────────▼───────────────────────────┐
│  Realtime transport (realtime.py)              │
│  Authenticated Socket.IO note join, refresh,   │
│  metadata events, errors, and lifecycle         │
└────────────────────────────────────────────────┘
```

## Why separate layers, not one file

- **`client.py` has zero MCP dependencies.** You can `pip install
  hedgedoc-mcp` and use `HedgeDocClient` directly in a script, a cron job,
  a Jupyter notebook — anything — without ever touching the MCP protocol.
  This is deliberate: the hard part of this project is correctly handling
  HedgeDoc 1.x's session-cookie auth, and that value shouldn't be locked
  behind an MCP-only interface.

- **`auth.py` is the policy layer.** It decides *how* to authenticate
  (cookie-first, login-fallback) based on what env vars are present, but
  doesn't know anything about MCP or HTTP details. This is what makes
  `SessionExpiredError` handling automatic instead of something every
  caller has to reimplement.

- **`server.py` is a thin MCP adapter.** It translates 7 tool calls into
  `client.py` method calls and formats results as `TextContent`. It
  contains exactly one piece of "real" logic: catching
  `SessionExpiredError` and retrying once after forcing a fresh
  `build_client()` call, so a single expired-cookie hiccup during a long
  agent session self-heals instead of failing the tool call outright.

- **`realtime.py` owns note-scoped Socket.IO infrastructure.** It reuses
  the HTTP cookie jar, joins by note ID, captures initial and requested
  refresh state, handles realtime errors and timeouts, and guarantees
  cleanup. Permission mutation uses this transport without implementing
  or abstracting HedgeDoc's Operational Transform protocol. A future OT
  client can reuse the authenticated connection and lifecycle while adding
  document revisions, operations, transforms, resynchronization, and
  authorship handling separately.

## Authentication flow in detail

```
                    ┌──────────────────────┐
                    │ Config.from_env()      │
                    │ reads HEDGEDOC_URL,    │
                    │ _SESSION_COOKIE,       │
                    │ _SESSION_COOKIE_NAME,  │
                    │ _EMAIL, _PASSWORD      │
                    └───────────┬────────────┘
                                │
                    ┌───────────▼────────────┐
                    │ build_client(config)    │
                    └───────────┬────────────┘
                                │
                  Has session_cookie? ──No──┐
                                │             │
                               Yes            │
                                │             │
                    ┌───────────▼────────────┐│
                    │ Set cookie, call         ││
                    │ client.whoami()          ││
                    └───────────┬────────────┘│
                                │             │
                  whoami() OK? ─┤             │
                     │          │             │
                    Yes         No             │
                     │          │             │
              ┌──────▼───┐  ┌───▼─────────────▼──┐
              │  Done —   │  │ Has email+password?  │
              │  return   │  └──────────┬───────────┘
              │  client   │             │
              └───────────┘         Yes / No
                                     │      │
                          ┌──────────▼──┐  ┌▼─────────────┐
                          │ client.login│  │ raise         │
                          │ (email,pw)  │  │ ConfigError    │
                          └─────────────┘  └───────────────┘
```

This is why setting **both** the session cookie and email/password is the
recommended production setup: the cookie makes every normal call fast
(zero extra login round-trip), while email/password makes expiry a
non-event instead of an outage.

## Why HedgeDoc 1.x specifically (not 2.x)

HedgeDoc 2.x (still in active development at the time of writing) has a
genuinely different, token-friendlier API design. This project targets
1.x because:

1. It's what's actually deployed on the vast majority of self-hosted
   instances today (including the one this was built and tested against —
   HedgeDoc 1.11.1).
2. 1.x's constraints (no tokens, no generic update endpoint) are exactly
   the kind of "annoying but well-understood" problem that's worth solving
   once, generically, so nobody else has to re-discover it via trial and
   error.

If/when 2.x API-token support becomes the norm, this project would ideally
grow a second, simpler auth backend behind the same `HedgeDocClient`
interface — contributions welcome (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

## Testing strategy

All tests in `tests/` mock HTTP or Socket.IO boundaries directly (no
live server) so the suite runs in
under a second with zero external dependencies. This was a deliberate
choice to keep CI fast and to make the auth fallback logic
(`build_client`'s cookie→login flow) easy to test in isolation —
see `tests/test_auth.py` for the four `build_client` scenarios (valid
cookie, expired cookie + fallback, expired cookie + no fallback, no
cookie at all).

Live end-to-end verification (this project's server actually creating and
reading notes on a real HedgeDoc instance, including a full
MCP stdio JSON-RPC handshake) was performed manually during development
against a live instance rather than committed as a test, since it requires
real credentials. If you're contributing and want to add an opt-in
live-integration test suite gated behind env vars, see
[CONTRIBUTING.md](../CONTRIBUTING.md).
