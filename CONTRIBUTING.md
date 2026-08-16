# Contributing to hedgedoc-mcp

Thanks for considering a contribution! This is a small, focused project —
here's how to get productive quickly.

## Setup

```bash
git clone https://github.com/kanishkpachauri/hedgedoc-mcp.git
cd hedgedoc-mcp
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Running tests

```bash
pytest              # all tests, mocked HTTP, no live server needed
pytest -v            # verbose
ruff check .          # lint
```

All 25+ tests should pass with zero network access. If you add a new
`HedgeDocClient` method, add a corresponding mocked test in
`tests/test_client.py` following the existing pattern (see `_FakeResponse`
helper at the top of that file).

## Project structure

```
src/hedgedoc_mcp/
  client.py    # HTTP client, zero MCP dependencies — the reusable core
  auth.py      # env var config + cookie-first/login-fallback auth policy
  server.py    # MCP server: 6 tools, thin adapter over client.py
tests/
  test_client.py   # mocked HTTP tests for client.py
  test_auth.py      # config parsing + auth fallback logic tests
docs/
  ARCHITECTURE.md   # how the three layers fit together
  LIMITATIONS.md     # what HedgeDoc 1.x genuinely can't do over HTTP
```

## Guidelines

- **Keep `client.py` MCP-free.** It should always be usable as a
  standalone Python client with zero knowledge of the `mcp` package. If
  you're adding MCP-specific logic, it belongs in `server.py`.
- **New client methods need mocked tests.** See the existing tests for the
  pattern — no live HedgeDoc instance required to contribute.
- **Match HedgeDoc's real API.** If you're not sure an endpoint exists,
  check the [official OpenAPI spec](https://docs.hedgedoc.org/dev/api/)
  first. Don't invent endpoints that don't exist in HedgeDoc 1.x — update
  `docs/LIMITATIONS.md` instead if something genuinely isn't possible.
- **New MCP tools need a docstring-quality `description`.** Agents decide
  whether to call a tool based on its description alone — be specific
  about what it does and does not do (see `hedgedoc_update_note`'s
  description for an example of documenting a real limitation inline).

## Reporting HedgeDoc version incompatibilities

This project was built and tested against HedgeDoc **1.11.1**. If you hit
a real difference on another 1.x version, please include:

- Your HedgeDoc version (`docker exec <container> cat package.json | grep version`, or check `/status`)
- The exact request/response that differs from what `client.py` expects
- Whether the issue is in `POST /new`, `GET /{note}/download`,
  `GET /{note}/info`, `/me`, `/history`, or `/login`

## Adding support for a new agent client

If you've wired this server into an agent not covered in the README (only
Claude Code, Codex, and Hermes are documented today), a PR adding a
`<details>` block with that agent's config format is very welcome — no
code changes needed, just documentation.

## Code of conduct

Be respectful, be constructive, assume good faith. That's it.
