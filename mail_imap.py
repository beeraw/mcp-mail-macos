"""Talking to the account's own server, for the two things Mail cannot do.

Mail's composer rewrites whatever it is given: a break inserted above the body,
the body wrapped in a cite blockquote, attachments slipped into the text ahead
of the signature. None of it is reachable from AppleScript. And Mail can only
send a message it composed itself, so a message built here could not be handed
back to it to send.

Both stop being problems once the message goes to the server directly: a draft
is an IMAP APPEND into the account's Drafts folder, and a send is an ordinary
SMTP submission. What arrives is byte for byte what was built.

Everything but the password is read from Mail's own settings, so nothing has to
be configured twice. The password is kept in the login keychain, under the
service name below and the account's own user name:

    security add-generic-password -U -s mcp-mail-macos -a <user> -w <password>

On an account with two step verification — every Google account here — that is
an app password, not the account password.
"""

from __future__ import annotations

import base64
import imaplib
import re
import smtplib
import ssl
import subprocess
import time
from dataclasses import dataclass
from typing import Sequence

import config
from mail_tools import MailError, _field, _parse_records, run_script

KEYCHAIN_SERVICE = config.get("keychain_service")


@dataclass
class Server:
    host: str
    port: int
    ssl_enabled: bool
    user: str


@dataclass
class Account:
    name: str
    identifier: str
    incoming: Server
    outgoing: Server


def _server(row: Sequence[str], offset: int) -> Server:
    port_text = _field(row, offset + 1)
    return Server(
        host=_field(row, offset),
        port=int(port_text) if port_text.isdigit() else 0,
        ssl_enabled=_field(row, offset + 2).lower() == "true",
        user=_field(row, offset + 3),
    )


def accounts() -> list[Account]:
    """Every account, with the server settings Mail already holds."""
    return [
        Account(
            name=_field(row, 0),
            identifier=_field(row, 1),
            incoming=_server(row, 2),
            outgoing=_server(row, 6),
        )
        for row in _parse_records(run_script("list_servers", []))
    ]


def find_account(name: str) -> Account:
    for account in accounts():
        if account.name == name:
            return account
    raise MailError("account_not_found", f"Mail has no account named {name!r}.")


def password_for(user: str) -> str:
    """The password from the login keychain, or a clear answer about adding it.

    The lookup runs through /usr/bin/security, the same tool that stored it, so
    the keychain grants it without a prompt. A password put there by hand in
    Keychain Access is refused until it is allowed once.
    """
    if not user:
        raise MailError("no_user_name", "This account has no user name in Mail.")
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
            capture_output=True,
            timeout=20,
        )
    except Exception as error:  # noqa: BLE001
        raise MailError("keychain_unavailable", f"Could not read the keychain: {error}") from error
    if completed.returncode != 0:
        raise MailError(
            "password_not_stored",
            f"No password stored for {user}.",
            "Add one with: security add-generic-password -U -s "
            f"{KEYCHAIN_SERVICE} -a {user} -w <app password>",
        )
    return completed.stdout.decode("utf-8").rstrip("\n")


def _connect(account: Account) -> imaplib.IMAP4:
    server = account.incoming
    if not server.host:
        raise MailError(
            "no_imap_server",
            f"The account {account.name!r} has no IMAP server.",
            "An Exchange or Outlook account cannot be reached this way.",
        )
    try:
        if server.ssl_enabled or server.port == 993:
            connection: imaplib.IMAP4 = imaplib.IMAP4_SSL(
                server.host, server.port or 993, ssl_context=ssl.create_default_context()
            )
        else:
            connection = imaplib.IMAP4(server.host, server.port or 143)
            connection.starttls(ssl.create_default_context())
    except Exception as error:  # noqa: BLE001
        raise MailError(
            "imap_unreachable", f"Could not reach {server.host}: {error}"
        ) from error

    try:
        connection.login(server.user, password_for(server.user))
    except imaplib.IMAP4.error as error:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001
            pass
        raise MailError(
            "imap_login_refused",
            f"{server.host} refused the login for {server.user}: {error}",
            "On an account with two step verification the stored password must "
            "be an app password, not the account password.",
        ) from error
    return connection


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Folder names to fall back on, per special use, when a server does not flag
# its own folders. The list is short on purpose: it only has to cover the
# languages an account here is actually configured in.
_FALLBACK_NAMES = {
    "\\drafts": {"drafts", "brouillons", "entwürfe", "borradores", "bozze"},
    "\\sent": {"sent", "sent messages", "sent mail", "messages envoyés", "éléments envoyés"},
}


# A LIST answer is: (attributes) "delimiter" name — where the name is quoted
# whenever it holds a space, which "Messages envoyés" does. Splitting the line
# on whitespace would keep only the last word of it.
_LIST_LINE = re.compile(
    r'^\((?P<attributes>[^)]*)\)\s+(?:"(?P<delimiter>[^"]*)"|NIL)\s+'
    r'(?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<bare>\S+))\s*$'
)


def decode_folder(name: str) -> str:
    """A folder name as a person reads it.

    IMAP writes anything outside ASCII in a modified UTF-7 of its own, so the
    Sent folder comes over the wire as "[Gmail]/Messages envoy&AOk-s". The
    server is always given the encoded form back; this is for saying which
    folder was used.
    """
    # Python ships no imap4-utf-7 codec, so the few rules are applied here:
    # "&" opens a base64 run of UTF-16 closed by "-", and "&-" is a literal
    # ampersand.
    if "&" not in name:
        return name
    out: list[str] = []
    index = 0
    while index < len(name):
        char = name[index]
        if char != "&":
            out.append(char)
            index += 1
            continue
        end = name.find("-", index + 1)
        if end == -1:
            out.append(name[index:])
            break
        chunk = name[index + 1 : end]
        if not chunk:
            out.append("&")
        else:
            padded = chunk.replace(",", "/")
            padded += "=" * (-len(padded) % 4)
            try:
                out.append(base64.b64decode(padded).decode("utf-16-be"))
            except Exception:  # noqa: BLE001 - an unreadable run stays as it came
                out.append(name[index : end + 1])
        index = end + 1
    return "".join(out)


def special_folder(connection: imaplib.IMAP4, special_use: str) -> str:
    """A folder found by what it is for, rather than by what it is called.

    A server that supports SPECIAL-USE flags the folder itself, which is the
    only reliable answer: the name is localised (Gmail answers
    "[Gmail]/Brouillons" here), and an ordinary folder called "Drafts" may sit
    next to the real one without being it. The name is returned as the server
    wrote it, since that is what it will be given back.
    """
    status, rows = connection.list()
    if status != "OK":
        raise MailError("imap_list_failed", "The server would not list its folders.")

    fallback = ""
    for row in rows or []:
        line = row.decode("utf-8", errors="replace") if isinstance(row, bytes) else str(row)
        parsed = _LIST_LINE.match(line.strip())
        if not parsed:
            continue
        name = parsed.group("quoted")
        name = name.replace('\\"', '"').replace("\\\\", "\\") if name is not None else parsed.group("bare")
        if special_use in parsed.group("attributes").lower():
            return name
        leaf = decode_folder(name).rsplit("/", 1)[-1].lower()
        if not fallback and leaf in _FALLBACK_NAMES[special_use]:
            fallback = name
    if fallback:
        return fallback
    raise MailError(
        "special_folder_not_found",
        f"The server does not say which folder is its {special_use.lstrip(chr(92))}.",
    )


def _append(account: Account, message_bytes: bytes, special_use: str, flags: str) -> str:
    connection = _connect(account)
    try:
        folder = special_folder(connection, special_use)
        status, response = connection.append(
            _quote(folder),
            flags,
            imaplib.Time2Internaldate(time.time()),
            message_bytes,
        )
        if status != "OK":
            raise MailError("imap_append_failed", f"The server refused it: {response!r}")
    finally:
        try:
            connection.logout()
        except Exception:  # noqa: BLE001 - the message is already filed
            pass
    return folder


def append_draft(account_name: str, message_bytes: bytes) -> dict[str, str]:
    """Files a message in the account's Drafts, exactly as it was built."""
    account = find_account(account_name)
    folder = _append(account, message_bytes, "\\drafts", r"(\Draft \Seen)")
    return {"folder": folder, "account": account.name}


def append_sent(account: Account, message_bytes: bytes) -> str | None:
    """Files a sent message in Sent, for a server that does not do it itself.

    Gmail files everything submitted through its own SMTP, so doing it here as
    well would show the message twice. Every other server files nothing, and
    without this the message would be sent and then be nowhere to be seen.
    """
    if "gmail.com" in account.outgoing.host.lower():
        return None
    try:
        return _append(account, message_bytes, "\\sent", r"(\Seen)")
    except MailError:
        # The message has gone out. Not being able to file a copy is worth
        # reporting, never worth turning a successful send into a failure.
        return None


def fetch_message(account_name: str, rfc_message_id: str, folder: str | None = None) -> bytes:
    """A message as the server holds it, whole.

    Taking a message apart and composing it again is how formatting and
    attachments get lost: the body comes back as plain text, the files have to
    be dug out of Mail's storage, and what leaves is a rebuild rather than what
    was read. Fetched like this, it is exactly what the server holds.

    Without a folder, the account's Drafts is searched.
    """
    if not rfc_message_id:
        raise MailError(
            "message_without_id",
            "This message carries no message id, so it cannot be found on the server.",
        )
    account = find_account(account_name)
    connection = _connect(account)
    try:
        folder = folder or special_folder(connection, "\\drafts")
        connection.select(_quote(folder), readonly=True)
        # The value must be quoted: a bare message id holds "<", "@" and "."
        # and the server answers "Could not parse command".
        status, found = connection.search(None, "HEADER", "Message-ID", _quote(rfc_message_id))
        identifiers = (found[0].split() if found and found[0] else []) if status == "OK" else []
        if not identifiers:
            raise MailError(
                "message_not_on_server",
                f"No message with id {rfc_message_id} in {decode_folder(folder)}.",
                "Mail may not have saved it to the server yet; try again in a moment.",
            )
        status, parts = connection.fetch(identifiers[-1], "(RFC822)")
        if status != "OK" or not parts or not isinstance(parts[0], tuple):
            raise MailError("message_fetch_failed", "The server would not hand back the message.")
        return parts[0][1]
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            connection.logout()
        except Exception:  # noqa: BLE001
            pass


def fetch_draft(account_name: str, rfc_message_id: str) -> bytes:
    """The draft as the server holds it. See fetch_message."""
    return fetch_message(account_name, rfc_message_id)


def delete_draft(account_name: str, rfc_message_id: str) -> bool:
    """Removes a draft from the server, for good.

    Deleting it through Mail is a fight: the account pushes the draft back
    several seconds after the local delete reports success, so it has to be
    deleted repeatedly and watched for twenty seconds. Expunged on the server,
    it is gone, and there is nothing left to come back.
    """
    if not rfc_message_id:
        return False
    account = find_account(account_name)
    connection = _connect(account)
    try:
        folder = special_folder(connection, "\\drafts")
        connection.select(_quote(folder))
        # The value must be quoted: a bare message id holds "<", "@" and "."
        # and the server answers "Could not parse command".
        status, found = connection.search(None, "HEADER", "Message-ID", _quote(rfc_message_id))
        identifiers = (found[0].split() if found and found[0] else []) if status == "OK" else []
        if not identifiers:
            return False
        for identifier in identifiers:
            connection.store(identifier, "+FLAGS", r"(\Deleted)")
        connection.expunge()
        return True
    finally:
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            connection.logout()
        except Exception:  # noqa: BLE001
            pass


def send_message(account_name: str, message_bytes: bytes, recipients: Sequence[str]) -> dict[str, str]:
    """Submits a message through the account's own outgoing server."""
    account = find_account(account_name)
    server = account.outgoing
    if not server.host:
        raise MailError(
            "no_smtp_server",
            f"The account {account.name!r} has no outgoing server in Mail.",
        )
    user = server.user or account.incoming.user
    password = password_for(user)
    context = ssl.create_default_context()

    try:
        if server.port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(server.host, 465, context=context, timeout=60)
        else:
            client = smtplib.SMTP(server.host, server.port or 587, timeout=60)
            client.ehlo()
            if server.ssl_enabled or server.port in (587, 0):
                client.starttls(context=context)
                client.ehlo()
    except Exception as error:  # noqa: BLE001
        raise MailError("smtp_unreachable", f"Could not reach {server.host}: {error}") from error

    try:
        client.login(user, password)
        client.sendmail(user, list(recipients), message_bytes)
    except smtplib.SMTPAuthenticationError as error:
        raise MailError(
            "smtp_login_refused",
            f"{server.host} refused the login for {user}: {error}",
            "On an account with two step verification the stored password must "
            "be an app password, not the account password.",
        ) from error
    except Exception as error:  # noqa: BLE001
        raise MailError("smtp_send_failed", f"The message was not sent: {error}") from error
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            pass

    result = {"account": account.name, "server": server.host}
    filed = append_sent(account, message_bytes)
    if filed:
        result["filed_in"] = filed
    return result
