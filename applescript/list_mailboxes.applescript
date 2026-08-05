-- argv: 1 = includeTotals ("0" or "1")
-- Returns one record per mailbox: account, path, unreadCount, totalCount
-- The total message count is optional because Mail computes it by walking the
-- mailbox, which costs seconds on a large one, while "unread count" is instant.

on run argv
	set includeTotals to (item 1 of argv) is "1"
	set theRows to {}
	tell application "Mail"
		repeat with anAccount in accounts
			set accountName to name of anAccount
			repeat with aMailbox in (every mailbox of anAccount)
				set unreadCount to 0
				try
					set unreadCount to unread count of aMailbox
				end try
				set totalCount to ""
				if includeTotals then
					try
						set totalCount to (count of messages of aMailbox) as text
					end try
				end if
				set end of theRows to my joinText({my cleanField(accountName), my cleanField(my fullMailboxPath(aMailbox)), unreadCount as text, totalCount}, my fieldSep())
			end repeat
		end repeat
	end tell
	return my joinText(theRows, my recordSep())
end run
