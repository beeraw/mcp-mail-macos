-- argv: 1 = account name, 2 = mailbox path, 3 = message id,
--       4 = target account name (may be ""), 5 = target mailbox path
-- Returns one record: target account, target path, new message id ("" if Mail
-- did not hand one back).
--
-- Mail assigns a new id to the moved copy, so the id the caller was holding
-- becomes stale. The new one is read back from the moved message when Mail
-- returns a usable reference.

on run argv
	set accountName to item 1 of argv
	set mailboxPathText to item 2 of argv
	set messageIdentifier to (item 3 of argv) as integer
	set targetAccountName to item 4 of argv
	set targetPathText to item 5 of argv

	set theMessage to my findMessage(accountName, mailboxPathText, messageIdentifier)
	set targetMailbox to my resolveMailbox(targetAccountName, targetPathText)

	tell application "Mail"
		-- Read before moving: the original reference stops resolving afterwards.
		set rfcIdentifier to ""
		try
			set rfcIdentifier to message id of theMessage
		end try

		set movedMessage to (move theMessage to targetMailbox)
		set newIdentifier to ""
		try
			set newIdentifier to (id of movedMessage) as text
		end try

		-- Mail usually hands back a reference without a usable id, so the moved
		-- copy is located again through its RFC Message-ID header. The lookup is
		-- skipped on a large target: a whose-clause walks the whole mailbox.
		if newIdentifier is "" and rfcIdentifier is not "" then
			try
				if (count of messages of targetMailbox) ≤ 2000 then
					set theCandidates to (messages of targetMailbox whose message id is rfcIdentifier)
					if (count of theCandidates) > 0 then
						set newIdentifier to (id of item 1 of theCandidates) as text
					end if
				end if
			end try
		end if
		set resolvedAccount to ""
		try
			set resolvedAccount to name of (account of targetMailbox)
		end try
		set resolvedPath to my fullMailboxPath(targetMailbox)
	end tell
	return my joinText({my cleanField(resolvedAccount), my cleanField(resolvedPath), newIdentifier}, my fieldSep())
end run
