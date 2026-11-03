# Runbook: full mailbox backfill with `dcrmine.py` (for the bot)

This runbook is for the bot that runs the backfill on the Debian VM as user `box`. Follow the steps in order. Two steps need the human: step 2 (approve the survey) and step 3 (look at the pilot). Everything else is yours to do.

**What this produces:** the people David has emailed with over the years (`contacts.csv`), their companies (`companies.csv`), every conversation linked to those people (`threads.csv`), and work conversations grouped into projects (`projects.csv`).

**Rules that never bend**
- The tool only reads mail. It never sends, deletes, moves or marks anything as read. Do not edit `dcrmine.py`.
- Never print, log or save the password. It reaches the tool only through the `ICLOUD_APP_PASSWORD` env var. Do not `echo` it, do not write it into a file, and do not paste it into chat.
- Do not add cost or time estimates to anything you send the human.

## 0. One-time setup
```bash
mkdir -p ~/dcr-mining/out && cd ~/dcr-mining
curl -fsSL -o dcrmine.py https://raw.githubusercontent.com/Andrewwilliamross/creative-bd-mail-mining/main/dcrmine.py
sha256sum dcrmine.py        # must equal the sha256 pinned in your skill (from the PR that shipped this version). If not, stop and tell the human.
python3 --version           # 3.9 or newer
M="python3 $HOME/dcr-mining/dcrmine.py --db $HOME/dcr-mining/dcr_mail.sqlite --out $HOME/dcr-mining/out"
$M --version
```
These env vars must already be set: `DCR_IMAP_USER` (David's Apple ID email) and `ICLOUD_APP_PASSWORD` (an app-specific password, not the Apple ID password). Check that they exist without printing them:
```bash
test -n "$DCR_IMAP_USER" && test -n "$ICLOUD_APP_PASSWORD" && echo "credentials present"
```
Define `M` again in every new shell (it is just a shortcut).

## 1. Snapshot unread mail (proof we change nothing)
```bash
$M seen-audit
```
This saves which messages are unread right now. In step 5 the same command proves none of them changed.

## 2. Survey, then the human approves
```bash
nohup $M survey > ~/dcr-mining/out/survey.log 2>&1 &
```
The survey takes minutes, not hours. Wait until `$M status` says `running: no`, then send the human `out/survey-report.md` and ask:
1. **"Are these all your own email addresses?"** Show the list under "Possibly your own other addresses". Put the confirmed list (plus any others they name) in `out/mining-plan.json` under `"self_addresses"`, and remove any they say are not theirs. Their own addresses must never show up as contacts.
2. **"Any folders to skip?"** Show the plan table. Only change `out/mining-plan.json` if they ask.

When they say it looks right:
```bash
$M approve-survey
```

## 3. Pilot: the last two years only
```bash
nohup $M --since 2025 run > ~/dcr-mining/out/run-pilot.log 2>&1 &
```
Check on it with `$M status`, about every 10 minutes. When it says `running: no` and `export done`, send the human `out/summary.md` and the top 30 rows of `out/contacts.csv`. Ask: "Do these look like your real contacts? Anyone here who shouldn't be, or anyone missing?"
- **Someone shouldn't be there** (a store, a newsletter): add a line `address,noise` (or `domain.com,noise`) to `out/sender-overrides.csv`.
- **Someone is missing** because their mail was treated as junk: add `address,keep`.

Then re-run the same pilot command. It only redoes the cheap steps.

## 4. Full backfill: every year
```bash
nohup $M run > ~/dcr-mining/out/run-full.log 2>&1 &
```
No `--since` means all years. This can take many hours on a big mailbox; that is normal.
- **Check progress:** `$M status`. It shows each folder as `stored/matching`, and `bodies still to fetch`.
- **If it stopped** (`running: no`, but `export` isn't `done`, or `bodies still to fetch` > 0): run the exact same command again. It continues where it stopped and never duplicates anything. Do the same after a VM restart, a dropped connection, or `Killed`.
- **If it says "another dcrmine process is already running"**: one is still working. Leave it alone and check `status` later.
- **If the log shows the server refusing login:** stop and ask the human to make a new app-specific password at appleid.apple.com. Don't retry in a loop.
- **"messages the server would not return"** in `status` is fine. They are retried three times on later runs, then skipped.

## 5. Prove nothing changed
```bash
$M seen-audit
```
It must print `RESULT: no message changed from unread to read`. If it prints anything else, tell the human right away and send them the output.

## 6. Hand over
Send the human:
- `out/summary.md`, the counts (read this first)
- `out/contacts.csv`, one row per person (unique email address)
- `out/companies.csv`, companies, with their shared inboxes (`info@`, `hello@` …) that David wrote to
- `out/threads.csv`, every conversation with its contacts and, if any, its project
- `out/projects.csv`, work conversations grouped by job

Keep `dcr_mail.sqlite`. It holds everything, so later questions can be answered without touching the mailbox again.

## Fixing results later (no mailbox access needed)
After editing `out/sender-overrides.csv` or `self_addresses`:
```bash
$M filter && $M thread && $M link && $M projects && $M export
```
If the filter now keeps mail it dropped before, run `$M run` once more to download those message texts.

## What gets left out on purpose
- newsletters, receipts, notifications and other automated mail, even from companies David once wrote to
- "unsubscribe" emails, which count as David writing to that address
- Apple "Hide My Email" relay addresses (they are translated back to the real sender)
- company inboxes like `info@` or `hello@` David never wrote to (the ones he did write to are listed in `companies.csv`)
- people who were only CC'd on mass emails, or on threads nobody answered. People CC'd on a small conversation (10 people or fewer) that David or a two-way contact was part of are kept.
