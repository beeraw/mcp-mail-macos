"""Changing a draft that already exists, without composing it again.

A draft is edited where it stands: the headers that were asked about are
rewritten, the text is changed inside the HTML it already has, files are added
or taken out — and everything else stays byte for byte what it was. Rebuilding
the message from its parts is how a signature logo, a quoted original, a thread
or a hand-made layout get lost, the same reason send_draft submits the draft as
it is rather than recomposing it.

Two kinds of draft are covered, both through the same edit:

- a draft in Mail lives on the account's server, where IMAP offers no way to
  change a message. The edited version is filed first and the old one removed
  only after, so a failure in between leaves two drafts, never none;
- an .eml draft (mail_files) is rewritten in place.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html as html_module
import mimetypes
import os
import re
import tempfile
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Any, Sequence

import mail_message
from mail_tools import MailError, MessageReference, _check_attachments

ADDRESS_FIELDS = ("to", "cc", "bcc")
_HEADER = {"to": "To", "cc": "Cc", "bcc": "Bcc"}

_PLAIN_ADDRESS = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")

# Where the text the user wrote ends, in the drafts this server builds and in
# the ones Mail composes itself: the signature, or the quoted original with the
# attribution line that introduces it. The earliest one found wins.
_SIGNATURE_START = re.compile(
    r'(?:<br\s*/?>\s*)?<div[^>]*\bid=["\']?AppleMailSignature'
    r'|<br[^>]*\bid=["\']?lineBreakAtBeginningOfSignature',
    re.IGNORECASE,
)
_QUOTE_START = re.compile(
    r"(?:<br\s*/?>\s*<div>[^<]*</div>\s*|<div[^>]*>\s*(?:<br\s*/?>\s*)?)?"
    r"<blockquote[^>]*\btype=[\"']?cite",
    re.IGNORECASE,
)
_BODY_OPEN = re.compile(r"<body[^>]*>", re.IGNORECASE)
# The closing tag, with the blank line compose_html leaves before it.
_BODY_CLOSE = re.compile(r"(?:<br\s*/?>\s*)*</body\s*>", re.IGNORECASE)
_PLAIN_SIGNATURE = re.compile(r"^-- ?$", re.MULTILINE)


# --------------------------------------------------------------------------
# Recipients
# --------------------------------------------------------------------------


def _requested(value: str | Sequence[str] | None, parameter: str) -> list[Address]:
    """Addresses the caller wrote, checked one by one, display names kept."""
    if value is None:
        return []
    items = [value.replace(";", ",")] if isinstance(value, str) else [str(item) for item in value]
    found: list[Address] = []
    for item in items:
        if not item.strip():
            continue
        pairs = email.utils.getaddresses([item])
        if not any(address for _, address in pairs):
            pairs = email.utils.getaddresses([item], strict=False)
        if not pairs or not all(_PLAIN_ADDRESS.match(address.strip()) for _, address in pairs):
            raise MailError(
                "invalid_address",
                f"{parameter} holds something that is not an address: {item!r}.",
                "Give plain addresses such as name@example.com.",
            )
        found.extend(Address(display_name=name.strip(), addr_spec=address.strip()) for name, address in pairs)
    return found


def _current(message: EmailMessage, field: str) -> list[Address]:
    header = message[_HEADER[field]]
    if header is None:
        return []
    try:
        return [address for address in header.addresses if address.addr_spec]
    except AttributeError:  # an unparsable header reads as plain text
        return [
            Address(display_name=name, addr_spec=address)
            for name, address in email.utils.getaddresses([str(header)], strict=False)
            if address
        ]


def _key(address: Address) -> str:
    return address.addr_spec.lower()


def _without_repeats(addresses: list[Address]) -> list[Address]:
    seen: set[str] = set()
    kept: list[Address] = []
    for address in addresses:
        if _key(address) not in seen:
            seen.add(_key(address))
            kept.append(address)
    return kept


def _edit_recipients(
    message: EmailMessage,
    replace: dict[str, str | Sequence[str] | None],
    add: dict[str, str | Sequence[str] | None],
    remove: str | Sequence[str] | None,
) -> list[str]:
    """Rewrites To, Cc and Bcc as asked. Returns the fields that changed.

    A field given whole replaces what was there; an address added joins it.
    Either way, an address placed in a field by this call leaves the other two:
    asking for someone in Cc who was in To moves them, it does not copy them.
    """
    before = {field: _current(message, field) for field in ADDRESS_FIELDS}
    after = {field: list(addresses) for field, addresses in before.items()}
    placed: dict[str, list[Address]] = {field: [] for field in ADDRESS_FIELDS}

    for field in ADDRESS_FIELDS:
        if replace.get(field) is not None:
            after[field] = _requested(replace[field], field)
            placed[field].extend(after[field])
    for field in ADDRESS_FIELDS:
        extra = _requested(add.get(field), f"add_{field}")
        after[field].extend(extra)
        placed[field].extend(extra)

    for field, addresses in placed.items():
        moved = {_key(address) for address in addresses}
        for other in ADDRESS_FIELDS:
            if other != field:
                after[other] = [a for a in after[other] if _key(a) not in moved]

    unwanted = _requested(remove, "remove")
    if unwanted:
        present = {_key(a) for field in ADDRESS_FIELDS for a in after[field]}
        missing = [a.addr_spec for a in unwanted if _key(a) not in present]
        if missing:
            raise MailError(
                "not_a_recipient",
                "Not among the draft's recipients: " + ", ".join(missing) + ".",
                "Check the address; nothing was changed.",
            )
        dropped = {_key(a) for a in unwanted}
        for field in ADDRESS_FIELDS:
            after[field] = [a for a in after[field] if _key(a) not in dropped]

    after = {field: _without_repeats(addresses) for field, addresses in after.items()}
    if not any(after.values()):
        raise MailError(
            "no_recipient",
            "The draft would be left without any recipient.",
            "Keep or add at least one address; nothing was changed.",
        )

    changed: list[str] = []
    for field in ADDRESS_FIELDS:
        if [str(a) for a in after[field]] == [str(a) for a in before[field]]:
            continue
        changed.append(field)
        del message[_HEADER[field]]
        if after[field]:
            message[_HEADER[field]] = after[field]
    return changed


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------


def _outside_tags(html: str, start: int) -> bool:
    return html.rfind("<", 0, start) <= html.rfind(">", 0, start)


def _spellings(old: str, in_html: bool) -> list[str]:
    """How a piece of text may be written in the part being searched.

    The HTML this server writes escapes quotes ("l'offre" is stored as
    "l&#x27;offre") and turns line breaks into <br>; Mail writes quotes as they
    are. Every likely spelling is tried, the plain one first.
    """
    if not in_html:
        return [old]
    forms = [old, html_module.escape(old, quote=False), html_module.escape(old)]
    forms += [form.replace("\n", "<br>") for form in forms if "\n" in form]
    unique: list[str] = []
    for form in forms:
        if form not in unique:
            unique.append(form)
    return unique


def _replace_once(text: str, old: str, new: str, in_html: bool) -> str:
    if not old:
        raise MailError("empty_replacement", "A replacement has nothing to look for.")
    for form in _spellings(old, in_html):
        hits = [
            match.start()
            for match in re.finditer(re.escape(form), text)
            if not in_html or _outside_tags(text, match.start())
        ]
        if not hits:
            continue
        if len(hits) > 1:
            raise MailError(
                "ambiguous_replacement",
                f"{old!r} appears {len(hits)} times in the draft.",
                "Quote a longer passage so that it appears only once; nothing was changed.",
            )
        replacement = html_module.escape(new, quote=False).replace("\n", "<br>") if in_html else new
        return text[: hits[0]] + replacement + text[hits[0] + len(form):]
    raise MailError(
        "text_not_found",
        f"{old!r} is not in the draft.",
        "Quote the text exactly as the draft has it; nothing was changed.",
    )


def _user_text_span(html: str) -> tuple[int, int]:
    """Where the text the user wrote sits, between the opening and what follows.

    What follows is the signature, the quoted original, or the end of the body,
    whichever comes first. Only that span is replaced by a new body.
    """
    opened = _BODY_OPEN.search(html)
    start = opened.end() if opened else 0
    ends = [
        match.start()
        for pattern in (_SIGNATURE_START, _QUOTE_START)
        for match in [pattern.search(html, start)]
        if match
    ]
    if not ends:
        closed = _BODY_CLOSE.search(html, start)
        ends = [closed.start() if closed else len(html)]
    return start, min(ends)


def _new_html_body(html: str, body: str) -> str:
    start, end = _user_text_span(html)
    # The same cleaning compose_html applies, so one empty line still separates
    # the text from what follows it.
    text = mail_message._without_trailing_blank(mail_message.to_html(body))
    return html[:start] + text + html[end:]


def _new_plain_body(text: str, body: str) -> str:
    text = text.replace("\r\n", "\n")
    signature = _PLAIN_SIGNATURE.search(text)
    rest = text[signature.start():] if signature else ""
    return body.rstrip() + ("\n\n" + rest if rest else "\n")


def _body_parts(message: EmailMessage) -> tuple[EmailMessage | None, EmailMessage | None]:
    """The HTML part read by the recipient, and its plain text alternative."""
    html_part = plain_part = None
    for part in message.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        if part.get_content_type() == "text/html" and html_part is None:
            html_part = part
        elif part.get_content_type() == "text/plain" and plain_part is None:
            plain_part = part
    return html_part, plain_part


def _set_text(part: EmailMessage, text: str, subtype: str) -> None:
    # Quoted-printable rather than the policy's 8bit: an accented body has to
    # survive any SMTP server the draft may later be sent through.
    part.set_content(text, subtype=subtype, charset="utf-8", cte="quoted-printable")


def _edit_body(
    message: EmailMessage,
    body: str | None,
    replacements: Sequence[dict[str, str]] | None,
) -> bool:
    if body is None and not replacements:
        return False
    html_part, plain_part = _body_parts(message)
    if html_part is None and plain_part is None:
        raise MailError("draft_without_text", "The draft has no text part to edit.")

    pairs: list[tuple[str, str]] = []
    for item in replacements or []:
        if not isinstance(item, dict) or "old" not in item or "new" not in item:
            raise MailError(
                "invalid_replacement",
                "Each replacement needs an 'old' and a 'new' text.",
            )
        pairs.append((str(item["old"]), str(item["new"])))

    if html_part is not None:
        html = html_part.get_content()
        if body is not None:
            html = _new_html_body(html, body)
        for old, new in pairs:
            html = _replace_once(html, old, new, in_html=True)
        _set_text(html_part, html, "html")
        if plain_part is not None:
            # The alternative only ever mirrors the HTML: derived again, it
            # cannot say something the HTML no longer does.
            _set_text(plain_part, mail_message.to_text(html), "plain")
    else:
        text = plain_part.get_content()
        if body is not None:
            text = _new_plain_body(text, body)
        for old, new in pairs:
            text = _replace_once(text, old, new, in_html=False)
        _set_text(plain_part, text, "plain")
    return True


# --------------------------------------------------------------------------
# Attachments
# --------------------------------------------------------------------------


def _is_attachment(part: EmailMessage) -> bool:
    return bool(part.get_filename()) and (part.get_content_disposition() or "") != "inline"


def _remove_attachments(message: EmailMessage, names: Sequence[str]) -> None:
    wanted = {name for name in names if name}
    found: set[str] = set()
    for container in list(message.walk()):
        if not container.is_multipart():
            continue
        kept = []
        for part in container.get_payload():
            if _is_attachment(part) and part.get_filename() in wanted:
                found.add(part.get_filename())
            else:
                kept.append(part)
        container.set_payload(kept)
    missing = sorted(wanted - found)
    if missing:
        present = [p.get_filename() for p in message.walk() if _is_attachment(p)]
        raise MailError(
            "attachment_not_in_draft",
            "Not attached to the draft: " + ", ".join(missing) + ".",
            "Attached: " + (", ".join(present) if present else "nothing") + ". Nothing was changed.",
        )


def _add_attachments(message: EmailMessage, paths: Sequence[str]) -> list[str]:
    added = []
    for path in _check_attachments(paths):
        guessed, _ = mimetypes.guess_type(path)
        maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
        with open(path, "rb") as handle:
            payload = handle.read()
        # Added after the body, the way the server lays out every message:
        # the text, the signature, then the files.
        message.add_attachment(
            payload, maintype=maintype, subtype=subtype or "octet-stream", filename=os.path.basename(path)
        )
        added.append(os.path.basename(path))
    return added


# --------------------------------------------------------------------------
# The edit itself
# --------------------------------------------------------------------------


def edit_message(
    raw: bytes,
    subject: str | None = None,
    to: str | Sequence[str] | None = None,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    add_to: str | Sequence[str] | None = None,
    add_cc: str | Sequence[str] | None = None,
    add_bcc: str | Sequence[str] | None = None,
    remove: str | Sequence[str] | None = None,
    body: str | None = None,
    replacements: Sequence[dict[str, str]] | None = None,
    add_attachments: Sequence[str] | None = None,
    remove_attachments: Sequence[str] | None = None,
) -> tuple[EmailMessage, list[str]]:
    """Applies the changes to a message, and names what changed.

    Nothing talks to Mail or to a server here. Every check runs before the
    result is handed back, so a refused change leaves the caller with nothing
    to write — the draft stays as it was.
    """
    message = email.message_from_bytes(raw, policy=email.policy.default)
    changed: list[str] = []

    if subject is not None and subject != (message["Subject"] or ""):
        del message["Subject"]
        message["Subject"] = subject
        changed.append("subject")

    changed += _edit_recipients(
        message,
        replace={"to": to, "cc": cc, "bcc": bcc},
        add={"to": add_to, "cc": add_cc, "bcc": add_bcc},
        remove=remove,
    )

    if _edit_body(message, body, replacements):
        changed.append("body")

    if remove_attachments:
        _remove_attachments(message, remove_attachments)
        changed.append("attachments")
    if add_attachments:
        _add_attachments(message, add_attachments)
        if "attachments" not in changed:
            changed.append("attachments")

    if not changed:
        raise MailError(
            "nothing_to_change",
            "No change was asked for, or the draft already reads that way.",
        )
    del message["Date"]
    message["Date"] = email.utils.formatdate(localtime=True)
    return message, changed


def summary(message: EmailMessage) -> dict[str, Any]:
    """What the edited draft now says, for the caller to check."""
    attachments, inline = mail_message.classify_parts(message.as_bytes())
    html_part, plain_part = _body_parts(message)
    if html_part is not None:
        text = mail_message.to_text(html_part.get_content())
    elif plain_part is not None:
        text = plain_part.get_content().strip()
    else:
        text = ""
    result: dict[str, Any] = {
        "subject": message["Subject"] or "",
        "from": message["From"] or "",
        "to": [str(a) for a in _current(message, "to")],
        "cc": [str(a) for a in _current(message, "cc")],
        "bcc": [str(a) for a in _current(message, "bcc")],
        "attachments": attachments,
        "body": text[:4000],
    }
    if inline:
        result["kept_inline"] = inline
    if not result["to"]:
        # Allowed, since a copy alone does reach its reader, but rarely meant:
        # it is what moving the only To recipient into Cc leaves behind.
        result["warning"] = "The draft has no recipient in To; its recipients are only in Cc or Bcc."
    return result


_EDIT_ARGUMENTS = (
    "subject", "to", "cc", "bcc", "add_to", "add_cc", "add_bcc", "remove",
    "body", "replacements", "add_attachments", "remove_attachments",
)


def edit_draft(message_id: str, **changes: Any) -> dict[str, Any]:
    """Edits a draft in Mail: the new version is filed, then the old one removed."""
    import mail_imap
    import mail_tools

    reference = MessageReference.decode(message_id)
    # read_draft refuses anything outside a Drafts mailbox, so a received
    # message can never be rewritten through here.
    records = mail_tools._parse_records(
        mail_tools.run_script(
            "read_draft",
            [reference.account, reference.mailbox, str(reference.identifier)],
            timeout=mail_tools.DEFAULT_TIMEOUT,
        )
    )
    if not records:
        raise MailError("draft_unreadable", "Mail returned nothing for this draft.")
    row = records[0]
    account_name = mail_tools._field(row, 7)
    old_id = mail_tools._field(row, 9)

    raw = mail_imap.fetch_draft(account_name, old_id)
    message, changed = edit_message(raw, **{k: changes.get(k) for k in _EDIT_ARGUMENTS})

    # A new id: the server and Mail must see a new message, not two versions
    # of one, or Gmail merges them and the removal below takes both.
    sender = email.utils.parseaddr(message["From"] or "")[1]
    del message["Message-ID"]
    message["Message-ID"] = email.utils.make_msgid(domain=sender.rpartition("@")[2] or None)
    new_id = message["Message-ID"]

    filed = mail_imap.append_draft(account_name, message.as_bytes())

    result = {"ok": True, "changed": changed, **summary(message)}
    result["mailbox"] = filed["folder"]
    result["rfc_message_id"] = new_id
    removed = False
    try:
        removed = mail_imap.delete_draft(account_name, old_id)
    except MailError:
        removed = False
    result["previous_removed"] = removed
    result["note"] = (
        "The draft was replaced by an edited copy, so its message_id has changed: "
        "Mail shows the new one at its next check, and list_messages(mailbox=\"drafts\") gives its id."
    )
    if not removed:
        result["note"] += " The previous version could not be removed and is still in Drafts: delete it in Mail."
    return result


def edit_draft_file(path: str, **changes: Any) -> dict[str, Any]:
    """Edits an .eml draft in place."""
    import mail_files

    _, full = mail_files._load(path)
    with open(full, "rb") as handle:
        raw = handle.read()
    message, changed = edit_message(raw, **{k: changes.get(k) for k in _EDIT_ARGUMENTS})

    # Written next to the file and swapped in, so an interrupted write never
    # leaves half a draft behind.
    handle, temporary = tempfile.mkstemp(dir=os.path.dirname(full), suffix=".eml.tmp")
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(message.as_bytes())
        os.replace(temporary, full)
    except BaseException:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise

    result = mail_files._recap(message, full)
    result.update({"ok": True, "changed": changed})
    return result
