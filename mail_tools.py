"""Business logic for the Mail.app MCP server.

Every function here drives Mail through AppleScript and returns plain Python
dictionaries. Nothing in this module knows about MCP, so it can be exercised
directly from test_manual.py.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import config

# Field and record separators used by the AppleScript side. They are stripped
# from values before joining, so no escaping is needed when splitting back.
FIELD_SEPARATOR = "\x1f"
RECORD_SEPARATOR = "\x1e"

SCRIPT_DIRECTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "applescript")
COMMON_SCRIPT = "_common"

# Mail can take a long time on a large mailbox, so the ceiling is generous.
DEFAULT_TIMEOUT = config.get("applescript_timeout")
WRITE_TIMEOUT = config.get("applescript_write_timeout")

FLAG_COLORS = {
    "red": 0,
    "orange": 1,
    "yellow": 2,
    "green": 3,
    "blue": 4,
    "purple": 5,
    "gray": 6,
    "grey": 6,
}

PERMISSION_HINT = (
    "macOS is blocking control of Mail. Open System Settings > Privacy & Security > "
    "Automation and enable Mail for the application running this server (Terminal, "
    "iTerm, Claude Code...), then start the server again."
)


class MailError(Exception):
    """An error that can be reported to the caller as structured data."""

    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error_code": self.code, "error": self.message}
        if self.hint:
            payload["hint"] = self.hint
        return payload


@dataclass(frozen=True)
class MessageReference:
    """Locates a message: Mail's own integer id plus where it was found.

    Mail's message id is stable across relaunches but is only unique within a
    mailbox, so the account and mailbox are carried along with it.
    """

    account: str
    mailbox: str
    identifier: int

    def encode(self) -> str:
        payload = json.dumps(
            {"a": self.account, "m": self.mailbox, "i": self.identifier},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, token: str) -> "MessageReference":
        try:
            padded = token + "=" * (-len(token) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
            return cls(account=payload["a"], mailbox=payload["m"], identifier=int(payload["i"]))
        except Exception as exc:  # noqa: BLE001 - any malformed token lands here
            raise MailError(
                "invalid_message_id",
                f"Unusable message_id: {token!r}",
                "Use a message_id returned by list_messages or search_messages.",
            ) from exc


def _load_script(name: str) -> str:
    path = os.path.join(SCRIPT_DIRECTORY, f"{name}.applescript")
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _build_script(name: str, apple_event_timeout: int) -> str:
    """Assembles the shared handlers, the tool script and a timeout wrapper.

    The tool script's own "on run" is renamed so the wrapper can call it from
    inside a "with timeout" block. Without that block every Apple event would
    give up after Mail's default 60 seconds, which a large mailbox exceeds.
    """
    body = _load_script(name)
    body = re.sub(r"^on run argv$", "on mainRun(argv)", body, count=1, flags=re.MULTILINE)
    body = re.sub(r"^end run$", "end mainRun", body, count=1, flags=re.MULTILINE)
    wrapper = (
        "\non run argv\n"
        f"\twith timeout of {apple_event_timeout} seconds\n"
        "\t\treturn mainRun(argv)\n"
        "\tend timeout\n"
        "end run\n"
    )
    return _load_script(COMMON_SCRIPT) + "\n" + body + wrapper


def _classify_error(stderr: str) -> MailError:
    text = stderr.strip()

    match = re.search(r"MAILERR:([a-z_]+):(.*)", text)
    if match:
        code, detail = match.group(1), match.group(2).strip()
        # Drop the AppleScript error number osascript appends, keep the detail.
        detail = re.sub(r"\s*\(-?\d+\)\s*$", "", detail).strip()
        detail = f"{code.replace('_', ' ')}: {detail}" if detail else code
        hints = {
            "account_not_found": "Call list_mailboxes to see the exact account names.",
            "mailbox_not_found": "Call list_mailboxes to see the exact mailbox paths.",
            "inbox_not_found": "This account has no mailbox named INBOX; pass an explicit mailbox.",
            "message_not_found": "The message may have been moved or deleted; search for it again.",
        }
        return MailError(code, detail, hints.get(code))

    if "-1743" in text or "not authorized" in text.lower() or "not authorised" in text.lower():
        return MailError("permission_denied", "macOS refused control of Mail.", PERMISSION_HINT)
    if "-1712" in text or "timed out" in text.lower():
        return MailError(
            "apple_event_timeout",
            "Mail did not answer in time.",
            "The mailbox is probably too large for this operation; lower limit or scan_limit, "
            "or target a smaller mailbox.",
        )
    if "-600" in text or "-609" in text or "isn't running" in text:
        return MailError(
            "mail_not_running",
            "Mail is not running and could not be started.",
            "Open Mail, wait for the accounts to load, then try again.",
        )
    if "-1728" in text or "-1719" in text:
        return MailError("not_found", text, "Check the account, mailbox and message id.")
    return MailError("applescript_error", text or "Unknown AppleScript failure.")


def run_script(
    name: str,
    args: Sequence[str] = (),
    timeout: int = DEFAULT_TIMEOUT,
) -> str:
    """Runs an AppleScript file and returns its raw stdout."""
    script = _build_script(name, apple_event_timeout=timeout)
    # "--" keeps osascript from reading a value starting with "-" as an option.
    command = ["osascript", "-e", script, "--", *[str(a) for a in args]]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout + 15,
        )
    except FileNotFoundError as exc:
        raise MailError("osascript_missing", "osascript was not found; this server needs macOS.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MailError(
            "timeout",
            f"The AppleScript call exceeded {timeout + 15} s and was aborted.",
            "Lower limit or scan_limit, or target a smaller mailbox.",
        ) from exc

    stdout = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        raise _classify_error(completed.stderr.decode("utf-8", errors="replace"))
    # osascript appends a newline to the returned text.
    return stdout[:-1] if stdout.endswith("\n") else stdout


def _parse_records(raw: str) -> list[list[str]]:
    if raw == "":
        return []
    return [record.split(FIELD_SEPARATOR) for record in raw.split(RECORD_SEPARATOR)]


def _field(record: Sequence[str], index: int, default: str = "") -> str:
    return record[index] if index < len(record) else default


def _as_bool(value: str) -> bool:
    return value.strip().lower() == "true"


def _as_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _join_list(values: Iterable[str] | None) -> str:
    if not values:
        return ""
    return FIELD_SEPARATOR.join(str(value) for value in values if str(value) != "")


def _as_address_list(value: str | Sequence[str] | None) -> list[str]:
    """Accepts a list, or a string holding comma or semicolon separated addresses."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;]", value)
    else:
        parts = list(value)
    return [part.strip() for part in parts if str(part).strip()]


def _message_from_row(row: Sequence[str], queried_account: str, queried_mailbox: str) -> dict[str, Any]:
    identifier = _as_int(_field(row, 0))
    reference = MessageReference(
        account=queried_account,
        mailbox=queried_mailbox,
        identifier=identifier,
    )
    message: dict[str, Any] = {
        "message_id": reference.encode(),
        "mail_id": identifier,
        "subject": _field(row, 1),
        "sender": _field(row, 2),
        "date_received": _field(row, 3),
        "read": _as_bool(_field(row, 4)),
        "flagged": _as_bool(_field(row, 5)),
        "mailbox": _field(row, 6),
        "account": _field(row, 7),
    }
    preview = _field(row, 8)
    if preview:
        message["preview"] = preview
    return message


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def list_mailboxes(include_totals: bool = False, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    raw = run_script("list_mailboxes", ["1" if include_totals else "0"], timeout=timeout)
    accounts: dict[str, dict[str, Any]] = {}
    for row in _parse_records(raw):
        account_name = _field(row, 0)
        entry = accounts.setdefault(account_name, {"account": account_name, "mailboxes": []})
        mailbox: dict[str, Any] = {
            "path": _field(row, 1),
            "unread": _as_int(_field(row, 2)),
        }
        total = _field(row, 3)
        if total != "":
            mailbox["total"] = _as_int(total)
        entry["mailboxes"].append(mailbox)
    return {
        "ok": True,
        "accounts": list(accounts.values()),
        "mailbox_count": sum(len(entry["mailboxes"]) for entry in accounts.values()),
    }


def list_messages(
    mailbox: str | None = None,
    account: str | None = None,
    limit: int = 20,
    unread_only: bool = False,
    scan_limit: int | None = None,
    include_preview: bool = False,
    preview_chars: int = 200,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    mailbox_argument = mailbox or ""
    account_argument = account or ""
    # Without a filter the window only has to be as deep as the limit; with one
    # it has to be deeper, since the matching messages are scattered in it.
    if scan_limit is None:
        scan_limit = max(limit * 10, 200) if unread_only else limit
    scan_limit = max(limit, min(int(scan_limit), 2000))

    raw = run_script(
        "list_messages",
        [
            account_argument,
            mailbox_argument,
            str(limit),
            "1" if unread_only else "0",
            str(scan_limit),
            "1" if include_preview else "0",
            str(max(20, int(preview_chars))),
        ],
        timeout=timeout,
    )
    records = _parse_records(raw)
    if not records:
        return {"ok": True, "messages": [], "total_in_mailbox": 0, "scanned": 0}

    meta = records[0]
    messages = [
        _message_from_row(row, account_argument, mailbox_argument) for row in records[1:]
    ]
    total = _as_int(_field(meta, 0))
    scanned = _as_int(_field(meta, 1))
    return {
        "ok": True,
        "messages": messages,
        "total_in_mailbox": total,
        "scanned": scanned,
        # True when messages older than the window were never looked at.
        "window_truncated": bool(unread_only and scanned < total),
    }


def search_messages(
    query: str,
    mailbox: str | None = None,
    account: str | None = None,
    field: str = "all",
    limit: int = 20,
    scan_limit: int = 300,
    body_scan_limit: int = 25,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Searches the most recent messages of a mailbox.

    Mail's own "whose" search is not used: it walks the whole mailbox inside
    Mail and takes well over two minutes on a few thousand messages, which no
    caller can wait for. Instead a bounded window of recent messages is pulled
    in bulk and filtered here, and bodies are only fetched for the leftover
    candidates. Coverage is reported so the caller knows what was searched.
    """
    field = field.lower().strip()
    if field not in {"all", "subject", "sender", "body"}:
        raise MailError(
            "invalid_field",
            f"Unknown field: {field!r}",
            "Use one of: all, subject, sender, body.",
        )
    if not query.strip():
        raise MailError("empty_query", "The query is empty.")

    limit = max(1, min(int(limit), 100))
    scan_limit = max(limit, min(int(scan_limit), 2000))
    body_scan_limit = max(0, min(int(body_scan_limit), 200))
    mailbox_argument = mailbox or ""
    account_argument = account or ""
    tokens = [token.lower() for token in query.split() if token]

    def matches(*values: str) -> bool:
        haystack = " ".join(values).lower()
        return all(token in haystack for token in tokens)

    raw = run_script(
        "list_messages",
        [
            account_argument,
            mailbox_argument,
            str(scan_limit),
            "0",
            str(scan_limit),
            "0",
            "200",
        ],
        timeout=timeout,
    )
    records = _parse_records(raw)
    if not records:
        return {"ok": True, "messages": [], "scanned": 0, "bodies_scanned": 0, "query": query}

    meta = records[0]
    rows = records[1:]
    total = _as_int(_field(meta, 0))
    scanned = _as_int(_field(meta, 1))

    hits: list[dict[str, Any]] = []
    leftovers: list[Sequence[str]] = []
    for row in rows:
        subject = _field(row, 1)
        sender = _field(row, 2)
        if field == "subject":
            candidate = matches(subject)
        elif field == "sender":
            candidate = matches(sender)
        elif field == "body":
            candidate = False
        else:
            candidate = matches(subject, sender)
        if candidate:
            if len(hits) < limit:
                hits.append(_message_from_row(row, account_argument, mailbox_argument))
        elif field in {"all", "body"}:
            leftovers.append(row)

    bodies_scanned = 0
    body_budget_exhausted = False
    if field in {"all", "body"} and len(hits) < limit and body_scan_limit and leftovers:
        window = leftovers[:body_scan_limit]
        body_budget_exhausted = len(leftovers) > body_scan_limit
        positions = [_field(row, 9) for row in window]
        body_raw = run_script(
            "get_bodies",
            [
                account_argument,
                mailbox_argument,
                _join_list(positions),
                "4000",
            ],
            timeout=timeout,
        )
        bodies = {
            _as_int(_field(record, 0)): _field(record, 1) for record in _parse_records(body_raw)
        }
        bodies_scanned = len(bodies)
        for row in window:
            if len(hits) >= limit:
                break
            body = bodies.get(_as_int(_field(row, 9)), "")
            if body and matches(body):
                hits.append(_message_from_row(row, account_argument, mailbox_argument))
        hits.sort(key=lambda message: message["date_received"], reverse=True)

    result = {
        "ok": True,
        "query": query,
        "field": field,
        "messages": hits,
        "scanned": scanned,
        "total_in_mailbox": total,
        "bodies_scanned": bodies_scanned,
        "coverage": "recent_window",
    }
    if scanned < total:
        result["note"] = (
            f"Only the {scanned} most recent messages of {total} were searched. "
            "Raise scan_limit to go further back."
        )
    if body_budget_exhausted:
        result["body_note"] = (
            f"Bodies were only read for {body_scan_limit} messages of the window; "
            "reading a body costs about a second each."
        )
    return result


def get_message(message_id: str, max_body_chars: int = 20000, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    reference = MessageReference.decode(message_id)
    raw = run_script(
        "get_message",
        [reference.account, reference.mailbox, str(reference.identifier), str(max(200, int(max_body_chars)))],
        timeout=timeout,
    )
    records = _parse_records(raw)
    if not records:
        raise MailError("message_not_found", "Mail returned nothing for this message.")
    row = records[0]

    attachments = []
    for part in _field(row, 13).split(";"):
        if not part:
            continue
        pieces = part.split("|")
        attachments.append(
            {
                "name": pieces[0] if pieces else "",
                "size": _as_int(pieces[1]) if len(pieces) > 1 else 0,
                "downloaded": _as_bool(pieces[2]) if len(pieces) > 2 else False,
            }
        )

    return {
        "ok": True,
        "message_id": message_id,
        "mail_id": _as_int(_field(row, 0)),
        "subject": _field(row, 1),
        "sender": _field(row, 2),
        "reply_to": _field(row, 3),
        "to": _field(row, 4),
        "cc": _field(row, 5),
        "bcc": _field(row, 6),
        "date_received": _field(row, 7),
        "read": _as_bool(_field(row, 8)),
        "flagged": _as_bool(_field(row, 9)),
        "rfc_message_id": _field(row, 10),
        "mailbox": _field(row, 11),
        "account": _field(row, 12),
        "attachments": attachments,
        "body_truncated": _as_bool(_field(row, 14)),
        "headers": _field(row, 15),
        "body": _field(row, 16),
    }


def count_unread(
    mailbox: str | None = None,
    account: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    raw = run_script("count_unread", [account or "", mailbox or ""], timeout=timeout)
    entries = [
        {
            "account": _field(row, 0),
            "mailbox": _field(row, 1),
            "unread": _as_int(_field(row, 2)),
        }
        for row in _parse_records(raw)
    ]
    return {
        "ok": True,
        "total_unread": sum(entry["unread"] for entry in entries),
        "mailboxes": entries,
    }


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


def _confirmation_needed(action: str, preview: dict[str, Any]) -> dict[str, Any]:
    """The answer given when a send was asked for without confirm=True.

    Rather than a bare refusal, it hands back exactly what would leave, so the
    caller can put it in front of the user before committing.
    """
    return {
        "ok": False,
        "error_code": "confirmation_required",
        "error": f"Nothing was sent: {action} needs confirm=true.",
        "hint": "Show this preview to the user, then call again with confirm=true.",
        "preview": preview,
    }


def _check_attachments(attachments: Sequence[str] | None) -> list[str]:
    paths: list[str] = []
    for raw_path in attachments or []:
        path = os.path.abspath(os.path.expanduser(str(raw_path)))
        if not os.path.isfile(path):
            raise MailError(
                "attachment_not_found",
                f"Attachment not found: {raw_path}",
                "Pass an absolute path to an existing file.",
            )
        paths.append(path)
    return paths


def _compose(
    mode: str,
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    attachments: Sequence[str] | None = None,
    sender: str | None = None,
    timeout: int = WRITE_TIMEOUT,
) -> dict[str, Any]:
    recipients = _as_address_list(to)
    if not recipients:
        raise MailError("no_recipient", "At least one recipient is required.")
    attachment_paths = _check_attachments(attachments)

    raw = run_script(
        "compose",
        [
            mode,
            _join_list(recipients),
            _join_list(_as_address_list(cc)),
            _join_list(_as_address_list(bcc)),
            subject,
            body,
            _join_list(attachment_paths),
            sender or "",
        ],
        timeout=timeout,
    )
    if mode == "send":
        # Noted so the autosave Mail may leave behind can be recognised and
        # swept later; it often appears too late to be caught here.
        try:
            import mail_files

            mail_files.record_sent(subject, recipients)
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a send
            pass

    records = _parse_records(raw)
    row = records[0] if records else []
    result: dict[str, Any] = {
        "ok": True,
        "mode": mode,
        "sent": mode == "send",
        "subject": _field(row, 1, subject),
        "to": recipients,
        "cc": _as_address_list(cc),
        "bcc": _as_address_list(bcc),
        "attachments": attachment_paths,
    }
    if mode == "draft":
        identifier = _field(row, 4)
        if identifier:
            result["message_id"] = MessageReference(
                account=_field(row, 6),
                mailbox=_field(row, 5),
                identifier=_as_int(identifier),
            ).encode()
            result["mailbox"] = _field(row, 5)
            result["account"] = _field(row, 6)
        else:
            result["note"] = (
                "Mail did not hand back an id for the draft; to send it later, find it "
                "with list_messages(mailbox='drafts')."
            )
    return result


def send_email(
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    attachments: Sequence[str] | None = None,
    sender: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    recipients = _as_address_list(to)
    if not recipients:
        raise MailError("no_recipient", "At least one recipient is required.")
    attachment_paths = _check_attachments(attachments)
    if not confirm:
        return _confirmation_needed(
            "sending this message",
            {
                "action": "send_email",
                "from": sender or "Mail's default account",
                "to": recipients,
                "cc": _as_address_list(cc),
                "bcc": _as_address_list(bcc),
                "subject": subject,
                "body": body,
                "attachments": [os.path.basename(path) for path in attachment_paths],
            },
        )
    return _compose("send", to, subject, body, cc, bcc, attachments, sender)


def create_draft(
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    attachments: Sequence[str] | None = None,
    sender: str | None = None,
) -> dict[str, Any]:
    return _compose("draft", to, subject, body, cc, bcc, attachments, sender)


def _draft_exists(reference: MessageReference) -> bool:
    try:
        run_script(
            "read_draft",
            [reference.account, reference.mailbox, str(reference.identifier)],
            timeout=DEFAULT_TIMEOUT,
        )
        return True
    except MailError:
        return False


def send_draft(message_id: str, confirm: bool = False) -> dict[str, Any]:
    """Sends a draft that already exists, with the content it holds.

    Mail offers no way to send a stored draft: "send" only understands an
    outgoing message (-1708), opening the draft turns it into one only after an
    unpredictable delay, and moving it to the Outbox does nothing. So the draft
    is read back and posted, then removed. The text that leaves is the text that
    was reviewed — it is read from the draft, never recomposed by the caller.
    """
    reference = MessageReference.decode(message_id)
    raw = run_script(
        "read_draft",
        [reference.account, reference.mailbox, str(reference.identifier)],
        timeout=DEFAULT_TIMEOUT,
    )
    records = _parse_records(raw)
    if not records:
        raise MailError("draft_unreadable", "Mail returned nothing for this draft.")
    row = records[0]

    subject = _field(row, 0)
    sender = _field(row, 1)
    to = _as_address_list(_field(row, 2))
    cc = _as_address_list(_field(row, 3))
    bcc = _as_address_list(_field(row, 4))
    attachment_names = [name for name in _field(row, 5).split("; ") if name]
    mailbox_path = _field(row, 6)
    account_name = _field(row, 7)
    body = _field(row, 8)

    if not to and not cc and not bcc:
        raise MailError(
            "draft_without_recipient",
            f"The draft {subject!r} has no recipient.",
            "Add one in Mail, then send it again.",
        )

    if not confirm:
        # The draft has been read but not touched: this is the cheapest way to
        # put its actual content in front of the user before it goes out. The
        # attachments are classified first, so the preview does not announce a
        # signature logo as a file the recipient would receive.
        real_names, inline_names = attachment_names, []
        if attachment_names:
            try:
                import mail_index

                real_names, inline_names = mail_index.extract_attachments(reference.identifier)
            except Exception:  # noqa: BLE001 - a preview must never fail
                pass
        return _confirmation_needed(
            "sending this draft",
            {
                "action": "send_draft",
                "from": sender,
                "to": to,
                "cc": cc,
                "bcc": bcc,
                "subject": subject,
                "body": body,
                "attachments": real_names,
                "kept_inline": inline_names,
                "drafted_in": {"account": account_name, "mailbox": mailbox_path},
            },
        )

    workspace: tempfile.TemporaryDirectory[str] | None = None
    attachment_paths: list[str] = []
    inline_parts: list[str] = []
    if attachment_names:
        # Mail refuses to export an attachment, so the files are read out of the
        # stored message. Dropping them silently is not an option.
        import mail_index

        workspace = tempfile.TemporaryDirectory(prefix="mcp-mail-draft-")
        try:
            attachment_paths, inline_parts = mail_index.extract_attachments(
                reference.identifier, workspace.name
            )
        except PermissionError as error:
            workspace.cleanup()
            raise MailError(
                "attachments_unreachable",
                f"The draft carries {len(attachment_names)} attachment(s) that cannot be read back.",
                "Full Disk Access is needed to re-attach them. Grant it, or open the draft in "
                "Mail and send it by hand.",
            ) from error
        except Exception as error:  # noqa: BLE001 - never send a truncated message
            workspace.cleanup()
            raise MailError(
                "attachments_unreachable",
                f"The draft's attachments could not be recovered: {error}",
                "Open the draft in Mail and send it by hand, so nothing is lost.",
            ) from error

        # Mail counts inline parts among the attachments, so the two lists only
        # have to add up once those are accounted for.
        if len(attachment_paths) + len(inline_parts) < len(attachment_names):
            workspace.cleanup()
            raise MailError(
                "attachments_incomplete",
                f"Only {len(attachment_paths)} of {len(attachment_names)} attachments could be "
                "recovered; nothing was sent.",
                "Open the draft in Mail and send it by hand, so nothing is lost.",
            )

    try:
        _compose(
            "send",
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            bcc=bcc,
            attachments=attachment_paths,
            sender=sender or None,
        )
    finally:
        if workspace is not None:
            workspace.cleanup()

    result: dict[str, Any] = {
        "ok": True,
        "sent": True,
        "subject": subject,
        "from": sender,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "attachments": [os.path.basename(path) for path in attachment_paths],
        "drafted_in": {"account": account_name, "mailbox": mailbox_path},
    }
    if inline_parts:
        # Worth stating: Mail listed these as attachments, they were not sent as such.
        result["kept_inline"] = inline_parts

    # Only once the message is gone: a failure here leaves a stray draft, which
    # is recoverable, where the reverse would lose the message.
    # Deleting once is not enough, and checking once even less. A Gmail account
    # pushes the draft back from the server several seconds after the local
    # delete — long after the delete has reported success. So the draft is
    # watched over a window and deleted again each time it reappears. That is
    # what makes this call take about twenty seconds; leaving a draft that can
    # be sent a second time would be the worse trade.
    removal_error: str | None = None
    attempts = 0
    present = True
    time.sleep(8)
    for pause in (5, 8):
        if present:
            try:
                _update_message(message_id, "delete")
                attempts += 1
            except MailError as error:
                removal_error = error.code
        time.sleep(pause)
        present = _draft_exists(reference)

    result["draft_removed"] = not present
    result["removal_attempts"] = attempts
    if present:
        result["note"] = (
            "The message was sent, but the draft keeps coming back in Drafts"
            + (f" ({removal_error})" if removal_error else " — the account restores it from the server")
            + ". Remove it from Mail so it is not sent twice."
        )
    return result


def reply_to_message(
    message_id: str,
    body: str,
    reply_all: bool = False,
    send: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    reference = MessageReference.decode(message_id)
    mode = "send" if send else "draft"
    if send and not confirm:
        original = get_message(message_id, max_body_chars=400)
        return _confirmation_needed(
            "sending this reply",
            {
                "action": "reply_to_message",
                "replying_to": original["subject"],
                "original_sender": original["sender"],
                "will_go_to": original["sender"]
                + ((", " + original["cc"]) if reply_all and original["cc"] else ""),
                "reply_all": reply_all,
                "body": body,
            },
        )
    raw = run_script(
        "reply",
        [
            reference.account,
            reference.mailbox,
            str(reference.identifier),
            body,
            "1" if reply_all else "0",
            mode,
        ],
        timeout=WRITE_TIMEOUT,
    )
    records = _parse_records(raw)
    row = records[0] if records else []
    return {
        "ok": True,
        "mode": mode,
        "sent": mode == "send",
        "subject": _field(row, 1),
        "recipient_count": _as_int(_field(row, 2)),
        "reply_all": reply_all,
    }


# --------------------------------------------------------------------------
# Organising
# --------------------------------------------------------------------------


def create_mailbox(
    name: str,
    parent: str | None = None,
    account: str | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise MailError("invalid_name", "The mailbox name is empty.")
    raw = run_script("create_mailbox", [account or "", name, parent or ""], timeout=DEFAULT_TIMEOUT)
    records = _parse_records(raw)
    row = records[0] if records else []
    return {
        "ok": True,
        "account": _field(row, 0),
        "path": _field(row, 1),
    }


def move_message(message_id: str, target_mailbox: str, target_account: str | None = None) -> dict[str, Any]:
    reference = MessageReference.decode(message_id)
    raw = run_script(
        "move_message",
        [
            reference.account,
            reference.mailbox,
            str(reference.identifier),
            target_account or reference.account,
            target_mailbox,
        ],
        timeout=DEFAULT_TIMEOUT,
    )
    records = _parse_records(raw)
    row = records[0] if records else []
    new_identifier = _field(row, 2)
    result: dict[str, Any] = {
        "ok": True,
        "moved_to": {"account": _field(row, 0), "mailbox": _field(row, 1)},
    }
    if new_identifier:
        result["message_id"] = MessageReference(
            account=target_account or reference.account,
            mailbox=target_mailbox,
            identifier=_as_int(new_identifier),
        ).encode()
    else:
        result["note"] = (
            "Mail did not return an id for the moved message; the previous message_id is stale. "
            "Search the target mailbox to get a fresh one."
        )
    return result


def _update_message(message_id: str, action: str, flag_index: str = "") -> dict[str, Any]:
    reference = MessageReference.decode(message_id)
    raw = run_script(
        "update_message",
        [reference.account, reference.mailbox, str(reference.identifier), action, flag_index],
        timeout=DEFAULT_TIMEOUT,
    )
    records = _parse_records(raw)
    row = records[0] if records else []
    result: dict[str, Any] = {"ok": True, "action": action, "message_id": message_id}
    if _field(row, 2):
        result["read"] = _as_bool(_field(row, 2))
    if _field(row, 3):
        result["flagged"] = _as_bool(_field(row, 3))
    if _field(row, 4):
        result["flag_index"] = _as_int(_field(row, 4))
    return result


def delete_message(message_id: str) -> dict[str, Any]:
    result = _update_message(message_id, "delete")
    result["note"] = "The message was moved to the account's trash."
    return result


def mark_as_read(message_id: str) -> dict[str, Any]:
    return _update_message(message_id, "read")


def mark_as_unread(message_id: str) -> dict[str, Any]:
    return _update_message(message_id, "unread")


def flag_message(message_id: str, flag_color: str | None = None) -> dict[str, Any]:
    if flag_color is None:
        return _update_message(message_id, "flag")
    color = str(flag_color).strip().lower()
    if color in {"none", "off", "unflag", "false"}:
        return _update_message(message_id, "unflag")
    if color not in FLAG_COLORS:
        raise MailError(
            "invalid_flag_color",
            f"Unknown colour: {flag_color!r}",
            "Use one of: " + ", ".join(sorted(set(FLAG_COLORS))) + ", or 'none' to clear the flag.",
        )
    return _update_message(message_id, "flag", str(FLAG_COLORS[color]))
