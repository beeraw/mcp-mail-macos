-- No arguments.
-- Records: account name, account id (the UUID used in the store and in the
-- mailbox URLs of Mail's internal index), account type, then every address the
-- account may send from, comma separated. The addresses are what ties a sender
-- back to the account whose signature Mail would have used.

on run argv
	set theRows to {}
	tell application "Mail"
		repeat with anAccount in accounts
			set accountType to ""
			try
				set accountType to (account type of anAccount) as text
			end try
			set theAddresses to ""
			try
				set theAddresses to my joinText(email addresses of anAccount, ",")
			end try
			set end of theRows to my joinText({my cleanField(name of anAccount), my cleanField(id of anAccount), my cleanField(accountType), my cleanField(theAddresses)}, my fieldSep())
		end repeat
	end tell
	return my joinText(theRows, my recordSep())
end run
