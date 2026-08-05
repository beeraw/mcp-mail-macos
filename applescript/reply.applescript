-- argv: 1 = account name, 2 = mailbox path, 3 = message id, 4 = body,
--       5 = replyAll ("0" or "1"), 6 = mode ("send" or "draft")
-- Returns one record: mode, subject of the reply, recipient count.
--
-- Mail's "reply" command is used rather than a fresh outgoing message so that
-- the In-Reply-To and References headers are filled in and the answer threads
-- properly. Those headers cannot be set from AppleScript on a message we build
-- ourselves. The price is that "reply" has to open a compose window, which it
-- does even when asked not to, so the window is closed again at the end.

on run argv
	set accountName to item 1 of argv
	set mailboxPathText to item 2 of argv
	set messageIdentifier to (item 3 of argv) as integer
	set replyBody to item 4 of argv
	set replyAll to (item 5 of argv) is "1"
	set theMode to item 6 of argv

	set theMessage to my findMessage(accountName, mailboxPathText, messageIdentifier)

	-- Outgoing messages already sent or saved stay in Mail's list, and a new one
	-- is not necessarily first, so the reply is identified by the id that was
	-- not there before rather than by its position.
	tell application "Mail"
		set knownIds to {}
		repeat with anOutgoing in outgoing messages
			try
				set end of knownIds to (id of anOutgoing)
			end try
		end repeat
		if replyAll then
			reply theMessage opening window true with reply to all
		else
			reply theMessage opening window true without reply to all
		end if
	end tell

	-- The reply window is created asynchronously; wait for it to show up.
	set theReply to missing value
	repeat with attemptNumber from 1 to 40
		delay 0.25
		tell application "Mail"
			repeat with anOutgoing in outgoing messages
				set outgoingId to missing value
				try
					set outgoingId to id of anOutgoing
				end try
				if outgoingId is not missing value and outgoingId is not in knownIds then
					set theReply to anOutgoing
					exit repeat
				end if
			end repeat
		end tell
		if theReply is not missing value then exit repeat
	end repeat
	if theReply is missing value then
		error "MAILERR:reply_window_missing:Mail did not open the reply"
	end if

	tell application "Mail"
		-- Mail pre-fills the quoted original; the answer goes above it.
		set existingContent to ""
		try
			set existingContent to content of theReply
		end try
		set content of theReply to replyBody & return & return & existingContent
		set theSubject to subject of theReply
		set recipientCount to (count of to recipients of theReply) + (count of cc recipients of theReply)
		if theMode is "send" then
			send theReply
		else
			save theReply
			try
				close theReply saving no
			end try
		end if
	end tell

	return my joinText({theMode, my cleanField(theSubject), recipientCount as text}, my fieldSep())
end run
