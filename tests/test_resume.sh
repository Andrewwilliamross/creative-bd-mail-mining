#!/usr/bin/env bash
# Kill the crawler mid-run (SIGKILL), re-run, and prove it resumes from the SQLite checkpoint with no duplicates.
set -euo pipefail; cd "$(dirname "$0")"; PORT=${PORT:-1177}; D=$(mktemp -d); export DCR_IMAP_USER=dcr.demo@mac.com ICLOUD_APP_PASSWORD=testpw
python3 fake_imap.py serve --port $PORT --log $D/cmds.log >/dev/null 2>&1 & S=$!; trap 'kill $S 2>/dev/null||true' EXIT; sleep 1.5
cp demo-out/mining-plan.json $D/; python3 -c "
import dcrmine,sys; db=dcrmine.opendb('$D/m.sqlite'); dcrmine.meta_set(db,'survey_done','test'); dcrmine.meta_set(db,'survey_approved','test')"
C="python3 dcrmine.py --plain --host 127.0.0.1 --port $PORT --db $D/m.sqlite --out $D --timeout 10 --batch 7 --delay 0.15"
timeout -s KILL 2.5 $C crawl >/dev/null 2>&1 || true
python3 -c "import sqlite3;db=sqlite3.connect('$D/m.sqlite');print('after SIGKILL: messages =',db.execute('select count(*) from messages').fetchone()[0],'| checkpoints:',db.execute('select display,done,total,last_uid from folders').fetchall()[:3])"
$C --dry-run crawl 2>&1 | tail -2
$C crawl 2>&1 | tail -1
python3 -c "
import sqlite3;db=sqlite3.connect('$D/m.sqlite');n=db.execute('select count(*) from messages').fetchone()[0];u=db.execute('select count(*) from (select distinct folder,uid from messages)').fetchone()[0];print('after resume: messages =',n,'unique (folder,uid) =',u);import fake_imap;B=fake_imap.build();e=sum(len(v) for k,v in B.f.items() if k not in ('Junk','Trash','Drafts'));assert n==u==e,(n,u,e);print('expected',e)"
echo RESUME TEST PASSED
