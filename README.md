# dcrmine: deep mailbox mining for the David Clay Ross BD bot

Read-only IMAP miner (Python 3.9+, **standard library only**) that turns a full-history iCloud mailbox into Notion-ready CSVs: contacts, companies, projects (with recurring parents and yearly occurrences), pipeline, a past-client **reconnect queue**, and per-project **dossiers**.

## Safety model
- **Read-only.** Folders are opened with `EXAMINE` (`select(readonly=True)`); bodies use `BODY.PEEK` only; the only IMAP verbs ever issued are `CAPABILITY LOGIN LIST STATUS EXAMINE UID SEARCH UID FETCH (LOGOUT)`. A code assertion refuses any other fetch form, and the test fixture's server refuses (and logs as VIOLATION) every other verb. Nothing is moved, flagged, deleted, or sent. No SMTP code exists.
- **Secrets.** The Apple app-specific password is read from the env var `ICLOUD_APP_PASSWORD` (name configurable with `--pass-env`) and is never written to disk, logs, or the database. Username: `--user` or `DCR_IMAP_USER`. Host defaults to `imap.mail.me.com:993` (SSL).
- **No judgement calls on people.** Nobody is auto-excluded for sentiment or disputes; those become neutral context notes. Only `do-not-contact.csv` (David's list) excludes anyone; bounced addresses go to `address-lookup.csv`.

## Stages (`python3 dcrmine.py [options] <command>`)
| # | command | what it does |
|---|---|---|
| 0 | `survey` | **Mandatory first step.** Read-only, cheap ops only (LIST, STATUS, SEARCH SINCE/BEFORE counts, header samples via BODY.PEEK). Writes `survey-report.md`, `survey.json`, `mining-plan.json`. Reports per-folder counts/size/date range, volume per year, top sender/recipient domains by year band, Sent correspondents, noise share, attachment-heavy threads, gaps, imported archives (Date header vs INTERNALDATE divergence), duplicate folders, encoding/date quirks, keyword and seed hits per year, estimated message counts for mining, and a recommended plan (deep / headers-only / skip, in priority phases). **No cost or time estimate.** |
| | `approve-survey` | Records that the human reviewed the report. `crawl` and `fetch` refuse to run without it. |
| 1 | `crawl` | All folders in plan order, headers + BODYSTRUCTURE only, batched, checkpointed per folder (UIDVALIDITY + last UID) in SQLite; resumable after any crash; reconnects with backoff. |
| 2 | `filter` | Spam/noise pre-filter with a per-sender decision table (`senders` table; override with `sender-overrides.csv`). Rescue rules keep human-written project mail; anything David wrote or replied to is always kept. Bounces are routed, not dropped. |
| 3 | `fetch` | Bodies for kept messages only (BODY.PEEK of the text part), quote/signature split, signature parsing (title, company, phone). |
| 4 | `thread` | References/In-Reply-To union-find, fallback on normalized subject + participants within 180 days. |
| 5 | `link` | Entity linking: people merged across aliases, organisations by domain/signature, job changes detected from sequential employers, segment + ICP signal, market from phone area code. |
| 6 | `classify` | Heuristic always; optional ScaleDown or OpenAI-compatible model on project-like threads only; cached by content hash; grounding guard drops any model-invented name or amount. `--backend heuristic\|scaledown\|openai\|auto`. |
| 7 | `export` | CSVs + `summary.md` (read first). |
| | `voice` | `voice-notes.md`: David's note style from his short personal sent mail. |
| | `run` | survey-gated chain of the above. `status` shows progress. |
`--dry-run` is supported by `crawl` (counts only, stores nothing), `fetch` (reports how many bodies it would fetch) and `classify` (no model calls). The survey is always read-only and cheap.

`dcrdossier.py` (needs `dcrmine.py` next to it): `python3 dcrdossier.py --seeds staple-seeds.json [--only slug] [--live]` builds one dossier per seed: seed-term search (local DB, plus live server-side SEARCH with `--live`), `require_any` filter to define the project, snowball expansion through key contacts within time windows (caps: `--max-rounds 3 --max-contacts 8 --max-threads 150 --max-per-round 400`, dedupe by thread), attachment inventory, timeline, and markdown with the ten required sections; recurring projects get `parent.md` plus one `<year>.md` per occurrence. Anything not in email says "not found in email".

## Outputs (`--out`)
`survey-report.md`, `mining-plan.json`, `summary.md`, `contacts.csv` (+`contacts_full.csv`), `companies.csv`, `projects.csv`, `project_threads.csv`, `pipeline.csv`, **`reengagement.csv`** (the reconnect queue: tier, kind, priority/score, memory hint, flags: JOB CHANGE / DEAD ADDRESS / DO NOT CONTACT / neutral CONTEXT), `address-lookup.csv`, `reengagement-excluded-dnc.csv`, `voice-notes.md`, `dossiers/<slug>/...`, `mail.sqlite`.

## Tests (all run offline against a synthetic mailbox of fictional `*.example` people)
- `fake_imap.py`: ~295-message mailbox, minimal read-only IMAP server, injects a disconnect. Includes an imported 1998-2003 archive (Date headers vs 2009 INTERNALDATE), duplicate Sent folders, bounce, job change, dispute, recurring Pepsi/Paramount threads, NOBL via an agency.
- `run_demo.sh [outdir]`: survey, refusal before approval, full pipeline, cache re-run, do-not-contact, dossiers, and a command-log audit that fails on any non-read-only verb.
- `test_resume.sh`: SIGKILL mid-crawl, resume, assert no duplicates and exact count.
- `test_llm_backends.sh` + `mock_llm.py`: ScaleDown and OpenAI-compatible paths against mocks built from the vendor docs, including a planted hallucination that must not leak.

## Known limits
- "Total billed" = the largest invoice amount seen per thread; deposit+balance invoicing understates. Amounts are extracted from email text; check PDFs.
- Heuristic summaries are extractive; segment/ICP assignment is heuristic; "David's role" is only filled when an inbound brief says so.
- ScaleDown/OpenAI paths were exercised only against mocks. Real iCloud behaviour (rate limiting, folder naming) is untested here; the code backs off and reconnects.
- Survey uses INTERNALDATE for SEARCH windows; for imported archives it adds SENT* searches.
