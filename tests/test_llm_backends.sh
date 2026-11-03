#!/usr/bin/env bash
# Exercises the scaledown + openai-compatible backends against mock_llm.py (request shapes per docs; NOT a model-quality test). Needs demo-out/ from run_demo.sh.
set -euo pipefail; cd "$(dirname "$0")"; PORT=${PORT:-1199}
python3 mock_llm.py $PORT & M=$!; trap 'kill $M 2>/dev/null||true' EXIT; sleep 1
C="python3 dcrmine.py --db demo-out/mail.sqlite --out demo-out"
echo "-- dry-run (no calls)"; SCALEDOWN_API_KEY=sd-test-key SCALEDOWN_BASE=http://127.0.0.1:$PORT $C classify --backend scaledown --force --dry-run
echo "-- scaledown backend"; SCALEDOWN_API_KEY=sd-test-key SCALEDOWN_BASE=http://127.0.0.1:$PORT $C classify --backend scaledown --force 2>&1 | tail -3
echo "-- cached rerun (should queue 0 calls)"; SCALEDOWN_API_KEY=sd-test-key SCALEDOWN_BASE=http://127.0.0.1:$PORT $C classify --backend scaledown 2>&1 | tail -1
echo "-- openai-compatible backend (+ScaleDown compress for long threads)"; LLM_BASE_URL=http://127.0.0.1:$PORT LLM_MODEL=mock $C classify --backend openai --force 2>&1 | tail -2
python3 - <<'PY'
import json,sqlite3
db=sqlite3.connect("demo-out/mail.sqlite")
for be in ("scaledown","openai"):
    rows=db.execute("select js from tclass where backend=?",(be,)).fetchall(); js=[json.loads(r[0]) for r in rows]
    proj=[j for j in js if j["is_project"]]
    print(be, "threads classified:",len(js),"projects:",len(proj))
    bad=[j for j in js if "Hallucinated" in json.dumps(j) or "Totally Invented" in json.dumps(j) or "unicorn" in json.dumps(j) or "999999" in json.dumps(j)]
    print("  hallucinations leaked into results:",len(bad)); assert not bad
    ex=[j for j in proj if j["source"].endswith(be)][0]; print("  sample:",{k:ex[k] for k in ("client","project_name","status","source")}, "| summary:",ex["summary"][:70])
PY
