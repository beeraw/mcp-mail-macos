-- argv: 1 = account name (may be "" for a local "On My Mac" mailbox)
--       2 = mailbox name
--       3 = parent mailbox path (may be "")
-- Returns one record: account, full path of the created mailbox.
--
-- Mail nests mailboxes through slash-separated names, so a child is created by
-- passing the whole path as the new mailbox name.

on run argv
	set accountName to item 1 of argv
	set mailboxName to item 2 of argv
	set parentPath to item 3 of argv

	if parentPath is not "" then
		set fullPath to parentPath & "/" & mailboxName
	else
		set fullPath to mailboxName
	end if

	tell application "Mail"
		if accountName is "" then
			set theMailbox to make new mailbox with properties {name:fullPath}
		else
			-- Passing the account as a property fails (-10000); the creation has
			-- to happen inside a tell block targeting the account itself.
			set theAccount to my findAccount(accountName)
			tell theAccount
				set theMailbox to make new mailbox with properties {name:fullPath}
			end tell
		end if
		set resolvedAccount to ""
		try
			set resolvedAccount to name of (account of theMailbox)
		end try
		set resolvedPath to my fullMailboxPath(theMailbox)
	end tell
	return my joinText({my cleanField(resolvedAccount), my cleanField(resolvedPath)}, my fieldSep())
end run
