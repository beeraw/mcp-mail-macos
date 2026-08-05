-- argv: 1 = account name, 2 = mailbox path, 3 = message positions joined by the
--       field separator, 4 = max characters kept per body
-- Records: position, body text.
--
-- Used by the body search. Positions come from a window that list_messages has
-- already scanned, so bodies are only fetched for the few candidates left after
-- the cheap subject and sender filters.

on run argv
	set accountName to item 1 of argv
	set mailboxPathText to item 2 of argv
	set positionList to my splitText(item 3 of argv, my fieldSep())
	set maxBodyChars to (item 4 of argv) as integer

	set theMailbox to my resolveMailbox(accountName, mailboxPathText)
	set theRows to {}
	repeat with aPosition in positionList
		set positionNumber to (aPosition as text) as integer
		set bodyText to ""
		try
			tell application "Mail"
				set bodyText to content of message positionNumber of theMailbox
			end tell
			if bodyText is missing value then set bodyText to ""
			if (length of bodyText) > maxBodyChars then
				set bodyText to text 1 thru maxBodyChars of bodyText
			end if
		end try
		set end of theRows to my joinText({positionNumber as text, my cleanField(bodyText)}, my fieldSep())
	end repeat
	return my joinText(theRows, my recordSep())
end run
