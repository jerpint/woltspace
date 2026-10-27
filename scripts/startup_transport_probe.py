"""Private synthetic tool-context probe; emits classes, never environment values."""
import errno
import json
import os
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


def health(endpoint):
    try:
        with urlopen(endpoint + "/health", timeout=2) as response:
            return "ok" if response.status == 200 else "http_error"
    except HTTPError:
        return "http_error"
    except (TimeoutError, socket.timeout):
        return "timeout"
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return "timeout"
        code = getattr(reason, "errno", None)
        return "refused" if code == errno.ECONNREFUSED else "blocked" if code in (errno.EPERM, errno.EACCES) else "unreachable"
    except OSError:
        return "unreachable"


def net_namespace():
    try:
        return os.readlink("/proc/self/ns/net")
    except OSError:
        return ""


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(data, out)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_config(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            raise ValueError("unsafe private probe configuration")
        config = json.load(source)
    if (not isinstance(config, dict) or set(config) != {"endpoint", "session", "notify", "net_namespace"}
            or not all(isinstance(value, str) for value in config.values())
            or Path(config["notify"]).resolve() != Path(__file__).resolve().parents[1] / "container/bin/notify"):
        raise ValueError("invalid private probe configuration")
    return config


def destination(actual, expected):
    if actual == expected:
        return "expected"
    try:
        dest = urlsplit(actual)
        if (dest.scheme == "http" and dest.hostname in ("localhost", "127.0.0.1")
                and dest.port == 7777 and not dest.path):
            return "default_7777"
    except ValueError:
        pass
    return "other"


def valid_result(data):
    return (isinstance(data, dict) and set(data) == {
        "phase", "api_present", "destination", "session_matches", "executable",
        "net_namespace", "health", "notify", "notify_exit",
    } and data["phase"] in ("health_started", "complete")
        and type(data["api_present"]) is bool and type(data["session_matches"]) is bool
        and data["destination"] in ("expected", "default_7777", "other")
        and data["executable"] in ("checkout", "other", "missing")
        and data["net_namespace"] in ("same", "different", "unavailable")
        and data["health"] in ("not_checked", "ok", "http_error", "timeout", "refused", "blocked", "unreachable")
        and data["notify"] in ("not_attempted", "skipped", "sent", "failed")
        and (data["notify_exit"] is None or type(data["notify_exit"]) is int and -255 <= data["notify_exit"] <= 255))


def run(config_path):
    """Called by the synthetic executable in one Codex tool invocation."""
    try:
        # Private config contains only expected local endpoint/session/path/ns.
        fd = os.open(config_path.with_suffix(".lock"), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        deadline = time.monotonic() + 10
        while True:
            config = read_config(config_path)
            if config.get("session"):
                break
            if time.monotonic() >= deadline:
                raise ValueError("probe configuration is not ready")
            time.sleep(0.05)
        expected = config["endpoint"]
        url = urlsplit(expected)
        if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                or url.port == 7777 or url.path or url.query or url.fragment or url.username):
            raise ValueError("probe endpoint is not the isolated loopback server")
        actual = os.environ.get("WOLTSPACE_API", "http://localhost:7777").rstrip("/")
        namespace = net_namespace()
        executable = shutil.which("notify")
        result = {
            "phase": "health_started", "api_present": "WOLTSPACE_API" in os.environ,
            "destination": destination(actual, expected),
            # Mirror notify's get_env canonical/nonempty legacy precedence.
            "session_matches": (os.environ.get("WOLTSPACE_WOLT_SESSION") or os.environ.get("WOLT_SESSION", "")) == config["session"],
            "executable": "missing" if not executable else "checkout" if Path(executable).resolve() == Path(config["notify"]).resolve() else "other",
            "net_namespace": "unavailable" if not namespace or not config["net_namespace"] else "same" if namespace == config["net_namespace"] else "different",
            "health": "not_checked", "notify": "not_attempted", "notify_exit": None,
        }
        result_path = config_path.with_name("transport-probe-result.json")
        write_json(result_path, result)
        result["health"] = health(expected)  # Always only the known test server.
        if (result["health"] == "ok" and result["destination"] == "expected"
                and result["session_matches"] and result["executable"] == "checkout"):
            try:
                response = subprocess.run(
                    [executable, "--whatsapp", "user:4242424242"], input="startup-inspection-ok",
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                )
                result["notify_exit"] = response.returncode
                result["notify"] = "sent" if response.returncode == 0 else "failed"
            except (OSError, subprocess.SubprocessError):
                result["notify"] = "failed"
        else:
            result["notify"] = "skipped"
        result["phase"] = "complete"
        write_json(result_path, result)
        print(json.dumps(result))
        return 0 if result["notify"] == "sent" else 2
    except Exception:
        print("Startup transport probe failed; no environment or exception detail emitted.")
        return 2
