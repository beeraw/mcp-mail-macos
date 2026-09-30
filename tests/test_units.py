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
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import mail_attachments
import mail_draft
import mail_eval
import mail_files
import mail_imap
import mail_index
import mail_message
import mail_operators
import mail_saved
import mail_search
import mail_stem
import mail_signature
import mail_tools
import mail_vectors
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
                'INSERT INTO messages_fts (rowid, subject, sender, "to", cc, attachments, body)'
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (identifier, subject, sender, recipients, "", attachments, body),
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
        result = mail_search.search_all("plan (", max_age_minutes=10**9)
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


class OperatorParserTests(unittest.TestCase):
    """mail_operators.parse: what is pulled out of a query and what is left."""

    def parse(self, query):
        return mail_operators.parse(query)

    def test_a_query_without_operators_is_returned_untouched(self):
        parsed = self.parse("invoice  12/2025 (a OR b)")
        self.assertEqual(parsed.filters, [])
        self.assertEqual(parsed.free_text, "invoice  12/2025 (a OR b)")

    def test_every_operator_is_recognised(self):
        query = (
            "from:jane to:john cc:example.org has:attachment filename:plan.pdf larger:2M"
            " smaller:10K older_than:1y newer_than:2w after:2026-01-05 before:2026/02/01"
            " is:unread in:inbox"
        )
        parsed = self.parse(query)
        self.assertEqual(parsed.free_text, "")
        self.assertEqual(
            [item.key for item in parsed.filters],
            ["from", "to", "cc", "has", "filename", "larger", "smaller", "older_than",
             "newer_than", "after", "before", "is", "in"],
        )

    def test_keys_are_case_insensitive_and_values_kept(self):
        parsed = self.parse("FROM:Jane IS:Unread")
        self.assertEqual([(i.key, i.value) for i in parsed.filters], [("from", "Jane"), ("is", "unread")])

    def test_a_quoted_value_may_hold_spaces(self):
        parsed = self.parse('from:"Jane Doe" invoice')
        self.assertEqual(parsed.filters[0].value, "Jane Doe")
        self.assertEqual(parsed.free_text, "invoice")

    def test_a_leading_minus_negates(self):
        parsed = self.parse("-is:bulk -from:example.net roadmap")
        self.assertTrue(all(item.negated for item in parsed.filters))
        self.assertEqual(parsed.free_text, "roadmap")

    def test_free_text_keeps_its_fts_syntax(self):
        parsed = self.parse('subject:roadmap from:jane "exact phrase" OR budget*')
        self.assertEqual(parsed.free_text, 'subject:roadmap "exact phrase" OR budget*')

    def test_an_operator_inside_a_quoted_phrase_is_text(self):
        parsed = self.parse('"from:jane" budget')
        self.assertEqual(parsed.filters, [])
        self.assertEqual(parsed.free_text, '"from:jane" budget')

    def test_a_space_after_the_colon_leaves_an_fts_column_filter(self):
        parsed = self.parse("to: jane {to cc}:john cc: example.org")
        self.assertEqual(parsed.filters, [])

    def test_an_unknown_key_stays_free_text(self):
        parsed = self.parse("label:work budget")
        self.assertEqual(parsed.filters, [])
        self.assertEqual(parsed.free_text, "label:work budget")

    def test_a_connector_left_dangling_is_dropped(self):
        self.assertEqual(self.parse("from:jane AND budget").free_text, "budget")
        self.assertEqual(self.parse("budget AND from:jane AND plan").free_text, "budget AND plan")

    def test_not_before_an_operator_negates_it(self):
        parsed = self.parse("NOT from:example.com invoice")
        self.assertEqual([(i.key, i.negated) for i in parsed.filters], [("from", True)])
        self.assertEqual(parsed.free_text, "invoice")
        self.assertEqual(self.parse("invoice NOT is:bulk").free_text, "invoice")
        self.assertTrue(self.parse("invoice NOT is:bulk").filters[0].negated)

    def test_parentheses_around_operators_only_are_dropped(self):
        for query, text in (("(from:a) b", "b"), ("(from:a has:attachment) b", "b"),
                            ("((from:a)) b", "b"), ("invoice AND (from:a)", "invoice")):
            parsed = self.parse(query)
            self.assertEqual(parsed.free_text, text, query)
            self.assertTrue(parsed.filters)
        self.assertEqual(self.parse("(a OR b) from:c").free_text, "(a OR b)")

    def test_or_and_mixed_groups_next_to_an_operator_are_refused(self):
        for query in ("from:a OR from:b", "word OR from:a", "from:a OR word",
                      "(from:a OR from:b) invoice", "(from:a invoice)", "word OR (from:a)",
                      "NOT (from:a)", "NOT -from:a", "-(from:a) b", "NOT NOT from:a"):
            with self.assertRaises(MailError, msg=query) as caught:
                self.parse(query)
            self.assertEqual(caught.exception.code, "invalid_operator")
            self.assertIn("AND", caught.exception.hint)

    def test_parentheses_in_a_quoted_phrase_are_not_groups(self):
        parsed = self.parse('"(x" from:a')
        self.assertEqual(parsed.free_text, '"(x"')

    def test_dates_are_normalised(self):
        self.assertEqual(self.parse("after:2026/01/05").filters[0].value, "2026-01-05")

    def test_from_me_is_normalised(self):
        self.assertEqual(self.parse("to:ME").filters[0].value, "me")

    def test_invalid_values_are_refused_with_a_hint(self):
        for query in (
            "after:yesterday", "before:2026-13-45", "larger:big", "smaller:5X",
            "older_than:3", "newer_than:d", "has:pdf", "is:archived", 'from:""',
        ):
            with self.assertRaises(MailError, msg=query) as caught:
                self.parse(query)
            self.assertEqual(caught.exception.code, "invalid_operator")
            self.assertTrue(caught.exception.hint)

    def test_sizes_and_ages(self):
        self.assertEqual(mail_operators._size_bytes("larger", "2M"), 2 * 1024**2)
        self.assertEqual(mail_operators._size_bytes("larger", "500k"), 500 * 1024)
        self.assertEqual(mail_operators._size_bytes("larger", "1234"), 1234)
        self.assertEqual(mail_operators._age_seconds("older_than", "2w"), 14 * 86400)
        self.assertEqual(mail_operators._age_seconds("older_than", "1y"), 365 * 86400)


class SearchOperatorTests(unittest.TestCase):
    """search_all with operators, on a throwaway index of fictional messages."""

    DAY = 86400

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "index.sqlite")
        index = mail_index.open_index(self.path)
        self.now = int(time.time())
        # id, subject, sender, to, cc, age days, size, attachment, bulk, [(mailbox, read, flagged)]
        rows = [
            (1, "Invoice March", "Jane Doe <jane@example.com>", ["john@example.org"], [], 10, 5000, 1, 0,
             [("INBOX", 0, 0)]),
            (2, "Invoice April", "Example Ltd <billing@example.net>", ["jane@example.com"],
             ["john@example.org"], 400, 3 * 1024**2, 1, 0, [("[Work]/Archive", 1, 1)]),
            (3, "Newsletter invoice", "news@shop.example.com", ["jane@example.com"], [], 5, 900, 0, 1,
             [("INBOX", 1, 0), ("[Gmail]/All Mail", 0, 0)]),
            (4, "Lunch", "john@example.org", ["jane@example.com", "kim@example.net"], ["me@example.com"], 60,
             1200, 0, 0, [("Sent", 1, 0)]),
        ]
        for identifier, subject, sender, to, cc, age, size, attached, bulk, places in rows:
            index.execute(
                "INSERT INTO messages (id, account, subject, sender, date_received, size,"
                " has_attachment, is_bulk, indexed_at) VALUES (?, 'Work', ?, ?, ?, ?, ?, ?, ?)",
                (identifier, subject, sender, self.now - age * self.DAY, size, attached, bulk, self.now),
            )
            index.execute(
                'INSERT INTO messages_fts (rowid, subject, sender, "to", cc, attachments, body,'
                " subject_stem, attachments_stem, body_stem) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, subject, sender, " ".join(to), " ".join(cc),
                 "plan.pdf" if identifier == 1 else ("budget-2026.xlsx" if identifier == 2 else ""),
                 "some words", mail_stem.stem_text(subject),
                 "plan pdf" if identifier == 1 else "", ""),
            )
            for kind, addresses in (("to", to), ("cc", cc)):
                for address in addresses:
                    index.execute(
                        "INSERT INTO recipients (message, kind, address, domain, name)"
                        " VALUES (?,?,?,?,?)",
                        (identifier, kind, address, address.split("@")[1], None),
                    )
            for mailbox, read, flagged in places:
                index.execute(
                    "INSERT INTO locations (message, account, mailbox, read, flagged)"
                    " VALUES (?, 'Work', ?, ?, ?)",
                    (identifier, mailbox, read, flagged),
                )
        index.commit()
        index.close()
        for target, value in (
            (mail_search, {"INDEX_PATH": self.path, "_OWN_ADDRESSES": ["me@example.com"]}),
        ):
            for name, replacement in value.items():
                patcher = mock.patch.object(target, name, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)
        store = mock.patch.object(mail_index, "find_store", side_effect=FileNotFoundError)
        store.start()
        self.addCleanup(store.stop)

    def ids(self, query, **kwargs):
        result = mail_search.search_all(query, max_age_minutes=10**9, snippets=False, **kwargs)
        return sorted(message["mail_id"] for message in result["messages"])

    def test_from_an_address_a_domain_or_a_name(self):
        self.assertEqual(self.ids("from:jane@example.com"), [1])
        self.assertEqual(self.ids("from:@example.com"), [1, 3])
        self.assertEqual(self.ids("from:example.net"), [2])
        self.assertEqual(self.ids("from:jane"), [1])
        self.assertEqual(self.ids('from:"jane doe"'), [1])
        self.assertEqual(self.ids("from:@example.org"), [4])

    def test_a_domain_does_not_match_a_longer_domain(self):
        self.assertEqual(self.ids("from:@ample.com"), [])

    def test_to_and_cc_use_the_recipients_table(self):
        self.assertEqual(self.ids("to:jane@example.com"), [2, 3, 4])
        self.assertEqual(self.ids("to:@example.net"), [4])
        self.assertEqual(self.ids("cc:john"), [2])
        self.assertEqual(self.ids("cc:@example.com"), [4])

    def test_the_to_column_stays_searchable_with_a_space_or_braces(self):
        self.assertEqual(self.ids("to: kim"), [4])
        self.assertEqual(self.ids("{to}:kim"), [4])
        self.assertEqual(self.ids("recipients:kim"), [4])

    def test_me_means_the_addresses_of_the_accounts(self):
        self.assertEqual(self.ids("cc:me"), [4])
        self.assertEqual(self.ids("from:me"), [])
        with mock.patch.object(mail_search, "_OWN_ADDRESSES", []):
            with self.assertRaises(MailError) as caught:
                self.ids("to:me")
        self.assertEqual(caught.exception.code, "no_own_address")

    def test_attachment_size_and_age(self):
        self.assertEqual(self.ids("has:attachment"), [1, 2])
        self.assertEqual(self.ids("-has:attachment"), [3, 4])
        self.assertEqual(self.ids("larger:1M"), [2])
        self.assertEqual(self.ids("smaller:1K"), [3])
        self.assertEqual(self.ids("older_than:1y"), [2])
        self.assertEqual(self.ids("newer_than:1w"), [3])

    def test_dates_include_after_and_exclude_before(self):
        day = time.strftime("%Y-%m-%d", time.localtime(self.now - 60 * self.DAY))
        self.assertIn(4, self.ids(f"after:{day}"))
        self.assertNotIn(4, self.ids(f"before:{day}"))
        self.assertIn(4, self.ids(f"-before:{day}"))

    def test_read_state_flag_and_bulk(self):
        self.assertEqual(self.ids("is:unread"), [1, 3])
        self.assertEqual(self.ids("is:read"), [2, 3, 4])
        self.assertEqual(self.ids("-is:unread"), [2, 4])
        self.assertEqual(self.ids("is:flagged"), [2])
        self.assertEqual(self.ids("is:starred"), [2])
        self.assertEqual(self.ids("is:bulk"), [3])
        self.assertEqual(self.ids("-is:bulk"), [1, 2, 4])

    def test_in_matches_a_mailbox_name_or_its_last_segment(self):
        self.assertEqual(self.ids("in:inbox"), [1, 3])
        self.assertEqual(self.ids("in:archive"), [2])
        self.assertEqual(self.ids('in:"[Work]/Archive"'), [2])
        self.assertEqual(self.ids("in:mail"), [])
        self.assertEqual(self.ids("in:all mail"), [])

    def test_filename_searches_the_attachment_names(self):
        self.assertEqual(self.ids("filename:plan.pdf"), [1])
        self.assertEqual(self.ids("filename:budget"), [2])
        self.assertEqual(self.ids("-filename:plan.pdf invoice"), [2, 3])
        self.assertEqual(self.ids("filename:pdf invoice"), [1])

    def test_operators_combine_with_and_and_with_free_text(self):
        self.assertEqual(self.ids("invoice from:@example.com"), [1, 3])
        self.assertEqual(self.ids("invoice from:@example.com -is:bulk"), [1])
        self.assertEqual(self.ids("from:jane from:example.com"), [1])
        self.assertEqual(self.ids("invoice OR lunch -is:bulk in:inbox"), [1])

    def test_keyword_parameters_still_combine(self):
        self.assertEqual(self.ids("from:@example.com", unread_only=True), [1, 3])
        self.assertEqual(self.ids("has:attachment", flagged_only=True), [2])
        self.assertEqual(self.ids("has:attachment", mailbox="INBOX"), [1])

    def test_an_operator_only_query_comes_back_newest_first(self):
        result = mail_search.search_all("-is:bulk", max_age_minutes=10**9, snippets=False)
        self.assertEqual([m["mail_id"] for m in result["messages"]], [1, 4, 2])
        self.assertEqual(result["sort"], "date")

    def test_not_and_grouped_operators_end_to_end(self):
        self.assertEqual(self.ids("invoice NOT is:bulk"), [1, 2])
        self.assertEqual(self.ids("NOT from:@example.com"), [2, 4])
        self.assertEqual(self.ids("(from:@example.com) invoice"), [1, 3])
        with self.assertRaises(MailError):
            self.ids("from:jane OR from:john")

    def test_the_answer_echoes_the_filters(self):
        result = mail_search.search_all(
            "invoice -is:bulk after:2020/01/02", max_age_minutes=10**9, snippets=False
        )
        self.assertEqual(result["filters"], {"-is": ["bulk"], "after": ["2020-01-02"]})
        self.assertEqual(result["query"], "invoice")
        self.assertEqual(result["original_query"], "invoice -is:bulk after:2020/01/02")
        self.assertEqual(mail_search.search_all("invoice", max_age_minutes=10**9, snippets=False)["filters"], {})

    def test_a_bad_value_is_refused_before_searching(self):
        with self.assertRaises(MailError) as caught:
            self.ids("invoice after:soon")
        self.assertEqual(caught.exception.code, "invalid_operator")

    def test_an_empty_query_is_still_refused(self):
        with self.assertRaises(MailError) as caught:
            self.ids("   ")
        self.assertEqual(caught.exception.code, "empty_query")

    def test_free_text_that_is_not_fts_is_still_retried_quoted(self):
        result = mail_search.search_all("invoice ( from:jane", max_age_minutes=10**9, snippets=False)
        self.assertIn("interpreted_as", result)
        self.assertEqual(result["filters"], {"from": ["jane"]})


class AggregateTests(unittest.TestCase):
    """aggregate on a throwaway index of fictional messages."""

    DAY = 86400

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "index.sqlite")
        index = mail_index.open_index(self.path)
        now = int(time.time())
        self.stamp = lambda year, month, day: int(datetime(year, month, day, 12).timestamp())
        # id, account, subject, sender, timestamp, bulk, [(account, mailbox, read, flagged)]
        rows = [
            (1, "Work", "Invoice one", "Jane Doe <Jane@Example.com>", self.stamp(2025, 1, 10), 0,
             [("Work", "INBOX", 0, 0)]),
            (2, "Work", "Invoice two", "Jane D. <jane@example.com>", self.stamp(2025, 1, 20), 0,
             [("Work", "INBOX", 1, 0), ("Work", "[Gmail]/All Mail", 1, 0)]),
            (3, "Work", "Invoice three", "jane@example.com", self.stamp(2025, 2, 5), 0,
             [("Work", "INBOX", 1, 1)]),
            (4, "Work", "Sale", "Shop <news@shop.example.net>", self.stamp(2025, 2, 6), 1,
             [("Work", "INBOX", 0, 0)]),
            (5, "Home", "Dinner", "John Roe <john@example.org>", self.stamp(2024, 12, 24), 0,
             [("Home", "INBOX", 1, 0)]),
            (6, "Home", "Dinner again", "Jane Doe <jane@example.com>", self.stamp(2026, 3, 1), 0,
             [("Home", "Archive", 0, 0)]),
            (7, "Home", "Undated", "john@example.org", None, 0, [("Home", "INBOX", 1, 0)]),
        ]
        for identifier, acct, subject, sender, moment, bulk, places in rows:
            index.execute(
                "INSERT INTO messages (id, account, subject, sender, date_received, is_bulk, indexed_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (identifier, acct, subject, sender, moment, bulk, now),
            )
            index.execute(
                'INSERT INTO messages_fts (rowid, subject, sender, "to", cc, attachments, body,'
                " subject_stem, attachments_stem, body_stem) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (identifier, subject, sender, "", "", "", "text", mail_stem.stem_text(subject), "", ""),
            )
            for place in places:
                index.execute(
                    "INSERT INTO locations (message, account, mailbox, read, flagged) VALUES (?,?,?,?,?)",
                    (identifier, *place),
                )
        for identifier, kind, address in ((1, "to", "me@example.com"), (1, "cc", "kim@example.net"),
                                          (2, "to", "me@example.com")):
            index.execute(
                "INSERT INTO recipients (message, kind, address, domain) VALUES (?,?,?,?)",
                (identifier, kind, address, address.split("@")[1]),
            )
        index.commit()
        index.close()
        for name, value in {"INDEX_PATH": self.path, "_OWN_ADDRESSES": ["me@example.com"]}.items():
            patcher = mock.patch.object(mail_search, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_aggregate(self, group_by, query="", **kwargs):
        return mail_search.aggregate(group_by, query, max_age_minutes=10**9, **kwargs)

    def counts(self, group_by, query="", **kwargs):
        result = self.run_aggregate(group_by, query, **kwargs)
        return {row["key"]: row["count"] for row in result["results"]}

    def test_sender_folds_case_and_names_and_keeps_the_common_name(self):
        result = self.run_aggregate("sender")
        first = result["results"][0]
        self.assertEqual((first["key"], first["count"]), ("jane@example.com", 4))
        self.assertEqual(first["name"], "Jane Doe")
        self.assertEqual(first["unread"], 2)
        self.assertEqual(result["total"], 7)
        self.assertEqual(result["groups"], 3)

    def test_domain(self):
        self.assertEqual(self.counts("domain"),
                         {"example.com": 4, "example.org": 2, "shop.example.net": 1})

    def test_month_and_year_are_chronological_and_limit_keeps_the_newest(self):
        months = [row["key"] for row in self.run_aggregate("month")["results"]]
        self.assertEqual(months, ["unknown", "2024-12", "2025-01", "2025-02", "2026-03"])
        self.assertEqual(self.counts("month")["2025-01"], 2)
        self.assertEqual([row["key"] for row in self.run_aggregate("month", limit=2)["results"]],
                         ["2025-02", "2026-03"])
        self.assertEqual(self.counts("year"), {"2024": 1, "2025": 4, "2026": 1, "unknown": 1})

    def test_account_and_mailbox(self):
        self.assertEqual(self.counts("account"), {"Work": 4, "Home": 3})
        boxes = self.run_aggregate("mailbox")
        by_key = {(row["account"], row["key"]): row for row in boxes["results"]}
        self.assertEqual(by_key[("Work", "INBOX")]["count"], 4)
        self.assertEqual(by_key[("Work", "[Gmail]/All Mail")]["count"], 1)
        self.assertEqual(by_key[("Work", "INBOX")]["unread"], 2)
        self.assertEqual(boxes["total"], 7)

    def test_mailbox_filter_restricts_the_groups_to_that_mailbox(self):
        result = self.run_aggregate("mailbox", mailbox="INBOX")
        self.assertEqual({row["key"] for row in result["results"]}, {"INBOX"})

    def test_message_in_several_mailboxes_counts_once(self):
        self.assertEqual(self.counts("sender", "from:jane@example.com", account="Work")["jane@example.com"], 3)
        self.assertEqual(self.run_aggregate("account")["total"], 7)

    def test_recipients(self):
        self.assertEqual(self.counts("recipient"), {"me@example.com": 2, "kim@example.net": 1})
        self.assertEqual(self.counts("recipient_domain"), {"example.com": 2, "example.net": 1})

    def test_recipient_unread_counts_a_message_once_per_key(self):
        index = mail_index.open_index(self.path)
        for address in ("me@example.com", "sam@example.com"):
            index.execute(
                "INSERT INTO recipients (message, kind, address, domain) VALUES (1, 'cc', ?, 'example.com')",
                (address,),
            )
        index.commit()
        index.close()
        rows = {row["key"]: row for row in self.run_aggregate("recipient")["results"]}
        self.assertEqual((rows["me@example.com"]["count"], rows["me@example.com"]["unread"]), (2, 1))
        rows = {row["key"]: row for row in self.run_aggregate("recipient_domain")["results"]}
        self.assertEqual((rows["example.com"]["count"], rows["example.com"]["unread"]), (2, 1))

    def test_query_operators_and_filters(self):
        self.assertEqual(self.counts("sender", "-is:bulk", account="Work"), {"jane@example.com": 3})
        self.assertEqual(self.counts("domain", "invoice"), {"example.com": 3})
        self.assertEqual(self.counts("sender", "to:me"), {"jane@example.com": 2})
        self.assertEqual(self.counts("month", since="2025-02-01", until="2025-02-28"), {"2025-02": 2})
        self.assertEqual(self.counts("sender", unread_only=True),
                         {"jane@example.com": 2, "news@shop.example.net": 1})
        self.assertEqual(self.counts("sender", flagged_only=True), {"jane@example.com": 1})
        result = self.run_aggregate("sender", "invoice from:jane")
        self.assertEqual(result["filters"], {"from": ["jane"]})

    def test_order_by_last_date_and_limit(self):
        keys = [row["key"] for row in self.run_aggregate("sender", order="last_date")["results"]]
        self.assertEqual(keys[0], "jane@example.com")
        self.assertEqual(len(self.run_aggregate("sender", limit=1)["results"]), 1)

    def test_empty_result(self):
        result = self.run_aggregate("sender", "nomatchatall")
        self.assertEqual((result["results"], result["total"], result["groups"]), ([], 0, 0))

    def test_invalid_arguments(self):
        with self.assertRaises(MailError) as caught:
            self.run_aggregate("colour")
        self.assertEqual(caught.exception.code, "invalid_group_by")
        with self.assertRaises(MailError):
            self.run_aggregate("sender", order="newest")


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

    def test_own_text_drops_quotes_and_stops_at_reply_headers(self):
        text = (
            "Hello Jane,\nthe scaffolding delivery is planned on Monday.\n"
            "> quoted zeppelin line\n\n"
            "On Tue, 3 Mar 2026 at 10:00, John Roe <john@example.com> wrote:\n"
            "> older cucumber text\nolder unquoted pineapple text\n"
        )
        own = mail_eval.own_text(text)
        self.assertIn("scaffolding", own)
        for absent in ("zeppelin", "cucumber", "pineapple"):
            self.assertNotIn(absent, own)

    def test_own_text_stops_at_other_reply_markers(self):
        for marker in (
            "Le mardi 3 mars 2026 à 10:00, Jane Doe a écrit :",
            "Le mardi 3 mars 2026 à 10:00, Jane Doe <jane@example.com>\na écrit :",
            "De : Jane Doe",
            "From: Jane Doe",
            "-----Original Message-----",
            "-----Message d'origine-----",
            "-- ",
        ):
            own = mail_eval.own_text(f"Kept sentence here.\n{marker}\nhidden zeppelin")
            self.assertEqual(own, "Kept sentence here.", marker)

    def test_own_text_keeps_ordinary_lines_starting_with_le_or_on(self):
        self.assertIn("Le devis arrive demain", mail_eval.own_text("Le devis arrive demain.\nOn verra."))

    def test_body_query_uses_close_distinctive_words_outside_the_subject(self):
        text = (
            "Bonjour, please confirm the scaffolding inspection schedule before Friday. "
            "The invoice reference 12345 covers everything and more text follows here "
            "so that the body is long enough to be considered by the generator."
        )
        for seed in range(20):
            query = mail_eval.derive_body_query(text, "Invoice question", random.Random(seed))
            words = query.split()
            self.assertTrue(2 <= len(words) <= 3)
            self.assertNotIn("invoice", words)
            self.assertTrue(all(len(word) >= 5 and not word.isdigit() for word in words))
            tokens = text.lower().replace(".", " ").replace(",", " ").split()
            positions = [tokens.index(word) for word in words]
            self.assertLessEqual(max(positions) - min(positions), mail_eval.BODY_WINDOW)

    def test_body_query_rejects_common_words_and_short_or_quoted_text(self):
        text = "alpha scaffolding inspection schedule " * 5 + "\n> quoted zeppelin wording here"
        rare = lambda word: word != "scaffolding"  # noqa: E731
        for seed in range(10):
            query = mail_eval.derive_body_query(text, "", random.Random(seed), rare)
            self.assertNotIn("scaffolding", query.split())
            self.assertNotIn("zeppelin", query)
        self.assertIsNone(mail_eval.derive_body_query("too short", "", random.Random(1)))
        self.assertIsNone(mail_eval.derive_body_query("> " + "quoted words only " * 30, "", random.Random(1)))
        self.assertIsNone(mail_eval.derive_body_query(text, "", random.Random(1), lambda word: False))

    def test_body_pairs_are_tagged_seedable_and_skip_missing_files(self):
        texts = {
            1: "scaffolding inspection schedule confirmed " * 6,
            2: None,
            3: "tiny",
            4: "plumbing radiator installation planned " * 6,
        }
        candidates = [(identifier, "") for identifier in texts]
        first = mail_eval.generate_body_pairs(candidates, 5, random.Random(3), texts.get)
        second = mail_eval.generate_body_pairs(candidates, 5, random.Random(3), texts.get)
        self.assertEqual(first, second)
        self.assertEqual(sorted(pair["expected"] for pair in first), [1, 4])
        self.assertTrue(all(pair["kind"] == "body" and pair["source"] == "auto" for pair in first))

    def test_subject_pairs_are_tagged_subject(self):
        pairs = mail_eval.generate_pairs([(1, "printer maintenance")], 1, random.Random(1))
        self.assertEqual(pairs[0]["kind"], "subject")

    def test_aggregates_per_kind_treat_a_missing_kind_as_subject(self):
        rows = [
            {"query": "a", "expected": 1, "rank": 1},
            {"query": "b", "expected": 2, "rank": None, "kind": "body"},
            {"query": "c", "expected": 3, "rank": 2, "kind": "body"},
        ]
        by_kind = mail_eval.aggregate_by_kind(rows)
        self.assertEqual(by_kind["subject"]["pairs"], 1)
        self.assertEqual(by_kind["body"]["pairs"], 2)
        self.assertEqual(by_kind["body"]["not_found"], 1)
        self.assertEqual(by_kind["all"]["pairs"], 3)

    def test_compare_works_with_an_old_result_without_kinds(self):
        old = {"aggregate": mail_eval.aggregate([1, 2]), "pairs": [
            {"query": "a", "expected": 1, "rank": 1}, {"query": "b", "expected": 2, "rank": 2}]}
        rows = [
            {"query": "a", "expected": 1, "rank": 2, "kind": "subject"},
            {"query": "b", "expected": 2, "rank": 2, "kind": "subject"},
            {"query": "c", "expected": 3, "rank": 1, "kind": "body"},
        ]
        new = {"aggregate": mail_eval.aggregate([2, 2, 1]), "by_kind": mail_eval.aggregate_by_kind(rows), "pairs": rows}
        comparison = mail_eval.compare(old, new)
        self.assertIn("subject", comparison["by_kind"])
        self.assertNotIn("body", comparison["by_kind"])
        self.assertLess(comparison["by_kind"]["subject"]["mrr"], 0)
        self.assertIn("compared with", mail_eval.format_comparison("old", comparison))

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



class BodyExtractionTests(unittest.TestCase):
    """extract_text: plain part first, HTML when the plain part says nothing."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def emlx(self, mime: str) -> str:
        raw = mime.replace("\n", "\r\n").encode()
        path = os.path.join(self.directory.name, f"{len(os.listdir(self.directory.name))}.emlx")
        with open(path, "wb") as handle:
            handle.write(str(len(raw)).encode() + b"\n" + raw)
        return path

    def alternative(self, plain: str, html: str) -> str:
        return self.emlx(
            "Message-ID: <a1@example.com>\n"
            "Subject: Example\n"
            "MIME-Version: 1.0\n"
            'Content-Type: multipart/alternative; boundary="B"\n\n'
            "--B\nContent-Type: text/plain; charset=utf-8\n\n" + plain + "\n"
            "--B\nContent-Type: text/html; charset=utf-8\n\n" + html + "\n--B--\n"
        )

    def test_plain_part_wins_when_it_has_content(self):
        path = self.alternative(
            "The quarterly figures are attached for review.", "<p>Different html wording</p>"
        )
        self.assertEqual(
            mail_index.extract_text(path),
            ("a1@example.com", "The quarterly figures are attached for review."),
        )

    def test_empty_plain_falls_back_to_html(self):
        path = self.alternative("", "<html><body><p>Meeting moved to <b>Friday</b></p></body></html>")
        self.assertEqual(mail_index.extract_text(path)[1], "Meeting moved to Friday")

    def test_whitespace_only_plain_falls_back_to_html(self):
        path = self.alternative("   ", "<p>Invoice reminder for Example Ltd</p>")
        self.assertEqual(mail_index.extract_text(path)[1], "Invoice reminder for Example Ltd")

    def test_placeholder_plain_falls_back_to_html(self):
        path = self.alternative(
            "This message contains HTML. Please use an HTML-capable client.",
            "<p>The delivery slot is booked for Monday morning.</p>",
        )
        self.assertEqual(
            mail_index.extract_text(path)[1], "The delivery slot is booked for Monday morning."
        )

    def test_short_genuine_plain_is_kept_when_html_adds_nothing(self):
        path = self.alternative("OK, thanks", "<p>OK</p>")
        self.assertEqual(mail_index.extract_text(path)[1], "OK, thanks")

    def test_empty_plain_and_empty_html_gives_no_body(self):
        path = self.alternative("", "<p> </p>")
        self.assertFalse(mail_index.has_body(mail_index.extract_text(path)[1]))

    def test_html_only_message_is_unchanged(self):
        path = self.emlx(
            "Message-ID: <h1@example.com>\nContent-Type: text/html; charset=utf-8\n\n<p>Hello Jane</p>\n"
        )
        self.assertEqual(mail_index.extract_text(path)[1], "Hello Jane")

    def test_legacy_flag_tells_what_the_old_logic_missed(self):
        path = self.alternative("", "<p>Meeting moved to Friday</p>")
        _, body, legacy_had_body = mail_index._extract(path)
        self.assertTrue(mail_index.has_body(body))
        self.assertFalse(legacy_had_body)

    def test_unreadable_file_has_no_body(self):
        _, body, legacy = mail_index._extract(os.path.join(self.directory.name, "missing.emlx"))
        self.assertEqual((body, legacy), ("", False))

    def test_has_body_threshold(self):
        self.assertFalse(mail_index.has_body(" \n -- "))
        self.assertTrue(mail_index.has_body("Yes"))


class SchemaVersionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "index.sqlite")

    def test_new_index_is_stamped(self):
        index = mail_index.open_index(self.path)
        self.assertEqual(mail_index.read_schema_version(index), mail_index.SCHEMA_VERSION)
        index.close()
        mail_index.open_index(self.path).close()  # reopening the same version is fine

    def test_index_without_stamp_is_version_one_and_refused(self):
        legacy = sqlite3.connect(self.path)
        legacy.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        )
        legacy.close()
        with self.assertRaises(mail_index.IndexSchemaError) as caught:
            mail_index.open_index(self.path)
        self.assertEqual(caught.exception.found, 1)

    def test_search_refuses_an_outdated_index_with_rebuild_hint(self):
        legacy = sqlite3.connect(self.path)
        legacy.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY)")
        legacy.close()
        with mock.patch.object(mail_search, "INDEX_PATH", self.path):
            with self.assertRaises(MailError) as caught:
                mail_search.search_all("anything", max_age_minutes=10**9)
            self.assertEqual(caught.exception.code, "index_outdated")
            self.assertIn("--build", caught.exception.hint)
            with self.assertRaises(MailError) as caught:
                mail_search.sync_index()
            self.assertEqual(caught.exception.code, "index_outdated")

    def test_swap_in_replaces_file_and_drops_stale_wal(self):
        for suffix, content in (("", b"old"), ("-wal", b"w"), ("-shm", b"s"), (".building", b"new")):
            with open(self.path + suffix, "wb") as handle:
                handle.write(content)
        mail_index.swap_in(self.path + ".building", self.path)
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b"new")
        self.assertFalse(os.path.exists(self.path + "-wal"))
        self.assertFalse(os.path.exists(self.path + "-shm"))
        self.assertFalse(os.path.exists(self.path + ".building"))


class MetadataV3Tests(unittest.TestCase):
    """To/Cc split, List-Id, attachments and the schema refusal."""

    def envelope(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE addresses (address TEXT, comment TEXT);"
            "CREATE TABLE recipients (message INTEGER, address INTEGER, type INTEGER, position INTEGER);"
            "CREATE TABLE attachments (message INTEGER, attachment_id TEXT, name TEXT);"
        )
        connection.executemany(
            "INSERT INTO addresses (ROWID, address, comment) VALUES (?, ?, ?)",
            [(1, "Jane@Example.com", "Jane Doe"), (2, "john@example.org", ""), (3, "team@example.net", "Team")],
        )
        connection.executemany(
            "INSERT INTO recipients (message, address, type, position) VALUES (?, ?, ?, ?)",
            [(10, 2, 1, 0), (10, 1, 0, 0), (10, 3, 1, 1), (11, 2, 0, 0), (12, 1, 2, 0)],
        )
        connection.executemany(
            "INSERT INTO attachments (message, attachment_id, name) VALUES (?, ?, ?)",
            [(10, "1.2", "plan.pdf"), (10, "1.3", "logo.png"), (11, "1.2", None)],
        )
        return connection

    def test_recipient_types_are_split_and_normalized(self):
        found = mail_index.recipients_by_message(self.envelope())
        self.assertEqual(
            found[10],
            [
                ("to", "jane@example.com", "Jane Doe"),
                ("cc", "john@example.org", ""),
                ("cc", "team@example.net", "Team"),
            ],
        )
        self.assertEqual(mail_index.recipient_text(found[10], "to"), "Jane Doe jane@example.com")
        self.assertEqual(
            mail_index.recipient_text(found[10], "cc"), "john@example.org Team team@example.net"
        )
        self.assertNotIn(12, found)  # unknown type: skipped, not misfiled

    def test_address_domain(self):
        self.assertEqual(mail_index.address_domain("jane@example.com"), "example.com")
        self.assertEqual(mail_index.address_domain("undisclosed"), "")

    def test_list_id_is_normalized(self):
        normalize = mail_index.normalize_list_id
        self.assertEqual(normalize("Example News <News.Example.COM>"), "news.example.com")
        self.assertEqual(normalize("<list.example.org>"), "list.example.org")
        self.assertEqual(normalize("bare.example.org"), "bare.example.org")
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize("  "), "")

    def test_has_attachment_comes_from_the_envelope(self):
        # message 10 has a real pdf next to a logo; 11 has only an unnamed part
        self.assertEqual(mail_index.attachment_flags(self.envelope()), {10})

    def test_inline_logos_do_not_count_as_attachments(self):
        connection = self.envelope()
        connection.execute("DELETE FROM attachments")
        names = {
            20: ["Logo.PNG", "sig.gif", "banner.svg", "scan.bmp"],
            21: ["image001.jpg", "Outlook-abc.jpg", "ATT00001.jpg"],
            22: ["logo.png", "holiday.JPG"],
            23: ["photo.heic", "notes.txt"],
        }
        connection.executemany(
            "INSERT INTO attachments (message, attachment_id, name) VALUES (?, '1', ?)",
            [(message, name) for message, values in names.items() for name in values],
        )
        self.assertEqual(mail_index.attachment_flags(connection), {22, 23})

    def test_recipients_column_filter_is_rewritten_to_to_and_cc(self):
        rewrite = mail_search._legacy_column_filters
        self.assertEqual(rewrite("recipients: jane"), "{to cc}: jane")
        self.assertEqual(rewrite("plan AND Recipients:jane"), "plan AND {to cc}:jane")
        self.assertEqual(rewrite("cc: jane"), "cc: jane")
        self.assertEqual(rewrite('"recipients: jane"'), '"recipients: jane"')

    def test_message_headers_mark_bulk_mail(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        def emlx(headers: str) -> str:
            raw = (headers + "Subject: Hi\n\nBody text here\n").replace("\n", "\r\n").encode()
            path = os.path.join(directory.name, f"{len(os.listdir(directory.name))}.emlx")
            with open(path, "wb") as handle:
                handle.write(str(len(raw)).encode() + b"\n" + raw)
            return path

        listed = mail_index.extract_message(emlx("List-Id: Weekly <Weekly.Example.org>\n"))
        self.assertEqual((listed.list_id, listed.unsubscribe), ("weekly.example.org", False))
        unsub = mail_index.extract_message(emlx("List-Unsubscribe: <mailto:u@example.org>\n"))
        self.assertEqual((unsub.list_id, unsub.unsubscribe), ("", True))
        plain = mail_index.extract_message(emlx(""))
        self.assertEqual((plain.list_id, plain.unsubscribe), ("", False))
        self.assertEqual(mail_index.extract_message(os.path.join(directory.name, "no.emlx")).body, "")

    def test_schema_is_version_five_and_refuses_older_versions(self):
        self.assertEqual(mail_index.SCHEMA_VERSION, 5)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "index.sqlite")
        old = sqlite3.connect(path)
        old.executescript(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY);"
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO meta VALUES ('schema_version', '2');"
        )
        old.close()
        with self.assertRaises(mail_index.IndexSchemaError) as caught:
            mail_index.open_index(path)
        self.assertEqual((caught.exception.found, caught.exception.expected), (2, 5))

    def test_bm25_weights_match_the_fts_columns(self):
        index = sqlite3.connect(":memory:")
        index.executescript(mail_index.SCHEMA)
        columns = [row[1] for row in index.execute("PRAGMA table_info(messages_fts)")]
        self.assertEqual(len(mail_search.BM25_WEIGHTS), len(columns))
        self.assertEqual(columns[:4], ["subject", "sender", "to", "cc"])

    def test_cc_column_search_and_result_flags(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = os.path.join(directory.name, "index.sqlite")
        index = mail_index.open_index(path)
        for identifier, cc, attached, bulk in ((1, "carol@example.org", 1, 0), (2, "", 0, 1)):
            index.execute(
                "INSERT INTO messages (id, account, subject, sender, date_received, indexed_at,"
                " has_attachment, is_bulk) VALUES (?, 'Work', 'Plan', 'jane@example.com', ?, ?, ?, ?)",
                (identifier, int(time.time()), int(time.time()), attached, bulk),
            )
            index.execute(
                'INSERT INTO messages_fts (rowid, subject, sender, "to", cc, attachments, body)'
                " VALUES (?, 'Plan', 'jane@example.com', 'bob@example.com', ?, '', 'text')",
                (identifier, cc),
            )
            index.execute(
                "INSERT INTO locations (message, account, mailbox, read, flagged)"
                " VALUES (?, 'Work', 'INBOX', 1, 0)",
                (identifier,),
            )
        index.commit()
        index.close()
        with mock.patch.object(mail_search, "INDEX_PATH", path), mock.patch.object(
            mail_index, "find_store", side_effect=FileNotFoundError
        ):
            found = mail_search.search_all("cc: carol", max_age_minutes=10**9)
            self.assertEqual([m["mail_id"] for m in found["messages"]], [1])
            self.assertTrue(found["messages"][0]["has_attachment"])
            self.assertFalse(found["messages"][0]["is_bulk"])
            legacy = mail_search.search_all("recipients: carol", max_age_minutes=10**9)
            self.assertEqual([m["mail_id"] for m in legacy["messages"]], [1])
            self.assertNotIn("interpreted_as", legacy)
            both = mail_search.search_all("plan", max_age_minutes=10**9)["messages"]
            self.assertEqual({m["mail_id"]: m["is_bulk"] for m in both}, {1: False, 2: True})


class IndexStatusCoverageTests(_FictionalIndexMixin, unittest.TestCase):
    def test_totals_split_by_body_indexed(self):
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE messages SET body_indexed = 1 WHERE id IN (1, 2, 3)")
        connection.execute("UPDATE messages SET account = 'Home' WHERE id = 6")
        connection.commit()
        connection.close()
        status = mail_search.index_status()
        self.assertEqual((status["indexed_messages"], status["with_body"], status["without_body"]), (6, 3, 3))
        work = next(entry for entry in status["accounts"] if entry["account"] == "Work")
        self.assertEqual((work["messages"], work["with_body"], work["without_body"]), (5, 3, 2))



class IndexLockTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = os.path.join(self.directory.name, "index.sqlite")

    def test_second_holder_is_refused_until_release(self):
        first = mail_index.IndexLock(self.database).acquire()
        with self.assertRaises(mail_index.IndexBusy):
            mail_index.IndexLock(self.database).acquire()
        first.release()
        mail_index.IndexLock(self.database).acquire().release()
        self.assertFalse(os.path.exists(self.database + ".sync.lock"))

    def test_lock_of_a_dead_process_is_taken_over(self):
        with open(self.database + ".sync.lock", "w") as handle:
            handle.write("999999999")
        with mock.patch.object(mail_index, "_process_alive", return_value=False):
            mail_index.IndexLock(self.database).acquire().release()

    def test_old_lock_of_a_live_owner_expires_only_without_heartbeat(self):
        owner = mail_index.IndexLock(self.database).acquire()
        old = time.time() - mail_index.LOCK_STALE_SECONDS - 60
        os.utime(owner.path, (old, old))
        owner.touch()  # the running build's heartbeat
        with self.assertRaises(mail_index.IndexBusy):
            mail_index.IndexLock(self.database).acquire()
        os.utime(owner.path, (old, old))  # no heartbeat for too long
        mail_index.IndexLock(self.database).acquire().release()

    def test_swap_in_removes_old_wal_before_replacing(self):
        calls = []
        real_replace = os.replace
        for suffix, content in (("", b"old"), ("-wal", b"w"), ("-shm", b"s"), (".building", b"new")):
            with open(self.database + suffix, "wb") as handle:
                handle.write(content)

        def spy(source, target):
            calls.append(os.path.exists(self.database + "-wal"))
            real_replace(source, target)

        with mock.patch.object(mail_index.os, "replace", spy):
            mail_index.swap_in(self.database + ".building", self.database)
        self.assertEqual(calls, [False])

    def test_sync_index_reports_busy_index(self):
        completed = mock.Mock(returncode=mail_index.LOCK_EXIT_CODE, stdout=b"", stderr=b"")
        with mock.patch.object(mail_search, "INDEX_PATH", self.database), \
                mock.patch("subprocess.run", return_value=completed):
            with self.assertRaises(MailError) as caught:
                mail_search.sync_index()
        self.assertEqual(caught.exception.code, "index_busy")

    def test_a_successful_sync_starts_the_attachment_sync_in_the_background(self):
        completed = mock.Mock(returncode=0, stdout=b"indexed 2 messages\n", stderr=b"")
        with mock.patch.object(mail_search, "INDEX_PATH", self.database), \
                mock.patch("subprocess.run", return_value=completed), \
                mock.patch.object(mail_files, "purge_drafts", return_value={"removed": []}), \
                mock.patch.object(mail_attachments, "start_background_sync", return_value=True) as start:
            result = mail_search.sync_index()
        start.assert_called_once()
        self.assertEqual(result["attachments_sync"], "started in the background")

    def test_the_background_attachment_sync_needs_an_existing_index_and_the_setting(self):
        path = os.path.join(self.directory.name, "attachments.sqlite")
        with mock.patch.object(mail_attachments, "database_path", lambda: path), \
                mock.patch.object(mail_attachments.subprocess, "Popen") as popen:
            self.assertFalse(mail_attachments.start_background_sync())
            open(path, "w").close()
            self.assertTrue(mail_attachments.start_background_sync())
            self.assertEqual(popen.call_args.kwargs["start_new_session"], True)
            popen.reset_mock()
            with mock.patch.dict(os.environ, {"MAIL_MCP_ATTACHMENTS_AUTO_SYNC": "0"}):
                self.assertFalse(mail_attachments.start_background_sync())
            popen.assert_not_called()

    def test_search_reports_file_without_message_table(self):
        sqlite3.connect(self.database).close()
        with mock.patch.object(mail_search, "INDEX_PATH", self.database):
            with self.assertRaises(MailError) as caught:
                mail_search.search_all("anything", max_age_minutes=10**9)
        self.assertEqual(caught.exception.code, "index_invalid")



class IndexLockRaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = os.path.join(self.directory.name, "index.sqlite")
        self.path = self.database + ".sync.lock"

    def test_loser_of_a_stale_takeover_does_not_delete_the_winners_lock(self):
        with open(self.path, "w") as handle:
            handle.write("999999999")
        loser = mail_index.IndexLock(self.database)
        winner = mail_index.IndexLock(self.database)
        real_stale = loser._stale
        state = {"winner": None}

        def stale_then_winner_acts(path=None):
            answer = real_stale(path)
            if path is None and state["winner"] is None:
                # The loser has judged the lock stale; now the winner takes over.
                state["winner"] = winner.acquire()
            return answer

        with mock.patch.object(mail_index, "_process_alive", side_effect=lambda pid: pid != 999999999), \
                mock.patch.object(loser, "_stale", stale_then_winner_acts):
            with self.assertRaises(mail_index.IndexBusy):
                loser.acquire()
        self.assertTrue(os.path.exists(self.path))
        with open(self.path) as handle:
            self.assertEqual(handle.read(), str(os.getpid()))
        state["winner"].release()

    def test_rename_lost_race_reports_busy(self):
        with open(self.path, "w") as handle:
            handle.write("999999999")
        lock = mail_index.IndexLock(self.database)
        with mock.patch.object(mail_index, "_process_alive", return_value=False), \
                mock.patch.object(mail_index.os, "rename", side_effect=FileNotFoundError):
            with self.assertRaises(mail_index.IndexBusy):
                lock.acquire()


class QuoteCuttingTests(unittest.TestCase):
    """cut_quotes / cut_html_quotes: quoted history goes, own text stays."""

    OWN = "The scaffolding delivery is planned on Monday morning."

    def cut(self, text):
        return mail_index.cut_quotes(text)

    def assertCutTo(self, text, expected="OWN"):
        cut, status = self.cut(text)
        self.assertEqual(status, "cut", text)
        self.assertEqual(cut, self.OWN if expected == "OWN" else expected)

    def test_quoted_lines_are_dropped_and_inline_answers_kept(self):
        self.assertCutTo(f"> older zeppelin\n{self.OWN}\n> more zeppelin")

    def test_english_and_french_reply_headers(self):
        for header in (
            "On Tue, 3 Mar 2026 at 10:00, Jane Doe <jane@example.com> wrote:",
            "Le mar. 3 mars 2026 à 10:00, Jane Doe <jane@example.com> a écrit :",
            "Le mar. 3 mars 2026 à 10:00, Jane Doe\n<jane@example.com> a écrit :",
        ):
            self.assertCutTo(f"{self.OWN}\n\n{header}\n> older zeppelin text\n> more")

    def test_outlook_header_blocks(self):
        for block in (
            "De : Jane Doe\nEnvoyé : mardi 3 mars 2026 10:00\nÀ : John Roe\nObjet : Example",
            "From: Jane Doe\nSent: Tuesday, March 3, 2026 10:00\nTo: John Roe\nSubject: Example",
        ):
            self.assertCutTo(f"{self.OWN}\n\n{block}\n\nolder zeppelin text")

    def test_original_message_markers(self):
        for marker in ("-----Original Message-----", "----- Message d'origine -----"):
            self.assertCutTo(f"{self.OWN}\n{marker}\nolder zeppelin text")

    def test_lone_from_or_de_in_prose_is_not_a_header(self):
        text = f"{self.OWN}\nDe : ceci est une phrase ordinaire.\nFrom: the warehouse we ship on Friday."
        self.assertEqual(self.cut(text), (text, "none"))

    def test_ordinary_lines_starting_with_le_or_on_are_kept(self):
        text = f"{self.OWN}\nLe devis arrive demain.\nOn verra."
        self.assertEqual(self.cut(text), (text, "none"))

    def test_signature_is_cut_only_when_short(self):
        self.assertCutTo(f"{self.OWN}\n-- \nJane Doe\nExample Ltd")
        long_block = "\n".join(f"line {n}" for n in range(20))
        text = f"{self.OWN}\n-- \n{long_block}"
        self.assertEqual(self.cut(text), (text, "none"))

    def test_bare_dashes_are_not_a_signature_delimiter(self):
        text = f"{self.OWN}\n--\nnot a signature separator"
        self.assertEqual(self.cut(text), (text, "none"))

    def test_forward_with_no_own_text_keeps_the_forwarded_content(self):
        text = "FYI\n-------- Message transféré --------\nThe pallet arrives on Thursday at the depot."
        self.assertEqual(self.cut(text), (text, "forward"))

    def test_forward_with_own_text_is_cut(self):
        for marker in ("Begin forwarded message:", "-------- Message transféré --------"):
            self.assertCutTo(f"{self.OWN} Please handle it.\n{marker}\nolder zeppelin text".replace(
                f"{self.OWN} Please handle it.", self.OWN))

    def test_safety_fallback_keeps_the_original_when_too_little_remains(self):
        text = "Thanks!\nOn Tue, 3 Mar 2026, Jane Doe wrote:\n> The pallet arrives on Thursday."
        self.assertEqual(self.cut(text), (text, "fallback"))

    def test_html_blockquote_cite_is_removed(self):
        html_text = f"<p>{self.OWN}</p><blockquote type=\"cite\"><p>older zeppelin</p><blockquote type=\"cite\">deeper</blockquote></blockquote><p>Regards from Jane Doe</p>"
        result = mail_index.strip_markup(mail_index.strip_html_quotes(html_text))
        self.assertEqual(result, f"{self.OWN} Regards from Jane Doe")

    def test_gmail_quote_container_is_removed(self):
        html_text = f'<div>{self.OWN}</div><div class="gmail_quote"><div class="gmail_attr">On Tue, Jane Doe wrote:</div><blockquote class="gmail_quote"><div>older zeppelin</div></blockquote></div>'
        result = mail_index.strip_markup(mail_index.strip_html_quotes(html_text))
        self.assertEqual(result, self.OWN)

    def test_outlook_reply_header_truncates_the_rest(self):
        for opener in (
            '<div id="divRplyFwdMsg"><b>From:</b> Jane Doe',
            '<div id="appendonsend"></div><hr><div id="divRplyFwdMsg"><b>De :</b> Jane Doe',
            '<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in"><p><b>From:</b> Jane Doe',
        ):
            html_text = f"<div>{self.OWN}</div>{opener}</p></div><div>older zeppelin</div>"
            result = mail_index.strip_markup(mail_index.strip_html_quotes(html_text))
            self.assertEqual(result, self.OWN, opener)

    def test_ordinary_html_blockquote_and_borders_are_left_alone(self):
        html_text = f'<div>{self.OWN}</div><blockquote>A famous saying here</blockquote><div style="border-top:solid 1px">Totals below</div>'
        self.assertEqual(mail_index.cut_html_quotes(html_text), (html_text, False))

    def test_html_fallback_when_nothing_else_remains(self):
        html_text = '<p>Hi</p><blockquote type="cite"><p>The pallet arrives on Thursday at the depot.</p></blockquote>'
        self.assertEqual(mail_index.strip_html_quotes(html_text), html_text)

    def test_extract_text_cuts_quotes_from_plain_and_html_parts(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)

        def emlx(mime):
            raw = mime.replace("\n", "\r\n").encode()
            path = os.path.join(directory.name, f"{len(os.listdir(directory.name))}.emlx")
            with open(path, "wb") as handle:
                handle.write(str(len(raw)).encode() + b"\n" + raw)
            return path

        plain = emlx(
            "Message-ID: <q1@example.com>\nContent-Type: text/plain; charset=utf-8\n\n"
            f"{self.OWN}\n> older zeppelin\n"
        )
        self.assertEqual(mail_index.extract_text(plain)[1], self.OWN)
        html_only = emlx(
            "Message-ID: <q2@example.com>\nContent-Type: text/html; charset=utf-8\n\n"
            f'<p>{self.OWN}</p><blockquote type="cite">older zeppelin</blockquote>\n'
        )
        self.assertEqual(mail_index.extract_text(html_only)[1], self.OWN)
        self.assertIn("zeppelin", mail_index.extract_message(html_only, quotes=False).body)

    def test_bottom_posted_reply_keeps_its_answer(self):
        text = (
            "Hello Jane,\n\nOn Tue, 3 Mar 2026 at 10:00, Jane Doe <jane@example.com> wrote:\n"
            f"> older zeppelin question\n\n{self.OWN}\nRegards"
        )
        cut, status = self.cut(text)
        self.assertEqual(status, "cut")
        self.assertIn("scaffolding", cut)
        self.assertNotIn("zeppelin", cut)

    def test_interleaved_answers_are_kept(self):
        text = f"Le mar. 3 mars 2026, Jane Doe a écrit :\n> older zeppelin question\n{self.OWN}\n> second zeppelin"
        self.assertCutTo(text)

    def test_own_line_ending_like_a_header_does_not_cut_the_rest(self):
        text = f"{self.OWN}\nLe client nous a écrit :\nthe pallet arrives on Thursday.\nRegards"
        self.assertEqual(self.cut(text), (text, "none"))
        wrapped = f"{self.OWN}\nOn the phone, Jane Doe\nwrote:\nthe pallet arrives on Thursday."
        self.assertEqual(self.cut(wrapped), (wrapped, "none"))

    def test_only_the_last_signature_delimiter_counts(self):
        text = f"{self.OWN}\n-- \nsection two follows here with real words\n-- \nJane Doe"
        cut, status = self.cut(text)
        self.assertEqual(status, "cut")
        self.assertIn("section two follows", cut)
        self.assertNotIn("Jane Doe", cut)

    def test_eager_mode_cuts_lone_headers_and_forwards_without_fallback(self):
        self.assertEqual(mail_index.strip_quotes("Kept.\nDe : Jane Doe\nhidden", eager=True), "Kept.")
        self.assertEqual(
            mail_index.strip_quotes("Kept.\n-------- Message transféré --------\nhidden", eager=True), "Kept."
        )


class FrenchStemmerTests(unittest.TestCase):
    """mail_stem.stem_word: light French stemming, English left mostly alone."""

    def test_stem_table(self):
        table = {
            # plurals and feminine
            "facture": "factur", "factures": "factur",
            "projet": "projet", "projets": "projet",
            "bureau": "bureau", "bureaux": "bureau",
            "chevaux": "cheval", "cheval": "cheval", "journaux": "journal",
            "travaux": "travail", "travail": "travail",
            # participles and verbs
            "relance": "relanc", "relances": "relanc", "relancé": "relanc",
            "relancée": "relanc", "relancer": "relanc", "relancent": "relanc",
            "relancerait": "relanc", "relanceront": "relanc",
            "payé": "pay", "payée": "pay", "payés": "pay", "payer": "pay",
            "appelle": "appel", "appeler": "appel", "appel": "appel",
            # words that must stay as they are
            "devis": "devis", "avis": "avis", "français": "francais",
            "française": "francais", "mois": "mois", "nous": "nous",
            "document": "document", "documents": "document", "moment": "moment",
            # short words, digits, identifiers
            "les": "les", "des": "des", "est": "est", "pour": "pour",
            "2025": "2025", "ref123": "ref123", "a1b2c3d4": "a1b2c3d4",
            "x" * 45: "x" * 45,
            # English
            "invoices": "invoic", "invoice": "invoic", "address": "address",
            "status": "status", "business": "business", "emails": "email",
        }
        for word, expected in table.items():
            with self.subTest(word=word):
                self.assertEqual(mail_stem.stem_word(word), expected)

    def test_accents_and_case_do_not_matter(self):
        self.assertEqual(mail_stem.stem_word("RELANCÉE"), mail_stem.stem_word("relancee"))
        self.assertEqual(mail_stem.stem_word("Factures"), "factur")

    def test_decomposed_accents_are_composed_first(self):
        decomposed = unicodedata.normalize("NFD", "résumés")
        self.assertNotEqual(decomposed, "résumés")
        self.assertEqual(mail_stem.stem_text(decomposed), mail_stem.stem_text("résumés"))
        self.assertEqual(mail_stem.stem_tokens(decomposed), ["resum"])
        self.assertEqual(
            mail_stem.rewrite_query(unicodedata.normalize("NFD", "relancé")), mail_stem.rewrite_query("relancé")
        )

    def test_rendez_vous_is_two_words(self):
        self.assertEqual(mail_stem.stem_tokens("Rendez-vous demain"), ["rend", "vous", "demain"])

    def test_text_keeps_one_token_per_word(self):
        text = "Les factures_2025 du client: 12,50 EUR (relancées)"
        self.assertEqual(
            mail_stem.stem_text(text).split(),
            ["les", "factur", "2025", "du", "client", "12", "50", "eur", "relanc"],
        )
        self.assertEqual(mail_stem.stem_text(""), "")

    def test_stems_are_idempotent_for_the_indexed_form(self):
        # A stem is re-tokenised by FTS5 but must not be stemmed again by anyone.
        for word in ("factures", "relancées", "chevaux", "payer", "projets"):
            stem = mail_stem.stem_word(word)
            self.assertEqual(mail_stem.stem_word(stem), stem)

    def test_a_stem_is_never_shorter_than_three_letters(self):
        for word in ("idee", "annees", "pays", "eaux", "rues", "veste"):
            self.assertGreaterEqual(len(mail_stem.stem_word(word)), 3)


class QueryRewriteTests(unittest.TestCase):
    """mail_stem.rewrite_query: raw OR stem, structure untouched."""

    RAW = "{subject sender to cc attachments body}"
    STEMS = "{subject_stem attachments_stem body_stem}"

    def test_bare_word_is_searched_raw_or_stemmed(self):
        self.assertEqual(
            mail_stem.rewrite_query("factures"),
            f'({self.RAW}: "factures" OR {self.STEMS}: "factur")',
        )

    def test_prefix_is_raw_or_stemmed_prefix(self):
        self.assertEqual(
            mail_stem.rewrite_query("fact*"),
            f'({self.RAW}: "fact"* OR {self.STEMS}: "fact"*)',
        )
        self.assertEqual(
            mail_stem.rewrite_query("relancer*"),
            f'({self.RAW}: "relancer"* OR {self.STEMS}: "relanc"*)',
        )

    def test_phrase_is_exact_raw_only(self):
        self.assertEqual(
            mail_stem.rewrite_query('"les factures payées"'),
            f'{self.RAW}: "les factures payees"',
        )
        self.assertEqual(mail_stem.rewrite_query('"a b"*'), f'{self.RAW}: "a b"*')

    def test_column_filters_pick_raw_and_stem_columns(self):
        self.assertEqual(
            mail_stem.rewrite_query("subject:factures"),
            '({subject}: "factures" OR {subject_stem}: "factur")',
        )
        self.assertEqual(mail_stem.rewrite_query("sender:jane"), '{sender}: "jane"')
        self.assertEqual(mail_stem.rewrite_query("{to cc}: bobs").strip(), '{to cc}: "bobs"')
        self.assertEqual(
            mail_stem.rewrite_query('subject:"les factures"'), '{subject}: "les factures"'
        )
        mixed = mail_stem.rewrite_query("{subject to}: factures")
        self.assertIn('{subject to}: "factures"', mixed)
        self.assertIn('{subject_stem}: "factur"', mixed)

    def test_unknown_column_is_read_as_a_word(self):
        rewritten = mail_stem.rewrite_query("re: factures")
        self.assertTrue(rewritten.startswith(f'{self.RAW}: "re" AND '))
        self.assertNotIn("re:", rewritten)

    def test_negated_filter_keeps_its_exclusion_untouched(self):
        self.assertEqual(mail_stem.rewrite_query("-subject:facture"), "-subject:facture")
        self.assertEqual(mail_stem.rewrite_query("-{to cc}:jane"), "-{to cc}:jane")
        self.assertEqual(mail_stem.rewrite_query("-subject:(a b)").replace(" AND ", " "), "-subject:(a b)")
        self.assertTrue(mail_stem.rewrite_query("plan -subject:facture").endswith("AND -subject:facture"))

    def test_negated_filter_excludes_the_column_in_a_real_index(self):
        # "-subject:facture" is FTS5 for "facture in any column but the subject".
        connection = sqlite3.connect(":memory:")
        connection.executescript(mail_index.SCHEMA)
        for identifier, subject, body in ((1, "facture", "plan"), (2, "plan", "facture")):
            connection.execute(
                "INSERT INTO messages_fts (rowid, subject, subject_stem, body, body_stem)"
                " VALUES (?, ?, ?, ?, ?)",
                (identifier, subject, mail_stem.stem_text(subject), body, mail_stem.stem_text(body)),
            )
        rows = connection.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?",
            (mail_stem.rewrite_query("-subject:facture"),),
        ).fetchall()
        self.assertEqual([row[0] for row in rows], [2])

    def test_initial_token_and_phrase_concatenation_stay_valid(self):
        self.assertEqual(mail_stem.rewrite_query("subject:^facture"), 'subject:^"facture"')
        self.assertEqual(mail_stem.rewrite_query("a + b"), '"a" + "b"')
        self.assertEqual(mail_stem.rewrite_query('"x y" + z'), '"x y" + "z"')
        connection = sqlite3.connect(":memory:")
        connection.executescript(mail_index.SCHEMA)
        for query in ("subject:^facture", "a + b", "a+b", "budget a + b"):
            connection.execute("SELECT * FROM messages_fts WHERE messages_fts MATCH ?", (mail_stem.rewrite_query(query),))

    def test_words_fts5_would_refuse_become_raw_phrases(self):
        raw = "{subject sender to cc attachments body}"
        self.assertEqual(mail_stem.rewrite_query("l'entreprise"), f'{raw}: "l entreprise"')
        self.assertEqual(mail_stem.rewrite_query("rendez-vous"), f'{raw}: "rendez vous"')
        self.assertEqual(mail_stem.rewrite_query("F-2025-001"), f'{raw}: "f 2025 001"')
        self.assertEqual(mail_stem.rewrite_query("l'entrepr*"), f'{raw}: "l entrepr"*')
        rewritten = mail_stem.rewrite_query("factures 12/2025")
        self.assertIn('"factur"', rewritten)  # the plain word keeps its stem expansion
        self.assertIn(f'{raw}: "12 2025"', rewritten)
        connection = sqlite3.connect(":memory:")
        connection.executescript(mail_index.SCHEMA)
        for query in ("l'entreprise", "rendez-vous", "F-2025-001", "factures 12/2025", "12:30"):
            connection.execute("SELECT * FROM messages_fts WHERE messages_fts MATCH ?", (mail_stem.rewrite_query(query),))

    def test_operators_and_parentheses_pass_through(self):
        rewritten = mail_stem.rewrite_query("(devis OR budget) NOT spam")
        self.assertRegex(rewritten, r'^\(\(.*"devis".*\) OR \(.*"budget".*\)\) NOT \(.*"spam".*\)$')
        self.assertEqual(mail_stem.rewrite_query("^devis"), '^"devis"')

    def test_implicit_and_is_written_out_between_expanded_terms(self):
        self.assertRegex(mail_stem.rewrite_query("factures jane"), r"\) +AND +\(")

    def test_near_words_are_raw_and_distance_is_kept(self):
        self.assertEqual(mail_stem.rewrite_query("NEAR(factures payées, 5)"), 'NEAR("factures" "payees", 5)')

    def test_invalid_syntax_is_left_invalid(self):
        for query in ("a++", "plan ++"):
            with self.subTest(query=query):
                connection = sqlite3.connect(":memory:")
                connection.execute("CREATE VIRTUAL TABLE t USING fts5(body)")
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("SELECT * FROM t WHERE t MATCH ?", (mail_stem.rewrite_query(query),))

    def test_rewritten_queries_are_valid_fts5(self):
        connection = sqlite3.connect(":memory:")
        connection.executescript(mail_index.SCHEMA)
        for query in (
            "factures", "fact*", '"les factures"', "subject:factures", "{to cc}: jane*",
            "factures OR (devis AND jane)", "factures NOT devis", "NEAR(factures devis, 3)",
            '"factures payées"*', "^factures", "(factures) (devis)",
        ):
            with self.subTest(query=query):
                connection.execute(
                    "SELECT * FROM messages_fts WHERE messages_fts MATCH ?", (mail_stem.rewrite_query(query),)
                )


class StemmedSearchTests(_FictionalIndexMixin, unittest.TestCase):
    """End to end: dual index, rewritten query, quoted-terms retry."""

    def setUp(self):
        super().setUp()
        index = sqlite3.connect(self.path)
        rows = [
            (11, "Relance des factures impayées", "jane@example.com", "bob@example.org",
             "Facture_2025.pdf", "Merci de payer rapidement. Rendez-vous demain."),
            (12, "Compte rendu", "john@example.org", "", "", "La facture a été payée hier."),
        ]
        now = int(time.time())
        for identifier, subject, sender, to, attachments, body in rows:
            index.execute(
                "INSERT INTO messages (id, account, subject, sender, date_received, indexed_at)"
                " VALUES (?, 'Work', ?, ?, ?, ?)", (identifier, subject, sender, now, now),
            )
            index.execute(
                'INSERT INTO messages_fts (rowid, subject, sender, "to", cc, attachments, body,'
                " subject_stem, attachments_stem, body_stem)"
                " VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?)",
                (identifier, subject, sender, to, attachments, body,
                 mail_stem.stem_text(subject), mail_stem.stem_text(attachments), mail_stem.stem_text(body)),
            )
            index.execute(
                "INSERT INTO locations (message, account, mailbox, read, flagged)"
                " VALUES (?, 'Work', 'INBOX', 1, 0)", (identifier,),
            )
        index.commit()
        index.close()

    def test_inflected_forms_find_each_other(self):
        self.assertEqual(set(self.ids("facture")), {11, 12})
        self.assertEqual(set(self.ids("factures")), {11, 12})
        self.assertEqual(set(self.ids("payer")), {11, 12})
        self.assertEqual(self.ids("relancer"), [11])
        self.assertEqual(self.ids("relancé"), [11])

    def test_the_exact_form_ranks_first(self):
        # Mail 11 holds "payer" in its body, mail 12 "payée": same stem.
        self.assertEqual(self.ids("payer"), [11, 12])
        self.assertEqual(self.ids("payée"), [12, 11])

    def test_names_and_addresses_stay_exact(self):
        self.assertIn(11, self.ids("jane"))
        self.assertEqual(set(self.ids("sender:john")), {2, 12})
        self.assertEqual(self.ids("{to}:bob"), [11])
        self.assertEqual(self.ids("sender:factures"), [])

    def test_quotes_are_exact_and_prefix_and_columns_work(self):
        self.assertEqual(set(self.ids("fact*")), {11, 12})
        self.assertEqual(self.ids('"rendez vous"'), [11])
        self.assertEqual(self.ids('"factures impayées"'), [11])
        self.assertEqual(self.ids('"facture impayées"'), [])
        self.assertEqual(self.ids('"payer"'), [11])
        self.assertEqual(self.ids('"impayées factures"'), [])
        self.assertEqual(set(self.ids("subject:facture")), {11})
        self.assertEqual(self.ids("attachments:factures"), [11])
        self.assertEqual(self.ids("facture NOT relance"), [12])

    def test_legacy_recipients_filter_and_quoted_retry_still_work(self):
        self.assertEqual(self.ids("recipients:bob"), [11])
        result = mail_search.search_all("factures (", max_age_minutes=10**9)
        self.assertIn("interpreted_as", result)
        self.assertEqual([message["mail_id"] for message in result["messages"]], [11, 12] if result["messages"] else [])
        # Slashes, hyphens and apostrophes no longer need the retry.
        result = mail_search.search_all("factures 2025/", max_age_minutes=10**9)
        self.assertNotIn("interpreted_as", result)
        self.assertEqual([message["mail_id"] for message in result["messages"]], [11])
        self.assertEqual(self.ids("factures 2025/03"), [])
        self.assertEqual(self.ids("rendez-vous"), [11])
        self.assertEqual(set(self.ids("les factures 2025-")), set())


class StemmedSnippetTests(unittest.TestCase):
    FILLER = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. "

    def test_snippet_centres_on_an_inflected_word(self):
        text = self.FILLER * 5 + "Les factures sont payees. " + self.FILLER * 5
        snippet = mail_index.make_snippet(text, "facture")
        self.assertIn("factures", snippet)
        snippet = mail_index.make_snippet(self.FILLER * 5 + "Il a relancé le client. " + self.FILLER * 5, "relancer")
        self.assertIn("relancé", snippet)

    def test_phrase_and_prefix_compare_stems(self):
        text = self.FILLER * 5 + "Merci pour les factures payées. " + self.FILLER * 5
        self.assertIn("factures payées", mail_index.make_snippet(text, '"facture payer"'))
        self.assertIn("factures", mail_index.make_snippet(text, "fact*"))

    def test_snippet_of_a_decomposed_text_lands_on_the_word(self):
        text = unicodedata.normalize("NFD", self.FILLER * 5 + "Le résumé est prêt. " + self.FILLER * 5)
        self.assertIn("résumé", mail_index.make_snippet(text, "résumés"))

    def test_the_exact_form_is_preferred_over_a_stem_match(self):
        text = (self.FILLER * 5 + "Une facture est jointe. " + self.FILLER * 5
                + "Les factures suivent. " + self.FILLER * 5)
        self.assertIn("Les factures suivent", mail_index.make_snippet(text, "factures"))

    def test_a_different_word_with_the_same_start_is_not_a_hit(self):
        text = self.FILLER * 5 + "Le facteur passe. " + self.FILLER * 5
        self.assertTrue(mail_index.make_snippet(text, "facture").startswith("Lorem"))


class AttachmentClassificationTests(unittest.TestCase):
    def classify(self, name, size=200_000, ocr=True):
        return mail_attachments.classify(name, size, ocr, 20)

    def test_documents_are_read(self):
        for name in ("quote.pdf", "plan.DOCX", "budget.xlsx", "old.doc", "deck.pptx"):
            self.assertEqual(self.classify(name), ("process", None), name)

    def test_archives_cad_and_media_are_skipped_by_type(self):
        for name in ("all.zip", "drawing.dwg", "clip.mp4", "song.mp3", "legacy.xls"):
            self.assertEqual(self.classify(name), ("skip", "type"), name)

    def test_size_limits_are_tighter_off_pdf(self):
        self.assertEqual(self.classify("a.pdf", 15 * 2**20), ("process", None))
        self.assertEqual(self.classify("a.pdf", 25 * 2**20), ("too_big", None))
        self.assertEqual(self.classify("a.docx", 15 * 2**20), ("too_big", None))

    def test_images_need_size_and_a_name_that_is_not_decoration(self):
        self.assertEqual(self.classify("scan.jpg"), ("process", None))
        self.assertEqual(self.classify("scan.jpg", 10_000), ("skip", "small_image"))
        self.assertEqual(self.classify("Company logo.png"), ("skip", "decoration"))
        self.assertEqual(self.classify("image003.png"), ("skip", "decoration"))
        self.assertEqual(self.classify("IMG_2041.jpg"), ("process", None))
        self.assertEqual(self.classify("scan.jpg", ocr=False), ("skip", "ocr_off"))

    def test_an_empty_file_is_skipped(self):
        self.assertEqual(self.classify("a.pdf", 0), ("skip", "empty_file"))

    def test_text_is_squeezed_and_capped(self):
        self.assertEqual(mail_attachments.tidy_text("a \n\n b\t c", 100), "a b c")
        self.assertEqual(mail_attachments.tidy_text("abcdef", 3), "abc")

    def test_default_database_sits_beside_the_index(self):
        with mock.patch.dict(os.environ, {"MAIL_MCP_INDEX_PATH": "/x/y/index.sqlite"}):
            os.environ.pop("MAIL_MCP_ATTACHMENTS_PATH", None)
            self.assertEqual(mail_attachments.database_path(), "/x/y/attachments.sqlite")
        with mock.patch.dict(os.environ, {"MAIL_MCP_ATTACHMENTS_PATH": "/z/a.sqlite"}):
            self.assertEqual(mail_attachments.database_path(), "/z/a.sqlite")

    def test_a_query_limited_to_a_message_field_skips_attachments(self):
        self.assertIsNone(mail_attachments.fts_query("subject: budget"))
        self.assertIsNone(mail_attachments.fts_query("sender: jane"))
        self.assertIsNotNone(mail_attachments.fts_query("budget"))
        self.assertIsNotNone(mail_attachments.fts_query('"12:30" budget'))


class AttachmentSyncTests(unittest.TestCase):
    """Discovery, resumable processing and cleanup, on a fictional store."""

    class Lock:
        def touch(self):
            pass

    def setUp(self):
        import base64
        import zipfile

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = os.path.join(self.directory.name, "V1")
        self.database = os.path.join(self.directory.name, "attachments.sqlite")
        root = os.path.join(self.store, "Acct.mbox", "UUID")
        # Message 12345: partial, its attachments are files on disk.
        folder = os.path.join(root, "Data", "5", "4", "3", "Messages")
        os.makedirs(folder)
        self.partial = os.path.join(folder, "12345.partial.emlx")
        with open(self.partial, "wb") as handle:
            handle.write(b"x")
        attachments = os.path.join(root, "Data", "2", "1", "Attachments", "12345")
        for part, name in (("1", "plan.docx"), ("2", "all.zip"), ("3", "logo.png")):
            os.makedirs(os.path.join(attachments, part))
        self.docx = os.path.join(attachments, "1", "plan.docx")
        with zipfile.ZipFile(self.docx, "w") as archive:
            archive.writestr("word/document.xml", (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Warranty certificate for Example Ltd</w:t></w:r></w:p></w:body></w:document>"))
        with open(os.path.join(attachments, "2", "all.zip"), "wb") as handle:
            handle.write(b"PK")
        with open(os.path.join(attachments, "3", "logo.png"), "wb") as handle:
            handle.write(b"x" * 100_000)
        # Message 20000: a full .emlx carrying a spreadsheet inside its MIME.
        sheet = os.path.join(self.directory.name, "sheet.xlsx")
        with zipfile.ZipFile(sheet, "w") as archive:
            archive.writestr("xl/sharedStrings.xml", (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<si><t>Invoice schedule</t></si></sst>"))
        with open(sheet, "rb") as handle:
            encoded = base64.encodebytes(handle.read()).decode()
        raw = (
            "From: jane@example.com\r\nSubject: Sheet\r\nMIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="b"\r\n\r\n--b\r\n'
            "Content-Type: text/plain\r\n\r\nSee attached.\r\n--b\r\n"
            'Content-Type: application/octet-stream; name="schedule.xlsx"\r\n'
            'Content-Disposition: attachment; filename="schedule.xlsx"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\n"
            + encoded + "--b--\r\n"
        ).encode()
        folder = os.path.join(root, "Data", "0", "0", "2", "Messages")
        os.makedirs(folder)
        self.full = os.path.join(folder, "20000.emlx")
        with open(self.full, "wb") as handle:
            handle.write(str(len(raw)).encode() + b"\n" + raw + b"<plist></plist>")
        self.files = {12345: self.partial, 20000: self.full}
        self.connection = mail_attachments.open_database(self.database)
        self.addCleanup(self.connection.close)

    def discover(self, **kwargs):
        return mail_attachments.discover(self.connection, self.store, self.files, kwargs.get("retry", False), log=lambda _: None)

    def process(self):
        return mail_attachments.process_pending(self.connection, self.files, self.Lock(), None, log=lambda _: None)

    def rows(self):
        return {row["filename"]: row for row in self.connection.execute("SELECT * FROM attachments")}

    def test_discovery_records_what_to_read_and_what_to_skip_with_reasons(self):
        self.discover()
        rows = self.rows()
        self.assertEqual(rows["plan.docx"]["status"], "pending")
        self.assertEqual(rows["schedule.xlsx"]["status"], "pending")
        self.assertEqual(rows["schedule.xlsx"]["part"], "mime:0")
        self.assertEqual((rows["all.zip"]["status"], rows["all.zip"]["reason"]), ("skipped", "type"))
        self.assertEqual((rows["logo.png"]["status"], rows["logo.png"]["reason"]), ("skipped", "decoration"))

    def test_processing_stores_text_and_makes_it_searchable(self):
        self.discover()
        self.assertEqual(self.process(), 2)
        rows = self.rows()
        self.assertEqual(rows["plan.docx"]["status"], "ok")
        self.assertIn("Warranty certificate", rows["plan.docx"]["text"])
        self.assertIn("Invoice schedule", rows["schedule.xlsx"]["text"])
        hits = mail_attachments.search_hits("warranty", path=self.database)
        self.assertEqual([(hit.message, hit.filename) for hit in hits], [(12345, "plan.docx")])
        self.assertEqual([hit.message for hit in mail_attachments.search_hits("schedule", path=self.database)], [20000])

    def test_a_second_run_finds_nothing_to_do(self):
        self.discover()
        self.process()
        self.connection.commit()
        changed, removed = self.discover()
        self.assertEqual((changed, removed), (0, 0))
        self.assertEqual(self.process(), 0)

    def test_an_interrupted_run_resumes_where_it_stopped(self):
        self.discover()
        with mock.patch.object(mail_attachments, "BATCH_ROWS", 1):
            first = mail_attachments.process_pending(self.connection, self.files, self.Lock(), 1, log=lambda _: None)
        self.assertEqual(first, 1)
        pending = self.connection.execute("SELECT count(*) FROM attachments WHERE status = 'pending'").fetchone()[0]
        self.assertEqual(pending, 1)
        self.discover()
        self.assertEqual(self.process(), 1)

    def test_a_changed_file_is_read_again(self):
        import zipfile

        self.discover()
        self.process()
        with zipfile.ZipFile(self.docx, "w") as archive:
            archive.writestr("word/document.xml", (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Revised terms</w:t></w:r></w:p></w:body></w:document>"))
        os.utime(self.docx, (1, 1))
        changed, _ = self.discover()
        self.assertEqual(changed, 1)
        self.process()
        self.assertEqual(mail_attachments.search_hits("warranty", path=self.database), [])
        self.assertEqual(len(mail_attachments.search_hits("revised", path=self.database)), 1)

    def test_rows_of_a_message_that_left_the_store_are_deleted(self):
        self.discover()
        self.process()
        del self.files[12345]
        _, removed = self.discover()
        self.assertEqual(removed, 3)
        self.assertEqual(mail_attachments.search_hits("warranty", path=self.database), [])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM attachments").fetchone()[0], 1)

    def test_a_file_that_disappeared_loses_its_row_but_not_its_siblings(self):
        self.discover()
        os.unlink(os.path.join(os.path.dirname(os.path.dirname(self.docx)), "2", "all.zip"))
        _, removed = self.discover()
        self.assertEqual(removed, 1)
        self.assertNotIn("all.zip", self.rows())

    def test_a_setting_change_reclassifies_skipped_images(self):
        self.discover()
        self.assertEqual(self.rows()["logo.png"]["reason"], "decoration")
        with mock.patch.dict(os.environ, {"MAIL_MCP_ATTACHMENTS_OCR_IMAGES": "0"}):
            self.discover()
        self.assertEqual(self.rows()["logo.png"]["reason"], "ocr_off")

    def test_mime_parts_are_written_to_a_private_directory_that_is_removed(self):
        created = []
        real = tempfile.mkdtemp

        def spy(*args, **kwargs):
            path = real(*args, **kwargs)
            created.append(path)
            return path

        self.discover()
        with mock.patch.object(mail_attachments.tempfile, "mkdtemp", spy):
            self.process()
        self.assertTrue(created)
        for path in created:
            self.assertFalse(os.path.exists(path))
            self.assertFalse(path.startswith(config.PROJECT_ROOT))

    def test_status_counts_rows_by_status(self):
        self.discover()
        self.process()
        report = mail_attachments.status(self.database)
        self.assertTrue(report["built"])
        self.assertEqual(report["files_by_status"], {"ok": 2, "skipped": 2})
        self.assertEqual(report["pending"], 0)
        self.assertFalse(mail_attachments.status(os.path.join(self.directory.name, "none.sqlite"))["built"])


class AttachmentSearchTests(_FictionalIndexMixin, unittest.TestCase):
    """search_all merging the text of attachments, from a separate database."""

    def setUp(self):
        super().setUp()
        self.attachments_path = os.path.join(self.directory.name, "attachments.sqlite")
        connection = mail_attachments.open_database(self.attachments_path)
        self.next_id = 1
        self.connection = connection
        self.addCleanup(connection.close)
        patcher = mock.patch.object(mail_attachments, "database_path", lambda: self.attachments_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, message, filename, text):
        identifier = self.next_id
        self.next_id += 1
        self.connection.execute(
            "INSERT INTO attachments (id, message, part, filename, ext, size, mtime, status, text)"
            " VALUES (?,?,?,?,?,?,0,'ok',?)",
            (identifier, message, f"1/{filename}", filename, os.path.splitext(filename)[1], 1, text),
        )
        mail_attachments.store_fts(self.connection, identifier, filename, text)
        self.connection.commit()

    def test_a_message_matched_only_through_an_attachment_is_found_and_says_so(self):
        self.add(2, "certificate.pdf", "This warranty certificate covers the boiler.")
        result = mail_search.search_all("warranty", max_age_minutes=10**9)
        self.assertEqual([m["mail_id"] for m in result["messages"]], [2])
        match = result["messages"][0]
        self.assertEqual(match["attachment_match"]["filename"], "certificate.pdf")
        self.assertTrue(match["snippet"].startswith("[attachment: certificate.pdf]"))
        self.assertIn("warranty", match["snippet"])

    def test_without_an_attachment_index_search_is_unchanged(self):
        os.unlink(self.attachments_path)
        self.assertEqual(self.ids("roadmap"), [2, 3, 1])
        self.assertNotIn("attachments_note", mail_search.search_all("roadmap", max_age_minutes=10**9))

    def test_an_unreadable_attachment_index_does_not_break_search(self):
        with open(self.attachments_path, "wb") as handle:
            handle.write(b"this is not a database" * 100)
        result = mail_search.search_all("roadmap", max_age_minutes=10**9)
        self.assertEqual([m["mail_id"] for m in result["messages"]], [2, 3, 1])

    def test_operators_still_filter_attachment_hits(self):
        self.add(2, "certificate.pdf", "warranty certificate")
        self.assertEqual(self.ids("warranty from:jane@example.com"), [])
        self.assertEqual(self.ids("warranty from:john@example.org"), [2])
        self.assertEqual(self.ids("warranty", since="2999-01-01"), [])

    def test_an_attachment_hit_lifts_a_message_that_also_matches_on_its_own(self):
        self.assertEqual(self.ids("budget"), [5, 4, 6])
        for number in range(3):
            self.add(4, f"budget{number}.xlsx", "budget budget budget budget quarterly budget")
        self.assertEqual(self.ids("budget")[0], 4)

    def test_a_narrow_filter_is_applied_before_the_attachment_cap(self):
        # Message 1 has the far better attachment hit, message 2 the only one
        # that passes the filter: a cap of one applied first would lose it.
        self.add(1, "strong.pdf", "warranty warranty warranty warranty warranty")
        self.add(2, "weak.pdf", "one warranty among many other words in this long certificate text")
        with mock.patch.object(mail_search, "ATTACHMENT_CANDIDATES", 1):
            self.assertEqual(self.ids("warranty from:john@example.org"), [2])

    def test_a_filter_matching_too_many_messages_widens_the_cap_instead(self):
        self.add(1, "strong.pdf", "warranty warranty warranty warranty warranty")
        self.add(2, "weak.pdf", "one warranty among many other words in this long certificate text")
        with mock.patch.object(mail_search, "ATTACHMENT_CANDIDATES", 1), \
                mock.patch.object(mail_search, "ATTACHMENT_ID_LIMIT", 0):
            self.assertEqual(self.ids("warranty from:john@example.org"), [2])

    def test_a_query_limited_to_a_message_field_ignores_attachments(self):
        self.add(2, "certificate.pdf", "warranty certificate")
        self.assertEqual(self.ids("subject: warranty"), [])

    def test_date_sort_places_an_attachment_only_hit_by_date(self):
        self.add(4, "budget.xlsx", "budget")
        self.assertEqual(self.ids("budget", sort="date"), [5, 4, 6])
        self.add(1, "notes.pdf", "budget forecast")
        self.assertEqual(self.ids("budget", sort="date"), [1, 5, 4, 6])

    def test_snippets_can_be_turned_off(self):
        self.add(2, "certificate.pdf", "warranty certificate")
        result = mail_search.search_all("warranty", max_age_minutes=10**9, snippets=False)
        self.assertNotIn("snippet", result["messages"][0])
        self.assertNotIn("snippet", result["messages"][0]["attachment_match"])

    def test_the_status_reports_the_attachment_index(self):
        self.add(2, "certificate.pdf", "warranty certificate")
        report = mail_search.index_status()
        self.assertTrue(report["attachments"]["built"])
        self.assertEqual(report["attachments"]["files_by_status"], {"ok": 1})


class AttachmentEvalPairTests(unittest.TestCase):
    TEXT = ("The heating installation certificate lists radiators, thermostats, boilers and "
            "manifolds delivered to the Example Ltd warehouse with serial numbers attached. ") * 3

    def test_pairs_expect_the_message_and_avoid_its_subject_words(self):
        pairs = mail_eval.generate_attachment_pairs(
            [(7, "Installation certificate", self.TEXT)], 5, random.Random(1))
        self.assertEqual(len(pairs), 1)
        self.assertEqual((pairs[0]["expected"], pairs[0]["kind"]), (7, "attachment"))
        self.assertFalse({"installation", "certificate"} & set(pairs[0]["query"].split()))

    def test_a_query_the_message_already_answers_is_dropped(self):
        pairs = mail_eval.generate_attachment_pairs(
            [(7, "Notes", self.TEXT)], 5, random.Random(1), found_by_message=lambda query, identifier: True)
        self.assertEqual(pairs, [])

    def test_rare_words_only(self):
        pairs = mail_eval.generate_attachment_pairs(
            [(7, "Notes", self.TEXT)], 5, random.Random(1), is_rare=lambda word: word == "manifolds")
        self.assertEqual(pairs, [])  # a single acceptable word makes no query

    def test_redrawing_attachment_pairs_keeps_the_other_kinds(self):
        existing = [
            {"query": "a b", "expected": 1, "source": "auto", "kind": "subject"},
            {"query": "c d", "expected": 2, "source": "auto", "kind": "attachment"},
            {"query": "e f", "expected": 3, "source": "manual"},
        ]
        fresh = [{"query": "g h", "expected": 4, "source": "auto", "kind": "attachment"}]
        merged = mail_eval.merge_pairs(existing, fresh, ("attachment",))
        self.assertEqual([pair["query"] for pair in merged], ["a b", "e f", "g h"])
        self.assertEqual([pair["query"] for pair in mail_eval.merge_pairs(existing, fresh)], ["e f", "g h"])

    def test_the_kind_is_reported_apart(self):
        rows = [{"rank": 1, "kind": "attachment"}, {"rank": None, "kind": "subject"}]
        by_kind = mail_eval.aggregate_by_kind(rows)
        self.assertEqual(by_kind["attachment"]["recall_at_1"], 1.0)
        self.assertEqual(by_kind["subject"]["not_found"], 1)


class AttachmentRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_a_killed_compile_leaves_no_binary_behind(self):
        build = os.path.join(self.directory.name, "build")
        with mock.patch.object(mail_attachments, "SWIFTC", "/usr/bin/true"), \
                mock.patch.object(mail_attachments.subprocess, "run",
                                  side_effect=KeyboardInterrupt) as run:
            with self.assertRaises(KeyboardInterrupt):
                mail_attachments.compile_tool("pdftext", build)
        self.assertEqual(os.listdir(build), [])
        self.assertTrue(run.called)

    def test_a_tool_that_does_not_run_is_not_installed(self):
        build = os.path.join(self.directory.name, "build")

        def fake(command, **kwargs):
            if command[0] == mail_attachments.SWIFTC:
                with open(command[command.index("-o") + 1], "w") as handle:
                    handle.write("broken")
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 1, b"", b"")

        with mock.patch.object(mail_attachments, "SWIFTC", "/usr/bin/true"), \
                mock.patch.object(mail_attachments.subprocess, "run", side_effect=fake):
            with self.assertRaises(MailError) as caught:
                mail_attachments.compile_tool("pdftext", build)
        self.assertEqual(caught.exception.code, "swiftc_failed")
        self.assertEqual(os.listdir(build), [])

    def test_old_work_directories_are_swept_and_recent_ones_kept(self):
        with mock.patch.object(mail_attachments.tempfile, "gettempdir", lambda: self.directory.name):
            old = os.path.join(self.directory.name, "mail-attachments-old")
            recent = os.path.join(self.directory.name, "mail-attachments-recent")
            other = os.path.join(self.directory.name, "unrelated-old")
            for path in (old, recent, other):
                os.mkdir(path)
                open(os.path.join(path, "payload.pdf"), "w").close()
            past = time.time() - 3600
            for path in (old, other):
                os.utime(path, (past, past))
            self.assertEqual(mail_attachments.sweep_workspaces(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))
        self.assertTrue(os.path.exists(other))

    def test_a_sync_sweeps_before_it_starts(self):
        with mock.patch.object(mail_attachments, "sweep_workspaces", return_value=2) as sweep, \
                mock.patch.object(mail_attachments.mail_index, "scan_message_files", return_value={}):
            database = os.path.join(self.directory.name, "attachments.sqlite")
            mail_attachments.sync(self.directory.name, database, log=lambda message: None)
        sweep.assert_called_once()

    def test_the_background_sync_child_is_reaped(self):
        path = os.path.join(self.directory.name, "attachments.sqlite")
        open(path, "w").close()
        child = mock.Mock()
        waited = threading.Event()
        child.wait.side_effect = lambda: waited.set()
        with mock.patch.object(mail_attachments, "database_path", lambda: path), \
                mock.patch.object(mail_attachments.subprocess, "Popen", return_value=child):
            self.assertTrue(mail_attachments.start_background_sync())
        self.assertTrue(waited.wait(5))

    def test_a_doc_too_large_or_too_verbose_is_bounded(self):
        path = os.path.join(self.directory.name, "big.doc")
        with open(path, "wb") as handle:
            handle.write(b"x" * 100)
        with mock.patch.object(mail_attachments, "DOC_MAX_BYTES", 10):
            with self.assertRaises(RuntimeError):
                mail_attachments.extract_doc(path)
        # A converter that never stops writing is cut at the cap.
        fake = subprocess.Popen(["/usr/bin/yes", "word"], stdout=subprocess.PIPE)
        self.addCleanup(fake.kill)
        with mock.patch.object(mail_attachments.subprocess, "Popen", return_value=fake):
            text = mail_attachments.extract_doc(path, limit=1000)
        self.assertEqual(len(text), 1000)
        fake.wait()


class AttachmentToolRunnerTests(unittest.TestCase):
    """run_batch against a stand-in tool, so no Swift and no real file is needed."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.tool = os.path.join(self.directory.name, "tool.sh")
        with open(self.tool, "w") as handle:
            handle.write(
                "#!/bin/sh\n"
                'for f in "$@"; do\n'
                '  case "$f" in --*) continue;; esac\n'
                '  case "$f" in *hang*) exec sleep 30;; *crash*) exit 3;; esac\n'
                '  printf \'{"path": "%s", "pages": 1, "text": "ok", "error": null}\\n\' "$f"\n'
                "done\n"
            )
        os.chmod(self.tool, 0o755)

    def test_every_file_gets_a_result_even_when_the_tool_hangs_or_dies(self):
        with mock.patch.object(mail_attachments, "TOOL_START_TIMEOUT", 8), \
                mock.patch.object(mail_attachments, "TOOL_STALL_TIMEOUT", 8):
            results = mail_attachments.run_batch(self.tool, ["a", "hang", "b", "crash", "c"])
        self.assertEqual([results[name]["error"] for name in ("a", "hang", "b", "crash", "c")],
                         [None, "tool_failed", None, "tool_failed", None])

    def test_a_missing_swift_compiler_is_explained(self):
        with mock.patch.object(mail_attachments, "SWIFTC", "/nonexistent/swiftc"), \
                mock.patch.object(mail_attachments, "BUILD_DIR", self.directory.name):
            with self.assertRaises(MailError) as caught:
                mail_attachments.compile_tool("pdftext", self.directory.name)
        self.assertEqual(caught.exception.code, "swiftc_missing")
        self.assertIn("xcode-select", caught.exception.hint)


class OfficeExtractionTests(unittest.TestCase):
    """docx / xlsx / pptx text extraction, on files generated here."""

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    A = "http://schemas.openxmlformats.org/drawingml/2006/main"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def make(self, name: str, members: dict[str, str]) -> str:
        import zipfile

        path = os.path.join(self.directory.name, name)
        with zipfile.ZipFile(path, "w") as archive:
            for member, xml in members.items():
                archive.writestr(member, xml)
        return path

    def test_docx_paragraphs_become_lines(self):
        path = self.make("a.docx", {"word/document.xml": (
            f'<w:document xmlns:w="{self.W}"><w:body>'
            "<w:p><w:r><w:t>Quote for Example Ltd</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Total </w:t></w:r><w:r><w:t>1200</w:t></w:r></w:p>"
            "</w:body></w:document>")})
        self.assertEqual(mail_attachments.extract_docx(path).split(), ["Quote", "for", "Example", "Ltd", "Total", "1200"])
        self.assertIn("\nTotal 1200", mail_attachments.extract_docx(path))

    def test_xlsx_reads_shared_and_inline_strings_but_not_numbers(self):
        path = self.make("a.xlsx", {
            "xl/sharedStrings.xml": f'<sst xmlns="{self.S}"><si><t>Invoice</t></si><si><r><t>Jane </t></r><r><t>Doe</t></r></si></sst>',
            "xl/worksheets/sheet1.xml": (
                f'<worksheet xmlns="{self.S}"><sheetData><row>'
                '<c t="s"><v>0</v></c><c><v>42</v></c>'
                '<c t="inlineStr"><is><t>Inline note</t></is></c>'
                "</row></sheetData></worksheet>"),
        })
        text = mail_attachments.extract_xlsx(path)
        self.assertIn("Invoice", text)
        self.assertIn("Jane Doe", text)
        self.assertIn("Inline note", text)
        self.assertNotIn("42", text)

    def test_pptx_slides_are_read_in_numeric_order(self):
        slide = lambda word: f'<p:sld xmlns:p="x" xmlns:a="{self.A}"><a:p><a:r><a:t>{word}</a:t></a:r></a:p></p:sld>'
        path = self.make("a.pptx", {
            "ppt/slides/slide10.xml": slide("tenth"),
            "ppt/slides/slide2.xml": slide("second"),
        })
        self.assertEqual(mail_attachments.extract_pptx(path).split(), ["second", "tenth"])

    def test_the_character_cap_is_applied(self):
        body = "".join(f"<w:p><w:r><w:t>{'word ' * 50}</w:t></w:r></w:p>" for _ in range(100))
        path = self.make("big.docx", {"word/document.xml": f'<w:document xmlns:w="{self.W}"><w:body>{body}</w:body></w:document>'})
        self.assertEqual(len(mail_attachments.extract_docx(path, limit=500)), 500)

    def test_a_corrupt_file_raises_instead_of_returning_garbage(self):
        path = os.path.join(self.directory.name, "bad.docx")
        with open(path, "wb") as handle:
            handle.write(b"not a zip")
        with self.assertRaises(Exception):
            mail_attachments.extract_docx(path)


class SavedSearchTests(unittest.TestCase):
    def setUp(self):
        import shutil

        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = os.path.join(self.folder, "nested", "saved.json")
        patcher = mock.patch.object(mail_saved, "storage_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_file_is_an_empty_list(self):
        self.assertEqual(mail_saved.list_all(), {"ok": True, "count": 0, "saved_searches": []})

    def test_save_list_show_delete(self):
        first = mail_saved.save("Unread from Jane", "Weekly check", query="is:unread from:jane@example.com", limit=5)
        self.assertFalse(first["replaced"])
        mail_saved.save("Budget", query="budget", account="Work", sort="date")
        listed = mail_saved.list_all()
        self.assertEqual([item["name"] for item in listed["saved_searches"]], ["Budget", "Unread from Jane"])
        shown = mail_saved.show("unread from jane")["saved_search"]
        self.assertEqual(shown["params"], {"query": "is:unread from:jane@example.com", "limit": 5})
        self.assertEqual(shown["description"], "Weekly check")
        self.assertEqual(mail_saved.delete("BUDGET")["deleted"], "Budget")
        self.assertEqual(mail_saved.list_all()["count"], 1)
        with self.assertRaises(MailError) as caught:
            mail_saved.show("Budget")
        self.assertEqual(caught.exception.code, "saved_search_not_found")

    def test_replace_is_case_insensitive_and_keeps_created(self):
        mail_saved.save("Invoices", query="invoice")
        created = mail_saved.show("Invoices")["saved_search"]["created"]
        again = mail_saved.save("INVOICES", query="invoice OR bill", unread_only=True)
        self.assertTrue(again["replaced"])
        self.assertEqual(mail_saved.list_all()["count"], 1)
        entry = mail_saved.show("invoices")["saved_search"]
        self.assertEqual(entry["created"], created)
        self.assertEqual(entry["name"], "INVOICES")
        self.assertEqual(entry["params"], {"query": "invoice OR bill", "unread_only": True})

    def test_name_and_parameter_validation(self):
        for bad in ("", "   ", None, "-flag", "a/b", "x" * 65, "semi;colon"):
            with self.assertRaises(MailError) as caught:
                mail_saved.save(bad, query="x")
            self.assertEqual(caught.exception.code, "invalid_name", bad)
        for parameters in ({"query": " "}, {"query": "x", "limit": True},
                           {"query": "x", "sort": "random"}, {"query": "x", "unread_only": "yes"}):
            with self.assertRaises(MailError) as caught:
                mail_saved.save("Ok", **parameters)
            self.assertEqual(caught.exception.code, "invalid_parameter", parameters)
        self.assertFalse(os.path.exists(self.path))

    def test_accented_names_are_accepted(self):
        mail_saved.save("Factures à relancer", query="facture")
        self.assertEqual(mail_saved.show("factures à relancer")["saved_search"]["name"], "Factures à relancer")

    def test_run_passes_stored_parameters_and_overrides(self):
        mail_saved.save("Weekly", query="is:unread newer_than:7d", account="Work", limit=50)
        with mock.patch.object(mail_search, "search_all", return_value={"ok": True, "results": []}) as search:
            answer = mail_saved.run("weekly", limit=5, sort="date")
        self.assertEqual(answer, {"ok": True, "results": []})
        search.assert_called_once_with(query="is:unread newer_than:7d", account="Work", limit=5, sort="date")

    def test_relative_date_operator_is_stored_as_written(self):
        mail_saved.save("Recent", query="newer_than:7d from:@example.com")
        with open(self.path, encoding="utf-8") as handle:
            raw = handle.read()
        self.assertIn("newer_than:7d", raw)
        with mock.patch.object(mail_search, "search_all", return_value={"ok": True}) as search:
            mail_saved.run("Recent")
        self.assertEqual(search.call_args.kwargs["query"], "newer_than:7d from:@example.com")

    def test_write_is_atomic(self):
        mail_saved.save("Keep", query="one")
        with open(self.path, encoding="utf-8") as handle:
            before = handle.read()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                mail_saved.save("Other", query="two")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(os.listdir(os.path.dirname(self.path)), ["saved.json"])

    def test_corrupt_file_is_a_clear_error_and_is_not_overwritten(self):
        os.makedirs(os.path.dirname(self.path))
        for content in ("{ not json", '{"searches": "nope"}', "[]"):
            with open(self.path, "w", encoding="utf-8") as handle:
                handle.write(content)
            for call in (mail_saved.list_all, lambda: mail_saved.save("A", query="x")):
                with self.assertRaises(MailError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "saved_searches_corrupt")
            with open(self.path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), content)

    def test_run_rejects_a_bad_stored_parameter(self):
        os.makedirs(os.path.dirname(self.path))
        bad = {"name": "Bad", "params": {"query": "x", "limit": "5"}}
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "searches": [bad]}, handle)
        with self.assertRaises(MailError) as caught:
            mail_saved.run("Bad")
        self.assertEqual(caught.exception.code, "saved_searches_corrupt")
        self.assertIn("'Bad'", caught.exception.message)

    def test_run_false_overrides_stored_true_and_limit_zero_is_accepted(self):
        mail_saved.save("Flags", query="x", unread_only=True, limit=0)
        with mock.patch.object(mail_search, "search_all", return_value={"ok": True}) as search:
            mail_saved.run("Flags", unread_only=False)
        self.assertEqual(search.call_args.kwargs, {"query": "x", "unread_only": False, "limit": 0})


class SavedSearchPathTests(unittest.TestCase):
    def test_default_path_is_beside_the_index(self):
        environment = {"MAIL_MCP_INDEX_PATH": "/scratch/index.sqlite", "MAIL_MCP_SAVED_SEARCHES_PATH": ""}
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(mail_saved.storage_path(), "/scratch/saved_searches.json")
        environment["MAIL_MCP_SAVED_SEARCHES_PATH"] = "/elsewhere/mine.json"
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(mail_saved.storage_path(), "/elsewhere/mine.json")


# --------------------------------------------------------------------------
# Search by meaning (MCPMAILMAC-12)
# --------------------------------------------------------------------------

# Words the fake model treats as one concept, so "strategy" is close to "roadmap"
# without sharing a letter: the whole point of a semantic search.
_CONCEPTS = {"strategy": "C_plan", "roadmap": "C_plan", "plan": "C_plan", "agenda": "C_plan",
             "money": "C_cash", "budget": "C_cash", "finance": "C_cash", "cost": "C_cash"}


class FakeEmbedder:
    """Deterministic stand-in for Ollama: a hashed bag of concepts, no network."""

    model = "fake"
    DIMENSIONS = 512

    def __init__(self):
        self.calls: list[list[str]] = []
        self.fail: EmbedderError | None = None

    def vector(self, text: str) -> list[float]:
        import zlib

        values = [0.0] * self.DIMENSIONS
        for word in text.lower().replace("\n", " ").split():
            token = _CONCEPTS.get(word.strip(".,"), word.strip(".,"))
            values[zlib.crc32(token.encode()) % self.DIMENSIONS] += 1.0
        return values

    def embed(self, texts, timeout=None):
        self.calls.append(list(texts))
        if self.fail is not None:
            raise self.fail
        return [self.vector(text) for text in texts]

    def reachable(self, timeout=1.0):
        return self.fail is None


EmbedderError = mail_vectors.EmbedderError


class ChunkingTests(unittest.TestCase):
    def test_a_short_text_is_one_chunk(self):
        self.assertEqual(mail_vectors.split_text("A short note."), ["A short note."])

    def test_an_empty_text_has_no_chunk(self):
        self.assertEqual(mail_vectors.split_text("   "), [])

    def test_a_long_text_is_cut_with_overlap_and_no_word_is_split(self):
        text = " ".join(f"word{number}" for number in range(600))
        chunks = mail_vectors.split_text(text, size=200, overlap=40, limit=100)
        self.assertGreater(len(chunks), 3)
        words = set(text.split())
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 200 + mail_vectors.MIN_TAIL_CHARS)
            self.assertTrue(set(chunk.split()) <= words)
        for before, after in zip(chunks, chunks[1:]):
            self.assertTrue(set(before.split()[-3:]) & set(after.split()[:12]), "chunks must overlap")

    def test_every_word_of_a_text_within_the_cap_is_covered(self):
        text = " ".join(f"word{number}" for number in range(300))
        chunks = mail_vectors.split_text(text, size=200, overlap=40, limit=100)
        self.assertEqual({word for chunk in chunks for word in chunk.split()}, set(text.split()))

    def test_the_number_of_chunks_is_capped(self):
        text = "Sentence number one is here. " * 2000
        self.assertEqual(len(mail_vectors.split_text(text)), mail_vectors.MAX_CHUNKS)

    def test_a_cut_prefers_the_end_of_a_sentence(self):
        text = ("First sentence of the note. " * 40).strip()
        chunks = mail_vectors.split_text(text, size=300, overlap=50, limit=10)
        self.assertTrue(chunks[0].endswith("."))

    def test_a_tiny_tail_joins_the_last_chunk(self):
        text = "a " * 495 + "end"
        chunks = mail_vectors.split_text(text.strip(), size=500, overlap=100, limit=10)
        self.assertTrue(chunks[-1].endswith("end"))
        self.assertGreater(len(chunks[-1]), mail_vectors.MIN_TAIL_CHARS)

    def test_links_and_long_tokens_are_not_embedded(self):
        cleaned = mail_vectors.clean_text("See https://example.com/a?b=1 and www.example.org now " + "x" * 80)
        self.assertEqual(cleaned, "See and now")

    def test_the_subject_travels_with_the_first_chunk_only(self):
        chunks = mail_vectors.message_chunks("Budget review", " ".join(["body"] * 600))
        self.assertTrue(chunks[0].embed.startswith("Budget review\n"))
        self.assertNotIn("Budget", chunks[1].embed)
        self.assertNotIn("Budget", chunks[0].text)

    def test_a_message_without_a_body_is_still_embedded_by_its_subject(self):
        chunks = mail_vectors.message_chunks("Budget review", "")
        self.assertEqual([(chunk.source, chunk.embed) for chunk in chunks], [("subject", "Budget review")])
        self.assertEqual(mail_vectors.message_chunks("", ""), [])

    def test_the_hash_follows_the_text(self):
        self.assertEqual(mail_vectors.text_hash("a", "b"), mail_vectors.text_hash("a", "b"))
        self.assertNotEqual(mail_vectors.text_hash("a", "b"), mail_vectors.text_hash("a", "c"))


class VectorMathTests(unittest.TestCase):
    def test_quantising_keeps_the_direction(self):
        vector = [0.1, -0.3, 0.25, 0.0]
        self.assertGreater(mail_vectors.cosine(mail_vectors.quantize(vector), mail_vectors.quantize([x * 7 for x in vector])), 0.9999)

    def test_opposite_vectors_score_minus_one(self):
        self.assertAlmostEqual(mail_vectors.cosine(mail_vectors.quantize([1, 2, 3]), mail_vectors.quantize([-1, -2, -3])), -1, places=2)

    def test_a_zero_vector_scores_zero(self):
        self.assertEqual(mail_vectors.cosine(mail_vectors.quantize([0, 0]), mail_vectors.quantize([1, 2])), 0.0)

    def test_the_python_similarity_agrees_with_sqlite_vec(self):
        connection = sqlite3.connect(":memory:")
        if not mail_vectors.load_sqlite_vec(connection):
            self.skipTest("sqlite-vec is not installed")
        rng = random.Random(4)
        left = mail_vectors.quantize([rng.gauss(0, 1) for _ in range(64)])
        right = mail_vectors.quantize([rng.gauss(0, 1) for _ in range(64)])
        native = 1.0 - connection.execute(
            "SELECT vec_distance_cosine(vec_int8(?), vec_int8(?))", (left, right)).fetchone()[0]
        self.assertAlmostEqual(native, mail_vectors.cosine(left, right), places=4)


class _VectorsMixin(_FictionalIndexMixin):
    """The fictional index plus a vectors file built with the fake embedder."""

    BODIES = {1: "the roadmap is discussed here", 2: "see you there", 3: "roadmap",
              4: "quarterly budget figures", 5: "quarterly budget figures", 6: "quarterly budget figures"}

    def setUp(self):
        super().setUp()
        self.vectors_path = os.path.join(self.directory.name, "vectors.sqlite")
        self.embedder = FakeEmbedder()
        for patcher in (
            mock.patch.object(mail_vectors, "database_path", lambda: self.vectors_path),
            mock.patch.object(mail_vectors, "default_embedder", lambda: self.embedder),
            mock.patch.dict(os.environ, {"MAIL_MCP_EMBEDDING_MODEL": "fake"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        mail_vectors.reset_state()
        self.addCleanup(mail_vectors.reset_state)

    def build(self, **kwargs):
        options = dict(files={number: str(number) for number in self.BODIES},
                       read_body=lambda path: self.BODIES[int(path)], embedder=self.embedder)
        options.update(kwargs)
        return mail_vectors.sync(None, self.vectors_path, self.path, **options)

    def search(self, query, **kwargs):
        return mail_search.search_all(query, max_age_minutes=10**9, **kwargs)


class VectorSyncTests(_VectorsMixin, unittest.TestCase):
    def test_a_build_embeds_every_message_once(self):
        result = self.build()
        self.assertEqual((result.embedded, result.chunks), (6, 6))
        self.assertEqual(sum(len(call) for call in self.embedder.calls), 6)

    def test_the_first_chunk_carries_the_subject(self):
        self.build()
        sent = [text for call in self.embedder.calls for text in call]
        self.assertIn("Kickoff\nroadmap", sent)

    def test_a_rerun_embeds_nothing_more(self):
        self.build()
        self.embedder.calls.clear()
        self.assertEqual(self.build().embedded, 0)
        self.assertEqual(self.embedder.calls, [])

    def test_an_interrupted_run_resumes_where_it_stopped(self):
        self.build(limit=2)
        self.embedder.calls.clear()
        result = self.build()
        self.assertEqual(result.embedded, 4)
        self.assertEqual(sum(len(call) for call in self.embedder.calls), 4)

    def test_a_server_that_goes_down_keeps_what_was_stored(self):
        first = FakeEmbedder()
        original = first.embed

        def flaky(texts, timeout=None):
            if len(first.calls) >= 2:
                first.fail = EmbedderError("down")
            return original(texts, timeout)

        first.embed = flaky
        with self.assertRaises(EmbedderError):
            self.build(embedder=first, batch=1)
        stored = sqlite3.connect(self.vectors_path).execute("SELECT count(*) FROM messages").fetchone()[0]
        self.assertEqual(stored, 2)
        self.assertEqual(self.build().embedded, 4)
        report = mail_vectors.status(6, self.vectors_path, probe=False)
        self.assertTrue(report["last_run_complete"])

    def test_a_run_cut_short_by_another_error_is_not_complete(self):
        with mock.patch.object(mail_index, "scan_message_files", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                mail_vectors.sync("/store", self.vectors_path, self.path, embedder=self.embedder)
        self.assertFalse(mail_vectors.status(6, self.vectors_path, probe=False)["last_run_complete"])

    def test_an_all_zero_vector_is_not_stored(self):
        class Zero(FakeEmbedder):
            def vector(self, text):
                return [0.0] * 8 if "Kickoff" in text else super().vector(text)

        self.assertEqual(self.build(embedder=Zero()).skipped, 1)

    def test_the_lock_is_released_after_a_failure(self):
        self.embedder.fail = EmbedderError("down")
        with self.assertRaises(EmbedderError):
            self.build()
        self.assertFalse(os.path.exists(self.vectors_path + ".sync.lock"))

    def test_a_second_run_at_the_same_time_is_refused(self):
        with mail_index.IndexLock(self.vectors_path):
            with self.assertRaises(mail_index.IndexBusy):
                self.build()

    def test_the_vectors_of_a_vanished_message_are_deleted(self):
        self.build()
        index = sqlite3.connect(self.path)
        index.execute("DELETE FROM messages WHERE id = 3")
        index.commit()
        index.close()
        self.assertEqual(self.build().removed, 1)
        with sqlite3.connect(self.vectors_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM chunks WHERE message = 3").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 5)

    def test_verify_embeds_again_only_what_changed(self):
        self.build()
        self.embedder.calls.clear()
        self.BODIES[3] = "a completely different text"
        try:
            result = self.build(verify=True)
        finally:
            self.BODIES[3] = "roadmap"
        self.assertEqual(result.embedded, 1)
        self.assertEqual(len(self.embedder.calls), 1)
        with sqlite3.connect(self.vectors_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM chunks WHERE message = 3").fetchone()[0], 1)

    def test_a_sample_embeds_a_reproducible_subset(self):
        self.build(sample=(50.0, 1))
        again = {row[0] for row in sqlite3.connect(self.vectors_path).execute("SELECT message FROM messages")}
        self.assertEqual(again, {i for i in range(1, 7) if mail_vectors._sample(i, 1, 50.0)})
        self.assertFalse(mail_vectors.status(6, self.vectors_path, probe=False)["last_run_complete"])

    def test_a_model_change_needs_a_rebuild(self):
        self.build()
        with mock.patch.dict(os.environ, {"MAIL_MCP_EMBEDDING_MODEL": "other"}):
            with self.assertRaises(MailError) as caught:
                mail_vectors.open_database(self.vectors_path)
        self.assertEqual(caught.exception.code, "vectors_outdated")

    def test_a_rebuild_starts_from_nothing(self):
        self.build()
        self.embedder.calls.clear()
        self.assertEqual(self.build(rebuild=True).embedded, 6)

    def test_a_refused_batch_is_retried_text_by_text(self):
        class Picky(FakeEmbedder):
            def embed(self, texts, timeout=None):
                if len(texts) > 1 or "Kickoff" in texts[0]:
                    raise EmbedderError("bad input", transient=False)
                return super().embed(texts)

        result = self.build(embedder=Picky(), batch=6)
        self.assertEqual(result.embedded, 6)
        self.assertGreaterEqual(result.skipped, 1)

    def test_the_status_reports_coverage_and_freshness(self):
        self.build(limit=3)
        partial = mail_vectors.status(6, self.vectors_path, probe=False)
        self.assertEqual((partial["messages"], partial["coverage"], partial["fresh"]), (3, 0.5, False))
        self.build()
        full = mail_vectors.status(6, self.vectors_path, probe=False)
        self.assertEqual((full["coverage"], full["fresh"], full["model"]), (1.0, True, "fake"))
        self.assertEqual(mail_vectors.status(6, self.vectors_path + ".none")["built"], False)


class SemanticSearchTests(_VectorsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.build()

    def ids(self, query, **kwargs):
        return [message["mail_id"] for message in self.search(query, **kwargs)["messages"]]

    def test_keyword_is_the_default_and_ignores_the_vectors(self):
        result = self.search("roadmap")
        self.assertEqual(result["mode"], "keyword")
        self.assertEqual(self.ids("strategy"), [])
        self.assertTrue(all(len(call) > 1 or call[0] != "strategy" for call in self.embedder.calls))

    def test_semantic_finds_a_message_that_shares_no_word_with_the_query(self):
        result = self.search("strategy", mode="semantic")
        self.assertEqual(result["mode"], "semantic")
        self.assertEqual(self.ids("strategy", mode="semantic")[0], 3)
        found = {message["mail_id"]: message for message in result["messages"]}
        self.assertEqual(found[3]["match"], "semantic")
        self.assertGreater(found[3]["similarity"], 0.5)

    def test_hybrid_ranks_a_message_found_both_ways_first(self):
        result = self.search("roadmap", mode="hybrid")
        first = result["messages"][0]
        self.assertEqual(first["match"], "both")
        self.assertIn(first["mail_id"], (2, 3))
        matches = {message["mail_id"]: message["match"] for message in result["messages"]}
        self.assertEqual(matches[4], "semantic")  # "Shared plan": no keyword hit

    def test_hybrid_reaches_a_semantic_only_message_keyword_misses(self):
        self.assertEqual(self.ids("strategy"), [])
        self.assertIn(3, self.ids("strategy", mode="hybrid"))

    def test_rrf_scores_are_the_sum_over_both_lists(self):
        hits = [mail_vectors.Hit(2, 0.9, 0), mail_vectors.Hit(1, 0.8, 0)]
        connection = mail_search._connect()
        try:
            keyword = [connection.execute("SELECT id, account, subject, sender, date_received, rfc_id,"
                                          " has_attachment, is_bulk FROM messages WHERE id = ?", (identifier,)).fetchone()
                       for identifier in (1, 3)]
            fused = mail_search._fuse(connection, keyword, hits, "hybrid", [], [], "", 10, "relevance")
        finally:
            connection.close()
        by_id = {entry["id"]: entry for entry in fused}
        k, weight = mail_search.RRF_K, mail_search.RRF_SEMANTIC_WEIGHT
        self.assertAlmostEqual(by_id[1]["rrf"], 1 / (k + 1) + weight / (k + 2))
        self.assertAlmostEqual(by_id[3]["rrf"], 1 / (k + 2))
        self.assertAlmostEqual(by_id[2]["rrf"], weight / (k + 1))
        self.assertEqual([entry["id"] for entry in fused], [1, 3, 2])
        self.assertEqual((by_id[1]["match"], by_id[2]["match"], by_id[3]["match"]), ("both", "semantic", "keyword"))

    def test_with_equal_weights_the_lists_count_alike(self):
        hits = [mail_vectors.Hit(2, 0.9, 0)]
        connection = mail_search._connect()
        try:
            keyword = [connection.execute("SELECT id, account, subject, sender, date_received, rfc_id,"
                                          " has_attachment, is_bulk FROM messages WHERE id = 1").fetchone()]
            with mock.patch.object(mail_search, "RRF_SEMANTIC_WEIGHT", 1.0):
                fused = mail_search._fuse(connection, keyword, hits, "hybrid", [], [], "", 10, "relevance")
        finally:
            connection.close()
        self.assertAlmostEqual(fused[0]["rrf"], fused[1]["rrf"])

    def test_a_date_sort_orders_the_fused_top_by_date(self):
        ranked = self.ids("roadmap", mode="hybrid", limit=6)
        by_date = self.ids("roadmap", mode="hybrid", limit=6, sort="date")
        self.assertEqual(sorted(ranked), sorted(by_date))
        dates = {row[0]: row[1] or 0 for row in sqlite3.connect(self.path).execute("SELECT id, date_received FROM messages")}
        self.assertEqual(by_date, sorted(by_date, key=lambda identifier: -dates[identifier]))

    def test_a_message_is_scored_by_its_best_chunk(self):
        with sqlite3.connect(self.vectors_path) as connection:
            connection.execute("DELETE FROM chunks WHERE message = 1")
            for number, text in enumerate(("weather forecast", "roadmap strategy", "lunch menu")):
                connection.execute(
                    "INSERT INTO chunks (message, chunk, source, vector) VALUES (1, ?, 'body', ?)",
                    (number, mail_vectors.quantize(self.embedder.vector(text))))
        hits = mail_vectors.search_hits(self.embedder.vector("strategy"), model="fake")
        ones = [hit for hit in hits if hit.message == 1]
        self.assertEqual(len(ones), 1)
        self.assertEqual(ones[0].chunk, 1)
        self.assertEqual([hit.score for hit in hits], sorted((hit.score for hit in hits), reverse=True))

    def test_operators_filter_semantic_hits(self):
        self.assertNotIn(3, self.ids("strategy from:john@example.org", mode="semantic"))
        only_jane = self.ids("strategy from:jane@example.com", mode="semantic")
        self.assertTrue(only_jane and 2 not in only_jane)
        self.assertEqual(self.ids("strategy from:nobody@example.com", mode="semantic"), [])

    def test_a_narrow_filter_is_applied_before_the_candidate_cap(self):
        # Only message 2 passes the filter, and it is far from the best match for
        # the query: a cap of one chunk applied first would lose it.
        with mock.patch.object(mail_search, "SEMANTIC_CHUNKS", 1):
            self.assertEqual(self.ids("strategy from:john@example.org", mode="semantic"), [2])

    def test_dates_and_accounts_filter_semantic_hits(self):
        self.assertEqual(self.ids("strategy", mode="semantic", since="2999-01-01"), [])
        self.assertEqual(self.ids("strategy", mode="semantic", account="Home"), [])
        self.assertTrue(self.ids("strategy", mode="semantic", account="Work"))
        self.assertIn(3, self.ids("strategy older_than:1000d", mode="semantic"))
        self.assertNotIn(3, self.ids("strategy newer_than:1000d", mode="semantic"))

    def test_too_many_ids_to_pass_along_still_filters_afterwards(self):
        with mock.patch.object(mail_search, "ATTACHMENT_ID_LIMIT", 0):
            self.assertEqual(self.ids("strategy from:john@example.org", mode="semantic"), [2])

    def test_a_semantic_only_hit_has_an_excerpt_of_the_chunk(self):
        body = "the roadmap for the year: " + "details " * 60
        with mock.patch.object(mail_index, "find_store", return_value="/store"), \
                mock.patch.object(mail_index, "find_message_file", return_value="/f"), \
                mock.patch.object(mail_index, "extract_message",
                                  return_value=mail_index.Extracted("", body, True, "", False)):
            result = self.search("strategy", mode="semantic", limit=1)
        snippet = result["messages"][0]["snippet"]
        self.assertTrue(snippet.startswith("the roadmap for the year"))
        self.assertLessEqual(len(snippet), 202)
        self.assertNotIn("snippet", self.search("strategy", mode="semantic", snippets=False)["messages"][0])

    def test_a_query_is_reduced_to_plain_words_for_the_model(self):
        self.search('subject: "strategy" AND (roadmap OR plan) NOT draft from:jane@example.com', mode="semantic")
        self.assertEqual(self.embedder.calls[-1], ["strategy roadmap plan"])
        self.assertEqual(mail_search.semantic_text("{to}: jane agenda"), "jane agenda")

    def test_embedding_the_same_query_twice_asks_the_server_once(self):
        self.embedder.calls.clear()
        self.search("strategy", mode="semantic")
        self.search("strategy", mode="semantic")
        self.assertEqual(len(self.embedder.calls), 1)

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(MailError) as caught:
            self.search("strategy", mode="magic")
        self.assertEqual(caught.exception.code, "invalid_mode")

    def test_semantic_needs_some_text(self):
        with self.assertRaises(MailError) as caught:
            self.search("from:jane@example.com", mode="semantic")
        self.assertEqual(caught.exception.code, "semantic_needs_text")

    def test_hybrid_on_operators_alone_is_a_plain_search(self):
        result = self.search("from:jane@example.com", mode="hybrid")
        self.assertEqual(result["mode"], "keyword")
        self.assertNotIn("semantic_note", result)


class SemanticFallbackTests(_VectorsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.build()
        mail_vectors.reset_state()
        self.embedder.calls.clear()

    def test_semantic_without_ollama_is_an_error_with_a_hint(self):
        self.embedder.fail = EmbedderError("Ollama is not reachable", "Start it.")
        with self.assertRaises(MailError) as caught:
            self.search("strategy", mode="semantic")
        self.assertEqual(caught.exception.code, "semantic_unavailable")
        self.assertEqual(caught.exception.hint, "Start it.")

    def test_hybrid_without_ollama_answers_with_keywords_and_says_so(self):
        self.embedder.fail = EmbedderError("Ollama is not reachable")
        result = self.search("roadmap", mode="hybrid")
        self.assertEqual(result["mode"], "keyword")
        self.assertIn("semantic_unavailable", result["semantic_note"])
        self.assertEqual([m["mail_id"] for m in result["messages"]], [2, 3, 1])

    def test_a_date_sorted_hybrid_that_falls_back_is_the_plain_date_search(self):
        self.embedder.fail = EmbedderError("down")
        self.assertEqual([m["mail_id"] for m in self.search("roadmap", mode="hybrid", sort="date")["messages"]], [1, 2, 3])

    def test_a_failure_is_not_retried_for_a_minute(self):
        self.embedder.fail = EmbedderError("down")
        self.search("roadmap", mode="hybrid")
        self.search("plan", mode="hybrid")
        self.assertEqual(len(self.embedder.calls), 1)
        mail_vectors.reset_state()
        self.embedder.fail = None
        self.assertEqual(self.search("roadmap", mode="hybrid")["mode"], "hybrid")

    def test_a_refused_input_is_not_a_reason_to_stop_asking(self):
        self.embedder.fail = EmbedderError("bad input", transient=False)
        self.search("roadmap", mode="hybrid")
        self.search("plan", mode="hybrid")
        self.assertEqual(len(self.embedder.calls), 2)

    def test_without_the_vectors_file_hybrid_falls_back_and_semantic_fails(self):
        os.unlink(self.vectors_path)
        result = self.search("roadmap", mode="hybrid")
        self.assertEqual(result["mode"], "keyword")
        self.assertIn("vectors_missing", result["semantic_note"])
        with self.assertRaises(MailError) as caught:
            self.search("roadmap", mode="semantic")
        self.assertEqual(caught.exception.code, "vectors_missing")
        self.assertIn("mail_vectors.py --build", caught.exception.hint)

    def test_a_vectors_file_of_another_model_is_not_used(self):
        with mock.patch.dict(os.environ, {"MAIL_MCP_EMBEDDING_MODEL": "other"}):
            result = self.search("roadmap", mode="hybrid")
        self.assertEqual(result["mode"], "keyword")
        self.assertIn("vectors_outdated", result["semantic_note"])

    def test_an_unreadable_vectors_file_does_not_break_search(self):
        with open(self.vectors_path, "wb") as handle:
            handle.write(b"this is not a database" * 100)
        result = self.search("roadmap", mode="hybrid")
        self.assertEqual([m["mail_id"] for m in result["messages"]], [2, 3, 1])
        self.assertIn("semantic_note", result)

    def test_without_sqlite_vec_a_small_search_still_works_in_python(self):
        with mock.patch.object(mail_vectors, "load_sqlite_vec", return_value=False):
            found = self.search("strategy", mode="semantic")
        self.assertEqual(found["messages"][0]["mail_id"], 3)

    def test_without_sqlite_vec_a_big_search_is_refused_with_a_hint(self):
        with mock.patch.object(mail_vectors, "load_sqlite_vec", return_value=False), \
                mock.patch.object(mail_vectors, "PYTHON_FALLBACK_CHUNKS", 2):
            with self.assertRaises(MailError) as caught:
                self.search("strategy", mode="semantic")
            self.assertEqual(caught.exception.code, "semantic_needs_sqlite_vec")
            self.assertIn("pip install sqlite-vec", caught.exception.hint)
            hybrid = self.search("roadmap", mode="hybrid")
        self.assertEqual(hybrid["mode"], "keyword")
        self.assertIn("semantic_needs_sqlite_vec", hybrid["semantic_note"])

    def test_a_null_similarity_from_sqlite_vec_is_skipped(self):
        with sqlite3.connect(self.vectors_path) as connection:
            connection.execute("UPDATE chunks SET vector = zeroblob(512) WHERE message = 3")
        hits = mail_vectors.search_hits(self.embedder.vector("strategy"), model="fake")
        self.assertNotIn(3, [hit.message for hit in hits])
        self.assertEqual(self.search("strategy", mode="hybrid")["mode"], "hybrid")

    def test_an_explicit_hybrid_on_partial_vectors_says_so(self):
        with sqlite3.connect(self.vectors_path) as connection:
            connection.execute("DELETE FROM messages WHERE message > 2")
        note = self.search("roadmap", mode="hybrid")["semantic_note"]
        self.assertIn("cover only 33%", note)

    def test_the_python_and_native_rankings_agree(self):
        connection = sqlite3.connect(":memory:")
        if not mail_vectors.load_sqlite_vec(connection):
            self.skipTest("sqlite-vec is not installed")
        query = self.embedder.vector("strategy budget")
        native = mail_vectors.search_hits(query, model="fake")
        with mock.patch.object(mail_vectors, "load_sqlite_vec", return_value=False):
            python = mail_vectors.search_hits(query, model="fake")
        self.assertEqual([hit.message for hit in native], [hit.message for hit in python])


class SemanticDefaultModeTests(_VectorsMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        mail_vectors.reset_state()

    def with_setting(self, value):
        return mock.patch.dict(os.environ, {"MAIL_MCP_SEARCH_MODE": value})

    def test_auto_without_vectors_is_keyword_and_silent(self):
        with self.with_setting("auto"):
            result = self.search("roadmap")
        self.assertEqual(result["mode"], "keyword")
        self.assertNotIn("semantic_note", result)
        self.assertEqual(self.embedder.calls, [])

    def test_auto_with_partial_vectors_stays_keyword(self):
        self.build(limit=2)
        with self.with_setting("auto"):
            self.assertEqual(self.search("roadmap")["mode"], "keyword")

    def test_auto_with_fresh_vectors_is_hybrid(self):
        self.build()
        with self.with_setting("auto"):
            self.assertEqual(self.search("roadmap")["mode"], "hybrid")
            self.assertEqual(self.search("roadmap", mode="keyword")["mode"], "keyword")

    def test_auto_needs_sqlite_vec(self):
        self.build()
        with self.with_setting("auto"), mock.patch.object(mail_vectors, "load_sqlite_vec", return_value=False):
            self.assertEqual(self.search("roadmap")["mode"], "keyword")

    def test_an_explicit_mode_beats_the_setting(self):
        self.build()
        with self.with_setting("keyword"):
            self.assertEqual(self.search("roadmap", mode="hybrid")["mode"], "hybrid")

    def test_the_index_status_reports_the_vectors(self):
        self.build()
        report = mail_search.index_status()["vectors"]
        self.assertTrue(report["built"])
        self.assertEqual((report["messages"], report["chunks"], report["coverage"]), (6, 6, 1.0))

    def test_a_message_sync_starts_the_vector_sync_only_when_asked_for_and_built(self):
        with mock.patch.object(mail_vectors.subprocess, "Popen") as popen:
            self.assertFalse(mail_vectors.start_background_sync())  # no file yet
            self.build()
            with mock.patch.dict(os.environ, {"MAIL_MCP_VECTORS_AUTO_SYNC": "0"}):
                self.assertFalse(mail_vectors.start_background_sync())
            self.assertEqual(popen.call_count, 0)
            self.assertTrue(mail_vectors.start_background_sync())
            self.assertEqual(popen.call_count, 1)


class OllamaClientTests(unittest.TestCase):
    """The HTTP client against a throwaway local server that speaks like Ollama."""

    def setUp(self):
        import http.server

        test = self
        self.requests: list[dict] = []
        self.delay = 0.0
        self.status = 200

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.requests.append({"path": self.path, "payload": payload})
                time.sleep(test.delay)
                body = json.dumps({"embeddings": [[1.0, 2.0]] * len(payload["input"])} if test.status == 200
                                  else {"error": "boom"}).encode()
                self.send_response(test.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                body = b'{"version": "0"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def test_texts_go_in_one_request_and_vectors_come_back_in_order(self):
        client = mail_vectors.OllamaEmbedder(self.url, "bge-m3", 5)
        self.assertEqual(client.embed(["a", "b"]), [[1.0, 2.0], [1.0, 2.0]])
        sent = self.requests[0]
        self.assertEqual(sent["path"], "/api/embed")
        self.assertEqual((sent["payload"]["model"], sent["payload"]["input"]), ("bge-m3", ["a", "b"]))
        self.assertTrue(client.reachable())

    def test_the_connection_is_kept_between_requests(self):
        client = mail_vectors.OllamaEmbedder(self.url, "bge-m3", 5)
        client.embed(["a"])
        first = client._connection
        client.embed(["b"])
        self.assertIs(client._connection, first)

    def test_a_dropped_connection_is_reopened(self):
        client = mail_vectors.OllamaEmbedder(self.url, "bge-m3", 5)
        client.embed(["a"])
        client._connection.sock.close()
        self.assertEqual(client.embed(["b"]), [[1.0, 2.0]])

    def test_a_slow_server_is_a_transient_timeout_with_a_hint(self):
        self.delay = 1.0
        client = mail_vectors.OllamaEmbedder(self.url, "bge-m3", 0.2)
        with self.assertRaises(EmbedderError) as caught:
            client.embed(["a"])
        self.assertTrue(caught.exception.transient)
        self.assertIn("ollama_timeout", caught.exception.hint)

    def test_a_server_that_is_not_running_is_reported_with_how_to_start_it(self):
        self.server.shutdown()
        self.server.server_close()
        client = mail_vectors.OllamaEmbedder(self.url, "bge-m3", 1)
        with self.assertRaises(EmbedderError) as caught:
            client.embed(["a"])
        self.assertTrue(caught.exception.transient)
        self.assertIn("ollama", caught.exception.hint.lower())
        self.assertFalse(client.reachable())

    def test_an_error_answer_is_not_transient(self):
        self.status = 500
        client = mail_vectors.OllamaEmbedder(self.url, "bge-m3", 5)
        with self.assertRaises(EmbedderError) as caught:
            client.embed(["a"])
        self.assertFalse(caught.exception.transient)

    def test_the_settings_pick_the_endpoint_and_the_model(self):
        with mock.patch.dict(os.environ, {"MAIL_MCP_OLLAMA_URL": self.url, "MAIL_MCP_EMBEDDING_MODEL": "other"}):
            mail_vectors.reset_state()
            try:
                self.assertEqual(mail_vectors.embed_query("hello"), [1.0, 2.0])
            finally:
                mail_vectors.reset_state()
        self.assertEqual(self.requests[0]["payload"]["model"], "other")


class EndpointChangeTests(unittest.TestCase):
    def test_a_new_endpoint_forgets_the_old_failure(self):
        mail_vectors.reset_state()
        self.addCleanup(mail_vectors.reset_state)
        mail_vectors.default_embedder()
        mail_vectors._down_until = time.time() + 60
        with mock.patch.dict(os.environ, {"MAIL_MCP_OLLAMA_URL": "http://127.0.0.1:1"}):
            mail_vectors.default_embedder()
        self.assertEqual(mail_vectors._down_until, 0.0)


class EvalModeTests(unittest.TestCase):
    def test_run_pairs_hands_the_mode_to_search(self):
        answer = {"messages": [{"mail_id": 7}]}
        with mock.patch.object(mail_search, "search_all", return_value=answer) as search:
            result = mail_eval.run_pairs([{"query": "budget", "expected": 7}], mode="hybrid")
        self.assertEqual(search.call_args.kwargs["mode"], "hybrid")
        self.assertEqual(result["aggregate"]["recall_at_1"], 1.0)

    def test_without_a_mode_the_configured_default_applies(self):
        with mock.patch.object(mail_search, "search_all", return_value={"messages": []}) as search:
            mail_eval.run_pairs([{"query": "budget", "expected": 7}])
        self.assertNotIn("mode", search.call_args.kwargs)

    def test_a_mode_the_search_cannot_serve_stops_the_run_with_its_hint(self):
        error = MailError("semantic_unavailable", "no vectors", "Build them.")
        with mock.patch.object(mail_search, "search_all", side_effect=error):
            with self.assertRaises(mail_eval.EvalError):
                mail_eval.run_pairs([{"query": "budget", "expected": 7}], mode="semantic")


class FindSimilarTests(_VectorsMixin, unittest.TestCase):
    """find_similar (MCPMAILMAC-13): neighbours of a stored message, no embedding call."""

    # 1 and 2 are one conversation; 3 is close to them, 4 further, 5 unrelated,
    # 6 has no body text that gets embedded (its vectors are deleted below).
    BODIES = {1: "roadmap strategy plan", 2: "roadmap strategy plan", 3: "roadmap strategy budget",
              4: "roadmap weather lunch cost", 5: "budget finance", 6: "budget finance"}

    def setUp(self):
        super().setUp()
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE messages SET conversation_id = 7 WHERE id IN (1, 2)")
        self.build()
        with sqlite3.connect(self.vectors_path) as connection:
            connection.execute("DELETE FROM chunks WHERE message = 6")
            connection.execute("DELETE FROM messages WHERE message = 6")
        self.embedder.calls.clear()

    def similar(self, message_id=1, **kwargs):
        return mail_search.find_similar(message_id, **kwargs)

    def ids(self, message_id=1, **kwargs):
        return [message["mail_id"] for message in self.similar(message_id, **kwargs)["messages"]]

    def test_the_message_itself_is_never_returned(self):
        self.assertNotIn(1, self.ids(exclude_thread=False))
        self.assertNotIn(3, self.ids(3))

    def test_the_conversation_is_excluded_by_default_and_kept_on_request(self):
        self.assertNotIn(2, self.ids())
        self.assertEqual(self.ids(exclude_thread=False)[0], 2)

    def test_results_come_closest_first_with_a_score(self):
        result = self.similar()
        self.assertEqual([m["mail_id"] for m in result["messages"]], [3, 4, 5])
        scores = [m["score"] for m in result["messages"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertTrue(all(key in result["messages"][0] for key in ("subject", "sender", "date_received", "message_id")))

    def test_limit_caps_the_list(self):
        self.assertEqual(self.ids(limit=2), [3, 4])

    def test_it_accepts_a_mail_id_a_string_id_and_an_encoded_reference(self):
        reference = mail_search.search_all("roadmap", max_age_minutes=10**9)["messages"][0]
        self.assertEqual(self.ids(reference["message_id"]), self.ids(reference["mail_id"]))
        self.assertEqual(self.ids("1"), self.ids(1))

    def test_operators_and_dates_filter_the_neighbours(self):
        self.assertEqual(self.ids(query="from:john@example.org", exclude_thread=False), [2])
        self.assertEqual(self.ids(query="from:nobody@example.com"), [])
        self.assertEqual(self.ids(since="2999-01-01"), [])
        self.assertEqual(self.ids(account="Home"), [])

    def test_plain_words_in_the_query_must_appear_in_the_message(self):
        self.assertEqual(self.ids(query="quarterly"), [4, 5])
        self.assertEqual(self.ids(query="quarterly from:jane@example.com"), [4, 5])

    def test_a_narrow_filter_is_applied_before_the_candidate_cap(self):
        with mock.patch.object(mail_search, "SIMILAR_MIN_CHUNKS", 1), \
                mock.patch.object(mail_search, "SIMILAR_CHUNKS_PER_RESULT", 1):
            self.assertEqual(self.ids(query="quarterly", limit=1), [4])

    def test_a_message_without_vectors_is_a_clear_error(self):
        with self.assertRaises(MailError) as caught:
            self.similar(6)
        self.assertEqual(caught.exception.code, "no_vectors")
        self.assertIn("mail_vectors.py --sync", caught.exception.hint)

    def test_a_message_missing_from_the_index_is_an_error(self):
        with self.assertRaises(MailError) as caught:
            self.similar(999)
        self.assertEqual(caught.exception.code, "not_indexed")

    def test_a_missing_vectors_file_is_an_error_with_a_hint(self):
        os.remove(self.vectors_path)
        with self.assertRaises(MailError) as caught:
            self.similar()
        self.assertEqual(caught.exception.code, "vectors_missing")

    def test_it_never_calls_the_embedding_server(self):
        self.embedder.fail = EmbedderError("Ollama is not reachable")
        self.assertEqual(self.ids(), [3, 4, 5])
        self.assertEqual(self.embedder.calls, [])

    def test_the_python_fallback_ranks_the_same_way(self):
        with mock.patch.object(mail_vectors, "load_sqlite_vec", lambda connection: False):
            self.assertEqual(self.ids(), [3, 4, 5])

    def test_a_message_vector_is_the_mean_of_its_unit_chunks(self):
        with sqlite3.connect(self.vectors_path) as connection:
            connection.execute("DELETE FROM chunks WHERE message = 1")
            for number, vector in enumerate(([10, 0, 0], [0, 100, 0])):
                connection.execute(
                    "INSERT INTO chunks (message, chunk, source, vector) VALUES (1, ?, 'body', ?)",
                    (number, mail_vectors.quantize(vector)))
        self.assertEqual([round(value, 2) for value in mail_vectors.message_vector(1)], [0.5, 0.5, 0.0])


if __name__ == "__main__":
    unittest.main()
