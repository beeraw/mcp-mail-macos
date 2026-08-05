-- Shared handlers. This file is prepended to every other script at runtime,
-- so each script can call the handlers below with the "my" prefix.
--
-- Output convention: every script returns plain text where fields are joined
-- with the ASCII Unit Separator (31) and records with the Record Separator (30).
-- Those characters never appear in real mail data, and cleanField() strips them
-- from values anyway, so the Python side can split without escaping.

on fieldSep()
	return (ASCII character 31)
end fieldSep

on recordSep()
	return (ASCII character 30)
end recordSep

on joinText(theList, theDelimiter)
	set savedDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to theDelimiter
	set theResult to theList as text
	set AppleScript's text item delimiters to savedDelimiters
	return theResult
end joinText

on splitText(theText, theDelimiter)
	if theText is "" then return {}
	set savedDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to theDelimiter
	set theResult to text items of theText
	set AppleScript's text item delimiters to savedDelimiters
	return theResult
end splitText

on replaceText(theText, searchString, replacement)
	set savedDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to searchString
	set theParts to text items of (theText as text)
	set AppleScript's text item delimiters to replacement
	set theResult to theParts as text
	set AppleScript's text item delimiters to savedDelimiters
	return theResult
end replaceText

-- Replaces the separator characters so a value can never break record parsing.
on cleanField(theValue)
	try
		set theText to theValue as text
	on error
		return ""
	end try
	set theText to my replaceText(theText, my fieldSep(), " ")
	return my replaceText(theText, my recordSep(), " ")
end cleanField

on toLower(theText)
	set upperChars to "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
	set lowerChars to "abcdefghijklmnopqrstuvwxyz"
	set theResult to ""
	repeat with aChar in (characters of (theText as text))
		set aChar to aChar as text
		considering case
			set charIndex to offset of aChar in upperChars
		end considering
		if charIndex > 0 then
			set theResult to theResult & (character charIndex of lowerChars)
		else
			set theResult to theResult & aChar
		end if
	end repeat
	return theResult
end toLower

on padTwo(aNumber)
	if aNumber < 10 then return "0" & (aNumber as text)
	return aNumber as text
end padTwo

-- ISO 8601 with the machine's UTC offset, assembled digit by digit.
-- Coercing a date or a large number straight to text is not an option: the
-- result follows the machine's locale and turns into "1,785863539E+9".
on toIso(theDate)
	if theDate is missing value then return ""
	set monthConstants to {January, February, March, April, May, June, July, August, September, October, November, December}
	set monthNumber to 1
	repeat with i from 1 to 12
		if (month of theDate) is (item i of monthConstants) then
			set monthNumber to i
			exit repeat
		end if
	end repeat
	set secondsOfDay to time of theDate
	set isoText to (year of theDate as text) & "-" & my padTwo(monthNumber) & "-" & my padTwo(day of theDate)
	set isoText to isoText & "T" & my padTwo(secondsOfDay div 3600) & ":" & my padTwo((secondsOfDay mod 3600) div 60) & ":" & my padTwo(secondsOfDay mod 60)
	set offsetSeconds to (time to GMT)
	if offsetSeconds < 0 then
		set offsetSign to "-"
		set offsetSeconds to -offsetSeconds
	else
		set offsetSign to "+"
	end if
	return isoText & offsetSign & my padTwo(offsetSeconds div 3600) & ":" & my padTwo((offsetSeconds mod 3600) div 60)
end toIso

on findAccount(accountName)
	tell application "Mail"
		repeat with anAccount in accounts
			if (name of anAccount) is accountName then return anAccount
		end repeat
		set wantedName to my toLower(accountName)
		repeat with anAccount in accounts
			if my toLower(name of anAccount) is wantedName then return anAccount
		end repeat
	end tell
	error "MAILERR:account_not_found:" & accountName
end findAccount

-- Mail exposes an account's mailboxes with leaf names only, but accepts a
-- slash-separated path when looking one up, so paths round-trip correctly.
on fullMailboxPath(theMailbox)
	tell application "Mail"
		set thePath to name of theMailbox
		set currentBox to theMailbox
		set mailboxClass to class of theMailbox
		repeat
			set parentBox to missing value
			try
				set parentBox to container of currentBox
			end try
			if parentBox is missing value then exit repeat
			if (class of parentBox) is not mailboxClass then exit repeat
			set thePath to (name of parentBox) & "/" & thePath
			set currentBox to parentBox
		end repeat
		return thePath
	end tell
end fullMailboxPath

on accountInbox(theAccount)
	tell application "Mail"
		repeat with candidateName in {"INBOX", "Inbox", "Boîte de réception", "Bandeja de entrada", "Posteingang"}
			try
				set theMailbox to mailbox (candidateName as text) of theAccount
				if (name of theMailbox) is not "" then return theMailbox
			end try
		end repeat
		set theAccountName to name of theAccount
	end tell
	error "MAILERR:inbox_not_found:" & theAccountName
end accountInbox

-- Resolves a mailbox from an optional account name and an optional path.
-- With no account, well-known names map to Mail's unified mailboxes.
on resolveMailbox(accountName, mailboxPath)
	tell application "Mail"
		if accountName is "" then
			if mailboxPath is "" then return inbox
			set lowerPath to my toLower(mailboxPath)
			if lowerPath is in {"inbox", "boîte de réception"} then return inbox
			if lowerPath is in {"sent", "messages envoyés", "éléments envoyés"} then return sent mailbox
			if lowerPath is in {"drafts", "brouillons"} then return drafts mailbox
			if lowerPath is in {"trash", "corbeille"} then return trash mailbox
			if lowerPath is in {"junk", "spam", "courrier indésirable"} then return junk mailbox
			repeat with anAccount in accounts
				try
					set theMailbox to mailbox mailboxPath of anAccount
					if (name of theMailbox) is not "" then return theMailbox
				end try
			end repeat
			error "MAILERR:mailbox_not_found:" & mailboxPath
		else
			set theAccount to my findAccount(accountName)
			if mailboxPath is "" then return my accountInbox(theAccount)
			set lowerPath to my toLower(mailboxPath)
			if lowerPath is "inbox" then
				try
					return my accountInbox(theAccount)
				end try
			end if
			try
				set theMailbox to mailbox mailboxPath of theAccount
				if (name of theMailbox) is not "" then return theMailbox
			end try
			error "MAILERR:mailbox_not_found:" & mailboxPath & " (account " & accountName & ")"
		end if
	end tell
end resolveMailbox

-- Looks a message up by Mail's own integer id, which is stable across launches.
-- "message id N of mailbox" cannot be used: Mail also defines "message id" as a
-- message property (the RFC header), so that reference form fails to compile.
-- The whose-clause below is resolved inside Mail and stays acceptable, unlike a
-- whose-clause on subject or content.
on findMessage(accountName, mailboxPath, messageIdentifier)
	set theMailbox to my resolveMailbox(accountName, mailboxPath)
	tell application "Mail"
		set theMatches to (messages of theMailbox whose id is messageIdentifier)
		if (count of theMatches) is 0 then
			error "MAILERR:message_not_found:" & messageIdentifier
		end if
		return item 1 of theMatches
	end tell
end findMessage

-- "Name <address>" for each recipient, comma separated.
on joinAddresses(recipientList)
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
		if theName is missing value then set theName to ""
		if theName is "" then
			set end of theParts to theAddress
		else
			set end of theParts to (theName & " <" & theAddress & ">")
		end if
	end repeat
	return my joinText(theParts, ", ")
end joinAddresses

-- The metadata row shared by list and search results.
on messageRow(theMessage, mailboxPathText, accountNameText)
	tell application "Mail"
		set theFields to {(id of theMessage) as text, my cleanField(subject of theMessage), my cleanField(sender of theMessage), my toIso(date received of theMessage), (read status of theMessage) as text, (flagged status of theMessage) as text, my cleanField(mailboxPathText), my cleanField(accountNameText)}
	end tell
	return my joinText(theFields, my fieldSep())
end messageRow
