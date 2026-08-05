"""Queries the local index built by mail_index.py.

This module never touches Mail: it reads the SQLite index and returns the same
message references the AppleScript tools use, so a hit can be opened, replied
to or moved without anything else changing.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

import config
from mail_tools import MailError, MessageReference

INDEX_PATH = config.get("index_path")

# Mailboxes a Gmail account duplicates everything into. A message is reachable
# through any of its mailboxes, but Mail resolves an id far faster in a small
# one, so these are the last resort when picking a reference.
BULK_MAILBOXES = ("[Gmail]/Tous les messages", "[Gmail]/All Mail", "[Gmail]/Important")


def _connect() -> sqlite3.Connection:
    if not os.path.isfile(INDEX_PATH):
        raise MailError(
            "index_missing",
            "The search index has not been built yet.",
            "Run: python3 mail_index.py --build (needs Full Disk Access).",
        )
    connection = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _as_timestamp(value: str | None, end_of_day: bool = False) -> int | None:
    if not value:
        return None
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            moment = datetime.strptime(text, pattern)
            if end_of_day:
                moment = moment.replace(hour=23, minute=59, second=59)
            return int(moment.timestamp())
        except ValueError:
            continue
    raise MailError("invalid_date", f"Unreadable date: {value!r}", "Use YYYY-MM-DD.")


def _quote_terms(query: str) -> str:
    """Rewrites a query as quoted terms, for when the raw one is not valid FTS5.

    Users type things like "facture 12/2025" or "re: devis"; the punctuation is
    FTS5 syntax and blows up. Quoting each word keeps the intent.
    """
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    return " AND ".join('"' + term.replace('"', "") + '"' for term in terms)


def _index_age_minutes() -> float | None:
    """Minutes since the last successful sync, or None if never built."""
    if not os.path.isfile(INDEX_PATH):
        return None
    connection = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = 'last_build'").fetchone()
    except sqlite3.DatabaseError:
        return None
    finally:
        connection.close()
    if not row or not row[0]:
        return None
    return (time.time() - int(row[0])) / 60


def _refresh_if_stale(max_age_minutes: float) -> dict[str, Any]:
    """Syncs the index when it has gone stale, and never fails the search.

    A search that silently misses a message received ten minutes ago is worse
    than a slow one, so staleness triggers a sync. But if the store cannot be
    read any more — Full Disk Access revoked — searching what is already
    indexed still beats returning an error.
    """
    age = _index_age_minutes()
    if age is None or age <= max_age_minutes:
        return {"synced": False, "index_age_minutes": round(age, 1) if age else 0.0}

    lock_path = INDEX_PATH + ".sync.lock"
    try:
        # A stale lock from a killed run must not block every later search.
        if os.path.exists(lock_path) and time.time() - os.path.getmtime(lock_path) > 1800:
            os.unlink(lock_path)
        handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(handle)
    except FileExistsError:
        return {"synced": False, "index_age_minutes": round(age, 1), "sync_note": "already running"}

    try:
        result = sync_index()
        return {
            "synced": True,
            "index_age_minutes": 0.0,
            "sync_added": result["added"],
            "sync_removed": result["removed"],
        }
    except MailError as error:
        return {
            "synced": False,
            "index_age_minutes": round(age, 1),
            "sync_note": f"{error.code}: {error.message}",
            "sync_hint": error.hint,
        }
    finally:
        try:
            os.unlink(lock_path)
        except OSError:
            pass


def _mailbox_sizes(connection: sqlite3.Connection) -> dict[tuple[str, str], int]:
    return {
        (row["account"], row["mailbox"]): row["n"]
        for row in connection.execute(
            "SELECT account, mailbox, count(*) AS n FROM locations GROUP BY account, mailbox"
        )
    }


def _pick_location(
    locations: list[sqlite3.Row], sizes: dict[tuple[str, str], int]
) -> sqlite3.Row | None:
    """Chooses the mailbox a reference should point at.

    Mail looks a message up by walking the mailbox it is told about, so the
    smallest one wins, and the Gmail catch-all mailboxes come last.
    """
    if not locations:
        return None

    def rank(location: sqlite3.Row) -> tuple[int, int]:
        bulk = 1 if location["mailbox"] in BULK_MAILBOXES else 0
        return bulk, sizes.get((location["account"], location["mailbox"]), 10**9)

    return sorted(locations, key=rank)[0]


def search_all(
    query: str,
    account: str | None = None,
    mailbox: str | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    max_age_minutes: float = config.get("index_max_age_minutes"),
) -> dict[str, Any]:
    """Searches every indexed message, across all accounts."""
    if not query.strip():
        raise MailError("empty_query", "The query is empty.")
    limit = max(1, min(int(limit), 200))
    freshness = _refresh_if_stale(max_age_minutes)
    since_ts = _as_timestamp(since)
    until_ts = _as_timestamp(until, end_of_day=True)

    connection = _connect()
    try:
        conditions = ["messages_fts MATCH ?"]
        parameters: list[Any] = [query]
        if since_ts:
            conditions.append("m.date_received >= ?")
            parameters.append(since_ts)
        if until_ts:
            conditions.append("m.date_received <= ?")
            parameters.append(until_ts)
        if account:
            conditions.append("EXISTS (SELECT 1 FROM locations l WHERE l.message = m.id"
                              " AND lower(l.account) = lower(?))")
            parameters.append(account)
        if mailbox:
            conditions.append("EXISTS (SELECT 1 FROM locations l WHERE l.message = m.id"
                              " AND lower(l.mailbox) = lower(?))")
            parameters.append(mailbox)
        if unread_only:
            conditions.append("EXISTS (SELECT 1 FROM locations l WHERE l.message = m.id"
                              " AND l.read = 0)")
        if flagged_only:
            conditions.append("EXISTS (SELECT 1 FROM locations l WHERE l.message = m.id"
                              " AND l.flagged = 1)")

        statement = (
            "SELECT m.id, m.account, m.subject, m.sender, m.date_received, m.rfc_id"
            "  FROM messages_fts f JOIN messages m ON m.id = f.rowid"
            f" WHERE {' AND '.join(conditions)}"
            " ORDER BY m.date_received DESC LIMIT ?"
        )
        used_query = query
        try:
            rows = connection.execute(statement, (*parameters, limit)).fetchall()
        except sqlite3.OperationalError:
            # The query was not valid FTS5 syntax; retry with the words quoted.
            used_query = _quote_terms(query)
            parameters[0] = used_query
            try:
                rows = connection.execute(statement, (*parameters, limit)).fetchall()
            except sqlite3.OperationalError as error:
                raise MailError("invalid_query", f"Unusable query: {error}") from error

        sizes = _mailbox_sizes(connection)
        messages = []
        for row in rows:
            locations = connection.execute(
                "SELECT account, mailbox, read, flagged FROM locations WHERE message = ?",
                (row["id"],),
            ).fetchall()
            chosen = _pick_location(locations, sizes)
            if chosen is None:
                continue
            reference = MessageReference(
                account=chosen["account"],
                mailbox=chosen["mailbox"],
                identifier=row["id"],
            )
            messages.append(
                {
                    "message_id": reference.encode(),
                    "mail_id": row["id"],
                    "subject": row["subject"] or "",
                    "sender": row["sender"] or "",
                    "date_received": datetime.fromtimestamp(
                        row["date_received"] or 0, tz=timezone.utc
                    ).astimezone().isoformat(),
                    "account": chosen["account"],
                    "mailbox": chosen["mailbox"],
                    "also_in": [
                        location["mailbox"]
                        for location in locations
                        if location["mailbox"] != chosen["mailbox"]
                    ],
                    "read": bool(chosen["read"]),
                    "flagged": bool(chosen["flagged"]),
                    "rfc_message_id": row["rfc_id"] or "",
                }
            )

        result: dict[str, Any] = {
            "ok": True,
            "query": query,
            "messages": messages,
            "indexed_messages": connection.execute(
                "SELECT count(*) FROM messages"
            ).fetchone()[0],
            "coverage": "all_indexed_mail",
            **freshness,
        }
        if used_query != query:
            result["interpreted_as"] = used_query
        return result
    finally:
        connection.close()


def get_thread(message_id: str, limit: int = 100) -> dict[str, Any]:
    """Returns every message of the conversation a message belongs to.

    Mail groups messages into conversations itself and the grouping is carried
    in the index, so the whole exchange comes back in one query — including the
    replies that were filed in another mailbox or sent from another account.
    """
    reference = MessageReference.decode(message_id)
    limit = max(1, min(int(limit), 500))

    connection = _connect()
    try:
        row = connection.execute(
            "SELECT conversation_id, subject FROM messages WHERE id = ?",
            (reference.identifier,),
        ).fetchone()
        if row is None:
            raise MailError(
                "not_indexed",
                "This message is not in the index.",
                "It may be newer than the last sync; run sync_index and try again.",
            )
        if row["conversation_id"] is None:
            raise MailError("no_thread", "Mail did not attach this message to a conversation.")

        sizes = _mailbox_sizes(connection)
        messages = []
        for message in connection.execute(
            "SELECT id, subject, sender, date_received FROM messages"
            " WHERE conversation_id = ? ORDER BY date_received ASC LIMIT ?",
            (row["conversation_id"], limit),
        ):
            locations = connection.execute(
                "SELECT account, mailbox, read, flagged FROM locations WHERE message = ?",
                (message["id"],),
            ).fetchall()
            chosen = _pick_location(locations, sizes)
            if chosen is None:
                continue
            messages.append(
                {
                    "message_id": MessageReference(
                        account=chosen["account"],
                        mailbox=chosen["mailbox"],
                        identifier=message["id"],
                    ).encode(),
                    "subject": message["subject"] or "",
                    "sender": message["sender"] or "",
                    "date_received": datetime.fromtimestamp(
                        message["date_received"] or 0, tz=timezone.utc
                    ).astimezone().isoformat(),
                    "account": chosen["account"],
                    "mailbox": chosen["mailbox"],
                    "read": bool(chosen["read"]),
                    "flagged": bool(chosen["flagged"]),
                    "is_requested": message["id"] == reference.identifier,
                }
            )
        return {
            "ok": True,
            "subject": row["subject"] or "",
            "message_count": len(messages),
            "messages": messages,
        }
    finally:
        connection.close()


def sync_index(timeout: int = 900) -> dict[str, Any]:
    """Brings the index up to date by running mail_index.py --sync.

    Needs Full Disk Access for the process running the MCP server, since it
    reads Mail's store. Without it, the index simply stops being refreshed;
    everything already indexed stays searchable.
    """
    import subprocess

    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mail_index.py")
    try:
        completed = subprocess.run(
            [sys.executable, script, "--sync"],
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise MailError(
            "sync_timeout",
            f"The sync exceeded {timeout} s.",
            "Run it from a terminal instead: python3 mail_index.py --sync",
        ) from error

    output = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip() or output
        if "Permission denied" in output or "Operation not permitted" in detail:
            raise MailError(
                "permission_denied",
                "Mail's store is not readable.",
                "Full Disk Access is needed to refresh the index. Grant it to the app running "
                "this server, or run: python3 mail_index.py --sync from a terminal that has it.",
            )
        raise MailError("sync_failed", detail[:500])

    # The periodic routine is also where stray .eml drafts get swept, so a
    # forgotten one never sits on disk indefinitely.
    purged: list[str] = []
    try:
        import mail_files

        purged = mail_files.purge_drafts()["removed"]
    except Exception:  # noqa: BLE001 - the index sync must not fail over this
        pass

    removed = 0
    added = 0
    for line in output.splitlines():
        match = re.search(r"removed (\d+) messages", line)
        if match:
            removed = int(match.group(1))
        match = re.search(r"indexed (\d+) messages", line)
        if match:
            added = int(match.group(1))
    result = {"ok": True, "added": added, "removed": removed, "log": output[-1000:]}
    if purged:
        result["purged_drafts"] = purged
    return result


def index_status() -> dict[str, Any]:
    """Reports what the index holds and how old it is."""
    connection = _connect()
    try:
        messages = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
        locations = connection.execute("SELECT count(*) FROM locations").fetchone()[0]
        span = connection.execute(
            "SELECT min(date_received), max(date_received) FROM messages WHERE date_received > 0"
        ).fetchone()
        built = connection.execute(
            "SELECT value FROM meta WHERE key = 'last_build'"
        ).fetchone()
        accounts = [
            {"account": row["account"], "messages": row["n"]}
            for row in connection.execute(
                "SELECT account, count(*) AS n FROM messages GROUP BY account ORDER BY n DESC"
            )
        ]
        built_at = int(built[0]) if built and built[0] else None
        return {
            "ok": True,
            "indexed_messages": messages,
            "mailbox_memberships": locations,
            "accounts": accounts,
            "oldest": time.strftime("%Y-%m-%d", time.localtime(span[0])) if span[0] else None,
            "newest": time.strftime("%Y-%m-%d", time.localtime(span[1])) if span[1] else None,
            "last_indexed": (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(built_at)) if built_at else None
            ),
            "age_hours": round((time.time() - built_at) / 3600, 1) if built_at else None,
            "database": INDEX_PATH,
            "size_mb": round(os.path.getsize(INDEX_PATH) / 1024 / 1024),
        }
    finally:
        connection.close()
