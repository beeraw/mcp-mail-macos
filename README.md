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
- [The 25 tools](#the-25-tools)
- [Drafts are files, not Mail drafts](#drafts-are-files-not-mail-drafts)
- [The search index](#the-search-index)
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
| `ledger_window_hours` | `2` | How long a send stays recorded, so its leftover autosave can be recognised |
| `index_path` | `index.sqlite` in the repo | The search index |
| `mail_root` | `~/Library/Mail` | Mail's storage, where the index is built from |
| `index_max_age_minutes` | `10` | Past this age, `search_all` refreshes the index before answering |
| `applescript_timeout` | `120` | Ceiling for a read call, in seconds |
| `applescript_write_timeout` | `180` | Ceiling for a send or move |
| `body_limit` | `200000` | Characters of body kept per message when indexing |

The `launchd` agent is the one place a path cannot come from configuration:
launchd needs absolute paths in the plist itself. Replace `/ABSOLUTE/PATH/TO`
in `launchd/com.mcp-mail-macos.sync.plist` before installing it.

---

## The 25 tools

### Search across everything

| Tool | Purpose |
| --- | --- |
| `search_all(query, account, mailbox, unread_only, flagged_only, since, until, limit)` | Search every account, through the local index |
| `get_thread(message_id, limit)` | The whole conversation a message belongs to |
| `index_status()` | What the index holds and how old it is |
| `sync_index()` | Bring the index up to date |

`search_all` covers the whole archive in milliseconds. Subject, sender,
recipients, body and attachment names are all indexed. FTS5 syntax works —
`subject: invoice`, `AND` / `OR` / `NOT`, `"exact phrase"`, `NEAR(one two, 5)`.
A query that is not valid FTS5 (`invoice 12/2025`) is reinterpreted word by
word, which the answer reports in `interpreted_as`.

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
| `create_draft(to, subject, body, cc, bcc, attachments, sender)` | Save a draft **in Mail**, returns its `message_id` |
| `send_draft(message_id, confirm)` | Send a draft Mail already holds |
| `reply_to_message(message_id, body, reply_all, send, confirm)` | Reply, staying in the thread |

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
`attachments` takes absolute paths to existing files, checked before Mail is
called. Without `sender`, Mail uses its default account — worth being explicit
when several accounts coexist.

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
real message. `send_draft_file` builds the message from that file, so what
leaves is what was reviewed, then moves the file to `mails/sent/`.

This is not a stylistic choice. **Mail cannot send a draft it holds.** Its
`send` command only understands an outgoing message, not a message sitting in a
mailbox; opening a draft turns it into one, but only after an unpredictable
delay that exceeded a minute in testing; moving it to the Outbox does nothing at
all. Anything drafted inside Mail therefore has to be re-posted and the original
deleted — and on a Gmail account that delete is undone by the server unless it
is issued once the send has settled.

Keeping drafts out of Mail removes the problem rather than working around it.
Sending becomes a single instant operation with nothing to clean up afterwards.

`send_draft` remains for drafts written by hand inside Mail. It re-posts and
deletes them, waiting for the sync to settle, which takes about twenty seconds.

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
messages    (id, account, rfc_id, subject, sender, date_received, size, conversation_id)
locations   (message, account, mailbox, read, flagged)
messages_fts(subject, sender, recipients, attachments, body)   -- FTS5, content=''
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
  sometimes over a minute. Moving it to the Outbox does nothing. Hence `.eml`
  files, and hence `send_draft` re-posting and deleting. The only faithful
  alternative reported by the community is GUI scripting (`Cmd+Shift+D` through
  System Events), which needs Accessibility permission and breaks with any
  interface change — deliberately not taken here.
- **Delete a mailbox.** `delete mailbox` fails with -10000 whatever the syntax.
  A mailbox created by `create_mailbox` has to be removed by hand.
- **Export an attachment.** Mail refuses to write the file anywhere (-10004), so
  `send_draft` reads attachments out of the stored `.emlx`, which needs Full Disk
  Access. Without it, it refuses to send rather than send a message missing its
  files.
- **Set headers on an outgoing message.** There is no way to build an
  `In-Reply-To` by hand, which is why `reply_to_message` goes through Mail's own
  `reply` command — briefly opening a compose window. That is the only way to get
  a reply properly attached to its thread.
- **Create a mailbox with an `account` property.** It has to happen inside a
  `tell` block targeting the account, or -10000.
- **Send HTML reliably.** Bodies go out as plain text. A hand-written rich-text
  draft loses its formatting through `send_draft`.

### Behaviours worth knowing

- **Mail autosaves what it composes.** With no window to close, that autosave is
  *sometimes* left behind as a draft once the message has gone — intermittently,
  depending on whether the timer fired before the send. It often appears several
  seconds after the send, too late to be cleaned up inline. The server records
  what it sends in `mails/.sent-ledger.json`, and the sweep removes drafts
  matching a recent send on **both** subject and recipient. A hand-written draft
  matches nothing and is never touched.
- **A Gmail account restores a deleted draft.** Deleting during the sync that
  follows a send reports success, then the server pushes the draft back a few
  seconds later. The same delete issued once the send has settled sticks
  permanently. That is why `send_draft` waits before deleting.
- **Outgoing messages accumulate** in Mail's internal list, even after being
  sent, and cannot be closed through AppleScript. They are invisible, and cleared
  by restarting Mail. This matters because identifying a compose window by
  position rather than by id will eventually pick the wrong one — and send it.
- **Mail counts a signature image among the attachments**, and telling it apart
  from a real one is not obvious. AppleScript can only add an attachment *into*
  the body, so a file ends up inline, with a Content-ID, referenced from the
  HTML — exactly like a signature logo. Skipping every inline part therefore
  drops real attachments without a word. The discriminator is how the HTML
  refers to it: `<img src="cid:…">` belongs to the body, while
  `<object data="cid:…">` is a file Mail is merely displaying. What was left
  behind is reported under `kept_inline`.
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
retention and the ledger. They run anywhere, in under a second:

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
├── server.py           # MCP entry point, the 25 tool definitions
├── mail_tools.py       # driving Mail through AppleScript
├── mail_files.py       # .eml drafts, retention, leftover sweep
├── mail_search.py      # querying the index
├── mail_index.py       # building and updating the index
├── test_manual.py      # manual checks against a real Mail install
├── tests/              # unit tests, no Mail required
├── applescript/        # one script per operation, plus shared handlers
│   ├── _common.applescript
│   └── …
├── launchd/            # optional periodic sync agent
├── requirements.txt
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
