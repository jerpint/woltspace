# Tests

## CLI Smoke Test (`test-cli.sh`)

End-to-end test for the `woltspace` CLI. Runs a full lifecycle against a temp directory — your real wolts are never touched.

### How to run

```bash
# Test main branch (what users get)
bash test/test-cli.sh

# Test local code
bash test/test-cli.sh --local

# Test a specific branch
bash test/test-cli.sh --branch refactor-init
```

### What it tests

| Test | What it checks |
|------|---------------|
| **init** | Creates wolts dir, `.env`, starts container |
| **server health** | FastAPI responds 200 on port 7777 |
| **wolt scaffolding** | `wolt.json` created from template on first boot |
| **stop** | Container stops and is removed |
| **start (resume)** | Restarts a stopped container, server comes back |
| **rebuild** | Builds fresh image, starts new container, server responds |
| **reconcile** | Stale "running" sessions are marked orphaned on boot |
| **init (idempotent)** | Re-running init with existing wolts doesn't break anything |
| **shell** | `docker exec` works, runs as `node` user |

### Isolation

The test is fully isolated from your real setup:

- `WOLTS_DIR=/tmp/test-woltspace-cli/wolts` — temp directory, cleaned up after
- `WOLTSPACE_CONTAINER=woltspace-test` — separate container name
- `WOLTSPACE_PORT=7778` — different host port (won't conflict with running woltspace on 7777)
- `WOLTSPACE_NONINTERACTIVE=true` — skips all prompts

You can run it while your real `woltspace` container is running.

### Duration

~3-5 minutes (mostly Docker build time). Cached layers make subsequent runs faster.

---

## In-Container Tests (`run-tests.sh`)

Unit and integration tests for bot, server, sessions, and creatures. Run inside the container:

```bash
# From /workspace/woltspace inside the container:
bash test/run-tests.sh              # all tests
bash test/run-tests.sh unit         # pure Python, no server needed
bash test/run-tests.sh integration  # requires running server
bash test/run-tests.sh -k "pattern" # filter by pattern
```

See `CLAUDE.md` for full test file descriptions and environment variables.

---

## Coverage

| Area | Test | Type |
|------|------|------|
| CLI lifecycle | `test-cli.sh` | host-side, end-to-end |
| Bot core | `test_bot_core.py` | unit |
| Session registry | `test_session_lifecycle.py` | unit + integration |
| Server endpoints | `test_server_health.py` | integration |
| Telegram adapter | `test_telegram_loop.py` | unit + live |
| Full pipeline | `test_closed_loop.py` | integration |
| Agent decisions | `test_agent_loop.py` | agent (haiku) |
| Wolf scheduler | `test_wolf.py` | unit |
| Wolt discovery | `test_wolts.py` | unit |

### Not yet covered

- Tunnel setup/teardown
- Multi-wolt switching
- `--branch` vs `--local` image equivalence
- `.first-run` → the create-wolt onboarding flow
- Bot adapter startup (Telegram, Slack)
