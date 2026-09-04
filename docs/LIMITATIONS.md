# Known limitations (HedgeDoc 1.x server constraints)

This document explains what's genuinely **not possible** with HedgeDoc 1.x's
HTTP surface — as opposed to a limitation of this client. If you hit one of
these, the fix is either "use the workaround below" or "wait for /
contribute HedgeDoc 2.x support," not "file a bug against hedgedoc-mcp."

All of this was verified against HedgeDoc 1.11.1's actual [OpenAPI
spec](https://docs.hedgedoc.org/dev/api/) and confirmed with live testing
against a real self-hosted instance.

## No API tokens

HedgeDoc 1.x authenticates every write endpoint via an Express session
cookie (`connect.sid`), set on login. There is no concept of an API token,
bearer auth, or scoped credential anywhere in the 1.x line — that's a
HedgeDoc 2.x / newer-fork feature.

**What this means for you:** you must provide either a session cookie
(`HEDGEDOC_SESSION_COOKIE`) or login credentials (`HEDGEDOC_EMAIL` +
`HEDGEDOC_PASSWORD`) so the client can obtain one. See the [README](../README.md#quick-start).

**Workaround this project uses:** `hedgedoc_mcp.auth.build_client()` tries
the session cookie first (fast path, zero login round-trips) and
transparently re-logs in with email/password if the cookie is missing or
expired. This means once you configure both, you never think about session
expiry again.

## No generic "update note by ID" endpoint

HedgeDoc 1.x's real-time collaborative editing happens over a Socket.IO
channel, not a REST endpoint. There is no `PUT /api/notes/{id}` or similar.

`POST /new/{alias}` is a creation endpoint, not an update workaround. With
FreeURL enabled, the first request for an alias creates the note; a second
request for that alias returns **HTTP 409 Conflict** and leaves the original
content unchanged.

`hedgedoc_update_note` therefore reports a clear unsupported-operation error
without issuing an HTTP request. Neither alias nor random-ID notes can be
updated through HedgeDoc 1.x's HTTP surface. They can be edited in the
browser, which uses the Socket.IO collaborative-editing protocol; supporting
that protocol is outside this HTTP client's current scope.

## Note permissions use Socket.IO

`POST /new` and `POST /new/{alias}` do not accept a permission parameter.
HedgeDoc persists permission in the note record and lets only the owner change
it through the editor's Socket.IO channel. This metadata event is separate from
the Operational Transform protocol used for note text.

`hedgedoc_create_note` therefore leaves the server default untouched when its
optional `permission` argument is omitted. When supplied, it creates the note
and then applies the permission through Socket.IO. `hedgedoc_set_permission`
does the same for an existing note. Success requires both HedgeDoc's matching
room-wide event, which is emitted after its database-update promise resolves,
and a subsequently requested `refresh` reporting the same permission. The
refresh reflects HedgeDoc's realtime state and may be backed by its in-memory
note cache; it is not described as an independent database read.

HedgeDoc 1.11.1 supports `freely`, `editable`, `limited`, `locked`, `protected`,
and `private`. In particular, `protected` allows signed-in users to read while
only the owner may edit; anonymous users cannot read it. Permission creation and
mutation are separate server operations. If the latter fails, a newly created
note still exists at its returned URL. The client reports that partial outcome
without claiming the requested permission was applied.

Live HedgeDoc 1.11.1 acceptance testing verified protected-note handoff in both
directions between distinct authenticated owners/readers, while anonymous access
did not expose protected content.

## No delete endpoint

There is no `DELETE /{note}` (or equivalent) anywhere in the HedgeDoc 1.x
API surface. Notes can only be deleted by a logged-in user through the web
UI (History page → trash icon), not over HTTP.

**This project does not expose a delete tool** because there is nothing to
wrap — it would need to drive the browser UI, which is a fundamentally
different (and much heavier) integration than the rest of this client.

## Anonymous note creation may be disabled

Whether `POST /new` works *without* being logged in depends entirely on
the target instance's configuration (`CMD_ALLOW_ANONYMOUS`). Many
self-hosted instances (including the one this project was built against)
disable it for security. If it's disabled and you have no valid session,
`hedgedoc_create_note` will fail with a `SessionExpiredError`-style
message even though nothing about *your* credentials is wrong.

**Fix:** make sure `HEDGEDOC_EMAIL`/`HEDGEDOC_PASSWORD` (or a fresh
`HEDGEDOC_SESSION_COOKIE`) are set so the client can authenticate.

## Session cookies expire

Express sessions have a TTL (commonly 14 days by default, or shorter/longer
depending on the instance's config), and are invalidated on server restart
or `SESSION_SECRET` rotation. A stale `HEDGEDOC_SESSION_COOKIE` alone
(without email/password configured as a fallback) will eventually stop
working.

**Fix:** set `HEDGEDOC_EMAIL` + `HEDGEDOC_PASSWORD` alongside the cookie so
`build_client()` can silently re-authenticate. If you only ever set the
cookie, you'll need to re-run `hedgedoc-mcp-login` manually when it
expires.

## Summary table

| Capability | Supported? | Notes |
|---|---|---|
| Create note (random ID) | ✅ | `hedgedoc_create_note` |
| Create note (custom alias) | ✅* | Requires `CMD_ALLOW_FREEURL=true` on the server |
| Change note permission | ✅ | Owner-only Socket.IO metadata event; matching broadcast and refresh are verified |
| Read note content | ✅ | Public endpoint, no auth needed |
| Update note (alias-based) | ❌ | `POST /new/{alias}` returns 409 when it already exists |
| Update note (random-ID) | ❌ | Not possible over HTTP in HedgeDoc 1.x |
| Delete note | ❌ | Only via the web UI, not exposed by this project |
| Note metadata (title, views, dates) | ✅ | `hedgedoc_note_info` |
| List revision history | 🟡 | `client.py` exposes it at the client level; not yet wired to an MCP tool |
| API token auth | ❌ | Does not exist in HedgeDoc 1.x — use session cookie / login instead |
