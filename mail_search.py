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
import mail_operators
import mail_stem
from mail_tools import MailError, MessageReference

INDEX_PATH = config.get("index_path")

# Mailboxes a Gmail account duplicates everything into. A message is reachable
# through any of its mailboxes, but Mail resolves an id far faster in a small
# one, so these are the last resort when picking a reference.
BULK_MAILBOXES = ("[Gmail]/Tous les messages", "[Gmail]/All Mail", "[Gmail]/Important")


# Ranking. The FTS5 columns are (subject, sender, to, cc, attachments, body,
# subject_stem, attachments_stem, body_stem); bm25() takes one weight per column,
# in that order. A word in the subject says what a mail is about, in the sender
# it says who wrote it, in an attachment name it says what was sent; To/Cc and
# body match far more loosely. The stem columns count for a third to a quarter of
# their raw twin (tuned on the eval pairs): a message holding the word as typed matches in both and ranks above
# one holding only another inflection.
BM25_WEIGHTS = (10.0, 5.0, 1.0, 1.0, 3.0, 1.0, 3.0, 1.0, 0.25)
# Recency bonus, applied as a multiplier on the (negative) bm25 score:
#   score = bm25 * (1 + RECENCY_BOOST / (1 + age_days / RECENCY_HALF_LIFE_DAYS))
# A mail received today gets its score boosted by 30 %, one a year old by 15 %,
# so between two equally good matches the newer wins, but a strong old match
# (score several times larger) still beats a weak recent one.
RECENCY_BOOST = 0.3
RECENCY_HALF_LIFE_DAYS = 365.0
SORT_MODES = ("relevance", "date")
# "auto" is only a setting (search_mode): hybrid when the vectors are ready.
SEARCH_MODES = ("keyword", "semantic", "hybrid", "auto")
# Reciprocal Rank Fusion: a message scores the sum of 1 / (RRF_K + rank) over the
# rankings that hold it. 60 is the constant of the original paper; it keeps a
# first place from drowning everything below it.
RRF_K = 60
# The semantic ranking counts a quarter of the keyword one in the fusion. With
# equal weights the nearest neighbours of a two-word query (loose by nature) pushed
# exact matches down: MRR on the eval pairs fell from 0.467 to 0.410. At 0.25 exact
# matches keep their place (0.440, recall@10 unchanged) while the meaning still
# lifts a message both rankings hold, and fills the list when few words match: on
# the pairs whose query words are inflected, MRR rises from 0.330 to 0.358. Chosen
# on a grid of weights, thresholds and caps (see the README).
RRF_SEMANTIC_WEIGHT = 0.25
# Semantic candidates below this cosine similarity are not returned: the nearest
# neighbours of a query about nothing in the mailbox are noise, not results.
MIN_SIMILARITY = 0.0
# Chunks compared per query before they are folded into one score per message.
SEMANTIC_CHUNKS = 200
# An attachment's text is a weaker witness than the message itself: its bm25
# (computed in attachments.sqlite, on another corpus) counts for half when it is
# added to the message's own score, or alone for a message matched only through
# an attachment.
ATTACHMENT_WEIGHT = 0.5
# Attachment hits pulled in before the message filters (dates, accounts,
# operators) are applied to them.
ATTACHMENT_CANDIDATES = 300
# Largest set of message ids handed to the attachment query for a filtered search.
ATTACHMENT_ID_LIMIT = 50_000


def _require_current_schema(connection: sqlite3.Connection) -> None:
    """Refuses an index built by another version of the code.

    Rebuilding on the fly would hold a search for many minutes and need Full
    Disk Access, so the caller is told to rebuild rather than surprised.
    """
    import mail_index

    try:
        found = mail_index.read_schema_version(connection)
    except sqlite3.DatabaseError:
        found = None
    if found is None:
        connection.close()
        raise MailError(
            "index_invalid",
            "The index file holds no message table (empty or not an index).",
            "Rebuild it: python3 mail_index.py --build",
        )
    if found != mail_index.SCHEMA_VERSION:
        connection.close()
        raise MailError(
            "index_outdated",
            f"The search index uses schema version {found}, this server needs "
            f"{mail_index.SCHEMA_VERSION}.",
            "Rebuild it: python3 mail_index.py --build (the current index keeps working "
            "until the new one is ready).",
        )


def _connect() -> sqlite3.Connection:
    if not os.path.isfile(INDEX_PATH):
        raise MailError(
            "index_missing",
            "The search index has not been built yet.",
            "Run: python3 mail_index.py --build (needs Full Disk Access).",
        )
    connection = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    _require_current_schema(connection)
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


def _legacy_column_filters(query: str) -> str:
    """Rewrites the old `recipients:` column filter to the To and Cc columns.

    The FTS column `recipients` was split into `to` and `cc` (schema v3); FTS5
    accepts a column set, `{to cc}: word`, which keeps old queries working.
    """
    return re.sub(r"(?<![\w\"])recipients\s*:", "{to cc}:", query, flags=re.IGNORECASE)


_OWN_ADDRESSES: list[str] | None = None


def _own_addresses() -> list[str]:
    """Addresses of the user's Mail accounts, for from:me / to:me.

    Asked to Mail once per process (an AppleScript round trip) and cached: the
    set of accounts hardly ever changes while the server runs. Only queries
    that use `me` pay for it; a Mail that cannot answer fails that query alone.
    """
    global _OWN_ADDRESSES
    if _OWN_ADDRESSES is None:
        import mail_draft

        _OWN_ADDRESSES = sorted(
            {address for account in mail_draft.accounts() for address in account["addresses"]}
        )
    return _OWN_ADDRESSES


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

    try:
        result = sync_index()
        return {
            "synced": True,
            "index_age_minutes": 0.0,
            "sync_added": result["added"],
            "sync_removed": result["removed"],
        }
    except MailError as error:
        if error.code == "index_busy":
            return {"synced": False, "index_age_minutes": round(age, 1), "sync_note": "already running"}
        return {
            "synced": False,
            "index_age_minutes": round(age, 1),
            "sync_note": f"{error.code}: {error.message}",
            "sync_hint": error.hint,
        }


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


def _mail_store() -> str | None:
    import mail_index

    try:
        return mail_index.find_store()
    except OSError:
        return None


def _snippet(store: str | None, identifier: int, query: str) -> str | None:
    """A result's snippet; None whenever it cannot be had, never an error.

    Mail may have moved or purged the file since indexing, or not downloaded the
    body yet: the result is still worth returning without it.
    """
    if store is None:
        return None
    import mail_index

    try:
        return mail_index.message_snippet(store, identifier, query)
    except Exception:  # noqa: BLE001 - a snippet is a convenience
        return None


def _attachment_snippet(hit: Any, query: str) -> str | None:
    """Excerpt of the matched attachment's text; None whenever it cannot be had."""
    import mail_attachments
    import mail_index

    try:
        return mail_index.make_snippet(mail_attachments.attachment_text(hit.attachment), query)
    except Exception:  # noqa: BLE001 - a snippet is a convenience
        return None


def _recency_factor(date_received: int | None) -> float:
    """The multiplier the relevance ORDER BY applies, for scores computed here."""
    age_days = max(time.time() - (date_received or 0), 0) / 86400.0
    return 1 + RECENCY_BOOST / (1 + age_days / RECENCY_HALF_LIFE_DAYS)


def _merge_attachment_hits(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    hits: dict[int, Any],
    filter_conditions: list[str],
    filter_parameters: list[Any],
    name_clause: str,
    sort: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Message rows ranked with what their attachments said.

    `rows` are the messages matched on their own fields. Messages matched only
    through an attachment are fetched here under the same filters (operators,
    dates, account...) so an attachment never bypasses them. Relevance: the
    message's recency-adjusted bm25 plus ATTACHMENT_WEIGHT times the attachment's,
    both negative, the lowest first. Date: newest first.
    """
    merged: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = dict(row)
        entry["only_attachment"] = False
        entry["score"] = (entry.pop("bm25_score", 0.0) or 0.0) * _recency_factor(entry["date_received"])
        merged[entry["id"]] = entry

    missing = [identifier for identifier in hits if identifier not in merged]
    if missing:
        conditions = list(filter_conditions)
        parameters = list(filter_parameters)
        if name_clause:
            conditions.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            parameters.append(name_clause)
        marks = ",".join("?" * len(missing))
        conditions.append(f"m.id IN ({marks})")
        statement = (
            "SELECT m.id, m.account, m.subject, m.sender, m.date_received, m.rfc_id,"
            "       m.has_attachment, m.is_bulk FROM messages m WHERE " + " AND ".join(conditions)
        )
        for row in connection.execute(statement, (*parameters, *missing)).fetchall():
            entry = dict(row)
            entry["only_attachment"] = True
            entry["score"] = 0.0
            merged[entry["id"]] = entry

    for identifier, entry in merged.items():
        hit = hits.get(identifier)
        if hit is not None:
            entry["score"] += ATTACHMENT_WEIGHT * hit.score * _recency_factor(entry["date_received"])
    if sort == "date":
        ordered = sorted(merged.values(), key=lambda entry: -(entry["date_received"] or 0))
    else:
        ordered = sorted(merged.values(), key=lambda entry: (entry["score"], -(entry["date_received"] or 0)))
    return ordered[:limit]


def _match_expression(text: str, has_text: bool, name_clause: str) -> str:
    """The FTS5 MATCH expression for the free text plus the filename: clauses."""
    parts = []
    if has_text:
        parts.append(f"({mail_stem.rewrite_query(text)})" if name_clause else mail_stem.rewrite_query(text))
    if name_clause:
        parts.append(name_clause)
    return " AND ".join(parts)


def _message_filters(
    parsed: mail_operators.ParsedQuery,
    since_ts: int | None,
    until_ts: int | None,
    account: str | None,
    mailbox: str | None,
    unread_only: bool,
    flagged_only: bool,
) -> tuple[str, list[str], list[Any]]:
    """Everything that narrows a search except the free text, as SQL on `messages m`.

    Shared by search_all and aggregate so both read a query and its filters the
    same way. Returns (filename FTS clause or "", conditions, parameters); the
    clause is separate because it is a MATCH and must join the free text's one.
    """
    operator_conditions, operator_parameters, wanted_names, unwanted_names = (
        mail_operators.build_conditions(parsed.filters, _own_addresses)
    )
    name_clause = " AND ".join(mail_operators.filename_match(name) for name in wanted_names)
    conditions: list[str] = []
    parameters: list[Any] = []
    for name in unwanted_names:
        conditions.append("m.id NOT IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
        parameters.append(mail_operators.filename_match(name))
    conditions.extend(operator_conditions)
    parameters.extend(operator_parameters)
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
    return name_clause, conditions, parameters


_FIELD_PREFIX = re.compile(
    r"\{[^{}]*\}\s*:|(?<![\w])(?:subject|sender|to|cc|attachments|recipients|body)(?:_stem)?\s*:", re.I)
_NEGATED_TERM = re.compile(r"\bNOT\s+(?:\"[^\"]*\"|\S+)")


def semantic_text(text: str) -> str:
    """The free text of a query as plain words for the embedding model.

    FTS5 syntax means nothing to it: column prefixes, quotes, parentheses and
    AND / OR are dropped, and so is a term negated with NOT (it says what the
    message is not about, which a vector cannot express).
    """
    text = _NEGATED_TERM.sub(" ", text)
    text = _FIELD_PREFIX.sub(" ", text)
    text = re.sub(r"[\"()*^]", " ", text)
    text = re.sub(r"\b(?:AND|OR|NEAR)\b", " ", text)
    return " ".join(text.split())


def _allowed_ids(
    connection: sqlite3.Connection,
    filters: list[str],
    filter_parameters: list[Any],
    name_clause: str,
) -> tuple[list[int] | None, bool]:
    """Ids of the messages passing the filters, for a side index that ranks apart.

    Filters (dates, account, operators) apply to messages, not to attachments or
    vectors: capping by score first would drop the hits of a narrow filter. So the
    other index is restricted to these ids. Returns (None, False) when there is no
    filter, and (None, True) when the ids are too many to pass along: the caller
    then widens its own cap and filters afterwards.
    """
    if not filters and not name_clause:
        return None, False
    id_conditions = list(filters)
    id_parameters = list(filter_parameters)
    if name_clause:
        id_conditions.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
        id_parameters.append(name_clause)
    ids = [row[0] for row in connection.execute(
        "SELECT m.id FROM messages m WHERE " + " AND ".join(id_conditions) + " LIMIT ?",
        (*id_parameters, ATTACHMENT_ID_LIMIT + 1),
    )]
    if len(ids) <= ATTACHMENT_ID_LIMIT:
        return ids, False
    return None, True


def _vectors_ready(connection: sqlite3.Connection) -> bool:
    """Whether "auto" may use the vectors: built by this recipe, covering the index, loadable."""
    try:
        import mail_vectors

        path = mail_vectors.database_path()
        if not os.path.isfile(path):
            return False
        total = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
        info = mail_vectors.status(total, path, probe=False)
        return bool(info.get("fresh") and info.get("sqlite_vec"))
    except Exception:  # noqa: BLE001 - a side index must never break keyword search
        return False


def _coverage_note(connection: sqlite3.Connection) -> str | None:
    """A warning when an explicitly requested semantic search runs on a partial vectors file."""
    try:
        import mail_vectors

        total = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
        info = mail_vectors.status(total, probe=False)
        coverage = info.get("coverage")
        if coverage is not None and coverage < mail_vectors.FRESH_COVERAGE:
            return (f"vectors cover only {coverage:.0%} of the index: meaning was searched on that part "
                    "(finish it with mail_vectors.py --sync)")
    except Exception:  # noqa: BLE001 - a note is a convenience
        pass
    return None


def _effective_mode(requested: str, has_text: bool, connection: sqlite3.Connection) -> str:
    """The mode actually run: "auto" resolved, and keyword when there is no text to compare."""
    if not has_text:
        return "keyword"
    if requested == "auto":
        return "hybrid" if _vectors_ready(connection) else "keyword"
    return requested


def _semantic_ranking(text: str, id_scope: Any) -> list[Any]:
    """Messages close in meaning to `text`, best first; MailError when it cannot be done."""
    import mail_vectors

    if not text.strip():
        return []
    try:
        vector = mail_vectors.embed_query(text)
    except mail_vectors.EmbedderError as error:
        raise MailError(
            "semantic_unavailable",
            f"The query could not be embedded: {error}",
            error.hint or 'Use mode="keyword", or start Ollama with the embedding model.',
        ) from error
    allowed, narrow = id_scope()
    hits = mail_vectors.search_hits(
        vector, allowed, SEMANTIC_CHUNKS * 5 if narrow else SEMANTIC_CHUNKS)
    return [hit for hit in hits if hit.score >= MIN_SIMILARITY]


def _fuse(
    connection: sqlite3.Connection,
    keyword_rows: list[Any],
    hits: list[Any],
    mode: str,
    filter_conditions: list[str],
    filter_parameters: list[Any],
    name_clause: str,
    limit: int,
    sort: str,
) -> list[dict[str, Any]]:
    """The keyword and semantic rankings as one list, by Reciprocal Rank Fusion.

    A message scores the sum of weight / (RRF_K + rank) over the rankings holding
    it (weight 1 for keywords, RRF_SEMANTIC_WEIGHT for meaning in a hybrid search),
    so one seen by both rises above one seen by either alone, and the two scales
    (bm25, cosine) never have to be compared. Semantic hits are read from the
    index under the same filters as keyword ones, so a filter cannot be bypassed
    by meaning. With mode "semantic" the keyword list is empty. Returns at most
    `limit` rows, each tagged "match" (keyword / semantic / both) and, when the
    meaning found it, "similarity" and "chunk"; sort="date" reorders that top.
    """
    entries: dict[int, dict[str, Any]] = {}
    for rank, row in enumerate(keyword_rows, 1):
        entry = dict(row)
        entry["match"] = "keyword"
        entry["rrf"] = 1.0 / (RRF_K + rank)
        entries[entry["id"]] = entry

    missing = [hit.message for hit in hits if hit.message not in entries]
    if missing:
        conditions = list(filter_conditions)
        parameters = list(filter_parameters)
        if name_clause:
            conditions.append("m.id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)")
            parameters.append(name_clause)
        fetched: dict[int, dict[str, Any]] = {}
        for start in range(0, len(missing), 500):
            group = missing[start:start + 500]
            statement = (
                "SELECT m.id, m.account, m.subject, m.sender, m.date_received, m.rfc_id,"
                "       m.has_attachment, m.is_bulk FROM messages m WHERE "
                + " AND ".join([*conditions, f"m.id IN ({','.join('?' * len(group))})"])
            )
            for row in connection.execute(statement, (*parameters, *group)).fetchall():
                fetched[row["id"]] = dict(row)
    else:
        fetched = {}

    rank = 0
    weight = RRF_SEMANTIC_WEIGHT if mode == "hybrid" else 1.0
    for hit in hits:
        entry = entries.get(hit.message)
        if entry is None:
            entry = fetched.get(hit.message)
            if entry is None:  # dropped by a filter
                continue
            entry["match"] = "semantic"
            entry["rrf"] = 0.0
            entries[hit.message] = entry
        else:
            entry["match"] = "both"
        rank += 1
        entry["rrf"] += weight / (RRF_K + rank)
        entry["similarity"] = hit.score
        entry["chunk"] = hit.chunk

    ordered = sorted(entries.values(), key=lambda entry: (-entry["rrf"], -(entry["date_received"] or 0)))[:limit]
    if sort == "date":
        ordered.sort(key=lambda entry: -(entry["date_received"] or 0))
    return ordered


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
    sort: str = "relevance",
    snippets: bool = True,
    mode: str | None = None,
) -> dict[str, Any]:
    """Searches every indexed message, across all accounts.

    sort="relevance" (default) orders by weighted bm25 with a moderate recency
    bonus; sort="date" orders newest first. With snippets=True each result
    carries ~200 characters of its body around the first matched word, read
    from the .emlx on disk (the index is contentless and cannot supply them).

    Gmail-style operators in the query (mail_operators) are parsed first and
    become SQL filters; only the remaining free text goes to FTS5. A query made
    of operators alone is allowed and comes back newest first. The answer's
    "filters" shows how the query was understood.

    mode: "keyword" (exact words: everything above), "semantic" (messages whose
    meaning is close to the text, from the embeddings of mail_vectors.py) or
    "hybrid" (both rankings fused by Reciprocal Rank Fusion). None takes the
    search_mode setting: "keyword", or "auto" = hybrid once the vectors exist and
    cover the index. Without Ollama or the vectors, hybrid and auto answer with
    keywords alone and say why in "semantic_note"; "semantic" fails with a hint.
    Operators, dates and accounts filter the semantic candidates like any other.
    """
    if sort not in SORT_MODES:
        raise MailError(
            "invalid_sort",
            f"Unknown sort: {sort!r}.",
            'Use "relevance" or "date".',
        )
    original_query = query
    parsed = mail_operators.parse(query)
    query = parsed.free_text
    if not query.strip() and not parsed.filters:
        raise MailError("empty_query", "The query is empty.")
    limit = max(1, min(int(limit), 200))
    requested_mode = mode if mode is not None else config.get("search_mode")
    if requested_mode not in SEARCH_MODES:
        raise MailError(
            "invalid_mode",
            f"Unknown mode: {requested_mode!r}.",
            'Use "keyword", "semantic" or "hybrid".',
        )
    if requested_mode == "semantic" and not query.strip():
        raise MailError(
            "semantic_needs_text",
            "A semantic search needs some text to compare, not only operators.",
            'Add words to the query, or use mode="keyword".',
        )
    freshness = _refresh_if_stale(max_age_minutes)
    since_ts = _as_timestamp(since)
    until_ts = _as_timestamp(until, end_of_day=True)

    connection = _connect()
    try:
        has_text = bool(query.strip())
        free_text = query  # before the field filters are rewritten for FTS5
        query = _legacy_column_filters(query)
        effective = _effective_mode(requested_mode, has_text, connection)
        name_clause, filter_conditions, filter_parameters = _message_filters(
            parsed, since_ts, until_ts, account, mailbox, unread_only, flagged_only,
        )

        def match_expression(text: str) -> str:
            return _match_expression(text, has_text, name_clause)

        use_fts = has_text or bool(name_clause)
        conditions: list[str] = ["messages_fts MATCH ?"] if use_fts else []
        parameters: list[Any] = [match_expression(query)] if use_fts else []
        fts_conditions = len(conditions)
        conditions.extend(filter_conditions)
        parameters.extend(filter_parameters)

        if not has_text:
            sort = "date"  # no free text, so nothing to rank by
        scope: list[Any] = []

        def id_scope() -> tuple[list[int] | None, bool]:
            """(ids matching the filters or None, whether they were too many to list)."""
            if not scope:
                scope.append(_allowed_ids(
                    connection, conditions[fts_conditions:], parameters[fts_conditions:], name_clause))
            return scope[0]

        # Meaning first: whether it answered decides how the keyword side is ranked.
        semantic = None
        semantic_note = None
        if effective in ("semantic", "hybrid"):
            try:
                semantic = _semantic_ranking(semantic_text(free_text), id_scope)
            except MailError as error:
                if effective == "semantic":
                    raise
                semantic_note = f"meaning not searched ({error.code}): {error.message}"
                effective = "keyword"
        if semantic is not None and requested_mode in ("semantic", "hybrid"):
            semantic_note = _coverage_note(connection)
        keyword_active = effective != "semantic"
        # The keyword ranking feeds the fusion, so it is by relevance then; a
        # date sort is applied to the fused list at the end.
        fusing = effective == "hybrid"
        keyword_sort = "relevance" if fusing else sort
        keyword_limit = min(200, max(limit * 3, 50)) if fusing else limit

        # Attachment text: another database, merged below. Only for free text;
        # a query of operators alone has nothing to look for in it.
        attachment_hits: dict[int, Any] = {}
        attachments_note = None
        if has_text and keyword_active:
            try:
                import mail_attachments

                allowed, narrow = id_scope()
                attachment_hits = {
                    hit.message: hit
                    for hit in mail_attachments.search_hits(
                        query, ATTACHMENT_CANDIDATES * 10 if narrow else ATTACHMENT_CANDIDATES, allowed=allowed)
                }
            except (sqlite3.DatabaseError, MailError) as error:
                attachments_note = f"attachment text not searched: {error}"

        rows = []
        used_query = query
        if keyword_active:
            wide = min(200, keyword_limit * 3) if attachment_hits else keyword_limit
            if keyword_sort == "date":
                order = "m.date_received DESC"
                order_parameters: list[Any] = []
            else:
                weights = ", ".join(str(weight) for weight in BM25_WEIGHTS)
                # bm25() is negative, the more negative the better, so a positive
                # multiplier above 1 improves a match. Ties fall back to newest.
                order = (
                    f"bm25(messages_fts, {weights})"
                    " * (1 + ? / (1 + max(? - coalesce(m.date_received, 0), 0) / 86400.0 / ?)),"
                    " m.date_received DESC"
                )
                order_parameters = [RECENCY_BOOST, int(time.time()), RECENCY_HALF_LIFE_DAYS]
            source = "messages_fts f JOIN messages m ON m.id = f.rowid" if use_fts else "messages m"
            where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
            score_column = (
                f", bm25(messages_fts, {', '.join(str(weight) for weight in BM25_WEIGHTS)}) AS bm25_score"
                if use_fts else ""
            )
            statement = (
                "SELECT m.id, m.account, m.subject, m.sender, m.date_received, m.rfc_id,"
                f"       m.has_attachment, m.is_bulk{score_column}"
                f"  FROM {source}{where}"
                f" ORDER BY {order} LIMIT ?"
            )
            used_query = query
            try:
                rows = connection.execute(statement, (*parameters, *order_parameters, wide)).fetchall()
            except sqlite3.OperationalError as error:
                if not has_text:
                    raise MailError("invalid_query", f"Unusable query: {error}") from error
                # The query was not valid FTS5 syntax; retry with the words quoted.
                used_query = _quote_terms(query)
                parameters[0] = match_expression(used_query)
                try:
                    rows = connection.execute(statement, (*parameters, *order_parameters, wide)).fetchall()
                except sqlite3.OperationalError as error:
                    raise MailError("invalid_query", f"Unusable query: {error}") from error

            if attachment_hits:
                rows = _merge_attachment_hits(
                    connection, rows, attachment_hits, conditions[fts_conditions:], parameters[fts_conditions:],
                    name_clause, keyword_sort, keyword_limit,
                )


        if semantic is not None:
            rows = _fuse(
                connection, rows, semantic, effective, conditions[fts_conditions:], parameters[fts_conditions:],
                name_clause, limit, sort,
            )
        else:
            rows = [dict(row) for row in rows][:limit]

        sizes = _mailbox_sizes(connection)
        store = _mail_store() if snippets else None
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
                    "has_attachment": bool(row["has_attachment"]),
                    "is_bulk": bool(row["is_bulk"]),
                }
            )
            hit = attachment_hits.get(row["id"])
            if hit is not None:
                messages[-1]["attachment_match"] = {"filename": hit.filename}
            if row.get("match"):
                messages[-1]["match"] = row["match"]
                if "similarity" in row:
                    messages[-1]["similarity"] = round(row["similarity"], 3)
            if snippets:
                if row.get("match") == "semantic":
                    # Nothing to anchor on: the chunk that matched is the excerpt.
                    import mail_vectors

                    messages[-1]["snippet"] = mail_vectors.chunk_excerpt(
                        store, row["id"], row["subject"] or "", row["chunk"])
                else:
                    messages[-1]["snippet"] = _snippet(store, row["id"], used_query)
                if hit is not None:
                    excerpt = _attachment_snippet(hit, used_query)
                    messages[-1]["attachment_match"]["snippet"] = excerpt
                    if row.get("only_attachment"):
                        messages[-1]["snippet"] = f"[attachment: {hit.filename}] {excerpt or ''}".strip()

        result: dict[str, Any] = {
            "ok": True,
            "query": query,
            "sort": sort,
            "mode": effective,
            "filters": mail_operators.describe(parsed.filters),
            "messages": messages,
            "indexed_messages": connection.execute(
                "SELECT count(*) FROM messages"
            ).fetchone()[0],
            "coverage": "all_indexed_mail",
            **freshness,
        }
        if attachments_note:
            result["attachments_note"] = attachments_note
        if semantic_note:
            result["semantic_note"] = semantic_note
        if used_query != query:
            result["interpreted_as"] = used_query
        if parsed.filters:
            result["original_query"] = original_query
        return result
    finally:
        connection.close()


AGGREGATE_GROUPS = ("sender", "domain", "month", "year", "account", "mailbox", "recipient", "recipient_domain")
# Groupings whose keys are periods: listed oldest first, and `limit` keeps the newest.
_CHRONOLOGICAL_GROUPS = ("month", "year")
AGGREGATE_ORDERS = ("count", "last_date")


def _bare_address(sender: str | None) -> str:
    """Lower-cased address of a stored sender ("Name <addr>" or a bare address)."""
    from email.utils import parseaddr

    return parseaddr(sender or "")[1].strip().lower()


def aggregate(
    group_by: str,
    query: str = "",
    account: str | None = None,
    mailbox: str | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
    order: str = "count",
    max_age_minutes: float = config.get("index_max_age_minutes"),
) -> dict[str, Any]:
    """Counts the messages matching a query, grouped by sender, domain, period...

    Takes search_all's query (text and Gmail operators, optional) and filters, so
    "who writes to me most" is aggregate("sender", "to:me") and "volume per month
    of one client" aggregate("month", "from:@example.com"). Runs as one SQL
    GROUP BY on the index: no per-message work, so it stays fast without a query.
    The free text is matched against the message index only, not the text inside
    attachments. A message held by several mailboxes counts once, except for
    group_by="mailbox" where it counts in each.

    Rows are {key, count, unread, last_date}, most frequent first (order="last_date":
    most recent first); month and year come oldest first and `limit` keeps the
    newest periods. A sender row also carries the most frequent display "name"; a
    mailbox row its "account". "total" is the number of matching messages,
    "groups" the number of distinct keys before `limit`.
    """
    if group_by not in AGGREGATE_GROUPS:
        raise MailError(
            "invalid_group_by",
            f"Unknown group_by: {group_by!r}.",
            "Use one of: " + ", ".join(AGGREGATE_GROUPS) + ".",
        )
    if order not in AGGREGATE_ORDERS:
        raise MailError("invalid_order", f"Unknown order: {order!r}.", 'Use "count" or "last_date".')
    limit = max(1, min(int(limit), 500))
    parsed = mail_operators.parse(query or "")
    free_text = parsed.free_text
    freshness = _refresh_if_stale(max_age_minutes)
    since_ts = _as_timestamp(since)
    until_ts = _as_timestamp(until, end_of_day=True)

    connection = _connect()
    try:
        has_text = bool(free_text.strip())
        free_text = _legacy_column_filters(free_text)
        name_clause, conditions, parameters = _message_filters(
            parsed, since_ts, until_ts, account, mailbox, unread_only, flagged_only,
        )
        use_fts = has_text or bool(name_clause)
        if use_fts:
            conditions.insert(0, "messages_fts MATCH ?")
            parameters.insert(0, _match_expression(free_text, has_text, name_clause))
        source = "messages_fts f JOIN messages m ON m.id = f.rowid" if use_fts else "messages m"
        unread = "EXISTS (SELECT 1 FROM locations u WHERE u.message = m.id AND u.read = 0)"
        month = "strftime('%Y-%m', m.date_received, 'unixepoch', 'localtime')"
        year = "strftime('%Y', m.date_received, 'unixepoch', 'localtime')"

        join = ""
        extra = ""
        extra_parameters: list[Any] = []
        count_expression = "count(*)"
        unread_expression = f"sum({unread})"
        extra_columns = ""
        if group_by in ("sender", "domain"):
            # Grouped on the raw stored sender, folded on the bare address below:
            # parsing "Name <addr>" is Python's job, the GROUP BY stays cheap.
            key_expression = "m.sender"
        elif group_by == "month":
            key_expression = month
        elif group_by == "year":
            key_expression = year
        elif group_by == "account":
            key_expression = "m.account"
        elif group_by == "mailbox":
            join = " JOIN locations l ON l.message = m.id"
            key_expression = "l.mailbox"
            extra_columns = ", l.account AS account"
            unread_expression = "sum(l.read = 0)"
            # Only the copies asked for: the message filters above match a
            # message through any of its locations.
            if mailbox:
                extra = " AND lower(l.mailbox) = lower(?)"
                extra_parameters.append(mailbox)
            if account:
                extra += " AND lower(l.account) = lower(?)"
                extra_parameters.append(account)
        else:  # recipient, recipient_domain
            join = " JOIN recipients r ON r.message = m.id"
            key_expression = "r.address" if group_by == "recipient" else "r.domain"
            count_expression = "count(DISTINCT m.id)"
            # A message can join several rows of one key (To and Cc, two
            # addresses of a domain): count it once for unread too.
            unread_expression = f"count(DISTINCT CASE WHEN {unread} THEN m.id END)"
        group_expression = key_expression + (", l.account" if group_by == "mailbox" else "")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        row_where = (where or " WHERE 1") + extra if extra else where
        statement = (
            f"SELECT {key_expression} AS key, {count_expression} AS n, {unread_expression} AS unread,"
            f" max(m.date_received) AS last{extra_columns}"
            f" FROM {source}{join}{row_where} GROUP BY {group_expression}"
        )
        total_statement = f"SELECT count(*) FROM {source}{where}"
        used_query = free_text
        try:
            rows = connection.execute(statement, [*parameters, *extra_parameters]).fetchall()
        except sqlite3.OperationalError as error:
            if not use_fts:
                raise MailError("invalid_query", f"Unusable query: {error}") from error
            used_query = _quote_terms(free_text)
            parameters[0] = _match_expression(used_query, has_text, name_clause)
            try:
                rows = connection.execute(statement, [*parameters, *extra_parameters]).fetchall()
            except sqlite3.OperationalError as error:
                raise MailError("invalid_query", f"Unusable query: {error}") from error

        groups: dict[str, dict[str, Any]] = {}
        names: dict[str, dict[str, int]] = {}
        for row in rows:
            key = row["key"]
            if group_by in ("sender", "domain"):
                address = _bare_address(key)
                key = address if group_by == "sender" else (address.rsplit("@", 1)[1] if "@" in address else address)
            key = key if key not in (None, "") else "unknown"
            slot = groups.setdefault(
                (key, row["account"]) if group_by == "mailbox" else key, {"key": key, "count": 0, "unread": 0, "last": 0,
                      **({"account": row["account"]} if group_by == "mailbox" else {})})
            slot["count"] += row["n"]
            slot["unread"] += row["unread"] or 0
            slot["last"] = max(slot["last"], row["last"] or 0)
            if group_by == "sender":
                from email.utils import parseaddr

                display = parseaddr(row["key"] or "")[0].strip()
                if display:
                    names.setdefault(key, {})[display] = names.setdefault(key, {}).get(display, 0) + row["n"]
        ordered = list(groups.values())
        if group_by in _CHRONOLOGICAL_GROUPS:
            # Undated messages ("unknown") go first, out of the way of the newest periods.
            ordered.sort(key=lambda item: ("" if item["key"] == "unknown" else item["key"]))
            ordered = ordered[-limit:]
        elif order == "last_date":
            ordered.sort(key=lambda item: (-item["last"], item["key"]))
            ordered = ordered[:limit]
        else:
            ordered.sort(key=lambda item: (-item["count"], item["key"]))
            ordered = ordered[:limit]
        results = []
        for item in ordered:
            entry = {
                "key": item["key"],
                "count": item["count"],
                "unread": item["unread"],
                "last_date": datetime.fromtimestamp(item["last"], tz=timezone.utc).astimezone().isoformat()
                if item["last"] else None,
            }
            if group_by == "sender" and item["key"] in names:
                entry["name"] = max(names[item["key"]].items(), key=lambda pair: (pair[1], pair[0]))[0]
            if "account" in item:
                entry["account"] = item["account"]
            results.append(entry)

        total = connection.execute(total_statement, parameters).fetchone()[0]
        result: dict[str, Any] = {
            "ok": True,
            "group_by": group_by,
            "query": free_text,
            "filters": mail_operators.describe(parsed.filters),
            "total": total,
            "groups": len(groups),
            "results": results,
            **freshness,
        }
        if used_query != free_text:
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

    if os.path.isfile(INDEX_PATH):
        probe = sqlite3.connect(f"file:{INDEX_PATH}?mode=ro", uri=True)
        _require_current_schema(probe)
        probe.close()

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
        if completed.returncode == 75:
            raise MailError(
                "index_busy",
                "Another sync or build is already running on the index.",
                "Try again in a few minutes.",
            )
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
    refreshed = 0
    for line in output.splitlines():
        match = re.search(r"removed (\d+) messages", line)
        if match:
            removed = int(match.group(1))
        match = re.search(r"indexed (\d+) messages", line)
        if match:
            added = int(match.group(1))
        match = re.search(r"refreshed (\d+) flags", line)
        if match:
            refreshed = int(match.group(1))
    result = {"ok": True, "added": added, "removed": removed, "refreshed": refreshed, "log": output[-1000:]}
    # New mail may carry attachments. Reading them takes minutes, far too long
    # to wait for, so it runs on its own, and only once the attachment index
    # exists (someone chose to build it).
    try:
        import mail_attachments

        if mail_attachments.start_background_sync():
            result["attachments_sync"] = "started in the background"
    except Exception:  # noqa: BLE001 - the message sync already succeeded
        pass
    # Same for the embeddings of new mail (Ollama, minutes for a busy day).
    try:
        import mail_vectors

        if mail_vectors.start_background_sync():
            result["vectors_sync"] = "started in the background"
    except Exception:  # noqa: BLE001 - the message sync already succeeded
        pass
    if purged:
        result["purged_drafts"] = purged
    return result


def _attachments_status() -> dict[str, Any]:
    """The attachment text index in a few numbers; never an error."""
    try:
        import mail_attachments

        return mail_attachments.status()
    except Exception as error:  # noqa: BLE001 - a side index must not break the status
        return {"built": False, "note": str(error)}


def _vectors_status(indexed_messages: int) -> dict[str, Any]:
    """The embeddings (search by meaning) in a few numbers; never an error."""
    try:
        import mail_vectors

        return mail_vectors.status(indexed_messages)
    except Exception as error:  # noqa: BLE001 - a side index must not break the status
        return {"built": False, "note": str(error)}


def index_status() -> dict[str, Any]:
    """Reports what the index holds and how old it is.

    with_body / without_body say how many messages have searchable body text:
    the others (file missing, partial download, empty message) only match on
    subject, sender, recipients and attachment names.
    """
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
            {
                "account": row["account"],
                "messages": row["n"],
                "with_body": row["with_body"],
                "without_body": row["n"] - row["with_body"],
            }
            for row in connection.execute(
                "SELECT account, count(*) AS n, coalesce(sum(body_indexed), 0) AS with_body"
                "  FROM messages GROUP BY account ORDER BY n DESC"
            )
        ]
        with_body = sum(account["with_body"] for account in accounts)
        built_at = int(built[0]) if built and built[0] else None
        return {
            "ok": True,
            "indexed_messages": messages,
            "with_body": with_body,
            "without_body": messages - with_body,
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
            "attachments": _attachments_status(),
            "vectors": _vectors_status(messages),
        }
    finally:
        connection.close()
