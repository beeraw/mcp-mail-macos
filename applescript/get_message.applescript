-- argv: 1 = account name, 2 = mailbox path, 3 = message id, 4 = max body characters
-- Returns a single record. Attachments are packed into one field as
-- "name|size|downloaded" triples separated by ";" (the "|" is safe here because
-- the field separator is the Unit Separator).

on formatRecipients(recipientList)
	set theParts to {}
	repeat with aRecipient in recipientList
		tell application "Mail"
			set theAddress to ""
			set theName to ""
			try
				set theAddress to address of aRecipient
			end try
			try
				set theName to name of aRecipient
			end try
		end tell
		if theAddress is missing value then set theAddress to ""
		if theName is missing value or theName is "" then
			set end of theParts to theAddress
		else
			set end of theParts to (theName & " <" & theAddress & ">")
		end if
	end repeat
	return my joinText(theParts, ", ")
end formatRecipients

on run argv
	set accountName to item 1 of argv
	set mailboxPathText to item 2 of argv
	set messageIdentifier to (item 3 of argv) as integer
	set maxBodyChars to (item 4 of argv) as integer

	set theMessage to my findMessage(accountName, mailboxPathText, messageIdentifier)
	tell application "Mail"
		set theMailbox to mailbox of theMessage
		set resolvedPath to my fullMailboxPath(theMailbox)
		set resolvedAccount to ""
		try
			set resolvedAccount to name of (account of theMailbox)
		end try

		set bodyText to ""
		try
			set bodyText to content of theMessage
			if bodyText is missing value then set bodyText to ""
		end try
		set bodyTruncated to "false"
		if (length of bodyText) > maxBodyChars then
			set bodyText to text 1 thru maxBodyChars of bodyText
			set bodyTruncated to "true"
		end if

		set headerText to ""
		try
			set headerText to all headers of theMessage
			if headerText is missing value then set headerText to ""
		end try
		if (length of headerText) > 8000 then set headerText to text 1 thru 8000 of headerText

		set attachmentParts to {}
		try
			repeat with anAttachment in (mail attachments of theMessage)
				set attachmentName to ""
				set attachmentSize to ""
				set attachmentDownloaded to ""
				try
					set attachmentName to name of anAttachment
				end try
				try
					set attachmentSize to (file size of anAttachment) as text
				end try
				try
					set attachmentDownloaded to (downloaded of anAttachment) as text
				end try
				set end of attachmentParts to (attachmentName & "|" & attachmentSize & "|" & attachmentDownloaded)
			end repeat
		end try

		set rfcMessageId to ""
		try
			set rfcMessageId to message id of theMessage
		end try
		set replyToText to ""
		try
			set replyToText to reply to of theMessage
		end try

		set theFields to {(id of theMessage) as text, my cleanField(subject of theMessage), my cleanField(sender of theMessage), my cleanField(replyToText), my cleanField(my formatRecipients(to recipients of theMessage)), my cleanField(my formatRecipients(cc recipients of theMessage)), my cleanField(my formatRecipients(bcc recipients of theMessage)), my toIso(date received of theMessage), (read status of theMessage) as text, (flagged status of theMessage) as text, my cleanField(rfcMessageId), my cleanField(resolvedPath), my cleanField(resolvedAccount), my cleanField(my joinText(attachmentParts, ";")), bodyTruncated, my cleanField(headerText), my cleanField(bodyText)}
	end tell
	return my joinText(theFields, my fieldSep())
end run
