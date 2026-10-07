"""Load, validate and atomically save config.json."""

import json
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("DASHBOARD_CONFIG", ROOT / "config.json"))
EXAMPLE_PATH = ROOT / "config.example.json"
DATA_DIR = Path(os.environ.get("DASHBOARD_DATA", ROOT / "data"))

NUMBER = (int, float)

# Expected shape: a type (or tuple of types), or a nested dict of the same.
_SCHEMA = {
    "listen": str,
    "public_host": str,
    "auth": {"username": str, "password_hash": str, "session_hours": NUMBER},
    "telegram": {"bot_token": str, "chat_id": (str, int), "enabled": bool},
    "watch": {"systemd": list, "supervisor": (str, list), "protected": list},
    "thresholds": dict,
    "alerts": {"repeat_minutes": NUMBER, "notify_recovery": bool, "notify_logins": bool},
}


# Admin tools (phases 13-19). Each is off unless config.json says true: the web page can never switch one on,
# because nothing in the program writes to config.json except `set-password`.
ADMIN_SWITCHES = ("power", "users", "ssh_keys", "private_keys", "configs")


class ConfigError(Exception):
    pass


def _check(value, schema, where: str) -> None:
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise ConfigError(f"{where}: expected an object")
        for key, sub in schema.items():
            if key not in value:
                raise ConfigError(f"{where}.{key}: missing")
            _check(value[key], sub, f"{where}.{key}")
    elif not isinstance(value, schema) or (schema is NUMBER and isinstance(value, bool)):
        raise ConfigError(f"{where}: wrong type {type(value).__name__}")


def validate(cfg: dict) -> None:
    _check(cfg, _SCHEMA, "config")

    if "idle_minutes" in cfg["auth"]:           # optional: configs written before it existed still load
        idle = cfg["auth"]["idle_minutes"]
        _check(idle, NUMBER, "config.auth.idle_minutes")
        if not 1 <= idle <= 1440:
            raise ConfigError("config.auth.idle_minutes: expected 1 to 1440 minutes")

    if "admin" in cfg:
        admin = cfg["admin"]
        if not isinstance(admin, dict):
            raise ConfigError("config.admin: expected an object")
        unknown = set(admin) - set(ADMIN_SWITCHES)
        if unknown:
            raise ConfigError(f"config.admin: unknown key(s): {', '.join(sorted(unknown))}; use {', '.join(ADMIN_SWITCHES)}")
        for key, value in admin.items():
            if not isinstance(value, bool):
                raise ConfigError(f"config.admin.{key}: expected true or false")

    host, _, port = cfg["listen"].rpartition(":")
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f"config.listen: expected host:port, got {cfg['listen']!r}")
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ConfigError("config.listen: must be a loopback address; nginx is the only way in")

    for name, rule in cfg["thresholds"].items():
        where = f"config.thresholds.{name}"
        _check(rule, {"warn": NUMBER, "crit": NUMBER}, where)
        if "for_s" in rule:
            _check(rule["for_s"], NUMBER, f"{where}.for_s")

    unknown = set(cfg["watch"]["protected"]) - set(cfg["watch"]["systemd"])
    if unknown:
        raise ConfigError(f"config.watch.protected: not in watch.systemd: {', '.join(sorted(unknown))}")


def admin_switches(cfg: dict) -> dict[str, bool]:
    """Which admin tools are on. A missing block, or a missing key, means off."""
    block = cfg.get("admin")
    block = block if isinstance(block, dict) else {}
    return {name: block.get(name) is True for name in ADMIN_SWITCHES}


def load(path: Path | None = None, allow_example: bool = False) -> tuple[dict, Path]:
    """Return (config, path it came from).

    With allow_example, fall back to config.example.json when config.json is
    missing or root-only, so read-only commands work during development.
    """
    path = Path(path or CONFIG_PATH)
    try:
        text = path.read_text()
    except (FileNotFoundError, PermissionError) as e:
        if not allow_example:
            hint = "run with sudo" if isinstance(e, PermissionError) else "see PLAN.md step 1.4"
            raise ConfigError(f"cannot read {path} ({e.strerror}); {hint}") from None
        path = EXAMPLE_PATH
        text = path.read_text()
    try:
        cfg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{path}: invalid JSON: {e}") from None
    validate(cfg)
    return cfg, path


def save(cfg: dict, path: Path | None = None) -> None:
    """Write config atomically with mode 600 (it holds secrets)."""
    validate(cfg)
    path = Path(path or CONFIG_PATH)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
