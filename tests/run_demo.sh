#!/usr/bin/env bash
# Human-readable end-to-end demo of dcrmine against the synthetic read-only fake IMAP server (the assertions live in the unittest suite).
# Usage from a fresh clone: tests/run_demo.sh [outdir]      (default: tests/demo-out)
set -euo pipefail
cd "$(dirname "$0")"; ROOT=$(cd .. && pwd); PY=${PYTHON:-python3}
OUT=${1:-demo-out}; PORT=${PORT:-1143}; rm -rf "$OUT"; mkdir -p "$OUT"
export DCR_IMAP_USER=dcr.demo@mac.com ICLOUD_APP_PASSWORD=testpw     # fixture-only throwaway password
$PY fake_imap.py serve --port $PORT --drop-after ${DROP_AFTER:-6} --log "$OUT/imap-commands.log" > "$OUT/server.out" 2>&1 & SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT; sleep 1.5
CONN=(--plain --host 127.0.0.1 --port "$PORT" --db "$OUT/mail.sqlite" --out "$OUT" --timeout 10)
C=("$PY" "$ROOT/dcrmine.py" "${CONN[@]}")
echo "== Stage 0 survey"; "${C[@]}" --seeds "$ROOT/staple-seeds.example.json" survey
echo "== crawl before approval must be refused"; ("${C[@]}" crawl 2>&1 || true) | tail -1
"${C[@]}" approve-survey
echo "== mining"; for s in crawl filter fetch thread link projects export; do echo "-- $s"; "${C[@]}" $s 2>&1 | tail -4; done
echo "== outputs"; for f in contacts companies threads projects; do printf '%-14s %s rows\n' "$f.csv" "$(($(wc -l < "$OUT/$f.csv") - 1))"; done
echo "== read-only audit of every IMAP command the server received"
$PY - "$OUT/imap-commands.log" <<'PY'
import re,sys,collections
c=collections.Counter(); bad=[]
for l in open(sys.argv[1]):
    l=l.strip()
    if not l or l.startswith("INJECTED"): continue
    if l.startswith("VIOLATION"): bad.append(l); continue
    parts=l.split(" ",2); cmd=parts[1].upper() if len(parts)>1 else ""
    if cmd=="UID": cmd="UID "+parts[2].split(" ",1)[0].upper()
    c[cmd]+=1
print(dict(c)); allowed={"CAPABILITY","LOGIN","LIST","STATUS","EXAMINE","UID FETCH","UID SEARCH","LOGOUT","NOOP"}
assert not bad, bad; assert set(c)<=allowed, set(c)-allowed
txt=open(sys.argv[1]).read(); assert not re.search(r"BODY\[|RFC822(?!\.SIZE)",txt.replace("BODY.PEEK[","")), "non-PEEK fetch seen"
print("READ-ONLY AUDIT PASSED: only", sorted(c), "| no STORE/COPY/APPEND/EXPUNGE/DELETE/SELECT, all body fetches BODY.PEEK; injected disconnect:", "INJECTED DISCONNECT" in txt)
PY
