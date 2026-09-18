"""Messages built here, then handed to the account's server rather than to Mail.

Mail's composer cannot produce the message that is wanted. Setting "html
content" on an outgoing message makes Mail wrap the body in a share wrapper — a
stray break at the very top, the body inside a cite blockquote — and it drops
any attachment into that body, ahead of the signature. None of it is reachable
from AppleScript, and it survives every ordering of the calls.

So the message is assembled as MIME (mail_message) and given to the server
(mail_imap): a draft is an APPEND into the account's Drafts, a send is an
ordinary SMTP submission. What lands is byte for byte what was built, in the
order that was asked for:

    the message, the account's signature, a blank line, then the attachments

The attachments being part of the message before anything else sees it is also
why they no longer go missing. Mail used to be asked to attach them to a message
it was still composing, and that attachment is asynchronous: saved too early,
the draft kept the placeholder and lost the file.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html as html_module
import os
import re
from datetime import datetime
from typing import Any, Sequence

import config
import mail_imap
import mail_message
import mail_signature
from mail_tools import (
    MailError,
    _as_address_list,
    _check_attachments,
    _field,
    _parse_records,
    run_script,
)


# The chain of message ids a reply has to carry forward, out of the raw headers.
_REFERENCES = re.compile(r"^References:[ \t]*(.*(?:\n[ \t]+.*)*)", re.IGNORECASE | re.MULTILINE)


def accounts() -> list[dict[str, Any]]:
    """Every account Mail knows, with the addresses it may send from."""
    rows = _parse_records(run_script("list_accounts", []))
    return [
        {
            "name": _field(row, 0),
            "id": _field(row, 1),
            "type": _field(row, 2),
            "addresses": [
                address.strip().lower()
                for address in _field(row, 3).split(",")
                if address.strip()
            ],
        }
        for row in rows
    ]


def resolve_account(sender: str | None) -> dict[str, Any]:
    """The account a message with this sender belongs to.

    Without a sender, Mail would have used its default account; the first
    account it lists is that one. The answer names the account either way, so
    the caller is never left guessing whose signature was used.
    """
    known = accounts()
    if not known:
        raise MailError(
            "no_account",
            "Mail has no account configured.",
            "Add one in Mail's settings, or start Mail and try again.",
        )
    if not sender:
        return known[0]

    parsed = _as_address_list(sender)
    wanted = (parsed[0] if parsed else sender).strip().lower()
    for account in known:
        if wanted in account["addresses"]:
            return account
    raise MailError(
        "sender_not_an_account",
        f"No account in Mail can send from {wanted}.",
        "Use one of: "
        + ", ".join(sorted({a for account in known for a in account["addresses"]})),
    )


def build(
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    attachments: Sequence[str] | None = None,
    sender: str | None = None,
    signature: bool = True,
    as_draft: bool = False,
    extra_headers: Sequence[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Assembles the message and everything needed to talk about it.

    Shared by the draft and the send so that the two cannot drift: what is
    reviewed as a draft and what leaves are built by the same code.
    """
    recipients = _as_address_list(to)
    if not recipients:
        raise MailError("no_recipient", "At least one recipient is required.")
    attachment_paths = _check_attachments(attachments)

    account = resolve_account(sender)
    from_address = sender or (account["addresses"][0] if account["addresses"] else "")
    chosen = mail_signature.signature_for_account(account["id"]) if signature else None

    headers = [
        ("Date", email.utils.formatdate(localtime=True)),
        ("Message-Id", email.utils.make_msgid()),
    ]
    if chosen is not None:
        # Mail reuses the signature it recognises here rather than appending a
        # second one when the draft is opened for editing.
        headers.append(("X-Apple-Mail-Signature", chosen.identifier))
    if as_draft:
        headers.append(("X-Uniform-Type-Identifier", "com.apple.mail-draft"))
    headers.extend(extra_headers or ())

    message = mail_message.build_message(
        to=recipients,
        subject=subject,
        body=body,
        cc=_as_address_list(cc),
        bcc=_as_address_list(bcc),
        attachment_paths=attachment_paths,
        sender=from_address or None,
        signature=chosen,
        extra_headers=headers,
    )

    return {
        "message": message,
        "account": account,
        "from": from_address,
        "to": recipients,
        "cc": _as_address_list(cc),
        "bcc": _as_address_list(bcc),
        "attachment_paths": attachment_paths,
        "signature": chosen,
    }


def _recap(prepared: dict[str, Any], subject: str) -> dict[str, Any]:
    signature = prepared["signature"]
    return {
        "subject": subject,
        "from": prepared["from"],
        "to": prepared["to"],
        "cc": prepared["cc"],
        "bcc": prepared["bcc"],
        "attachments": [os.path.basename(path) for path in prepared["attachment_paths"]],
        "account": prepared["account"]["name"],
        "signature": signature.name if signature is not None else None,
    }


def create_draft(
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    attachments: Sequence[str] | None = None,
    sender: str | None = None,
    signature: bool = True,
) -> dict[str, Any]:
    """Files a draft in the account's Drafts, where Mail will show it."""
    prepared = build(
        to, subject, body, cc, bcc, attachments, sender, signature, as_draft=True
    )
    filed = mail_imap.append_draft(
        prepared["account"]["name"], prepared["message"].as_bytes()
    )

    result = _recap(prepared, subject)
    result.update(
        {
            "ok": True,
            "mode": "draft",
            "sent": False,
            "mailbox": filed["folder"],
            "note": "The draft is on the server; Mail shows it at its next check.",
        }
    )
    return result


# "Re:" already there, in the forms a mail client is likely to have written.
_ALREADY_A_REPLY = re.compile(r"^\s*(?:re|ré|rép|aw|antw|sv|vs)\s*(?:\[\d+\])?\s*:", re.IGNORECASE)


def _bracketed(message_id: str) -> str:
    """A message id as a header must carry it, between angle brackets.

    Mail hands its "message id" over without them, and a header holding a bare
    id is not the one the next client will match against, so the thread breaks.
    """
    cleaned = (message_id or "").strip()
    if not cleaned:
        return ""
    return cleaned if cleaned.startswith("<") else f"<{cleaned}>"


def _addresses_of(*fields: str) -> list[str]:
    """The addresses in one or more header values, in order, without repeats.

    Parsed rather than split on commas: a display name may hold one, and
    "Petit, Jean-Luc <j@x.fr>" must not become two recipients.
    """
    seen: set[str] = set()
    found: list[str] = []
    for _, address in email.utils.getaddresses([field for field in fields if field]):
        cleaned = address.strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            found.append(cleaned)
    return found


def _readable_date(iso: str) -> str:
    """The date as it reads in an attribution line, not as a machine stores it.

    The format is a setting because the line is: a French reader expects the
    day first, an American one the month, and neither expects an ISO stamp.
    """
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return moment.strftime(config.get("reply_date_format"))


def _quoted_original(original: dict[str, Any]) -> str:
    """The original message, as a reply quotes it.

    Its own HTML is used when the server can hand the message back, so tables,
    links and emphasis survive being answered. Otherwise the plain text Mail
    reports is used, which is still readable.
    """
    html = ""
    try:
        raw = mail_imap.fetch_message(
            original["account"], original["rfc_message_id"], original["mailbox"]
        )
        message = email.message_from_bytes(raw, policy=email.policy.default)
        part = message.get_body(preferencelist=("html", "plain"))
        if part is not None:
            content = part.get_content()
            html = content if part.get_content_type() == "text/html" else mail_message.to_html(content)
    except Exception:  # noqa: BLE001 - quoting must never stop a reply
        html = ""
    if not html:
        html = mail_message.to_html(original.get("body") or "")

    attribution = config.get("reply_attribution").format(
        date=_readable_date(original.get("date_received") or ""),
        sender=original.get("sender") or "",
    )
    return (
        f"<br><div>{html_module.escape(attribution)}</div>"
        f'<blockquote type="cite">{html}</blockquote>'
    )


def reply(
    message_id: str,
    body: str,
    reply_all: bool = False,
    attachments: Sequence[str] | None = None,
    as_draft: bool = False,
    signature: bool = True,
) -> dict[str, Any]:
    """Answers a message, staying attached to its thread.

    Mail's own "reply" command used to be the only way to get In-Reply-To and
    References right, at the price of a compose window and of Mail rewriting
    the body. Building the message here sets those headers directly, so the
    answer threads and still looks like every other message this server sends.
    """
    import mail_tools

    original = mail_tools.get_message(message_id, max_body_chars=200_000)

    answer_to = _addresses_of(original.get("reply_to") or original.get("sender") or "")
    if not answer_to:
        raise MailError(
            "no_reply_address",
            "The message being answered names no sender to reply to.",
        )

    account = resolve_account(None)
    for candidate in accounts():
        if candidate["name"] == original.get("account"):
            account = candidate
            break
    mine = set(account["addresses"])
    from_address = next(
        (
            address
            for address in _addresses_of(original.get("to", ""), original.get("cc", ""))
            if address.lower() in mine
        ),
        account["addresses"][0] if account["addresses"] else "",
    )

    copies: list[str] = []
    if reply_all:
        answered = {address.lower() for address in answer_to}
        copies = [
            address
            for address in _addresses_of(original.get("to", ""), original.get("cc", ""))
            if address.lower() not in mine and address.lower() not in answered
        ]

    subject = original.get("subject") or ""
    if not _ALREADY_A_REPLY.match(subject):
        subject = f"Re: {subject}"

    # References is the whole chain, In-Reply-To just the message answered.
    # Together they are what every client threads on.
    headers: list[tuple[str, str]] = []
    parent = original.get("rfc_message_id") or ""
    if parent:
        chain = _REFERENCES.search(original.get("headers") or "")
        existing = " ".join((chain.group(1) if chain else "").split())
        headers.append(("In-Reply-To", _bracketed(parent)))
        headers.append(("References", f"{existing} {_bracketed(parent)}".strip()))

    prepared = build(
        to=answer_to,
        subject=subject,
        # Converted before the quote is appended: concatenated the other way
        # round, the HTML of the quote would make the whole thing look like
        # HTML already and the plain body would go out with its newlines lost.
        body=mail_message.to_html(body) + _quoted_original(original),
        cc=copies,
        attachments=attachments,
        sender=from_address or None,
        signature=signature,
        as_draft=as_draft,
        extra_headers=headers,
    )

    result = _recap(prepared, subject)
    result["threaded"] = bool(parent)
    result["replying_to"] = original.get("subject") or ""
    if as_draft:
        filed = mail_imap.append_draft(
            prepared["account"]["name"], prepared["message"].as_bytes()
        )
        result.update({"ok": True, "mode": "draft", "sent": False, "mailbox": filed["folder"]})
        return result

    message = prepared["message"]
    del message["Bcc"]
    delivered = mail_imap.send_message(
        prepared["account"]["name"], message.as_bytes(), prepared["to"] + prepared["cc"]
    )
    result.update({"ok": True, "mode": "send", "sent": True, "server": delivered["server"]})
    return result


def send(
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    attachments: Sequence[str] | None = None,
    sender: str | None = None,
    signature: bool = True,
) -> dict[str, Any]:
    """Sends the message through the account's own outgoing server."""
    prepared = build(to, subject, body, cc, bcc, attachments, sender, signature)
    envelope = prepared["to"] + prepared["cc"] + prepared["bcc"]

    message = prepared["message"]
    # Bcc names people the other recipients must not learn about; the envelope
    # carries them, the message itself must not.
    del message["Bcc"]

    delivered = mail_imap.send_message(
        prepared["account"]["name"], message.as_bytes(), envelope
    )

    result = _recap(prepared, subject)
    result.update({"ok": True, "mode": "send", "sent": True, "server": delivered["server"]})
    return result
