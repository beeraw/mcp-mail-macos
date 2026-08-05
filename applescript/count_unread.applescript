-- argv: 1 = account name (may be ""), 2 = mailbox path (may be "")
-- With no mailbox given, walks every mailbox of the matching accounts.
-- Only the "unread count" property is read, which Mail keeps up to date, so
-- this stays fast even on a mailbox holding tens of thousands of messages.
-- Records: account, mailbox path, unread count.

on run argv
	set accountName to item 1 of argv
	set mailboxPathText to item 2 of argv
	set theRows to {}

	if mailboxPathText is not "" then
		set theMailbox to my resolveMailbox(accountName, mailboxPathText)
		tell application "Mail"
			set resolvedAccount to ""
			try
				set resolvedAccount to name of (account of theMailbox)
			end try
			set end of theRows to my joinText({my cleanField(resolvedAccount), my cleanField(my fullMailboxPath(theMailbox)), (unread count of theMailbox) as text}, my fieldSep())
		end tell
		return my joinText(theRows, my recordSep())
	end if

	tell application "Mail"
		if accountName is "" then
			set theAccounts to accounts
		else
			set theAccounts to {my findAccount(accountName)}
		end if
		repeat with anAccount in theAccounts
			set currentName to name of anAccount
			repeat with aMailbox in (every mailbox of anAccount)
				set unreadCount to 0
				try
					set unreadCount to unread count of aMailbox
				end try
				if unreadCount > 0 then
					set end of theRows to my joinText({my cleanField(currentName), my cleanField(my fullMailboxPath(aMailbox)), unreadCount as text}, my fieldSep())
				end if
			end repeat
		end repeat
	end tell
	return my joinText(theRows, my recordSep())
end run
