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
from typing import Any, Iterator, NamedTuple

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
    # server, whose guards only catch Exception.
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


# --------------------------------------------------------------------------
# Finding one message's file, and cutting a snippet out of it
# --------------------------------------------------------------------------

# Mail V10 keeps a message at
#   <account uuid>/<mailbox path>.mbox/<store uuid>/Data/<a>/<b>/<c>/Messages/<id>.emlx
# (or <id>.partial.emlx while the body is not downloaded), where a, b, c are the
# thousands, ten-thousands and hundred-thousands digits of the id. A message has
# one file in one mailbox only: a Gmail label or the "All Mail" view does not
# duplicate it, so the mailbox recorded in the index cannot give the path. The
# shard can, and it is tried under every mailbox root: a few dozen stat calls.
# Measured on a 54,000 file store the formula matched every file, while a full
# walk to find the same paths takes about a second.
# Snippets skip files larger than this: parsing a multi-megabyte message (big
# attachments inline) would blow the per-result time budget for a convenience.
SNIPPET_MAX_FILE_BYTES = 4 * 1024 * 1024
ROOT_CACHE_SECONDS = 600
ROOT_REFRESH_MIN_SECONDS = 60

_root_cache: dict[str, tuple[float, list[str]]] = {}


def shard_candidates(identifier: int) -> list[str]:
    """Relative Data sub-folders where the file of message `identifier` may be."""
    digits: list[str] = []
    rest = identifier // 1000
    while rest:
        digits.append(str(rest % 10))
        rest //= 10
    shards = ["/".join(digits[:3])]
    if len(digits) > 3:
        # Not seen on the stores measured so far; cheap insurance.
        shards.append("/".join(digits))
    return shards


def mailbox_roots(store: str) -> list[str]:
    """Every <mailbox>.mbox/<store uuid> directory, i.e. every place a Data folder lives."""
    roots: list[str] = []
    for directory, subdirectories, _ in os.walk(store, onerror=lambda error: None):
        if "Data" in subdirectories:
            roots.append(directory)
        subdirectories[:] = [
            name
            for name in subdirectories
            if not name.startswith(".") and name not in {"Data", "MailData", "Attachments"}
        ]
    return roots


def find_message_file(store: str, identifier: int) -> str | None:
    """Path of the .emlx for a message id, or None if it is not on disk.

    The mailbox roots are cached for a few minutes; a miss re-reads them at most
    once a minute, so a mailbox created since is found without every search for
    a not yet downloaded message paying for a walk.
    """
    now = time.monotonic()
    cached = _root_cache.get(store)
    fresh = False
    if cached is None or now - cached[0] > ROOT_CACHE_SECONDS:
        cached = (now, mailbox_roots(store))
        _root_cache[store] = cached
        fresh = True
    while True:
        for root in cached[1]:
            for shard in shard_candidates(identifier):
                folder = os.path.join(root, "Data", shard, "Messages")
                for suffix in (".emlx", ".partial.emlx"):
                    path = os.path.join(folder, f"{identifier}{suffix}")
                    if os.path.isfile(path):
                        return path
        if fresh or now - cached[0] < ROOT_REFRESH_MIN_SECONDS:
            return None
        cached = (now, mailbox_roots(store))
        _root_cache[store] = cached
        fresh = True


_QUERY_TOKEN = re.compile(r'"([^"]*)"|([^\s"()]+)')
_FTS_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


class _Fold(dict):
    """Character table that drops accents and case, one character for one.

    unicode61 folds "é" and "E" onto "e". Doing it per character keeps the
    folded text the same length as the original, so an offset found in one is
    valid in the other.
    """

    def __missing__(self, code: int) -> str:
        char = chr(code)
        folded = unicodedata.normalize("NFKD", char)[:1].lower()
        if len(folded) != 1:
            folded = char
        self[code] = folded
        return folded


_FOLD = _Fold()


def fold_text(text: str) -> str:
    return text.translate(_FOLD)


def query_terms(query: str) -> list[tuple[list[str], bool]]:
    """Reads the searchable words out of an FTS5 query.

    Returns (words, prefix) entries: a quoted phrase is one entry with several
    words, "term*" one entry flagged as a prefix. Operators, NEAR arguments,
    column filters ("subject:") and negated terms are left out.
    """
    terms: list[tuple[list[str], bool]] = []
    negate = False
    for match in _QUERY_TOKEN.finditer(query):
        phrase, bare = match.group(1), match.group(2)
        prefix = False
        if bare is not None:
            if bare in _FTS_OPERATORS:
                negate = bare == "NOT"
                continue
            if ":" in bare:
                bare = bare.split(":", 1)[1]
            prefix = bare.endswith("*")
            words = re.findall(r"\w+", fold_text(bare))
        else:
            words = re.findall(r"\w+", fold_text(phrase))
        if negate:
            negate = False
            continue
        if words:
            terms.append((words, prefix))
    return terms


def _first_hit(folded: str, terms: list[tuple[list[str], bool]]) -> int | None:
    best: int | None = None
    for words, prefix in terms:
        pattern = r"(?<!\w)" + r"\W+".join(re.escape(word) for word in words)
        if not prefix:
            pattern += r"(?!\w)"
        found = re.search(pattern, folded)
        if found and (best is None or found.start() < best):
            best = found.start()
    if best is None:
        # A phrase that is not there as such: fall back on its single words.
        for words, prefix in terms:
            if len(words) < 2:
                continue
            for word in words:
                found = re.search(r"(?<!\w)" + re.escape(word), folded)
                if found and (best is None or found.start() < best):
                    best = found.start()
    return best


def make_snippet(text: str, query: str, length: int = 200) -> str | None:
    """About `length` characters of `text` around the first query word.

    Matching ignores case and accents like the index does. Without a hit (the
    match was in the subject, the sender or an attachment name) the start of the
    text is returned. Cuts fall on word boundaries and are marked with "…".
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) <= length:
        return text
    hit = _first_hit(fold_text(text), query_terms(query))
    start = 0
    if hit is not None:
        start = max(0, hit - length // 3)
        # Begin on the sentence the hit belongs to when it starts close enough.
        boundary = max(text.rfind(mark, start, hit) for mark in (". ", "! ", "? "))
        if boundary != -1:
            start = boundary + 2
        elif start > 0 and text[start - 1] != " ":
            space = text.find(" ", start, hit)
            start = space + 1 if space != -1 else start
    end = min(len(text), start + length)
    if end < len(text):
        cut = text.rfind(" ", start, end + 1)
        if cut > start:
            end = cut
    snippet = text[start:end].strip()
    return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")


def message_snippet(store: str, identifier: int, query: str, length: int = 200) -> str | None:
    """Snippet for one message, or None when its file cannot be found or read."""
    path = find_message_file(store, identifier)
    if path is None:
        return None
    try:
        if os.path.getsize(path) > SNIPPET_MAX_FILE_BYTES:
            return None
    except OSError:
        return None
    _, body = extract_text(path)
    return make_snippet(body, query, length)


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


# A plain part shorter than this is suspect: multipart/alternative senders often
# ship an empty one, or a one-line notice, next to the real HTML body.
PLAIN_MIN_CHARS = 20
PLAIN_PLACEHOLDER_MAX_CHARS = 200
_PLAIN_PLACEHOLDER = re.compile(
    r"(?i)(html[- ](capable|enabled|only)|contains html|does not support html|"
    r"view (this|the) (message|email|e-mail) in (a|your) (web )?browser|"
    r"enable html|requires? an html)"
)
# Fewer word characters than this and a message counts as having no body.
BODY_MIN_WORD_CHARS = 3


def has_body(text: str) -> bool:
    """True when the extracted text is more than whitespace and stray punctuation."""
    return len(re.findall(r"\w", text)) >= BODY_MIN_WORD_CHARS


def _plain_is_unusable(plain_text: str) -> bool:
    """Empty, near-empty or a 'your client cannot show HTML' placeholder."""
    stripped = plain_text.strip()
    if len(stripped) < PLAIN_MIN_CHARS:
        return True
    return len(stripped) < PLAIN_PLACEHOLDER_MAX_CHARS and bool(_PLAIN_PLACEHOLDER.search(stripped))


class Extracted(NamedTuple):
    rfc_id: str
    body: str
    legacy_had_body: bool  # whether the pre-fallback logic would have found a body
    list_id: str  # normalized List-Id, '' when absent
    unsubscribe: bool  # a List-Unsubscribe header is present


_NOTHING = Extracted("", "", False, "", False)


def _extract(path: str) -> tuple[str, str, bool]:
    """(rfc id, body text, whether the pre-fallback logic would have found a body)."""
    found = extract_message(path)
    return found.rfc_id, found.body, found.legacy_had_body


def extract_message(path: str) -> Extracted:
    """Everything the index takes from one .emlx: body, message id and bulk markers."""
    raw = read_raw_message(path)
    if raw is None:
        return _NOTHING
    try:
        message = email.message_from_bytes(raw, policy=email.policy.default)
    except Exception:  # noqa: BLE001 - a malformed message still has a file
        return _NOTHING
    try:
        list_id = normalize_list_id(str(message.get("list-id") or ""))
        unsubscribe = bool(message.get("list-unsubscribe"))
    except Exception:  # noqa: BLE001 - a malformed header must not lose the body
        list_id, unsubscribe = "", False

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

    plain_text = "\n".join(plain)
    legacy = plain_text if plain else strip_markup("\n".join(markup))
    body = legacy
    if plain and markup and _plain_is_unusable(plain_text):
        # The plain part says nothing; the HTML part is the message. Only swap
        # when it actually holds more, so a genuine short reply is left alone.
        rendered = strip_markup("\n".join(markup))
        if len(rendered) > len(plain_text.strip()) or (
            len(plain_text.strip()) >= PLAIN_MIN_CHARS and has_body(rendered)
        ):
            body = rendered
    return Extracted(rfc_id, body[:BODY_LIMIT], has_body(legacy[:BODY_LIMIT]), list_id, unsubscribe)


def extract_text(path: str) -> tuple[str, str]:
    """Returns (rfc message id, body text) for one .emlx file.

    The text/plain part wins when it says something. When it is empty or a
    placeholder, the HTML part is used instead.
    """
    rfc_id, body, _ = _extract(path)
    return rfc_id, body


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


# recipients.type in Mail's Envelope Index: 0 is To, 1 is Cc. No other value
# occurs (a Bcc is never in a received message); anything unknown is skipped
# rather than filed under the wrong header.
RECIPIENT_TYPES = {0: "to", 1: "cc"}


def recipients_by_message(envelope: sqlite3.Connection) -> dict[int, list[tuple[str, str, str]]]:
    """Recipients per message as (kind, lower-cased address, display name), in header order."""
    result: dict[int, list[tuple[str, str, str]]] = {}
    for row in envelope.execute(
        "SELECT r.message, r.type, a.address, a.comment FROM recipients r"
        "  JOIN addresses a ON a.ROWID = r.address"
        " ORDER BY r.message, r.type, r.position"
    ):
        kind = RECIPIENT_TYPES.get(row["type"])
        if kind is None:
            continue
        result.setdefault(row["message"], []).append(
            (kind, (row["address"] or "").strip().lower(), (row["comment"] or "").strip())
        )
    return result


def recipient_text(recipients: list[tuple[str, str, str]], kind: str) -> str:
    """The searchable text of one recipient kind: display names and addresses."""
    return " ".join(
        f"{name} {address}".strip() for entry_kind, address, name in recipients if entry_kind == kind
    )


def address_domain(address: str) -> str:
    return address.rpartition("@")[2] if "@" in address else ""


def normalize_list_id(value: str | None) -> str:
    """The identifier inside <...> of a List-Id header, lower-cased; '' when absent.

    The text before the brackets is a free-form description, so only the part in
    brackets identifies the list. A bare value without brackets is taken as is.
    """
    if not value:
        return ""
    text = str(value).strip()
    match = re.search(r"<([^<>]*)>", text)
    return (match.group(1) if match else text).strip().lower()


def attachment_flags(envelope: sqlite3.Connection) -> set[int]:
    """Messages Mail records at least one real attachment for.

    The Envelope Index is the reliable source, including for .partial.emlx
    files whose attachments are stored apart from the message file. It also
    lists inline images, mostly signature logos, so a name only counts when it
    is not a png/gif/bmp/svg and not a client-generated inline name (image001,
    outlook-*, att0*). jpg/jpeg/heic stay: those are usually real photos.
    """
    return {
        row[0]
        for row in envelope.execute(
            "SELECT DISTINCT message FROM attachments"
            " WHERE name IS NOT NULL"
            "   AND lower(name) NOT GLOB '*.png' AND lower(name) NOT GLOB '*.gif'"
            "   AND lower(name) NOT GLOB '*.bmp' AND lower(name) NOT GLOB '*.svg'"
            "   AND lower(name) NOT GLOB 'image[0-9]*'"
            "   AND lower(name) NOT GLOB 'outlook-*'"
            "   AND lower(name) NOT GLOB 'att0*'"
        )
    }


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
    indexed_at        INTEGER NOT NULL,
    body_indexed      INTEGER NOT NULL DEFAULT 0,  -- 1 when a non-trivial body was extracted
    has_attachment    INTEGER NOT NULL DEFAULT 0,  -- Mail records an attachment for it
    list_id           TEXT,                        -- List-Id, inside <...>, lower-cased
    is_bulk           INTEGER NOT NULL DEFAULT 0   -- 1 when List-Id or List-Unsubscribe is present
);
CREATE INDEX IF NOT EXISTS messages_rfc ON messages(rfc_id);
CREATE INDEX IF NOT EXISTS messages_date ON messages(date_received);
CREATE INDEX IF NOT EXISTS messages_list_id ON messages(list_id);

-- Exact and domain filters (to:, cc:) run here; the FTS columns serve free-text
-- search. Addresses are lower-cased, domain is the part after the last @.
CREATE TABLE IF NOT EXISTS recipients (
    message   INTEGER NOT NULL,
    kind      TEXT NOT NULL,           -- 'to' or 'cc'
    address   TEXT NOT NULL,
    domain    TEXT NOT NULL,
    name      TEXT
);
CREATE INDEX IF NOT EXISTS recipients_message ON recipients(message);
CREATE INDEX IF NOT EXISTS recipients_address ON recipients(address, kind);
CREATE INDEX IF NOT EXISTS recipients_domain ON recipients(domain, kind);

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
    subject, sender, "to", cc, attachments, body,
    content='', contentless_delete=1
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


# Bumped whenever a change to the tables or to what gets indexed means an
# existing index must be rebuilt. An index with no version is version 1.
#   2: messages.body_indexed, HTML fallback for empty text/plain parts
#   3: To and Cc kept apart (FTS columns and a recipients table), has_attachment,
#      list_id and is_bulk
SCHEMA_VERSION = 3


class IndexSchemaError(Exception):
    """The index on disk was built by another version of this code."""

    def __init__(self, found: int, expected: int):
        super().__init__(f"index schema version {found}, this code needs {expected}")
        self.found = found
        self.expected = expected


def read_schema_version(connection: sqlite3.Connection) -> int | None:
    """The version stamped in an index, 1 for one that predates versioning,
    None when the file holds no index at all."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "messages" not in tables:
        return None
    if "meta" not in tables:
        return 1
    row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    try:
        return int(row[0]) if row else 1
    except (TypeError, ValueError):
        return 1


def open_index(path: str) -> sqlite3.Connection:
    """Opens (creating it if new) the index; refuses one from another schema version.

    Mixing versions would insert rows a stale table cannot hold, or leave old
    rows without the new columns' meaning, so the caller must rebuild instead.
    """
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    found = read_schema_version(connection)
    if found is not None and found != SCHEMA_VERSION:
        connection.close()
        raise IndexSchemaError(found, SCHEMA_VERSION)
    # A backfill is tens of thousands of inserts; the default journal makes it
    # fsync far more often than this workload needs.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
    )
    connection.commit()
    return connection


LOCK_STALE_SECONDS = 1800
LOCK_EXIT_CODE = 75


class IndexBusy(Exception):
    """Another sync or build holds the index lock."""


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class IndexLock:
    """Exclusive `<index>.sync.lock`, shared by --sync, --build and auto-sync.

    The lock file holds the owner's PID. It is stale when that process is gone,
    or when nobody has touched it for LOCK_STALE_SECONDS: a live owner keeps
    it fresh with touch(), so a long build is never mistaken for a dead one.
    """

    def __init__(self, database: str):
        self.path = database + ".sync.lock"
        self.held = False

    def _stale(self, path: str | None = None) -> bool:
        path = path or self.path
        try:
            age = time.time() - os.path.getmtime(path)
            with open(path, "r", encoding="ascii", errors="replace") as handle:
                text = handle.read().strip()
        except OSError:
            return False
        if text.isdigit() and not _process_alive(int(text)):
            return True
        return age > LOCK_STALE_SECONDS

    def _take_over(self) -> bool:
        """Claims a stale lock; True when this process removed it.

        Two processes may both judge the lock stale. Renaming it away is atomic,
        so only one rename succeeds; unlinking by name instead could delete the
        fresh lock the winner has just created.
        """
        claimed = f"{self.path}.stale.{os.getpid()}"
        try:
            os.rename(self.path, claimed)
        except FileNotFoundError:
            return False
        if not self._stale(claimed):
            # What we moved was a live lock created after our check: put it back.
            try:
                os.link(claimed, self.path)
            except OSError:
                pass
            os.unlink(claimed)
            return False
        os.unlink(claimed)
        return True

    def acquire(self) -> "IndexLock":
        for _ in range(2):
            try:
                handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._stale() and self._take_over():
                    continue
                raise IndexBusy(self.path) from None
            with os.fdopen(handle, "w", encoding="ascii") as out:
                out.write(str(os.getpid()))
            self.held = True
            return self
        raise IndexBusy(self.path)

    def touch(self) -> None:
        if self.held:
            try:
                os.utime(self.path)
            except OSError:
                pass

    def release(self) -> None:
        if self.held:
            self.held = False
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def __enter__(self) -> "IndexLock":
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def swap_in(temporary: str, target: str) -> None:
    """Atomically replaces the live index with a finished build.

    Call with the IndexLock held. The old file's WAL and shared-memory files
    belong to the old content: left next to the new file, SQLite could replay
    them into it. They go first, so that no window exists where the new file
    sits beside them; a reader that still has the old file open keeps its own
    inode and is unaffected.
    """
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(target + suffix)
        except FileNotFoundError:
            pass
    os.replace(temporary, target)


def build(
    files: dict[int, str],
    envelope: sqlite3.Connection,
    index: sqlite3.Connection,
    resume: bool,
    lock: IndexLock | None = None,
) -> dict[str, int]:
    mailboxes = load_mailboxes(envelope)
    account_names = load_account_names()

    print("reading Mail's index...")
    where = membership(envelope)
    recipients = recipients_by_message(envelope)
    attachments = attachments_by_message(envelope)
    with_attachment = attachment_flags(envelope)
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
    without_body = 0
    recovered = 0
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
        found = extract_message(path) if path else _NOTHING
        rfc_id, body, legacy_had_body = found.rfc_id, found.body, found.legacy_had_body
        if path is None:
            missing_file += 1
        body_indexed = has_body(body)
        if not body_indexed:
            without_body += 1
        elif not legacy_had_body:
            recovered += 1

        message_recipients = recipients.get(identifier, [])
        sender = row["sender"] or ""
        if row["sender_name"]:
            sender = f"{row['sender_name']} <{sender}>"

        index.execute(
            "INSERT OR REPLACE INTO messages"
            " (id, account, rfc_id, subject, sender, date_received, size, conversation_id, indexed_at,"
            "  body_indexed, has_attachment, list_id, is_bulk)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                int(body_indexed),
                int(identifier in with_attachment),
                found.list_id or None,
                int(bool(found.list_id or found.unsubscribe)),
            ),
        )
        index.execute("DELETE FROM recipients WHERE message = ?", (identifier,))
        index.executemany(
            "INSERT INTO recipients (message, kind, address, domain, name) VALUES (?,?,?,?,?)",
            [
                (identifier, kind, address, address_domain(address), name)
                for kind, address, name in message_recipients
            ],
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
            'INSERT INTO messages_fts (rowid, subject, sender, "to", cc, attachments, body)'
            " VALUES (?,?,?,?,?,?,?)",
            (
                identifier,
                row["subject"] or "",
                sender,
                recipient_text(message_recipients, "to"),
                recipient_text(message_recipients, "cc"),
                attachments.get(identifier, ""),
                body,
            ),
        )

        done += 1
        if done % 100 == 0:
            index.commit()
            if lock:
                lock.touch()
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
    print(f"{without_body} messages without a searchable body, "
          f"{recovered} recovered from the HTML part")
    return {"done": done, "missing_file": missing_file, "without_body": without_body, "recovered": recovered}


def refresh_locations(envelope: sqlite3.Connection, index: sqlite3.Connection) -> tuple[int, int]:
    """Brings read, flagged and mailbox membership of indexed messages up to date.

    build(resume=True) skips ids it already holds, so without this a message
    read, flagged or moved after indexing keeps its old state forever. Nothing
    here opens a message file: membership() and the messages table already
    carry everything, and the comparison happens in memory so that only rows
    that really changed are written.

    Returns (rows whose read/flagged changed, rows added or removed because a
    message changed mailbox).
    """
    # One read transaction: the three reads below must see the same snapshot,
    # or a Mail write landing between them looks like a mass move.
    began = not envelope.in_transaction
    if began:
        envelope.execute("BEGIN")
    try:
        mailboxes = load_mailboxes(envelope)
        where = membership(envelope)
        state = {
            row["ROWID"]: (row["read"], row["flagged"])
            for row in envelope.execute("SELECT ROWID, read, flagged FROM messages WHERE deleted = 0")
        }
    finally:
        if began:
            envelope.rollback()

    known: dict[tuple[int, str], tuple[Any, Any]] = {}
    known_names: dict[tuple[int, str], str] = {}  # location -> account name, for uuid lookup below
    for row in index.execute("SELECT message, mailbox, account, read, flagged FROM locations"):
        known[(row["message"], row["mailbox"])] = (row["read"], row["flagged"])
        known_names[(row["message"], row["mailbox"])] = row["account"]
    indexed = {row[0] for row in index.execute("SELECT id FROM messages")}

    wanted: dict[tuple[int, str], tuple[Any, Any]] = {}
    uuids: dict[tuple[int, str], str] = {}
    for identifier, mailbox_ids in where.items():
        if identifier not in indexed or identifier not in state:
            continue  # not indexed yet: build() will take care of it
        for mailbox_id in mailbox_ids:
            if mailbox_id in mailboxes:
                key = (identifier, mailboxes[mailbox_id][1])
                wanted[key] = state[identifier]
                uuids[key] = mailboxes[mailbox_id][0]

    # Account names without AppleScript: every existing location row whose
    # mailbox path belongs to exactly one account teaches that account's name.
    # An account never seen falls back to the uuid, as build() does.
    by_path: dict[str, set[str]] = {}
    for uuid, path in mailboxes.values():
        by_path.setdefault(path, set()).add(uuid)
    names: dict[str, str] = {}
    for (_, path), name in known_names.items():
        if len(by_path.get(path, ())) == 1:
            names[next(iter(by_path[path]))] = name

    updates = [
        (read, flagged, message, mailbox)
        for (message, mailbox), (read, flagged) in wanted.items()
        if (message, mailbox) in known and known[(message, mailbox)] != (read, flagged)
    ]
    inserts = [
        (message, names.get(uuids[(message, mailbox)], uuids[(message, mailbox)]), mailbox, read, flagged)
        for (message, mailbox), (read, flagged) in wanted.items()
        if (message, mailbox) not in known
    ]

    # Deleting is the dangerous half: Mail mid-sync can show empty or partial
    # mailboxes and labels. Only delete when the picture looks complete.
    deletes = []
    if not mailboxes or (known and not wanted):
        print("refresh: Mail's mailbox list looks empty or incomplete, skipping stale row removal")
    else:
        for key in known:
            if key in wanted or key[0] not in state:
                continue
            ids = where.get(key[0], set())
            if not ids or any(mailbox_id not in mailboxes for mailbox_id in ids):
                continue  # membership unknown or unparsable: keep what we have
            deletes.append(key)

    index.executemany("UPDATE locations SET read = ?, flagged = ? WHERE message = ? AND mailbox = ?", updates)
    index.executemany(
        "INSERT OR REPLACE INTO locations (message, account, mailbox, read, flagged) VALUES (?,?,?,?,?)",
        inserts,
    )
    index.executemany("DELETE FROM locations WHERE message = ? AND mailbox = ?", deletes)
    index.commit()
    return len(updates), len(inserts) + len(deletes)


def sync(
    files: dict[int, str],
    envelope: sqlite3.Connection,
    index: sqlite3.Connection,
    lock: IndexLock | None = None,
) -> None:
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
        index.execute("DELETE FROM recipients WHERE message = ?", (identifier,))
        index.execute("DELETE FROM messages_fts WHERE rowid = ?", (identifier,))
    index.commit()
    print(f"removed {len(gone)} messages that Mail no longer lists")

    flags, moved = refresh_locations(envelope, index)
    print(f"refreshed {flags} flags, {moved} mailbox changes")

    build(files, envelope, index, resume=True, lock=lock)


def search(index: sqlite3.Connection, query: str, limit: int, sort: str = "relevance") -> None:
    """Prints the hits; same ranking as mail_search.search_all."""
    if sort == "date":
        order, extra = "m.date_received DESC", []
    else:
        import mail_search  # deferred: keeps the indexer importable on its own

        weights = ", ".join(str(weight) for weight in mail_search.BM25_WEIGHTS)
        order = (
            f"bm25(messages_fts, {weights})"
            " * (1 + ? / (1 + max(? - coalesce(m.date_received, 0), 0) / 86400.0 / ?)),"
            " m.date_received DESC"
        )
        extra = [mail_search.RECENCY_BOOST, int(time.time()), mail_search.RECENCY_HALF_LIFE_DAYS]
    rows = index.execute(
        "SELECT m.id, m.account, m.subject, m.sender, m.date_received,"
        "       (SELECT group_concat(mailbox, ', ') FROM locations WHERE message = m.id) AS boxes"
        "  FROM messages_fts f JOIN messages m ON m.id = f.rowid"
        " WHERE messages_fts MATCH ?"
        f" ORDER BY {order} LIMIT ?",
        (query, *extra, limit),
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
    parser.add_argument("--sort", choices=("relevance", "date"), default="relevance",
                        help="order of --search hits (default: relevance)")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--force", action="store_true", help="build even if the checks fail")
    arguments = parser.parse_args()

    if arguments.search:
        try:
            index = open_index(arguments.database)
        except IndexSchemaError as error:
            print(f"{error}. Rebuild it: python3 mail_index.py --build")
            return 1
        search(index, arguments.search, arguments.limit, arguments.sort)
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

        lock = IndexLock(arguments.database)
        try:
            lock.acquire()
        except IndexBusy:
            print("Another sync or build is running on this index; try again when it ends.")
            return LOCK_EXIT_CODE
        try:
            if arguments.sync:
                index = open_index(arguments.database)
                sync(files, envelope, index, lock)
            else:
                # Built beside the live file and swapped in at the end, so a
                # search never meets a half-built index and a crash loses nothing.
                temporary = arguments.database + ".building"
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.unlink(temporary + suffix)
                    except FileNotFoundError:
                        pass
                index = open_index(temporary)
                build(files, envelope, index, resume=False, lock=lock)
                index.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                index.execute("PRAGMA journal_mode = DELETE")
                index.close()
                swap_in(temporary, arguments.database)
        except IndexSchemaError as error:
            print(f"{error}. Rebuild it: python3 mail_index.py --build")
            return 1
        finally:
            lock.release()
        size = os.path.getsize(arguments.database) / 1024 / 1024
        print(f"index: {arguments.database} ({size:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
