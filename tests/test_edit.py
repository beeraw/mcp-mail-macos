"""Unit tests for editing a draft that already exists (mail_edit).

Run from the project root:

    python3 -m unittest discover -s tests -t .

Every message here is fictional and built in memory; nothing reaches Mail or a
server.
"""

from __future__ import annotations

import email
import email.policy
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mail_edit
import mail_imap
import mail_message
import mail_signature
import mail_tools
from mail_tools import MailError, MessageReference

QUOTE = (
    '<br><div>Le 1 oct. 2026, Jane Doe a écrit :</div>'
    '<blockquote type="cite"><div>The original question.</div></blockquote>'
)


def _signature() -> mail_signature.Signature:
    return mail_signature.Signature(
        identifier="SIG-1",
        name="Work",
        html='<span>John Smith</span><img src="cid:LOGO-1">',
        images=[
            mail_signature.InlineImage(
                content_id="LOGO-1", maintype="image", subtype="png",
                filename="logo.png", payload=b"\x89PNG fake",
            )
        ],
    )


def _draft(body="Bonjour,\n\nVoici l'offre de 100 €.\n\nCordialement,", quote=QUOTE, **overrides) -> bytes:
    arguments = {
        "to": ["Jane Doe <jane@example.com>"],
        "cc": ["bob@example.com"],
        "subject": "Offre",
        "body": mail_message.to_html(body) + quote,
        "sender": "John Smith <john@example.com>",
        "signature": _signature(),
        "extra_headers": [
            ("Message-Id", "<draft-1@example.com>"),
            ("In-Reply-To", "<question@example.com>"),
            ("References", "<question@example.com>"),
            ("X-Uniform-Type-Identifier", "com.apple.mail-draft"),
        ],
    }
    arguments.update(overrides)
    return mail_message.build_message(**arguments).as_bytes()


def _html(message) -> str:
    part, _ = mail_edit._body_parts(message)
    return part.get_content()


def _plain(message) -> str:
    _, part = mail_edit._body_parts(message)
    return part.get_content()


def _addresses(message, header) -> list[str]:
    value = message[header]
    return [a.addr_spec for a in value.addresses] if value is not None else []


class TextEditTests(unittest.TestCase):
    def test_a_passage_is_replaced_where_it_is_written(self):
        message, changed = mail_edit.edit_message(
            _draft(), replacements=[{"old": "l'offre de 100 €", "new": "l'offre de 120 €"}]
        )
        self.assertEqual(changed, ["body"])
        self.assertIn("120 €", _html(message))
        self.assertNotIn("100 €", _html(message))
        # The alternative follows, so no reader sees the old amount.
        self.assertIn("120 €", _plain(message))
        self.assertNotIn("100 €", _plain(message))

    def test_everything_around_the_text_stays(self):
        message, _ = mail_edit.edit_message(_draft(), replacements=[{"old": "100", "new": "120"}])
        html = _html(message)
        self.assertIn('<blockquote type="cite">', html)
        self.assertIn('id="AppleMailSignature"', html)
        self.assertEqual(message["In-Reply-To"], "<question@example.com>")
        self.assertEqual(message["X-Uniform-Type-Identifier"], "com.apple.mail-draft")
        attachments, inline = mail_message.classify_parts(message.as_bytes())
        self.assertEqual((attachments, inline), ([], ["logo.png"]))

    def test_a_passage_found_twice_is_refused(self):
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(body="un, un"), replacements=[{"old": "un", "new": "deux"}])
        self.assertEqual(caught.exception.code, "ambiguous_replacement")

    def test_a_passage_not_in_the_draft_is_refused(self):
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(), replacements=[{"old": "absent", "new": "x"}])
        self.assertEqual(caught.exception.code, "text_not_found")

    def test_markup_is_never_matched(self):
        # "div" is in every tag of the body, never in its text.
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(), replacements=[{"old": "div", "new": "p"}])
        self.assertEqual(caught.exception.code, "text_not_found")

    def test_a_new_body_keeps_the_signature_and_the_quote(self):
        message, _ = mail_edit.edit_message(_draft(), body="Bonjour,\n\nAutre texte.\n\nCordialement,")
        html = _html(message)
        self.assertIn("Autre texte.", html)
        self.assertNotIn("offre", html)
        self.assertLess(html.index("Autre texte."), html.index("a écrit"))
        self.assertIn("The original question.", html)
        self.assertIn("John Smith", html)

    def test_a_new_body_in_a_draft_without_signature_or_quote(self):
        raw = _draft(quote="", signature=None)
        message, _ = mail_edit.edit_message(raw, body="Nouveau.")
        self.assertRegex(_html(message), r"<body><div>Nouveau\.</div><br></body>")

    def test_a_plain_text_draft_is_edited_as_text(self):
        raw = (
            b"From: john@example.com\r\nTo: jane@example.com\r\nSubject: Plain\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nOld text.\r\n-- \r\nJohn\r\n"
        )
        message, _ = mail_edit.edit_message(raw, body="New text.")
        self.assertEqual(_plain(message).replace("\r\n", "\n"), "New text.\n\n-- \nJohn\n")


class RecipientEditTests(unittest.TestCase):
    def test_a_field_given_whole_replaces_it(self):
        message, changed = mail_edit.edit_message(_draft(), to=["alice@example.com", "carol@example.com"])
        self.assertEqual(changed, ["to"])
        self.assertEqual(_addresses(message, "To"), ["alice@example.com", "carol@example.com"])
        self.assertEqual(_addresses(message, "Cc"), ["bob@example.com"])

    def test_an_empty_field_clears_it(self):
        message, _ = mail_edit.edit_message(_draft(), cc="")
        self.assertIsNone(message["Cc"])

    def test_an_added_address_moves_rather_than_repeats(self):
        message, changed = mail_edit.edit_message(_draft(), add_to="bob@example.com", add_bcc="hidden@example.com")
        self.assertEqual(_addresses(message, "To"), ["jane@example.com", "bob@example.com"])
        self.assertEqual(_addresses(message, "Cc"), [])
        self.assertEqual(_addresses(message, "Bcc"), ["hidden@example.com"])
        self.assertEqual(changed, ["to", "cc", "bcc"])

    def test_an_empty_to_is_pointed_out(self):
        message, _ = mail_edit.edit_message(_draft(), add_cc="jane@example.com")
        self.assertIsNone(message["To"])
        self.assertIn("warning", mail_edit.summary(message))
        self.assertNotIn("warning", mail_edit.summary(mail_edit.edit_message(_draft(), subject="Autre")[0]))

    def test_display_names_are_kept_and_encoded(self):
        message, _ = mail_edit.edit_message(_draft(), add_cc="Société Exemple <contact@example.org>")
        headers = message.as_bytes().split(b"\n\n", 1)[0]
        self.assertTrue(all(byte < 128 for byte in headers))
        reread = email.message_from_bytes(message.as_bytes(), policy=email.policy.default)
        self.assertEqual(
            [(a.display_name, a.addr_spec) for a in reread["Cc"].addresses],
            [("", "bob@example.com"), ("Société Exemple", "contact@example.org")],
        )
        self.assertEqual(reread["To"].addresses[0].display_name, "Jane Doe")

    def test_an_address_is_removed_from_whichever_field_holds_it(self):
        message, _ = mail_edit.edit_message(_draft(), remove="BOB@example.com")
        self.assertIsNone(message["Cc"])

    def test_removing_someone_absent_is_refused(self):
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(), remove="nobody@example.com")
        self.assertEqual(caught.exception.code, "not_a_recipient")

    def test_a_draft_is_never_left_without_recipient(self):
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(), remove=["jane@example.com", "bob@example.com"])
        self.assertEqual(caught.exception.code, "no_recipient")

    def test_something_that_is_not_an_address_is_refused(self):
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(), add_cc="Jane Doe")
        self.assertEqual(caught.exception.code, "invalid_address")


class AttachmentEditTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.file = os.path.join(self.workspace.name, "devis.pdf")
        with open(self.file, "wb") as handle:
            handle.write(b"%PDF-1.4 fake")

    def test_a_file_is_added_after_the_body(self):
        message, changed = mail_edit.edit_message(_draft(), add_attachments=[self.file])
        self.assertEqual(changed, ["attachments"])
        self.assertEqual(message.get_content_type(), "multipart/mixed")
        parts = message.get_payload()
        self.assertEqual(parts[0].get_content_type(), "multipart/related")
        self.assertEqual(parts[-1].get_filename(), "devis.pdf")
        attachments, inline = mail_message.classify_parts(message.as_bytes())
        self.assertEqual((attachments, inline), (["devis.pdf"], ["logo.png"]))
        # The thread headers stay on the message, not on the part moved under it.
        self.assertEqual(message["In-Reply-To"], "<question@example.com>")

    def test_a_file_is_removed_by_name_and_the_logo_stays(self):
        raw = _draft(attachment_paths=[self.file])
        message, _ = mail_edit.edit_message(raw, remove_attachments=["devis.pdf"])
        attachments, inline = mail_message.classify_parts(message.as_bytes())
        self.assertEqual((attachments, inline), ([], ["logo.png"]))

    def test_the_signature_logo_cannot_be_removed_as_an_attachment(self):
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(), remove_attachments=["logo.png"])
        self.assertEqual(caught.exception.code, "attachment_not_in_draft")

    def test_a_missing_file_is_refused(self):
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(), add_attachments=["/nowhere/devis.pdf"])
        self.assertEqual(caught.exception.code, "attachment_not_found")


class GeneralEditTests(unittest.TestCase):
    def test_the_subject_is_replaced(self):
        message, changed = mail_edit.edit_message(_draft(), subject="Offre révisée")
        self.assertEqual(changed, ["subject"])
        self.assertEqual(message["Subject"], "Offre révisée")

    def test_asking_for_nothing_is_refused(self):
        with self.assertRaises(MailError) as caught:
            mail_edit.edit_message(_draft(), subject="Offre")
        self.assertEqual(caught.exception.code, "nothing_to_change")


class _Patch:
    def __init__(self, test: unittest.TestCase):
        self.test = test

    def __call__(self, module, name, value):
        original = getattr(module, name)
        setattr(module, name, value)
        self.test.addCleanup(setattr, module, name, original)


class EditDraftInMailTests(unittest.TestCase):
    def setUp(self):
        self.patch = _Patch(self)
        row = mail_tools.FIELD_SEPARATOR.join(
            ["Offre", "john@example.com", "jane@example.com", "", "", "", "[Gmail]/Brouillons",
             "Work", "Body.", "<draft-1@example.com>"]
        )
        self.patch(mail_tools, "run_script", lambda *args, **kwargs: row)
        self.patch(mail_imap, "fetch_draft", lambda account, mid: _draft())
        self.calls = []
        self.patch(
            mail_imap, "append_draft",
            lambda account, raw: self.calls.append(("append", raw)) or {"folder": "[Gmail]/Brouillons"},
        )
        self.patch(mail_imap, "delete_draft", lambda account, mid: self.calls.append(("delete", mid)) or True)
        self.reference = MessageReference(account="Work", mailbox="[Gmail]/Brouillons", identifier=1).encode()

    def test_the_new_version_is_filed_before_the_old_one_goes(self):
        answer = mail_edit.edit_draft(self.reference, subject="Offre révisée")
        self.assertEqual([call[0] for call in self.calls], ["append", "delete"])
        self.assertEqual(self.calls[1][1], "<draft-1@example.com>")
        filed = email.message_from_bytes(self.calls[0][1], policy=email.policy.default)
        self.assertEqual(filed["Subject"], "Offre révisée")
        self.assertNotEqual(filed["Message-ID"], "<draft-1@example.com>")
        self.assertTrue(filed["Message-ID"].endswith("@example.com>"))
        self.assertEqual(answer["rfc_message_id"], filed["Message-ID"])
        self.assertTrue(answer["previous_removed"])

    def test_a_refused_change_files_nothing(self):
        with self.assertRaises(MailError):
            mail_edit.edit_draft(self.reference, remove="nobody@example.com")
        self.assertEqual(self.calls, [])

    def test_an_old_version_left_behind_is_reported(self):
        self.patch(mail_imap, "delete_draft", lambda account, mid: False)
        answer = mail_edit.edit_draft(self.reference, subject="Offre révisée")
        self.assertFalse(answer["previous_removed"])
        self.assertIn("still in Drafts", answer["note"])


class EditDraftFileTests(unittest.TestCase):
    def test_the_file_is_rewritten_in_place(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "draft.eml")
            with open(path, "wb") as handle:
                handle.write(_draft())
            answer = mail_edit.edit_draft_file(path, add_cc="carol@example.com")
            self.assertEqual(answer["changed"], ["cc"])
            self.assertEqual(os.listdir(folder), ["draft.eml"])
            with open(path, "rb") as handle:
                reread = email.message_from_bytes(handle.read(), policy=email.policy.default)
            self.assertEqual(_addresses(reread, "Cc"), ["bob@example.com", "carol@example.com"])
            self.assertEqual(reread["Message-ID"], "<draft-1@example.com>")


if __name__ == "__main__":
    unittest.main()
