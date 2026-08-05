"""Drafts written as .eml files, outside Mail.

Mail has no way to send a draft it holds, so anything drafted inside it has to
be re-posted and then deleted — a delete that the account can undo, and that
costs a twenty second wait to make stick. Keeping drafts out of Mail removes
that whole problem: the file is written here, reviewed, and one single message
is sent from it.

The file is also a better record than a draft. It sits still, it can be opened
(macOS renders an .eml in Mail), diffed, versioned. What gets sent is read back
from it at send time, so the message that leaves is the one that was reviewed.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import json
import mimetypes
import os
import re
import tempfile
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Sequence

import config
from mail_tools import MailError, _as_address_list, _check_attachments, _compose, _confirmation_needed

# Drafts live inside the project by default, not in the user's home or working
# directory. See config.py to put them elsewhere.
DEFAULT_FOLDER = config.get("drafts_folder")
SENT_SUBFOLDER = "sent"

# A draft that was never sent is a file holding a message: it must not sit on
# disk indefinitely because someone changed their mind and moved on.
PENDING_RETENTION_DAYS = config.get("pending_retention_days")
ARCHIVE_RETENTION_DAYS = config.get("archive_retention_days")


def _resolve_folder(folder: str | None, create: bool = True) -> str:
    path = os.path.abspath(os.path.expanduser(folder or DEFAULT_FOLDER))
    if create:
        os.makedirs(path, exist_ok=True)
    elif not os.path.isdir(path):
        raise MailError(
            "folder_not_found",
            f"No such folder: {path}",
            "Pass the folder where the drafts were written, or write one first.",
        )
    return path


def _slug(text: str, limit: int = 48) -> str:
    """A file name fragment: readable, but safe on any filesystem."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text[:limit].rstrip("-")) or "no-subject"


def purge_drafts(
    folder: str | None = None,
    pending_days: int = PENDING_RETENTION_DAYS,
    archive_days: int = ARCHIVE_RETENTION_DAYS,
) -> dict[str, Any]:
    """Removes drafts left behind, so none linger on disk.

    Runs on its own whenever a draft is written or listed, and from sync_index,
    so a forgotten draft is cleared even if nobody thinks to ask.
    """
    target_folder = _resolve_folder(folder, create=False)
    now = time.time()
    removed: list[str] = []

    for directory, days in (
        (target_folder, pending_days),
        (os.path.join(target_folder, SENT_SUBFOLDER), archive_days),
    ):
        if not os.path.isdir(directory):
            continue
        cutoff = now - days * 86400
        for name in os.listdir(directory):
            if not name.endswith(".eml"):
                continue
            path = os.path.join(directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed.append(os.path.relpath(path, target_folder))
            except OSError:
                continue

    # The same sweep also clears the drafts Mail left behind after a send.
    mail_drafts: list[str] = []
    try:
        mail_drafts = purge_mail_drafts(folder)
    except Exception:  # noqa: BLE001 - never let this break the file purge
        pass

    result = {
        "ok": True,
        "folder": target_folder,
        "removed": removed,
        "kept_pending_days": pending_days,
        "kept_archive_days": archive_days,
    }
    if mail_drafts:
        result["mail_drafts_removed"] = mail_drafts
    return result


# Mail autosaves what it is composing, and with no window to close that autosave
# is sometimes left behind as a draft — appearing several seconds after the
# message has already gone, which is why it cannot be cleaned up inline. It is
# swept later instead, and only when it matches a message this server sent.
LEDGER_NAME = ".sent-ledger.json"
LEDGER_WINDOW_HOURS = config.get("ledger_window_hours")


def _ledger_path(folder: str | None) -> str:
    return os.path.join(_resolve_folder(folder), LEDGER_NAME)


def _read_ledger(folder: str | None) -> list[dict[str, Any]]:
    try:
        with open(_ledger_path(folder), "r", encoding="utf-8") as handle:
            entries = json.load(handle)
        return entries if isinstance(entries, list) else []
    except (OSError, ValueError):
        return []


def record_sent(subject: str, recipients: Sequence[str], folder: str | None = None) -> None:
    """Notes a message this server sent, so its autosave can be recognised later.

    Nothing is written into the message itself: a marker in the body would be
    visible to the recipient, and AppleScript cannot set a custom header. The
    note stays here, next to the drafts.
    """
    now = time.time()
    entries = [
        entry
        for entry in _read_ledger(folder)
        if now - float(entry.get("at", 0)) < LEDGER_WINDOW_HOURS * 3600
    ]
    entries.append(
        {
            "subject": subject,
            "to": [address.lower() for address in recipients],
            "at": now,
        }
    )
    try:
        with open(_ledger_path(folder), "w", encoding="utf-8") as handle:
            json.dump(entries, handle, ensure_ascii=False)
    except OSError:
        pass


def _addresses(text: str) -> set[str]:
    return {match.lower() for match in re.findall(r"[\w.+-]+@[\w.-]+", text or "")}


def purge_mail_drafts(folder: str | None = None) -> list[str]:
    """Removes drafts Mail left behind after this server sent a message.

    A draft is only removed when it matches a recent send on both subject and
    recipient. A draft written by hand matches nothing, so it is never touched.
    """
    entries = _read_ledger(folder)
    if not entries:
        return []

    import mail_tools

    now = time.time()
    recent = [
        entry for entry in entries if now - float(entry.get("at", 0)) < LEDGER_WINDOW_HOURS * 3600
    ]
    if not recent:
        return []

    try:
        drafts = mail_tools.list_messages(mailbox="drafts", limit=25)["messages"]
    except Exception:  # noqa: BLE001 - Mail may be busy or unreachable
        return []

    removed: list[str] = []
    for draft in drafts:
        candidates = [entry for entry in recent if entry["subject"] == draft["subject"]]
        if not candidates:
            continue
        try:
            full = mail_tools.get_message(draft["message_id"], max_body_chars=200)
        except Exception:  # noqa: BLE001
            continue
        draft_addresses = _addresses(full.get("to", "")) | _addresses(full.get("cc", ""))
        if not any(set(entry["to"]) & draft_addresses for entry in candidates):
            continue
        try:
            mail_tools.delete_message(draft["message_id"])
            removed.append(draft["subject"])
        except Exception:  # noqa: BLE001
            continue
    return removed


def _sweep(folder: str | None) -> list[str]:
    """Best-effort purge; a failure here must never break the caller."""
    try:
        return purge_drafts(folder)["removed"]
    except Exception:  # noqa: BLE001
        return []


def _recap(message: EmailMessage, path: str) -> dict[str, Any]:
    """The summary shown to the user: everything that decides whether to send."""
    attachments = []
    body = ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            attachments.append(
                {
                    "name": part.get_filename(),
                    "size": len(part.get_payload(decode=True) or b""),
                }
            )
        elif part.get_content_type() == "text/plain" and not body:
            body = part.get_content()
    return {
        "path": path,
        "file": os.path.basename(path),
        "from": message.get("From") or "",
        "to": _as_address_list(message.get("To") or ""),
        "cc": _as_address_list(message.get("Cc") or ""),
        "bcc": _as_address_list(message.get("Bcc") or ""),
        "subject": message.get("Subject") or "",
        "body": body.rstrip(),
        "attachments": attachments,
        "written_at": message.get("Date") or "",
    }


def _load(path: str) -> tuple[EmailMessage, str]:
    full = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(full):
        raise MailError(
            "draft_file_not_found",
            f"No such draft: {path}",
            "Call list_drafts to see what is waiting.",
        )
    try:
        with open(full, "rb") as handle:
            message = email.message_from_binary_file(handle, policy=email.policy.default)
    except Exception as error:  # noqa: BLE001 - a broken file is a clear answer
        raise MailError("draft_file_unreadable", f"Unreadable draft: {error}") from error
    return message, full


def write_draft(
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    attachments: Sequence[str] | None = None,
    sender: str | None = None,
    folder: str | None = None,
) -> dict[str, Any]:
    """Writes a draft as a self-contained .eml file. Mail is not involved."""
    recipients = _as_address_list(to)
    if not recipients:
        raise MailError("no_recipient", "At least one recipient is required.")
    attachment_paths = _check_attachments(attachments)
    target_folder = _resolve_folder(folder)

    message = EmailMessage()
    message["From"] = sender or ""
    message["To"] = ", ".join(recipients)
    if _as_address_list(cc):
        message["Cc"] = ", ".join(_as_address_list(cc))
    if _as_address_list(bcc):
        message["Bcc"] = ", ".join(_as_address_list(bcc))
    message["Subject"] = subject
    message["Date"] = email.utils.formatdate(localtime=True)
    message.set_content(body)

    for path in attachment_paths:
        guessed, _ = mimetypes.guess_type(path)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        with open(path, "rb") as handle:
            message.add_attachment(
                handle.read(),
                maintype=maintype,
                subtype=subtype,
                filename=os.path.basename(path),
            )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    full = os.path.join(target_folder, f"{stamp}-{_slug(subject)}.eml")
    # Two drafts written within the same second must not overwrite each other.
    stem, extension = os.path.splitext(full)
    counter = 1
    while os.path.exists(full):
        full = f"{stem}-{counter}{extension}"
        counter += 1
    with open(full, "wb") as handle:
        handle.write(message.as_bytes())

    result = _recap(message, full)
    result["ok"] = True
    result["note"] = "Nothing is in Mail: this file is the draft. Send it with send_draft_file."
    swept = _sweep(folder)
    if swept:
        result["purged"] = swept
    return result


def list_drafts(folder: str | None = None) -> dict[str, Any]:
    """Lists the drafts still waiting to be sent, newest first."""
    target_folder = _resolve_folder(folder, create=False)
    swept = _sweep(folder)
    drafts = []
    for name in sorted(os.listdir(target_folder), reverse=True):
        if not name.endswith(".eml"):
            continue
        try:
            message, full = _load(os.path.join(target_folder, name))
        except MailError:
            continue
        drafts.append(_recap(message, full))
    result = {
        "ok": True,
        "folder": target_folder,
        "waiting": len(drafts),
        "drafts": drafts,
        "retention": f"pending drafts are removed after {PENDING_RETENTION_DAYS} days, "
                     f"sent ones after {ARCHIVE_RETENTION_DAYS}",
    }
    if swept:
        result["purged"] = swept
    return result


def read_draft_file(path: str) -> dict[str, Any]:
    """Reads one draft in full, to put it in front of the user."""
    message, full = _load(path)
    result = _recap(message, full)
    result["ok"] = True
    return result


def send_draft_file(path: str, confirm: bool = False, keep_file: bool = False) -> dict[str, Any]:
    """Sends a draft file, then files it away under sent/.

    The message is built from the file, so what leaves is what was reviewed. No
    draft ever exists in Mail, so there is nothing to clean up afterwards.
    """
    message, full = _load(path)
    recap = _recap(message, full)

    if not recap["to"] and not recap["cc"] and not recap["bcc"]:
        raise MailError(
            "draft_without_recipient",
            f"The draft {recap['subject']!r} has no recipient.",
            "Rewrite it with write_draft, or edit the file.",
        )

    if not confirm:
        preview = dict(recap)
        preview["action"] = "send_draft_file"
        return _confirmation_needed("sending this draft", preview)

    with tempfile.TemporaryDirectory(prefix="mcp-mail-eml-") as workspace:
        attachment_paths = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart" or not part.get_filename():
                continue
            safe = os.path.basename(part.get_filename())
            safe = re.sub(r"[/\\\x00]", "_", safe) or "piece"
            target = os.path.join(workspace, safe)
            with open(target, "wb") as handle:
                handle.write(part.get_payload(decode=True) or b"")
            attachment_paths.append(target)

        if len(attachment_paths) != len(recap["attachments"]):
            raise MailError(
                "attachments_incomplete",
                f"Only {len(attachment_paths)} of {len(recap['attachments'])} attachments could "
                "be read back from the file; nothing was sent.",
            )

        _compose(
            "send",
            to=recap["to"],
            subject=recap["subject"],
            body=recap["body"],
            cc=recap["cc"],
            bcc=recap["bcc"],
            attachments=attachment_paths,
            sender=recap["from"] or None,
        )

    result = dict(recap)
    result["ok"] = True
    result["sent"] = True

    if keep_file:
        result["filed_as"] = full
        return result

    archive = os.path.join(os.path.dirname(full), SENT_SUBFOLDER)
    os.makedirs(archive, exist_ok=True)
    destination = os.path.join(archive, os.path.basename(full))
    stem, extension = os.path.splitext(destination)
    counter = 1
    while os.path.exists(destination):
        destination = f"{stem}-{counter}{extension}"
        counter += 1
    os.replace(full, destination)
    result["filed_as"] = destination
    return result


def discard_draft_file(path: str) -> dict[str, Any]:
    """Deletes a draft file that will not be sent."""
    _, full = _load(path)
    os.remove(full)
    return {"ok": True, "discarded": full}
