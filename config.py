"""Local settings, with sane defaults.

Nothing here has to be configured: every value falls back to something that
works out of the box. Override what you need, in either of two ways.

A `config.json` next to this file — copy `config.example.json` and edit it. It
is gitignored, so local paths never end up in a commit:

    {"drafts_folder": "~/Documents/Outgoing mail", "pending_retention_days": 3}

Or an environment variable, which wins over the file. Handy when the MCP client
passes it in its own configuration:

    MAIL_MCP_DRAFTS_FOLDER="~/Documents/Outgoing mail"

Booleans accept 1/0, true/false, yes/no.
"""

from __future__ import annotations

import json
import os
from typing import Any

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")
ENVIRONMENT_PREFIX = "MAIL_MCP_"

DEFAULTS: dict[str, Any] = {
    # Where .eml drafts are written, and where sent ones are filed away.
    "drafts_folder": os.path.join(PROJECT_ROOT, "mails"),
    # How long a draft may sit on disk before the sweep removes it.
    "pending_retention_days": 7,
    "archive_retention_days": 30,
    # How long a send stays recorded, so its leftover autosave can be recognised.
    "ledger_window_hours": 2,
    # The search index, and Mail's storage it is built from.
    "index_path": os.path.join(PROJECT_ROOT, "index.sqlite"),
    "mail_root": os.path.expanduser("~/Library/Mail"),
    # Past this age, search_all refreshes the index before answering.
    "index_max_age_minutes": 10,
    # Ceilings for AppleScript calls. Mail is slow on a large mailbox.
    "applescript_timeout": 120,
    "applescript_write_timeout": 180,
    # Characters of body text kept per message when indexing.
    "body_limit": 200_000,
}

_FILE_VALUES: dict[str, Any] | None = None


def _from_file() -> dict[str, Any]:
    global _FILE_VALUES
    if _FILE_VALUES is None:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            _FILE_VALUES = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            # A missing file is the normal case; a malformed one must not stop
            # the server from starting on defaults.
            _FILE_VALUES = {}
    return _FILE_VALUES


def _coerce(value: Any, default: Any) -> Any:
    """Environment variables arrive as text; the default says what they mean."""
    if isinstance(default, bool):
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(value, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return os.path.expanduser(str(value))
    return value


def get(key: str) -> Any:
    """Environment variable, then config.json, then the built-in default."""
    default = DEFAULTS[key]
    from_environment = os.environ.get(ENVIRONMENT_PREFIX + key.upper())
    if from_environment is not None:
        return _coerce(from_environment, default)
    values = _from_file()
    if key in values:
        return _coerce(values[key], default)
    return default


def describe() -> dict[str, Any]:
    """Every setting with its effective value and where it came from."""
    result = {}
    for key in DEFAULTS:
        if os.environ.get(ENVIRONMENT_PREFIX + key.upper()) is not None:
            source = "environment"
        elif key in _from_file():
            source = "config.json"
        else:
            source = "default"
        result[key] = {"value": get(key), "source": source}
    return result


def reload() -> None:
    """Forgets the cached file, so a test or a caller can change it."""
    global _FILE_VALUES
    _FILE_VALUES = None
