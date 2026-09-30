#!/usr/bin/env python3
"""Search by meaning: embeddings of the messages, in a file of their own (MCPMAILMAC-12).

Keyword search finds the words you typed. This finds what a message is about:
each message is cut into overlapping chunks (subject and own text, quotes
already stripped), each chunk is turned into a vector by a local embedding model
served by Ollama (bge-m3, 1024 dimensions), and a query is matched against those
vectors by cosine similarity. mail_search fuses that ranking with the keyword one.

The vectors live in `vectors.sqlite`, beside the search index, so they can be
built, rebuilt or absent without touching message search. One row per chunk holds
the vector as int8 (about a quarter of float32, and the ranking is the same to
within rounding, see README). Cosine similarity is computed by sqlite-vec's
scalar functions when the extension can be loaded, and by a plain Python loop over
a small candidate set otherwise. Everything else is the standard library; Ollama
is reached with urllib.

    python3 mail_vectors.py --sync      # resumable, incremental
    python3 mail_vectors.py --build     # from scratch
    python3 mail_vectors.py --status

Only the message text is embedded, never stored: the vectors file holds numbers,
chunk positions and a hash of the text, not mail. Reading the messages needs Full
Disk Access, like mail_index.py; nothing is ever written under ~/Library/Mail.
Attachments are not embedded (see the README for why).
"""

from __future__ import annotations

import argparse
import array
import hashlib
import http.client
import json
import math
import os
import random
import re
import signal
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import config
import mail_index
from mail_tools import MailError

# --------------------------------------------------------------------------
# Text to embed
# --------------------------------------------------------------------------

CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
# A long message is represented by its first chunks: the start says what it is
# about, and the tail of a long thread is mostly what earlier messages said.
MAX_CHUNKS = 8
# A tail shorter than this is folded into the last chunk instead of getting one
# that would be mostly the overlap.
MIN_TAIL_CHARS = 60
# Words this long are addresses, tokens or encoded data: noise for a model.
MAX_WORD_CHARS = 40

_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_SENTENCE_END = (". ", "! ", "? ", "\n")


def clean_text(text: str) -> str:
    """The text as the model should read it: no links, no long tokens, one line."""
    text = _URL.sub(" ", text)
    words = [word for word in text.split() if len(word) <= MAX_WORD_CHARS]
    return " ".join(words)


def _last_sentence_end(text: str, low: int, high: int) -> int:
    """Index just after the last sentence end in text[low:high], or -1."""
    found = [text.rfind(mark, low, high) + len(mark) for mark in _SENTENCE_END if text.rfind(mark, low, high) != -1]
    return max(found, default=-1)


def split_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP,
               limit: int = MAX_CHUNKS) -> list[str]:
    """Cuts `text` (already cleaned) into chunks of about `size` characters.

    Consecutive chunks share `overlap` characters, so a sentence cut at a border
    is whole in one of them (a tail shorter than MIN_TAIL_CHARS is folded into the
    last chunk, which may then exceed `size` by that much). A cut prefers a sentence end in the last 30 % of the
    window, then a space; a chunk never starts in the middle of a word. At most
    `limit` chunks are returned.
    """
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length and len(chunks) < limit:
        end = min(start + size, length)
        if end < length:
            window = start + int(size * 0.7)
            cut = _last_sentence_end(text, window, end)
            if cut > start:
                end = cut
            else:
                space = text.rfind(" ", window, end)
                if space > start:
                    end = space
            if length - end < MIN_TAIL_CHARS:
                end = length
        chunks.append(text[start:end].strip())
        if end >= length:
            break
        start = max(end - overlap, start + 1)
        space = text.find(" ", start, end)
        if space != -1:
            start = space + 1
    return [chunk for chunk in chunks if chunk]


@dataclass
class Chunk:
    number: int
    source: str  # "body", or "subject" for a message with no usable body
    text: str  # what the reader sees (body only)
    embed: str  # what the model gets: the first chunk also carries the subject


def message_chunks(subject: str, body: str) -> list[Chunk]:
    """The chunks of one message. A message with no body is still findable by subject."""
    subject = clean_text(subject or "")
    pieces = split_text(clean_text(body or ""))
    if not pieces:
        return [Chunk(0, "subject", "", subject)] if subject else []
    return [
        Chunk(number, "body", piece, f"{subject}\n{piece}" if number == 0 and subject else piece)
        for number, piece in enumerate(pieces)
    ]


def text_hash(subject: str, body: str) -> str:
    """Identifies what was embedded, so a message is only embedded again if it changed."""
    digest = hashlib.sha1()
    for chunk in message_chunks(subject, body):
        digest.update(chunk.embed.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Vectors
# --------------------------------------------------------------------------


def quantize(vector: Iterable[float]) -> bytes:
    """A vector as int8 bytes, scaled so its largest component is 127.

    Cosine similarity ignores a vector's length, so each vector has its own
    scale and none needs to be stored.
    """
    values = list(vector)
    peak = max((abs(value) for value in values), default=0.0)
    if peak == 0.0:
        return bytes(len(values))
    scale = 127.0 / peak
    return struct.pack(f"{len(values)}b", *(int(round(value * scale)) for value in values))


def cosine(left: bytes, right: bytes) -> float:
    """Cosine similarity of two int8 vectors; the Python stand-in for sqlite-vec."""
    a = array.array("b", left)
    b = array.array("b", right)
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


# --------------------------------------------------------------------------
# The embedding server
# --------------------------------------------------------------------------

BATCH_CHUNKS = 32
# After a failure to reach Ollama, searches skip it for this long instead of each
# paying the timeout again.
COOLDOWN_SECONDS = 60
KEEP_ALIVE = "30m"


class EmbedderError(Exception):
    """Ollama could not embed: not running, model missing, or timed out."""

    def __init__(self, message: str, hint: str = "", transient: bool = True):
        super().__init__(message)
        self.hint = hint
        self.transient = transient


class OllamaEmbedder:
    """Embeds texts through Ollama's /api/embed, over one kept-alive connection.

    A search embeds one short query, so the connection set-up would be a large
    part of its latency: the connection is reused, and reopened when it dropped.
    """

    def __init__(self, url: str | None = None, model: str | None = None, timeout: float | None = None):
        self.url = url or config.get("ollama_url")
        self.model = model or config.get("embedding_model")
        self.timeout = float(timeout if timeout is not None else config.get("ollama_timeout"))
        parsed = urllib.parse.urlparse(self.url)
        self._https = parsed.scheme == "https"
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or (443 if self._https else 80)
        self._prefix = parsed.path.rstrip("/")
        self._connection: http.client.HTTPConnection | None = None
        self._lock = threading.Lock()

    def _open(self) -> http.client.HTTPConnection:
        factory = http.client.HTTPSConnection if self._https else http.client.HTTPConnection
        return factory(self._host, self._port, timeout=self.timeout)

    def _request(self, method: str, path: str, payload: dict | None, timeout: float) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        last: Exception | None = None
        for attempt in range(2):  # a kept-alive connection may have been closed by the server
            connection = self._connection or self._open()
            connection.timeout = timeout
            try:
                if connection.sock is not None:
                    connection.sock.settimeout(timeout)
                connection.request(method, self._prefix + path, body, headers)
                response = connection.getresponse()
                data = response.read()
            except (http.client.HTTPException, OSError) as error:
                connection.close()
                self._connection = None
                last = error
                if isinstance(error, (TimeoutError, ConnectionRefusedError)):
                    break
                continue
            self._connection = connection
            if response.status != 200:
                text = data.decode("utf-8", errors="replace")[:200]
                # An answer, even an error, means the server is up: the input is
                # what it refused, which is not worth a cool-down.
                raise EmbedderError(f"Ollama answered {response.status}: {text}", transient=False)
            try:
                return json.loads(data)
            except ValueError as error:
                raise EmbedderError(f"Ollama sent an unreadable answer: {error}") from error
        if isinstance(last, TimeoutError):
            raise EmbedderError(f"Ollama did not answer within {timeout:g} s.",
                                "Raise ollama_timeout, or check that the model is loaded.")
        raise EmbedderError(f"Ollama is not reachable at {self.url}: {last}",
                            "Start it (brew services start ollama) and pull the model "
                            f"(ollama pull {self.model}).")

    def embed(self, texts: list[str], timeout: float | None = None) -> list[list[float]]:
        with self._lock:
            answer = self._request(
                "POST", "/api/embed",
                {"model": self.model, "input": texts, "keep_alive": KEEP_ALIVE},
                timeout if timeout is not None else max(self.timeout, 120.0) if len(texts) > 1 else self.timeout,
            )
        vectors = answer.get("embeddings") if isinstance(answer, dict) else None
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbedderError("Ollama returned no embeddings for this input.", transient=False)
        return vectors

    def reachable(self, timeout: float = 1.0) -> bool:
        """Whether Ollama answers at all (not whether the model is pulled)."""
        try:
            with self._lock:
                self._request("GET", "/api/version", None, timeout)
            return True
        except EmbedderError:
            return False


_embedder: OllamaEmbedder | None = None
_embedder_key: tuple[str, str] | None = None
_down_until = 0.0
_query_cache: dict[tuple[str, str], list[float]] = {}
QUERY_CACHE_SIZE = 64


def default_embedder() -> OllamaEmbedder:
    """The shared embedder; rebuilt when the settings change."""
    global _embedder, _embedder_key
    key = (config.get("ollama_url"), config.get("embedding_model"))
    if _embedder is None or _embedder_key != key:
        global _down_until
        _down_until = 0.0  # a failure of the old endpoint says nothing of the new one
        _embedder = OllamaEmbedder()
        _embedder_key = key
    return _embedder


def embed_query(text: str, embedder: Any = None) -> list[float]:
    """The vector of a query. Raises EmbedderError, and remembers a failure for a minute."""
    global _down_until
    embedder = embedder or default_embedder()
    key = (getattr(embedder, "model", ""), text)
    cached = _query_cache.get(key)
    if cached is not None:
        return cached
    if time.time() < _down_until:
        raise EmbedderError("Ollama failed a moment ago; not asked again yet.")
    try:
        vector = embedder.embed([text])[0]
    except EmbedderError as error:
        if error.transient:
            _down_until = time.time() + COOLDOWN_SECONDS
        raise
    if QUERY_CACHE_SIZE > 0:
        while len(_query_cache) >= QUERY_CACHE_SIZE:
            _query_cache.pop(next(iter(_query_cache)))
        _query_cache[key] = vector
    return vector


def reset_state() -> None:
    """Forgets the failure cool-down and cached queries (tests, model change)."""
    global _down_until, _embedder
    _down_until = 0.0
    _embedder = None
    _query_cache.clear()


# --------------------------------------------------------------------------
# The vectors file
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1
# Bump when the chunking or the text preparation changes: vectors of the old
# recipe and of the new one must not be mixed.
RECIPE = f"chunk-v1:{CHUNK_CHARS}:{CHUNK_OVERLAP}:{MAX_CHUNKS}"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS messages (
    message     INTEGER PRIMARY KEY,
    text_hash   TEXT NOT NULL,
    chunks      INTEGER NOT NULL,
    has_body    INTEGER NOT NULL,
    embedded_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY,
    message INTEGER NOT NULL,
    chunk   INTEGER NOT NULL,
    source  TEXT NOT NULL,
    vector  BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_message ON chunks (message, chunk);
"""


def database_path() -> str:
    """vectors_path, or vectors.sqlite beside the search index."""
    configured = config.get("vectors_path")
    if configured:
        return configured
    return os.path.join(os.path.dirname(config.get("index_path")), "vectors.sqlite")


def load_sqlite_vec(connection: sqlite3.Connection) -> bool:
    """Loads the optional sqlite-vec extension into `connection`; False if it cannot be."""
    try:
        import sqlite_vec  # optional dependency
    except ImportError:
        return False
    try:
        connection.enable_load_extension(True)
        try:
            sqlite_vec.load(connection)
        finally:
            connection.enable_load_extension(False)
        return True
    except (AttributeError, sqlite3.Error, OSError):
        return False


def _signature(model: str) -> str:
    return f"{model}|{RECIPE}"


def open_database(path: str, model: str | None = None, create: bool = True) -> sqlite3.Connection:
    """Opens the vectors file, refusing one made with other settings.

    With create=True (the builder) it is created if need be; otherwise it is
    opened read-only, so a search can never alter it.
    """
    model = model or config.get("embedding_model")
    if create:
        connection = sqlite3.connect(path, timeout=30)
    else:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    if create:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.executescript(_SCHEMA)
    meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
    if create and "schema_version" not in meta:
        connection.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        connection.execute("INSERT INTO meta VALUES ('signature', ?)", (_signature(model),))
        connection.commit()
    elif meta.get("schema_version") != str(SCHEMA_VERSION) or meta.get("signature") != _signature(model):
        connection.close()
        raise MailError(
            "vectors_outdated",
            "The vectors file was made with another model, chunking or schema "
            f"({meta.get('signature', '?')}, this code uses {_signature(model)}).",
            "Rebuild it: python3 mail_vectors.py --build",
        )
    return connection


def _log(message: str) -> None:
    print(message, flush=True)


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------


@dataclass
class SyncResult:
    embedded: int = 0  # messages
    chunks: int = 0
    removed: int = 0
    skipped: int = 0  # chunks the model refused
    seconds: float = 0.0
    interrupted: bool = False
    total: int = 0


class _Terminated(SystemExit):
    """Raised by SIGTERM so the cleanup in finally blocks runs."""


def _on_sigterm(signum, frame):  # noqa: ANN001
    raise _Terminated(143)


def _sample(identifier: int, seed: int, percent: float) -> bool:
    """Whether a message belongs to a reproducible random `percent` of the mailbox."""
    digest = hashlib.md5(f"{seed}:{identifier}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2**32 * 100 < percent


def embed_resilient(embedder: Any, texts: list[str]) -> list[list[float] | None]:
    """Embeds a batch; when the server refuses it, one text at a time.

    A text the model cannot take (rare: a malformed input) becomes None and is
    skipped, so one message never blocks a build. An unreachable server is not
    retried here: EmbedderError propagates and the run stops, resumable.
    """
    try:
        return list(embedder.embed(texts))
    except EmbedderError as error:
        if error.transient:
            raise
    result: list[list[float] | None] = []
    for text in texts:
        try:
            result.append(embedder.embed([text])[0])
        except EmbedderError as error:
            if error.transient:
                raise
            result.append(None)
    return result


def _flush(connection: sqlite3.Connection, embedder: Any, pending: list[dict], result: SyncResult) -> None:
    """Embeds the chunks of the pending messages and stores each message whole."""
    texts = [chunk.embed for entry in pending for chunk in entry["chunks"]]
    vectors = embed_resilient(embedder, texts) if texts else []
    position = 0
    now = int(time.time())
    for entry in pending:
        identifier = entry["message"]
        connection.execute("DELETE FROM chunks WHERE message = ?", (identifier,))
        stored = 0
        for chunk in entry["chunks"]:
            vector = vectors[position]
            position += 1
            if vector is None or not any(vector):  # refused, or all zero (no direction to compare)
                result.skipped += 1
                continue
            connection.execute(
                "INSERT INTO chunks (message, chunk, source, vector) VALUES (?,?,?,?)",
                (identifier, chunk.number, chunk.source, quantize(vector)),
            )
            stored += 1
        connection.execute(
            "INSERT OR REPLACE INTO messages (message, text_hash, chunks, has_body, embedded_at)"
            " VALUES (?,?,?,?,?)",
            (identifier, entry["hash"], stored, entry["has_body"], now),
        )
        result.embedded += 1
        result.chunks += stored
    connection.commit()
    pending.clear()


def sync(
    store: str | None,
    database: str,
    index_path: str,
    embedder: Any = None,
    limit: int | None = None,
    rebuild: bool = False,
    verify: bool = False,
    sample: tuple[float, int] | None = None,
    batch: int = BATCH_CHUNKS,
    log: Callable[[str], None] = _log,
    files: dict[int, str] | None = None,
    read_body: Callable[[str], str] | None = None,
) -> SyncResult:
    """Embeds the indexed messages that have no vectors yet. Safe to interrupt and to rerun.

    The list of messages comes from the search index, so the two always agree:
    a message the index dropped loses its vectors here. A message already embedded
    is skipped without being read; `verify` reads it again and embeds it anew if
    its text hash changed. `sample` = (percent, seed) restricts the run to a
    reproducible random subset (measurements). `files` and `read_body` let tests
    run without Mail's store.
    """
    embedder = embedder or default_embedder()
    started = time.time()
    lock = mail_index.IndexLock(database)
    lock.acquire()
    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread
        previous = None
    result = SyncResult()
    connection = None
    succeeded = False
    try:
        if rebuild:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(database + suffix)
                except FileNotFoundError:
                    pass
        connection = open_database(database, getattr(embedder, "model", None))
        index = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        try:
            rows = index.execute("SELECT id, coalesce(subject, '') FROM messages ORDER BY id").fetchall()
        finally:
            index.close()
        subjects = {identifier: subject for identifier, subject in rows}
        wanted = set(subjects)
        if sample:
            wanted = {identifier for identifier in wanted if _sample(identifier, sample[1], sample[0])}

        known = {row[0]: row[1] for row in connection.execute("SELECT message, text_hash FROM messages")}
        vanished = [identifier for identifier in known if identifier not in subjects]
        for start in range(0, len(vanished), 500):
            group = vanished[start:start + 500]
            marks = ",".join("?" * len(group))
            connection.execute(f"DELETE FROM chunks WHERE message IN ({marks})", group)
            connection.execute(f"DELETE FROM messages WHERE message IN ({marks})", group)
        connection.commit()
        result.removed = len(vanished)
        if vanished:
            log(f"removed the vectors of {len(vanished)} messages no longer indexed")

        todo = sorted(wanted if verify else wanted - set(known))
        if limit is not None:
            todo = todo[:limit]
        result.total = len(todo)
        log(f"{len(todo)} messages to embed ({len(known)} already done)")
        if not todo:
            succeeded = True
            return result
        if files is None:
            log("scanning message files...")
            files = mail_index.scan_message_files(store) if store else {}
        if read_body is None:
            def read_body(path: str) -> str:
                return mail_index.extract_message(path).body

        pending: list[dict] = []
        pending_chunks = 0
        try:
            for position, identifier in enumerate(todo, 1):
                path = files.get(identifier)
                body = read_body(path) if path else ""
                subject = subjects[identifier]
                digest = text_hash(subject, body)
                if verify and known.get(identifier) == digest:
                    continue
                chunks = message_chunks(subject, body)
                pending.append({"message": identifier, "hash": digest, "chunks": chunks,
                                "has_body": int(bool(chunks and chunks[0].source == "body"))})
                pending_chunks += len(chunks)
                if pending_chunks >= batch:
                    _flush(connection, embedder, pending, result)
                    pending_chunks = 0
                    lock.touch()
                    rate = result.chunks / max(time.time() - started, 0.001)
                    remaining = (len(todo) - position) * (time.time() - started) / max(position, 1)
                    log(f"  {position}/{len(todo)} messages, {result.chunks} chunks, "
                        f"{rate:.1f} chunks/s, about {remaining / 60:.0f} min left")
            if pending:
                _flush(connection, embedder, pending, result)
            succeeded = True
        except (KeyboardInterrupt, SystemExit, EmbedderError):
            result.interrupted = True
            raise
        finally:
            connection.commit()
    finally:
        result.seconds = time.time() - started
        if connection is not None:
            done = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
            complete = succeeded and not result.interrupted and not sample and limit is None
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)", (str(int(time.time())),))
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('last_run_seconds', ?)", (str(round(result.seconds)),))
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('last_run_complete', ?)", ("1" if complete else "0",))
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('messages_done', ?)", (str(done),))
            connection.commit()
            connection.close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        lock.release()
    return result


# --------------------------------------------------------------------------
# Searching (read only)
# --------------------------------------------------------------------------

# Chunks fetched before they are folded into one score per message.
CANDIDATE_CHUNKS = 200
# Without sqlite-vec the similarity is computed in Python: about 100 microseconds
# a chunk, so past this many candidates the search is left to keywords.
PYTHON_FALLBACK_CHUNKS = 10_000
# Coverage of the index below which the vectors are not trusted as the default.
FRESH_COVERAGE = 0.95


@dataclass
class Hit:
    message: int
    score: float  # cosine similarity of the best chunk
    chunk: int  # number of that chunk


def _connect_readonly(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def search_hits(
    query_vector: list[float],
    allowed: list[int] | None = None,
    limit_chunks: int = CANDIDATE_CHUNKS,
    path: str | None = None,
    model: str | None = None,
) -> list[Hit]:
    """Messages nearest to the query vector, best first, one entry per message.

    `allowed` restricts the search to those message ids (the filters of the
    query: operators, dates, account...), applied before ranking so a narrow
    filter never loses its hits to the global nearest neighbours. A message
    scores as its best chunk.
    """
    path = path or database_path()
    if not os.path.isfile(path):
        raise MailError("vectors_missing", "The vectors file has not been built.",
                        "Build it: python3 mail_vectors.py --build")
    try:
        connection = open_database(path, model, create=False)
    except sqlite3.DatabaseError as error:
        raise MailError("vectors_invalid", f"The vectors file is unreadable: {error}") from error
    try:
        query = quantize(query_vector)
        if allowed is not None and not allowed:
            return []
        if allowed is not None:
            connection.execute("CREATE TEMP TABLE wanted (message INTEGER PRIMARY KEY)")
            connection.executemany("INSERT OR IGNORE INTO wanted VALUES (?)", ((i,) for i in allowed))
        restriction = " WHERE message IN (SELECT message FROM wanted)" if allowed is not None else ""
        if load_sqlite_vec(connection):
            rows = connection.execute(
                "SELECT message, chunk, 1.0 - vec_distance_cosine(vec_int8(vector), vec_int8(?)) AS similarity"
                f" FROM chunks{restriction} ORDER BY similarity DESC LIMIT ?",
                (query, limit_chunks),
            ).fetchall()
        else:
            count = connection.execute(f"SELECT count(*) FROM chunks{restriction}").fetchone()[0]
            if count > PYTHON_FALLBACK_CHUNKS:
                raise MailError(
                    "semantic_needs_sqlite_vec",
                    f"Comparing {count} chunks without sqlite-vec would take too long.",
                    "Install the optional extension: .venv/bin/pip install sqlite-vec "
                    "(or narrow the search with operators or dates).",
                )
            scored = [
                (row["message"], row["chunk"], cosine(query, row["vector"]))
                for row in connection.execute(f"SELECT message, chunk, vector FROM chunks{restriction}")
            ]
            scored.sort(key=lambda item: -item[2])
            rows = scored[:limit_chunks]
    except sqlite3.DatabaseError as error:
        raise MailError("vectors_invalid", f"The vectors file cannot be searched: {error}") from error
    finally:
        connection.close()
    best: dict[int, Hit] = {}
    for message, chunk, similarity in ((row[0], row[1], row[2]) for row in rows):
        if similarity is None:  # sqlite-vec has no cosine for an all-zero vector
            continue
        if message not in best:  # rows come best first
            best[message] = Hit(message, float(similarity), chunk)
    return sorted(best.values(), key=lambda hit: -hit.score)


def chunk_excerpt(store: str | None, identifier: int, subject: str, chunk: int, length: int = 200) -> str | None:
    """About `length` characters of the chunk that matched; None when it cannot be had.

    The vectors file holds no text, so the message is read again and cut with the
    same recipe. A message with no body has nothing to show.
    """
    if not store:
        return None
    try:
        path = mail_index.find_message_file(store, identifier)
        if path is None:
            return None
        pieces = split_text(clean_text(mail_index.extract_message(path).body))
    except Exception:  # noqa: BLE001 - an excerpt is a convenience
        return None
    if not pieces:
        return None
    text = pieces[min(chunk, len(pieces) - 1)]
    if len(text) <= length:
        return text
    cut = text.rfind(" ", 0, length)
    return text[: cut if cut > length // 2 else length].rstrip() + "…"


def status(index_messages: int | None = None, path: str | None = None, probe: bool = True) -> dict:
    """What the vectors hold; {"built": False} when the file does not exist."""
    path = path or database_path()
    result: dict[str, Any] = {
        "built": False, "database": path, "model": config.get("embedding_model"),
        "sqlite_vec": _sqlite_vec_available(),
    }
    if not os.path.isfile(path):
        return result
    try:
        connection = _connect_readonly(path)
        try:
            meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
            messages = connection.execute("SELECT count(*) FROM messages").fetchone()[0]
            chunks = connection.execute("SELECT count(*) FROM chunks").fetchone()[0]
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        result["note"] = "unreadable"
        return result
    last = int(meta["last_run"]) if meta.get("last_run") else None
    current = meta.get("signature") == _signature(config.get("embedding_model"))
    result.update(
        built=True,
        size_mb=round(os.path.getsize(path) / 1024 / 1024),
        messages=messages,
        chunks=chunks,
        signature=meta.get("signature"),
        current=current,
        last_run=time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else None,
        last_run_seconds=int(meta["last_run_seconds"]) if meta.get("last_run_seconds") else None,
        last_run_complete=meta.get("last_run_complete") == "1",
    )
    if index_messages:
        result["coverage"] = round(min(messages / index_messages, 1.0), 4)
        result["fresh"] = current and messages / index_messages >= FRESH_COVERAGE
    if probe:
        result["ollama_reachable"] = default_embedder().reachable()
    return result


def _sqlite_vec_available() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        return load_sqlite_vec(connection)
    finally:
        connection.close()


def start_background_sync() -> bool:
    """Starts `--sync` detached, once a vectors file exists; True when started.

    Never waits and never fails the caller. If another run holds the lock the new
    one exits at once (exit code 75), so calling this often is harmless.
    """
    if not config.get("vectors_auto_sync") or not os.path.isfile(database_path()):
        return False
    try:
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--sync"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "MAIL_MCP_VECTORS_PATH": database_path(),
                 "MAIL_MCP_INDEX_PATH": config.get("index_path")},
        )
    except OSError:
        return False
    threading.Thread(target=child.wait, daemon=True).start()
    return True


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--sync", action="store_true", help="embed what is new; resumes an interrupted run")
    action.add_argument("--build", action="store_true", help="start from scratch")
    action.add_argument("--status", action="store_true", help="what the vectors file holds")
    parser.add_argument("--database", default=None, help="default: vectors_path or beside the index")
    parser.add_argument("--limit", type=int, default=None, help="embed at most this many messages in this run")
    parser.add_argument("--batch", type=int, default=BATCH_CHUNKS, help="chunks per request to Ollama")
    parser.add_argument("--sample", type=float, default=None, metavar="PERCENT",
                        help="only a reproducible random PERCENT of the messages (measurements)")
    parser.add_argument("--seed", type=int, default=1, help="seed for --sample")
    parser.add_argument("--verify", action="store_true",
                        help="read already embedded messages again and embed those whose text changed")
    args = parser.parse_args(argv)
    database = args.database or database_path()
    index_path = config.get("index_path")

    if args.status:
        count = None
        if os.path.isfile(index_path):
            probe = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
            try:
                count = probe.execute("SELECT count(*) FROM messages").fetchone()[0]
            except sqlite3.DatabaseError:
                count = None
            finally:
                probe.close()
        print(json.dumps(status(count, database), indent=1))
        return 0
    if not os.path.isfile(index_path):
        print(f"No search index at {index_path}: build it first (python3 mail_index.py --build).")
        return 1
    try:
        store = mail_index.find_store()
    except PermissionError:
        print("Permission denied on ~/Library/Mail.")
        print("Grant Full Disk Access to the app running this script, then try again.")
        return 1
    except FileNotFoundError as error:
        print(error)
        return 1
    try:
        result = sync(
            store, database, index_path, limit=args.limit, rebuild=args.build, verify=args.verify,
            sample=(args.sample, args.seed) if args.sample else None, batch=max(1, args.batch),
        )
    except mail_index.IndexBusy:
        print("Another vectors run holds the lock; try again when it ends.")
        return mail_index.LOCK_EXIT_CODE
    except EmbedderError as error:
        print(f"Embedding stopped: {error}")
        if error.hint:
            print(error.hint)
        print("Nothing is lost: rerun --sync to resume.")
        return 1
    except MailError as error:
        print(f"{error.code}: {error.message}")
        if error.hint:
            print(error.hint)
        return 1
    except PermissionError:
        print("Permission denied on ~/Library/Mail. Grant Full Disk Access, then try again.")
        return 1
    print(f"done in {result.seconds:.0f} s: {result.embedded} messages, {result.chunks} chunks, "
          f"{result.removed} removed, {result.skipped} chunks refused by the model")
    print(f"vectors: {database} ({os.path.getsize(database) / 1024 / 1024:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
