#!/usr/bin/env python3
"""Local search index over the whole Mail store, across every account.

Mail's AppleScript search only reaches a window of recent messages, because it
costs about a second per message body. This builds a SQLite index instead, fed
from two sources:

  - Mail's own index (MailData/Envelope Index) for metadata, mailbox membership,
    read and flag status. It is copied and opened read-only.
  - The .emlx files for the body text, which is indexed into FTS5 but never
    stored: a hit gives back a reference the MCP tools already know how to use,
    and the message itself is re-read from Mail on demand.

Needs Full Disk Access for whatever runs it (Terminal, for instance).

    python3 mail_index.py --check     # verify assumptions, touch nothing
    python3 mail_index.py --build     # full backfill
    python3 mail_index.py --sync      # incremental, safe to run often
    python3 mail_index.py --search "invoice acme"

The schema of Mail's internal index is undocumented and changes between macOS
releases, so --check runs first and --build refuses to start if it fails.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import html
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unicodedata
import urllib.parse
from typing import Any, Iterator

import config

MAIL_ROOT = config.get("mail_root")
DEFAULT_DATABASE = config.get("index_path")
BODY_LIMIT = config.get("body_limit")

# Mailbox urls look like imap://<account uuid>/<percent encoded path>.
MAILBOX_URL = re.compile(r"^(?P<scheme>imap|ews|local|pop)://(?P<account>[^/]+)/?(?P<path>.*)$")


# --------------------------------------------------------------------------
# Mail's store
# --------------------------------------------------------------------------


def find_store() -> str:
    # An ordinary exception, not SystemExit: this is also called from the MCP
    # server (through extract_attachments), whose guards only catch Exception.
    versions = sorted(
        entry for entry in os.listdir(MAIL_ROOT) if entry.startswith("V") and entry[1:].isdigit()
    )
    if not versions:
        raise FileNotFoundError(f"No versioned Mail store found under {MAIL_ROOT}.")
    return os.path.join(MAIL_ROOT, versions[-1])


def open_envelope(store: str, workspace: str) -> sqlite3.Connection:
    """Copies Mail's index aside and opens it read-only.

    Mail holds the database open with a write-ahead log, so the -wal and -shm
    files have to travel with it or recent changes are missing.
    """
    source = os.path.join(store, "MailData", "Envelope Index")
    if not os.path.isfile(source):
        # Same reason as find_store(): an ordinary exception, so a caller on
        # the server side can catch it and report it as data.
        raise FileNotFoundError(f"Mail's index is missing: {source}")
    target = os.path.join(workspace, "envelope.sqlite")
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(source + suffix):
            shutil.copy2(source + suffix, target + suffix)
    connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_mailboxes(envelope: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """Maps a mailbox rowid to (account uuid, slash separated path).

    Paths are percent encoded and in decomposed form in the url; they are
    recomposed here so they compare equal to what AppleScript reports.
    """
    mailboxes: dict[int, tuple[str, str]] = {}
    for row in envelope.execute("SELECT ROWID, url FROM mailboxes"):
        match = MAILBOX_URL.match(row["url"] or "")
        if not match:
            continue
        path = urllib.parse.unquote(match.group("path"))
        path = unicodedata.normalize("NFC", path)
        mailboxes[row["ROWID"]] = (match.group("account"), path)
    return mailboxes


def load_account_names() -> dict[str, str]:
    """Maps account uuids to the names Mail shows, via AppleScript.

    Falls back to the uuid itself: the index is still usable without names.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import mail_tools

        names = {}
        for record in mail_tools._parse_records(mail_tools.run_script("list_accounts", [])):
            if len(record) >= 2:
                names[record[1]] = record[0]
        return names
    except Exception as error:  # noqa: BLE001 - names are a convenience
        print(f"  (could not read account names: {error})")
        return {}


def scan_message_files(store: str) -> dict[int, str]:
    """Maps a message id to its .emlx path by walking the store once.

    Mail shards messages into Data/<digits>/Messages directories derived from
    the id, but the layout is undocumented; walking is a few seconds and does
    not depend on that formula holding.
    """
    files: dict[int, str] = {}
    for directory, subdirectories, filenames in os.walk(store, onerror=lambda error: None):
        subdirectories[:] = [name for name in subdirectories if not name.startswith(".")]
        for name in filenames:
            if not name.endswith(".emlx"):
                continue
            identifier = name.split(".")[0]
            if identifier.isdigit():
                files[int(identifier)] = os.path.join(directory, name)
    return files


def find_message_file(identifier: int) -> str | None:
    """Locates one message's .emlx file, stopping as soon as it is found.

    Mail shards messages by id, but the layout is undocumented, so the store is
    walked instead. Drafts sit in small directories and turn up quickly.
    """
    wanted = {f"{identifier}.emlx", f"{identifier}.partial.emlx"}
    for directory, subdirectories, filenames in os.walk(find_store(), onerror=lambda error: None):
        subdirectories[:] = [name for name in subdirectories if not name.startswith(".")]
        for name in filenames:
            if name in wanted:
                return os.path.join(directory, name)
    return None


def extract_attachments(identifier: int, destination: str | None = None) -> tuple[list[str], list[str]]:
    """Writes a message's real attachments to disk.

    Returns the paths written, and the names of the parts deliberately left
    behind. With no destination, nothing is written and the names are simply
    classified — which is what a preview needs.

    Mail's own "save" command on an attachment is refused by its sandbox
    (-10004) whatever the destination, so the parts are read out of the stored
    message instead. Needs Full Disk Access.

    A signature logo is a part with a file name like any other, but it belongs
    to the body and Mail counts it among the attachments; re-sending it as one
    would show the recipient a stray image. Telling the two apart is not
    obvious: AppleScript inserts a real attachment *into* the body, so it also
    ends up inline, with a Content-ID, referenced from the HTML. What separates
    them is how the HTML refers to it — an <img src="cid:…"> is part of the body,
    while an <object data="cid:…"> is an attachment Mail is merely showing.
    """
    path = find_message_file(identifier)
    if path is None:
        raise FileNotFoundError(f"No stored file found for message {identifier}")
    raw = read_raw_message(path)
    if raw is None:
        raise ValueError(f"Unreadable message file: {path}")

    message = email.message_from_bytes(raw, policy=email.policy.default)

    # Content-IDs the HTML body displays as images. Those, and only those,
    # belong to the body rather than to the list of attachments.
    body_images: set[str] = set()
    for part in message.walk():
        if part.get_content_type() != "text/html":
            continue
        try:
            html_text = (part.get_payload(decode=True) or b"").decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
        except LookupError:
            continue
        for cid in re.findall(r"<img[^>]+src=[\"']?cid:([^\"'>\s]+)", html_text, re.IGNORECASE):
            body_images.add(cid.strip())

    written: list[str] = []
    skipped: list[str] = []
    for index, part in enumerate(message.walk()):
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename and disposition != "attachment":
            continue
        content_id = (part.get("content-id") or "").strip().strip("<>")
        if disposition != "attachment" and content_id and content_id in body_images:
            skipped.append(filename or f"piece-{index}")
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        if destination is None:
            written.append(filename or f"piece-{index}")
            continue
        safe = os.path.basename(filename or f"piece-{index}")
        safe = re.sub(r"[/\\\x00]", "_", safe) or f"piece-{index}"
        target = os.path.join(destination, safe)
        # Two attachments can share a name; neither may overwrite the other.
        stem, extension = os.path.splitext(target)
        counter = 1
        while os.path.exists(target):
            target = f"{stem}-{counter}{extension}"
            counter += 1
        with open(target, "wb") as handle:
            handle.write(payload)
        written.append(target)
    return written, skipped


def read_raw_message(path: str) -> bytes | None:
    """Reads the RFC822 payload out of an .emlx file (byte count, message, plist)."""
    try:
        with open(path, "rb") as handle:
            try:
                length = int(handle.readline().strip())
            except ValueError:
                return None
            return handle.read(length)
    except OSError:
        return None


def extract_text(path: str) -> tuple[str, str]:
    """Returns (rfc message id, body text) for one .emlx file."""
    raw = read_raw_message(path)
    if raw is None:
        return "", ""
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:  # noqa: BLE001 - a malformed message still has a file
        return "", ""

    rfc_id = (message.get("message-id") or "").strip().strip("<>")
    plain: list[str] = []
    markup: list[str] = []
    for part in message.walk():
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        if part.get_filename():
            continue
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception:  # noqa: BLE001
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        (plain if content_type == "text/plain" else markup).append(text)

    body = "\n".join(plain) if plain else strip_markup("\n".join(markup))
    return rfc_id, body[:BODY_LIMIT]


def strip_markup(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# --------------------------------------------------------------------------
# Reading Mail's index
# --------------------------------------------------------------------------


def membership(envelope: sqlite3.Connection) -> dict[int, set[int]]:
    """Every mailbox a message belongs to.

    The primary mailbox is a column on the message; Gmail labels and any other
    extra mailbox live in the labels table. Neither is complete on its own.
    """
    result: dict[int, set[int]] = {}
    for row in envelope.execute("SELECT ROWID, mailbox FROM messages WHERE deleted = 0"):
        result.setdefault(row["ROWID"], set()).add(row["mailbox"])
    for row in envelope.execute("SELECT message_id, mailbox_id FROM labels"):
        result.setdefault(row["message_id"], set()).add(row["mailbox_id"])
    return result


def message_rows(envelope: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    yield from envelope.execute(
        "SELECT m.ROWID AS id, m.date_received, m.size, m.conversation_id,"
        "       m.read, m.flagged, m.mailbox,"
        "       s.subject AS subject, a.address AS sender, a.comment AS sender_name"
        "  FROM messages m"
        "  LEFT JOIN subjects s ON s.ROWID = m.subject"
        "  LEFT JOIN addresses a ON a.ROWID = m.sender"
        " WHERE m.deleted = 0"
    )


def recipients_by_message(envelope: sqlite3.Connection) -> dict[int, str]:
    result: dict[int, list[str]] = {}
    for row in envelope.execute(
        "SELECT r.message, a.address, a.comment FROM recipients r"
        "  JOIN addresses a ON a.ROWID = r.address"
    ):
        entry = row["address"] or ""
        if row["comment"]:
            entry = f"{row['comment']} {entry}"
        result.setdefault(row["message"], []).append(entry)
    return {key: " ".join(value) for key, value in result.items()}


def attachments_by_message(envelope: sqlite3.Connection) -> dict[int, str]:
    result: dict[int, list[str]] = {}
    for row in envelope.execute("SELECT message, name FROM attachments WHERE name IS NOT NULL"):
        result.setdefault(row["message"], []).append(row["name"])
    return {key: " ".join(value) for key, value in result.items()}


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def run_checks(store: str, envelope: sqlite3.Connection, files: dict[int, str]) -> bool:
    ok = True

    def report(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  [{'ok' if passed else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))

    print("checking Mail's index against what the indexer expects:")

    tables = {
        row[0] for row in envelope.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {"messages", "mailboxes", "labels", "subjects", "addresses", "recipients"}
    report("expected tables present", required <= tables, ", ".join(sorted(required - tables)))
    if not required <= tables:
        return False

    mailboxes = load_mailboxes(envelope)
    total_mailboxes = envelope.execute("SELECT count(*) FROM mailboxes").fetchone()[0]
    report("mailbox urls parse", len(mailboxes) == total_mailboxes,
           f"{len(mailboxes)}/{total_mailboxes}")

    total_messages = envelope.execute(
        "SELECT count(*) FROM messages WHERE deleted = 0"
    ).fetchone()[0]
    known = sum(
        1
        for row in envelope.execute("SELECT ROWID FROM messages WHERE deleted = 0")
        if row[0] in files
    )
    ratio = known / total_messages if total_messages else 0
    report("message ids map to .emlx files", ratio > 0.9,
           f"{known}/{total_messages} ({ratio:.0%})")

    # The decisive one: rebuilt membership has to agree with Mail's own totals.
    counts: dict[int, int] = {}
    for mailbox_ids in membership(envelope).values():
        for mailbox_id in mailbox_ids:
            counts[mailbox_id] = counts.get(mailbox_id, 0) + 1
    checked = 0
    agreeing = 0
    for row in envelope.execute(
        "SELECT ROWID, url, total_count FROM mailboxes WHERE total_count > 100"
    ):
        expected = row["total_count"]
        got = counts.get(row["ROWID"], 0)
        checked += 1
        if expected and abs(got - expected) / expected < 0.02:
            agreeing += 1
        else:
            path = mailboxes.get(row["ROWID"], ("", row["url"]))[1]
            print(f"        {path}: rebuilt {got}, Mail says {expected}")
    report("membership matches Mail's counts", checked and agreeing == checked,
           f"{agreeing}/{checked} mailboxes")

    sample = [
        row[0]
        for row in envelope.execute("SELECT ROWID FROM messages WHERE deleted = 0 LIMIT 200")
        if row[0] in files
    ][:50]
    with_id = 0
    with_body = 0
    for identifier in sample:
        rfc_id, body = extract_text(files[identifier])
        with_id += bool(rfc_id)
        with_body += bool(body)
    report("RFC Message-ID readable from files", with_id > len(sample) * 0.9,
           f"{with_id}/{len(sample)}")
    report("body text readable from files", with_body > len(sample) * 0.9,
           f"{with_body}/{len(sample)}")

    dates = envelope.execute(
        "SELECT min(date_received), max(date_received) FROM messages WHERE date_received > 0"
    ).fetchone()
    plausible = bool(dates[0]) and 10**8 < dates[1] < 10**10
    report("date_received looks like a unix timestamp", plausible,
           f"{time.strftime('%Y-%m-%d', time.localtime(dates[0]))} to "
           f"{time.strftime('%Y-%m-%d', time.localtime(dates[1]))}" if plausible else str(dates))
    return ok


# --------------------------------------------------------------------------
# Our index
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY,   -- Mail's own message id
    account           TEXT NOT NULL,
    rfc_id            TEXT,                  -- durable across moves and accounts
    subject           TEXT,
    sender            TEXT,
    date_received     INTEGER,
    size              INTEGER,
    conversation_id   INTEGER,
    indexed_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_rfc ON messages(rfc_id);
CREATE INDEX IF NOT EXISTS messages_date ON messages(date_received);

CREATE TABLE IF NOT EXISTS locations (
    message   INTEGER NOT NULL,
    account   TEXT NOT NULL,
    mailbox   TEXT NOT NULL,
    read      INTEGER,
    flagged   INTEGER,
    PRIMARY KEY (message, mailbox)
);
CREATE INDEX IF NOT EXISTS locations_mailbox ON locations(account, mailbox);

-- Indexed but not stored: the body is searchable, never kept. A hit returns a
-- reference and the message itself is re-read from Mail on demand.
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, sender, recipients, attachments, body,
    content='', contentless_delete=1
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def open_index(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    # A backfill is tens of thousands of inserts; the default journal makes it
    # fsync far more often than this workload needs.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript(SCHEMA)
    return connection


def build(
    files: dict[int, str],
    envelope: sqlite3.Connection,
    index: sqlite3.Connection,
    resume: bool,
) -> None:
    mailboxes = load_mailboxes(envelope)
    account_names = load_account_names()

    print("reading Mail's index...")
    where = membership(envelope)
    recipients = recipients_by_message(envelope)
    attachments = attachments_by_message(envelope)
    rows = list(message_rows(envelope))
    print(f"  {len(rows)} messages")

    already: set[int] = set()
    if resume:
        already = {row[0] for row in index.execute("SELECT id FROM messages")}
        if already:
            print(f"  {len(already)} already indexed, skipping them")

    started = time.time()
    done = 0
    missing_file = 0
    for row in rows:
        identifier = row["id"]
        if identifier in already:
            continue
        mailbox_ids = where.get(identifier, set())
        account_uuid = ""
        for mailbox_id in mailbox_ids:
            if mailbox_id in mailboxes:
                account_uuid = mailboxes[mailbox_id][0]
                break
        account = account_names.get(account_uuid, account_uuid)

        path = files.get(identifier)
        rfc_id, body = extract_text(path) if path else ("", "")
        if path is None:
            missing_file += 1

        sender = row["sender"] or ""
        if row["sender_name"]:
            sender = f"{row['sender_name']} <{sender}>"

        index.execute(
            "INSERT OR REPLACE INTO messages"
            " (id, account, rfc_id, subject, sender, date_received, size, conversation_id, indexed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                account,
                rfc_id or None,
                row["subject"],
                sender,
                row["date_received"],
                row["size"],
                row["conversation_id"],
                int(started),
            ),
        )
        index.execute("DELETE FROM locations WHERE message = ?", (identifier,))
        for mailbox_id in mailbox_ids:
            if mailbox_id not in mailboxes:
                continue
            uuid, mailbox_path = mailboxes[mailbox_id]
            index.execute(
                "INSERT OR REPLACE INTO locations (message, account, mailbox, read, flagged)"
                " VALUES (?,?,?,?,?)",
                (
                    identifier,
                    account_names.get(uuid, uuid),
                    mailbox_path,
                    row["read"],
                    row["flagged"],
                ),
            )
        index.execute("DELETE FROM messages_fts WHERE rowid = ?", (identifier,))
        index.execute(
            "INSERT INTO messages_fts (rowid, subject, sender, recipients, attachments, body)"
            " VALUES (?,?,?,?,?,?)",
            (
                identifier,
                row["subject"] or "",
                sender,
                recipients.get(identifier, ""),
                attachments.get(identifier, ""),
                body,
            ),
        )

        done += 1
        if done % 100 == 0:
            index.commit()
            rate = done / max(time.time() - started, 0.001)
            remaining = (len(rows) - len(already) - done) / max(rate, 0.001)
            print(f"  {done} indexed, {rate:.0f}/s, about {remaining / 60:.0f} min left")

    index.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_build', ?)", (str(int(time.time())),)
    )
    index.commit()
    elapsed = time.time() - started
    print(f"\nindexed {done} messages in {elapsed / 60:.1f} min")
    if missing_file:
        print(f"{missing_file} messages had no file on disk: metadata only, no body search")


def sync(files: dict[int, str], envelope: sqlite3.Connection, index: sqlite3.Connection) -> None:
    """Brings the index back in line: new messages in, gone ones out.

    Removal is not a special case. What Mail's index no longer lists is deleted
    here too, and a message that changed mailbox simply gets new locations.
    """
    live = {row[0] for row in envelope.execute("SELECT ROWID FROM messages WHERE deleted = 0")}
    held = {row[0] for row in index.execute("SELECT id FROM messages")}

    gone = held - live
    for identifier in gone:
        index.execute("DELETE FROM messages WHERE id = ?", (identifier,))
        index.execute("DELETE FROM locations WHERE message = ?", (identifier,))
        index.execute("DELETE FROM messages_fts WHERE rowid = ?", (identifier,))
    index.commit()
    print(f"removed {len(gone)} messages that Mail no longer lists")

    build(files, envelope, index, resume=True)


def search(index: sqlite3.Connection, query: str, limit: int) -> None:
    rows = index.execute(
        "SELECT m.id, m.account, m.subject, m.sender, m.date_received,"
        "       (SELECT group_concat(mailbox, ', ') FROM locations WHERE message = m.id) AS boxes"
        "  FROM messages_fts f JOIN messages m ON m.id = f.rowid"
        " WHERE messages_fts MATCH ?"
        " ORDER BY m.date_received DESC LIMIT ?",
        (query, limit),
    ).fetchall()
    print(f"{len(rows)} hit(s)\n")
    for row in rows:
        when = time.strftime("%Y-%m-%d", time.localtime(row["date_received"] or 0))
        print(f"{when}  {(row['subject'] or '')[:70]}")
        print(f"            {(row['sender'] or '')[:60]}  [{row['account']}]")
        print(f"            {row['boxes']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify assumptions and stop")
    parser.add_argument("--build", action="store_true", help="full backfill")
    parser.add_argument("--sync", action="store_true", help="incremental update")
    parser.add_argument("--search", metavar="QUERY", help="query the index")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--force", action="store_true", help="build even if the checks fail")
    arguments = parser.parse_args()

    if arguments.search:
        index = open_index(arguments.database)
        search(index, arguments.search, arguments.limit)
        return 0

    if not (arguments.check or arguments.build or arguments.sync):
        parser.error("pick one of --check, --build, --sync, --search")

    try:
        store = find_store()
    except PermissionError:
        print("Permission denied on ~/Library/Mail.")
        print("Grant Full Disk Access to the app running this script, then try again.")
        return 1
    except FileNotFoundError as error:
        print(error)
        return 1

    with tempfile.TemporaryDirectory() as workspace:
        try:
            envelope = open_envelope(store, workspace)
        except FileNotFoundError as error:
            print(error)
            return 1

        print("scanning message files...")
        files = scan_message_files(store)
        print(f"  {len(files)} files\n")

        if arguments.check or arguments.build:
            passed = run_checks(store, envelope, files)
            print()
            if arguments.check:
                print("all checks passed: the index can be built."
                      if passed else "some checks failed: see above.")
                return 0 if passed else 1
            if not passed and not arguments.force:
                print("Refusing to build on assumptions that do not hold. Use --force to override.")
                return 1

        index = open_index(arguments.database)
        if arguments.sync:
            sync(files, envelope, index)
        else:
            build(files, envelope, index, resume=True)
        size = os.path.getsize(arguments.database) / 1024 / 1024
        print(f"index: {arguments.database} ({size:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
