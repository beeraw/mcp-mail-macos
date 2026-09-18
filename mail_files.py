"""Drafts written as .eml files, outside Mail.

A file is a better record than a draft. It sits still, it can be opened (macOS
renders an .eml in Mail), diffed, versioned. And nothing has to go into Mail and
come back out: the file is written here, reviewed, and submitted as it stands.

The message is built by mail_draft, the same as a draft filed in Mail and the
same as a direct send, so the three cannot render differently. Sending is a
straight SMTP submission of the file's own bytes — no unpacking, no rebuilding,
so what leaves is what was reviewed, down to the byte.
"""

from __future__ import annotations

import email
import email.policy
import os
import re
import time
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from typing import Any, Sequence

import config
from mail_tools import MailError, _as_address_list, _confirmation_needed

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

    return {
        "ok": True,
        "folder": target_folder,
        "removed": removed,
        "kept_pending_days": pending_days,
        "kept_archive_days": archive_days,
    }


def _sweep(folder: str | None) -> list[str]:
    """Best-effort purge; a failure here must never break the caller."""
    try:
        return purge_drafts(folder)["removed"]
    except Exception:  # noqa: BLE001
        return []


def _recap(message: EmailMessage, path: str) -> dict[str, Any]:
    """The summary shown to the user: everything that decides whether to send."""
    attachments: list[dict[str, Any]] = []
    inline: list[str] = []
    body = ""
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_filename():
            # An inline part is not an attachment: the signature logo travels
            # that way, and announcing it would have the reader expect a file
            # the recipient never receives.
            disposition = (part.get("Content-Disposition") or "").lower()
            if disposition.startswith("inline"):
                inline.append(part.get_filename())
                continue
            attachments.append(
                {
                    "name": part.get_filename(),
                    "size": len(part.get_payload(decode=True) or b""),
                }
            )
        elif part.get_content_type() == "text/plain" and not body:
            # Decoded by hand rather than with get_content(): a message just
            # built here is an ordinary Message, which has no such method, and
            # only one read back from a file is an EmailMessage.
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="replace")
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
        "kept_inline": inline,
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
    import mail_draft

    target_folder = _resolve_folder(folder)
    # The same builder as the Mail draft and the direct send, so the three
    # cannot drift: HTML body, the account's signature, then the attachments.
    prepared = mail_draft.build(
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        sender=sender,
    )
    message = prepared["message"]
    recipients = prepared["to"]
    attachment_paths = prepared["attachment_paths"]

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

    # The file is submitted as it stands. Nothing is unpacked and recomposed,
    # so what leaves is what was reviewed, down to the byte — no attachment can
    # be lost on the way, and the formatting cannot be rebuilt differently.
    import mail_draft
    import mail_imap

    account = mail_draft.resolve_account(recap["from"] or None)
    envelope = recap["to"] + recap["cc"] + recap["bcc"]
    # The envelope carries the blind recipients; the message must not name them.
    del message["Bcc"]
    delivered = mail_imap.send_message(account["name"], message.as_bytes(), envelope)

    result = dict(recap)
    result["ok"] = True
    result["sent"] = True
    result["account"] = delivered["account"]

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
