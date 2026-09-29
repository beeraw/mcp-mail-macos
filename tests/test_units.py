"""Unit tests for everything that does not need Mail.

Run from the project root:

    python3 -m unittest discover -s tests -t .

Nothing here launches osascript or touches ~/Library/Mail, so these run on any
machine and in CI. The live path is covered by test_manual.py instead.
"""

from __future__ import annotations

import email
import email.policy
import json
import os
import random
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import mail_draft
import mail_eval
import mail_files
import mail_imap
import mail_index
import mail_message
import mail_search
import mail_signature
import mail_tools
from mail_tools import MailError, MessageReference


class MessageReferenceTests(unittest.TestCase):
    def test_round_trip(self):
        reference = MessageReference(account="Work", mailbox="[Gmail]/All Mail", identifier=115250)
        decoded = MessageReference.decode(reference.encode())
        self.assertEqual(decoded, reference)

    def test_survives_accents_and_slashes(self):
        reference = MessageReference(account="Perso", mailbox="Crèche/Suivi", identifier=1)
        self.assertEqual(MessageReference.decode(reference.encode()).mailbox, "Crèche/Suivi")

    def test_token_is_url_safe(self):
        token = MessageReference(account="A", mailbox="B", identifier=2).encode()
        self.assertNotIn("=", token)
        self.assertNotIn("/", token)
        self.assertNotIn("+", token)

    def test_malformed_token_is_reported(self):
        for token in ("", "not-base64", "!!!!"):
            with self.subTest(token=token):
                with self.assertRaises(MailError) as caught:
                    MessageReference.decode(token)
                self.assertEqual(caught.exception.code, "invalid_message_id")


class AddressParsingTests(unittest.TestCase):
    def test_accepts_string_list_and_none(self):
        self.assertEqual(mail_tools._as_address_list("a@b.fr"), ["a@b.fr"])
        self.assertEqual(mail_tools._as_address_list(["a@b.fr", "c@d.fr"]), ["a@b.fr", "c@d.fr"])
        self.assertEqual(mail_tools._as_address_list(None), [])

    def test_splits_on_comma_and_semicolon(self):
        self.assertEqual(
            mail_tools._as_address_list("a@b.fr, c@d.fr; e@f.fr"),
            ["a@b.fr", "c@d.fr", "e@f.fr"],
        )

    def test_drops_empty_fragments(self):
        self.assertEqual(mail_tools._as_address_list("a@b.fr,,  ,c@d.fr"), ["a@b.fr", "c@d.fr"])


    def test_header_addresses_survive_an_address_used_as_display_name(self):
        self.assertEqual(
            mail_draft._addresses_of(
                "Rose <r@example.org>, alice@example.org <alice@example.org>, B <b@c.fr>",
                "Sam <s@example.com>",
            ),
            ["r@example.org", "alice@example.org", "b@c.fr", "s@example.com"],
        )

    def test_header_addresses_keep_a_comma_inside_a_quoted_name(self):
        self.assertEqual(
            mail_draft._addresses_of('"Doe, Jane" <j@x.fr>, a@b.fr'),
            ["j@x.fr", "a@b.fr"],
        )

class RecordParsingTests(unittest.TestCase):
    def test_splits_records_and_fields(self):
        raw = "a\x1fb\x1e" + "c\x1fd"
        self.assertEqual(mail_tools._parse_records(raw), [["a", "b"], ["c", "d"]])

    def test_empty_input_yields_no_record(self):
        self.assertEqual(mail_tools._parse_records(""), [])

    def test_missing_field_falls_back(self):
        self.assertEqual(mail_tools._field(["a"], 3, "fallback"), "fallback")

    def test_scalar_helpers(self):
        self.assertTrue(mail_tools._as_bool("true"))
        self.assertFalse(mail_tools._as_bool("false"))
        self.assertEqual(mail_tools._as_int("12"), 12)
        self.assertEqual(mail_tools._as_int("not a number", 7), 7)


class ErrorClassificationTests(unittest.TestCase):
    def test_recognises_own_errors_and_keeps_the_detail(self):
        error = mail_tools._classify_error("MAILERR:mailbox_not_found:Drafts (account Work) (-1728)")
        self.assertEqual(error.code, "mailbox_not_found")
        self.assertIn("Drafts (account Work)", error.message)
        self.assertNotIn("-1728", error.message)
        self.assertIsNotNone(error.hint)

    def test_maps_applescript_numbers(self):
        cases = {
            "execution error: Not authorized to send Apple events (-1743)": "permission_denied",
            "execution error: AppleEvent timed out. (-1712)": "apple_event_timeout",
            "execution error: Application isn't running. (-600)": "mail_not_running",
        }
        for stderr, expected in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(mail_tools._classify_error(stderr).code, expected)

    def test_unknown_failure_is_still_structured(self):
        error = mail_tools._classify_error("something unexpected")
        self.assertEqual(error.code, "applescript_error")
        self.assertEqual(error.to_dict()["ok"], False)


class ScriptAssemblyTests(unittest.TestCase):
    def test_run_handler_is_wrapped_in_a_timeout(self):
        script = mail_tools._build_script("list_mailboxes", apple_event_timeout=42)
        self.assertIn("on mainRun(argv)", script)
        self.assertIn("with timeout of 42 seconds", script)
        self.assertIn("return mainRun(argv)", script)
        # The original entry point must not survive, or the wrapper never runs.
        self.assertNotIn("\non run argv\n\tset ", script)

    def test_shared_handlers_are_prepended(self):
        script = mail_tools._build_script("get_message", apple_event_timeout=10)
        self.assertIn("on resolveMailbox(", script)
        self.assertIn("on toIso(", script)

    def test_every_script_assembles(self):
        directory = mail_tools.SCRIPT_DIRECTORY
        names = [
            name[: -len(".applescript")]
            for name in os.listdir(directory)
            if name.endswith(".applescript") and not name.startswith("_")
        ]
        # A floor, not a count: it guards against an empty or unreadable
        # directory, and must not have to move every time a script goes away.
        self.assertGreaterEqual(len(names), 8)
        for name in names:
            with self.subTest(script=name):
                self.assertIn("with timeout of", mail_tools._build_script(name, 30))


class FlagColorTests(unittest.TestCase):
    def test_rejects_unknown_colour_before_touching_mail(self):
        reference = MessageReference("A", "B", 1).encode()
        with self.assertRaises(MailError) as caught:
            mail_tools.flag_message(reference, "turquoise")
        self.assertEqual(caught.exception.code, "invalid_flag_color")

    def test_known_colours_map_to_indexes(self):
        self.assertEqual(mail_tools.FLAG_COLORS["red"], 0)
        self.assertEqual(mail_tools.FLAG_COLORS["grey"], mail_tools.FLAG_COLORS["gray"])


class AttachmentCheckTests(unittest.TestCase):
    def test_missing_file_is_refused(self):
        with self.assertRaises(MailError) as caught:
            mail_tools._check_attachments(["/nowhere/at/all.pdf"])
        self.assertEqual(caught.exception.code, "attachment_not_found")

    def test_existing_file_becomes_absolute(self):
        with tempfile.TemporaryDirectory() as workspace:
            path = os.path.join(workspace, "note.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            self.assertEqual(mail_tools._check_attachments([path]), [os.path.abspath(path)])


class SearchHelperTests(unittest.TestCase):
    def test_dates_accept_several_formats(self):
        self.assertIsNotNone(mail_search._as_timestamp("2026-01-31"))
        self.assertIsNotNone(mail_search._as_timestamp("31/01/2026"))
        self.assertIsNone(mail_search._as_timestamp(None))

    def test_end_of_day_is_later_than_start(self):
        start = mail_search._as_timestamp("2026-01-31")
        end = mail_search._as_timestamp("2026-01-31", end_of_day=True)
        self.assertGreater(end, start)

    def test_unreadable_date_is_reported(self):
        with self.assertRaises(MailError) as caught:
            mail_search._as_timestamp("le 31 janvier")
        self.assertEqual(caught.exception.code, "invalid_date")

    def test_punctuated_query_is_quoted_term_by_term(self):
        self.assertEqual(mail_search._quote_terms("invoice 12/2025"), '"invoice" AND "12/2025"')

    def test_reference_points_at_the_smallest_mailbox(self):
        locations = [
            {"account": "Work", "mailbox": "[Gmail]/Tous les messages"},
            {"account": "Work", "mailbox": "Invoices"},
        ]
        sizes = {("Work", "[Gmail]/Tous les messages"): 30821, ("Work", "Invoices"): 2529}
        chosen = mail_search._pick_location(locations, sizes)
        self.assertEqual(chosen["mailbox"], "Invoices")

    def test_bulk_mailbox_is_the_last_resort(self):
        locations = [{"account": "Work", "mailbox": "[Gmail]/Tous les messages"}]
        self.assertIsNotNone(mail_search._pick_location(locations, {}))
        self.assertIsNone(mail_search._pick_location([], {}))


class MailboxUrlTests(unittest.TestCase):
    def test_decodes_percent_encoding_and_recomposes_accents(self):
        match = mail_index.MAILBOX_URL.match(
            "imap://ACCOUNT-UUID/%5BGmail%5D/Messages%20envoye%CC%81s"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group("account"), "ACCOUNT-UUID")

    def test_every_scheme_is_recognised(self):
        for url in ("imap://a/b", "ews://a/b", "local://a/b", "pop://a/b"):
            with self.subTest(url=url):
                self.assertIsNotNone(mail_index.MAILBOX_URL.match(url))


class SlugTests(unittest.TestCase):
    def test_strips_accents_and_punctuation(self):
        self.assertEqual(mail_files._slug("Décompte 03 — lot 12"), "decompte-03-lot-12")

    def test_falls_back_when_nothing_survives(self):
        self.assertEqual(mail_files._slug("!!! ???"), "no-subject")

    def test_is_bounded(self):
        self.assertLessEqual(len(mail_files._slug("x" * 200)), 48)


class DraftFileTests(unittest.TestCase):
    """The .eml workflow, end to end, without Mail."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.folder = self.workspace.name
        self.attachment = os.path.join(self.folder, "note.txt")
        with open(self.attachment, "w", encoding="utf-8") as handle:
            handle.write("attached content")
        # The builder asks Mail for its accounts and reads the signature from
        # ~/Library/Mail; neither is available here, so both are answered.
        patch = _Patch(self)
        account = {"name": "Work", "id": "ACCOUNT-UUID", "type": "imap", "addresses": ["me@example.com"]}
        patch(mail_draft, "accounts", lambda: [account])
        patch(mail_signature, "signature_for_account", lambda account_id: None)
        # make_msgid() looks the host name up, which can hang for half a
        # minute on a runner without reverse DNS.
        patch(socket, "getfqdn", lambda name="": "example.com")

    def tearDown(self):
        self.workspace.cleanup()

    def _write(self, **overrides):
        arguments = {
            "to": "someone@example.com",
            "subject": "Invoice 2026-04",
            "body": "Hello,\n\nHere it is.",
            "folder": self.folder,
        }
        arguments.update(overrides)
        return mail_files.write_draft(**arguments)

    def test_written_draft_reads_back_identically(self):
        written = self._write(cc="boss@example.com", attachments=[self.attachment])
        reread = mail_files.read_draft_file(written["path"])
        self.assertEqual(reread["subject"], "Invoice 2026-04")
        self.assertEqual(reread["to"], ["someone@example.com"])
        self.assertEqual(reread["cc"], ["boss@example.com"])
        self.assertIn("Here it is.", reread["body"])
        self.assertEqual([a["name"] for a in reread["attachments"]], ["note.txt"])

    def test_attachment_payload_survives_the_round_trip(self):
        written = self._write(attachments=[self.attachment])
        with open(written["path"], "rb") as handle:
            message = email.message_from_binary_file(handle, policy=email.policy.default)
        payloads = [
            part.get_payload(decode=True)
            for part in message.walk()
            if part.get_filename() == "note.txt"
        ]
        self.assertEqual(payloads, [b"attached content"])

    def test_recipient_is_required(self):
        with self.assertRaises(MailError) as caught:
            self._write(to="")
        self.assertEqual(caught.exception.code, "no_recipient")

    def test_html_body_is_labelled_html(self):
        written = self._write(body="<p>Hello,</p><ul><li>one</li></ul>")
        with open(written["path"], "rb") as handle:
            message = email.message_from_binary_file(handle, policy=email.policy.default)
        body = message.get_body(preferencelist=("html",))
        self.assertIsNotNone(body, "a HTML body must not be posted as text/plain")
        self.assertIn("<li>one</li>", body.get_content())

    def test_plain_body_is_turned_into_html(self):
        # Every message goes out as HTML, so a plain text body is converted
        # rather than posted as text/plain: blank lines become empty lines and
        # single newlines become breaks.
        written = self._write(body="Hello,\n\nfirst\nsecond\n\nCordialement,")
        with open(written["path"], "rb") as handle:
            message = email.message_from_binary_file(handle, policy=email.policy.default)
        html = message.get_body(preferencelist=("html",))
        self.assertIsNotNone(html, "a plain body must still be posted as HTML")
        content = html.get_content()
        self.assertIn("<div>Hello,</div><div><br></div><div>first", content)
        self.assertIn("first<br>second", content)
        # And a plain text alternative stays there for a reader without HTML.
        plain = message.get_body(preferencelist=("plain",))
        self.assertIsNotNone(plain)
        self.assertIn("first\nsecond", plain.get_content())

    def test_markup_in_a_plain_body_is_escaped(self):
        # "<3" must reach the reader as written, not as a broken tag.
        written = self._write(body="Merci <3 & bonne journee")
        with open(written["path"], "rb") as handle:
            message = email.message_from_binary_file(handle, policy=email.policy.default)
        content = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("&lt;3", content)
        self.assertIn("&amp;", content)

    def test_body_opens_the_message_with_no_break_before_it(self):
        # The stray break Mail used to insert above the body is what this
        # guards against: the document opens on the first paragraph.
        written = self._write(body="Bonjour,")
        with open(written["path"], "rb") as handle:
            message = email.message_from_binary_file(handle, policy=email.policy.default)
        content = message.get_body(preferencelist=("html",)).get_content()
        self.assertRegex(content, r"<body>\s*<div>Bonjour,</div>")

    def test_listing_reports_what_is_waiting(self):
        self._write(subject="First")
        self._write(subject="Second")
        listed = mail_files.list_drafts(self.folder)
        self.assertEqual(listed["waiting"], 2)
        self.assertIn("retention", listed)

    def test_sending_without_confirmation_sends_nothing(self):
        written = self._write()
        answer = mail_files.send_draft_file(written["path"], confirm=False)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error_code"], "confirmation_required")
        self.assertEqual(answer["preview"]["subject"], "Invoice 2026-04")
        self.assertTrue(os.path.isfile(written["path"]))

    def test_discard_removes_the_file(self):
        written = self._write()
        mail_files.discard_draft_file(written["path"])
        self.assertFalse(os.path.exists(written["path"]))

    def test_unknown_path_is_reported(self):
        with self.assertRaises(MailError) as caught:
            mail_files.read_draft_file(os.path.join(self.folder, "nope.eml"))
        self.assertEqual(caught.exception.code, "draft_file_not_found")

    def test_filenames_do_not_collide(self):
        first = self._write(subject="Same subject")
        second = self._write(subject="Same subject")
        self.assertNotEqual(first["file"], second["file"])


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.folder = self.workspace.name

    def tearDown(self):
        self.workspace.cleanup()

    def _aged_file(self, name: str, days: float) -> str:
        path = os.path.join(self.folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("From: a@b.fr\n\nbody\n")
        old = time.time() - days * 86400
        os.utime(path, (old, old))
        return path

    def test_old_pending_draft_is_removed_and_recent_one_kept(self):
        old = self._aged_file("old.eml", days=10)
        fresh = self._aged_file("fresh.eml", days=1)
        result = mail_files.purge_drafts(self.folder)
        self.assertIn("old.eml", result["removed"])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_archive_is_kept_longer_than_pending(self):
        archived = self._aged_file(os.path.join("sent", "archived.eml"), days=10)
        mail_files.purge_drafts(self.folder)
        self.assertTrue(os.path.exists(archived))
        mail_files.purge_drafts(self.folder, archive_days=5)
        self.assertFalse(os.path.exists(archived))

    def test_non_eml_files_are_never_touched(self):
        keep = self._aged_file("notes.txt", days=999)
        mail_files.purge_drafts(self.folder)
        self.assertTrue(os.path.exists(keep))

    def test_missing_folder_is_reported(self):
        with self.assertRaises(MailError) as caught:
            mail_files.purge_drafts(os.path.join(self.folder, "absent"))
        self.assertEqual(caught.exception.code, "folder_not_found")


class StoredMessageTests(unittest.TestCase):
    """Reading Mail's .emlx container and classifying its parts."""

    def _emlx(self, folder: str, identifier: int, message: email.message.EmailMessage) -> str:
        raw = message.as_bytes()
        path = os.path.join(folder, f"{identifier}.emlx")
        with open(path, "wb") as handle:
            handle.write(str(len(raw)).encode("ascii") + b"\n")
            handle.write(raw)
            handle.write(b"<?xml version='1.0'?><plist></plist>")
        return path

    def test_reads_the_payload_back_out(self):
        with tempfile.TemporaryDirectory() as workspace:
            message = email.message.EmailMessage()
            message["Subject"] = "Stored"
            message.set_content("body text")
            path = self._emlx(workspace, 42, message)
            raw = mail_index.read_raw_message(path)
            self.assertIn(b"Stored", raw)
            self.assertNotIn(b"plist", raw)

    def test_a_broken_container_returns_nothing(self):
        with tempfile.TemporaryDirectory() as workspace:
            path = os.path.join(workspace, "broken.emlx")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("not a byte count\n")
            self.assertIsNone(mail_index.read_raw_message(path))


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.original_file = config.CONFIG_FILE
        config.CONFIG_FILE = os.path.join(self.workspace.name, "config.json")
        config.reload()

    def tearDown(self):
        config.CONFIG_FILE = self.original_file
        config.reload()
        os.environ.pop("MAIL_MCP_PENDING_RETENTION_DAYS", None)
        os.environ.pop("MAIL_MCP_DRAFTS_FOLDER", None)
        self.workspace.cleanup()

    def _write_config(self, values):
        import json

        with open(config.CONFIG_FILE, "w", encoding="utf-8") as handle:
            json.dump(values, handle)
        config.reload()

    def test_defaults_apply_with_no_file(self):
        self.assertEqual(config.get("pending_retention_days"), 7)
        self.assertEqual(config.describe()["pending_retention_days"]["source"], "default")

    def test_file_overrides_the_default(self):
        self._write_config({"pending_retention_days": 3})
        self.assertEqual(config.get("pending_retention_days"), 3)
        self.assertEqual(config.describe()["pending_retention_days"]["source"], "config.json")

    def test_environment_wins_over_the_file(self):
        self._write_config({"pending_retention_days": 3})
        os.environ["MAIL_MCP_PENDING_RETENTION_DAYS"] = "1"
        self.assertEqual(config.get("pending_retention_days"), 1)
        self.assertEqual(config.describe()["pending_retention_days"]["source"], "environment")

    def test_paths_expand_the_tilde(self):
        os.environ["MAIL_MCP_DRAFTS_FOLDER"] = "~/Somewhere"
        self.assertFalse(config.get("drafts_folder").startswith("~"))

    def test_a_malformed_file_falls_back_instead_of_raising(self):
        with open(config.CONFIG_FILE, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        config.reload()
        self.assertEqual(config.get("pending_retention_days"), 7)

    def test_unusable_number_keeps_the_default(self):
        os.environ["MAIL_MCP_PENDING_RETENTION_DAYS"] = "soon"
        self.assertEqual(config.get("pending_retention_days"), 7)

    def test_example_file_only_uses_known_keys(self):
        import json

        with open(os.path.join(config.PROJECT_ROOT, "config.example.json"), encoding="utf-8") as handle:
            example = json.load(handle)
        unknown = set(example) - set(config.DEFAULTS) - {"_comment"}
        self.assertEqual(unknown, set())


class ConfirmationGuardTests(unittest.TestCase):
    def test_preview_carries_what_would_be_sent(self):
        answer = mail_tools.send_email(
            to="a@b.fr, c@d.fr",
            subject="Nothing leaves",
            body="Body.",
            cc="e@f.fr",
        )
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error_code"], "confirmation_required")
        preview = answer["preview"]
        self.assertEqual(preview["to"], ["a@b.fr", "c@d.fr"])
        self.assertEqual(preview["cc"], ["e@f.fr"])
        self.assertEqual(preview["subject"], "Nothing leaves")

    def test_a_send_without_recipient_is_refused_before_the_guard(self):
        with self.assertRaises(MailError) as caught:
            mail_tools.send_email(to="", subject="x", body="y")
        self.assertEqual(caught.exception.code, "no_recipient")


class MessageLayoutTests(unittest.TestCase):
    """The order asked for: message, signature, blank line, attachments."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.attachment = os.path.join(self.workspace.name, "note.txt")
        with open(self.attachment, "w", encoding="utf-8") as handle:
            handle.write("attached content")
        self.signature = mail_signature.Signature(
            identifier="SIG-1",
            name="Work",
            html='<span>Jane Doe</span><img src="cid:LOGO-1">',
            images=[
                mail_signature.InlineImage(
                    content_id="LOGO-1",
                    maintype="image",
                    subtype="png",
                    filename="logo.png",
                    payload=b"\x89PNG fake",
                )
            ],
        )

    def tearDown(self):
        self.workspace.cleanup()

    def _build(self, **overrides):
        arguments = {
            "to": ["a@b.fr"],
            "subject": "Layout",
            "body": "Bonjour,",
            "signature": self.signature,
        }
        arguments.update(overrides)
        return mail_message.build_message(**arguments)

    def test_signature_follows_the_body(self):
        html = mail_message.compose_html("Bonjour,", self.signature)
        self.assertLess(
            html.index("Bonjour,"),
            html.index("AppleMailSignature"),
            "the signature must come after the message, never before",
        )

    def test_one_empty_line_separates_the_body_from_the_signature(self):
        # Trailing newlines in the body and the break Mail stores at the top of
        # a signature used to stack up into two or three empty lines.
        signature = mail_signature.Signature(
            identifier="SIG-2",
            name="Work",
            html="<span><br>Jane Doe<br>Example Ltd</span>",
            images=[],
        )
        html = mail_message.compose_html("Bonjour,\n\nCordialement\n\n\n", signature)
        self.assertIn(
            '<div>Cordialement</div><br><div id="AppleMailSignature"><span>Jane Doe',
            html,
        )

    def test_a_html_body_loses_its_trailing_blank_lines(self):
        html = mail_message.compose_html(
            "<p>Cordialement<br></p><p><br></p>&nbsp;<br>", self.signature
        )
        self.assertIn('<p>Cordialement</p><br><div id="AppleMailSignature">', html)

    def test_a_blank_line_closes_the_signature(self):
        # What separates the signature from the attachments underneath.
        html = mail_message.compose_html("Bonjour,", self.signature)
        self.assertTrue(html.endswith("<br></body></html>"))

    def test_attachments_come_after_everything(self):
        message = self._build(attachment_paths=[self.attachment])
        self.assertEqual(message.get_content_type(), "multipart/mixed")
        parts = message.get_payload()
        self.assertEqual(parts[0].get_content_type(), "multipart/related")
        self.assertEqual(parts[-1].get_filename(), "note.txt")

    def test_the_signature_logo_is_not_an_attachment(self):
        # It travels inside the body, by content id, so the reader is not shown
        # a file they never received.
        message = self._build(attachment_paths=[self.attachment])
        related = message.get_payload()[0]
        logo = related.get_payload()[-1]
        self.assertEqual(logo.get("Content-Id"), "<LOGO-1>")
        self.assertIn("inline", logo.get("Content-Disposition", ""))

    def test_a_message_without_attachment_needs_no_mixed_wrapper(self):
        self.assertEqual(self._build().get_content_type(), "multipart/related")

    def test_the_body_is_not_wrapped_in_a_quote(self):
        # Mail wrapped anything it composed in a cite blockquote; nothing here
        # may reintroduce one.
        html = mail_message.compose_html("Bonjour,", self.signature)
        self.assertNotIn("blockquote", html)


class ReplyTests(unittest.TestCase):
    """Threading, recipients and quoting, without touching Mail."""

    def test_a_message_id_is_bracketed_for_the_header(self):
        self.assertEqual(mail_draft._bracketed("abc@x.fr"), "<abc@x.fr>")
        self.assertEqual(mail_draft._bracketed("<abc@x.fr>"), "<abc@x.fr>")
        self.assertEqual(mail_draft._bracketed(""), "")

    def test_a_comma_in_a_display_name_does_not_split_a_recipient(self):
        self.assertEqual(
            mail_draft._addresses_of('"Doe, Jane" <j@x.fr>, a@b.fr'),
            ["j@x.fr", "a@b.fr"],
        )

    def test_the_same_address_is_kept_once(self):
        self.assertEqual(
            mail_draft._addresses_of("a@b.fr, A@B.FR", "c@d.fr"), ["a@b.fr", "c@d.fr"]
        )

    def test_a_subject_already_answering_keeps_one_prefix(self):
        for subject in ("Re: Devis", "RE: Devis", "Ré : Devis", "re:Devis"):
            with self.subTest(subject=subject):
                self.assertTrue(mail_draft._ALREADY_A_REPLY.match(subject))

    def test_a_fresh_subject_is_not_mistaken_for_an_answer(self):
        for subject in ("Devis", "Rebond commercial", "Retard de livraison"):
            with self.subTest(subject=subject):
                self.assertIsNone(mail_draft._ALREADY_A_REPLY.match(subject))

    def test_the_chain_of_references_is_read_from_the_headers(self):
        headers = (
            "From: a@b.fr\n"
            "References: <one@x.fr>\n <two@x.fr>\n"
            "Subject: Devis\n"
        )
        found = mail_draft._REFERENCES.search(headers)
        self.assertIsNotNone(found)
        self.assertEqual(found.group(1).split(), ["<one@x.fr>", "<two@x.fr>"])

    def test_an_unreadable_date_is_left_as_it_came(self):
        self.assertEqual(mail_draft._readable_date("pas une date"), "pas une date")


class ReplyExtraRecipientsTests(unittest.TestCase):
    """add_to, add_cc and bcc on a reply, with Mail, IMAP and SMTP stubbed."""

    ORIGINAL = {
        "account": "Work",
        "subject": "Quote",
        "sender": "Alice <alice@example.org>",
        "reply_to": "",
        "to": "me@example.com, Bob <bob@example.org>",
        "cc": "carol@example.org",
        "body": "Hello",
        "date_received": "",
        "rfc_message_id": "orig@example.org",
        "headers": "",
    }

    def setUp(self):
        patch = _Patch(self)
        account = {"name": "Work", "id": "ACCOUNT-UUID", "type": "imap", "addresses": ["me@example.com"]}
        patch(mail_draft, "accounts", lambda: [account])
        patch(mail_signature, "signature_for_account", lambda account_id: None)
        patch(socket, "getfqdn", lambda name="": "example.com")
        patch(mail_tools, "get_message", lambda message_id, max_body_chars=0: dict(self.ORIGINAL))
        self.sent = []
        self.drafts = []
        patch(
            mail_imap,
            "send_message",
            lambda account, raw, envelope: self.sent.append((raw, envelope)) or {"server": "smtp.example.com"},
        )
        patch(mail_imap, "sent_copy_fields", lambda delivered: {})
        patch(mail_imap, "append_draft", lambda account, raw: self.drafts.append(raw) or {"folder": "Drafts"})

    def _recipients(self, reply_all=True, **extra):
        return mail_draft.reply_recipients(dict(self.ORIGINAL), reply_all, **extra)

    def test_nothing_added_leaves_the_computed_recipients_alone(self):
        found = self._recipients()
        self.assertEqual(found["to"], ["alice@example.org"])
        self.assertEqual(found["cc"], ["bob@example.org", "carol@example.org"])
        self.assertEqual(found["bcc"], [])
        self.assertEqual(found["added"], [])

    def test_add_cc_works_when_answering_the_sender_alone(self):
        found = self._recipients(reply_all=False, add_cc="dave@example.org")
        self.assertEqual(found["cc"], ["dave@example.org"])
        self.assertEqual(found["added"], ["dave@example.org"])

    def test_a_display_name_and_a_bare_address_are_the_same_recipient(self):
        found = self._recipients(add_to=["Alice B <ALICE@example.org>"], add_cc=["Carol <carol@EXAMPLE.org>"])
        self.assertEqual(found["to"], ["alice@example.org"])
        self.assertEqual(found["cc"], ["bob@example.org", "carol@example.org"])
        self.assertEqual(found["added"], [])

    def test_an_address_added_to_to_moves_up_from_cc(self):
        found = self._recipients(add_to="carol@example.org")
        self.assertEqual(found["to"], ["alice@example.org", "carol@example.org"])
        self.assertEqual(found["cc"], ["bob@example.org"])

    def test_an_address_added_to_cc_already_in_to_stays_in_to(self):
        found = self._recipients(add_cc="alice@example.org")
        self.assertNotIn("alice@example.org", found["cc"])

    def test_bcc_never_repeats_a_visible_recipient(self):
        found = self._recipients(bcc="bob@example.org; dave@example.org, DAVE@example.org")
        self.assertEqual(found["bcc"], ["dave@example.org"])

    def test_an_own_address_is_kept_when_explicitly_asked_for(self):
        found = self._recipients(add_cc="me@example.com")
        self.assertIn("me@example.com", found["cc"])

    def test_an_invalid_address_is_refused(self):
        for parameter in ("add_to", "add_cc", "bcc"):
            with self.subTest(parameter=parameter):
                with self.assertRaises(MailError) as caught:
                    self._recipients(**{parameter: ["not an address"]})
                self.assertEqual(caught.exception.code, "invalid_address")

    def test_the_preview_shows_added_and_blind_recipients(self):
        preview = mail_tools.reply_to_message(
            "id", "Thanks", add_to="dave@example.org", bcc="erin@example.org"
        )
        self.assertEqual(preview["error_code"], "confirmation_required")
        shown = preview["preview"]
        self.assertIn("dave@example.org", shown["will_go_to"])
        self.assertEqual(shown["blind_copied_to"], "erin@example.org")
        self.assertEqual(shown["added"], ["dave@example.org", "erin@example.org"])
        self.assertEqual(self.sent, [])

    def test_a_sent_reply_puts_bcc_in_the_envelope_but_not_in_the_header(self):
        result = mail_draft.reply("id", "Thanks", bcc="erin@example.org", add_cc="dave@example.org")
        raw, envelope = self.sent[0]
        self.assertIn("erin@example.org", envelope)
        self.assertIn("dave@example.org", envelope)
        parsed = email.message_from_bytes(raw)
        self.assertIsNone(parsed["Bcc"])
        self.assertNotIn(b"erin@example.org", raw)
        self.assertEqual(result["added"], ["dave@example.org", "erin@example.org"])

    def test_a_draft_keeps_the_bcc_header(self):
        mail_draft.reply("id", "Thanks", as_draft=True, bcc="erin@example.org")
        self.assertEqual(email.message_from_bytes(self.drafts[0])["Bcc"], "erin@example.org")
        self.assertEqual(self.sent, [])


class PartClassificationTests(unittest.TestCase):
    """What the reader actually receives, told apart from what only decorates."""

    def _message(self, image_headers: str, html: str = "") -> bytes:
        """A message whose image part carries exactly the given headers."""
        raw = (
            "MIME-Version: 1.0\n"
            'Content-Type: multipart/mixed; boundary="B"\n\n'
            "--B\n"
            "Content-Type: text/html; charset=utf-8\n\n"
            f"{html}\n"
            "--B\n"
            f"{image_headers.strip()}\n\n"
            "fake\n"
            "--B\n"
            "Content-Type: text/plain\n"
            'Content-Disposition: attachment; filename="note.txt"\n\n'
            "content\n"
            "--B--\n"
        )
        return raw.encode("utf-8")

    def test_an_inline_part_is_not_announced_as_a_file(self):
        sent, inline = mail_message.classify_parts(
            self._message(
                "Content-Type: image/png\n"
                "Content-Id: <LOGO-1>\n"
                'Content-Disposition: inline; filename="logo.png"'
            )
        )
        self.assertEqual(sent, ["note.txt"])
        self.assertEqual(inline, ["logo.png"])

    def test_a_part_the_body_displays_belongs_to_the_body(self):
        # No disposition at all: what settles it is the HTML showing it.
        sent, inline = mail_message.classify_parts(
            self._message(
                'Content-Type: image/png; name="logo.png"\nContent-Id: <LOGO-1>',
                '<img src="cid:LOGO-1">',
            )
        )
        self.assertEqual(inline, ["logo.png"])
        self.assertEqual(sent, ["note.txt"])

    def test_a_message_the_server_built_classifies_the_same_way(self):
        signature = mail_signature.Signature(
            identifier="S", name="Work", html='<img src="cid:LOGO-1">',
            images=[mail_signature.InlineImage("LOGO-1", "image", "png", "logo.png", b"x")],
        )
        with tempfile.TemporaryDirectory() as workspace:
            path = os.path.join(workspace, "note.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")
            built = mail_message.build_message(
                to=["a@b.fr"], subject="S", body="Bonjour,",
                attachment_paths=[path], signature=signature,
            )
        sent, inline = mail_message.classify_parts(built.as_bytes())
        self.assertEqual(sent, ["note.txt"])
        self.assertEqual(inline, ["logo.png"])


class SignatureReadingTests(unittest.TestCase):
    def test_apple_object_becomes_an_image(self):
        # Apple writes its inline images with a tag only Mail can render.
        rewritten = mail_signature._rewrite_apple_objects(
            '<SPAN><OBJECT height=50 width=64 '
            'type=application/x-apple-msg-attachment data="cid:LOGO-1"></OBJECT></SPAN>'
        )
        self.assertIn('<img src="cid:LOGO-1"', rewritten)
        self.assertIn('width="64"', rewritten)
        self.assertNotIn("<OBJECT", rewritten.upper())

    def test_string_attachment_class_is_removed(self):
        # Left in place, Mail deletes the element when the draft is opened.
        html = mail_signature._drop_string_attachment_class(
            '<span class="Apple-string-attachment" style="font-size: 13px;">Jane</span>'
            "<SPAN class=Apple-string-attachment><img src=\"cid:LOGO-1\"></SPAN>"
        )
        self.assertNotIn("apple-string-attachment", html.lower())
        self.assertIn('<span style="font-size: 13px;">Jane</span>', html)
        self.assertIn('<SPAN><img src="cid:LOGO-1"></SPAN>', html)

    def test_an_object_that_is_not_an_image_is_dropped(self):
        self.assertEqual(
            mail_signature._rewrite_apple_objects('<object data="http://x/y"></object>'), ""
        )


class FolderNameTests(unittest.TestCase):
    def test_imap_utf7_is_decoded_for_reading(self):
        self.assertEqual(
            mail_imap.decode_folder("[Gmail]/Messages envoy&AOk-s"),
            "[Gmail]/Messages envoyés",
        )

    def test_a_plain_name_is_left_alone(self):
        self.assertEqual(mail_imap.decode_folder("[Gmail]/Brouillons"), "[Gmail]/Brouillons")

    def test_a_literal_ampersand_survives(self):
        self.assertEqual(mail_imap.decode_folder("Black &- White"), "Black & White")


class _Patch:
    """Replaces module attributes for one test, and puts them back after."""

    def __init__(self, test: unittest.TestCase):
        self.test = test

    def __call__(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.test.addCleanup(setattr, module, name, original)


def _account(host: str) -> mail_imap.Account:
    server = mail_imap.Server(host=host, port=465, ssl_enabled=True, user="me@example.com")
    return mail_imap.Account(name="Work", identifier="ACCOUNT-UUID", incoming=server, outgoing=server)


class SentCopyTests(unittest.TestCase):
    def setUp(self):
        self.patch = _Patch(self)
        self.patch(mail_imap, "SENT_COPY_WAIT", 0.0)
        self.patch(mail_imap, "SENT_COPY_POLL", 0.0)
        self.appended = []
        self.patch(mail_imap, "append_sent", lambda account, raw: self.appended.append(raw) or "Sent")

    def test_gmail_copy_found_is_not_filed_twice(self):
        self.patch(mail_imap, "find_in_sent", lambda account, mid: "[Gmail]/Messages envoyés")
        copy = mail_imap.ensure_sent_copy(_account("smtp.gmail.com"), b"raw", "<a@b>")
        self.assertEqual(copy, {"verified": True, "folder": "[Gmail]/Messages envoyés", "filed_here": False})
        self.assertEqual(self.appended, [])

    def test_gmail_copy_missing_is_filed_here(self):
        found = iter([None, "[Gmail]/Messages envoyés"])
        self.patch(mail_imap, "find_in_sent", lambda account, mid: next(found))
        copy = mail_imap.ensure_sent_copy(_account("smtp.gmail.com"), b"raw", "<a@b>")
        self.assertTrue(copy["verified"])
        self.assertTrue(copy["filed_here"])
        self.assertEqual(self.appended, [b"raw"])

    def test_a_copy_that_cannot_be_found_is_reported(self):
        self.patch(mail_imap, "find_in_sent", lambda account, mid: None)
        copy = mail_imap.ensure_sent_copy(_account("mail.other.fr"), b"raw", "<a@b>")
        self.assertFalse(copy["verified"])
        self.assertIn("sent_copy_warning", mail_imap.sent_copy_fields({"sent_copy": copy}))


class SendDraftTests(unittest.TestCase):
    DRAFT = (
        b"From: Me <me@example.com>\r\nTo: you@x.fr\r\nBcc: hidden@x.fr\r\nSubject: Hello\r\n"
        b"Message-ID: <draft-id@example.com>\r\nDate: Mon, 28 Sep 2026 10:00:00 +0300\r\n"
        b"X-Uniform-Type-Identifier: com.apple.mail-draft\r\n\r\nBody.\r\n"
    )

    def setUp(self):
        self.patch = _Patch(self)
        row = mail_tools.FIELD_SEPARATOR.join(
            ["Hello", "Me <me@example.com>", "you@x.fr", "", "hidden@x.fr", "", "[Gmail]/Brouillons", "Work", "Body.", "<draft-id@example.com>"]
        )
        self.patch(mail_tools, "run_script", lambda *args, **kwargs: row)
        self.patch(mail_imap, "fetch_draft", lambda account, mid: self.DRAFT)
        self.sent = []
        self.deleted = []
        self.patch(mail_imap, "delete_draft", lambda account, mid: self.deleted.append(mid) or True)
        self.reference = MessageReference(account="Work", mailbox="[Gmail]/Brouillons", identifier=1).encode()

    def _send(self, verified: bool):
        def send_message(account, raw, envelope):
            self.sent.append((raw, envelope))
            return {"account": account, "server": "smtp", "sent_copy": {"verified": verified, "folder": "Sent", "filed_here": False}}

        self.patch(mail_imap, "send_message", send_message)
        return mail_tools.send_draft(self.reference, confirm=True)

    def test_the_draft_leaves_under_a_new_message_id(self):
        self._send(verified=True)
        raw, envelope = self.sent[0]
        message = email.message_from_bytes(raw)
        self.assertNotEqual(message["Message-ID"], "<draft-id@example.com>")
        self.assertTrue(message["Message-ID"].endswith("@example.com>"))
        self.assertIsNone(message["X-Uniform-Type-Identifier"])
        self.assertIsNone(message["Bcc"])
        self.assertIn("hidden@x.fr", envelope)

    def test_the_draft_is_removed_once_the_copy_is_confirmed(self):
        answer = self._send(verified=True)
        self.assertTrue(answer["draft_removed"])
        self.assertEqual(self.deleted, ["<draft-id@example.com>"])

    def test_the_draft_is_kept_when_no_copy_is_confirmed(self):
        answer = self._send(verified=False)
        self.assertFalse(answer["draft_removed"])
        self.assertEqual(self.deleted, [])
        self.assertIn("sent_copy_warning", answer)


class _FictionalIndexMixin:
    """Throwaway FTS5 index of fictional messages; not collected by itself."""

    DAY = 86400

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "index.sqlite")
        index = mail_index.open_index(self.path)
        now = int(time.time())
        # id, subject, sender, recipients, attachments, body, age in days
        rows = [
            (1, "Weekly notes", "jane@example.com", "", "", "the roadmap is discussed here", 1),
            (2, "Roadmap review", "john@example.org", "", "", "see you there", 400),
            (3, "Kickoff", "jane@example.com", "", "", "roadmap", 2000),
            (4, "Shared plan", "jane@example.com", "", "", "quarterly budget figures", 500),
            (5, "Shared plan", "jane@example.com", "", "", "quarterly budget figures", 3),
            (6, "Shared plan", "jane@example.com", "", "", "quarterly budget figures", None),
        ]
        for identifier, subject, sender, recipients, attachments, body, age in rows:
            index.execute(
                "INSERT INTO messages (id, account, subject, sender, date_received, indexed_at)"
                " VALUES (?, 'Work', ?, ?, ?, ?)",
                (identifier, subject, sender, None if age is None else now - age * self.DAY, now),
            )
            index.execute(
                "INSERT INTO messages_fts (rowid, subject, sender, recipients, attachments, body)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (identifier, subject, sender, recipients, attachments, body),
            )
            index.execute(
                "INSERT INTO locations (message, account, mailbox, read, flagged)"
                " VALUES (?, 'Work', 'INBOX', 1, 0)",
                (identifier,),
            )
        index.commit()
        index.close()
        patcher = mock.patch.object(mail_search, "INDEX_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Ranking tests must not look into the real mail store for snippets.
        store = mock.patch.object(mail_index, "find_store", side_effect=FileNotFoundError)
        store.start()
        self.addCleanup(store.stop)
        self.addCleanup(self.directory.cleanup)

    def ids(self, query, **kwargs):
        result = mail_search.search_all(query, max_age_minutes=10**9, **kwargs)
        return [message["mail_id"] for message in result["messages"]]


class SearchRankingTests(_FictionalIndexMixin, unittest.TestCase):
    """search_all ordering."""

    def test_a_subject_hit_outranks_a_body_only_hit(self):
        # Mail 1 is far more recent, but only mentions the word in its body.
        self.assertEqual(self.ids("roadmap")[0], 2)

    def test_a_strong_old_match_beats_a_weak_recent_one(self):
        ranked = self.ids("roadmap")
        self.assertLess(ranked.index(2), ranked.index(1))

    def test_date_sort_keeps_newest_first(self):
        self.assertEqual(self.ids("roadmap", sort="date"), [1, 2, 3])

    def test_relevance_is_the_default_and_is_reported(self):
        result = mail_search.search_all("roadmap", max_age_minutes=10**9)
        self.assertEqual(result["sort"], "relevance")

    def test_an_unknown_sort_is_refused(self):
        with self.assertRaises(MailError) as caught:
            mail_search.search_all("roadmap", sort="best", max_age_minutes=10**9)
        self.assertEqual(caught.exception.code, "invalid_sort")

    def test_recency_breaks_ties_between_equal_matches(self):
        self.assertEqual(self.ids("budget"), [5, 4, 6])

    def test_a_null_dated_match_does_not_rank_first(self):
        self.assertNotEqual(self.ids("budget")[0], 6)

    def test_ranking_survives_the_quoted_terms_retry(self):
        result = mail_search.search_all("plan 12/2025", max_age_minutes=10**9)
        self.assertIn("interpreted_as", result)
        self.assertEqual(result["messages"], [])
        self.assertEqual(self.ids("shared/plan"), [5, 4, 6])


class SnippetTests(unittest.TestCase):
    FILLER = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor. "

    def test_a_term_in_the_middle_is_centred_within_the_length(self):
        text = self.FILLER * 6 + "The budget review is on Friday. " + self.FILLER * 6
        snippet = mail_index.make_snippet(text, "budget")
        self.assertIn("budget", snippet)
        self.assertTrue(snippet.startswith("…") or snippet.startswith("The"))
        self.assertTrue(snippet.endswith("…"))
        self.assertLessEqual(len(snippet), 202)

    def test_a_term_at_the_start_has_no_leading_ellipsis(self):
        snippet = mail_index.make_snippet("Budget " + self.FILLER * 6, "budget")
        self.assertTrue(snippet.startswith("Budget"))
        self.assertTrue(snippet.endswith("…"))

    def test_matching_ignores_case_and_accents(self):
        text = self.FILLER * 5 + "Le compte rendu de la réunion arrive. " + self.FILLER * 5
        self.assertIn("réunion", mail_index.make_snippet(text, "REUNION"))
        self.assertIn("réunion", mail_index.make_snippet(text, "réunion"))

    def test_a_prefix_query_matches_a_longer_word(self):
        text = self.FILLER * 5 + "Invoices are attached. " + self.FILLER * 5
        self.assertIn("Invoices", mail_index.make_snippet(text, "invoic*"))

    def test_an_exact_term_does_not_match_inside_a_longer_word(self):
        text = "Notes on invoicing. " + self.FILLER * 5 + "The invoice is late. " + self.FILLER * 5
        self.assertIn("invoice is late", mail_index.make_snippet(text, "invoice"))

    def test_a_phrase_is_located_as_a_whole(self):
        text = (
            "The plan is fine. " + self.FILLER * 5 + "Please confirm the delivery date today. "
            + self.FILLER * 5
        )
        first = mail_index.make_snippet(text, '"delivery date"')
        self.assertIn("delivery date", first)
        self.assertNotIn("The plan is fine", first)

    def test_operators_and_column_filters_are_ignored(self):
        text = self.FILLER * 5 + "The kickoff is set. " + self.FILLER * 5
        query = 'subject:kickoff AND NOT spam OR NEAR(alpha beta, 3)'
        self.assertIn("kickoff", mail_index.make_snippet(text, query))
        self.assertEqual(mail_index.query_terms("AND OR NOT"), [])
        self.assertEqual(mail_index.query_terms("a NOT b"), [(["a"], False)])

    def test_no_hit_returns_the_start_of_the_body(self):
        snippet = mail_index.make_snippet(self.FILLER * 6, "absent")
        self.assertTrue(snippet.startswith("Lorem ipsum"))
        self.assertTrue(snippet.endswith("…"))

    def test_whitespace_is_collapsed(self):
        self.assertEqual(mail_index.make_snippet("one \n\n  two\t three", "two"), "one two three")

    def test_cuts_fall_on_word_boundaries(self):
        words = " ".join(f"word{number}" for number in range(200))
        snippet = mail_index.make_snippet(words, "word100").strip("…")
        for piece in snippet.split(" "):
            self.assertRegex(piece, r"^word\d+$")

    def test_an_empty_body_has_no_snippet(self):
        self.assertIsNone(mail_index.make_snippet("  \n ", "anything"))


class MessageFileLookupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = os.path.join(self.directory.name, "V10")
        mail_index._root_cache.clear()
        self.addCleanup(mail_index._root_cache.clear)

    def place(self, mailbox, identifier, partial=False, body="Hello there, budget news.\r\n"):
        shard = mail_index.shard_candidates(identifier)[0]
        folder = os.path.join(
            self.store, "ACCOUNT-UUID", mailbox + ".mbox", "STORE-UUID", "Data", shard, "Messages"
        )
        os.makedirs(folder, exist_ok=True)
        raw = ("Subject: Test\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" + body).encode()
        path = os.path.join(folder, f"{identifier}{'.partial' if partial else ''}.emlx")
        with open(path, "wb") as handle:
            handle.write(str(len(raw)).encode() + b"\n" + raw + b"<plist></plist>")
        return path

    def test_the_shard_comes_from_the_id_digits(self):
        self.assertEqual(mail_index.shard_candidates(117939), ["7/1/1"])
        self.assertEqual(mail_index.shard_candidates(5231), ["5"])
        self.assertEqual(mail_index.shard_candidates(42), [""])

    def test_a_file_is_found_under_any_mailbox_and_nesting(self):
        path = self.place("Inbox", 117939)
        other = self.place("Projects/Alpha", 20481)
        self.assertEqual(mail_index.find_message_file(self.store, 117939), path)
        self.assertEqual(mail_index.find_message_file(self.store, 20481), other)

    def test_a_partial_file_is_found(self):
        path = self.place("Inbox", 9500, partial=True)
        self.assertEqual(mail_index.find_message_file(self.store, 9500), path)

    def test_a_missing_file_gives_none(self):
        self.place("Inbox", 117939)
        self.assertIsNone(mail_index.find_message_file(self.store, 555555))

    def test_a_mailbox_created_later_is_found_after_the_refresh_delay(self):
        self.place("Inbox", 117939)
        mail_index.find_message_file(self.store, 117939)
        late = self.place("Later", 118940)
        self.assertIsNone(mail_index.find_message_file(self.store, 118940))
        stamp, roots = mail_index._root_cache[self.store]
        mail_index._root_cache[self.store] = (stamp - 120, roots)
        self.assertEqual(mail_index.find_message_file(self.store, 118940), late)

    def test_message_snippet_reads_the_file(self):
        self.place("Inbox", 117939)
        self.assertIn("budget", mail_index.message_snippet(self.store, 117939, "budget"))
        self.assertIsNone(mail_index.message_snippet(self.store, 999999, "budget"))


class SearchSnippetTests(_FictionalIndexMixin, unittest.TestCase):
    """search_all with snippets, against a fictional store on disk."""

    def setUp(self):
        super().setUp()
        self.store_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.store_directory.cleanup)
        store = os.path.join(self.store_directory.name, "V10")
        mail_index._root_cache.clear()
        self.addCleanup(mail_index._root_cache.clear)
        folder = os.path.join(store, "ACCOUNT", "Inbox.mbox", "STORE", "Data", "Messages")
        os.makedirs(folder)
        raw = b"Subject: x\r\nContent-Type: text/plain\r\n\r\nThe roadmap is discussed here.\r\n"
        with open(os.path.join(folder, "1.emlx"), "wb") as handle:
            handle.write(str(len(raw)).encode() + b"\n" + raw)
        self.find_store = mock.patch.object(mail_index, "find_store", return_value=store)
        self.find_store.start()
        self.addCleanup(self.find_store.stop)

    def test_results_carry_a_snippet_and_a_missing_file_gives_null(self):
        result = mail_search.search_all("roadmap", max_age_minutes=10**9)
        by_id = {message["mail_id"]: message for message in result["messages"]}
        self.assertIn("roadmap is discussed", by_id[1]["snippet"])
        self.assertIsNone(by_id[2]["snippet"])

    def test_snippets_can_be_switched_off(self):
        result = mail_search.search_all("roadmap", max_age_minutes=10**9, snippets=False)
        self.assertTrue(all("snippet" not in message for message in result["messages"]))

    def test_get_thread_returns_the_conversation(self):
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE messages SET conversation_id = 7 WHERE id IN (1, 2)")
        connection.commit()
        connection.close()
        reference = MessageReference("Work", "INBOX", 1).encode()
        result = mail_search.get_thread(reference)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["messages"]), 2)

    def test_an_oversized_file_gets_no_snippet(self):
        with mock.patch.object(mail_index, "SNIPPET_MAX_FILE_BYTES", 10):
            result = mail_search.search_all("roadmap", max_age_minutes=10**9)
        self.assertIsNone(
            {m["mail_id"]: m for m in result["messages"]}[1]["snippet"]
        )

    def test_a_failing_snippet_never_breaks_the_search(self):
        with mock.patch.object(mail_index, "message_snippet", side_effect=RuntimeError("boom")):
            result = mail_search.search_all("roadmap", max_age_minutes=10**9)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["messages"][0]["snippet"])


class SearchEvaluationTests(unittest.TestCase):
    def test_reply_prefixes_stopwords_and_short_tokens_are_dropped(self):
        words = mail_eval.usable_words("RE: TR: Fwd: The quarterly budget for 2026 and near it")
        self.assertEqual(words, ["quarterly", "budget"])

    def test_french_stopwords_and_accents(self):
        words = mail_eval.usable_words("Réunion pour les travaux dans la cuisine")
        self.assertEqual(words, ["réunion", "travaux", "cuisine"])

    def test_duplicates_are_removed(self):
        self.assertEqual(mail_eval.usable_words("budget Budget budget review"), ["budget", "review"])

    def test_a_subject_with_too_few_words_gives_no_query(self):
        rng = random.Random(1)
        self.assertIsNone(mail_eval.derive_query("Re: ok merci", rng))
        self.assertIsNone(mail_eval.derive_query("Budget", rng))
        self.assertIsNone(mail_eval.derive_query("", rng))

    def test_the_query_uses_two_to_four_subject_words_in_order(self):
        subject = "alpha bravo charlie delta echo foxtrot"
        for seed in range(20):
            query = mail_eval.derive_query(subject, random.Random(seed))
            words = query.split()
            self.assertTrue(2 <= len(words) <= 4)
            self.assertEqual(words, sorted(words, key=subject.split().index))

    def test_generation_is_reproducible_and_skips_unusable_subjects(self):
        candidates = [(1, "Re: ok"), (2, "printer maintenance schedule"), (3, "budget review meeting")]
        first = mail_eval.generate_pairs(candidates, 5, random.Random(7))
        second = mail_eval.generate_pairs(candidates, 5, random.Random(7))
        self.assertEqual(first, second)
        self.assertEqual(sorted(pair["expected"] for pair in first), [2, 3])
        self.assertTrue(all(pair["source"] == "auto" for pair in first))

    def test_identical_queries_are_kept_once(self):
        candidates = [(1, "printer maintenance"), (2, "Re: printer maintenance")]
        pairs = mail_eval.generate_pairs(candidates, 5, random.Random(1))
        self.assertEqual(len(pairs), 1)

    def test_merge_keeps_manual_pairs_and_replaces_auto_ones(self):
        existing = [
            {"query": "old", "expected": 1, "source": "auto"},
            {"query": "mine", "expected": 2, "source": "manual"},
        ]
        generated = [{"query": "new", "expected": 3, "source": "auto"}]
        merged = mail_eval.merge_pairs(existing, generated)
        self.assertEqual([pair["query"] for pair in merged], ["mine", "new"])

    def test_rank_of_handles_one_or_several_expected_ids(self):
        self.assertEqual(mail_eval.rank_of(5, [9, 5, 7]), 2)
        self.assertEqual(mail_eval.rank_of([7, 5], [9, 5, 7]), 2)
        self.assertIsNone(mail_eval.rank_of(4, [9, 5, 7]))

    def test_aggregate_metrics(self):
        result = mail_eval.aggregate([1, 2, None, 11])
        self.assertEqual(result["pairs"], 4)
        self.assertEqual(result["not_found"], 1)
        self.assertAlmostEqual(result["mrr"], (1 + 0.5 + 1 / 11) / 4, places=4)
        self.assertEqual(result["recall_at_1"], 0.25)
        self.assertEqual(result["recall_at_10"], 0.5)

    def test_aggregate_of_nothing_is_zero(self):
        self.assertEqual(mail_eval.aggregate([])["pairs"], 0)

    def test_compare_reports_deltas_and_pairs_that_got_worse(self):
        def result(ranks):
            rows = [{"query": f"q{i}", "expected": i, "rank": rank} for i, rank in enumerate(ranks)]
            return {"aggregate": mail_eval.aggregate(ranks), "pairs": rows}

        comparison = mail_eval.compare(result([1, 2, 5, None]), result([1, 4, 3, None]))
        self.assertEqual([row["query"] for row in comparison["worse"]], ["q1"])
        self.assertEqual((comparison["worse"][0]["before"], comparison["worse"][0]["after"]), (2, 4))
        self.assertLess(comparison["deltas"]["recall_at_10"], 1)
        lost = mail_eval.compare(result([3]), result([None]))
        self.assertEqual(len(lost["worse"]), 1)
        self.assertEqual(lost["deltas"]["not_found"], 1)

    def test_pairs_file_round_trip_and_read_only_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "sub", "pairs.json")
            pairs = [{"query": "printer maintenance", "expected": [1, 2], "source": "manual"}]
            mail_eval.save_pairs(path, pairs)
            self.assertEqual(mail_eval.load_pairs(path), (pairs, 0))
            self.assertEqual(mail_eval.load_pairs(os.path.join(directory, "missing.json")), ([], 0))

            database = os.path.join(directory, "index.sqlite")
            import sqlite3
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, subject TEXT)")
            connection.execute("INSERT INTO messages VALUES (1, 'printer maintenance')")
            connection.commit()
            connection.close()
            self.assertEqual(mail_eval.load_candidates(database), [(1, "printer maintenance")])
            reader = mail_eval.open_readonly(database)
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("INSERT INTO messages VALUES (2, 'x')")
            reader.close()

    def test_the_example_file_is_loadable(self):
        example = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval.example.json")
        pairs, skipped = mail_eval.load_pairs(example)
        self.assertEqual((len(pairs), skipped), (4, 0))

    def test_malformed_pairs_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pairs.json")
            rows = [
                {"query": "printer maintenance", "expected": 1},
                {"query": "printer maintenance"},
                {"query": "printer maintenance", "expected": []},
                {"query": "  ", "expected": 3},
                {"query": "printer maintenance", "expected": "abc"},
                {"query": "printer maintenance", "expected": [4, 5]},
                "junk",
            ]
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"pairs": rows}, handle)
            good, skipped = mail_eval.load_pairs(path)
            self.assertEqual([pair["expected"] for pair in good], [1, [4, 5]])
            self.assertEqual(skipped, 5)

    def test_a_corrupt_pairs_file_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pairs.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            with self.assertRaises(mail_eval.EvalError):
                mail_eval.load_pairs(path)

    def test_errors_are_kept_apart_from_not_found(self):
        result = mail_eval.aggregate([1, None], errors=3)
        self.assertEqual((result["not_found"], result["errors"], result["pairs"]), (1, 3, 2))

    def test_compare_shows_the_errors_delta(self):
        before = {"aggregate": mail_eval.aggregate([1], errors=0), "pairs": []}
        after = {"aggregate": mail_eval.aggregate([1], errors=2), "pairs": []}
        self.assertEqual(mail_eval.compare(before, after)["deltas"]["errors"], 2)

    def test_result_names_cannot_escape_the_results_folder(self):
        with self.assertRaises(mail_eval.EvalError):
            mail_eval.result_path("../pairs")


class RefreshLocationsTests(unittest.TestCase):
    """sync() must not leave read, flagged and mailbox frozen at indexing time."""

    def setUp(self):
        self.envelope = sqlite3.connect(":memory:")
        self.envelope.row_factory = sqlite3.Row
        self.envelope.executescript(
            "CREATE TABLE mailboxes (url TEXT);"
            "CREATE TABLE messages (mailbox INTEGER, read INTEGER, flagged INTEGER, deleted INTEGER);"
            "CREATE TABLE labels (message_id INTEGER, mailbox_id INTEGER);"
        )
        self.envelope.executemany(
            "INSERT INTO mailboxes (ROWID, url) VALUES (?, ?)",
            [(1, "imap://AAAA-1111/INBOX"), (2, "imap://AAAA-1111/Archive")],
        )
        self.index = sqlite3.connect(":memory:")
        self.index.row_factory = sqlite3.Row
        self.index.executescript(mail_index.SCHEMA)
        for identifier in (10, 11):
            self.index.execute(
                "INSERT INTO messages (id, account, indexed_at) VALUES (?, 'Work', 0)", (identifier,)
            )
            self.index.execute(
                "INSERT INTO locations VALUES (?, 'Work', 'INBOX', 0, 0)", (identifier,)
            )

    def tearDown(self):
        self.envelope.close()
        self.index.close()

    def add_message(self, identifier, mailbox, read, flagged):
        self.envelope.execute(
            "INSERT INTO messages (ROWID, mailbox, read, flagged, deleted) VALUES (?,?,?,?,0)",
            (identifier, mailbox, read, flagged),
        )

    def locations(self, identifier):
        return {
            row["mailbox"]: (row["read"], row["flagged"])
            for row in self.index.execute("SELECT * FROM locations WHERE message = ?", (identifier,))
        }

    def test_read_and_flag_flips_reach_the_index(self):
        self.add_message(10, 1, 1, 0)
        self.add_message(11, 1, 0, 1)
        self.assertEqual(mail_index.refresh_locations(self.envelope, self.index), (2, 0))
        self.assertEqual(self.locations(10), {"INBOX": (1, 0)})
        self.assertEqual(self.locations(11), {"INBOX": (0, 1)})

    def test_unchanged_rows_are_not_rewritten(self):
        self.add_message(10, 1, 0, 0)
        self.add_message(11, 1, 1, 0)
        before = self.index.total_changes
        self.assertEqual(mail_index.refresh_locations(self.envelope, self.index), (1, 0))
        self.assertEqual(self.index.total_changes - before, 1)
        self.assertEqual(mail_index.refresh_locations(self.envelope, self.index), (0, 0))

    def test_a_moved_message_swaps_its_location(self):
        self.add_message(10, 2, 0, 0)
        self.add_message(11, 1, 0, 0)
        self.assertEqual(mail_index.refresh_locations(self.envelope, self.index), (0, 2))
        self.assertEqual(self.locations(10), {"Archive": (0, 0)})

    def test_a_message_in_two_mailboxes_keeps_both_rows_in_step(self):
        self.add_message(10, 1, 1, 1)
        self.envelope.execute("INSERT INTO labels VALUES (10, 2)")
        self.add_message(11, 1, 0, 0)
        mail_index.refresh_locations(self.envelope, self.index)
        self.assertEqual(self.locations(10), {"INBOX": (1, 1), "Archive": (1, 1)})

    def test_an_empty_mailbox_list_never_deletes(self):
        self.add_message(10, 1, 0, 0)
        self.add_message(11, 1, 0, 0)
        self.envelope.execute("DELETE FROM mailboxes")
        self.assertEqual(mail_index.refresh_locations(self.envelope, self.index), (0, 0))
        self.assertEqual(self.locations(10), {"INBOX": (0, 0)})

    def test_a_message_in_an_unparsable_mailbox_keeps_its_rows(self):
        self.envelope.execute("INSERT INTO mailboxes (ROWID, url) VALUES (3, 'garbage')")
        self.add_message(10, 3, 0, 0)
        self.add_message(11, 1, 0, 0)
        mail_index.refresh_locations(self.envelope, self.index)
        self.assertEqual(self.locations(10), {"INBOX": (0, 0)})

    def test_a_message_with_no_membership_keeps_its_rows(self):
        self.add_message(11, 1, 0, 0)
        self.envelope.execute(
            "INSERT INTO messages (ROWID, mailbox, read, flagged, deleted) VALUES (10, NULL, 0, 0, 0)"
        )
        mail_index.refresh_locations(self.envelope, self.index)
        self.assertEqual(self.locations(10), {"INBOX": (0, 0)})

    def test_a_bulk_move_is_fully_applied(self):
        for identifier in range(100, 400):
            self.index.execute("INSERT INTO messages (id, account, indexed_at) VALUES (?, 'Work', 0)", (identifier,))
            self.index.execute("INSERT INTO locations VALUES (?, 'Work', 'INBOX', 0, 0)", (identifier,))
            self.add_message(identifier, 2, 0, 0)
        self.add_message(10, 1, 0, 0)
        self.add_message(11, 1, 0, 0)
        self.assertEqual(mail_index.refresh_locations(self.envelope, self.index), (0, 600))
        self.assertEqual(self.locations(100), {"Archive": (0, 0)})

    def test_new_rows_use_the_account_of_their_mailbox(self):
        self.envelope.execute("UPDATE mailboxes SET url = 'imap://BBBB-2222/Archive' WHERE ROWID = 2")
        self.index.execute("INSERT INTO locations VALUES (11, 'Home', 'Archive', 0, 0)")
        self.add_message(10, 1, 0, 0)
        self.envelope.execute("INSERT INTO labels VALUES (10, 2)")
        self.add_message(11, 1, 0, 0)
        self.envelope.execute("INSERT INTO labels VALUES (11, 2)")
        mail_index.refresh_locations(self.envelope, self.index)
        row = self.index.execute("SELECT account FROM locations WHERE message = 10 AND mailbox = 'Archive'").fetchone()
        self.assertEqual(row["account"], "Home")


    def test_messages_not_indexed_yet_are_left_to_build(self):
        self.add_message(10, 1, 0, 0)
        self.add_message(11, 1, 0, 0)
        self.add_message(12, 1, 1, 0)
        mail_index.refresh_locations(self.envelope, self.index)
        self.assertEqual(self.locations(12), {})


if __name__ == "__main__":
    unittest.main()
