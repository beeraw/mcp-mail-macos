"""Unit tests for everything that does not need Mail.

Run from the project root:

    python3 -m unittest discover -s tests -t .

Nothing here launches osascript or touches ~/Library/Mail, so these run on any
machine and in CI. The live path is covered by test_manual.py instead.
"""

from __future__ import annotations

import email
import email.policy
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import mail_draft
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
            name="KDS",
            html='<span>Jean-Luc Petit</span><img src="cid:LOGO-1">',
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
            name="KDS",
            html="<span><br>Jean-Luc Petit<br>SARL KDS</span>",
            images=[],
        )
        html = mail_message.compose_html("Bonjour,\n\nCordialement\n\n\n", signature)
        self.assertIn(
            '<div>Cordialement</div><br><div id="AppleMailSignature"><span>Jean-Luc Petit',
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
            mail_draft._addresses_of('"Petit, Jean-Luc" <j@x.fr>, a@b.fr'),
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
            identifier="S", name="KDS", html='<img src="cid:LOGO-1">',
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
            '<span class="Apple-string-attachment" style="font-size: 13px;">Jean-Luc</span>'
            "<SPAN class=Apple-string-attachment><img src=\"cid:LOGO-1\"></SPAN>"
        )
        self.assertNotIn("apple-string-attachment", html.lower())
        self.assertIn('<span style="font-size: 13px;">Jean-Luc</span>', html)
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


if __name__ == "__main__":
    unittest.main()
