"""Lodge secrets stay off command lines and out of the processes it starts
for apps."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import apps  # noqa: E402

LODGE_SECRETS = {
    "SLACK_APP_TOKEN": "s1",
    "TELEGRAM_BOT_TOKEN": "s2",
    "GITHUB_APP_PRIVATE_KEY": "s3",
    "CLOUDFLARE_TUNNEL_TOKEN": "s4",
    "CLAUDE_CODE_OAUTH_TOKEN": "s5",
    "slack_lower_token": "s6",
}


def test_app_environment_drops_lodge_secrets_and_keeps_the_rest(monkeypatch):
    for key, value in LODGE_SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("APP_SETTING", "kept")

    env = apps.app_environment(PORT="4000")

    assert not set(LODGE_SECRETS) & set(env)
    assert env["APP_SETTING"] == "kept"
    assert env["PORT"] == "4000"


def test_skills_and_docs_never_put_a_bearer_token_on_curls_command_line():
    # `-H "Authorization: Bearer $TOKEN"` or `${TOKEN}`, any quoting.
    bearer_arg = re.compile(r"""-H\s+["']Authorization:\s*Bearer\s+\$""")
    paths = [*(ROOT / "container" / "skills").rglob("*.md"), *(ROOT / "docs").rglob("*.md")]
    offenders = [
        str(path.relative_to(ROOT)) for path in paths if bearer_arg.search(path.read_text())
    ]
    assert offenders == []
