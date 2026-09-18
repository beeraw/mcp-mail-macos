"""Building the message itself, once, for every path that needs one.

Mail's own composer cannot be asked for the message this server wants. Setting
"html content" on an outgoing message makes Mail wrap the body in a share
wrapper — a stray <br> at the very top and a cite blockquote around everything —
and it drops any attachment inside that body, ahead of the signature. Neither is
reachable from AppleScript, and both survive every ordering of the calls.

So the message is assembled here instead, as plain MIME, in the one order that
was asked for:

    the message, the account's signature, a blank line, then the attachments

The layout below is the ordinary one for a mail carrying both an inline image
and a file, and every client reads it:

    multipart/mixed
      multipart/related          the body, with the signature's images
        multipart/alternative
          text/plain             for a reader that shows no HTML
          text/html              what is actually read
        image/...                the signature logo, by content id
      application/...            the attachments, after everything above
"""

from __future__ import annotations

import email
import email.policy
import html as html_module
import mimetypes
import os
import re
from email.encoders import encode_base64
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Sequence

from mail_signature import Signature

# Enough of a tag to say the caller wrote HTML rather than a paragraph that
# happens to contain a "<".
_HTML_MARKUP = re.compile(
    r"<(?:p|div|ul|ol|li|br|hr|strong|b|em|i|a|span|table|tr|td|h[1-6]|html|body)\b[^>]*>",
    re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
# A paragraph ends with a blank line between it and the next; a break is worth
# one newline. Told apart so the plain text alternative reads like the message.
_PARAGRAPH_END = re.compile(r"</(?:p|div|li|tr|h[1-6])\s*>", re.IGNORECASE)
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)


def is_html(body: str) -> bool:
    return bool(_HTML_MARKUP.search(body or ""))


def to_html(body: str) -> str:
    """A plain text body as HTML, so every message goes out as HTML.

    Written the way Mail writes what is typed into it: one div per paragraph,
    single newlines as breaks, and an empty line as a div holding a break.
    Paragraph tags were used before, but their margins add space no one typed —
    most visibly between the last line and the signature. Anything that looks
    like markup is escaped: a body containing "<3" must not become a broken tag.
    """
    if is_html(body):
        return body
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [block for block in re.split(r"\n\s*\n", text.strip()) if block.strip()]
    if not paragraphs:
        return ""
    return _EMPTY_LINE.join(
        "<div>" + "<br>".join(html_module.escape(line) for line in block.split("\n")) + "</div>"
        for block in paragraphs
    )


_EMPTY_LINE = "<div><br></div>"

# Blank space at the very end of a body: breaks, non-breaking spaces and empty
# blocks, possibly followed by the tags that close the last line.
_TRAILING_BLANK = re.compile(
    r"(?:\s|&nbsp;|<br\s*/?>|<(p|div)\b[^>]*>(?:\s|&nbsp;|<br\s*/?>)*</\1\s*>)+"
    r"((?:</[a-z][a-z0-9]*\s*>\s*)*)$",
    re.IGNORECASE,
)
# Everything a signature opens with before its first visible character.
_LEADING_MARKUP = re.compile(r"^(?:\s|<[^>]*>)*")


def _without_trailing_blank(html: str) -> str:
    """The body with nothing left after its last word.

    A body ending on blank lines would push the signature further down than
    the single empty line that is meant to separate them.
    """
    while True:
        trimmed = _TRAILING_BLANK.sub(r"\2", html)
        if trimmed == html:
            return html
        html = trimmed


def _without_leading_breaks(html: str) -> str:
    """The signature with the breaks it opens with removed.

    Mail stores most signatures starting on a break, because it expects to
    paste them under a line of their own. Here the separation is placed once,
    by compose_html; kept, those breaks would add a second and third empty line.
    """
    prefix = _LEADING_MARKUP.match(html).group(0)
    return _LINE_BREAK.sub("", prefix) + html[len(prefix):]


def to_text(html: str) -> str:
    """A readable plain text fallback, derived from the HTML that will be sent.

    This is the alternative part, shown only to a reader that refuses HTML. It
    does not have to be a faithful rendering, it has to be legible.
    """
    text = _PARAGRAPH_END.sub("\n\n", html or "")
    text = _LINE_BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html_module.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def compose_html(body: str, signature: Signature | None) -> str:
    """The full HTML document: the body, the signature, then a blank line.

    The blank line is the last thing in the document on purpose. It is what
    separates the signature from the attachments the reader sees underneath.

    Between the body and the signature there is exactly one empty line, whatever
    the body ends with and whatever the signature starts with.
    """
    parts = [_without_trailing_blank(to_html(body))]
    if signature is not None and signature.html.strip():
        # Mail marks its own signature with this id and separates it from the
        # body by a break; keeping both makes the message look like one Mail
        # composed, and lets Mail recognise the signature when the draft is
        # opened for editing.
        parts.append('<br><div id="AppleMailSignature">')
        parts.append(_without_leading_breaks(signature.html))
        parts.append("</div>")
    parts.append("<br>")
    return (
        '<html><head><meta charset="utf-8"></head><body>'
        + "".join(parts)
        + "</body></html>"
    )


def _attachment_part(path: str) -> MIMEBase:
    guessed, _ = mimetypes.guess_type(path)
    maintype, _, subtype = (guessed or "application/octet-stream").partition("/")
    with open(path, "rb") as handle:
        payload = handle.read()
    if maintype == "text":
        # Kept as bytes rather than decoded text: a file that is not valid UTF-8
        # must still arrive intact.
        part = MIMEBase("text", subtype or "plain")
        part.set_payload(payload)
        encode_base64(part)
    elif maintype == "application":
        part = MIMEApplication(payload, _subtype=subtype or "octet-stream")
    else:
        part = MIMEBase(maintype, subtype or "octet-stream")
        part.set_payload(payload)
        encode_base64(part)
    part.add_header("Content-Disposition", "attachment", filename=os.path.basename(path))
    return part


def classify_parts(raw: bytes) -> tuple[list[str], list[str]]:
    """The files a reader would receive, and the ones that only decorate.

    A signature logo is a part with a file name like any other, and Mail counts
    it among the attachments — so a preview built from Mail's own list announces
    an image the recipient never gets as a file. What settles it is the part's
    disposition: an attachment is offered, an inline part belongs to the body.
    For a message written elsewhere that says neither, a part the HTML shows
    with <img src="cid:…"> is taken as belonging to the body.
    """
    message = email.message_from_bytes(raw, policy=email.policy.default)

    shown: set[str] = set()
    for part in message.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True) or b""
        try:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            continue
        for cid in re.findall(r"<img[^>]+src=[\"']?cid:([^\"'>\s]+)", text, re.IGNORECASE):
            shown.add(cid.strip())

    attachments: list[str] = []
    inline: list[str] = []
    for part in message.walk():
        filename = part.get_filename()
        if part.get_content_maintype() == "multipart" or not filename:
            continue
        disposition = (part.get_content_disposition() or "").lower()
        content_id = (part.get("Content-Id") or "").strip("<>")
        if disposition == "attachment":
            attachments.append(filename)
        elif disposition == "inline" or content_id in shown:
            inline.append(filename)
        else:
            attachments.append(filename)
    return attachments, inline


def build_message(
    to: Sequence[str],
    subject: str,
    body: str,
    cc: Sequence[str] | None = None,
    bcc: Sequence[str] | None = None,
    attachment_paths: Sequence[str] | None = None,
    sender: str | None = None,
    signature: Signature | None = None,
    extra_headers: Sequence[tuple[str, str]] | None = None,
) -> MIMEBase:
    """Assembles the message. Nothing here talks to Mail."""
    html = compose_html(body, signature)

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(to_text(html), "plain", "utf-8"))
    alternative.attach(MIMEText(html, "html", "utf-8"))

    related = MIMEMultipart("related", type="text/html")
    related.attach(alternative)
    for image in signature.images if signature else []:
        part = MIMEBase(image.maintype, image.subtype)
        part.set_payload(image.payload)
        encode_base64(part)
        part.add_header("Content-Id", f"<{image.content_id}>")
        part.add_header("Content-Disposition", "inline", filename=image.filename)
        related.attach(part)

    paths = list(attachment_paths or [])
    if paths:
        root: MIMEBase = MIMEMultipart("mixed")
        root.attach(related)
        for path in paths:
            root.attach(_attachment_part(path))
    else:
        root = related

    if sender:
        root["From"] = sender
    root["To"] = ", ".join(to)
    if cc:
        root["Cc"] = ", ".join(cc)
    if bcc:
        root["Bcc"] = ", ".join(bcc)
    root["Subject"] = subject
    for name, value in extra_headers or ():
        root[name] = value
    return root
