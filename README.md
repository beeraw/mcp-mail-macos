# mcp-mail-macos

An MCP server that drives macOS Mail: read, search, send, organise. It runs over
stdio, launched on demand by the MCP client — there is no long-lived process.

Two mechanisms live side by side, deliberately. **Actions** go through
AppleScript, the only interface that can make Mail do anything. **Search** goes
through a local SQLite index built from Mail's own storage, because AppleScript
needs seconds per message and cannot search an archive of tens of thousands of
mails in any usable time.

Tested on macOS 27, Python 3.14, Mail 16, against Gmail, IMAP and Exchange
accounts, on a mailbox of roughly 50,000 messages spanning several years.

> **Read this before installing.** This server grants an agent the right to
> read, send, move and delete mail on *every* account Mail is configured with,
> and the search index needs Full Disk Access, which macOS cannot scope to a
> single folder. See [Before you trust it with your mail](#before-you-trust-it-with-your-mail).

---

## Contents

- [Before you trust it with your mail](#before-you-trust-it-with-your-mail)
- [Requirements](#requirements)
- [Install](#install)
- [macOS permissions](#macos-permissions)
- [Add to Claude Code](#add-to-claude-code)
- [Configuration](#configuration)
- [The 27 tools](#the-27-tools)
- [Drafts are files, not Mail drafts](#drafts-are-files-not-mail-drafts)
- [The search index](#the-search-index)
  - [Search by meaning](#search-by-meaning)
  - [Similar messages](#similar-messages)
- [Message identifiers](#message-identifiers)
- [Response format](#response-format)
- [Known limitations](#known-limitations)
- [Testing](#testing)
- [Project layout](#project-layout)
- [License](#license)

---

## Before you trust it with your mail

This is a local tool for one person on their own machine. It is not a service,
and it was not designed to be exposed to several users. What it asks for is
broad, and worth weighing before installing.

**Automation lets an agent act on every account.** One grant, once, and the
server can read, send, reply, move and delete across all of them. There is no
per-account allowlist — adding one means filtering in two places (see
[Scope](#scope)).

**Full Disk Access is all or nothing.** The index reads `~/Library/Mail`, which
macOS protects; there is no setting scoped to that folder. Granting it also
grants Messages, browser history and other applications' data to whatever
application you granted it to, and for every future session until you revoke it.

**Message content reaches the agent unfiltered.** Anyone can send you mail, and
that mail lands in an agent's context as text. That is the classic prompt
injection setup, and no permission dialog stands between the two.

Reasonable precautions, in rough order of value:

1. **Try it on a secondary account first**, before pointing it at anything that
   matters.
2. **Keep the confirmation guard.** Every send requires `confirm=true` and
   returns a preview otherwise. It makes each send deliberate and shows exactly
   what would leave.
3. **Only grant Full Disk Access if you need indexed search**, and revoke it
   afterwards — everything already indexed stays searchable. Set
   `index_max_age_minutes` high in `config.json` so the server stops trying to
   refresh.
4. **Decide whether an agent should send at all.** Preparing drafts as `.eml`
   files and sending them yourself is a perfectly good mode; `write_draft`
   touches nothing but a folder.
5. **Run the checks before real use**: `python3 -m unittest discover -s tests -t .`
   for the logic, then `test_manual.py read` against your own Mail.

---

## Requirements

| | |
| --- | --- |
| **OS** | macOS, with Mail configured and its accounts loaded. AppleScript and Mail's storage layout are the whole foundation, so there is no path to another platform. |
| **Python** | 3.11 or later — the code uses `X \| None` annotations and `tomllib`-era stdlib behaviour. Developed on 3.14. |
| **Mail** | Version 16 (macOS 13+). The AppleScript dictionary has been stable across these releases; the internal index schema has not (see below). |

### Dependencies

One, declared in `requirements.txt`:

```
mcp>=1.2.0
```

That is the official Model Context Protocol SDK. Both generations work and the
import picks whichever is installed:

| SDK | Class | Import |
| --- | --- | --- |
| 1.x | `FastMCP` | `mcp.server.fastmcp` |
| 2.x | `MCPServer` | `mcp.server.mcpserver` |

The decorator API is identical between the two, so nothing else changes.

One more, **optional**, in `requirements-semantic.txt`:

```
sqlite-vec>=0.1.6
```

It computes cosine similarity inside SQLite for search by meaning
([below](#search-by-meaning)), and is imported only when that feature runs.
Without it the server, the index and keyword search are untouched; semantic
search then compares vectors in plain Python, which is fine for a filtered
search and refused (with a hint) for a whole-mailbox one. Search by meaning also
needs [Ollama](https://ollama.com) running locally with the `bge-m3` model
(`brew install ollama`, `brew services start ollama`, `ollama pull bge-m3`,
about 1.2 GB); it is reached over HTTP with `urllib`, nothing to install in
Python.

**Everything else is standard library** — `sqlite3` for the index and its FTS5
tables, `email` for parsing `.emlx` containers and writing `.eml` drafts,
`subprocess` for `osascript`, `unicodedata`, `urllib.parse`, `json`, `tempfile`.
No compiled extension, no build step.

Running the unit tests needs nothing at all beyond the standard library: they
never import the SDK.

---

## Install

```bash
git clone https://github.com/beeraw/mcp-mail-macos.git
cd mcp-mail-macos
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-semantic.txt   # optional: search by meaning
```

---

## macOS permissions

Two separate grants, for two different needs. The first is required. The second
only concerns indexed search.

### 1. Automation — driving Mail

On the first call, macOS asks for permission to control Mail. The dialog appears
**once**, and it is attributed to the application launching the server, not to
Python.

If it was denied it will not ask again: restore it in **System Settings →
Privacy & Security → Automation**, unfold the application concerned and tick
**Mail**. Until then every tool returns `permission_denied` with that reminder.

To trigger the prompt at a quiet moment, before wiring anything up:

```bash
.venv/bin/python test_manual.py read
```

### 2. Full Disk Access — building the index

The indexer reads `~/Library/Mail`, which macOS protects through TCC. There is
no setting scoped to that folder: the only lever is **Full Disk Access**, all or
nothing, in **System Settings → Privacy & Security**.

The grant goes to the process's **responsible application**. For Claude Code
that is `/Applications/Claude.app` — not the nested `claude-code` binary, and
not Python. It is only read at launch, so the application has to be quit and
restarted.

| Granted to | Consequence |
| --- | --- |
| `/Applications/Claude.app` | The index refreshes itself from the MCP server. In exchange the grant covers every protected location, not just Mail, and applies to future sessions. |
| Terminal only | The server can search the index but not refresh it. `sync_index` returns `permission_denied` with instructions; updates happen by hand or through a `launchd` agent. |

Revoking it later breaks nothing: everything already indexed stays searchable,
only updates stop.

### 3. An account password — writing drafts and sending

Reading mail goes through Mail. Writing one does not: a draft is filed on the
server over IMAP, and a send is submitted over SMTP. Mail holds the host, the
port and the user name for both, so the only thing missing is a password it
will not hand out.

One is stored per account in the login keychain, under the service name in
`keychain_service`, keyed by the account's own user name:

```bash
security add-generic-password -U -s mcp-mail-macos -a you@example.com -w 'the password'
```

On an account with two step verification — every Google account, for one — that
must be an **app password**, not the account password. Google offers them at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
If the page answers that the setting is unavailable, two step verification is
off on that account, or a Workspace administrator has disabled app passwords.

The lookup runs through `/usr/bin/security`, the same tool that stored the
password, so the keychain grants it without a prompt. A password added by hand
through Keychain Access is refused until it is allowed once.

An account with no IMAP server — an Exchange or Outlook account — cannot be
written to this way, and says so rather than failing obscurely.

---

## Add to Claude Code

Absolute paths, since the server can be launched from anywhere:

```bash
claude mcp add mail-macos -s user -- /path/to/mcp-mail-macos/.venv/bin/python /path/to/mcp-mail-macos/server.py
```

`-s user` makes it available in every project; without the flag it stays scoped
to the current one. Check with `claude mcp list`. The tools appear once Claude
Code restarts.

---

## Configuration

Nothing has to be configured: every setting falls back to something that works
out of the box, and drafts and the index stay inside the repository directory.

To change any of it, copy the example and edit what you need:

```bash
cp config.example.json config.json
```

`config.json` is gitignored, so local paths never end up in a commit. Any key
may be omitted. An environment variable of the form `MAIL_MCP_<KEY>` overrides
both the file and the default — convenient when the MCP client passes its own
configuration:

```bash
claude mcp add mail-macos -s user -e MAIL_MCP_DRAFTS_FOLDER="$HOME/Documents/Outgoing mail" -- /path/to/.venv/bin/python /path/to/server.py
```

| Key | Default | What it does |
| --- | --- | --- |
| `drafts_folder` | `mails/` in the repo | Where `.eml` drafts are written, and where sent ones are filed |
| `pending_retention_days` | `7` | How long an unsent draft may sit on disk |
| `archive_retention_days` | `30` | How long a sent draft stays archived |
| `reply_attribution` | `On {date}, {sender} wrote:` | The line above the quoted original in a reply, in the language of the person answered |
| `reply_date_format` | `%Y-%m-%d %H:%M` | How `{date}` above is written out, in `strftime` terms |
| `keychain_service` | `mcp-mail-macos` | Keychain service name the account passwords are stored under |
| `index_path` | `index.sqlite` in the repo | The search index |
| `mail_root` | `~/Library/Mail` | Mail's storage, where the index is built from |
| `index_max_age_minutes` | `10` | Past this age, `search_all` refreshes the index before answering |
| `applescript_timeout` | `120` | Ceiling for a read call, in seconds |
| `applescript_write_timeout` | `180` | Ceiling for a send or move |
| `body_limit` | `200000` | Characters of body kept per message when indexing |
| `attachments_path` | beside `index_path` | The attachment text index, `attachments.sqlite` |
| `attachments_ocr_images` | `true` | OCR of loose images (at least 50 KB, not named like a logo); scanned PDFs are always read |
| `attachments_max_mb` | `20` | Largest attachment read: PDFs up to this, other types up to half |
| `attachments_char_limit` | `100000` | Characters kept per attachment |
| `attachments_auto_sync` | `true` | After a message sync, start the attachment sync in the background (once that index exists) |
| `saved_searches_path` | beside `index_path` | The named searches of `saved_search`, `saved_searches.json` (gitignored) |
| `vectors_path` | beside `index_path` | The embeddings for search by meaning, `vectors.sqlite` (gitignored) |
| `ollama_url` | `http://localhost:11434` | Where Ollama listens |
| `embedding_model` | `bge-m3` | The model asked to embed text; changing it needs `mail_vectors.py --build` |
| `ollama_timeout` | `5` | Seconds allowed to embed a query before `search_all` answers with keywords alone |
| `search_mode` | `keyword` | `search_all`'s mode when the caller gives none: `keyword`, or `auto` (hybrid once the vectors exist and cover the index) |
| `vectors_auto_sync` | `true` | After a message sync, start `mail_vectors.py --sync` in the background (once the vectors exist) |

The `launchd` agent is the one place a path cannot come from configuration:
launchd needs absolute paths in the plist itself. Replace `/ABSOLUTE/PATH/TO`
in `launchd/com.mcp-mail-macos.sync.plist` before installing it.

---

## The 27 tools

### Search across everything

| Tool | Purpose |
| --- | --- |
| `search_all(query, account, mailbox, unread_only, flagged_only, since, until, limit, sort, snippets, mode)` | Search every account, through the local index; `mode` adds search by meaning |
| `aggregate(group_by, query, account, mailbox, unread_only, flagged_only, since, until, limit, order)` | Count matching messages per sender, domain, month, year, account, mailbox or recipient ("who writes to me most", volumes per month) |
| `saved_search(action, name, description, query, account, mailbox, unread_only, flagged_only, since, until, limit, sort, snippets)` | Save a frequent search under a name (`save`), re-run it (`run`, with optional overrides), or `list` / `show` / `delete` saved ones |
| `get_thread(message_id, limit)` | The whole conversation a message belongs to |
| `find_similar(message_id, query, account, mailbox, unread_only, flagged_only, since, until, limit, exclude_thread, snippets)` | The messages closest in meaning to a given one ("more like this"), from the stored vectors |
| `index_status()` | What the index holds, how old it is, how many messages have a searchable body (per account and overall), and the state of the attachment and vector indexes |
| `sync_index()` | Bring the index up to date |

`search_all` covers the whole archive in milliseconds. Subject, sender,
To and Cc recipients, body and attachment names are all indexed. FTS5 syntax
works — `subject: invoice`, `sender: jane` (`recipients:` searches
To and Cc together), `AND` / `OR` / `NOT`, `"exact phrase"`, `NEAR(one two, 5)`.
A query that is not valid FTS5 (`invoice 12/2025`) is reinterpreted word by
word, which the answer reports in `interpreted_as`.

`saved_search` keeps a frequent search under a name. `save` stores the
`search_all` parameters you pass (the query, with any Gmail operators, is
required) plus an optional description; `run` executes it and returns exactly
what `search_all` returns, and any parameter passed to `run` overrides the
stored one for that run (`saved_search("run", "unread from Jane", limit=5)`).
Saving under an existing name (any case) replaces that search. Store relative
operators such as `newer_than:7d` rather than fixed dates: the query is kept as
text and evaluated again at each run. One tool with an action, rather than one
tool per action, keeps the tool list short. The file is written atomically and
holds queries only; `aggregate` does not take a saved search.

`aggregate` takes the same query, operators and filters but returns counts
instead of messages: `aggregate("sender", "to:me -is:bulk", since="2026-01-01")`
lists who writes most, `aggregate("month", "from:@example.com")` gives the
volume per month. Nothing is excluded by default; `-is:bulk` leaves out
newsletters. A message in several mailboxes counts once (except
`group_by="mailbox"`); text inside attachments is not searched.

#### Operators

Gmail-style operators are read first and become SQL filters (on `messages`,
`locations` and `recipients`); only the remaining text goes to FTS5. Write them
with no space after the colon, quote values that hold spaces (`from:"jane doe"`),
and prefix `-` to negate (`-is:bulk`). Keys are case-insensitive, operators
combine with AND only (also two of the same kind): `OR` next to an operator, or an operator sharing parentheses with `OR` or free text, is refused with a hint (use `-key:` to exclude, run two searches for alternatives). `NOT from:x` is `-from:x`, and parentheses wrapping operators only, `(from:a has:attachment)`, are ignored. Operators also combine with the
`account`, `mailbox`, `unread_only`, `flagged_only`, `since` and `until`
parameters. A query made only of operators works and returns the newest first.
An unknown `word:` stays free text; a bad value is refused with a hint. The
answer's `filters` shows how the query was understood (`original_query` holds
what was typed).

| Operator | Meaning |
| --- | --- |
| `from:` | sender: full address, `@example.com` (domain and subdomains) or part of the name or address (`from:example.com`, `from:jane`); `from:me` = your accounts |
| `to:` `cc:` | same, on the recipients table; `to:me` / `cc:me` = your accounts |
| `has:attachment` | a real attachment (same meaning as `has_attachment`) |
| `filename:` | attachment name, as typed or stemmed (`filename:plan.pdf`, `filename:budget`) |
| `larger:` `smaller:` | size, `500K`, `2M`, `1G` or bytes (K = 1024) |
| `older_than:` `newer_than:` | relative to now: `30d`, `2w`, `6m` (30 days), `1y` (365 days) |
| `after:` `before:` | `YYYY-MM-DD` or `YYYY/MM/DD`, local midnight; `after` inclusive, `before` exclusive |
| `is:unread` `is:read` `is:flagged` `is:starred` `is:bulk` | read state, flag (`starred` = `flagged`), newsletter / mailing list |
| `in:` | mailbox, case-insensitive: the whole name or its last path segment as Mail stores it, so a localised display name such as "Boîte de réception" is not mapped to INBOX (`in:inbox`, `in:archive` for `[Work]/Archive`) |

A message filed in several mailboxes (Gmail's All Mail and Important, typically)
matches `is:unread`, `is:read`, `is:flagged` and `in:` when any of its copies
does; a negation is the exact opposite (`-is:unread` = unread in none).
`to:` and `cc:` are also names of FTS columns: the operator wins. To search
those columns as words, write a space (`to: jane`) or braces (`{to}: jane`,
`{to cc}: jane`). `me` is read from Mail's account list on first use (one
AppleScript call, cached for the life of the server); if Mail cannot answer,
only that query fails, with a hint to use the address itself.

Results are ranked by relevance by default (`sort="relevance"`): weighted bm25
with a subject match counting most, then sender, attachment names, and
To/Cc/body, times a moderate recency bonus (up to +30 % for a mail received
today, +15 % at one year old; a strong old match still beats a weak recent one).
Each result carries `has_attachment` (a real attachment: inline logos and
png/gif/bmp/svg names are ignored) and `is_bulk` (a List-Id or List-Unsubscribe
header: mailing lists and newsletters), and a `snippet` (`snippets=false` to skip): about 200
characters of the body around the first matched word, matched like the index
does (case and accents ignored, `term*` prefixes and quoted phrases handled), or
the start of the body when the word is only in the subject, sender or an
attachment name. The index cannot store text, so it is read from the message's
`.emlx` file; the file is located from the id alone (Mail shards its folders by
the id's digits), with no extra index column. It is `null` when the file is
missing or not downloaded. `sort="date"` returns the newest matches first instead. The weights and the
bonus are constants at the top of `mail_search.py`. The command-line
`python3 mail_index.py --search QUERY` ranks the same way, `--sort date` to
order by date.

When the attachment index exists, the text of PDFs, Word, Excel, PowerPoint and
scans is searched too (see [Attachment text](#attachment-text)). A message found
only through an attachment carries `attachment_match` (`filename`, and a
`snippet` of the attachment when snippets are on), and its `snippet` starts with
`[attachment: name]`. Without `attachments.sqlite` nothing changes.

`get_thread` uses the conversation grouping Mail computes itself, carried in the
index. The whole exchange comes back, including replies filed in another mailbox
or sent from another account.

### Read directly

| Tool | Purpose |
| --- | --- |
| `list_mailboxes(include_totals)` | Accounts and mailboxes, with unread counts |
| `list_messages(mailbox, account, limit, unread_only, include_preview, scan_limit)` | Messages of one mailbox, newest first |
| `get_message(message_id, max_body_chars)` | Full message: body, headers, attachments |
| `count_unread(mailbox, account)` | Unread counts, per mailbox or across accounts |

These ask Mail directly, so they see the real state including what has just
arrived.

There is deliberately no tool that searches through Mail. Mail serves Apple
events on the thread that draws its interface, so any search wide enough to be
useful freezes the app for minutes — and the timeout does not rescue it: killing
`osascript` leaves Mail chewing on the event it already accepted, so the freeze
outlives the call. Search goes through the index, which reads the same store
from disk and needs the same Full Disk Access. If the index is missing,
`search_all` says so and `sync_index` builds it; there is no faster path worth
having.

### Prepare a message for review

| Tool | Purpose |
| --- | --- |
| `write_draft(to, subject, body, cc, bcc, attachments, sender, folder)` | Write a draft as an `.eml` file, outside Mail |
| `list_drafts(folder)` | Drafts waiting to be sent |
| `read_draft_file(path)` | Full content of one draft |
| `send_draft_file(path, confirm, keep_file)` | Send the draft, then file it away |
| `discard_draft_file(path)` | Delete a draft that will not be sent |
| `purge_drafts(folder)` | Sweep forgotten drafts |

See [Drafts are files](#drafts-are-files-not-mail-drafts) for why they live
outside Mail.

### Send

| Tool | Purpose |
| --- | --- |
| `send_email(to, subject, body, cc, bcc, attachments, sender, confirm)` | Compose and send |
| `create_draft(to, subject, body, cc, bcc, attachments, sender, signature)` | Save a draft **in Mail** |
| `send_draft(message_id, confirm)` | Send a draft Mail already holds |
| `reply_to_message(message_id, body, reply_all, attachments, send, confirm, add_to, add_cc, bcc)` | Reply, staying in the thread, with attachments if any; `add_to`, `add_cc` and `bcc` add recipients to the computed ones without duplicates |

**Confirmation is mandatory.** Every tool that actually sends — `send_email`,
`send_draft_file`, `send_draft` and `reply_to_message(send=True)` — does nothing
unless `confirm=true`. Called without it they return `confirmation_required`
along with a `preview` block describing precisely what would go out: sender,
recipients, subject, body, and the attachments actually carried. It doubles as a
dry run.

The guard covers every send path rather than one of them: protecting only the
draft path would push a caller to recompose with `send_email`, which is the
behaviour worth avoiding in the first place.

`to`, `cc` and `bcc` accept one address, a comma-separated string, or a list.
`attachments` takes absolute paths to existing files, checked before anything is
built. Without `sender`, the account Mail lists first is used — worth being
explicit when several accounts coexist, since the sender decides which signature
is appended.

**Every message goes out as HTML**, laid out the way Mail lays out its own: the
message, then the account's signature, then a blank line, then the attachments.
A plain text body is converted — blank lines become paragraphs, single newlines
become breaks, and anything resembling markup is escaped — so a caller never has
to write HTML to get a well-formed message. Pass `signature=false` to leave the
signature off.

The signature is not configured here. It is read from Mail's own settings for
the sending account: which signature is selected, its HTML, and the images it
carries. Editing it in Mail is enough; there is no second copy to keep in step.

### Organise

| Tool | Purpose |
| --- | --- |
| `create_mailbox(name, parent, account)` | Create a mailbox, optionally nested |
| `move_message(message_id, target_mailbox, target_account)` | Move to another mailbox |
| `delete_message(message_id)` | Move to trash |
| `mark_as_read(message_id)` / `mark_as_unread(message_id)` | Read status |
| `flag_message(message_id, flag_color)` | red, orange, yellow, green, blue, purple, gray, or `none` |

---

## Drafts are files, not Mail drafts

`write_draft` writes a self-contained `.eml` file — attachments embedded — into
`mails/`. macOS renders an `.eml` in Mail on double-click, so it reads like a
real message. `send_draft_file` submits that file as it stands, so what
leaves is byte for byte what was reviewed, then moves it to `mails/sent/`.

This is not a stylistic choice. **Mail cannot send a draft it holds.** Its
`send` command only understands an outgoing message, not a message sitting in a
mailbox; opening a draft turns it into one, but only after an unpredictable
delay that exceeded a minute in testing; moving it to the Outbox does nothing at
all. Anything drafted inside Mail therefore has to be re-posted and the original
deleted — and on a Gmail account that delete is undone by the server unless it
is issued once the send has settled.

Keeping drafts out of Mail removes the problem rather than working around it.
Sending becomes a single instant operation with nothing to clean up afterwards.

`send_draft` remains for drafts that already sit in Mail, whether written by
hand or filed there by `create_draft`. It fetches the message from the server,
submits it unchanged and expunges the draft — so the formatting, the signature
and every attachment survive, and the deleted draft does not come back.

**Retention.** An unsent draft is removed after 7 days, an archived one after
30. The sweep runs on every write, every listing, and from `sync_index`, so a
forgotten draft does not sit on disk indefinitely. Files live in
`mcp-mail-macos/mails/` unless `folder` says otherwise, and are gitignored:
they hold real message content.

---

## The search index

### Why

Mail answers message by message. On a mailbox of around 20,000 messages, reading
metadata costs about 0.65 s per message and reading a body about 1.7 s. Mail's own search
(`whose subject contains …`) takes about 21 s over 2,500 messages, and searching
bodies exceeds 120 s — to the point of leaving Mail unresponsive to *every*
subsequent call for minutes.

Searching an archive of tens of thousands of messages that way would take hours. The index
sidesteps it by reading Mail's storage directly.

### What feeds it

Two sources, neither sufficient alone:

- **`MailData/Envelope Index`**, Mail's internal SQLite database, for metadata,
  mailbox membership, and read and flag status. It is copied — together with its
  write-ahead log — then opened read-only.
- **The `.emlx` files**, for body text and the RFC `Message-ID` header.

Mail's index holds no full text; the files do not say which mailboxes a message
belongs to.

### Indexed, not stored

Bodies go into an FTS5 table declared `content=''`: searchable, never kept. The
database stores only what is needed to display a result and act on it — subject,
sender, date, `Message-ID`, locations. Reading a message goes back through
`get_message`. For roughly 50,000 messages the index weighs about 80 MB.

```
messages    (id, account, rfc_id, subject, sender, date_received, size, conversation_id,
             body_indexed, has_attachment, list_id, is_bulk)
locations   (message, account, mailbox, read, flagged)
recipients  (message, kind, address, domain, name)   -- kind is 'to' or 'cc', address lower-cased
messages_fts(subject, sender, "to", cc, attachments, body)   -- FTS5, content=''
```

Splitting message from locations absorbs Gmail's duplication: a message exists
once on disk, in All Mail, and labels are only views. A mailbox of some 50,000
distinct messages yields around 135,000 locations — which is exactly the figure
AppleScript reports when its mailboxes are summed.

The durable key is the RFC `Message-ID`, not Mail's internal id, which changes
whenever a message moves.

### Build and update

```bash
python3 mail_index.py --check    # verify assumptions, build nothing
python3 mail_index.py --build    # full backfill
python3 mail_index.py --sync     # incremental
python3 mail_index.py --search "invoice acme"
```

`--check` validates seven points, including that message ids map to files and —
most importantly — that membership rebuilt from both sources matches Mail's own
per-mailbox counts. `--build` refuses to start if any of them fails:
`Envelope Index` is undocumented and changes between macOS releases, so a clean
refusal beats a silently wrong index.

`--sync` diffs the set of messages Mail lists against the set the index holds.
Deletion is not a special case, and a move reads as a change of location at
constant `Message-ID`. A pass with nothing to do costs about two seconds.

### Body coverage

Some messages end up with no searchable body: the file is missing, only a
partial download exists, or the message is empty. `messages.body_indexed` is
`1` when a non-trivial body (at least a few word characters) was extracted and
`0` otherwise, and `index_status` reports `with_body` and `without_body` for
each account and overall. Those messages still match on subject, sender,
recipients and attachment names.

For `multipart/alternative` messages the `text/plain` part is used, unless it
is empty, near-empty or a "this message contains HTML" placeholder: then the
HTML part is stripped of markup and indexed instead.

Quoted history is cut from the indexed body, so a reply no longer competes with
the messages it quotes. Only safe patterns count: lines starting with `>` (with
the `On ... wrote:` / `Le ... a écrit :` line that introduces them, and the
non-quoted lines after the block are kept, for bottom-posted and interleaved
replies); Outlook `From:`/`De :` header blocks (at least three header lines) and
`-----Original Message-----` markers, which cut what follows; the last `-- `
signature line when the block after it is 15 lines or fewer; in HTML,
`blockquote type=cite`, Gmail quote containers and Outlook's reply header. If
fewer than 20 word characters would remain, the full text is kept, and a
forward keeps its content when the text above it is tiny. Bodies indexed before
this need a rebuild (`python3 mail_index.py --build`).

### French stemming

Search understands French plurals, feminines and common verb forms: `facture`
finds `factures`, `relancé` finds `relance` and `relancer`, `travail` finds
`travaux`. Accents and case are ignored as before. Under the hood every text
column (subject, attachment names, body) is indexed twice, as written and as a
light stem (`mail_stem.py`, pure Python, no dependency), and a query word is
searched in both. A message holding the exact word ranks above one holding only
another form. Things to know:

- **Quotes mean exact.** `"facture"` and `"les factures"` match the words as
  written, in that order, with no stemming.
- `fact*` matches word starts in both forms. Sender, To and Cc are never
  stemmed, so names and addresses stay exact.
- Numbers, codes and words under four letters are left alone. The stemmer is
  deliberately light: it will not join a noun and its verb (`paiement`, `payer`).
- The stems change the index content: an index from before needs
  `python3 mail_index.py --build` (schema version 5, about 25 % larger).

### Schema version

The index carries a `schema_version` in its `meta` table (an index that
predates versioning counts as version 1). When the code expects a newer one,
`search_all`, `sync_index` and `mail_index.py --sync` refuse with an
`index_outdated` error and the hint to run `python3 mail_index.py --build`;
they never mix formats. The version also moves when the indexed content changes,
not only the tables: version 5 (stemmed columns next to the raw ones) needs a
rebuild from version 4; version 4 (quoted history cut from bodies) needed one
from version 3; version 3 (To and Cc kept apart, `has_attachment`, `list_id`,
`is_bulk`) needed one from version 2. `--build` always starts from scratch, writing to
`<index>.building` and swapping the finished file in atomically, so the live
index keeps answering until the new one is ready and a crashed build loses
nothing. `--sync`, `--build` and the automatic sync share one lock file
(`<index>.sync.lock`, holding the owner's PID and refreshed while it works), so
they never run at the same time.

### Freshness

`search_all` checks the index's age and runs the sync itself past
`max_age_minutes` (10 by default). The answer carries `index_age_minutes` and
`synced`, so the caller knows what was searched. If disk access was revoked in
the meantime the search still succeeds against the existing index and says so in
`sync_note` rather than failing — a stale result beats an error. A lock prevents
two concurrent syncs.

### Background sync

`launchd/com.mcp-mail-macos.sync.plist` runs `--sync` every ten minutes,
independently of any client:

```bash
cp launchd/com.mcp-mail-macos.sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.mcp-mail-macos.sync.plist
```

A warning before installing it: the agent reads `~/Library/Mail`, so Full Disk
Access has to be granted to the program it runs, `/usr/bin/python3`. That hands
the grant to **every Python script** on the machine — wider than an app-scoped
one. A venv interpreter is no better: its path carries a version number and the
grant breaks on the first upgrade.

The agent is only worth it if the index must stay current with no client
running. Otherwise `search_all`'s own freshness check is enough, and the grant
stays scoped to a single application.

### Attachment text

Attachment *names* are always indexed. Their *text* lives in a second database,
`attachments.sqlite`, built by `mail_attachments.py`. It is separate on purpose:
it is large (text of tens of thousands of files), it is rebuilt on its own
schedule, and search works exactly as before when it is absent or broken.

```bash
python3 mail_attachments.py --sync      # first run reads everything; later ones only what is new
python3 mail_attachments.py --build     # start from scratch
python3 mail_attachments.py --status    # rows by status, size, last run
python3 mail_attachments.py --measure   # time the extractors on a random sample
```

Where the files are: Mail keeps the attachments of a message it has not fully
downloaded (`.partial.emlx`) as plain files in
`<mailbox>.mbox/<store>/Data/<shard>/Attachments/<id>/<part>/<file>`, and those of
a full `.emlx` inside its MIME. Both are read; a MIME part is written to a private
temporary directory (`mkdtemp`, mode 0700, outside the repository) for the length
of one batch and removed after it, on exit and on SIGTERM. Nothing is written
under `~/Library/Mail`, and Full Disk Access is needed as for the index.

What is read, and how:

| Kind | Method | Rule |
| --- | --- | --- |
| PDF | PDFKit text layer (`tools/pdftext.swift`) | up to 50 pages, `attachments_max_mb` |
| Scanned PDF (under 20 characters of text) | Vision OCR, fr-FR + en-US, accurate (`tools/ocr.swift`) | first 3 pages |
| docx, xlsx, xlsm, pptx | zip + XML (numbers are left out of sheets) | half of `attachments_max_mb` |
| doc | `textutil` | same |
| png, jpg, jpeg, heic, tiff | Vision OCR | at least 50 KB, name not a logo, banner, signature, social icon or `imageNNN`; off with `attachments_ocr_images` |
| zip, rar, dwg, audio, video, gif, xls, anything else | skipped | recorded with the reason |

The Swift tools are compiled on first use into `tools/build/` (gitignored) with
`/usr/bin/swiftc`, and again whenever their source is newer; a missing compiler
is reported with `xcode-select --install`. Each takes many files per call, so the
process start is paid once. A tool that stays silent for 40 s is killed; the file
it choked on is marked `error` and the rest of the batch goes again.

Resumable: discovery first records every attachment as a row (`pending`, or
`skipped` / `too_big` with a reason), then the pending rows are read in batches of
40 with a commit each. Kill the run at any point and run `--sync` again: it picks up
the remaining rows. A row is read again when its size or modification time
changes (`--retry-errors` retries failures); rows of messages that left Mail, and
of files that disappeared, are deleted. Changing a setting (OCR on or off)
reclassifies the skipped rows on the next run. A run takes `attachments.sqlite.sync.lock`,
independent of the index lock: the two syncs can run at once, since the
attachment one reads Mail's files, not the index.

| Column | Meaning |
| --- | --- |
| `status` | `pending`, `ok`, `empty` (no text found), `skipped`, `too_big`, `error` |
| `reason` | why skipped or failed: `type`, `small_image`, `decoration`, `ocr_off`, `encrypted`, `tool_failed`... |
| `method` | `text`, `ocr`, `office`, `textutil` |
| `text` | the extracted text, one line, capped at `attachments_char_limit` |

`attachments_fts(filename, text, filename_stem, text_stem)` is contentless like the
message table, with the same French stemming; the text itself stays in
`attachments.text` for snippets.

How search uses it: `search_all` runs the free text against the message index as
before, then against `attachments_fts` (bm25 with the file name counting three
times the text), and merges by message. A message found by both gets its own
recency-adjusted score plus `ATTACHMENT_WEIGHT` (0.5) times the attachment's; a
message found only through an attachment is ranked by that half score. Attachment
hits go through the same filters as any result (operators, dates, account,
mailbox, `filename:`), so an attachment never bypasses them. A query restricted
to a message field (`subject:`, `sender:`...) leaves attachments out;
`sort="date"` orders the merged set by date. `has:attachment` is unchanged and
`filename:` still matches the names in the message index.

Keeping it current: a search never waits for attachments (reading them takes
minutes). Instead, once `attachments.sqlite` exists, every successful
`sync_index` (and so every stale-index refresh from `search_all`) starts
`mail_attachments.py --sync` detached in the background; if one is already
running the new one exits at once. Set `attachments_auto_sync` to `false` to
turn that off and run it yourself, or from launchd next to the message sync
(copy `launchd/com.mcp-mail-macos.sync.plist`, replace its `--sync` program by
`mail_attachments.py --sync`, and the same Full Disk Access caveat applies).
`index_status` reports `attachments`: rows by status, size and last run.

Measured on a mailbox of about 54,000 messages and 34,000 attachment files on disk
(plus about 13,500 named parts inside full messages), on Apple silicon: PDF text
about 30 ms a file, scanned PDF OCR about 350 ms, Word and Excel a few ms, images
about 60 ms. A full first run takes on the order of half an hour.

### Search by meaning

Keyword search finds the words you typed. `search_all(..., mode="semantic")`
finds the messages that are *about* what you typed ("unpaid bills" finds a
"payment reminder"), and `mode="hybrid"` fuses both rankings. It is optional:
without the vectors file, Ollama or `sqlite-vec`, everything above behaves
exactly as before.

| `mode` | What runs |
| --- | --- |
| `keyword` | The search described above (bm25, attachments, stemming). |
| `semantic` | The query is embedded and compared with the message chunks; no keyword involved. Fails with a hint when it cannot run. |
| `hybrid` | Both, then Reciprocal Rank Fusion (`k = 60`): a message scores the sum of `1 / (60 + rank)` over the rankings that hold it. Falls back to keyword, with a `semantic_note` in the answer, when it cannot run. |
| omitted | The `search_mode` setting: `keyword` (the default), or `auto` = hybrid when the vectors are fresh (built with this recipe, covering at least 95 % of the index, `sqlite-vec` loadable), keyword otherwise. |

Each result of `semantic` and `hybrid` carries `match` (`keyword`, `semantic` or
`both`) and, when meaning found it, `similarity` (cosine of its best chunk). A
result found only by meaning has no matched word to anchor a snippet on, so its
snippet is the passage that matched. Everything that narrows a keyword search
narrows this one too: operators, dates, account, mailbox. The filters are
applied before the nearest-neighbour cut, so a narrow filter never loses its hits
to the global top. `sort="date"` reorders the fused best matches newest first.
With free text absent (operators alone) there is nothing to embed and the search
is a keyword one.

Building the vectors, once, then keeping them current:

```bash
brew install ollama && brew services start ollama && ollama pull bge-m3
.venv/bin/pip install -r requirements-semantic.txt
python3 mail_vectors.py --build    # from scratch; --sync resumes and only does what is new
python3 mail_vectors.py --status
```

What is embedded: the subject and the message's own text (quoted history already
cut, links and tokens over 40 characters dropped), in chunks of about 1,000
characters with 150 overlapping, at most 8 per message, so a long thread is
represented by its first 8,000 characters or so. The subject is added to the first
chunk. A message with no readable body is embedded by its subject alone. The file
holds the vector, the chunk number and a hash of the text, never the text: an
excerpt is cut again from the message when needed. `--sync` skips a message
already embedded without reading it (`--verify` reads them again and re-embeds
those whose text changed), deletes the vectors of messages the index dropped,
commits every batch of 32 chunks (an interrupted run, SIGTERM included, resumes
where it stopped) and takes `vectors.sqlite.sync.lock`, independent of the
index's. `--sample PERCENT --seed N` embeds a reproducible random subset, for
measurements. Once the file exists, every successful `sync_index` starts
`--sync` in the background (`vectors_auto_sync`); a search never waits for it.

Measured on a mailbox of about 50,700 messages (Apple silicon, bge-m3 in Ollama):

| | |
| --- | --- |
| Chunks | 70,946: 1.4 per message; 91 % of messages take one or two, 1 % reach the cap of 8, 141 empty ones none |
| Full build | 36 minutes: about 33 chunks a second whatever the batch size from 4 up (16 to 64 measured), 32 per request; the .emlx reading (about 2 minutes for the whole mailbox) is negligible next to it |
| File size | 96 MB (int8, 1 KB a chunk); float32 would be about 4 times larger |
| Quantisation | int8, one scale per vector. Against float32, 99.3 % of the top 10 neighbours are kept; a binary quantisation (128 bytes a chunk) keeps 66 % and was rejected |
| Query latency | embedding a query 11 ms once the model is loaded (0.8 s when Ollama has to load it again, after 30 minutes idle); `keyword` 24 ms, `semantic` 110 ms, `hybrid` 124 ms, of which about 75 ms is the comparison with the 71,000 chunks (sqlite-vec, exact, no approximate index) |

The vectors are plain int8 blobs in an ordinary table compared with sqlite-vec's
scalar functions, not a `vec0` virtual table: `vec0` does the same exact scan (95 ms
against 89 ms for 125,000 chunks), but its contents cannot be read without the
extension and it cannot be restricted by message id, both of which the Python
fallback and the filters need. The embedding call has a 5 second timeout
(`ollama_timeout`); after a failure Ollama is not asked again for a minute, so a
search never pays the timeout twice, and `hybrid` answers with keywords and says
why in `semantic_note`.

How well it works, on the pairs of [Evaluating search quality](#evaluating-search-quality)
(550 pairs, `mail_eval.py --run --mode ...`; MRR / recall@10). These pairs are two or
three words of a message, so they measure exact words, which keyword search is built
for; semantic search is not expected to win there, and attachment text is not embedded.

| Mode | Pairs as drawn | Query words inflected (`--inflect`) |
| --- | --- | --- |
| `keyword` | 0.467 / 0.747 | 0.330 / 0.547 |
| `semantic` | 0.102 / 0.204 | 0.092 / 0.173 |
| `hybrid` | 0.440 / 0.744 | 0.358 / 0.593 |

Hybrid uses the semantic ranking at a quarter of the weight of the keyword one
(`RRF_SEMANTIC_WEIGHT`). With equal weights, the loose neighbours of a short query
pushed exact matches down (MRR 0.410 on the pairs as drawn). Weights from 1 to 0.15,
a minimum similarity and a cap on the semantic list were tried on the same pairs;
0.25 without threshold kept recall@10 level with keyword and gained on inflected
queries (subject pairs: recall@10 0.465 to 0.580, 40 fewer misses out of 200). The
price is a lower first place on exact words (MRR minus 0.026), which is why
`keyword` stays the default: it is right for the most frequent kind of query, needs
no Ollama and answers in 24 ms. Search by meaning is where words differ, for
instance a query in English finds French mail about "unpaid bills" (a keyword search
finds nothing); use `mode="hybrid"` or `"semantic"` for those, or set
`search_mode` to `auto` if you prefer it everywhere.


Attachments are not embedded in this version. Their text is the largest volume
of the mailbox (a PDF alone can hold a hundred thousand characters), it is often
noise for a language model (letterheads, tables, OCR of stamps), and keyword
search already reaches it: in `hybrid` the keyword side still merges attachment
hits, so a message found through an attachment word stays in the fused list.

### Similar messages

`find_similar(message_id)` returns the messages whose meaning is closest to a given
one. It needs the vectors file but neither Ollama nor the network: the query is the
message's own stored chunk vectors, averaged (each brought to unit length first). The
message itself is never returned, and with `exclude_thread=True` (the default) neither
is the rest of its conversation, so the answer is other mail about the subject; pass
`false` to keep the replies. `message_id` is a `message_id` reference or the numeric
`mail_id` of `search_all`. `query` narrows the candidates like `search_all`'s: Gmail
operators, dates, account and mailbox filter them before the nearest-neighbour cut,
and plain words must appear in the message. Results have the shape of `search_all`'s,
with `score` (cosine of the best chunk) and the matching passage as `snippet`. A
message without vectors (newer than the last `mail_vectors.py --sync`, or no text)
is an error saying so. On the 71,000 chunks above a search takes about 80 ms (100 ms with snippets),
almost all of it the exact comparison; a filter that leaves fewer chunks is faster.

Averaging the chunks was chosen against keeping the best score over each chunk as a
query, on 12 random multi-chunk messages (top 10 each, thread excluded): same sender
in 61 and 69 of 120, a subject word in common in 88 and 83 of 120, against 0 and 12
for random messages, at 68 ms against 168 ms. The two are as on-topic; the average
costs one scan instead of up to eight.

### Evaluating search quality

`mail_eval.py` measures where search puts the message you were looking for, so a
change to ranking or matching can be shown to help rather than just to differ.
It opens the index read-only and never syncs during a run.

```bash
python3 mail_eval.py --generate 200 --seed 1        # pairs from random messages
python3 mail_eval.py --attach 100 --seed 3          # add pairs from attachment text (keeps the other pairs)
python3 mail_eval.py --run                          # rank of each expected message
python3 mail_eval.py --run --save-baseline before   # keep the numbers
python3 mail_eval.py --run --compare before         # deltas, and pairs that got worse
python3 mail_eval.py --run --mode hybrid            # keyword, semantic or hybrid (needs the vectors)
```

A pair is a query and the message it should find. `--generate` derives the query
from two to four distinctive words of a random message's subject; the body is
not stored in the index, so it is not used. Pairs written by hand
(`"source": "manual"`) are kept when the automatic ones are regenerated;
`eval.example.json` shows the format with made-up data. The report gives MRR,
recall@1, recall@10 and the number of pairs not found in the top 50. Add `--json`
for machine output.

`--attach N` adds pairs of a third kind, `attachment`: two or three words that are
rare in one attachment's text (and not in its message's own fields), expected
result the message. Alone it redraws only the attachment pairs and keeps every
other one, so the subject and body numbers stay comparable across runs; the
report gives one line per kind.

Pairs and results are written under `eval/`, which is gitignored: they contain
real subjects.

---

## Message identifiers

Every message carries an opaque `message_id` encoding the account, the mailbox
path and Mail's internal id. It is stable between calls and survives a Mail
restart — it is not a position in a list.

Two caveats. The internal id is only unique within a mailbox, hence the account
and path travelling with it. And a move creates a new one: `move_message`
returns the new `message_id` when it can find the moved copy through its
`Message-ID` header, and says so when it cannot.

A reference from `search_all` or `get_thread` has the same shape and works
directly in `get_message`, `reply_to_message` or `move_message`. It points at
the **smallest** mailbox holding the message, because Mail resolves an id by
walking the mailbox it is given: aiming at a folder of a few thousand messages
rather than one holding tens of thousands changes the response time by an order
of magnitude.

---

## Response format

Every function returns a dictionary. On failure:

```json
{
  "ok": false,
  "error_code": "mailbox_not_found",
  "error": "mailbox not found: Drafts (account Work)",
  "hint": "Call list_mailboxes to see the exact mailbox paths."
}
```

Errors are data, not protocol exceptions, so a caller can correct itself from
the code and the hint.

| Code | Cause |
| --- | --- |
| `permission_denied` | macOS refuses control of Mail, or access to its storage |
| `apple_event_timeout`, `timeout` | Mail did not answer in time |
| `mail_not_running` | Mail is closed and could not be started |
| `account_not_found`, `mailbox_not_found`, `message_not_found` | Target not found |
| `invalid_message_id` | Malformed identifier |
| `attachment_not_found` | Attachment missing from disk |
| `confirmation_required` | A send was requested without `confirm=true`; nothing left, `preview` describes it |
| `not_a_draft` | `send_draft` aimed at a message that is not in a Drafts mailbox |
| `attachments_unreachable`, `attachments_incomplete` | Attachments could not be recovered; nothing was sent |
| `draft_file_not_found`, `draft_file_unreadable`, `folder_not_found` | `.eml` draft or its folder missing |
| `index_missing`, `not_indexed`, `sync_failed`, `sync_timeout` | Index absent, incomplete, or not refreshable |

---

## Known limitations

All of these come from Mail, not from this server. The figures were measured on
an M4 Pro MacBook Pro against real accounts.

### Speed

| Operation | ~2,500-message mailbox | ~20,000-message mailbox |
| --- | --- | --- |
| Metadata for 20 messages | ~1 s | ~13 s |
| Metadata for 200 messages | ~13 s | — |
| One message body | ~1.7 s | ~1.7 s |
| A mailbox's `unread count` | instant | instant |

This dictates the defaults: `include_preview` and `include_totals` are off, a
single `list_messages` call reads at most twenty previews however many messages
it returns and says so in the answer, and search goes through the index rather
than through Mail.

### What Mail cannot do at all

- **Send a draft it holds.** `send` only understands an outgoing message
  (-1708). Opening the draft produces one only after an unpredictable delay,
  sometimes over a minute. Moving it to the Outbox does nothing. The only
  faithful alternative reported by the community is GUI scripting
  (`Cmd+Shift+D` through System Events), which needs Accessibility permission
  and breaks with any interface change — deliberately not taken here. So a send
  does not go through Mail at all: the message is submitted over SMTP, through
  the account's own outgoing server.
- **Compose a message without rewriting it.** Setting `html content` on an
  outgoing message makes Mail wrap the body in its share wrapper — a stray
  `<br>` above the first line, inside a `<blockquote type="cite">` — and an
  attachment made through `make new attachment` lands *inside* that body, ahead
  of the signature. Neither is reachable from AppleScript, and both survive
  every ordering of the calls, a full HTML document, and setting the property
  after `save`. So messages are built as MIME here and handed to the server.
- **Delete a mailbox.** `delete mailbox` fails with -10000 whatever the syntax.
  A mailbox created by `create_mailbox` has to be removed by hand.
- **Export an attachment.** Mail refuses to write the file anywhere (-10004).
  This no longer matters for sending: `send_draft` fetches the whole message
  from the server and submits it unchanged, so the attachments never have to be
  read back out of Mail's storage.
- **Set headers on an outgoing message.** `In-Reply-To` and `References` cannot
  be set on a message Mail composes, which used to force `reply_to_message`
  through Mail's own `reply` command — opening a compose window and rewriting
  the body. Building the message here sets them directly instead.
- **Create a mailbox with an `account` property.** It has to happen inside a
  `tell` block targeting the account, or -10000.
- **Delete a draft for good.** A Gmail account pushes back a draft deleted
  through Mail a few seconds after the delete reports success. Expunged on the
  server, it is gone — which is how `send_draft` removes it.

### Behaviours worth knowing

- **Mail never composes here any more**, so the autosaves it used to leave
  behind after a send, and the outgoing messages that accumulated invisibly in
  its internal list, no longer happen at all. The sweep that hunted them down
  is gone with them.
- **Mail counts a signature image among the attachments**, so a preview built
  from Mail's own list announces an image the recipient never receives as a
  file — and lists nothing at all before Mail has downloaded the parts. What a
  preview describes is therefore the message on the server, with each part
  settled by its disposition: an attachment is offered, an inline part belongs
  to the body. For a message written elsewhere that says neither, a part the
  HTML shows with `<img src="cid:…">` is taken as part of the body. What was
  left behind is reported under `kept_inline`.
- **Gmail labels are mailboxes**, and one message appears in several. `INBOX` can
  resolve to All Mail: a message's `mailbox` field reports where Mail sees it,
  which is not always what was queried.
- **`every mailbox of account` returns leaf names**, but lookup by slash-separated
  path works. The server rebuilds full paths by walking the `container` property.
- **An attachment's name is sometimes inconsistent** between calls; its size is
  reliable.
- **A disabled account disappears** from Mail's list without an error.
- **AppleScript calls are wrapped in an explicit `with timeout`**; without it any
  call over 60 s fails, which a large mailbox reaches easily.
- **Numbers and dates coerced to text follow the machine's locale** — a date
  becomes `1,785863539E+9`. The server assembles ISO 8601 dates digit by digit to
  avoid it.

### Message content reaches the client unfiltered

Everything these tools return — bodies, subjects, sender names, attachment names
— is whatever arrived in the mailbox, passed through untouched. A message can
therefore contain text that reads like an instruction, and an agent consuming
this server will see it alongside its own. Treat mail content as data, never as
direction, and be wary of a tool call whose arguments were lifted verbatim from
a message. This is not specific to this server, but it is worth stating: reading
mail on an agent's behalf is exactly the situation prompt injection targets.

`read_draft_file` takes a path and parses whatever is there as an email, so any
readable file on the machine can be turned into a body and handed back. That is
deliberate — `folder` would be pointless otherwise, and attachments already
require arbitrary paths — but it means the server is as trusted as the client
driving it. It is meant to run locally, for one user.

### Scope

The server exposes **every account** Mail knows about, for reading and writing
alike. There is no account allowlist. Adding one means filtering in two places —
`MessageReference.decode` and `resolveMailbox` on the AppleScript side, plus a
`WHERE account IN (...)` on the index — because the AppleScript tools reach Mail
directly and would otherwise still see everything.

The index reflects Mail's local store. What an account has not synced does not
exist for Mail, and therefore not for search either.

---

## Testing

Unit tests cover everything that does not need Mail: identifier encoding,
address parsing, error classification, AppleScript assembly, `.eml` round-trips,
retention, message layout and reply threading. They run anywhere, in under a
second:

```bash
python3 -m unittest discover -s tests -t .
```

Manual checks exercise the live path against a real Mail install:

```bash
.venv/bin/python test_manual.py read                              # read-only
.venv/bin/python test_manual.py read --account Work --mailbox INBOX
.venv/bin/python test_manual.py write --to you@example.com        # draft + mailbox
.venv/bin/python test_manual.py write --to you@example.com --send # really sends
```

`read` changes nothing: eight checks, two of which verify that errors surface
cleanly. `write` creates a draft and a test mailbox in Mail; the mailbox has to
be deleted by hand, since Mail cannot do it through AppleScript.

---

## Project layout

```
mcp-mail-macos/
├── server.py           # MCP entry point, the 27 tool definitions
├── mail_tools.py       # driving Mail through AppleScript
├── mail_message.py     # building the message: body, signature, attachments
├── mail_signature.py   # the signature Mail would have used, from its settings
├── mail_draft.py       # drafting and sending, on top of the two above
├── mail_imap.py        # the account's own server, for filing and sending
├── mail_files.py       # .eml drafts, retention, leftover sweep
├── mail_search.py      # querying the index
├── mail_saved.py       # named searches (saved_search tool)
├── mail_index.py       # building and updating the index
├── mail_stem.py        # French light stemmer and query rewrite
├── mail_attachments.py # attachment text: extractors, attachments.sqlite, sync
├── mail_vectors.py     # embeddings for search by meaning: vectors.sqlite, Ollama client, sync
├── mail_eval.py        # search relevance evaluation (pairs, MRR, recall)
├── test_manual.py      # manual checks against a real Mail install
├── tools/              # Swift sources (PDFKit text, Vision OCR), compiled into tools/build/
├── tests/              # unit tests, no Mail required
├── applescript/        # one script per operation, plus shared handlers
│   ├── _common.applescript
│   └── …
├── launchd/            # optional periodic sync agent
├── requirements.txt
├── requirements-semantic.txt   # optional: sqlite-vec
└── README.md
```

AppleScript files are assembled at run time: `_common.applescript` is prepended
to each script, and a `with timeout` wrapper is added around the `run` handler.
Parameters travel through `argv` rather than string interpolation, which rules
out injection, and `--` protects values starting with a dash. Results are
serialised with ASCII separators 31 and 30, which never appear in real mail and
are stripped from values before joining — hence no escaping when parsing.

---

## License

MIT. See [LICENSE](LICENSE).
