"""The connectors this lodge knows (RFC #617).

A connector is a folder: `connector.json` (its manifest) and its program. The
lodge finds them in two places:

- bundled, in this install's `container/connectors/<name>/` (Telegram);
- local, in the data root's `.space/connectors/<name>/`, so a connector can be
  tried in one lodge before it is contributed. A local folder never replaces a
  bundled connector of the same name.

Either way the folder is never imported into the lodge: the lodge starts its
program and talks to it over a socket. Reading a manifest runs no code: route
fields are checked with the regular expressions it declares.

Slack predates folders and still has classes in the lodge; its entry below
gives it a manifest and a route codec until it moves out.

Light on purpose: no provider SDK is imported, so the server, the CLI and the
launcher can all read what a connector declares without loading it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from connector_contract import ConnectorSpec, Manifest, ManifestError, RouteCodec

logger = logging.getLogger(__name__)

BUNDLED_DIR = Path(__file__).resolve().parents[1] / "connectors"
MANIFEST_FILE = "connector.json"
MAX_MANIFEST_BYTES = 64 * 1024

_SLACK_CHANNEL = re.compile(r"[A-Za-z0-9_-]+")
_SLACK_THREAD = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _slack_parse(text: str) -> dict:
    channel, sep, thread_ts = (text or "").partition(":")
    if not sep or _SLACK_CHANNEL.fullmatch(channel) is None:
        raise ValueError("Slack channel has invalid characters")
    if _SLACK_THREAD.fullmatch(thread_ts) is None:
        raise ValueError("Slack thread timestamp must be numeric")
    return {"channel": channel, "thread_ts": thread_ts}


def _slack_from_record(record: dict) -> dict | None:
    if record.get("adapter") != "slack":
        return None
    try:
        return _slack_parse(f"{record.get('chat_id') or ''}:{record.get('thread_ts') or ''}")
    except ValueError:
        return None


SLACK = Manifest(
    name="slack",
    capabilities=frozenset({"text", "files", "voice_out", "threads", "status"}),
    command_input=frozenset({"buttons"}),
    events=frozenset({"message", "selection"}),
    route_fields=("channel", "thread_ts"),
    secrets=("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
    owner_env="SLACK_OWNER_USER",
)

# Connectors with classes of their own in the lodge (no folder yet).
SPECS: dict[str, ConnectorSpec] = {
    "slack": ConnectorSpec(
        SLACK,
        RouteCodec(("channel", "thread_ts"), _slack_parse, _slack_from_record,
                   legacy_args=lambda r: ["--slack", r["channel"], r["thread_ts"]]),
        title="Slack",
    ),
}


# --- folders -----------------------------------------------------------------------

def _strings(data: dict, key: str, path: Path) -> tuple[str, ...]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ManifestError(f"{path}: {key} must be a list of strings")
    return tuple(value)


def _codec(name: str, route: list, legacy_flag: str, path: Path) -> RouteCodec:
    """A route codec from declared fields and their patterns: data, not code."""
    if not isinstance(route, list) or not 0 < len(route) <= 4:
        raise ManifestError(f"{path}: route must list 1 to 4 [field, pattern] pairs")
    fields, patterns = [], []
    for entry in route:
        if not (isinstance(entry, list) and len(entry) == 2 and all(isinstance(x, str) for x in entry)):
            raise ManifestError(f"{path}: each route entry is [field, pattern]")
        field, pattern = entry
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", field) or field == "adapter":
            raise ManifestError(f"{path}: invalid route field {field!r}")
        if ":" in pattern or len(pattern) > 200:
            raise ManifestError(f"{path}: a route pattern may not match ':' and is at most 200 characters")
        try:
            patterns.append(re.compile(pattern))
        except re.error as exc:
            raise ManifestError(f"{path}: route pattern for {field}: {exc}") from None
        fields.append(field)
    fields = tuple(fields)

    def parse(text: str) -> dict:
        parts = (text or "").split(":")
        if len(parts) != len(fields):
            raise ValueError(f"{name} route is {':'.join(fields)}")
        for field, pattern, part in zip(fields, patterns, parts):
            if pattern.fullmatch(part) is None or len(part) > 256:
                raise ValueError(f"{name} route {field} is not valid")
        return dict(zip(fields, parts))

    def from_record(record: dict) -> dict | None:
        if record.get("adapter") != name:
            return None
        try:
            return parse(":".join(str(record.get(f) or "") for f in fields))
        except ValueError:
            return None

    legacy = (lambda r: [legacy_flag, *(r[f] for f in fields)]) if legacy_flag else None
    return RouteCodec(fields, parse, from_record, legacy_args=legacy)


def load_folder(folder: Path, *, bundled: bool) -> ConnectorSpec:
    """The spec a connector folder declares; ManifestError when it is unusable."""
    path = Path(folder) / MANIFEST_FILE
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ManifestError(f"{path}: larger than {MAX_MANIFEST_BYTES} bytes")
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: not a JSON object")
    name = data.get("name")
    if name != Path(folder).name:
        raise ManifestError(f"{path}: name {name!r} must match its folder")
    secrets = data.get("secrets", [])
    if not isinstance(secrets, list) or not all(
            isinstance(s, list) and len(s) == 2 and all(isinstance(x, str) for x in s)
            and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", s[0]) for s in secrets):
        raise ManifestError(f"{path}: secrets is a list of [ENV_NAME, config_key] pairs")
    manifest = Manifest(
        name=name,
        protocol=data.get("protocol", 0),
        capabilities=frozenset(_strings(data, "capabilities", path)),
        command_input=frozenset(_strings(data, "command_input", path)),
        command_prefix=data.get("command_prefix", "/"),
        events=frozenset(_strings(data, "events", path)),
        route_fields=tuple(field for field, _ in data.get("route", []) if isinstance(field, str)),
        secrets=tuple(s[0] for s in secrets),
    )
    command = _strings(data, "command", path)
    if not command:
        raise ManifestError(f"{path}: command is required")
    title = data.get("title", name)
    legacy_flag = data.get("legacy_flag", "") if bundled else ""
    for key, value in (("title", title), ("dependency", data.get("dependency", "")),
                       ("enable_env", data.get("enable_env", ""))):
        if not isinstance(value, str):
            raise ManifestError(f"{path}: {key} must be a string")
    return ConnectorSpec(
        manifest,
        _codec(name, data.get("route"), legacy_flag, path),
        title=title,
        folder=str(Path(folder).resolve()),
        command=command,
        bundled=bundled,
        dependency=data.get("dependency", ""),
        dependency_label=data.get("dependency_label", "") if isinstance(data.get("dependency_label", ""), str) else "",
        config_keys=tuple((s[0], s[1]) for s in secrets),
        # Only a bundled connector may be switched on by the environment.
        enable_env=data.get("enable_env", "") if bundled else "",
        help=_strings(data, "help", path),
        owners_env=data.get("owners_env", "") if bundled else "",
        owners_config=data.get("owners_config", "") if bundled else "",
    )


def local_dir(wolts_dir=None) -> Path:
    root = Path(wolts_dir or os.environ.get("WOLTSPACE_WOLTS_DIR") or "/workspace/wolts")
    return root / ".space" / "connectors"


def folders(wolts_dir=None, bundled_dir: Path | None = None) -> dict[str, ConnectorSpec]:
    """Every usable connector folder, bundled first. Unusable ones are logged and skipped."""
    found: dict[str, ConnectorSpec] = {}
    for root, bundled in ((bundled_dir or BUNDLED_DIR, True), (local_dir(wolts_dir), False)):
        try:
            entries = sorted(p for p in Path(root).iterdir() if (p / MANIFEST_FILE).is_file())
        except OSError:
            continue
        for folder in entries:
            if folder.name in found or folder.name in SPECS:
                if not bundled:
                    logger.warning("connector %s in %s is ignored: a bundled one has that name",
                                   folder.name, root)
                continue
            try:
                found[folder.name] = load_folder(folder, bundled=bundled)
            except ManifestError as exc:
                logger.warning("connector folder skipped: %s", exc)
    return found


def spec(name: str, wolts_dir=None) -> ConnectorSpec | None:
    if name in SPECS:
        return SPECS[name]
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        return None
    return folders(wolts_dir).get(name)


def manifest(name: str, wolts_dir=None) -> Manifest | None:
    found = spec(name, wolts_dir)
    return found.manifest if found else None


def declared(wolts_dir=None) -> list[ConnectorSpec]:
    """The connectors that run from a folder, bundled first."""
    return list(folders(wolts_dir).values())
