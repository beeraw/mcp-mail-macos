-- No arguments.
-- Records: account name, account id, incoming host, port, ssl, user name,
-- outgoing host, port, ssl, user name.
--
-- The server settings are read from Mail rather than configured again here:
-- they are already right, and a second copy would be one more thing to keep in
-- step the day an account moves. Only the password is not in Mail's reach.
--
-- An account with no IMAP server of its own — an Exchange account, say —
-- returns empty hosts, and the caller declines it rather than guessing.

on serverFields(theServer)
	tell application "Mail"
		set hostName to ""
		set portNumber to ""
		set sslFlag to ""
		set userName to ""
		try
			set hostName to (server name of theServer) as text
		end try
		try
			set portNumber to (port of theServer) as text
		end try
		try
			set sslFlag to (uses ssl of theServer) as text
		end try
		try
			set userName to (user name of theServer) as text
		end try
	end tell
	if hostName is "missing value" then set hostName to ""
	return {my cleanField(hostName), my cleanField(portNumber), my cleanField(sslFlag), my cleanField(userName)}
end serverFields

on run argv
	set theRows to {}
	tell application "Mail"
		repeat with anAccount in accounts
			set incoming to my serverFields(anAccount)
			set outgoing to {"", "", "", ""}
			try
				set outgoing to my serverFields(smtp server of anAccount)
			end try
			set theFields to {my cleanField(name of anAccount), my cleanField(id of anAccount)}
			repeat with aField in incoming
				set end of theFields to (aField as text)
			end repeat
			repeat with aField in outgoing
				set end of theFields to (aField as text)
			end repeat
			set end of theRows to my joinText(theFields, my fieldSep())
		end repeat
	end tell
	return my joinText(theRows, my recordSep())
end run
