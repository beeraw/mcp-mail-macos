-- argv: 1 = account name, 2 = mailbox path, 3 = message id
-- Returns one record: subject, sender, to, cc, bcc, attachment names,
-- mailbox path, account, body.
--
-- Refuses anything that is not a draft. The role is checked against Mail's
-- unified drafts mailbox rather than against a name, since the folder is
-- "[Gmail]/Brouillons" on one account and "Drafts" on another. Matching on the
-- id alone would not do: ids are only unique within a mailbox, so the holding
-- mailbox is compared too, to be sure the draft found is this very message.

on run argv
	set accountName to item 1 of argv
	set mailboxPathText to item 2 of argv
	set messageIdentifier to (item 3 of argv) as integer

	set theMessage to my findMessage(accountName, mailboxPathText, messageIdentifier)

	tell application "Mail"
		set holdingMailbox to mailbox of theMessage
		set holdingPath to my fullMailboxPath(holdingMailbox)
		set holdingAccount to ""
		try
			set holdingAccount to name of (account of holdingMailbox)
		end try

		set isDraft to false
		try
			repeat with aCandidate in (messages of drafts mailbox whose id is messageIdentifier)
				if my fullMailboxPath(mailbox of aCandidate) is holdingPath then
					set isDraft to true
					exit repeat
				end if
			end repeat
		end try
		if not isDraft then
			error "MAILERR:not_a_draft:message " & messageIdentifier & " sits in " & holdingPath & ", which is not a drafts mailbox"
		end if

		set theSubject to subject of theMessage
		if theSubject is missing value then set theSubject to ""
		set theSender to ""
		try
			set theSender to sender of theMessage
		end try
		if theSender is missing value then set theSender to ""
		set theBody to ""
		try
			set theBody to content of theMessage
		end try
		if theBody is missing value then set theBody to ""

		set attachmentNames to {}
		try
			repeat with anAttachment in (mail attachments of theMessage)
				try
					set end of attachmentNames to (name of anAttachment)
				end try
			end repeat
		end try

		set theFields to {my cleanField(theSubject), my cleanField(theSender), my cleanField(my joinAddresses(to recipients of theMessage)), my cleanField(my joinAddresses(cc recipients of theMessage)), my cleanField(my joinAddresses(bcc recipients of theMessage)), my cleanField(my joinText(attachmentNames, "; ")), my cleanField(holdingPath), my cleanField(holdingAccount), my cleanField(theBody)}
	end tell
	return my joinText(theFields, my fieldSep())
end run
