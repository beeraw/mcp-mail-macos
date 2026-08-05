-- argv: 1 = account name, 2 = mailbox path, 3 = message id,
--       4 = action ("read", "unread", "flag", "unflag", "delete"),
--       5 = flag index for "flag" ("" keeps whatever Mail picks)
-- Returns one record: action, message id, read status, flagged status, flag index.
-- The status fields are empty after a delete: the message no longer sits where
-- it was read from.

on run argv
	set accountName to item 1 of argv
	set mailboxPathText to item 2 of argv
	set messageIdentifier to (item 3 of argv) as integer
	set theAction to item 4 of argv
	set flagIndexText to item 5 of argv

	set theMessage to my findMessage(accountName, mailboxPathText, messageIdentifier)

	tell application "Mail"
		if theAction is "read" then
			set read status of theMessage to true
		else if theAction is "unread" then
			set read status of theMessage to false
		else if theAction is "flag" then
			set flagged status of theMessage to true
			if flagIndexText is not "" then
				set flag index of theMessage to (flagIndexText as integer)
			end if
		else if theAction is "unflag" then
			set flagged status of theMessage to false
		else if theAction is "delete" then
			-- Mail's "delete" moves the message to the account's trash.
			delete theMessage
			return my joinText({theAction, messageIdentifier as text, "", "", ""}, my fieldSep())
		else
			error "MAILERR:unknown_action:" & theAction
		end if

		set currentFlagIndex to ""
		try
			set currentFlagIndex to (flag index of theMessage) as text
		end try
		set theFields to {theAction, messageIdentifier as text, (read status of theMessage) as text, (flagged status of theMessage) as text, currentFlagIndex}
	end tell
	return my joinText(theFields, my fieldSep())
end run
