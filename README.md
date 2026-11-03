# dcrmine: mailbox extraction for the David Clay Ross CRM

A read-only IMAP miner for iCloud mailboxes. It needs Python 3.9+ and uses only the standard library; the whole tool is one file, `dcrmine.py`. It pulls three things out of a mailbox:

1. **Contacts.** One row per unique email address of a real person, across all years. The name comes from the email's From/To line, or from the person's signature when the header has no full name.
2. **Threads, linked to contacts.** Every conversation, with the contacts who were in it.
3. **Projects.** Work threads about the same job with the same people, grouped together, so an annual job is one project across years.

**Running it on a real mailbox: follow [RUNBOOK.md](RUNBOOK.md)** (written for the bot that runs the backfill).

It does extraction only. It does no scoring, ranking, relationship labels, money extraction or per-email categorisation. That reasoning happens later, by querying the outputs and the SQLite database.

## Safety model
- **Read-only, enforced at the wire.**
  - Every connection is a `GuardedIMAP4(_SSL)`, whose `_command` checks each command just before it is sent.
  - Only `CAPABILITY LOGIN LIST STATUS EXAMINE UID SEARCH UID FETCH NOOP LOGOUT` are allowed through.
  - `UID FETCH` may only ask for `BODY.PEEK[...]`, `BODYSTRUCTURE`, headers, sizes, dates and flags.
  - Anything else (`SELECT`, `STORE`, `COPY`, `MOVE`, `APPEND`, `EXPUNGE`, `BODY[]`, `RFC822`, ...) raises `ReadOnlyViolation` before it reaches the server.
  - Folders are opened with `EXAMINE`.
  - Nothing is moved, flagged, deleted or sent. The tool contains no SMTP code.
  - `seen-audit` proves this on a real mailbox.
- **Secrets.**
  - The Apple app-specific password is read from the env var `ICLOUD_APP_PASSWORD`; `--pass-env` changes the variable name.
  - The password is never written to disk, logs or the database.
  - Username comes from `--user` or `DCR_IMAP_USER`.
  - Host defaults to `imap.mail.me.com:993` (SSL).

## Stages (`python3 dcrmine.py [options] <command>`)
| # | command | what it does |
|---|---|---|
| 0 | `survey` | **Mandatory first step.** Read-only and cheap: LIST, STATUS, SEARCH counts, and header samples via BODY.PEEK. Writes `survey-report.md`, `survey.json` and `mining-plan.json`: folders, volume per year, top correspondents, noise share, imported archives, encoding/date quirks, keyword/seed hits, other addresses the owner sends to under their own name (`self_addresses` in the plan, for the human to confirm), and a recommended plan (deep / headers-only / skip). **No cost or time estimate.** |
| | `approve-survey` | Records that a human reviewed the report. `crawl` and `fetch` refuse to run without it. |
| 1 | `crawl` | Headers plus BODYSTRUCTURE for every message in scope, in plan order. Batched and checkpointed per folder in SQLite; reconnects with backoff. |
| 2 | `filter` | Drops junk, newsletters and automated mail. Uses Apple folder flags (`\Junk`, `\Trash`, Drafts), mailing-list and bulk headers, no-reply and bulk-sending addresses (`mail.`/`e.`/`news.` subdomains and the like), and a per-sender table (`sender-overrides.csv` overrides it). Mail from someone David wrote to is kept, except list/bulk mail he didn't reply to, so a store he once emailed doesn't bring in all its newsletters. "Unsubscribe" emails David sent don't count as writing to someone. Apple Hide-My-Email / Sign-in-with-Apple relay addresses are translated back to the real sender. |
| 3 | `fetch` | The text part of kept messages only, via BODY.PEEK. Splits off quoted history and the signature, then reads the signature: name, title, company, phone. |
| 4 | `thread` | Rebuilds conversations from References/In-Reply-To. Falls back to the same normalized subject plus a shared participant within 180 days. |
| 5 | `link` | Contacts: one row per unique address of a person (no merging of a person's several addresses). A person counts if they wrote to David, David wrote to them, or they were CC'd on a conversation of 10 people or fewer that David or a two-way contact took part in. Automated addresses never count. Shared inboxes (`info@`, `hello@`, `reservations@` …) count only if David wrote to them, and they are listed under their company, not as contacts. Name priority: full name in a header, then full name from the signature, then a `first.last@` address shape, then whatever partial name exists. The owner's own addresses (login, `From:` of Sent mail, `DCR_SELF`, and `self_addresses` in `mining-plan.json`) are never contacts. Links each thread to its contacts. |
| 6 | `projects` | A thread can start a project only if David took part and it shows work: work words in the subject or text, or a document/drawing attachment. Threads that share a contact and (nearly) the same subject words, years ignored, become one project. `Why included` lists the words/files that qualified it. |
| 7 | `export` | Writes the CSVs and `summary.md`. |
| | `run` | crawl → filter → fetch → thread → link → projects → export (gated by the survey). |
| | `status` | Safe while a stage runs. Shows whether a process is active, the last event per stage (UTC), per-folder progress, messages the server refused, and bodies still to fetch. |
| | `seen-audit` | Read-only proof that mining did not mark mail read. The first run saves `seen-audit.json` (unread UIDs per folder); later runs compare and print `RESULT: ...`. `--force` resets the baseline. |

`--dry-run` works with `crawl` (counts only) and `fetch` (reports how many bodies it would fetch).

**Bounded runs.**
- `--since YYYY[-MM-DD]` / `--before YYYY[-MM-DD]` restrict the crawl by date.
- `--limit N` crawls at most N new messages per folder per run, newest first.
- Widening later picks up exactly what is missing, because the crawl's work list is always "UIDs matching the criteria minus UIDs already stored".

**Resuming.**
- Every stage is safe to re-run with the same command after a crash, `kill -9` or VM restart.
- A lock file (`<db>.lock`) makes a second copy exit with a clear message instead of interleaving with a still-running `nohup` job.
- If the server renumbers a folder (UIDVALIDITY change), the crawl re-crawls that folder.
- A message the server refuses to return is retried on later runs, and given up after 3 tries.
- All timestamps are UTC.

## Outputs (`--out`)
| file | one row per | columns |
|---|---|---|
| `contacts.csv` | unique email address | Name, Email, Company, Title, Phone, Domain, Name source (`header`/`signature`/`address`), First contact, Last contact, Messages from them, Messages to them, Threads, Projects |
| `companies.csv` | company domain (or, for personal addresses, a signature company name) | Company, Domain, Contacts, Shared inboxes (that David wrote to), Threads, First contact, Last contact |
| `threads.csv` | conversation with at least one contact or shared inbox | Thread ID, Subject, First date, Last date, Messages, Messages from David, Contacts, Attachments, Project ID (blank if not part of a project) |
| `projects.csv` | project | Project ID, Project, First date, Last date, Years, Threads, Messages, Contacts, Companies, Thread IDs, Why included |

There are also `summary.md` (counts; read first), the survey files, and the SQLite database (`--db`). The database's tables are `messages`, `addrs`, `bodies`, `sigs`, `threads`, `contacts` (`kind` = person or shared inbox), `thread_contacts`, `projects` and `project_threads`; they hold every header, the kept bodies and every link, for later querying.

`staple-seeds.example.json` is a fictional sample. `survey --seeds` counts hits for named staple projects. It accepts a list of `{"name", "terms"}` entries, or a plain `{"name": [terms]}` map.

## Tests (stdlib `unittest`, offline, synthetic mailbox of fictional `*.example` people)
From a fresh clone, run `python3 -m unittest discover -s tests -v`; CI runs it on Python 3.9 and 3.13. `tests/run_demo.sh [outdir]` prints a human-readable end-to-end walk-through.
- `fake_imap.py`: a ~300-message read-only IMAP server. Fixtures:
  - an imported 1998-2004 archive (Date headers vs 2009 INTERNALDATE)
  - raw 8-bit Latin-1 headers and unknown-charset RFC 2047
  - missing or garbage dates
  - duplicate Sent folders
  - a sender with no display name whose name is only in the signature
  - a recurring annual project
  - a modified-UTF-7 folder sent as a LIST literal, and a `\Noselect` parent
  - a client folder named "Bulk orders"
  - clutter found on a real mailbox: a store's shared inbox with newsletters plus one real exchange, `unsubscribe@`/`leave-` emails David sent, a bank's `mail.` sending subdomain, a Hide-My-Email relay sender, and a mass email with 14 other recipients
  - unread messages that a non-PEEK fetch would mark read

  Fault knobs: disconnect before or in the middle of a FETCH response, NO for chosen UIDs, UIDVALIDITY change or omission, slow FETCH.
- `test_parsing`: headers/charsets/dates, folder names, FETCH/BODYSTRUCTURE literals, body decoding, quote/signature split, names from signatures, address kinds (relay decoding, unsubscribe, bulk-sending, shared inbox), signature footer lines that aren't companies.
- `test_pipeline`: survey (no cost/time estimate, imported-archive detection, gate) and a full `run` with an injected disconnect. Then checks the filter, threading, contacts (unique addresses, signature names), thread↔contact links, project grouping (annual job = one project; same-template subjects for different jobs stay apart), output columns, re-run stability, `seen-audit`, and that no secret appears in any output.
- `test_resume`:
  - SIGKILL mid-crawl then resume (exact count, row ids preserved)
  - a concurrent re-run is refused; `status` works during a crawl
  - a mid-FETCH disconnect, and NO for one message
  - UIDVALIDITY change and omission
  - `--since` / `--limit` widening
  - fetch never stores an empty body on failure
- `test_readonly`: the IMAP command audit. A unit matrix of forbidden and allowed commands; live attempts of every mutating imaplib call against the fake server (none may reach the wire, no flag may change); a static scan of the source.
- `test_standalone`: the file copied alone into an empty folder runs under `python3 -I` from another directory; imports are stdlib-only.
- `test_scale`: a 200k-message folder over IMAP (SEARCH reply > 1 MB). `DCR_SCALE=1` adds a 120k-message memory benchmark of the offline stages.

## Known limits
- One person with several addresses appears as several contacts; merging is left for later.
- A real person who sends mostly through a mailing tool (with list headers) is kept only for the mail David replied to, or for mail without those headers.
- People David emailed once from a personal address (e.g. a cold outreach list) are still contacts; the tool doesn't judge who matters.
- A signature name is taken only when it is a clean 2–3 word name line that matches the address or sits right under the sign-off.
- A thread counts as work from word and attachment cues, so a personal thread that mentions e.g. "build" with David replying can become a project. `Why included` shows the cue.
- The survey uses INTERNALDATE for SEARCH windows; for imported archives it adds SENT* searches.
