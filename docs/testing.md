# Test and package checks

CI runs focused release checks on Python 3.11 and 3.13 with Node 22, the Node
suite, and Python/npm package builds. The full historical suite is being
refactored and is not currently a release gate. Package verification compares
bundled Python modules, platform skills, shared-skill documentation and TUI
JavaScript with the checkout, checks the notify executable in both Python
artifacts, and checks Python/TUI runtime versions against both Docker pins.
These checks do not publish packages or exercise authenticated agents.

For checkout tests, install Python and both Node dependency sets:

```sh
python -m pip install '.[test]'
npm ci
npm ci --prefix tui
export PYTHONPATH="$PWD:$PWD/src:$PWD/container/lib"
export WOLTSPACE_DIR="$PWD"
python -m pytest -q test/test_lodge_skills.py test/test_skills_sync.py \
  test/test_session_target_api.py test/test_execution_policy.py \
  test/test_auto_grants_cli.py test/test_backup.py test/test_native_doctor.py \
  test/test_closed_loop.py::TestRegressions::test_notify_footer_appended \
  test/test_closed_loop.py::TestRegressions::test_notify_script_executable \
  test/test_closed_loop.py::TestRegressions::test_den_reply_footer_consistent \
  test/test_native_resilience.py \
  test/test_native_surfaces.py test/test_telegram_loop.py \
  test/test_wolts.py::TestCredentials
npm test
```

Run as an ordinary user so permission tests can exercise unreadable files. Git
metadata is required by the tracked-source environment scans. `tmux` enables
local process integration checks. Source/API fixtures must explicitly select
host or external isolation, use temporary data and authentication paths, and
stub external processes or HTTP calls unless that behavior is the test subject.
Site and onboarding probes disable the public tunnel; tunnel configuration and
process behavior have separate tests.

The livereload subprocess probe explicitly closes its websocket and waits for
the ASGI handler to finish before leaving TestClient's websocket context.
TestClient otherwise cancels that handler immediately after sending the
disconnect, which can leave a native file-watch worker running at interpreter
exit. The probe still requires a clean subprocess exit; it does not bypass
shutdown. This exercises graceful disconnect, not forced server cancellation.

The historical suite can be investigated with `python -m pytest test -q` in an
isolated checkout/environment. Some groups depend on a running lodge, Telegram
configuration, or authenticated agents. Existing `requires_*` markers and
explicit live-send/real-spawn opt-ins guard those groups. Do not enable those
opt-ins against personal services for an ordinary CI run. The clean-environment
baseline and targeted checks are different claims: a successful focused run
does not establish that the full suite or live agent flows passed.

CI selects the repaired source-contract cases from `test_closed_loop.py` and the
credential fixture group from `test_wolts.py`. Other closed-loop cases still
assume a writable deployed `/workspace/wolts` environment. The full wolts file
also has a pre-existing assertion that a raccoon resolves to literal `opus`,
which disagrees with the current model catalog in observed runs. Those groups
remain part of the parked historical suite; expanding the focused selection
requires resolving those environment and model assumptions first.
