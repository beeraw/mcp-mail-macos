#!/usr/bin/env python3
"""Manual test runner for the Mail tools, without going through Claude Code.

Read-only checks:
    python test_manual.py read
    python test_manual.py read --account Work --mailbox INBOX

Write checks (they really touch Mail; a draft is created and deleted, and if
--send is passed a message is actually sent):
    python test_manual.py write --to you@example.com
    python test_manual.py write --to you@example.com --send

Everything each check does is printed as it goes, so a failure is easy to place.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

import mail_tools

PASSED = 0
FAILED = 0


def check(label: str, function: Callable[[], Any]) -> Any:
    """Runs one check, prints how long it took, and keeps score."""
    global PASSED, FAILED
    print(f"\n▶ {label}")
    started = time.time()
    try:
        result = function()
    except mail_tools.MailError as error:
        FAILED += 1
        print(f"  ✗ {error.code}: {error.message}")
        if error.hint:
            print(f"    hint: {error.hint}")
        return None
    except Exception as error:  # noqa: BLE001 - a manual runner reports everything
        FAILED += 1
        print(f"  ✗ unexpected: {type(error).__name__}: {error}")
        return None
    elapsed = time.time() - started
    if isinstance(result, dict) and result.get("ok") is False:
        FAILED += 1
        print(f"  ✗ ({elapsed:.1f}s) {json.dumps(result, ensure_ascii=False)}")
        return None
    PASSED += 1
    print(f"  ✓ ({elapsed:.1f}s)")
    return result


def show(payload: Any, limit: int = 600) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(text) > limit:
        text = text[:limit] + " ..."
    print("    " + text.replace("\n", "\n    "))


def run_read_tests(account: str | None, mailbox: str | None) -> None:
    mailboxes = check("list_mailboxes", lambda: mail_tools.list_mailboxes())
    if mailboxes:
        print(f"    {mailboxes['mailbox_count']} mailboxes across "
              f"{len(mailboxes['accounts'])} accounts")
        for entry in mailboxes["accounts"]:
            paths = ", ".join(box["path"] for box in entry["mailboxes"][:6])
            print(f"    - {entry['account']}: {paths} ...")
        if account is None and mailboxes["accounts"]:
            first = mailboxes["accounts"][0]
            account = first["account"]
            if mailbox is None and first["mailboxes"]:
                mailbox = first["mailboxes"][0]["path"]
            print(f"    (no target given, falling back to {account} / {mailbox})")

    check("count_unread (every account)", lambda: mail_tools.count_unread())

    listing = check(
        f"list_messages ({account} / {mailbox})",
        lambda: mail_tools.list_messages(mailbox=mailbox, account=account, limit=5),
    )
    if listing:
        print(f"    {listing['total_in_mailbox']} messages in this mailbox")
        for message in listing["messages"]:
            print(f"    - {message['date_received']} {message['subject'][:60]}")

    check(
        "list_messages (unread only)",
        lambda: mail_tools.list_messages(
            mailbox=mailbox, account=account, limit=5, unread_only=True, scan_limit=50
        ),
    )

    if listing and listing["messages"]:
        message_id = listing["messages"][0]["message_id"]
        full = check("get_message", lambda: mail_tools.get_message(message_id, max_body_chars=400))
        if full:
            show(
                {
                    "subject": full["subject"],
                    "sender": full["sender"],
                    "to": full["to"],
                    "attachments": full["attachments"],
                    "body_start": full["body"][:200],
                }
            )

        word = ""
        for candidate in listing["messages"][0]["subject"].split():
            if len(candidate) >= 5:
                word = candidate
                break
        if word:
            check(
                f"search_messages (subject, {word!r})",
                lambda: mail_tools.search_messages(
                    word, mailbox=mailbox, account=account, field="subject", limit=5, scan_limit=100
                ),
            )

    check(
        "search_messages (unknown mailbox, expected to fail cleanly)",
        lambda: _expect_error(
            lambda: mail_tools.search_messages("x", mailbox="___no_such_mailbox___"),
            "mailbox_not_found",
        ),
    )
    check(
        "get_message (malformed id, expected to fail cleanly)",
        lambda: _expect_error(lambda: mail_tools.get_message("not-a-real-id"), "invalid_message_id"),
    )


def _expect_error(function: Callable[[], Any], expected_code: str) -> dict[str, Any]:
    try:
        function()
    except mail_tools.MailError as error:
        if error.code == expected_code:
            return {"ok": True, "raised": error.code}
        return {"ok": False, "error": f"expected {expected_code}, got {error.code}"}
    return {"ok": False, "error": f"expected {expected_code}, nothing was raised"}


def run_write_tests(recipient: str, really_send: bool, account: str | None) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    subject = f"[mcp-mail-macos] manual test {stamp}"

    draft = check(
        "create_draft",
        lambda: mail_tools.create_draft(
            to=recipient,
            subject=subject,
            body="Message created by test_manual.py. It can be deleted.",
        ),
    )
    if draft:
        show(draft)
        print("    check that the draft shows up in Mail > Drafts")

    mailbox_name = f"mcp-test-{time.strftime('%H%M%S')}"
    created = check(
        f"create_mailbox ({mailbox_name})",
        lambda: mail_tools.create_mailbox(name=mailbox_name, account=account),
    )
    if created:
        show(created)
        print("    this mailbox has to be deleted by hand: Mail cannot delete one over AppleScript")

    if really_send:
        sent = check(
            "send_email",
            lambda: mail_tools.send_email(
                to=recipient,
                subject=subject + " (sent)",
                body="Message sent by test_manual.py.",
            ),
        )
        if sent:
            show(sent)

        # The point of this one: what leaves has to be the draft itself, not a
        # copy, and Drafts has to be empty afterwards.
        drafted = check(
            "create_draft (to be sent by send_draft)",
            lambda: mail_tools.create_draft(
                to=recipient,
                subject=subject + " (via send_draft)",
                body="Draft written by test_manual.py, then sent as it stands.",
            ),
        )
        if drafted and drafted.get("message_id"):
            result = check(
                "send_draft (takes about 20 s: the draft is watched until it stays gone)",
                lambda: mail_tools.send_draft(drafted["message_id"]),
            )
            if result:
                show(result)
                if not result.get("draft_removed"):
                    print("    the draft came back; remove it from Mail by hand")
        elif drafted:
            print("    no message_id returned, send_draft cannot be exercised")
    else:
        print("\n▶ send_email / send_draft\n  – skipped (pass --send to really send)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["read", "write"])
    parser.add_argument("--account", help="account to test against")
    parser.add_argument("--mailbox", help="mailbox path to test against")
    parser.add_argument("--to", help="recipient for the write checks")
    parser.add_argument("--send", action="store_true", help="really send a message")
    arguments = parser.parse_args()

    if arguments.mode == "read":
        run_read_tests(arguments.account, arguments.mailbox)
    else:
        if not arguments.to:
            parser.error("--to is required in write mode")
        run_write_tests(arguments.to, arguments.send, arguments.account)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
