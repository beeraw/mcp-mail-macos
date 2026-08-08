"""MCP server exposing macOS Mail.app over stdio.

Run it with:  python server.py
Every tool returns a dictionary. Failures come back as {"ok": false, ...} with
an error code and, when there is one, a hint on how to fix the situation,
rather than as a protocol-level exception: the caller can then correct itself.
"""

from __future__ import annotations

from typing import Any, Sequence

try:
    # SDK 2.x: FastMCP was renamed, but the decorator API is unchanged.
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover - SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

import mail_files
import mail_search
import mail_tools
from mail_tools import MailError

mcp = _Server("mail-macos")


def _guard(function, *args, **kwargs) -> dict[str, Any]:
    try:
        return function(*args, **kwargs)
    except MailError as error:
        return error.to_dict()
    except Exception as error:  # noqa: BLE001 - never break the MCP session
        return {"ok": False, "error_code": "unexpected_error", "error": str(error)}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


@mcp.tool()
def list_mailboxes(include_totals: bool = False) -> dict[str, Any]:
    """List every account and mailbox, with their unread counts.

    Args:
        include_totals: also count the messages in each mailbox. Off by default
            because Mail walks each mailbox to answer, which takes seconds.
    """
    return _guard(mail_tools.list_mailboxes, include_totals=include_totals)


@mcp.tool()
def list_messages(
    mailbox: str | None = None,
    account: str | None = None,
    limit: int = 20,
    unread_only: bool = False,
    include_preview: bool = False,
    scan_limit: int | None = None,
) -> dict[str, Any]:
    """List the most recent messages of a mailbox, newest first.

    Args:
        mailbox: mailbox path as returned by list_mailboxes, for instance
            "INBOX" or "[Gmail]/Important". Defaults to the unified inbox.
        account: account name; without it, well-known names resolve to Mail's
            unified mailboxes.
        limit: how many messages to return (max 200).
        unread_only: keep only unread messages found in the scanned window.
        include_preview: include the first characters of the body. Costly:
            Mail needs about a second per message.
        scan_limit: how many recent messages to look at when filtering.
    """
    return _guard(
        mail_tools.list_messages,
        mailbox=mailbox,
        account=account,
        limit=limit,
        unread_only=unread_only,
        include_preview=include_preview,
        scan_limit=scan_limit,
    )


@mcp.tool()
def search_all(
    query: str,
    account: str | None = None,
    mailbox: str | None = None,
    unread_only: bool = False,
    flagged_only: bool = False,
    since: str | None = None,
    until: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search every message of every account, from the local index.

    This is the only way to search: it covers the whole archive in
    milliseconds, and it never asks Mail, which serves every request on its
    interface thread. The index is built by mail_index.py; if it is missing or
    stale, index_status says so and sync_index refreshes it.

    Subject, sender, recipients, body and attachment names are all searched.
    Words can be restricted to one field with "subject: facture", combined with
    AND / OR / NOT, and quoted for an exact phrase.

    Args:
        query: what to look for.
        account: restrict to one account.
        mailbox: restrict to one mailbox path.
        unread_only: keep only messages still unread somewhere.
        flagged_only: keep only flagged messages.
        since: earliest date, YYYY-MM-DD.
        until: latest date, YYYY-MM-DD.
        limit: how many messages to return (max 200).
    """
    return _guard(
        mail_search.search_all,
        query=query,
        account=account,
        mailbox=mailbox,
        unread_only=unread_only,
        flagged_only=flagged_only,
        since=since,
        until=until,
        limit=limit,
    )


@mcp.tool()
def get_thread(message_id: str, limit: int = 100) -> dict[str, Any]:
    """List every message of the conversation a message belongs to, oldest first.

    Covers the whole exchange, including replies filed in another mailbox or
    sent from another account. Read from the index, so it costs milliseconds.

    Args:
        message_id: any message of the thread.
        limit: how many messages to return (max 500).
    """
    return _guard(mail_search.get_thread, message_id=message_id, limit=limit)


@mcp.tool()
def index_status() -> dict[str, Any]:
    """Report what the search index holds and when it was last refreshed."""
    return _guard(mail_search.index_status)


@mcp.tool()
def sync_index() -> dict[str, Any]:
    """Refresh the search index: new messages in, deleted ones out.

    Needs Full Disk Access for the process running this server.
    """
    return _guard(mail_search.sync_index)


@mcp.tool()
def get_message(message_id: str, max_body_chars: int = 20000) -> dict[str, Any]:
    """Fetch a message in full: body, headers and attachment list.

    Args:
        message_id: identifier returned by list_messages or search_all.
        max_body_chars: body characters to keep; the answer says whether it was cut.
    """
    return _guard(mail_tools.get_message, message_id=message_id, max_body_chars=max_body_chars)


@mcp.tool()
def count_unread(mailbox: str | None = None, account: str | None = None) -> dict[str, Any]:
    """Count unread messages, for one mailbox or across every account.

    Args:
        mailbox: mailbox path; without it, every mailbox holding unread mail is listed.
        account: restrict the count to one account.
    """
    return _guard(mail_tools.count_unread, mailbox=mailbox, account=account)


# --------------------------------------------------------------------------
# Sending
# --------------------------------------------------------------------------


@mcp.tool()
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
    """Compose and send a message.

    Nothing leaves until confirm is true. Called without it, this returns a
    preview of exactly what would be sent — show that to the user, then call
    again with confirm=true.

    To send something already drafted, use send_draft instead: composing it
    again here would post a rewritten copy and strand the reviewed draft.

    Args:
        to: one address, a comma-separated string, or a list of addresses.
        subject: subject line.
        body: plain text body.
        cc: carbon copy recipients.
        bcc: blind carbon copy recipients.
        attachments: absolute paths of existing files.
        sender: address to send from; without it Mail uses its default account.
        confirm: set to true to actually send.
    """
    return _guard(
        mail_tools.send_email,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        sender=sender,
        confirm=confirm,
    )


@mcp.tool()
def create_draft(
    to: str | Sequence[str],
    subject: str,
    body: str,
    cc: str | Sequence[str] | None = None,
    bcc: str | Sequence[str] | None = None,
    attachments: Sequence[str] | None = None,
    sender: str | None = None,
) -> dict[str, Any]:
    """Compose a message and save it to Drafts without sending it.

    Args:
        to: one address, a comma-separated string, or a list of addresses.
        subject: subject line.
        body: plain text body.
        cc: carbon copy recipients.
        bcc: blind carbon copy recipients.
        attachments: absolute paths of existing files.
        sender: address to send from; without it Mail uses its default account.
    """
    return _guard(
        mail_tools.create_draft,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        sender=sender,
    )


@mcp.tool()
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
    """Write a draft as an .eml file, without touching Mail.

    This is the way to prepare a message for review. Mail is not involved, so no
    draft is created there and none can be left behind. The user can open the
    file to read it as a real message, and send_draft_file posts exactly what
    the file holds.

    Args:
        to: one address, a comma-separated string, or a list of addresses.
        subject: subject line.
        body: plain text body.
        cc: carbon copy recipients.
        bcc: blind carbon copy recipients.
        attachments: absolute paths of existing files; they are embedded in the file.
        sender: address to send from; without it Mail uses its default account.
        folder: where to write it; defaults to the server's own mails folder.
    """
    return _guard(
        mail_files.write_draft,
        to=to,
        subject=subject,
        body=body,
        cc=cc,
        bcc=bcc,
        attachments=attachments,
        sender=sender,
        folder=folder,
    )


@mcp.tool()
def list_drafts(folder: str | None = None) -> dict[str, Any]:
    """List the .eml drafts waiting to be sent, newest first.

    Args:
        folder: where to look; defaults to the server's own mails folder.
    """
    return _guard(mail_files.list_drafts, folder=folder)


@mcp.tool()
def read_draft_file(path: str) -> dict[str, Any]:
    """Read one .eml draft in full, to show it before sending.

    Args:
        path: path of the draft file, as returned by write_draft or list_drafts.
    """
    return _guard(mail_files.read_draft_file, path=path)


@mcp.tool()
def send_draft_file(path: str, confirm: bool = False, keep_file: bool = False) -> dict[str, Any]:
    """Send an .eml draft, then file it away under sent/.

    What leaves is built from the file, so the message sent is the one that was
    reviewed. Nothing is ever drafted in Mail, so there is nothing to clean up.

    Args:
        path: path of the draft file.
        confirm: set to true to actually send. Without it, the file's content
            comes back as a preview and nothing is sent.
        keep_file: leave the file where it is instead of filing it away.
    """
    return _guard(mail_files.send_draft_file, path=path, confirm=confirm, keep_file=keep_file)


@mcp.tool()
def discard_draft_file(path: str) -> dict[str, Any]:
    """Delete an .eml draft that will not be sent.

    Args:
        path: path of the draft file.
    """
    return _guard(mail_files.discard_draft_file, path=path)


@mcp.tool()
def purge_drafts(folder: str | None = None) -> dict[str, Any]:
    """Remove .eml drafts left on disk: unsent after 7 days, sent after 30.

    Runs on its own whenever a draft is written or listed, and from sync_index.

    Args:
        folder: where to sweep; defaults to the server's own mails folder.
    """
    return _guard(mail_files.purge_drafts, folder=folder)


@mcp.tool()
def send_draft(message_id: str, confirm: bool = False) -> dict[str, Any]:
    """Send a draft that already exists, with the content it holds.

    Always use this to send something already drafted, rather than composing it
    again with send_email. The text is read back from the draft, so what leaves
    is what the user reviewed, word for word; the draft is then removed. Calling
    send_email instead would post a freshly written copy and leave the reviewed
    draft sitting in Drafts.

    Refuses any message that is not in a Drafts mailbox, and says where it was
    found instead. Attachments are carried over; if they cannot be recovered,
    nothing is sent rather than a message missing its files.

    Args:
        message_id: identifier of the draft, as returned by create_draft or by
            list_messages(mailbox="drafts").
        confirm: set to true to actually send. Without it, the draft's real
            content comes back as a preview and nothing is sent.
    """
    return _guard(mail_tools.send_draft, message_id=message_id, confirm=confirm)


@mcp.tool()
def reply_to_message(
    message_id: str,
    body: str,
    reply_all: bool = False,
    send: bool = True,
    confirm: bool = False,
) -> dict[str, Any]:
    """Reply to an existing message, keeping it in the same thread.

    Mail briefly opens a compose window to build the reply; that is the only way
    to get the threading headers filled in.

    Args:
        message_id: identifier of the message being answered.
        body: the answer, inserted above the quoted original.
        reply_all: also answer the other recipients.
        send: send straight away; set to false to leave the reply in Drafts.
        confirm: required when send is true. Without it, who the reply would go
            to comes back as a preview and nothing is sent.
    """
    return _guard(
        mail_tools.reply_to_message,
        message_id=message_id,
        body=body,
        reply_all=reply_all,
        send=send,
        confirm=confirm,
    )


# --------------------------------------------------------------------------
# Organising
# --------------------------------------------------------------------------


@mcp.tool()
def create_mailbox(name: str, parent: str | None = None, account: str | None = None) -> dict[str, Any]:
    """Create a mailbox.

    Args:
        name: name of the new mailbox.
        parent: path of the parent mailbox, to nest it.
        account: account to create it in; without it, the mailbox is local ("On My Mac").
    """
    return _guard(mail_tools.create_mailbox, name=name, parent=parent, account=account)


@mcp.tool()
def move_message(message_id: str, target_mailbox: str, target_account: str | None = None) -> dict[str, Any]:
    """Move a message to another mailbox.

    Mail gives the moved copy a new identifier, returned as message_id; the old
    one no longer resolves.

    Args:
        message_id: message to move.
        target_mailbox: destination mailbox path.
        target_account: destination account; defaults to the message's own account.
    """
    return _guard(
        mail_tools.move_message,
        message_id=message_id,
        target_mailbox=target_mailbox,
        target_account=target_account,
    )


@mcp.tool()
def delete_message(message_id: str) -> dict[str, Any]:
    """Move a message to the trash.

    Args:
        message_id: message to delete.
    """
    return _guard(mail_tools.delete_message, message_id=message_id)


@mcp.tool()
def mark_as_read(message_id: str) -> dict[str, Any]:
    """Mark a message as read.

    Args:
        message_id: message to mark.
    """
    return _guard(mail_tools.mark_as_read, message_id=message_id)


@mcp.tool()
def mark_as_unread(message_id: str) -> dict[str, Any]:
    """Mark a message as unread.

    Args:
        message_id: message to mark.
    """
    return _guard(mail_tools.mark_as_unread, message_id=message_id)


@mcp.tool()
def flag_message(message_id: str, flag_color: str | None = None) -> dict[str, Any]:
    """Flag a message, optionally with a colour.

    Args:
        message_id: message to flag.
        flag_color: red, orange, yellow, green, blue, purple, gray, or "none"
            to clear the flag.
    """
    return _guard(mail_tools.flag_message, message_id=message_id, flag_color=flag_color)


if __name__ == "__main__":
    mcp.run(transport="stdio")
