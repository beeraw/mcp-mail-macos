"""Text of mail attachments, in a search index of its own (MCPMAILMAC-11).

Attachments live in two places. Messages stored as `.partial.emlx` keep them as
plain files under `<mailbox>.mbox/<store>/Data/<shard>/Attachments/<id>/<part>/<file>`
(the shard is the one `mail_index.shard_candidates` gives for the Messages folder);
a full `.emlx` carries them inside its MIME, which are written to a private
temporary directory just long enough to be read. Extractors are small functions
using the standard library and macOS tools only:

- PDF text layer, and OCR of scanned PDFs and images: the Swift tools in `tools/`
  (PDFKit, Vision), compiled on demand into `tools/build/` and run on batches of
  files, one process for many;
- docx / xlsx / pptx: zipfile + XML; .doc: `textutil`.

The text goes to `attachments.sqlite`, separate from the message index so it can
grow, be rebuilt, or be absent without touching message search. Each attachment is
one row whose `status` makes the run resumable: discovery records everything as
`pending` (or skipped, with the reason), processing works through the pending
rows and commits every batch. Rerun `--sync` after an interruption or to pick up
new mail; `--build` starts from scratch.

    python3 mail_attachments.py --sync      # resumable, incremental
    python3 mail_attachments.py --build     # from scratch
    python3 mail_attachments.py --status
    python3 mail_attachments.py --measure   # time the extractors on a random sample

Reading Mail's store needs Full Disk Access, like mail_index.py; nothing is ever
written under ~/Library/Mail.
"""

from __future__ import annotations

import argparse
import email
import email.policy
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import stat
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

import config
import mail_index
import mail_stem
from mail_tools import MailError

TOOLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools")
BUILD_DIR = os.path.join(TOOLS_DIR, "build")  # gitignored
SWIFTC = "/usr/bin/swiftc"
# A tool that stays silent this long is stuck (a file takes well under a second,
# the first line also pays for loading the frameworks and the OCR model).
TOOL_START_TIMEOUT = 90
TOOL_STALL_TIMEOUT = 40

OFFICE_EXTENSIONS = {".docx", ".xlsx", ".xlsm", ".pptx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".gif", ".tif", ".tiff"}
SKIPPED_EXTENSIONS = {".zip", ".rar", ".dwg", ".mp3", ".mp4", ".mov", ".m4a", ".wav"}

MAX_CHARS = 100_000
# textutil holds the whole converted document in memory; refuse absurd inputs.
DOC_MAX_BYTES = 20 * 1024 * 1024
# A zip part is read in full to be parsed; refuse absurd expansions.
MAX_XML_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Locating attachments
# ---------------------------------------------------------------------------


def attachment_files(store: str, identifier: int) -> list[str]:
    """Files kept on disk for a message id (empty for a full .emlx)."""
    found: list[str] = []
    for root in mail_index.mailbox_roots(store):
        for shard in mail_index.shard_candidates(identifier):
            folder = os.path.join(root, "Data", shard, "Attachments", str(identifier))
            for directory, _, names in os.walk(folder):
                found.extend(os.path.join(directory, name) for name in sorted(names))
    return found


def iter_disk_attachments(store: str):
    """Yields (message id, path) for every attachment file kept beside the messages."""
    for root in mail_index.mailbox_roots(store):
        for directory, _, names in os.walk(os.path.join(root, "Data")):
            parts = directory.split(os.sep)
            if "Attachments" not in parts:
                continue
            after = parts[parts.index("Attachments") + 1 :]
            if not after or not after[0].isdigit():
                continue
            for name in names:
                yield int(after[0]), os.path.join(directory, name)


_NAMED_PART = re.compile(rb"(?i)name\*?(?:\d+\*?)?\s*=")


def named_parts(emlx_path: str):
    """Yields (index, filename, content) for the named parts of a full .emlx.

    The file starts with the byte length of the MIME message on its own line,
    then the message, then an XML property list that is not part of it. The
    index counts the yielded parts, so it names one again on a later read.
    Most messages have no named part; a byte search spares parsing them.
    """
    with open(emlx_path, "rb") as handle:
        length = int(handle.readline().strip() or 0)
        raw = handle.read(length)
    if not _NAMED_PART.search(raw):
        return
    message = email.message_from_bytes(raw, policy=email.policy.default)
    index = 0
    for part in message.walk():
        filename = part.get_filename()
        if part.get_content_maintype() == "multipart" or not filename:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            yield index, filename, payload
            index += 1


def mime_attachments(emlx_path: str) -> list[tuple[str, bytes]]:
    """(filename, content) of the attachments inside a full .emlx."""
    return [(filename, payload) for _, filename, payload in named_parts(emlx_path)]


# ---------------------------------------------------------------------------
# Pure extractors
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML_BYTES:
        return None
    return archive.read(info)


def _xml_texts(data: bytes, text_tag: str, break_tags: set[str], limit: int) -> str:
    """Concatenates every `text_tag` element; a `break_tags` element ends a line."""
    out: list[str] = []
    size = 0
    for event, element in ElementTree.iterparse(_bytes_io(data), events=("end",)):
        name = _local(element.tag)
        if name == text_tag:
            if element.text:
                out.append(element.text)
                size += len(element.text)
        elif name in break_tags:
            out.append("\n")
        element.clear()
        if size >= limit:
            break
    return "".join(out)


def _bytes_io(data: bytes):
    import io

    return io.BytesIO(data)


def extract_docx(path: str, limit: int = MAX_CHARS) -> str:
    with zipfile.ZipFile(path) as archive:
        data = _read_member(archive, "word/document.xml")
        if data is None:
            return ""
        return _xml_texts(data, "t", {"p", "br", "tab"}, limit)[:limit]


def extract_pptx(path: str, limit: int = MAX_CHARS) -> str:
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
            key=lambda name: int(re.findall(r"\d+", name)[0]),
        )
        chunks: list[str] = []
        size = 0
        for name in slides:
            data = _read_member(archive, name)
            if data is None:
                continue
            text = _xml_texts(data, "t", {"p"}, limit - size)
            chunks.append(text)
            size += len(text)
            if size >= limit:
                break
        return "\n".join(chunks)[:limit]


def extract_xlsx(path: str, limit: int = MAX_CHARS) -> str:
    """Shared strings first (where Excel keeps nearly all text), then inline strings.

    Numbers are left out on purpose: they are noise for a text search and would
    eat the character cap on large sheets.
    """
    with zipfile.ZipFile(path) as archive:
        chunks: list[str] = []
        size = 0
        shared = _read_member(archive, "xl/sharedStrings.xml")
        if shared is not None:
            text = _xml_texts(shared, "t", {"si"}, limit)
            chunks.append(text)
            size += len(text)
        for name in sorted(archive.namelist()):
            if size >= limit:
                break
            if not re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name):
                continue
            data = _read_member(archive, name)
            if data is None or b"<is>" not in data and b"<is " not in data:
                continue  # no inline strings in this sheet
            text = _inline_strings(data, limit - size)
            chunks.append(text)
            size += len(text)
        return "\n".join(chunks)[:limit]


def _inline_strings(data: bytes, limit: int) -> str:
    out: list[str] = []
    size = 0
    inside = False
    for event, element in ElementTree.iterparse(_bytes_io(data), events=("start", "end")):
        name = _local(element.tag)
        if event == "start":
            if name == "is":
                inside = True
        elif name == "t" and inside and element.text:
            out.append(element.text)
            size += len(element.text)
        elif name == "is":
            inside = False
            out.append("\n")
        if event == "end" and name in {"c", "is"}:
            element.clear()
        if size >= limit:
            break
    return "".join(out)


def extract_doc(path: str, limit: int = MAX_CHARS) -> str:
    """Legacy Word through the system converter; empty when it cannot read the file."""
    if os.path.getsize(path) > DOC_MAX_BYTES:
        raise RuntimeError("doc too large")
    process = subprocess.Popen(["/usr/bin/textutil", "-convert", "txt", "-stdout", path],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    timer = threading.Timer(30, process.kill)
    timer.start()
    try:
        # Bytes, not characters: 4 per character is the UTF-8 worst case.
        data = process.stdout.read(limit * 4)
        capped = len(data) >= limit * 4
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()  # capped or timed out: stop a converter still writing
        process.wait()
    if process.returncode != 0 and not capped:
        raise RuntimeError("textutil failed")
    return data.decode("utf-8", errors="replace")[:limit]


def extract_office(path: str, limit: int = MAX_CHARS) -> str:
    extension = os.path.splitext(path)[1].lower()
    if extension == ".docx":
        return extract_docx(path, limit)
    if extension in (".xlsx", ".xlsm"):
        return extract_xlsx(path, limit)
    if extension == ".pptx":
        return extract_pptx(path, limit)
    if extension == ".doc":
        return extract_doc(path, limit)
    raise ValueError(f"unsupported office extension {extension}")


# ---------------------------------------------------------------------------
# Swift tools
# ---------------------------------------------------------------------------


def compile_tool(name: str, build_dir: str = BUILD_DIR) -> str:
    """Path of the compiled tool, rebuilt when its source is newer."""
    source = os.path.join(TOOLS_DIR, f"{name}.swift")
    binary = os.path.join(build_dir, name)
    if os.path.exists(binary) and os.path.getmtime(binary) >= os.path.getmtime(source):
        return binary
    if not os.path.exists(SWIFTC):
        raise MailError(
            "swiftc_missing",
            f"The Swift compiler is not installed ({SWIFTC}); PDF and image text need it.",
            "Install the command line tools: xcode-select --install",
        )
    os.makedirs(build_dir, exist_ok=True)
    # Compiled beside the final name and moved into place only once it runs: a
    # kill mid-compile must not leave a truncated binary newer than the source.
    partial = f"{binary}.{os.getpid()}.partial"
    try:
        result = subprocess.run([SWIFTC, "-O", source, "-o", partial], capture_output=True, text=True)
        if result.returncode == 0:
            probe = subprocess.run([partial], capture_output=True, timeout=30)  # no files: exits 0
            if probe.returncode != 0:
                result = subprocess.CompletedProcess([], 1, "", "the compiled tool does not run")
            else:
                os.replace(partial, binary)
    finally:
        try:
            os.unlink(partial)
        except FileNotFoundError:
            pass
    if result.returncode != 0:
        raise MailError(
            "swiftc_failed",
            f"Compiling {name}.swift failed: {result.stderr.strip()[-400:]}",
            "Check that the command line tools are complete: xcode-select --install",
        )
    return binary


def run_tool(binary: str, paths: list[str], options: list[str] | None = None,
             first_timeout: float | None = None, stall_timeout: float | None = None):
    """Runs a batch tool; yields (result dict, milliseconds spent on that file).

    The tool flushes one JSON line per file, so the gap between two lines is the
    time that file took (the first one also carries the process start). A
    watchdog kills the process when no line arrives for `stall_timeout` seconds
    (`first_timeout` for the first, which loads the frameworks): a file that hangs
    PDFKit or Vision must not hang the run.
    """
    process = subprocess.Popen(
        [binary, *(options or []), *paths], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    first_timeout = TOOL_START_TIMEOUT if first_timeout is None else first_timeout
    stall_timeout = TOOL_STALL_TIMEOUT if stall_timeout is None else stall_timeout
    watchdog = threading.Timer(first_timeout, process.kill)
    watchdog.start()
    last = time.monotonic()
    try:
        assert process.stdout is not None
        for line in process.stdout:
            now = time.monotonic()
            watchdog.cancel()
            watchdog = threading.Timer(stall_timeout, process.kill)
            watchdog.start()
            try:
                result = json.loads(line)
            except ValueError:
                continue
            yield result, (now - last) * 1000
            last = now
    finally:
        watchdog.cancel()
        process.kill()
        process.wait()


def run_batch(binary: str, paths: list[str], options: list[str] | None = None) -> dict[str, dict]:
    """Results by path for every file, whatever happens to the tool.

    When the tool dies or is killed mid-batch, the first file without a result
    is the one it choked on: it is reported as failed and the rest go again.
    """
    results: dict[str, dict] = {}
    remaining = list(paths)
    while remaining:
        for result, _ in run_tool(binary, remaining, options):
            results[result["path"]] = result
        remaining = [path for path in remaining if path not in results]
        if remaining:
            results[remaining[0]] = {"path": remaining[0], "pages": 0, "text": "", "error": "tool_failed"}
            remaining = remaining[1:]
    return results


# ---------------------------------------------------------------------------
# Deciding what is worth reading
# ---------------------------------------------------------------------------

PDF_EXTENSIONS = {".pdf"}
OFFICE_TEXT_EXTENSIONS = OFFICE_EXTENSIONS | {".doc"}
OCR_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic", ".tif", ".tiff"}
MIN_IMAGE_BYTES = 50 * 1024
# Characters below which a text layer counts as absent: the PDF is a scan.
SCAN_THRESHOLD = 20
OCR_PAGES = 3
TEXT_PAGES = 50
# Mail clients name inline pictures (signatures, banners, social icons) in
# predictable ways; a photo from a phone or a scanner is left alone.
_DECORATION_NAME = re.compile(
    r"(?i)(logo|signature|banner|banniere|bannière|spacer|facebook|linkedin|twitter|instagram|youtube|icon)"
    r"|^(image\d+|outlook-[\w-]+|cid[_:].*|~wrd\d+)\.\w+$"
)


def classify(filename: str, size: int, ocr_images: bool, max_mb: int) -> tuple[str, str | None]:
    """("process", None), ("skip", reason) or ("too_big", None) for one attachment."""
    extension = os.path.splitext(filename)[1].lower()
    if extension not in PDF_EXTENSIONS | OFFICE_TEXT_EXTENSIONS | OCR_IMAGE_EXTENSIONS:
        return "skip", "type"
    if size <= 0:
        return "skip", "empty_file"
    limit = max_mb * 1024 * 1024
    if extension not in PDF_EXTENSIONS:
        limit //= 2
    if size > limit:
        return "too_big", None
    if extension in OCR_IMAGE_EXTENSIONS:
        if not ocr_images:
            return "skip", "ocr_off"
        if size < MIN_IMAGE_BYTES:
            return "skip", "small_image"
        if _DECORATION_NAME.search(filename):
            return "skip", "decoration"
    return "process", None


def tidy_text(text: str, limit: int) -> str:
    """One line of single spaced words, cut at the character cap."""
    return " ".join(text.split())[:limit]


# ---------------------------------------------------------------------------
# The attachment database
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1
FTS_WEIGHTS = (3.0, 1.0, 1.0, 0.25)  # filename, text, filename_stem, text_stem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    message INTEGER NOT NULL,
    part TEXT NOT NULL,          -- "<n>/<file>" under Attachments/<id>, or "mime:<n>" inside the .emlx
    path TEXT,                   -- where the file is on disk (null for a part of the MIME)
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime INTEGER NOT NULL,
    status TEXT NOT NULL,        -- pending | ok | empty | skipped | too_big | error
    reason TEXT,                 -- why skipped or failed
    method TEXT,                 -- text | ocr | office | textutil
    pages INTEGER,
    chars INTEGER,
    text TEXT,
    UNIQUE (message, part)
);
CREATE INDEX IF NOT EXISTS attachments_message ON attachments (message);
CREATE INDEX IF NOT EXISTS attachments_status ON attachments (status);
-- Full .emlx whose MIME has been listed: their attachments never change.
CREATE TABLE IF NOT EXISTS sources (message INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- Contentless like the message index: the text itself lives in attachments.text
-- (needed for snippets), this holds only the searchable words.
CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(
    filename, text, filename_stem, text_stem,
    content='', contentless_delete=1
);
"""


def database_path() -> str:
    """attachments_path, or attachments.sqlite beside the search index."""
    configured = config.get("attachments_path")
    if configured:
        return configured
    return os.path.join(os.path.dirname(config.get("index_path")), "attachments.sqlite")


def open_database(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.executescript(_SCHEMA)
    row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        connection.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        connection.commit()
    elif int(row[0]) != SCHEMA_VERSION:
        connection.close()
        raise MailError(
            "attachments_outdated",
            f"The attachment index uses schema version {row[0]}, this code needs {SCHEMA_VERSION}.",
            "Rebuild it: python3 mail_attachments.py --build",
        )
    return connection


def store_fts(connection: sqlite3.Connection, identifier: int, filename: str, text: str) -> None:
    connection.execute("DELETE FROM attachments_fts WHERE rowid = ?", (identifier,))
    connection.execute(
        "INSERT INTO attachments_fts (rowid, filename, text, filename_stem, text_stem) VALUES (?,?,?,?,?)",
        (identifier, filename, text, mail_stem.stem_text(filename), mail_stem.stem_text(text)),
    )


def delete_rows(connection: sqlite3.Connection, identifiers: list[int]) -> None:
    for start in range(0, len(identifiers), 500):
        chunk = identifiers[start : start + 500]
        marks = ",".join("?" * len(chunk))
        connection.executemany("DELETE FROM attachments_fts WHERE rowid = ?", [(i,) for i in chunk])
        connection.execute(f"DELETE FROM attachments WHERE id IN ({marks})", chunk)


# ---------------------------------------------------------------------------
# Discovery: what does the mail store hold, and what changed
# ---------------------------------------------------------------------------


@dataclass
class Found:
    message: int
    part: str
    path: str | None
    filename: str
    size: int
    mtime: int


def discover_disk(files: dict[int, str], store: str) -> list[Found]:
    found = []
    for identifier, path in iter_disk_attachments(store):
        if identifier not in files:
            continue
        try:
            info = os.stat(path)
        except OSError:
            continue
        part = path.split(f"{os.sep}Attachments{os.sep}{identifier}{os.sep}", 1)[-1]
        found.append(Found(identifier, part, path, os.path.basename(path), info.st_size, int(info.st_mtime)))
    return found


def discover_mime(identifier: int, emlx_path: str) -> list[Found]:
    try:
        return [
            Found(identifier, f"mime:{index}", None, filename, len(payload), 0)
            for index, filename, payload in named_parts(emlx_path)
        ]
    except (OSError, ValueError):
        return []


@dataclass
class SyncResult:
    seen: int = 0
    new: int = 0
    removed: int = 0
    processed: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    interrupted: bool = False
    seconds: float = 0.0


def _log(message: str) -> None:
    print(message, flush=True)


def discover(connection: sqlite3.Connection, store: str, files: dict[int, str], retry_errors: bool,
             log=_log) -> tuple[int, int]:
    """Brings the table in line with the store; returns (new or changed, removed).

    Every attachment worth reading becomes a `pending` row, so an interrupted
    run leaves its whole to-do list in the database: the next run has nothing
    to rediscover. Skips are recorded too, with their reason, so a file is
    judged once and a change of settings (OCR on or off) is picked up.
    """
    ocr_images = bool(config.get("attachments_ocr_images"))
    max_mb = int(config.get("attachments_max_mb"))
    existing = {
        (row["message"], row["part"]): row
        for row in connection.execute(
            "SELECT id, message, part, path, size, mtime, status, reason FROM attachments"
        )
    }
    scanned = {row[0] for row in connection.execute("SELECT message FROM sources")}
    changed = 0

    def record(item: Found) -> None:
        nonlocal changed
        action, reason = classify(item.filename, item.size, ocr_images, max_mb)
        status = {"process": "pending", "skip": "skipped", "too_big": "too_big"}[action]
        extension = os.path.splitext(item.filename)[1].lower()
        row = existing.get((item.message, item.part))
        if row is not None and row["size"] == item.size and row["mtime"] == item.mtime:
            before = row["status"]
            if before in ("ok", "empty", "pending"):
                unchanged = status == "pending"
            elif before == "error":
                unchanged = status == "pending" and not retry_errors
            else:  # skipped, too_big
                unchanged = before == status and row["reason"] == reason
            if unchanged:
                if row["path"] != item.path:
                    connection.execute("UPDATE attachments SET path = ? WHERE id = ?", (item.path, row["id"]))
                return
        changed += 1
        if row is not None:
            connection.execute("DELETE FROM attachments_fts WHERE rowid = ?", (row["id"],))
            connection.execute("DELETE FROM attachments WHERE id = ?", (row["id"],))
        connection.execute(
            "INSERT INTO attachments (message, part, path, filename, ext, size, mtime, status, reason)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (item.message, item.part, item.path, item.filename, extension, item.size, item.mtime, status, reason),
        )

    disk = discover_disk(files, store)
    seen = {(item.message, item.part) for item in disk}
    for item in disk:
        record(item)
    connection.commit()
    log(f"  {len(disk)} attachment files on disk")

    listed = 0
    full = [(identifier, path) for identifier, path in files.items()
            if not path.endswith(".partial.emlx") and identifier not in scanned]
    for position, (identifier, path) in enumerate(full, 1):
        for item in discover_mime(identifier, path):
            seen.add((item.message, item.part))
            record(item)
            listed += 1
        connection.execute("INSERT OR IGNORE INTO sources VALUES (?)", (identifier,))
        if position % 500 == 0:
            connection.commit()
    connection.commit()
    log(f"  {len(full)} full messages read, {listed} attachments inside them")
    # Parts of a message already listed earlier are still there.
    for (identifier, part), row in existing.items():
        if part.startswith("mime:") and identifier in files and identifier in scanned:
            seen.add((identifier, part))

    gone = [row["id"] for key, row in existing.items()
            if key[0] not in files or (key not in seen and not key[1].startswith("mime:"))]
    delete_rows(connection, gone)
    connection.executemany("DELETE FROM sources WHERE message = ?", [(m,) for m in scanned if m not in files])
    connection.commit()
    return changed, len(gone)


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------

BATCH_ROWS = 40


def _extract_batch(rows: list[sqlite3.Row], files: dict[int, str], workspace: str, limit: int) -> dict[int, dict]:
    """Extraction outcome by row id: status, reason, method, pages, text."""
    outcome: dict[int, dict] = {}
    paths: dict[int, str] = {}
    by_message: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        if row["part"].startswith("mime:"):
            by_message.setdefault(row["message"], []).append(row)
        elif row["path"] and os.path.isfile(row["path"]):
            paths[row["id"]] = row["path"]
        else:
            outcome[row["id"]] = {"status": "error", "reason": "file_missing"}
    for message, parts in by_message.items():
        emlx = files.get(message)
        try:
            payloads = {index: payload for index, _, payload in named_parts(emlx)} if emlx else {}
        except (OSError, ValueError):
            payloads = {}
        for row in parts:
            payload = payloads.get(int(row["part"].split(":", 1)[1]))
            if payload is None:
                outcome[row["id"]] = {"status": "error", "reason": "part_missing"}
                continue
            target = os.path.join(workspace, f"{row['id']}{row['ext']}")
            with open(target, "wb") as handle:
                handle.write(payload)
            paths[row["id"]] = target

    def finish(identifier: int, method: str, pages: int, text: str, error: str | None) -> None:
        if error:
            outcome[identifier] = {"status": "error", "reason": error, "method": method}
            return
        text = tidy_text(text, limit)
        outcome[identifier] = {
            "status": "ok" if len(text) >= 3 else "empty",
            "method": method, "pages": pages, "text": text,
        }

    by_id = {row["id"]: row for row in rows}
    pdf_ids = [i for i in paths if by_id[i]["ext"] in PDF_EXTENSIONS]
    image_ids = [i for i in paths if by_id[i]["ext"] in OCR_IMAGE_EXTENSIONS]
    for identifier in paths:
        if identifier in pdf_ids or identifier in image_ids:
            continue
        try:
            method = "textutil" if by_id[identifier]["ext"] == ".doc" else "office"
            finish(identifier, method, 0, extract_office(paths[identifier], limit), None)
        except Exception as failure:  # noqa: BLE001 - corrupt zip, bad XML, textutil refusing
            finish(identifier, "office", 0, "", type(failure).__name__.lower())

    scans: list[int] = []
    if pdf_ids:
        tool = compile_tool("pdftext")
        results = run_batch(tool, [paths[i] for i in pdf_ids],
                            ["--max-pages", str(TEXT_PAGES), "--max-chars", str(limit)])
        for identifier in pdf_ids:
            result = results[paths[identifier]]
            if result["error"]:
                finish(identifier, "text", result["pages"], "", result["error"])
            elif len(result["text"].strip()) < SCAN_THRESHOLD and result["pages"] > 0:
                scans.append(identifier)
            else:
                finish(identifier, "text", result["pages"], result["text"], None)
    to_ocr = scans + image_ids
    if to_ocr:
        tool = compile_tool("ocr")
        results = run_batch(tool, [paths[i] for i in to_ocr],
                            ["--level", "accurate", "--max-pages", str(OCR_PAGES), "--max-chars", str(limit)])
        for identifier in to_ocr:
            result = results[paths[identifier]]
            finish(identifier, "ocr", result["pages"], result["text"], result["error"])
    return outcome


def process_pending(connection: sqlite3.Connection, files: dict[int, str], lock, limit_rows: int | None,
                    log=_log) -> int:
    """Reads every pending attachment, BATCH_ROWS at a time, one commit per batch."""
    pending = [row["id"] for row in connection.execute("SELECT id FROM attachments WHERE status = 'pending' ORDER BY id")]
    if limit_rows is not None:
        pending = pending[:limit_rows]
    char_limit = int(config.get("attachments_char_limit"))
    started = time.time()
    done = 0
    for start in range(0, len(pending), BATCH_ROWS):
        chunk = pending[start : start + BATCH_ROWS]
        rows = connection.execute(
            f"SELECT * FROM attachments WHERE id IN ({','.join('?' * len(chunk))}) ORDER BY id", chunk
        ).fetchall()
        # Payloads of MIME parts are real mail content: a private directory,
        # emptied after every batch and whatever happens (see the finally).
        workspace = tempfile.mkdtemp(prefix=WORKSPACE_PREFIX)
        try:
            outcome = _extract_batch(rows, files, workspace, char_limit)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        for row in rows:
            result = outcome.get(row["id"], {"status": "error", "reason": "no_result"})
            text = result.get("text", "")
            connection.execute(
                "UPDATE attachments SET status = ?, reason = ?, method = ?, pages = ?, chars = ?, text = ? WHERE id = ?",
                (result["status"], result.get("reason"), result.get("method"), result.get("pages"),
                 len(text), text or None, row["id"]),
            )
            if text:
                store_fts(connection, row["id"], row["filename"], text)
        connection.commit()
        lock.touch()
        done += len(rows)
        rate = done / max(time.time() - started, 0.001)
        log(f"  {done}/{len(pending)} read, {rate:.1f}/s, about {(len(pending) - done) / max(rate, 0.001) / 60:.0f} min left")
    return done


WORKSPACE_PREFIX = "mail-attachments-"


def sweep_workspaces(older_than: float = 300.0) -> int:
    """Removes private work directories a killed run left behind; returns how many.

    They hold decoded attachments of real mail. Only called with the sync lock
    held, so no live run of ours can own one; the age and owner checks protect
    anything else that happens to share the prefix.
    """
    root = tempfile.gettempdir()
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            info = os.lstat(path)
            if (not name.startswith(WORKSPACE_PREFIX) or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid() or time.time() - info.st_mtime < older_than):
                continue
            shutil.rmtree(path)
            removed += 1
        except OSError:
            continue
    return removed


class _Terminated(SystemExit):
    """Raised by SIGTERM so the cleanup in finally blocks runs."""


def _on_sigterm(signum, frame):  # noqa: ANN001
    raise _Terminated(143)


def sync(store: str, database: str, limit_rows: int | None = None, retry_errors: bool = False,
         rebuild: bool = False, log=_log) -> SyncResult:
    """Discovers, then reads what is pending. Safe to interrupt and to rerun."""
    import mail_index

    started = time.time()
    lock = mail_index.IndexLock(database)
    lock.acquire()
    try:
        previous = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread
        previous = None
    result = SyncResult()
    connection = None
    try:
        if rebuild:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(database + suffix)
                except FileNotFoundError:
                    pass
        swept = sweep_workspaces()
        if swept:
            log(f"removed {swept} work directories left by an interrupted run")
        connection = open_database(database)
        log("scanning message files...")
        files = mail_index.scan_message_files(store)
        log(f"  {len(files)} messages")
        result.new, result.removed = discover(connection, store, files, retry_errors, log)
        try:
            result.processed = process_pending(connection, files, lock, limit_rows, log)
        except (KeyboardInterrupt, SystemExit):
            result.interrupted = True
            raise
        finally:
            connection.commit()
    finally:
        result.seconds = time.time() - started
        if connection is not None:
            complete = not result.interrupted and not connection.execute(
                "SELECT 1 FROM attachments WHERE status = 'pending' LIMIT 1").fetchone()
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('last_run', ?)", (str(int(time.time())),))
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('last_run_seconds', ?)", (str(round(result.seconds)),))
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('last_run_complete', ?)", ("1" if complete else "0",))
            connection.commit()
            result.by_status = dict(connection.execute("SELECT status, count(*) FROM attachments GROUP BY status").fetchall())
            connection.close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
        lock.release()
    return result


# ---------------------------------------------------------------------------
# Searching and reporting (read only)
# ---------------------------------------------------------------------------

_OWN_COLUMNS = {"filename", "text", "filename_stem", "text_stem"}
_FILTER = re.compile(r"\{([^{}]*)\}\s*:|(?<![\w])(subject|sender|to|cc|attachments|body)(?:_stem)?\s*:", re.I)


def fts_query(query: str) -> str | None:
    """The message query rewritten for the attachment table.

    None when it restricts words to a message column (subject:, sender:...):
    those say nothing about an attachment, so it is not searched.
    """
    rewritten = mail_stem.rewrite_query(query)
    rewritten = rewritten.replace("{subject sender to cc attachments body}", "{filename text}")
    rewritten = rewritten.replace("{subject_stem attachments_stem body_stem}", "{filename_stem text_stem}")
    unquoted = re.sub(r'"[^"]*"', '""', rewritten)
    for match in _FILTER.finditer(unquoted):
        names = (match.group(1) or match.group(2) or "").lower().split()
        if not names or not set(names) <= _OWN_COLUMNS:
            return None
    return rewritten


@dataclass
class Hit:
    message: int
    attachment: int
    filename: str
    score: float  # weighted bm25, negative: the more negative, the better


def search_hits(query: str, limit: int = 300, path: str | None = None,
                allowed: list[int] | None = None) -> list[Hit]:
    """Best attachment per message for a query, best first; [] when there is no index."""
    path = path or database_path()
    if not os.path.isfile(path):
        return []
    match = fts_query(query)
    if match is None:
        return []
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        weights = ", ".join(str(weight) for weight in FTS_WEIGHTS)
        statement = (
            f"SELECT a.message, a.id, a.filename, bm25(attachments_fts, {weights}) AS score"
            " FROM attachments_fts JOIN attachments a ON a.id = attachments_fts.rowid"
            " WHERE attachments_fts MATCH ? ORDER BY score LIMIT ?"
        )
        if allowed is not None:
            # Only these messages count: the cap then applies among them.
            connection.execute("CREATE TEMP TABLE allowed (message INTEGER PRIMARY KEY)")
            connection.executemany("INSERT OR IGNORE INTO allowed VALUES (?)", [(i,) for i in allowed])
            statement = statement.replace(
                " WHERE attachments_fts MATCH ?",
                " WHERE a.message IN (SELECT message FROM allowed) AND attachments_fts MATCH ?")
        rows = connection.execute(statement, (match, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    best: dict[int, Hit] = {}
    for message, identifier, filename, score in rows:
        if message not in best:
            best[message] = Hit(message, identifier, filename, score)
    return list(best.values())


def attachment_text(attachment: int, path: str | None = None) -> str:
    path = path or database_path()
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT text FROM attachments WHERE id = ?", (attachment,)).fetchone()
    finally:
        connection.close()
    return (row[0] or "") if row else ""


def status(path: str | None = None) -> dict:
    """What the attachment index holds; {"built": False} when it does not exist."""
    path = path or database_path()
    if not os.path.isfile(path):
        return {"built": False, "database": path}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        by_status = dict(connection.execute("SELECT status, count(*) FROM attachments GROUP BY status").fetchall())
        by_method = dict(connection.execute(
            "SELECT method, count(*) FROM attachments WHERE status = 'ok' GROUP BY method").fetchall())
        meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.DatabaseError:
        return {"built": False, "database": path, "note": "unreadable"}
    finally:
        connection.close()
    last = int(meta["last_run"]) if meta.get("last_run") else None
    return {
        "built": True,
        "database": path,
        "size_mb": round(os.path.getsize(path) / 1024 / 1024),
        "files_by_status": by_status,
        "readable_by_method": by_method,
        "pending": by_status.get("pending", 0),
        "last_run": time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if last else None,
        "last_run_seconds": int(meta["last_run_seconds"]) if meta.get("last_run_seconds") else None,
        "last_run_complete": meta.get("last_run_complete") == "1",
    }


def start_background_sync() -> bool:
    """Starts `--sync` detached, once an attachment index exists; True when started.

    Never waits and never fails the caller. If another run holds the lock the
    new one exits at once (exit code 75), so calling this often is harmless.
    """
    if not config.get("attachments_auto_sync") or not os.path.isfile(database_path()):
        return False
    try:
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--sync"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "MAIL_MCP_ATTACHMENTS_PATH": database_path()},
        )
    except OSError:
        return False
    # Reap the child when it ends, or it stays a zombie until the next Popen.
    threading.Thread(target=child.wait, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _summary(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    times = sorted(row["ms"] for row in rows)
    lengths = [len(row["text"]) for row in rows]
    with_text = [length for length in lengths if length >= 20]
    return {
        "n": len(rows),
        "ms_mean": round(statistics.mean(times), 1),
        "ms_p95": round(times[min(len(times) - 1, int(len(times) * 0.95))], 1),
        "ms_max": round(times[-1], 1),
        "with_text_pct": round(100 * len(with_text) / len(rows)),
        "chars_median": int(statistics.median(with_text)) if with_text else 0,
        "chars_mean": int(statistics.mean(with_text)) if with_text else 0,
        "errors": {
            kind: sum(1 for row in rows if row["error"] == kind)
            for kind in {row["error"] for row in rows if row["error"]}
        },
    }


def _measure_office(paths: list[str]) -> list[dict]:
    rows = []
    for path in paths:
        started = time.monotonic()
        try:
            text, error = extract_office(path), None
        except Exception as failure:  # corrupt zip, bad XML, timeout...
            text, error = "", type(failure).__name__
        rows.append({"path": path, "text": text, "error": error, "ms": (time.monotonic() - started) * 1000})
    return rows


def _measure_tool(binary: str, paths: list[str], options: list[str], batch: int = 20) -> list[dict]:
    rows = []
    for start in range(0, len(paths), batch):
        for result, ms in run_tool(binary, paths[start : start + batch], options):
            rows.append({"path": result["path"], "text": result["text"], "error": result["error"], "ms": ms})
    return rows


def measure(store: str, sizes: dict[str, int], seed: int, max_bytes: int) -> dict:
    """Times every extractor on a random sample of the attachments on disk."""
    by_extension: dict[str, list[str]] = {}
    total_by_extension: dict[str, int] = {}
    for _, path in iter_disk_attachments(store):
        extension = os.path.splitext(path)[1].lower()
        total_by_extension[extension] = total_by_extension.get(extension, 0) + 1
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= max_bytes:
            by_extension.setdefault(extension, []).append(path)
    rng = random.Random(seed)

    def draw(extensions: set[str], count: int, minimum_bytes: int = 0) -> list[str]:
        pool = [
            path
            for extension in extensions
            for path in by_extension.get(extension, [])
            if minimum_bytes == 0 or os.path.getsize(path) >= minimum_bytes
        ]
        return rng.sample(pool, min(count, len(pool)))

    pdftext = compile_tool("pdftext")
    ocr = compile_tool("ocr")
    report: dict = {"disk_files_by_extension": dict(sorted(total_by_extension.items(), key=lambda kv: -kv[1])[:20])}

    pdfs = draw({".pdf"}, sizes["pdf_probe"])
    rows = _measure_tool(pdftext, pdfs, [])
    report["pdf_text"] = _summary(rows[: sizes["pdf"]])
    report["pdf_probe_scanned_pct"] = round(100 * sum(1 for r in rows if len(r["text"].strip()) < 20 and not r["error"]) / max(1, len(rows)))
    scanned = [r["path"] for r in rows if len(r["text"].strip()) < 20 and not r["error"]][: sizes["scanned"]]

    for kind, extensions in (("docx", {".docx"}), ("xlsx", {".xlsx", ".xlsm"}), ("doc", {".doc"})):
        report[kind] = _summary(_measure_office(draw(extensions, sizes[kind])))

    images = draw({".png", ".jpg", ".jpeg", ".heic"}, sizes["image"])
    big_images = draw({".png", ".jpg", ".jpeg", ".heic"}, sizes["image"], minimum_bytes=50 * 1024)
    for level in ("fast", "accurate"):
        options = ["--level", level, "--max-pages", "3"]
        report[f"ocr_{level}_images_random"] = _summary(_measure_tool(ocr, images, options))
        report[f"ocr_{level}_images_over_50kb"] = _summary(_measure_tool(ocr, big_images, options))
        report[f"ocr_{level}_scanned_pdf"] = _summary(_measure_tool(ocr, scanned, options, batch=5))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--sync", action="store_true", help="discover and read what is new; resumes an interrupted run")
    action.add_argument("--build", action="store_true", help="start from scratch")
    action.add_argument("--status", action="store_true", help="what the attachment index holds")
    action.add_argument("--measure", action="store_true", help="time the extractors on a random sample")
    parser.add_argument("--database", default=None, help="default: attachments_path or beside the index")
    parser.add_argument("--limit", type=int, default=None, help="read at most this many attachments in this run")
    parser.add_argument("--retry-errors", action="store_true", help="read again the files that failed")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-mb", type=int, default=25, help="--measure: skip files larger than this")
    parser.add_argument("--pdf", type=int, default=100)
    parser.add_argument("--docx", type=int, default=30)
    parser.add_argument("--xlsx", type=int, default=30)
    parser.add_argument("--doc", type=int, default=20)
    parser.add_argument("--image", type=int, default=50)
    parser.add_argument("--scanned", type=int, default=20)
    parser.add_argument("--pdf-probe", type=int, default=300, help="--measure: PDFs read to find scanned ones")
    args = parser.parse_args(argv)
    database = args.database or database_path()

    if args.status:
        print(json.dumps(status(database), indent=1))
        return 0
    try:
        store = mail_index.find_store()
    except PermissionError:
        print("Permission denied on ~/Library/Mail.")
        print("Grant Full Disk Access to the app running this script, then try again.")
        return 1
    except FileNotFoundError as error:
        print(error)
        return 1
    if args.measure:
        sizes = {name: getattr(args, name.replace("-", "_")) for name in
                 ("pdf", "docx", "xlsx", "doc", "image", "scanned", "pdf_probe")}
        print(json.dumps(measure(store, sizes, args.seed, args.max_mb * 1024 * 1024), indent=1))
        return 0
    try:
        result = sync(store, database, args.limit, args.retry_errors, rebuild=args.build)
    except mail_index.IndexBusy:
        print("Another attachment run holds the lock; try again when it ends.")
        return mail_index.LOCK_EXIT_CODE
    except MailError as error:
        print(f"{error.code}: {error.message}")
        if error.hint:
            print(error.hint)
        return 1
    except PermissionError:
        print("Permission denied on ~/Library/Mail. Grant Full Disk Access, then try again.")
        return 1
    print(f"done in {result.seconds:.0f} s: {result.new} new or changed, {result.removed} removed, "
          f"{result.processed} read; by status {result.by_status}")
    print(f"attachments: {database} ({os.path.getsize(database) / 1024 / 1024:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
