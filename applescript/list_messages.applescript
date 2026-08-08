-- argv: 1 = account name ("" for the unified mailbox)
--       2 = mailbox path ("" for the inbox)
--       3 = limit
--       4 = unreadOnly ("0" or "1")
--       5 = scan window size (how many recent messages are looked at)
--       6 = includePreview ("0" or "1")
--       7 = preview length in characters
--       8 = how many previews may be read at most
--
-- First record: total message count, window size actually scanned, rows
-- returned, previews read. Then one messageRow per message.
--
-- Properties are read with plural references ("subject of messages 1 thru n")
-- rather than in a loop: that is one Apple event per property instead of one
-- per message, and it is several times faster on a large mailbox.
--
-- A preview has no such trick: "content" has to be asked message by message,
-- about a second each, and Mail serves those on the thread that draws its
-- interface. Hence the budget on argv 8 — past it, previews come back empty
-- rather than freezing Mail for minutes.

on run argv
	set accountName to item 1 of argv
	set mailboxPathText to item 2 of argv
	set rowLimit to (item 3 of argv) as integer
	set unreadOnly to (item 4 of argv) is "1"
	set windowSize to (item 5 of argv) as integer
	set includePreview to (item 6 of argv) is "1"
	set previewLength to (item 7 of argv) as integer
	set previewBudget to (item 8 of argv) as integer

	set theMailbox to my resolveMailbox(accountName, mailboxPathText)
	tell application "Mail"
		set resolvedPath to my fullMailboxPath(theMailbox)
		set resolvedAccount to ""
		try
			set resolvedAccount to name of (account of theMailbox)
		end try
		set totalCount to count of messages of theMailbox
		if totalCount is 0 then
			return my joinText({"0", "0", "0", "0"}, my fieldSep())
		end if
		if windowSize > totalCount then set windowSize to totalCount
		if windowSize < 1 then set windowSize to 1

		set idList to id of messages 1 thru windowSize of theMailbox
		set subjectList to subject of messages 1 thru windowSize of theMailbox
		set senderList to sender of messages 1 thru windowSize of theMailbox
		set dateList to date received of messages 1 thru windowSize of theMailbox
		set readList to read status of messages 1 thru windowSize of theMailbox
		set flaggedList to flagged status of messages 1 thru windowSize of theMailbox
	end tell

	set theRows to {}
	set returnedCount to 0
	set previewsRead to 0
	repeat with i from 1 to windowSize
		if returnedCount ≥ rowLimit then exit repeat
		set isRead to item i of readList
		if (not unreadOnly) or (isRead is false) then
			set previewText to ""
			if includePreview and previewsRead < previewBudget then
				set previewsRead to previewsRead + 1
				try
					tell application "Mail"
						set bodyText to content of message i of theMailbox
					end tell
					if bodyText is missing value then set bodyText to ""
					if (length of bodyText) > previewLength then
						set bodyText to text 1 thru previewLength of bodyText
					end if
					set previewText to my cleanField(my replaceText(bodyText, return, " "))
					set previewText to my replaceText(previewText, linefeed, " ")
				end try
			end if
			set theFields to {(item i of idList) as text, my cleanField(item i of subjectList), my cleanField(item i of senderList), my toIso(item i of dateList), isRead as text, (item i of flaggedList) as text, my cleanField(resolvedPath), my cleanField(resolvedAccount), previewText, i as text}
			set end of theRows to my joinText(theFields, my fieldSep())
			set returnedCount to returnedCount + 1
		end if
	end repeat

	set metaRow to my joinText({totalCount as text, windowSize as text, returnedCount as text, previewsRead as text}, my fieldSep())
	return my joinText({metaRow} & theRows, my recordSep())
end run
