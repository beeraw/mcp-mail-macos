"""The signature Mail.app would have used, read from Mail's own settings.

A message this server builds is assembled outside Mail, so Mail never gets the
chance to append the signature it would have added to a message composed by
hand. Rather than keep a second copy of the signature here — which would drift
the day it is edited in Mail's settings — it is read back from where Mail keeps
it, and the account's own choice is honoured.

Three files hold the answer:

  com.apple.mail preferences  SignaturesSelected maps an account UUID to the
                              signature UUID chosen for it, and
                              SignatureSelectionMethods says whether a signature
                              is used at all.
  AllSignatures.plist         maps that signature UUID to its name.
  <uuid>.mailsignature        the signature itself, stored as a MIME message:
                              a text/html part, plus the images it references.
"""

from __future__ import annotations

import email
import email.policy
import os
import plistlib
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

MAIL_ROOT = os.path.expanduser("~/Library/Mail")

# Mail's storage is versioned (V9, V10, ...). The highest one is the live one.
_STORE_PATTERN = re.compile(r"^V(\d+)$")

# Apple writes an inline image as <object type="application/x-apple-msg-attachment"
# data="cid:...">, which only Mail knows how to render: every other client shows
# a blank. The same reference as an <img> renders everywhere, Mail included.
_APPLE_OBJECT = re.compile(
    r"<object\b(?P<attributes>[^>]*)>\s*</object\s*>",
    re.IGNORECASE,
)
_HEAD_BLOCK = re.compile(r"<head\b[^>]*>.*?</head\s*>", re.IGNORECASE | re.DOTALL)
# Mail's composer treats an element of this class as one of its own string
# attachments. Met in a draft it did not write, it drops the element and its
# content when the draft is opened: the signature vanished, text and logo alike,
# while every other client — and Mail's own preview — still showed it.
_STRING_ATTACHMENT_CLASS = re.compile(
    r"""\s+class\s*=\s*(["']?)Apple-string-attachment\1(?=[\s>/])""",
    re.IGNORECASE,
)
_ATTRIBUTE = re.compile(r"""(?P<name>[\w-]+)\s*=\s*(?P<value>"[^"]*"|'[^']*'|[^\s>]+)""")


@dataclass
class InlineImage:
    """An image the signature refers to by content id."""

    content_id: str
    maintype: str
    subtype: str
    filename: str
    payload: bytes


@dataclass
class Signature:
    identifier: str
    name: str
    html: str
    images: list[InlineImage] = field(default_factory=list)


def _store_root() -> str | None:
    """The versioned folder Mail is currently using, e.g. ~/Library/Mail/V10."""
    try:
        versions = [
            (int(match.group(1)), name)
            for name in os.listdir(MAIL_ROOT)
            if (match := _STORE_PATTERN.match(name))
        ]
    except OSError:
        return None
    if not versions:
        return None
    return os.path.join(MAIL_ROOT, max(versions)[1])


def _preferences() -> dict[str, Any]:
    """Mail's preferences, read through defaults so a cached write is included."""
    try:
        raw = subprocess.run(
            ["defaults", "export", "com.apple.mail", "-"],
            capture_output=True,
            timeout=10,
        ).stdout
        loaded = plistlib.loads(raw)
    except Exception:  # noqa: BLE001 - no preferences means no signature, not a failure
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _signature_names() -> dict[str, str]:
    """signature UUID -> the name shown in Mail's settings, and in AppleScript."""
    root = _store_root()
    if not root:
        return {}
    path = os.path.join(root, "MailData", "Signatures", "AllSignatures.plist")
    try:
        with open(path, "rb") as handle:
            entries = plistlib.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(entries, list):
        return {}
    return {
        entry["SignatureUniqueId"]: entry.get("SignatureName", "")
        for entry in entries
        if isinstance(entry, dict) and entry.get("SignatureUniqueId")
    }


def _rewrite_apple_objects(html: str) -> str:
    """Turns Apple's <object data="cid:..."> into an <img src="cid:...">."""

    def replace(match: re.Match[str]) -> str:
        attributes = dict(
            (found.group("name").lower(), found.group("value").strip("\"'"))
            for found in _ATTRIBUTE.finditer(match.group("attributes"))
        )
        source = attributes.get("data", "")
        if not source.lower().startswith("cid:"):
            # Not an inline image; dropping it is safer than shipping a tag no
            # client but Mail understands.
            return ""
        parts = [f'<img src="{source}"']
        for dimension in ("width", "height"):
            if attributes.get(dimension):
                parts.append(f'{dimension}="{attributes[dimension]}"')
        parts.append('style="border:0;" alt="">')
        return " ".join(parts)

    return _APPLE_OBJECT.sub(replace, html)


def _drop_string_attachment_class(html: str) -> str:
    """Removes the class that makes Mail discard the signature on opening."""
    return _STRING_ATTACHMENT_CLASS.sub("", html)


def _load_signature_file(root: str, signature_id: str, name: str) -> Signature | None:
    path = os.path.join(root, "MailData", "Signatures", f"{signature_id}.mailsignature")
    try:
        with open(path, "rb") as handle:
            message = email.message_from_binary_file(handle, policy=email.policy.default)
    except (OSError, ValueError):
        return None

    html = ""
    images: list[InlineImage] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        content_id = (part.get("Content-Id") or "").strip("<>")
        if part.get_content_type() == "text/html" and not content_id and not html:
            try:
                html = part.get_content()
            except Exception:  # noqa: BLE001 - a signature that cannot be decoded is skipped
                return None
        elif content_id:
            payload = part.get_payload(decode=True) or b""
            if not payload:
                continue
            images.append(
                InlineImage(
                    content_id=content_id,
                    maintype=part.get_content_maintype(),
                    subtype=part.get_content_subtype(),
                    filename=part.get_filename() or content_id,
                    payload=payload,
                )
            )

    if not html.strip():
        return None

    html = _HEAD_BLOCK.sub("", html)
    html = _rewrite_apple_objects(html)
    html = _drop_string_attachment_class(html)
    referenced = {image.content_id for image in images if f"cid:{image.content_id}" in html}
    return Signature(
        identifier=signature_id,
        name=name,
        html=html.strip(),
        images=[image for image in images if image.content_id in referenced],
    )


def signature_for_account(account_id: str) -> Signature | None:
    """The signature Mail would append for this account, or None.

    None is the answer whenever Mail itself would add nothing: no signature
    chosen for the account, or a selection method that means "none".
    """
    if not account_id:
        return None
    root = _store_root()
    if not root:
        return None

    preferences = _preferences()
    method = (preferences.get("SignatureSelectionMethods") or {}).get(account_id)
    if isinstance(method, str) and method.lower() in {"none", "nosignature"}:
        return None

    signature_id = (preferences.get("SignaturesSelected") or {}).get(account_id)
    if not isinstance(signature_id, str) or not signature_id:
        return None

    name = _signature_names().get(signature_id, "")
    return _load_signature_file(root, signature_id, name)
