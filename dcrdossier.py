#!/usr/bin/env python3
"""dcrdossier - staple-project dossier miner (read-only). Needs dcrmine.py next to it and a db from `dcrmine.py crawl/filter/fetch/thread/link` (survey approved).
 python3 dcrdossier.py --seeds staple-seeds.json [--only slug] [--live] [dcrmine connection args: --host --user --plain ...] --db mail.sqlite --out mining-out
Never invents facts: anything not evidenced says 'not found in email'."""
import argparse, collections, csv, json, os, re, sys, time
import dcrmine as D
NF = "not found in email"
KINDS = [("render/design", r"render|mood ?board|concept|sketch|3d|skp|floor ?plan|elevation|drawing"), ("quote/estimate", r"quote|estimate|proposal|bid"), ("invoice", r"invoice|inv[-_ ]?\d"), ("purchase order/contract", r"\bpo\b|purchase[-_ ]?order|contract|agreement|deal[-_ ]?memo|w-?9|coi"),
         ("call sheet/schedule", r"call[-_ ]?sheet|schedule|timeline"), ("brief/client notes", r"brief|notes|feedback|deck|guidelines|spec"), ("photos", r"\.(jpe?g|png|heic)$|photo|progress|install")]
def kind_of(fn): return next((k for k, rx in KINDS if re.search(rx, fn.lower())), "other")
def sentences(t): return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", t or "") if 8 < len(x.strip()) < 260]
def thread_data(db, tid, selfs):
    ms = db.execute("select m.id,m.ts,m.from_addr,m.from_name,m.subject,m.att,m.is_sent,m.folder,b.text from messages m left join bodies b on b.mid=m.id where m.thread=? order by m.ts", (tid,)).fetchall()
    return [dict(m) for m in ms]
def find_primary(db, seed):
    terms = [t.lower() for t in seed["terms"]]; ids = set()
    for t in terms:
        like = f"%{t}%"
        for r in db.execute("select m.id from messages m where m.decision in ('keep','seed') and (lower(m.subject) like ? or lower(m.from_addr) like ? or m.id in (select mid from addrs where lower(addr) like ? or lower(name) like ?) or m.id in (select mid from bodies where lower(text) like ?))", (like,) * 5): ids.add(r[0])
    return ids
def live_seed(db, c, seed, a):
    """server-side SEARCH of every folder for the seed terms; ingest unseen/noise-filtered hits as decision='seed' (headers+structure only; bodies fetched later)"""
    new = 0
    for f in D.list_folders(c):
        if D.is_junk_folder(f) or D.is_drafts_folder(f) or D.is_nonmail_folder(f): continue
        c.select(f["name"]); hits = set()
        for t in seed["terms"]:
            q = D.qs(t); hits |= set(c.search("OR", "SUBJECT", q, "OR", "FROM", q, "OR", "TO", q, "CC", q))
            if not a.no_text_search:
                try: hits |= set(c.search("TEXT", q, retries=1))
                except Exception: pass
        hits = sorted(hits)
        have = {r[0]: r[1] for r in db.execute("select uid,decision from messages where folder=?", (f["name"],))}
        todo = [u for u in hits if u not in have]
        for i in range(0, len(todo), 100):
            r = c.fetch(todo[i:i + 100], f"(UID RFC822.SIZE INTERNALDATE FLAGS {D.HDR_ITEMS} BODYSTRUCTURE)")
            for u, x in r.items(): D.store_message(db, f["name"], 1 if D.is_sent_folder(f) else 0, u, x); new += 1
        db.execute("update messages set decision='seed', dreason='dossier seed hit' where folder=? and uid in (%s) and (decision is null or decision = 'noise')" % ",".join(map(str, hits or [0])), (f["name"],))
        db.commit()
    return new
def ensure_bodies(db, c, a):
    rows = db.execute("select id,folder,uid,parts,from_addr,ts,decision from messages where decision in ('keep','seed') and id not in (select mid from bodies)").fetchall()
    if rows and c: D.fetch_bodies(db, c, rows, a.max_body, a.body_batch)
def run_seed(db, c, a, seed, selfs):
    t0 = time.time(); log = []
    if c: n = live_seed(db, c, seed, a); log.append(f"live search ingested {n} unseen messages")
    D.cmd_thread(a) if c else None
    ensure_bodies(db, c, a)
    if c: D.cmd_thread(a)
    prim_m = find_primary(db, seed); tids = {r[0] for r in db.execute("select distinct thread from messages where id in (%s) and thread is not null" % ",".join(map(str, prim_m or [0])))}
    req = [x.lower() for x in seed.get("require_any", [])]
    def has_req(tid):
        if not req: return True
        txt = " ".join(((m["subject"] or "") + " " + (m["text"] or "")).lower() for m in thread_data(db, tid, selfs)); return any(x in txt for x in req)
    def client_side(tid):
        pt = json.loads(db.execute("select people from threads where id=?", (tid,)).fetchone()[0] or "[]"); nm = " ".join((m["from_name"] or "") for m in thread_data(db, tid, selfs)).lower()
        keys = [x.lower() for x in seed.get("domains", []) + seed.get("people", [])]
        return not keys or any(k in p for p in pt for k in keys) or any(k in nm for k in keys)
    primary = {t for t in tids if has_req(t) and client_side(t)}; log.append(f"round 0: {len(prim_m)} message hits -> {len(tids)} threads, {len(primary)} match the project definition")
    cur = set(primary); related = {t: 0 for t in tids - primary if has_req(t)}; cur |= set(related)
    for rnd in range(1, a.max_rounds + 1):
        contacts = collections.Counter(); ranges = []
        for t in cur:
            r = db.execute("select first_ts,last_ts,people from threads where id=?", (t,)).fetchone()
            if not r: continue
            ranges.append((r[0] - 30 * 86400, r[1] + 60 * 86400))
            for p in json.loads(r[2]): contacts[p] += 1
        key = [p for p, n in contacts.most_common() if (n >= 2 or any(x.lower() in p for x in seed.get("people", []) + seed.get("domains", []))) and D.dom_of(p) not in D.ESP and not D.NOISE_LOCAL.match(p.split("@")[0])][:a.max_contacts]
        if not key or not ranges: break
        if c:
            wins = sorted(ranges); merged = []
            for lo, hi in wins:
                if merged and lo <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], hi)
                else: merged.append([lo, hi])
            for f in D.list_folders(c):
                if D.is_junk_folder(f) or D.is_drafts_folder(f) or D.is_nonmail_folder(f): continue
                c.select(f["name"]); hits = set()
                for ad in key:
                    for lo, hi in merged[:12]:
                        hits |= set(c.search("OR", "FROM", D.qs(ad), "OR", "TO", D.qs(ad), "CC", D.qs(ad), "SENTSINCE", D.dstr(D.year_of(lo)), "SENTBEFORE", D.dstr(D.year_of(hi) + 1)))
                have = {r[0] for r in db.execute("select uid from messages where folder=?", (f["name"],))}; todo = sorted(hits - have)[:a.max_per_round]
                for i in range(0, len(todo), 100):
                    r = c.fetch(todo[i:i + 100], f"(UID RFC822.SIZE INTERNALDATE FLAGS {D.HDR_ITEMS} BODYSTRUCTURE)")
                    for u, x in r.items(): D.store_message(db, f["name"], 1 if D.is_sent_folder(f) else 0, u, x)
                db.execute("update messages set decision='seed', dreason='snowball round %d' where folder=? and uid in (%s) and decision is null" % (rnd, ",".join(map(str, hits or [0]))), (f["name"],)); db.commit()
            ensure_bodies(db, c, a); D.cmd_thread(a)
        new = set()
        for lo, hi in ranges:
            for r in db.execute("select distinct m.thread from messages m join addrs ad on ad.mid=m.id where ad.addr in (%s) and m.ts between ? and ? and m.thread is not null and m.decision in ('keep','seed')" % ",".join("?" * len(key)), (*key, lo, hi)): new.add(r[0])
        new -= cur
        if len(cur) + len(new) > a.max_threads: new = set(sorted(new)[: max(0, a.max_threads - len(cur))])
        for t in new: related[t] = rnd
        log.append(f"round {rnd}: key contacts {len(key)} -> {len(new)} new related threads"); cur |= new
        if not new: break
    return primary, related, log
def money_by_year(items):
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for tid, c in items:
        for m in c["money"]:
            if m["kind"] != "mention": out[c["year"]][m["kind"]].append(m)
    return out
def build_dossier(db, seed, primary, related, a, selfs):
    D.classify_threads(db, a, tids=sorted(primary | set(related)), quiet=True)
    th = {}
    for tid in sorted(primary | set(related)):
        tr = db.execute("select * from threads where id=?", (tid,)).fetchone()
        if tr: th[tid] = {"t": dict(tr), "msgs": thread_data(db, tid, selfs), "c": D.get_class(db, tid) or {}, "primary": tid in primary}
    return th
def render(seed, th, scope_year=None, parent=False, a=None, db=None, selfs=()):
    sel = {k: v for k, v in th.items() if scope_year is None or D.year_of(v["t"]["first_ts"]) == scope_year}
    prim = {k: v for k, v in sel.items() if v["primary"]}
    L = []; w = L.append; yrs = sorted({D.year_of(v["t"]["first_ts"]) for v in prim.values()}); allm = [m for v in sel.values() for m in v["msgs"]]
    title = seed["name"] + (f" - {scope_year}" if scope_year else "")
    deliv = collections.OrderedDict(); money = collections.defaultdict(list); people = {}; atts = []; fb = []; design = []; lessons = []; tl = []
    for tid, v in sorted(sel.items(), key=lambda kv: kv[1]["t"]["first_ts"]):
        c = v["c"]
        for d_ in c.get("deliverables", []): deliv.setdefault(d_, (D.year_of(v["t"]["first_ts"]), v["t"]["subj"]))
        for mo in c.get("money", []):
            if mo["kind"] != "mention": money[D.year_of(v["t"]["first_ts"])].append((mo, v["t"]["subj"]))
        for m in v["msgs"]:
            ad = m["from_addr"]; txt = m["text"] or ""; d0 = D.ymd(m["ts"]); who = "David" if m["is_sent"] else (m["from_name"] or ad)
            if not m["is_sent"] and ad not in selfs: people.setdefault(ad, {"name": m["from_name"], "n": 0, "first": m["ts"], "last": m["ts"], "threads": set()}); p = people[ad]; p["n"] += 1; p["last"] = max(p["last"], m["ts"]); p["first"] = min(p["first"], m["ts"]); p["threads"].add(tid)
            for x in json.loads(m["att"] or "[]"):
                if x.get("name"): atts.append((d0, x["name"], x["type"], x.get("size", 0), who, kind_of(x["name"]), v["t"]["subj"]))
            for sn in sentences(txt):
                if re.search(r"render|mood ?board|concept|sketch|revis|draft|floor ?plan|drawing|mock-?up|material|finish|color|colour|dimension|scale", sn, re.I) and len(design) < 14: design.append((d0, who, sn))
                if not m["is_sent"] and re.search(r"love|like|change|revis|can we|could we|approved|feedback|notes|concern|don'?t like|please (update|add|move|send)|darker|lighter|bigger|smaller|great work|hit", sn, re.I) and len(fb) < 14: fb.append((d0, who, sn))
                if re.search(r"next time|lesson|should have|going forward|for next year|last year|same spec|as last year|remember to|heads up|note for", sn, re.I) and len(lessons) < 10: lessons.append((d0, who, sn))
            kw = [("payment", r"payment sent|\bpaid\b|payment received|thank you for the payment"), ("invoice", r"invoice (attached|will follow|#)|send the invoice|attached invoice"), ("delivery/install", r"delivered|installed|shipped|tracking number|wrapped|strike (is )?complete"), ("build/fabrication", r"underway|progress photos|load[- ]?in complete|fabrication is"),
                  ("approval/PO", r"approved|purchase order|booked|confirmed|you got the job|go ahead"), ("quote sent", r"quote|estimate|bid is|proposal"), ("inquiry/brief", r"are you available|we need|can you (design|build)|pitch|looking for|time to kick off")]
            lab = next((l_ for l_, rx in kw if re.search(rx, txt, re.I)), None)
            if lab: tl.append((lab, m["ts"], who, m["subject"], tid))
    first_in = next((m for v in sorted(prim.values(), key=lambda v: v["t"]["first_ts"]) for m in v["msgs"] if not m["is_sent"]), None)
    org = lambda ad: D_org(db, ad)
    cl = collections.Counter(org(ad) or "?" for ad, p in people.items() for _ in range(p["n"]) if D_dom_ok(ad, seed)).most_common(1)
    props = {"Type": seed.get("type", ""), "Client": cl[0][0] if cl else NF, "Recurring": "Yes" if seed.get("recurring") else "No", "Example template": "Yes (reusable example project template)", "Occurrence year": scope_year or "", "Parent project": seed["name"] if scope_year else "", "Years seen": ", ".join(map(str, yrs)) or NF,
             "Source threads": len(sel), "Total billed": f"${sum(max([mo['amount'] for mo, _ in v if mo['kind'] == 'invoice'] or [0]) for v in money.values()):,.0f}" if any(mo['kind'] == 'invoice' for v in money.values() for mo, _ in v) else NF}
    w(f"# {title}\n"); w("**Properties**\n" + "\n".join(f"- {k}: {v}" for k, v in props.items()) + "\n")
    w("## Overview\n")
    w(f"Found {len(prim)} thread(s) matching this project ({len(sel) - len(prim)} related thread(s) from contacts/vendors) in {sum(len(v['msgs']) for v in sel.values())} messages, years {props['Years seen']}. " + (seed.get("note", "") if parent else ""))
    if first_in: w(f"\nHiring path: the first inbound message in the earliest thread came from {first_in['from_name'] or first_in['from_addr']} at {org(first_in['from_addr']) or first_in['from_addr'].split('@')[-1]} ({D.ymd(first_in['ts'])}, \"{first_in['subject']}\").")
    roles = collections.Counter(v["c"].get("role") for v in prim.values() if v["c"].get("role")); w(f"\nDavid's role: {roles.most_common(1)[0][0] if roles else NF}.")
    summ = [v["c"].get("summary", "") for v in sorted(prim.values(), key=lambda v: v["t"]["first_ts"]) if v["c"].get("summary")]
    for sm in summ[: (3 if parent else 2)]: w(f"- {sm[:420]}")
    if parent and seed.get("title_terms"):
        found = {tt: sorted({D.year_of(v["t"]["first_ts"]) for v in th.values() if tt in " ".join((m["subject"] or "").lower() for m in v["msgs"])}) for tt in seed["title_terms"]}; w("\nSeed titles found in subjects: " + "; ".join(f"{k}: {v or NF}" for k, v in found.items()))
    if parent and seed.get("recurring"): w("\nOccurrences (one child page each): " + (", ".join(f"{y}: " + "; ".join(sorted({v['t']['subj'][:50] for v in prim.values() if D.year_of(v['t']['first_ts']) == y})) for y in yrs) or NF))
    w("\n## Client and key contacts\n\n| Name | Organisation | Title (from signature) | Email | Messages | Active |\n|---|---|---|---|---:|---|")
    for ad, p in sorted(people.items(), key=lambda kv: -kv[1]["n"])[:15]:
        sg = db.execute("select title from sigs where addr=? and title!='' order by ts desc limit 1", (ad,)).fetchone(); w(f"| {p['name'] or NF} | {org(ad) or NF} | {sg[0] if sg else NF} | {ad} | {p['n']} | {D.year_of(p['first'])}-{D.year_of(p['last'])} |")
    if not people: w(NF)
    w("\n## Deliverables\n"); w("\n".join(f"- {d_} (first seen {y}: \"{s[:60]}\")" for d_, (y, s) in deliv.items()) or NF)
    w("\n## Costs, quotes, invoices and payments\n")
    if money:
        w("| Year | Kind | Amount | Date | Evidence (verbatim) |\n|---|---|---:|---|---|")
        for y in sorted(money):
            for mo, sj in money[y]: w(f"| {y} | {mo['kind']} | ${mo['amount']:,.0f} | {mo['date']} | \"{mo['evidence'][:110]}\" |")
        w("\nAmounts are extracted from email text; quotes in a thread may be revised later. Verify against the invoice PDFs listed below.")
    else: w(NF)
    w("\n## Timeline\n")
    if tl:
        for lab, ts, who, subj, tid in sorted(tl, key=lambda x: x[1]): w(f"- {D.ymd(ts)} - {lab} - {who}: \"{subj[:70]}\"")
    else: w(NF)
    w("\n## Design process\n"); w("\n".join(f"- {d0} {wh}: \"{s}\"" for d0, wh, s in design) or NF)
    w("\n## Client materials and feedback\n\nClient-supplied files: " + (", ".join(f"{n} ({d0})" for d0, n, t, sz, wh, k, sj in atts if wh != "David" and k in ("brief/client notes", "purchase order/contract", "call sheet/schedule"))[:600] or NF) + "\n"); w("\n".join(f"- {d0} {wh}: \"{s}\"" for d0, wh, s in fb) or "Feedback: " + NF)
    w("\n## Vendors and fabrication partners\n")
    cdom = {D.dom_of(ad) for ad in people if D_dom_ok(ad, seed)}; vend = []
    for ad, p in people.items():
        o = org(ad)
        if o and not D_dom_ok(ad, seed):
            sg = db.execute("select title from sigs where addr=? and title!='' order by ts desc limit 1", (ad,)).fetchone(); vend.append(f"- {p['name'] or ad} - {o}{' - ' + sg[0] if sg else ''} ({p['n']} msgs, {len(p['threads'])} thread(s); role inferred from participation, verify)")
    w("\n".join(vend) or NF)
    w("\n## Lessons and reuse notes\n")
    yq = {y: max([mo["amount"] for mo, _ in v if mo["kind"] == "quote"] or [0]) for y, v in money.items()}; yq = {y: q for y, q in yq.items() if q}
    if len(yq) >= 2: w("Quote by year: " + ", ".join(f"{y}: ${q:,.0f}" for y, q in sorted(yq.items())) + f" (change {min(yq)}→{max(yq)}: {((yq[max(yq)] / yq[min(yq)]) - 1) * 100:+.0f}%).")
    w("\n".join(f"- {d0} {wh}: \"{s}\"" for d0, wh, s in lessons) or ("" if len(yq) >= 2 else NF))
    w("\n## Attachment inventory\n\n| Date | File | Type | Kind | From | Thread |\n|---|---|---|---|---|---|")
    for d0, n, t, sz, wh, k, sj in sorted(atts)[:60]: w(f"| {d0} | {n} | {t} ({sz//1024} KB) | {k} | {wh} | {sj[:40]} |")
    if not atts: w(f"| | {NF} | | | | |")
    w("\n## Source threads\n\n| First | Last | Subject | Msgs | Primary? | Thread ID |\n|---|---|---|---:|---|---|")
    for tid, v in sorted(sel.items(), key=lambda kv: kv[1]["t"]["first_ts"]): w(f"| {D.ymd(v['t']['first_ts'])} | {D.ymd(v['t']['last_ts'])} | {v['t']['subj'][:70]} | {v['t']['n']} | {'yes' if v['primary'] else 'related'} | {tid} |")
    return "\n".join(L) + "\n", atts
def D_dom_ok(ad, seed): return any(x.lower() in ad.lower() for x in seed.get("domains", [])) or not seed.get("domains")
def D_org(db, ad): return D.org_by_addr(db, ad)
def main():
    ap = argparse.ArgumentParser(add_help=False); ap.add_argument("--seeds", default="staple-seeds.json"); ap.add_argument("--only"); ap.add_argument("--live", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=3); ap.add_argument("--max-contacts", type=int, default=8); ap.add_argument("--max-threads", type=int, default=150); ap.add_argument("--max-per-round", type=int, default=400)
    mine, rest = ap.parse_known_args(); a = D.build_parser().parse_args(["status"] + rest); a.__dict__.update(vars(mine)); db = D.opendb(a.db); D.need_survey(db)
    selfs = set(json.loads(D.meta_get(db, "self", "[]"))); c = D.Conn(a) if a.live else None
    for seed in json.load(open(a.seeds)):
        if a.only and seed["slug"] != a.only: continue
        print(f"== dossier: {seed['name']}"); primary, related, log = run_seed(db, c, a, seed, selfs); [print("  " + l) for l in log]
        th = build_dossier(db, seed, primary, related, a, selfs); out = os.path.join(a.out, "dossiers", seed["slug"]); os.makedirs(out, exist_ok=True)
        yrs = sorted({D.year_of(v["t"]["first_ts"]) for v in th.values() if v["primary"]})
        md, atts = render(seed, th, None, True, a, db, selfs); open(os.path.join(out, "parent.md" if seed.get("recurring") else "project.md"), "w").write(md)
        if seed.get("recurring"):
            for y in yrs: open(os.path.join(out, f"{y}.md"), "w").write(render(seed, th, y, False, a, db, selfs)[0])
        csv.writer(open(os.path.join(out, "attachments.csv"), "w", newline="")).writerows([["date", "file", "type", "size", "from", "kind", "thread"]] + [[d0, n, t, s, wh, k, sj] for d0, n, t, s, wh, k, sj in sorted(atts)])
        json.dump({"seed": seed, "log": log, "primary": sorted(primary), "related": related, "years": yrs}, open(os.path.join(out, "dossier.json"), "w"), indent=1)
        print(f"  -> {out}/ ({len(primary)} primary + {len(related)} related threads, years {yrs})")
if __name__ == "__main__": main()
