"""Named searches: search_all parameters saved under a name, re-run by name.

They live in one small JSON file, `saved_searches.json` beside the search index
unless `saved_searches_path` says otherwise. That file is gitignored: what a
person searches for is personal, and it must never end up in the repository.

A saved search stores the query text, not its results, and the operators inside
it are stored as written. "newer_than:7d" therefore stays relative and is
evaluated again at every run ("the last week", whenever it is run), whereas an
"after:2026-01-01" or a `since` date is a fixed bound. Prefer the relative
operators for anything meant to be reused.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any

import config
import mail_search
from mail_tools import MailError

FORMAT_VERSION = 1
MAX_NAME_LENGTH = 64
# Letters (any language), digits, and a few separators; must start with an
# alphanumeric character so a name cannot be mistaken for an option or a path.
_NAME_PATTERN = re.compile(r"^[^\W_][\w .'\-]*$", re.UNICODE)

# The search_all parameters a saved search may carry, and their types. Anything
# not stored takes search_all's own default at run time.
PARAMETERS: dict[str, type] = {
    "query": str,
    "account": str,
    "mailbox": str,
    "unread_only": bool,
    "flagged_only": bool,
    "since": str,
    "until": str,
    "limit": int,
    "sort": str,
    "snippets": bool,
}
ACTIONS = ("save", "list", "show", "delete", "run")


def storage_path() -> str:
    """saved_searches_path, or saved_searches.json beside the search index."""
    configured = config.get("saved_searches_path")
    if configured:
        return configured
    return os.path.join(os.path.dirname(config.get("index_path")), "saved_searches.json")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise MailError("invalid_name", "A saved search needs a name.", "Pass name, e.g. 'unread invoices'.")
    name = " ".join(name.split())
    if len(name) > MAX_NAME_LENGTH or not _NAME_PATTERN.match(name):
        raise MailError(
            "invalid_name",
            f"Invalid name {name!r}.",
            f"Use at most {MAX_NAME_LENGTH} characters: letters, digits, spaces, '.', '-', '_' or an apostrophe, "
            "starting with a letter or digit.",
        )
    return name


def _load(path: str) -> list[dict[str, Any]]:
    """Every saved search; a missing file is simply an empty list."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise MailError(
            "saved_searches_corrupt",
            f"The saved searches file {path} cannot be read: {error}",
            "Fix or delete the file (it only holds saved searches), or point saved_searches_path elsewhere.",
        ) from error
    entries = data.get("searches") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) and isinstance(entry.get("name"), str) and isinstance(entry.get("params"), dict)
        for entry in entries
    ):
        raise MailError(
            "saved_searches_corrupt",
            f"The saved searches file {path} does not have the expected structure.",
            "Fix or delete the file (it only holds saved searches), or point saved_searches_path elsewhere.",
        )
    return entries


def _write(path: str, entries: list[dict[str, Any]]) -> None:
    """Temp file in the same folder, then os.replace: never a half-written file."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".saved_searches.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"version": FORMAT_VERSION, "searches": entries}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _find(entries: list[dict[str, Any]], name: str) -> int | None:
    wanted = name.casefold()
    for index, entry in enumerate(entries):
        if entry["name"].casefold() == wanted:
            return index
    return None


def _require(entries: list[dict[str, Any]], name: str) -> int:
    index = _find(entries, name)
    if index is None:
        known = ", ".join(entry["name"] for entry in entries) or "none saved yet"
        raise MailError("saved_search_not_found", f"No saved search named {name!r}.", f"Known names: {known}.")
    return index


def _clean_parameters(given: dict[str, Any]) -> dict[str, Any]:
    """Keeps the parameters that were set, and checks their types."""
    cleaned: dict[str, Any] = {}
    for key, value in given.items():
        if value is None:
            continue
        expected = PARAMETERS[key]
        # bool is an int in Python: a limit of True must not slip through.
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise MailError("invalid_parameter", f"{key} must be {expected.__name__}, got {value!r}.")
        cleaned[key] = value
    # limit is not range-checked: search_all clamps it to 1..200 itself.
    if "sort" in cleaned and cleaned["sort"] not in ("relevance", "date"):
        raise MailError("invalid_parameter", "sort must be 'relevance' or 'date'.")
    return cleaned


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: entry.get(key) for key in ("name", "description", "params", "created", "updated")}


def save(name: str, description: str | None = None, **parameters: Any) -> dict[str, Any]:
    """Creates a saved search, or replaces the one of that name (any case).

    The parameters given replace the stored ones as a whole, they are not merged
    into them; "created" is kept when a search is replaced.
    """
    name = validate_name(name)
    params = _clean_parameters(parameters)
    if not str(params.get("query", "")).strip():
        raise MailError(
            "invalid_parameter",
            "A saved search needs a query.",
            "Free text and/or Gmail operators, e.g. 'is:unread newer_than:7d from:@example.com'.",
        )
    path = storage_path()
    entries = _load(path)
    now = _now()
    index = _find(entries, name)
    entry = {
        "name": name,
        "description": (description or "").strip() or None,
        "params": params,
        "created": entries[index].get("created", now) if index is not None else now,
        "updated": now,
    }
    if index is None:
        entries.append(entry)
    else:
        entries[index] = entry
    _write(path, entries)
    return {"ok": True, "replaced": index is not None, "saved_search": _summary(entry)}


def list_all() -> dict[str, Any]:
    entries = sorted(_load(storage_path()), key=lambda entry: entry["name"].casefold())
    return {"ok": True, "count": len(entries), "saved_searches": [_summary(entry) for entry in entries]}


def show(name: str) -> dict[str, Any]:
    name = validate_name(name)
    entries = _load(storage_path())
    return {"ok": True, "saved_search": _summary(entries[_require(entries, name)])}


def delete(name: str) -> dict[str, Any]:
    name = validate_name(name)
    path = storage_path()
    entries = _load(path)
    removed = entries.pop(_require(entries, name))
    _write(path, entries)
    return {"ok": True, "deleted": removed["name"]}


def run(name: str, **overrides: Any) -> dict[str, Any]:
    """Runs a saved search through search_all; parameters given here win."""
    name = validate_name(name)
    entries = _load(storage_path())
    stored = entries[_require(entries, name)]["params"]
    unknown = set(stored) - set(PARAMETERS)
    if unknown:
        raise MailError("saved_searches_corrupt", f"Saved search {name!r} carries unknown parameters: {sorted(unknown)}.")
    try:
        stored = _clean_parameters(stored)
    except MailError as error:
        raise MailError(
            "saved_searches_corrupt",
            f"Saved search {name!r} holds an invalid parameter: {error.message}",
            "Save it again with saved_search(action='save'), or fix the file.",
        ) from error
    parameters = {**stored, **_clean_parameters(overrides)}
    parameters.setdefault("query", "")
    return mail_search.search_all(**parameters)
