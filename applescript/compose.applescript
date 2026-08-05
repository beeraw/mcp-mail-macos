-- argv: 1 = mode ("send" or "draft")
--       2 = to addresses, joined by the field separator
--       3 = cc addresses
--       4 = bcc addresses
--       5 = subject
--       6 = body
--       7 = attachment POSIX paths, joined by the field separator
--       8 = sender address ("" leaves Mail's default account)
-- Returns one record: mode, subject, recipient count, attachment count, then
-- for a draft its id, mailbox path and account (empty when sending).

on addRecipients(theMessage, addressText, recipientKind)
	set addressList to my splitText(addressText, my fieldSep())
	repeat with anAddress in addressList
		set trimmedAddress to anAddress as text
		if trimmedAddress is not "" then
			tell application "Mail"
				tell theMessage
					if recipientKind is "to" then
						make new to recipient at end of to recipients with properties {address:trimmedAddress}
					else if recipientKind is "cc" then
						make new cc recipient at end of cc recipients with properties {address:trimmedAddress}
					else
						make new bcc recipient at end of bcc recipients with properties {address:trimmedAddress}
					end if
				end tell
			end tell
		end if
	end repeat
	return (count of addressList)
end addRecipients

on run argv
	set theMode to item 1 of argv
	set toText to item 2 of argv
	set ccText to item 3 of argv
	set bccText to item 4 of argv
	set theSubject to item 5 of argv
	set theBody to item 6 of argv
	set attachmentText to item 7 of argv
	set senderAddress to item 8 of argv

	set attachmentPaths to my splitText(attachmentText, my fieldSep())

	tell application "Mail"
		-- The window stays hidden: a draft is saved explicitly below, and an
		-- outgoing message can be sent without ever being displayed.
		set theMessage to make new outgoing message with properties {subject:theSubject, content:theBody, visible:false}
		if senderAddress is not "" then
			set sender of theMessage to senderAddress
		end if
	end tell

	set toCount to my addRecipients(theMessage, toText, "to")
	my addRecipients(theMessage, ccText, "cc")
	my addRecipients(theMessage, bccText, "bcc")

	set attachmentCount to 0
	repeat with aPath in attachmentPaths
		set pathText to aPath as text
		if pathText is not "" then
			tell application "Mail"
				tell theMessage
					make new attachment with properties {file name:(POSIX file pathText as alias)} at after the last paragraph of content
				end tell
			end tell
			set attachmentCount to attachmentCount + 1
		end if
	end repeat
	-- Mail attaches asynchronously; sending too early can drop the file.
	if attachmentCount > 0 then delay 2

	set draftIdentifier to ""
	set draftPath to ""
	set draftAccount to ""

	tell application "Mail"
		if theMode is "send" then
			send theMessage
		else
			-- The ids present before saving are noted so the new draft can be
			-- told apart afterwards. Without that, finding it again means
			-- matching on the subject, which breaks as soon as two drafts share
			-- one. The snapshot is skipped on an unusually full drafts mailbox.
			set knownIds to {}
			set snapshotTaken to false
			try
				if (count of messages of drafts mailbox) ≤ 500 then
					set knownIds to id of messages of drafts mailbox
					set snapshotTaken to true
				end if
			end try

			save theMessage
			try
				close theMessage saving no
			end try

			if snapshotTaken then
				repeat with attemptNumber from 1 to 40
					delay 0.25
					try
						repeat with aDraft in (messages of drafts mailbox)
							set candidateId to id of aDraft
							if candidateId is not in knownIds then
								set draftIdentifier to candidateId as text
								set draftPath to my fullMailboxPath(mailbox of aDraft)
								try
									set draftAccount to name of (account of (mailbox of aDraft))
								end try
								exit repeat
							end if
						end repeat
					end try
					if draftIdentifier is not "" then exit repeat
				end repeat
			end if
		end if
	end tell

	return my joinText({theMode, my cleanField(theSubject), toCount as text, attachmentCount as text, draftIdentifier, my cleanField(draftPath), my cleanField(draftAccount)}, my fieldSep())
end run
