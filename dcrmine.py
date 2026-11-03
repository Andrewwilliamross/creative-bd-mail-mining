#!/usr/bin/env python3
"""dcrmine - READ-ONLY deep mailbox miner (stdlib only).  Stages:
 survey -> approve-survey -> crawl -> filter -> fetch -> thread -> link -> classify -> export   (or: run)
Mail safety: EXAMINE (readonly) only; FETCH uses BODY.PEEK only; commands limited to LIST/STATUS/EXAMINE/SEARCH/FETCH.
Secrets: password only from env (default ICLOUD_APP_PASSWORD); never printed or stored."""
import argparse, base64, collections, csv, datetime as dt, email, email.utils, hashlib, html, imaplib, json, os, quopri
import re, socket, sqlite3, ssl, sys, time, urllib.error, urllib.request
from email.header import decode_header, make_header
from html.parser import HTMLParser

VERSION = "1.0"
NOW = time.time(); THIS_YEAR = dt.datetime.now().year
def log(*a): print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS folders(name TEXT PRIMARY KEY, display TEXT, flags TEXT, uidvalidity INTEGER, last_uid INTEGER DEFAULT 0, total INTEGER, done INTEGER DEFAULT 0, is_sent INTEGER DEFAULT 0, updated REAL);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, folder TEXT, uid INTEGER, msgid TEXT, irt TEXT, refs TEXT, from_addr TEXT, from_name TEXT, subject TEXT, ts INTEGER, date_bad INTEGER DEFAULT 0, size INTEGER, has_att INTEGER DEFAULT 0, att TEXT, parts TEXT, bulk INTEGER DEFAULT 0, hints TEXT, is_sent INTEGER DEFAULT 0, decision TEXT, dreason TEXT, thread INTEGER, UNIQUE(folder,uid));
CREATE TABLE IF NOT EXISTS addrs(mid INTEGER, role TEXT, addr TEXT, name TEXT);
CREATE INDEX IF NOT EXISTS ix_addrs_mid ON addrs(mid); CREATE INDEX IF NOT EXISTS ix_addrs_addr ON addrs(addr);
CREATE INDEX IF NOT EXISTS ix_msg_msgid ON messages(msgid); CREATE INDEX IF NOT EXISTS ix_msg_thread ON messages(thread);
CREATE TABLE IF NOT EXISTS senders(addr TEXT PRIMARY KEY, name TEXT, domain TEXT, n INTEGER, n_replied INTEGER, n_bulk INTEGER, written_to INTEGER, first_ts INTEGER, last_ts INTEGER, decision TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS bodies(mid INTEGER PRIMARY KEY, text TEXT, quoted TEXT, sig TEXT, trunc INTEGER);
CREATE TABLE IF NOT EXISTS sigs(mid INTEGER, addr TEXT, name TEXT, title TEXT, company TEXT, phone TEXT, site TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS threads(id INTEGER PRIMARY KEY, subj TEXT, first_ts INTEGER, last_ts INTEGER, n INTEGER, people TEXT);
CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, emails TEXT, org_id INTEGER, title TEXT, phone TEXT, first_ts INTEGER, last_ts INTEGER, n_in INTEGER, n_out INTEGER, last_two_way INTEGER, prev_orgs TEXT, moved INTEGER DEFAULT 0, merged_by TEXT);
CREATE TABLE IF NOT EXISTS email_person(addr TEXT PRIMARY KEY, pid INTEGER);
CREATE TABLE IF NOT EXISTS orgs(id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT, domains TEXT, site TEXT, segment TEXT, market TEXT, first_ts INTEGER, last_ts INTEGER, n INTEGER, icp_signal TEXT);
CREATE TABLE IF NOT EXISTS person_org(pid INTEGER, oid INTEGER, title TEXT, first_ts INTEGER, last_ts INTEGER, n INTEGER, PRIMARY KEY(pid,oid));
CREATE TABLE IF NOT EXISTS tclass(tid INTEGER, backend TEXT, hash TEXT, pv TEXT, js TEXT, ts REAL, PRIMARY KEY(tid,backend));
"""
def opendb(path):
    db = sqlite3.connect(path); db.row_factory = sqlite3.Row
    db.executescript(SCHEMA); return db
def meta_get(db, k, d=None):
    r = db.execute("select v from meta where k=?", (k,)).fetchone(); return r[0] if r else d
def meta_set(db, k, v):
    db.execute("insert or replace into meta values(?,?)", (k, v)); db.commit()

# ---------------------------------------------------------------- text helpers
def dh(v):
    if v is None: return ""
    try: return str(make_header(decode_header(str(v)))).replace("\r", "").replace("\n", " ").strip()
    except Exception: return re.sub(r"\s+", " ", str(v)).strip()
def utf7d(s):
    def rep(m):
        t = m.group(1)
        if not t: return "&"
        try: return base64.b64decode(t.replace(",", "/") + "=" * (-len(t) % 4)).decode("utf-16-be")
        except Exception: return m.group(0)
    return re.sub(r"&([^-]*)-", rep, s)
def norm_name(n):
    n = dh(n).strip().strip("\"'")
    if "," in n and "@" not in n:
        p = [x.strip() for x in n.split(",", 1)]
        if len(p) == 2 and p[0] and p[1] and " " not in p[0]: n = p[1] + " " + p[0]
    return re.sub(r"\s+", " ", n).strip()
def name_key(n):
    t = re.sub(r"[^a-z ]", "", norm_name(n).lower()).split()
    return " ".join(t) if len(t) >= 2 and "@" not in n else ""
def parse_addrs(v):
    out = []
    for nm, ad in email.utils.getaddresses([dh(v)] if v else []):
        ad = (ad or "").strip().lower()
        if "@" in ad and " " not in ad: out.append((ad, norm_name(nm)))
    return out
FREE = set("gmail.com googlemail.com yahoo.com ymail.com aol.com hotmail.com outlook.com live.com msn.com icloud.com me.com mac.com comcast.net att.net sbcglobal.net earthlink.net verizon.net bellsouth.net cox.net charter.net protonmail.com proton.me gmx.com mail.com juno.com netscape.net roadrunner.com rocketmail.com".split())
SLD2 = {"co.uk", "com.au", "co.nz", "co.jp", "com.br", "org.uk", "ac.uk"}
def reg_domain(d):
    p = d.lower().split(".")
    if len(p) >= 3 and ".".join(p[-2:]) in SLD2: return ".".join(p[-3:])
    return ".".join(p[-2:]) if len(p) >= 2 else d
def dom_of(addr): return reg_domain(addr.split("@")[-1]) if "@" in addr else ""
def norm_subject(s):
    s = dh(s).lower()
    for _ in range(6):
        n = re.sub(r"^\s*((re|fw|fwd|aw|sv|antw|tr)\s*(\[\d+\])?\s*:|\[[^\]]{1,20}\])\s*", "", s)
        if n == s: break
        s = n
    return re.sub(r"\s+", " ", s).strip()
def ymd(ts): return time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else ""
def year_of(ts): return time.gmtime(ts).tm_year if ts else 0

# ---------------------------------------------------------------- noise heuristics
ESP = set("""mailchimp.com mcsv.net mcdlv.net rsgsv.net sendgrid.net sendgrid.com constantcontact.com ccsend.com rs6.net mailgun.org mailgun.com amazonses.com sparkpostmail.com sparkpost.com hubspot.com hubspotemail.net hs-send.com klaviyo.com klaviyomail.com exacttarget.com et.com salesforce.com marketo.com mktomail.com campaign-archive.com createsend.com cmail19.com cmail20.com campaignmonitor.com mandrillapp.com mailjet.com sendinblue.com brevo.com convertkit.com substack.com beehiiv.com eventbrite.com e.eventbrite.com linkedin.com facebookmail.com twitter.com x.com pinterest.com instagram.com quora.com meetup.com nextdoor.com youtube.com paypal.com ebay.com etsy.com shopify.com squarespace.com wix.com godaddy.com groupon.com zoom.us docusign.net""".split())
NOISE_LOCAL = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply|notifications?|notify|alerts?|newsletter|news|mailer[-_.]?daemon|postmaster|bounce[sd]?|bounces?|updates?|marketing|promo(tions?)?|offers?|deals?|info@|billing|receipts?|orders?|support|customerservice|automated|system|auto[-_.]?reply)\b", re.I)
SUBJ_NOISE = re.compile(r"(\d+\s?% off|\bsale\b|free shipping|newsletter|unsubscribe|webinar|your (order|receipt|statement|subscription|password)|has shipped|order confirmation|special offer|limited time|last chance|digest|weekly update|verify your|security alert|promo code|coupon|you'?re invited|rsvp|e-?receipt)", re.I)
SUBJ_PROJECT = re.compile(r"(quote|estimate|invoice|\bpo\b|purchase order|deposit|call sheet|render|set design|build|load[- ]?in|strike|trophy|bid\b|budget|fabricat|mock-?up|drawings?|floor ?plan|mood ?board|shoot|prop|backdrop|install|sketch|proposal)", re.I)
PROJECT_ATT = re.compile(r"\.(pdf|xlsx?|docx?|jpe?g|png|tiff?|psd|ai|dwg|dxf|skp|3dm|rvt|indd|mov|mp4|zip)$", re.I)
def noise_hints(h):
    """h: email.message.Message headers. returns (bulk_flag, [hints])"""
    hs = []
    g = lambda k: dh(h.get(k, ""))
    if g("List-Unsubscribe") or g("List-Id") or g("List-Post"): hs.append("list")
    if g("Precedence").lower() in ("bulk", "list", "junk"): hs.append("precedence")
    if g("Auto-Submitted").lower() not in ("", "no"): hs.append("auto-submitted")
    if g("X-Campaign") or g("X-Mailchimp-ID") or g("X-MC-User") or g("Feedback-ID") or g("X-SG-EID") or g("X-Mailer").lower().startswith(("mailchimp", "sendgrid", "constant")): hs.append("esp-header")
    fa = parse_addrs(h.get("From", ""))
    if fa:
        ad = fa[0][0]; loc, dom = ad.split("@")[0], ad.split("@")[1]
        if NOISE_LOCAL.match(loc + ("@" if loc == "info" else "")): hs.append("noreply-local")
        if reg_domain(dom) in ESP or dom in ESP: hs.append("esp-domain")
    return (1 if hs else 0), hs

# ---------------------------------------------------------------- IMAP layer (read-only)
class Conn:
    ALLOWED_UID = {"FETCH", "SEARCH"}
    def __init__(s, a): s.a = a; s.m = None; s.folder = None
    def connect(s):
        a = s.a
        if s.m:
            try: s.m.logout()
            except Exception: pass
        s.m = None
        pw = os.environ.get(a.pass_env, "")
        if not a.user or not pw: raise SystemExit(f"Set --user (or DCR_IMAP_USER) and env {a.pass_env} (app-specific password).")
        m = imaplib.IMAP4(a.host, a.port, timeout=a.timeout) if a.plain else imaplib.IMAP4_SSL(a.host, a.port, ssl_context=ssl.create_default_context(), timeout=a.timeout)
        m.login(a.user, pw); s.m = m
        if s.folder: s.select(s.folder, retry=False)
    def retry(s, fn, retries=8):
        delay = 2
        for i in range(retries):
            try:
                if s.m is None: s.connect()
                return fn(s.m)
            except (imaplib.IMAP4.abort, ConnectionError, socket.timeout, ssl.SSLError, OSError, EOFError) as e:
                log(f"IMAP link problem ({type(e).__name__}: {str(e)[:80]}); reconnect in {delay}s ({i+1}/{retries})")
                s.m = None; time.sleep(delay); delay = min(delay * 2, 120)
        raise RuntimeError("IMAP: giving up after repeated disconnects (progress is checkpointed; re-run to resume)")
    @staticmethod
    def q(name): return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    def select(s, name, retry=True):
        fn = lambda m: m.select(s.q(name), readonly=True)   # EXAMINE: server cannot change flags
        typ, d = s.retry(fn) if retry else fn(s.m)
        if typ != "OK": raise ValueError(f"cannot examine {name}: {d}")
        s.folder = name; return d
    def list(s):
        typ, d = s.retry(lambda m: m.list()); return d
    def status(s, name, items):
        typ, d = s.retry(lambda m: m.status(s.q(name), items)); return d
    def uidvalidity(s):
        typ, d = s.retry(lambda m: m.response("UIDVALIDITY")); return int(d[0]) if d and d[0] else 0
    def search(s, *crit, retries=8):
        typ, d = s.retry(lambda m: m.uid("SEARCH", *crit), retries)
        return [int(x) for x in (d[0] or b"").split()] if typ == "OK" and d and d[0] else []
    def fetch(s, uids, items, retries=8):
        if re.search(r"BODY\[|RFC822(?!\.SIZE)|BINARY\[(?!.*PEEK)", items.replace("PEEK[", "PEEK(")):
            raise AssertionError("non-PEEK fetch refused (would set \\Seen)")
        typ, d = s.retry(lambda m: m.uid("FETCH", ",".join(map(str, uids)), items), retries)
        return parse_fetch(d) if typ == "OK" else {}

def parse_list_line(item):
    lit = None
    if isinstance(item, tuple): item, lit = item[0] + b"\x00", item[1]
    line = item.decode("latin-1")
    m = re.match(r'\((?P<fl>[^)]*)\)\s+(?P<dl>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<nm>.*)$', line.rstrip("\x00"))
    if not m: return None
    nm = m.group("nm").strip()
    if lit is not None: nm = lit.decode("latin-1")
    elif nm.startswith('"'): nm = re.sub(r"\\(.)", r"\1", nm[1:-1])
    return {"name": nm, "flags": m.group("fl"), "delim": m.group("dl").strip('"'), "display": utf7d(nm)}
def list_folders(c):
    out = []
    for it in c.list():
        if it is None: continue
        f = parse_list_line(it)
        if f and "\\noselect" not in f["flags"].lower(): out.append(f)
    return out
def is_sent_folder(f):
    return "\\sent" in f["flags"].lower() or bool(re.search(r"(^|[/.])(sent( (messages|items|mail))?|outbox)$", f["display"], re.I))
def is_junk_folder(f): return bool(re.search(r"\\(junk|trash)", f["flags"].lower())) or bool(re.search(r"(^|[/.])(junk|spam|trash|deleted( items| messages)?|bulk mail)$", f["display"], re.I))
def is_drafts_folder(f): return "\\drafts" in f["flags"].lower() or bool(re.search(r"(^|[/.])drafts?$", f["display"], re.I))
def is_nonmail_folder(f): return bool(re.search(r"(^|[/.])(notes|calendar|contacts|tasks|journal|conversation history|rss.*)$", f["display"], re.I))

def sexp(s, i=0):
    while s[i] == " ": i += 1
    c = s[i]
    if c == "(":
        i += 1; L = []
        while True:
            while s[i] == " ": i += 1
            if s[i] == ")": return L, i + 1
            v, i = sexp(s, i); L.append(v)
    if c == '"':
        j = i + 1; b = []
        while s[j] != '"':
            if s[j] == "\\": j += 1
            b.append(s[j]); j += 1
        return "".join(b), j + 1
    m = re.compile(r'[^\s()"]+').match(s, i); v = m.group()
    return (None if v.upper() == "NIL" else v), m.end()
def parse_fetch(data):
    msgs = []; cur = None
    start = re.compile(rb"^\s*\d+ \(")
    for it in data:
        if isinstance(it, tuple):
            head, lit = it
            if cur is None or start.match(head):
                if cur is not None: msgs.append(cur)
                cur = ["", [], False]
            cur[1].append(lit); cur[0] += re.sub(rb"\{\d+\}$", b"", head).decode("latin-1") + "\x00%d\x00" % (len(cur[1]) - 1)
        elif isinstance(it, bytes):
            if start.match(it):
                if cur is not None: msgs.append(cur)
                cur = ["", [], False]
            if cur is None: continue
            cur[0] += it.decode("latin-1"); cur[2] = True
    if cur is not None: msgs.append(cur)
    out = {}
    for text, lits, _ in msgs:
        m = re.search(r"UID (\d+)", text)
        if not m: continue
        r = {"uid": int(m.group(1)), "sections": {}}
        for k, rx in (("size", r"RFC822\.SIZE (\d+)"), ("idate", r'INTERNALDATE "([^"]+)"'), ("flags", r"FLAGS \(([^)]*)\)")):
            mm = re.search(rx, text); r[k] = mm.group(1) if mm else None
        for mm in re.finditer(r"BODY\[([^\]]*)\](?:<\d+>)?\s*\x00(\d+)\x00", text):
            r["sections"][mm.group(1).upper()] = lits[int(mm.group(2))]
        mm = re.search(r"BODYSTRUCTURE ", text)
        if mm:
            try: r["struct"] = sexp(text, mm.end())[0]
            except Exception: r["struct"] = None
        out[r["uid"]] = r
    return out
def walk_struct(node, path=""):
    """yield dict per leaf part"""
    if not node: return
    if isinstance(node[0], list):
        i = 0; n = 0
        while i < len(node) and isinstance(node[i], list):
            n += 1; yield from walk_struct(node[i], (path + "." if path else "") + str(n)); i += 1
        return
    pr = lambda l: {str(l[j]).lower(): l[j + 1] for j in range(0, len(l) - 1, 2)} if isinstance(l, list) else {}
    typ, sub = (node[0] or "text").lower(), (node[1] or "plain").lower()
    params = pr(node[2] if len(node) > 2 else None)
    disp, dparams = "", {}
    for e in node[7:]:
        if isinstance(e, list) and e and isinstance(e[0], str) and e[0].lower() in ("attachment", "inline"):
            disp = e[0].lower(); dparams = pr(e[1] if len(e) > 1 else None)
    fn = dh(dparams.get("filename") or params.get("name") or "")
    try: size = int(node[6])
    except Exception: size = 0
    yield {"path": path or "1", "type": typ + "/" + sub, "charset": (params.get("charset") or "").lower(), "enc": (node[5] or "7bit").lower() if len(node) > 5 else "7bit", "size": size, "disp": disp, "filename": fn}

# ---------------------------------------------------------------- header parsing (shared by survey + crawl)
HDR_FIELDS = "MESSAGE-ID IN-REPLY-TO REFERENCES FROM TO CC REPLY-TO DATE SUBJECT LIST-ID LIST-UNSUBSCRIBE LIST-POST PRECEDENCE AUTO-SUBMITTED X-MAILER X-CAMPAIGN X-MAILCHIMP-ID X-MC-USER FEEDBACK-ID X-SG-EID SENDER"
HDR_ITEMS = f"BODY.PEEK[HEADER.FIELDS ({HDR_FIELDS})]"
def parse_idate(s):
    try:
        d = email.utils.parsedate_to_datetime(s)
        if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
        return int(d.timestamp())
    except Exception: return None
def parse_headers(hb, idate=None):
    msg = email.message_from_bytes(hb or b""); issues = []
    raw_date = msg.get("Date"); ts = None
    if not raw_date: issues.append("no-date")
    else:
        try:
            d = email.utils.parsedate_to_datetime(str(raw_date))
            if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
            ts = int(d.timestamp())
        except Exception: issues.append("bad-date")
    idts = parse_idate(idate) if idate else None; date_bad = 0
    if ts is not None and (year_of(ts) < 1980 or ts > NOW + 400 * 86400): issues.append("date-out-of-range"); ts = None
    if ts is None: ts = idts or 0; date_bad = 1
    elif idts and abs(ts - idts) > 400 * 86400: issues.append("date-vs-internal")
    for k in ("Subject", "From", "To"):
        v = msg.get(k)
        if v:
            try: str(make_header(decode_header(str(v))))
            except Exception: issues.append("header-encoding"); break
    if hb and any(b > 127 for b in hb): issues.append("raw-8bit-header")
    bulk, hints = noise_hints(msg)
    fr = parse_addrs(msg.get("From", ""))
    subj = dh(msg.get("Subject", ""))
    refs = re.findall(r"<[^>]+>", str(msg.get("References", "")))
    irt = re.findall(r"<[^>]+>", str(msg.get("In-Reply-To", "")))
    mid = re.findall(r"<[^>]+>", str(msg.get("Message-ID", "")))
    if not mid: issues.append("no-message-id")
    return {"msgid": mid[0].lower() if mid else "", "irt": irt[0].lower() if irt else "", "refs": [r.lower() for r in refs], "from": fr[0] if fr else ("", ""),
            "to": parse_addrs(msg.get("To", "")), "cc": parse_addrs(msg.get("Cc", "")), "subject": subj, "ts": ts, "idts": idts, "date_bad": date_bad,
            "bulk": bulk, "hints": hints, "issues": issues, "is_reply": bool(re.match(r"\s*(re|aw)\s*:", subj, re.I) or irt)}
def struct_info(st):
    parts, atts = [], []
    for p in walk_struct(st) if st else []:
        if p["type"].startswith("text/") and p["disp"] != "attachment" and not p["filename"]: parts.append(p)
        elif p["type"].startswith("multipart/"): continue
        else: atts.append({"name": p["filename"], "type": p["type"], "size": p["size"], "inline": p["disp"] == "inline"})
    return parts, [x for x in atts if x["name"] or not x["inline"]]

# ---------------------------------------------------------------- STAGE 0: SURVEY (cheap ops only; headers sampled, no bodies)
BANDS = [("1993-1999", 0, 1999), ("2000-2004", 2000, 2004), ("2005-2009", 2005, 2009), ("2010-2014", 2010, 2014), ("2015-2019", 2015, 2019), ("2020-now", 2020, 3000)]
def band_of(y): return next(b[0] for b in BANDS if b[1] <= y <= b[2])
SURVEY_KW = ["quote", "estimate", "invoice", "purchase order", "PO", "deposit", "call sheet", "render", "set design", "build", "load-in", "strike", "trophy", "set"]
NOISY_KW = {"po", "set", "build", "strike", "render"}   # SUBJECT search is substring-based; these over-match (e.g. 'offset', 'post')
DEFAULT_SEEDS = {"Pepsi / Rookie of the Year": ["pepsi", "rookie of the year"], "Paramount": ["paramount"], "Charlie Ward": ["charlie ward"], "NOBL": ["nobl"],
                 "SpongeBob": ["spongebob"], "Street Fighter": ["street fighter"], "Ebenezer": ["ebenezer"]}
def dstr(y): return f"1-Jan-{y}"
def qs(x): return '"' + x.replace('"', "") + '"'
def window(c, kind, lo, hi):
    p = "SENT" if kind == "sent" else ""
    if lo is None: return set(c.search(f"{p}BEFORE", dstr(hi)))
    if hi is None: return set(c.search(f"{p}SINCE", dstr(lo)))
    return set(c.search(f"{p}SINCE", dstr(lo), f"{p}BEFORE", dstr(hi)))
def year_sets(c, kind, lo, hi):
    u = window(c, kind, lo, hi)
    if not u: return {}
    if hi - lo == 1: return {lo: u}
    mid = (lo + hi) // 2; out = {}; out.update(year_sets(c, kind, lo, mid)); out.update(year_sets(c, kind, mid, hi)); return out
def folder_years(c, kind):
    ys = year_sets(c, kind, 1993, THIS_YEAR + 1)
    pre = window(c, kind, None, 1993)
    fut = window(c, kind, THIS_YEAR + 1, None)
    if pre: ys[1992] = pre          # 1992 key == "before 1993"
    if fut: ys[THIS_YEAR + 1] = fut  # future-dated
    return ys
def pick(uids, k):
    u = sorted(uids)
    if len(u) <= k: return u
    return [u[int(i * (len(u) - 1) / (k - 1))] for i in range(k)] if k > 1 else u[:1]
def fetch_headers(c, uids, batch=100):
    out = {}
    for i in range(0, len(uids), batch):
        r = c.fetch(uids[i:i + batch], f"(UID RFC822.SIZE INTERNALDATE {HDR_ITEMS} BODYSTRUCTURE)")
        for u, x in r.items():
            h = parse_headers(x["sections"].get(f"HEADER.FIELDS ({HDR_FIELDS})", b""), x.get("idate"))
            parts, atts = struct_info(x.get("struct")); h["size"] = int(x.get("size") or 0); h["atts"] = atts; out[u] = h
    return out
def survey_collect(a):
    c = Conn(a); t0 = time.time(); S = {"generated": time.strftime("%Y-%m-%d %H:%M"), "host": a.host, "user_domain": (a.user or "").split("@")[-1], "folders": [], "seeds": {}, "keywords": {}, "quirks": []}
    seeds = json.load(open(a.seeds)) if a.seeds else DEFAULT_SEEDS
    kws = [k.strip() for k in a.keywords.split(",")] if a.keywords else SURVEY_KW
    fl = list_folders(c); log(f"survey: {len(fl)} selectable folders")
    W = collections.defaultdict(lambda: collections.defaultdict(float))   # weighted counters
    folder_data = {}; selfaddr = collections.Counter(); sent_corr = {}; recv_from = collections.Counter(); att_subj = {}; msgid_sets = {}
    for f in fl:
        nm = f["name"]; info = {"name": nm, "display": f["display"], "flags": f["flags"], "depth": f["display"].count(f["delim"]) if f["delim"] not in ("NIL", "") else 0,
                                "sent": is_sent_folder(f), "junk": is_junk_folder(f), "drafts": is_drafts_folder(f), "nonmail": is_nonmail_folder(f)}
        try:
            st = " ".join(x.decode("latin-1") if isinstance(x, bytes) else str(x) for x in c.status(nm, "(MESSAGES UIDNEXT UIDVALIDITY)"))
        except Exception as e: st = ""; info["status_error"] = str(e)[:80]
        g = lambda k: int(m.group(1)) if (m := re.search(k + r" (\d+)", st)) else None
        info["messages"], info["uidnext"], info["uidvalidity"] = g("MESSAGES"), g("UIDNEXT"), g("UIDVALIDITY")
        S["folders"].append(info)
        if not info["messages"] or info["nonmail"]: continue
        c.select(nm); log(f"survey: {f['display']} ({info['messages']} msgs)")
        ys = folder_years(c, "internal"); info["years_internal"] = {str(y): len(u) for y, u in sorted(ys.items())}
        info["accounted"] = sum(len(u) for u in ys.values())
        allsets = ys; sample_n = K = a.sent_sample if info["sent"] else a.sample
        recs = []
        for y, u in sorted(ys.items()):
            sel = pick(u, K); hh = fetch_headers(c, sel)
            for uid, h in hh.items(): h["win"] = y; h["w"] = len(u) / max(1, len(sel)); h["uid"] = uid; recs.append(h)
        # imported-archive detection: Date header vs INTERNALDATE
        div = sum(1 for h in recs if "date-vs-internal" in h["issues"]) / max(1, len(recs))
        info["divergence"] = round(div, 2)
        info["import_name_hint"] = bool(re.search(r"import|archive|old|mbox|backup|restored|transferred|on my mac|pre-?\d{4}|\b(19|20)\d\d\b", f["display"], re.I))
        info["imported_archive"] = div >= 0.4
        if info["imported_archive"]:
            ysd = folder_years(c, "sent"); info["years_sent_header"] = {str(y): len(u) for y, u in sorted(ysd.items())}
        info["date_basis"] = "SENTSINCE" if info["imported_archive"] else "SINCE"
        # per-folder sample statistics
        wsum = sum(h["w"] for h in recs) or 1; info["sampled"] = len(recs)
        info["noise_share"] = round(sum(h["w"] for h in recs if h["bulk"]) / wsum, 3)
        info["noise_reasons"] = dict(collections.Counter(x for h in recs if h["bulk"] for x in h["hints"]).most_common(8))
        info["att_share"] = round(sum(h["w"] for h in recs if h["atts"]) / wsum, 3)
        info["proj_att_share"] = round(sum(h["w"] for h in recs if any(PROJECT_ATT.search(x["name"] or "") for x in h["atts"])) / wsum, 3)
        info["heavy_share"] = round(sum(h["w"] for h in recs if h["size"] > 1_500_000) / wsum, 3)
        info["avg_size_kb"] = round(sum(h["w"] * h["size"] for h in recs) / wsum / 1024, 1)
        info["reply_share"] = round(sum(h["w"] for h in recs if h["is_reply"]) / wsum, 3)
        info["issues"] = {k: round(sum(h["w"] for h in recs if k in h["issues"]), 1) for k in ("no-date", "bad-date", "date-out-of-range", "header-encoding", "raw-8bit-header", "no-message-id")}
        info["issues"] = {k: v for k, v in info["issues"].items() if v}
        dy = collections.Counter(); [dy.update({year_of(h["ts"]): h["w"]}) for h in recs]
        info["years_date_header_est"] = {str(y): round(v) for y, v in sorted(dy.items()) if y}
        yrs = [int(y) for y in info["years_internal"]]; info["first_internal"], info["last_internal"] = min(yrs), max(yrs)
        dyr = [y for y in dy if y]; info["first_date_hdr"], info["last_date_hdr"] = (min(dyr), max(dyr)) if dyr else (None, None)
        msgid_sets[nm] = {h["msgid"] for h in recs if h["msgid"]}
        folder_data[nm] = (info, recs, ys)
        for h in recs:
            b = band_of(year_of(h["ts"]) if h["ts"] else h["win"]); w = h["w"]; fa = h["from"][0]
            if info["sent"]:
                selfaddr[fa] += w
                for ad, n in h["to"] + h["cc"]:
                    d = sent_corr.setdefault(ad, {"n": 0.0, "name": n, "years": set(), "first": 9999, "last": 0}); d["n"] += w
                    y = year_of(h["ts"]); d["years"].add(y); d["first"] = min(d["first"], y); d["last"] = max(d["last"], y)
                    if n and not d["name"]: d["name"] = n
                    W["to_dom_" + b][dom_of(ad) or ad] += w
            elif not (info["junk"] or info["drafts"]):
                if fa: recv_from[fa] += w; W["from_dom_" + b][dom_of(fa)] += w
                for ad, n in h["to"] + h["cc"]: W["to_dom_" + b][dom_of(ad)] += w
            if h["bulk"] and fa: W["noise_dom"][dom_of(fa)] += w
            if h["atts"] and not h["bulk"]:
                k = norm_subject(h["subject"])[:80]
                if k:
                    d = att_subj.setdefault(k, {"n": 0.0, "mb": 0.0, "exts": collections.Counter(), "years": set(), "folder": info["display"]}); d["n"] += w; d["mb"] += w * h["size"] / 1e6
                    d["years"].add(year_of(h["ts"])); d["exts"].update(os.path.splitext(x["name"] or "")[1].lower() or x["type"] for x in h["atts"])
    # keyword + seed searches (server-side SEARCH; no data transfer)
    log("survey: keyword/seed counts")
    searchable = [(i, folder_data[i["name"]]) for i in S["folders"] if i["name"] in folder_data and not (i["junk"] or i["drafts"])]
    for k in kws: S["keywords"][k] = {"total": 0, "by_folder": {}, "noisy": k.lower() in NOISY_KW}
    seed_hits = {}
    for info, (_i, recs, ys) in searchable:
        c.select(info["name"]); unions = set().union(*ys.values()) if ys else set()
        for k in kws:
            u = set(c.search("SUBJECT", qs(k)))
            if u: S["keywords"][k]["by_folder"][info["display"]] = len(u); S["keywords"][k]["total"] += len(u)
        for sname, terms in seeds.items():
            sd = S["seeds"].setdefault(sname, {"terms": terms, "header_hits": 0, "text_hits": 0, "by_folder": {}, "by_year": {}, "contacts": {}, "subjects": []})
            hh, tt = set(), set()
            for t in terms:
                hh |= set(c.search("OR", "SUBJECT", qs(t), "OR", "FROM", qs(t), "OR", "TO", qs(t), "CC", qs(t)))
                if not a.no_text_search:
                    try: tt |= set(c.search("TEXT", qs(t), retries=1))
                    except Exception as e: sd["text_search_error"] = type(e).__name__; c.m = None
            tt |= hh
            if hh or tt:
                sd["header_hits"] += len(hh); sd["text_hits"] += len(tt); sd["by_folder"][info["display"]] = {"header": len(hh), "text": len(tt)}
                basis = folder_years(c, "sent") if info["imported_archive"] else ys
                for y, u in basis.items():
                    n = len(u & tt)
                    if n: sd["by_year"][str(y)] = sd["by_year"].get(str(y), 0) + n
                seed_hits.setdefault(sname, []).append((info["name"], pick(tt, 15)))
    for sname, lst in seed_hits.items():                           # who are the people/domains behind each seed (header peek only)
        sd = S["seeds"][sname]; ca = collections.Counter(); cd = collections.Counter(); sj = []
        for fname, uids in lst:
            c.select(fname)
            for uid, h in fetch_headers(c, uids).items():
                for ad, n in [h["from"]] + h["to"] + h["cc"]:
                    if ad and ad not in selfaddr and dom_of(ad) not in ("mac.com", "me.com", "icloud.com"): ca[(ad, n)] += 1; cd[dom_of(ad)] += 1
                sj.append((ymd(h["ts"]), h["subject"][:80]))
        sd["contacts"] = [{"addr": k[0], "name": k[1], "n": v} for k, v in ca.most_common(12)]; sd["domains"] = cd.most_common(8); sd["subjects"] = sorted(set(sj))[:12]
    # aggregates
    selfs = [a_ for a_, _ in selfaddr.most_common(5)]; S["self_addresses"] = selfs
    yt = collections.Counter(); ytd = collections.Counter()
    for info in S["folders"]:
        for y, n in info.get("years_internal", {}).items(): yt[int(y)] += n
        for y, n in (info.get("years_sent_header") or info.get("years_date_header_est") or {}).items(): ytd[int(y)] += n
    S["year_totals_internal"] = {str(k): v for k, v in sorted(yt.items())}; S["year_totals_date_header"] = {str(k): v for k, v in sorted(ytd.items())}
    S["bands"] = {b[0]: {"from": [(d, round(v)) for d, v in sorted(W["from_dom_" + b[0]].items(), key=lambda x: -x[1])[:15]], "to": [(d, round(v)) for d, v in sorted(W["to_dom_" + b[0]].items(), key=lambda x: -x[1])[:15]]} for b in BANDS}
    S["noise_domains"] = [(d, round(v)) for d, v in sorted(W["noise_dom"].items(), key=lambda x: -x[1])[:20]]
    recv_n = collections.Counter({k: v for k, v in recv_from.items()})
    S["sent_correspondents"] = sorted([{"addr": k, "name": v["name"], "est_sent": round(v["n"]), "first": v["first"], "last": v["last"], "years": len(v["years"]), "replied_back_est": round(recv_n.get(k, 0)), "two_way": recv_n.get(k, 0) > 0} for k, v in sent_corr.items() if k not in selfaddr and v["n"] >= 1], key=lambda x: -x["est_sent"])[:60]
    S["att_threads"] = sorted([{"subject": k, "est_msgs": round(v["n"]), "est_mb": round(v["mb"], 1), "types": dict(v["exts"].most_common(4)), "years": f"{min(v['years'])}-{max(v['years'])}", "folder": v["folder"]} for k, v in att_subj.items() if v["n"] >= 2 or v["mb"] > 5], key=lambda x: -x["est_mb"])[:20]
    # candidate organisation domains (two-way first), from weighted domain counters over all bands
    dom_in, dom_out = collections.Counter(), collections.Counter()
    for b in BANDS:
        for d, v in W["from_dom_" + b[0]].items(): dom_in[d] += v
        for d, v in W["to_dom_" + b[0]].items(): dom_out[d] += v
    noise_d = {d for d, v in W["noise_dom"].items() if v >= 0.5 * (dom_in.get(d, 0) or 1)}
    cand = []
    for d in set(dom_in) | set(dom_out):
        if not d or d in FREE or d in noise_d or d in ESP or d == S["user_domain"]: continue
        sent_to = sum(v["n"] for k, v in sent_corr.items() if dom_of(k) == d)
        cand.append({"domain": d, "est_in": round(dom_in.get(d, 0)), "est_sent_to": round(sent_to), "two_way": bool(sent_to and dom_in.get(d)), "score": round(2 * sent_to + dom_in.get(d, 0) + (30 if sent_to and dom_in.get(d) else 0))})
    S["candidate_domains"] = sorted(cand, key=lambda x: -x["score"])[:40]
    S["freemail_correspondents"] = [x for x in S["sent_correspondents"] if dom_of(x["addr"]) in FREE][:15]
    # gaps / duplicates / quirks
    ys = {int(k): v for k, v in (S["year_totals_date_header"] or S["year_totals_internal"]).items()}
    if ys:
        lo, hi = min(ys), max(ys); vals = [ys.get(y, 0) for y in range(max(lo, 1993), hi + 1)]
        med = sorted(vals)[len(vals) // 2] if vals else 0
        S["gap_years"] = [y for y in range(max(lo, 1993), hi + 1) if ys.get(y, 0) == 0 or (med >= 20 and ys.get(y, 0) < 0.15 * med)]
    dups = []
    names = list(folder_data)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            A, B = folder_data[names[i]][0], folder_data[names[j]][0]; sa, sb = msgid_sets[names[i]], msgid_sets[names[j]]
            ov = len(sa & sb) / max(1, min(len(sa), len(sb)))
            same_family = (A["sent"] and B["sent"]) or re.sub(r"\W|s$", "", A["display"].lower()) == re.sub(r"\W|s$", "", B["display"].lower())
            if ov >= 0.3 or (A["messages"] == B["messages"] and A["messages"] > 20 and A["uidnext"] == B["uidnext"]) or (same_family and A["messages"] and B["messages"]):
                dups.append({"a": A["display"], "b": B["display"], "sample_msgid_overlap": round(ov, 2), "same_count": A["messages"] == B["messages"]})
    S["duplicate_folders"] = dups
    S["sample_sizes"] = {"per_year_per_folder": a.sample, "sent_per_year": a.sent_sample}
    S["elapsed_s"] = round(time.time() - t0)
    return S

def survey_plan(S):
    folders = []
    seed_by_folder = collections.Counter()
    for sd in S["seeds"].values():
        for f, v in sd["by_folder"].items(): seed_by_folder[f] += v["text"]
    for i in S["folders"]:
        if not i.get("messages") or i["nonmail"]: continue
        d = i["display"]; why = []
        if i["sent"]: act, pr = "deep", 1; why.append("Sent: strongest two-way signal")
        elif i["junk"]: act, pr = ("deep" if seed_by_folder[d] else "skip"), 9; why.append("Junk/Trash" + ("; but contains seed hits" if seed_by_folder[d] else ""))
        elif i["drafts"]: act, pr = "skip", 9; why.append("Drafts (unsent)")
        elif i.get("noise_share", 0) >= 0.8 and not seed_by_folder[d] and i.get("proj_att_share", 0) < 0.05: act, pr = "headers", 8; why.append(f"{int(i['noise_share']*100)}% bulk/noise and no seed hits")
        else:
            act = "deep"; dens = (1 - i.get("noise_share", 0)); pr = 2 if (seed_by_folder[d] or i.get("proj_att_share", 0) > 0.1 or i["imported_archive"]) else 3; why.append(f"{int(dens*100)}% non-noise in sample")
        if i.get("imported_archive"): why.append("imported archive: INTERNALDATE != Date header; filter by Date (SENTSINCE)")
        if seed_by_folder[d]: why.append(f"{seed_by_folder[d]} seed hits")
        yrs = {int(y): n for y, n in (i.get("years_sent_header") or i["years_internal"]).items() if 1993 <= int(y) <= THIS_YEAR}
        rng = [min(yrs), max(yrs)] if yrs else None
        folders.append({"name": i["name"], "display": d, "action": act, "priority": pr, "messages": i["messages"], "date_basis": i["date_basis"], "years": rng, "est_non_noise": round(i["messages"] * (1 - i.get("noise_share", 0))), "why": "; ".join(why)})
    folders.sort(key=lambda x: (x["priority"], -x["messages"]))
    pd = [x["domain"] for x in S["candidate_domains"] if x["two_way"]][:25]
    sd_ = [d for d, v in S["noise_domains"] if v >= 3][:40]
    return {"version": 1, "generated": S["generated"], "folders": folders, "priority_domains": pd, "skip_domains": sd_,
            "phases": ["A: deep-mine Sent folders (all years)", "B: seed dossiers (live SEARCH) once A is done", "C: remaining deep folders by priority, newest year-band first", "D: headers-only folders (contacts table completeness)"]}

def survey_report(S, P):
    L = []; w = L.append; tot = sum(f.get("messages") or 0 for f in S["folders"])
    w(f"# Mailbox survey report\n\nGenerated {S['generated']} · host `{S['host']}` · {len(S['folders'])} selectable folders · **{tot:,} messages** · survey used only LIST / STATUS / EXAMINE / SEARCH counts and sampled header peeks (no bodies fetched). Sample: up to {S['sample_sizes']['per_year_per_folder']} headers per folder-year ({S['sample_sizes']['sent_per_year']} for Sent). Percentages and domain volumes are *estimates scaled from samples*; counts from SEARCH are exact.\n")
    w("**Review required:** read sections 1-9, edit `mining-plan.json` if needed, then run `approve-survey`. Mining will not start before that.\n")
    w("## 1. Folders\n\n| Folder | Msgs | Date range (INTERNALDATE) | Noise | Attach | Flags |\n|---|---:|---|---:|---:|---|")
    for i in S["folders"]:
        fl = [x for x in ("sent", "junk", "drafts", "nonmail") if i.get(x)] + (["IMPORTED-ARCHIVE"] if i.get("imported_archive") else [])
        rng = (f"{i.get('first_internal','')}-{i.get('last_internal','')}" + (f" (Date hdr {i.get('first_date_hdr')}-{i.get('last_date_hdr')})" if i.get("imported_archive") else "")) if i.get("years_internal") else ""
        w(f"| {i['display']} | {i.get('messages') or 0:,} | {rng} | {int(i.get('noise_share',0)*100)}% | {int(i.get('att_share',0)*100)}% | {', '.join(fl)} |")
    w("\n## 2. Volume per year (exact SEARCH counts; folders summed)\n")
    w("| Year | INTERNALDATE count | Date-header est. |  | Year | INTERNALDATE count | Date-header est. |\n|---|---:|---:|---|---|---:|---:|")
    yrs = sorted(set(map(int, S["year_totals_internal"])) | set(map(int, S["year_totals_date_header"]))); half = (len(yrs) + 1) // 2
    for k in range(half):
        a_, b_ = yrs[k], (yrs[k + half] if k + half < len(yrs) else None)
        cell = lambda y: f"{'pre-1993' if y == 1992 else y} | {S['year_totals_internal'].get(str(y), 0):,} | {S['year_totals_date_header'].get(str(y), 0):,}" if y is not None else " | | "
        w(f"| {cell(a_)} |  | {cell(b_)} |")
    w("\nPer-folder per-year counts are in `survey.json` (`folders[].years_internal`).")
    if S.get("gap_years"): w(f"\n**Gap/dip years:** {', '.join(map(str, S['gap_years']))} (zero or <15% of median) - ask David whether mail from these years lives elsewhere (old computer, other account, mbox exports).")
    w("\n## 3. Top correspondent domains by year band (est. messages)\n")
    for b in BANDS:
        d = S["bands"][b[0]]
        if not d["from"] and not d["to"]: continue
        w(f"- **{b[0]}** - from: " + ", ".join(f"{x} ({n})" for x, n in d["from"][:8]) + " · to/cc: " + ", ".join(f"{x} ({n})" for x, n in d["to"][:8]))
    w("\n## 4. Sent-folder correspondents (strongest two-way signal)\n\n| Person | Address | Est. msgs sent | Years | Wrote back? |\n|---|---|---:|---|---|")
    for x in S["sent_correspondents"][:30]: w(f"| {x['name']} | {x['addr']} | {x['est_sent']} | {x['first']}-{x['last']} | {'yes (~'+str(x['replied_back_est'])+')' if x['two_way'] else 'not seen in samples'} |")
    w("\n## 5. Noise share\n")
    w("Overall bulk/list/noreply/ESP share by folder is in section 1. Top noise domains (est.): " + ", ".join(f"{d} ({n})" for d, n in S["noise_domains"][:12]))
    for i in S["folders"]:
        if i.get("noise_reasons"): w(f"- {i['display']}: " + ", ".join(f"{k} {v}" for k, v in i["noise_reasons"].items()))
    w("\n## 6. Attachment-heavy threads (sampled; headers + BODYSTRUCTURE only)\n\n| Subject | Est. msgs | Est. MB | Types | Years | Folder |\n|---|---:|---:|---|---|---|")
    for x in S["att_threads"][:12]: w(f"| {x['subject']} | {x['est_msgs']} | {x['est_mb']} | {x['types']} | {x['years']} | {x['folder']} |")
    w("\n## 7. Gaps, imported archives and quirks\n")
    for i in S["folders"]:
        q = []
        if i.get("imported_archive"): q.append(f"imported archive ({int(i['divergence']*100)}% of samples have Date header far from INTERNALDATE; Date-header range {i['first_date_hdr']}-{i['last_date_hdr']}, INTERNALDATE range {i['first_internal']}-{i['last_internal']})")
        elif i.get("import_name_hint"): q.append("name suggests archive/import")
        if i.get("issues"): q.append("issues in samples (est. msgs): " + ", ".join(f"{k}={v}" for k, v in i["issues"].items()))
        if i.get("accounted") is not None and i["accounted"] != i["messages"]: q.append(f"{i['messages']-i['accounted']} msgs not covered by date windows")
        if q: w(f"- **{i['display']}**: " + "; ".join(q))
    for d in S["duplicate_folders"]: w(f"- Possible duplicate/overlapping folders: **{d['a']}** and **{d['b']}** (sample Message-ID overlap {d['sample_msgid_overlap']}, same count: {d['same_count']}) - the crawl de-duplicates by Message-ID, but confirm with David.")
    w("\n## 8. Where the signal is\n\n**Candidate client/organisation domains** (two-way first): " + ", ".join(f"{x['domain']}{' ⇄' if x['two_way'] else ''} (in {x['est_in']}/out {x['est_sent_to']})" for x in S["candidate_domains"][:25]))
    if S["freemail_correspondents"]: w("\n**Free-mail correspondents David writes to** (individuals, often freelancers/vendors): " + ", ".join(f"{x['name'] or x['addr']} ({x['est_sent']})" for x in S["freemail_correspondents"][:10]))
    w("\n**Subject keyword hits** (exact SEARCH counts; ‡ = substring match, inflated): " + ", ".join(f"{k}{'‡' if v['noisy'] else ''} {v['total']}" for k, v in S["keywords"].items()))
    w("\n**Staple-project seeds** (header = subject/from/to/cc matches; text = incl. bodies; by year = exact):\n")
    for n, sd in S["seeds"].items():
        w(f"- **{n}** (terms: {', '.join(sd['terms'])}): header hits {sd['header_hits']}, text hits {sd['text_hits']}" + (f" (text search error: {sd['text_search_error']})" if sd.get("text_search_error") else "") + (" · years: " + ", ".join(f"{y}:{v}" for y, v in sorted(sd['by_year'].items())) if sd["by_year"] else ""))
        if sd["contacts"]: w("  - contacts seen: " + ", ".join(f"{x['name'] or x['addr']} <{x['addr']}> ×{x['n']}" for x in sd["contacts"][:6]))
        if sd["subjects"]: w("  - sample subjects: " + "; ".join(f"{d} {s}" for d, s in sd["subjects"][:5]))
    est_all = sum(f["messages"] for f in P["folders"] if f["action"] != "skip"); est_deep = sum(f["est_non_noise"] for f in P["folders"] if f["action"] == "deep")
    rs = [i.get("reply_share", 0) * (i.get("messages") or 0) for i in S["folders"] if i.get("messages")]
    est_threads = round(est_deep * (1 - (sum(rs) / max(1, tot))))
    w(f"\n## 9. Estimated volumes for the mining stages (messages, not time or cost)\n\n- Header crawl (stage 1): ~{est_all:,} messages in folders not skipped.\n- Body fetch after the noise filter (stage 3): ~{est_deep:,} messages in deep folders (sample-based; real number known after `filter`).\n- Threads to classify: roughly {est_threads:,} (non-reply share of kept messages; real number known after `thread`).")
    w("\n## 10. Recommended mining plan (also in `mining-plan.json`)\n\n| Order | Folder | Action | Msgs | Dates by | Years | Why |\n|---:|---|---|---:|---|---|---|")
    for n, f in enumerate(P["folders"], 1): w(f"| {n} | {f['display']} | **{f['action']}** | {f['messages']:,} | {f['date_basis']} | {'-'.join(map(str, f['years'])) if f['years'] else ''} | {f['why']} |")
    w("\nPhases: " + " → ".join(P["phases"]))
    w("\n**Mine first (priority senders/domains):** " + (", ".join(P["priority_domains"][:20]) or "none identified") + "\n\n**Skip as noise:** " + (", ".join(P["skip_domains"][:25]) or "none identified") + "\n\nSkipped/headers-only folders still get a header crawl if you choose, so contacts stay complete; bodies are never fetched for them.")
    return "\n".join(L) + "\n"

def cmd_survey(a):
    S = survey_collect(a); P = survey_plan(S); os.makedirs(a.out, exist_ok=True)
    json.dump(S, open(os.path.join(a.out, "survey.json"), "w"), indent=1, default=list)
    json.dump(P, open(os.path.join(a.out, "mining-plan.json"), "w"), indent=1)
    open(os.path.join(a.out, "survey-report.md"), "w").write(survey_report(S, P))
    db = opendb(a.db); meta_set(db, "survey_done", S["generated"]); meta_set(db, "survey_approved", "")
    print(f"survey written to {a.out}/ (survey-report.md, survey.json, mining-plan.json). Review, then: approve-survey")
def cmd_approve(a):
    db = opendb(a.db)
    if not meta_get(db, "survey_done"): raise SystemExit("run `survey` first")
    meta_set(db, "survey_approved", time.strftime("%Y-%m-%d %H:%M")); print("survey approved; crawl may start")

# ---------------------------------------------------------------- STAGE 1: crawl (headers + structure, all folders, resumable)
def need_survey(db):
    if not meta_get(db, "survey_approved"):
        raise SystemExit("Stage 0 not done: run `survey`, review survey-report.md with the owner, then `approve-survey`. Mining refuses to start before that.")
def load_plan(a):
    p = os.path.join(a.out, "mining-plan.json"); return json.load(open(p)) if os.path.exists(p) else None
def store_message(db, folder, is_sent, uid, x):
    h = parse_headers(x["sections"].get(f"HEADER.FIELDS ({HDR_FIELDS})", b""), x.get("idate"))
    parts, atts = struct_info(x.get("struct"))
    cur = db.execute("insert or replace into messages(folder,uid,msgid,irt,refs,from_addr,from_name,subject,ts,date_bad,size,has_att,att,parts,bulk,hints,is_sent) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (folder, uid, h["msgid"], h["irt"], " ".join(h["refs"]), h["from"][0], h["from"][1], h["subject"], h["ts"], h["date_bad"], int(x.get("size") or 0), 1 if atts else 0,
         json.dumps(atts), json.dumps(parts), h["bulk"], ",".join(h["hints"]), is_sent))
    mid = cur.lastrowid
    db.execute("delete from addrs where mid=?", (mid,))
    rows = [(mid, "from", *h["from"])] + [(mid, "to", *t) for t in h["to"]] + [(mid, "cc", *t) for t in h["cc"]]
    db.executemany("insert into addrs values(?,?,?,?)", [r for r in rows if r[2]])
def crawl_folder(c, db, f, a, p):
    nm = f["name"]; c.select(nm); uv = c.uidvalidity(); sent = 1 if is_sent_folder(f) else 0
    row = db.execute("select * from folders where name=?", (nm,)).fetchone(); last = 0
    if row and row["uidvalidity"] == uv: last = row["last_uid"]
    elif row:
        log(f"{f['display']}: UIDVALIDITY changed, re-crawling folder")
        db.execute("delete from addrs where mid in (select id from messages where folder=?)", (nm,)); db.execute("delete from messages where folder=?", (nm,))
    crit = ["ALL"]
    if p and (p.get("crawl_since") or p.get("crawl_before")):
        pre = "SENT" if p.get("date_basis") == "SENTSINCE" else ""; crit = []
        if p.get("crawl_since"): crit += [pre + "SINCE", dstr(p["crawl_since"])]
        if p.get("crawl_before"): crit += [pre + "BEFORE", dstr(p["crawl_before"])]
    uids = [u for u in c.search(*crit) if u > last]
    db.execute("insert or replace into folders(name,display,flags,uidvalidity,last_uid,total,done,is_sent,updated) values(?,?,?,?,?,?,?,?,?)", (nm, f["display"], f["flags"], uv, last, len(uids) + (row["done"] if row and row["uidvalidity"] == uv else 0), row["done"] if row and row["uidvalidity"] == uv else 0, sent, time.time()))
    db.commit(); log(f"{f['display']}: {len(uids)} new messages to crawl (resume after UID {last})")
    if a.dry_run: return len(uids)
    done = row["done"] if row and row["uidvalidity"] == uv else 0; grand = done + len(uids)
    for i in range(0, len(uids), a.batch):
        b = uids[i:i + a.batch]; r = c.fetch(b, f"(UID RFC822.SIZE INTERNALDATE FLAGS {HDR_ITEMS} BODYSTRUCTURE)")
        for u in b:
            if u in r: store_message(db, nm, sent, u, r[u])
        done += len(b); db.execute("update folders set last_uid=?, done=?, updated=? where name=?", (max(b), done, time.time(), nm)); db.commit()
        if (i // a.batch) % 10 == 0: log(f"  {f['display']}: {done}/{grand}")
        time.sleep(a.delay)
    return len(uids)
def cmd_crawl(a):
    db = opendb(a.db); need_survey(db); c = Conn(a); plan = load_plan(a); pf = {f["name"]: f for f in plan["folders"]} if plan else {}
    fl = [f for f in list_folders(c) if not is_nonmail_folder(f)]
    fl.sort(key=lambda f: (pf.get(f["name"], {}).get("priority", 5 if not is_junk_folder(f) else 9)))
    total = 0
    for f in fl:
        p = pf.get(f["name"])
        if p and p["action"] == "skip" and not a.include_skipped: log(f"skip {f['display']} (plan)"); continue
        if not p and (is_junk_folder(f) or is_drafts_folder(f)) and not a.include_skipped: continue
        meta_set(db, "action:" + f["name"], p["action"] if p else "deep")
        total += crawl_folder(c, db, f, a, p)
    print(f"crawl complete: {db.execute('select count(*) from messages').fetchone()[0]} messages in db ({total} new this run)" + (" [dry-run]" if a.dry_run else ""))

# ---------------------------------------------------------------- STAGE 2: pre-filter + per-sender decision table
def self_set(db, a):
    s = {x.strip().lower() for x in os.environ.get("DCR_SELF", "").split(",") if x.strip()} | ({a.user.lower()} if getattr(a, "user", None) else set())
    db.execute("update messages set is_sent=1 where folder in (select name from folders where is_sent=1)")
    for r in db.execute("select from_addr, count(*) n from messages where is_sent=1 group by from_addr having n>=2"): s.add(r[0])
    db.execute("update messages set is_sent=1 where from_addr in (%s)" % ",".join("?" * len(s)), tuple(s)) if s else None
    return s
def cmd_filter(a):
    db = opendb(a.db); selfs = self_set(db, a); meta_set(db, "self", json.dumps(sorted(selfs)))
    plan = load_plan(a) or {}; pri = set(plan.get("priority_domains", [])); skipd = set(plan.get("skip_domains", []))
    ov = {}
    ovf = os.path.join(a.out, "sender-overrides.csv")
    if os.path.exists(ovf):
        for r in csv.reader(open(ovf)):
            if len(r) >= 2 and r[1] in ("keep", "noise"): ov[r[0].strip().lower()] = r[1]
    # duplicates across folders (same Message-ID)
    db.execute("update messages set decision=null, dreason=null")
    db.execute("update messages set decision='dup', dreason='duplicate message-id' where id in (select m.id from messages m join messages k on k.msgid=m.msgid and k.msgid!='' and k.id<m.id)")
    sent_refs = set(); written = collections.Counter(); sent_dom = collections.Counter()
    for r in db.execute("select irt, refs, id from messages where is_sent=1"):
        if r[0]: sent_refs.add(r[0])
        sent_refs.update((r[1] or "").split())
    for r in db.execute("select a.addr from addrs a join messages m on m.id=a.mid where m.is_sent=1 and a.role in ('to','cc')"):
        written[r[0]] += 1; sent_dom[dom_of(r[0])] += 1
    st = {}
    for r in db.execute("select id,from_addr,from_name,msgid,ts,bulk,subject,has_att,att from messages where is_sent=0 and from_addr!='' and (decision is null)"):
        s = st.setdefault(r["from_addr"], {"name": r["from_name"], "n": 0, "rep": 0, "bulk": 0, "first": r["ts"], "last": r["ts"], "proj": 0})
        s["n"] += 1; s["bulk"] += r["bulk"]; s["first"] = min(s["first"], r["ts"]); s["last"] = max(s["last"], r["ts"]); s["name"] = s["name"] or r["from_name"]
        if r["msgid"] in sent_refs: s["rep"] += 1
        if SUBJ_PROJECT.search(r["subject"] or "") or (r["has_att"] and any(PROJECT_ATT.search(x.get("name") or "") for x in json.loads(r["att"] or "[]"))): s["proj"] += 1
    dec = {}
    for ad, s in st.items():
        d = dom_of(ad); loc = ad.split("@")[0]; wt = written.get(ad, 0)
        if ad in ov or d in ov: k, why = ov.get(ad) or ov.get(d), "owner override"
        elif wt: k, why = "keep", "two-way: David wrote to this address"
        elif d in pri: k, why = "keep", "priority domain from survey"
        elif NOISE_LOCAL.match(loc) and s["bulk"] >= 1: k, why = "noise", "noreply/automated address + bulk headers"
        elif d in ESP or d in skipd and s["rep"] == 0: k, why = "noise", "ESP / survey noise domain"
        elif s["bulk"] / s["n"] >= 0.5: k, why = "noise", "bulk/list headers"
        elif s["n"] >= 6 and s["rep"] == 0 and s["proj"] / s["n"] < 0.2 and d not in FREE: k, why = "noise", "one-way blast (>=6 msgs, never replied, no project signals)"
        elif sent_dom.get(d) and d not in FREE: k, why = "keep", "known organisation domain (David wrote to a colleague)"
        else: k, why = "keep", "unknown human sender, low volume"
        dec[ad] = (k, why)
        db.execute("insert or replace into senders values(?,?,?,?,?,?,?,?,?,?,?)", (ad, s["name"], d, s["n"], s["rep"], s["bulk"], wt, s["first"], s["last"], k, why))
    n_up = 0
    for r in db.execute("select id,from_addr,subject,folder,has_att,att,bulk from messages where is_sent=0 and decision is null").fetchall():
        k, why = dec.get(r["from_addr"], ("keep", "no sender"))
        if re.match(r"(mailer-daemon|postmaster)", r["from_addr"]) and re.search(r"undeliver|delivery status|failure notice|returned mail|delivery failed|undelivered|could not be delivered", r["subject"] or "", re.I):
            db.execute("update messages set decision='bounce', dreason='delivery failure notice (read for dead addresses)' where id=?", (r["id"],)); continue
        junk = re.search(r"(junk|spam|trash|deleted|bulk)", r["folder"], re.I) and not written.get(r["from_addr"])
        if junk: k, why = "noise", "junk/trash folder"
        elif k == "keep" and SUBJ_NOISE.search(r["subject"] or "") and not written.get(r["from_addr"]) and not SUBJ_PROJECT.search(r["subject"] or ""): k, why = "noise", "promotional/receipt subject"
        elif k == "noise" and not r["bulk"] and SUBJ_PROJECT.search(r["subject"] or "") and r["has_att"] and not NOISE_LOCAL.match(r["from_addr"].split("@")[0]) and r["from_addr"] not in ov and dom_of(r["from_addr"]) not in ESP:
            k, why = "keep", "project-like subject + attachment from human (rescued from sender-level noise)"; n_up += 1
        db.execute("update messages set decision=?, dreason=? where id=?", (k, why, r["id"]))
    db.execute("update messages set decision='keep', dreason='sent by owner' where is_sent=1 and decision is null")
    db.commit()
    cnt = db.execute("select decision, count(*) from messages group by decision").fetchall()
    print("filter decisions:", {r[0]: r[1] for r in cnt}, f"| rescued {n_up} project-like messages from noise senders")
    top = db.execute("select addr,n,reason from senders where decision='noise' order by n desc limit 15").fetchall()
    print("top noise senders:", "; ".join(f"{r[0]}({r[1]})" for r in top))

# ---------------------------------------------------------------- STAGE 3: selective body fetch + quote/signature split + signature parsing
class _Strip(HTMLParser):
    def __init__(s): super().__init__(convert_charrefs=True); s.o = []; s.skip = 0
    def handle_starttag(s, t, at):
        if t in ("script", "style", "head"): s.skip += 1
        if t in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "blockquote"): s.o.append("\n")
    def handle_endtag(s, t):
        if t in ("script", "style", "head"): s.skip = max(0, s.skip - 1)
        if t in ("p", "div", "tr", "li", "table"): s.o.append("\n")
    def handle_data(s, d):
        if not s.skip: s.o.append(d)
def html_to_text(h):
    p = _Strip()
    try: p.feed(h)
    except Exception: return re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", re.sub(r"[ \t\xa0]+", " ", "".join(p.o))).strip()
def decode_part(raw, enc, charset, ctype):
    try:
        if enc == "base64": raw = base64.b64decode(raw + b"=" * (-len(raw.strip()) % 4))
        elif enc == "quoted-printable": raw = quopri.decodestring(raw)
    except Exception: pass
    for cs in (charset, "utf-8", "windows-1252"):
        if not cs: continue
        try: t = raw.decode(cs); break
        except Exception: continue
    else: t = raw.decode("latin-1", "replace")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return html_to_text(t) if ctype == "text/html" else t
QSTART = re.compile(r"^(on .{5,250}wrote:|-{2,}\s*(original message|forwarded message)\s*-{2,}|_{8,}|begin forwarded message:?)$", re.I)
def split_quote(t):
    L = t.split("\n")
    for i, l in enumerate(L):
        s = l.strip()
        if QSTART.match(s) or (i + 1 < len(L) and QSTART.match((s + " " + L[i + 1].strip()).strip())) or (s.startswith(">") and i > 0):
            return "\n".join(L[:i]).strip(), "\n".join(L[i:]).strip()
        if re.match(r"^from:\s.+", s, re.I) and any(re.match(r"^(sent|date):", x.strip(), re.I) for x in L[i + 1:i + 4]) and i > 0:
            return "\n".join(L[:i]).strip(), "\n".join(L[i:]).strip()
    return t.strip(), ""
CLOSER = re.compile(r"^(thanks|thank you|many thanks|best|best regards|regards|kind regards|warm regards|warmly|cheers|sincerely|talk soon|all the best|take care|respectfully|-?\s?d(avid)?|dave)[,!.\s]*$", re.I)
PHONE = re.compile(r"(?:\+?1[\s.\-]?)?\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b")
SITE = re.compile(r"((?:https?://)?(?:www\.)[\w.\-/]+|https?://[\w.\-/]+|\b[\w\-]+\.(?:com|net|org|tv|co|io|studio|agency)\b/?)", re.I)
TITLE = re.compile(r"\b(vp|vice president|svp|evp|president|ceo|coo|founder|co-?founder|owner|partner|principal|director|head of [\w ]+|executive producer|producer|line producer|production manager|manager|coordinator|supervisor|art director|creative director|production designer|set designer|set decorator|designer|buyer|art buyer|producer|editor|photographer|stylist|agent|rep|assistant|associate|lead|chief [\w ]+)\b", re.I)
def split_sig(t):
    L = t.rstrip().split("\n")
    for i in range(len(L) - 1, max(-1, len(L) - 16), -1):
        if L[i].strip() in ("--", "-- ") or CLOSER.match(L[i].strip()):
            return "\n".join(L[:i]).strip(), "\n".join(L[i:]).strip()
    paras = re.split(r"\n\s*\n", t.rstrip())
    if len(paras) >= 2 and len(paras[-1].split("\n")) <= 8 and (PHONE.search(paras[-1]) or SITE.search(paras[-1])): return "\n\n".join(paras[:-1]).strip(), paras[-1].strip()
    return t.strip(), ""
def parse_sig(sig, name):
    if not sig: return None
    lines = [l.strip(" \t|·•") for l in sig.split("\n") if l.strip()][:10]; r = {"title": "", "company": "", "phone": "", "site": ""}
    for i, l in enumerate(lines):
        if not r["phone"] and PHONE.search(l): r["phone"] = PHONE.search(l).group()
        if not r["site"] and SITE.search(l) and "@" not in l.replace(SITE.search(l).group(), ""): r["site"] = SITE.search(l).group().rstrip("/")
        if not r["title"] and TITLE.search(l) and len(l) < 90 and not PHONE.search(l) and "@" not in l and not l.lower().startswith(("sent from", "tel", "cell", "office", "phone")):
            m = re.split(r"\s+(?:\||/|@|at|,|-|–|—)\s+", l, maxsplit=1)
            if len(m) == 2 and TITLE.search(m[0]): r["title"], r["company"] = m[0].strip(), m[1].strip()
            else:
                r["title"] = l
                for nx in lines[i + 1:i + 3]:
                    if not PHONE.search(nx) and "@" not in nx and not SITE.search(nx) and len(nx) < 60 and not re.match(r"^\d", nx) and not nx.lower().startswith(("sent from", "tel", "cell", "office", "phone")): r["company"] = nx; break
    r["company"] = re.sub(r"\s+", " ", r["company"]).strip(" ,.")
    return r if any(r.values()) else None
def fetch_bodies(db, c, rows, cap=60000, batch=50):
    by = collections.defaultdict(list)
    for r in rows: by[r["folder"]].append(r)
    for folder, lst in by.items():
        c.select(folder)
        for i in range(0, len(lst), batch):
            chunk = lst[i:i + batch]; groups = collections.defaultdict(list); meta = {}
            for r in chunk:
                parts = json.loads(r["parts"] or "[]"); pick_ = next((p for p in parts if p["type"] == "text/plain"), None) or next((p for p in parts if p["type"] == "text/html"), None)
                if not pick_: db.execute("insert or replace into bodies values(?,?,?,?,?)", (r["id"], "", "", "", 0)); continue
                groups[pick_["path"]].append(r["uid"]); meta[r["uid"]] = (r, pick_)
            for path, uids in groups.items():
                got = c.fetch(uids, f"(UID BODY.PEEK[{path}]<0.{cap}>)")
                for u in uids:
                    r, p_ = meta[u]; raw = got.get(u, {}).get("sections", {}).get(path.upper(), b"")
                    t = decode_part(raw, p_["enc"], p_["charset"], p_["type"]); new, quoted = split_quote(t); body, sig = split_sig(new)
                    db.execute("insert or replace into bodies values(?,?,?,?,?)", (r["id"], body[:20000], quoted[:3000], sig[:1500], 1 if len(raw) >= cap else 0))
                    ps = parse_sig(sig, "")
                    if ps and r["from_addr"] and r["decision"] != "bounce": db.execute("insert into sigs values(?,?,?,?,?,?,?,?)", (r["id"], r["from_addr"], "", ps["title"], ps["company"], ps["phone"], ps["site"], r["ts"]))
            db.commit()
        log(f"fetch: {folder} done")
def cmd_fetch(a):
    db = opendb(a.db); need_survey(db); c = Conn(a)
    rows = db.execute("select id,folder,uid,parts,from_addr,ts,decision from messages where decision in ('keep','bounce','seed') and id not in (select mid from bodies) order by folder, uid").fetchall()
    rows = [r for r in rows if r["decision"] == "seed" or meta_get(db, "action:" + r["folder"], "deep") == "deep" or r["decision"] == "bounce"]
    log(f"fetch: {len(rows)} messages need bodies")
    if a.dry_run: print(f"[dry-run] would fetch {len(rows)} bodies (text parts only, capped at {a.max_body} bytes each)"); return
    fetch_bodies(db, c, rows, a.max_body, a.body_batch)
    print("fetch complete:", db.execute("select count(*) from bodies").fetchone()[0], "bodies stored")

# ---------------------------------------------------------------- STAGE 4: thread reconstruction
GENERIC_SUBJ = {"", "hi", "hello", "hey", "(no subject)", "no subject", "fwd", "photos", "pics", "question", "update", "thanks", "thank you", "info", "following up", "quick question"}
def cmd_thread(a):
    db = opendb(a.db); selfs = set(json.loads(meta_get(db, "self", "[]"))); par = {}
    def find(x):
        while par.setdefault(x, x) != x: par[x] = par[par[x]]; x = par[x]
        return x
    def union(x, y): par[find(x)] = find(y)
    rows = db.execute("select id,msgid,irt,refs,subject,ts,from_addr from messages where decision in ('keep','seed')").fetchall()
    byid = {}
    for r in rows:
        find(r["id"])
        if r["msgid"]: byid.setdefault(r["msgid"], r["id"])
    for r in rows:
        for ref in ([r["irt"]] if r["irt"] else []) + (r["refs"] or "").split():
            if ref in byid: union(r["id"], byid[ref])
    parts = collections.defaultdict(set)
    for r in db.execute("select mid, addr from addrs where mid in (select id from messages where decision in ('keep','seed'))"):
        if r["addr"] not in selfs: parts[r["mid"]].add(r["addr"])
    groups = collections.defaultdict(list)
    for r in rows:
        k = norm_subject(r["subject"])
        if len(k) >= 8 and k not in GENERIC_SUBJ: groups[k].append(r)
    for k, g in groups.items():
        g.sort(key=lambda r: r["ts"])
        for p, q in zip(g, g[1:]):
            if q["ts"] - p["ts"] <= 180 * 86400 and (parts[p["id"]] & parts[q["id"]]): union(p["id"], q["id"])
    comp = collections.defaultdict(list)
    for r in rows: comp[find(r["id"])].append(r)
    db.execute("delete from threads")
    for root, g in comp.items():
        tid = min(r["id"] for r in g); g.sort(key=lambda r: r["ts"]); ppl = sorted(set().union(*[parts[r["id"]] for r in g]))
        subj = re.sub(r"^\s*((re|fw|fwd|aw)\s*:\s*)+", "", g[0]["subject"] or "", flags=re.I).strip() or norm_subject(g[0]["subject"])
        db.execute("insert into threads values(?,?,?,?,?,?)", (tid, subj, g[0]["ts"], g[-1]["ts"], len(g), json.dumps(ppl)))
        db.executemany("update messages set thread=? where id=?", [(tid, r["id"]) for r in g])
    db.commit(); n = db.execute("select count(*), max(n) from threads").fetchone()
    print(f"threads: {n[0]} (largest has {n[1]} messages) from {len(rows)} kept messages")

# ---------------------------------------------------------------- STAGE 5: entity linking (people <-> orgs, aliases, job changes over time)
SEGMENTS = [  # (segment, name/domain regex, job-title regex)
    ("Network/streamer publicity", r"paramount|netflix|hbo|\bnbc|\babc\b|cbs|\bfox\b|warner (bros|media)|disney|hulu|peacock|universal (pictures|studios)|sony pictures|lionsgate|mgm|network|streaming", r"publicity|key.?art|creative advertising|photo (manager|publicity)"),
    ("Ad agency", r"agency|advertising|worldwide|bbdo|ogilvy|wieden|saatchi|mccann|droga|leo burnett|y&r|tbwa", r"art producer|agency producer|art buyer"),
    ("Record label/management", r"records|music|\blabel\b|umg|interscope|republic|capitol|atlantic", r"a&r|artist manager|video commissioner|label"),
    ("Photographer/rep", r"photograph|represent|\breps?\b", r"photographer|agent"),
    ("Magazine", r"magazine|vogue|esquire|instyle|vanity fair|\belle\b|conde nast|time inc", r"photo (editor|director)|visuals director|picture editor"),
    ("Production company", r"productions?\b|pictures\b|films?\b|entertainment|fieldhouse", r"executive producer|line producer|production manager|head of production"),
    ("Partner/subcontractor", r"fabricat|scenic|scenery|metalworks|millwork|woodworks|construction|exhibit|set ?shop|prop (house|closet|shop)|propcloset|paint|lumber|printing|sign shop", r"prop (house|master)|fabricator|shop (owner|manager)"),
    ("DTC brand", r"\bgear\b|travel|luggage|apparel|beauty|cosmetics|footwear|\bshop(ify)?\b", r"e-?commerce|growth marketing|brand (director|manager)"),
    ("Brand in-house studio", r"pepsi|nike|apple|target|samsung|coca|beverages|foods|\bbrands?\b", r"creative production|studio producer|brand experiences|marketing (director|manager)|brand creative|content studio")]
AREA = {"LA": ("310", "323", "818", "213", "424", "747", "626", "661", "805"), "NY": ("212", "646", "917", "718", "347", "929"), "Nashville": ("615", "629")}
def segment_of(name, domain, titles):
    best, bs = "", 0
    for seg, nrx, trx in SEGMENTS:
        sc = len(re.findall(nrx, f"{name} {domain}".lower())) * 2 + len(re.findall(trx, " ".join(titles).lower()))
        if sc > bs: best, bs = seg, sc
    return best
def market_of(phones):
    c = collections.Counter()
    for p in phones:
        d = re.sub(r"\D", "", p); d = d[1:] if len(d) == 11 and d[0] == "1" else d
        for k, v in AREA.items():
            if d[:3] in v: c[k] += 1
    return c.most_common(1)[0][0] if c else ""
def norm_co(s): return re.sub(r"\b(inc|llc|ltd|corp|co|the|company|incorporated)\b|[^a-z0-9]", "", s.lower())
def cmd_link(a):
    db = opendb(a.db); selfs = set(json.loads(meta_get(db, "self", "[]"))); cut = NOW - 730 * 86400
    A = collections.defaultdict(lambda: {"names": collections.Counter(), "in": 0, "out": 0, "first": 1 << 60, "last": 0, "lin": 0, "lout": 0, "sender_ts": []})
    for r in db.execute("select a.addr,a.name,a.role,m.ts,m.is_sent from addrs a join messages m on m.id=a.mid where m.decision in ('keep','seed')"):
        ad = r["addr"]
        if ad in selfs or NOISE_LOCAL.match(ad.split("@")[0]) or dom_of(ad) in ESP: continue
        x = A[ad]
        if r["name"]: x["names"][r["name"]] += 1
        x["first"] = min(x["first"], r["ts"]); x["last"] = max(x["last"], r["ts"])
        if r["role"] == "from" and not r["is_sent"]: x["in"] += 1; x["lin"] = max(x["lin"], r["ts"]); x["sender_ts"].append(r["ts"])
        elif r["role"] in ("to", "cc") and r["is_sent"]: x["out"] += 1; x["lout"] = max(x["lout"], r["ts"])
    sigs = collections.defaultdict(list)
    for r in db.execute("select s.addr,s.title,s.company,s.phone,s.site,s.ts from sigs s order by s.ts"): sigs[r["addr"]].append(r)
    par = {}
    def find(x):
        while par.setdefault(x, x) != x: par[x] = par[par[x]]; x = par[x]
        return x
    nk = collections.defaultdict(list)
    for ad, x in A.items():
        find(ad); best = x["names"].most_common(1)[0][0] if x["names"] else ""
        if name_key(best): nk[name_key(best)].append(ad)
    merged_by = {}
    for k, ads in nk.items():
        for p in ads[1:]:
            q = ads[0]; xp, xq = A[p], A[q]
            overlap = min(xp["last"], xq["last"]) - max(xp["first"], xq["first"])
            if dom_of(p) == dom_of(q) or dom_of(p) in FREE or dom_of(q) in FREE or overlap < 180 * 86400:
                par[find(p)] = find(q); merged_by[p] = merged_by[q] = "name"
    groups = collections.defaultdict(list)
    for ad in A: groups[find(ad)].append(ad)
    # org per domain (company votes from signatures)
    dom_co = collections.defaultdict(collections.Counter)
    for ad, rs in sigs.items():
        for s in rs:
            if s["company"] and 3 <= len(s["company"]) <= 50 and not re.search(r",\s*[A-Z]{2}\b|^\d|@", s["company"]): dom_co[dom_of(ad)][s["company"]] += 1
    def org_of(ad, ts=None):
        d = dom_of(ad)
        if d in FREE:
            co = collections.Counter(s["company"] for s in sigs.get(ad, []) if s["company"]).most_common(1)
            return ("co:" + norm_co(co[0][0]), co[0][0], d) if co and norm_co(co[0][0]) else (None, None, d)
        co = dom_co[d].most_common(1)
        if co and co[0][1] >= 2 and norm_co(co[0][0]): return ("co:" + norm_co(co[0][0]), co[0][0], d)
        return ("d:" + d, d.split(".")[0].replace("-", " ").title(), d)
    db.execute("delete from people"); db.execute("delete from email_person"); db.execute("delete from orgs"); db.execute("delete from person_org")
    orgs = {}; org_n = collections.Counter(); org_first = {}; org_last = {}; org_doms = collections.defaultdict(set); org_titles = collections.defaultdict(list); org_phones = collections.defaultdict(list)
    people = []
    for root, ads in groups.items():
        names = collections.Counter(); [names.update(A[x]["names"]) for x in ads]
        name = max(names, key=lambda n: (n.count(" ") > 0, names[n])) if names else ads[0].split("@")[0].replace(".", " ").title()
        po = {}; 
        for ad in ads:
            for tsv in A[ad]["sender_ts"] or [A[ad]["last"]]:
                k, nm, d = org_of(ad)
                if not k: continue
                e = po.setdefault(k, {"name": nm, "first": tsv, "last": tsv, "n": 0, "title": "", "tts": 0})
                e["first"] = min(e["first"], tsv); e["last"] = max(e["last"], tsv); e["n"] += 1; org_doms[k].add(d)
            for s in sigs.get(ad, []):
                k, nm, d = org_of(ad)
                if k in po and s["title"] and s["ts"] >= po[k]["tts"]: po[k]["title"] = s["title"]; po[k]["tts"] = s["ts"]
                if k: org_titles[k].append(s["title"]); 
                if k and s["phone"]: org_phones[k].append(s["phone"])
        order = sorted(po.items(), key=lambda kv: kv[1]["last"])
        cur = order[-1] if order else None; prev = [(k, v) for k, v in order[:-1]]
        moved = int(bool(cur and prev and any(cur[1]["first"] > v["last"] - 90 * 86400 for _, v in prev) and sum(v["n"] for _, v in prev) >= 1 and cur[1]["n"] >= 1 and len(po) >= 2))
        x_in = sum(A[x]["in"] for x in ads); x_out = sum(A[x]["out"] for x in ads); lin = max(A[x]["lin"] for x in ads); lout = max(A[x]["lout"] for x in ads)
        phones = [s["phone"] for ad in ads for s in sigs.get(ad, []) if s["phone"]]
        people.append({"name": name, "ads": sorted(ads, key=lambda x: -A[x]["last"]), "cur": cur, "prev": prev, "moved": moved, "in": x_in, "out": x_out, "first": min(A[x]["first"] for x in ads), "last": max(A[x]["last"] for x in ads),
                       "two_way": x_in > 0 and x_out > 0, "lt": max(lin, lout) if x_in and x_out else 0, "phone": phones[-1] if phones else "", "merged": "name" if any(x in merged_by for x in ads) and len(ads) > 1 else ""})
        for k, v in po.items():
            org_n[k] += v["n"]; org_first[k] = min(org_first.get(k, 1 << 60), v["first"]); org_last[k] = max(org_last.get(k, 0), v["last"]); orgs.setdefault(k, v["name"])
    oid = {}
    for k, nm in orgs.items():
        titles = [t for t in org_titles[k] if t]; doms = sorted(org_doms[k]); seg = segment_of(nm, " ".join(doms), titles)
        sig = ""
        if re.search(r"creative advertising|advertising|marketing|publicity|key art", " ".join(titles).lower()) and seg == "Network/streamer publicity": sig = "Studio creative-advertising team (Paramount pattern): commissions pitch-meeting environments"
        cur = db.execute("insert into orgs(key,name,domains,site,segment,market,first_ts,last_ts,n,icp_signal) values(?,?,?,?,?,?,?,?,?,?)", (k, nm, ",".join(doms), doms[0] if doms and doms[0] not in FREE else "", seg, market_of(org_phones[k]), org_first[k], org_last[k], org_n[k], sig)); oid[k] = cur.lastrowid
    for p in people:
        cur = p["cur"]; prev = "; ".join(f"{v['name']} ({year_of(v['first'])}-{year_of(v['last'])})" for k, v in p["prev"])
        pid = db.execute("insert into people(name,emails,org_id,title,phone,first_ts,last_ts,n_in,n_out,last_two_way,prev_orgs,moved,merged_by) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (p["name"], ";".join(p["ads"]), oid.get(cur[0]) if cur else None, cur[1]["title"] if cur else "", p["phone"], p["first"], p["last"], p["in"], p["out"], p["lt"], prev, p["moved"], p["merged"])).lastrowid
        db.executemany("insert into email_person values(?,?)", [(x, pid) for x in p["ads"]])
        for k, v in ([cur] if cur else []) + p["prev"]: db.execute("insert or ignore into person_org values(?,?,?,?,?,?)", (pid, oid[k], v["title"], v["first"], v["last"], v["n"]))
    db.commit(); print(f"link: {len(people)} people (from {len(A)} addresses), {len(orgs)} organisations, {sum(p['moved'] for p in people)} possible job changes")

# ---------------------------------------------------------------- STAGE 6: per-thread classification (heuristic | scaledown | openai-compatible), cached
PV = "v1"
STAGES = ["inquiry", "bid", "booked", "in-progress", "delivered", "invoiced", "paid"]
STAGE_RX = [("paid", r"payment (has been )?(received|sent)|paid in full|wire (sent|went out)|check (is )?(in the mail|mailed|cut)|thank you for (the|your) payment|received your payment|payment confirmed"),
            ("invoiced", r"invoice (attached|#|no\.?|number)|attached (is )?(the |my |our )?invoice|please find (the |my )?invoice|net ?(30|15|45)|balance due|remit"),
            ("delivered", r"\bdelivered\b|\binstalled\b|\bwrapped\b|strike (complete|done)|arrived safely|it arrived|picked up|shipped (out|today)|tracking number"),
            ("in-progress", r"\bbuilding\b|fabricat|load[- ]?in|in progress|shoot day|call sheet|\bpaint(ing|ed)\b|\bshop (is|has)|build (schedule|update)|work in progress|progress photos"),
            ("booked", r"you got the job|we('re| are) (good|moving forward|confirmed)|confirmed|booked|awarded|green ?light|approved|purchase order|\bpo\b ?(#|number|attached)|signed (the )?(deal memo|agreement|contract)|deal memo|go ahead"),
            ("bid", r"\bquote\b|estimate|\bbid\b|proposal|pricing|budget (for|of|is)|ballpark|rough number|cost (to|for)"),
            ("inquiry", r"are you available|availability|hold (the )?dates?|interested in|looking for a (set|production)|reaching out|can you (design|build)|would you be able")]
KIND_RX = [("trophy", "Event/activation", r"trophy|award|activation|event\b"), ("conference room", "Broadcast/studio", r"conference room|boardroom|pitch meeting|marketing meeting"), ("key art", "Publicity/key art", r"key.?art|publicity|premiere|poster shoot"),
           ("commercial", "Commercial", r"commercial|\bspot\b|tv spot|airport|product shoot"), ("campaign", "Ad campaign", r"campaign|lookbook"), ("editorial", "Editorial", r"editorial|cover shoot|magazine"),
           ("retail", "Retail/trade show", r"trade ?show|retail|window display|booth"), ("music", "Music", r"album|music video|tour")]
DELIV = [("trophy", r"trophy"), ("conference room set/environment", r"conference room|boardroom"), ("backdrop", r"backdrop"), ("set build", r"set build|build the set|set design|\bset\b"), ("props", r"\bprops?\b"), ("renderings", r"render(ing)?s?"), ("drawings/floor plan", r"drawings?|floor ?plan|elevations?"),
         ("mood board / references", r"mood ?board|reference images?"), ("fabrication", r"fabricat"), ("install / load-in", r"install|load[- ]?in"), ("delivery/shipping", r"deliver(y|ed)|shipp(ed|ing)|crate"), ("strike", r"\bstrike\b"), ("3D model / SketchUp", r"sketch ?up|3d model|\.skp"), ("signage/graphics", r"signage|graphics|vinyl|logo")]
ROLE_RX = re.compile(r"(production designer|set designer|art director|set decorator|prop master|lead designer|design (and|&) build|designer and fabricator|creative director)", re.I)
MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?\s?([kK])?\b")
def money_items(text, date=""):
    out = []
    for m in MONEY.finditer(text):
        v = float(m.group(1).replace(",", "")) * (1000 if m.group(3) else 1) + (int(m.group(2)) / 100 if m.group(2) else 0)
        ctx = text[max(0, m.start() - 70): m.end() + 50].replace("\n", " ").strip(); lc = ctx.lower()
        near = {}; a0, a1 = m.start() - max(0, m.start() - 70), m.end() - max(0, m.start() - 70)
        for kind, rx in (("payment", r"paid|payment|wire|check|received"), ("deposit", r"deposit"), ("invoice", r"invoice|inv\b|balance due|net ?30"), ("quote", r"quote|estimate|bid|proposal|price|pricing|cost"), ("budget", r"budget")):
            for mm in re.finditer(rx, lc):
                d = a0 - mm.end() if mm.end() <= a0 else mm.start() - a1 + 25
                near[kind] = min(near.get(kind, 999), max(d, 0))
        k = min(near, key=near.get) if near else "mention"
        out.append({"kind": k, "amount": v, "date": date, "evidence": ctx[:140]})
    return out
def build_digest(db, tid, selfs, per=1500, cap=12000):
    L = []; ms = db.execute("select m.id,m.ts,m.from_addr,m.from_name,m.subject,m.att,m.is_sent,b.text from messages m left join bodies b on b.mid=m.id where m.thread=? order by m.ts", (tid,)).fetchall()
    for m in ms:
        who = "DAVID" if m["is_sent"] else (m["from_name"] or m["from_addr"]); atts = [x["name"] for x in json.loads(m["att"] or "[]") if x.get("name")]
        L.append(f"[{ymd(m['ts'])}] {who} <{m['from_addr']}>: {m['subject']}" + (f" (attachments: {', '.join(atts[:6])})" if atts else "") + "\n" + (m["text"] or "")[:per])
    return "\n\n".join(L)[:cap], ms
def org_by_addr(db, addr):
    d = dom_of(addr)
    if d and d not in FREE:
        r = db.execute("select name from orgs where ','||domains||',' like ?", ("%," + d + ",%",)).fetchone()
        if r: return r[0]
    r = db.execute("select o.name from email_person e join people p on p.id=e.pid join orgs o on o.id=p.org_id where e.addr=?", (addr,)).fetchone()
    return r[0] if r else None
def heuristic(db, tid, digest, ms, selfs):
    low = digest.lower(); subj = db.execute("select subj from threads where id=?", (tid,)).fetchone()[0]
    sc = sum(min(len(re.findall(p, low)), 3) * w for p, w in [(r"quote|estimate|\bbid\b|proposal", 2), (r"invoice|deposit|purchase order|\bpo\b", 2), (r"call sheet|load[- ]?in|strike|fabricat|install", 2), (r"set design|set build|\bset\b|props?|backdrop|trophy|conference room|render", 1), (r"shoot|commercial|campaign|key.?art|deliver", 1), (r"\$\s?\d", 1)])
    sent = sum(1 for m in ms if m["is_sent"]); atts = sum(1 for m in ms if m["att"] and m["att"] != "[]")
    is_proj = sc >= 4 and (sent >= 1 or atts >= 1)
    status = ""; 
    for st, rx in STAGE_RX:
        if re.search(rx, low): 
            if not status or STAGES.index(st) > STAGES.index(status): status = st
    if re.search(r"went with (someone|another)|not moving forward|passed on|no longer need|cancel(l)?ed|budget fell through", low) and status in ("", "inquiry", "bid"): status = "lost"
    money = []
    for m in ms: money += money_items((m["text"] or ""), ymd(m["ts"]))
    ptype = next((t for kw, t, rx in KIND_RX if re.search(rx, low)), "")
    deliv = [lab for lab, rx in DELIV if re.search(rx, low)]
    role = ROLE_RX.search(" ".join((m["text"] or "") for m in ms if m["is_sent"])) or ROLE_RX.search(digest)
    ppl = []; seen = set()
    for m in ms:
        ad = m["from_addr"]
        if ad in seen or ad in selfs: continue
        seen.add(ad); s = db.execute("select title, company from sigs where addr=? and title!='' order by ts desc limit 1", (ad,)).fetchone()
        ppl.append({"name": m["from_name"], "email": ad, "role": s["title"] if s else ""})
    orgc = collections.Counter()
    for p in ppl:
        r = org_by_addr(db, p["email"])
        if r: orgc[r] += 1
    first = ms[0]; ex = " ".join(re.split(r"(?<=[.!?])\s+", (first["text"] or "").strip())[:2])[:300]
    return {"is_project": bool(is_proj), "kind": "project_work" if is_proj else "other", "client": orgc.most_common(1)[0][0] if orgc and is_proj else "", "brand": "", "project_name": (subj or "").strip().title()[:90], "year": year_of(first["ts"]),
            "role": role.group(1).lower() if role else "", "deliverables": deliv, "money": money[:25], "status": status or "unknown", "type": ptype, "people": ppl[:12], "summary": f"{len(ms)} message(s) {ymd(ms[0]['ts'])} to {ymd(ms[-1]['ts'])}. Opening: {ex}", "confidence": round(min(1.0, sc / 10), 2), "source": "heuristic", "score": sc}

def http_json(url, body, headers, timeout=120, retries=5):
    data = json.dumps(body).encode(); last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, {"Content-Type": "application/json", **headers}, method="POST"), timeout=timeout) as r: return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read()[:200]!r}"
            if e.code in (429, 500, 502, 503, 504): time.sleep(2 ** i); continue
            raise RuntimeError(last)
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e: last = str(e); time.sleep(2 ** i)
    raise RuntimeError("LLM API failed: " + str(last))
def grounded(s, text): return bool(s) and re.sub(r"\W+", " ", str(s).lower()).strip() in re.sub(r"\W+", " ", text.lower())
KIND_LABELS = [("project_work", "Does this email thread discuss a paid creative production job, such as designing, building, fabricating, delivering or installing a set, props, trophy or display, including quotes, schedules, invoices or payments for it?"),
               ("project_inquiry", "Is this email thread an inquiry, request for availability, bid or proposal for a possible set design or production job?"),
               ("payment_admin", "Is this email thread mainly about an invoice, payment, tax form or other bookkeeping?"),
               ("solicitation", "Is this email thread an unsolicited sales pitch, promotion or mass announcement?"),
               ("personal", "Is this email thread a personal or family conversation unrelated to work?"),
               ("other_business", "Is this email thread professional correspondence that is not about a specific job, such as networking or general updates?")]
STATUS_LABELS = [("inquiry", "Is the work only being asked about, with no price or commitment yet?"), ("bid", "Has a quote, estimate or proposal been given but not yet accepted?"), ("booked", "Has the client confirmed or approved the job, or issued a purchase order?"),
                 ("in_progress", "Is the set or object currently being designed, built or prepared?"), ("delivered", "Has the work been delivered, installed or shot and wrapped?"), ("invoiced", "Has an invoice been sent and is payment pending?"), ("paid", "Has payment been received and the job closed?"), ("lost", "Did the client decline, cancel or choose someone else?")]
class SD:
    name = "scaledown"
    def __init__(s): s.key = os.environ["SCALEDOWN_API_KEY"]; s.base = os.environ.get("SCALEDOWN_BASE", "https://api.scaledown.xyz").rstrip("/"); s.h = {"x-api-key": s.key}
    def classify(s, items, labels):
        out = {}
        for i in range(0, len(items), 50):
            r = http_json(s.base + "/classify", {"chunks": [{"id": str(k), "text": t} for k, t in items[i:i + 50]], "labels": [{"name": n, "rubric": q} for n, q in labels]}, s.h)
            for x in r.get("results", []): out[x["id"]] = (x.get("top_label"), (x.get("scores") or {}).get(x.get("top_label"), 0))
        return out
    def extract(s, text):
        sch = {"type": "object", "properties": {"client_company": {"type": "string", "description": "Name of the company or brand that hired the designer or is the customer for the job"}, "project_name": {"type": "string", "description": "Title of the project, job, film, commercial or campaign"},
               "person": {"type": "string", "description": "Full name of a person"}, "money_amount": {"type": "string", "description": "Dollar amount"}, "deliverable": {"type": "string", "description": "Physical item, set or object the designer must design, build or deliver"}, "designer_role": {"type": "string", "description": "Job title or role the designer was hired for"}}}
        r = http_json(s.base + "/v1/chat/completions", {"model": "extract", "messages": [{"role": "user", "content": text}], "response_format": {"type": "json_schema", "json_schema": {"name": "project", "schema": sch}}}, s.h)
        ents = json.loads(r["choices"][0]["message"]["content"]).get("entities", []); g = collections.defaultdict(list)
        for e in ents: g[e["type"]].append((e.get("confidence", 0), e["text"]))
        return {k: [t for _, t in sorted(v, reverse=True)] for k, v in g.items()}
    def summarize(s, text):
        r = http_json(s.base + "/v1/chat/completions", {"model": "summarize", "max_tokens": 300, "messages": [{"role": "system", "content": "Summarize this email thread about a set design or production job in 3 sentences: client, what was designed or built, dates, money and status. Use only facts stated in the thread."}, {"role": "user", "content": text}]}, s.h)
        return json.loads(r["choices"][0]["message"]["content"]).get("summary", "")
    def run(s, batch):  # batch: [(tid, digest, heur)]
        kinds = s.classify([(t, d) for t, d, h in batch], KIND_LABELS); res = {}
        proj = [(t, d) for t, d, h in batch if kinds.get(str(t), ("",))[0] in ("project_work", "project_inquiry", "payment_admin")]
        stat = s.classify(proj, STATUS_LABELS) if proj else {}
        for t, d, h in batch:
            k = kinds.get(str(t), (None, 0)); o = {"kind": k[0], "confidence": round(float(k[1] or 0), 2), "is_project": k[0] in ("project_work", "project_inquiry")}
            if str(t) in stat: o["status"] = stat[str(t)][0].replace("_", "-")
            if k[0] in ("project_work", "project_inquiry", "payment_admin"):
                e = s.extract(d); o["client"] = (e.get("client_company") or [""])[0]; o["project_name"] = (e.get("project_name") or [""])[0]; o["role"] = (e.get("designer_role") or [""])[0]
                o["deliverables"] = e.get("deliverable", [])[:8]; o["people_names"] = e.get("person", [])[:12]; o["amounts"] = e.get("money_amount", [])[:15]; o["summary"] = s.summarize(d)
            res[t] = o
        return res
class OA:
    name = "openai"
    def __init__(s):
        s.base = os.environ["LLM_BASE_URL"].rstrip("/"); s.model = os.environ.get("LLM_MODEL", "llama3.1:8b"); s.h = {"Authorization": "Bearer " + os.environ.get("LLM_API_KEY", "none")}
    def run(s, batch):
        res = {}; sysm = ("You extract facts from one email thread for a set designer's CRM. Reply with ONE JSON object only: {\"kind\": one of project_work|project_inquiry|payment_admin|solicitation|personal|other_business, \"is_project\": bool, \"client\": str, \"brand\": str, \"project_name\": str, \"year\": int, \"role\": str, \"deliverables\": [str], "
               "\"money\": [{\"kind\": quote|invoice|payment|deposit|budget, \"amount\": number, \"date\": \"YYYY-MM-DD\", \"evidence\": exact quote from thread}], \"status\": inquiry|bid|booked|in-progress|delivered|invoiced|paid|lost|unknown, \"people\": [{\"name\": str, \"role\": str}], \"summary\": 3 sentences}. Use ONLY facts in the thread; use \"\" or [] when absent. Never guess amounts.")
        for t, d, h in batch:
            d2 = compress(d, "Extract project facts") if len(d) > 8000 else d
            body = {"model": s.model, "temperature": 0, "messages": [{"role": "system", "content": sysm}, {"role": "user", "content": d2}], "response_format": {"type": "json_object"}}
            try: r = http_json(s.base + "/chat/completions", body, s.h)
            except RuntimeError: body.pop("response_format"); r = http_json(s.base + "/chat/completions", body, s.h)
            txt = r["choices"][0]["message"]["content"]; m = re.search(r"\{.*\}", txt, re.S)
            try: res[t] = json.loads(m.group(0)) if m else {}
            except Exception: res[t] = {}
        return res
def compress(text, prompt):
    k = os.environ.get("SCALEDOWN_API_KEY")
    if not k: return text
    try:
        r = http_json(os.environ.get("SCALEDOWN_BASE", "https://api.scaledown.xyz").rstrip("/") + "/compress/raw/", {"context": text, "prompt": prompt, "scaledown": {"rate": "auto"}}, {"x-api-key": k})
        return (r.get("results") or r).get("compressed_prompt") or text
    except Exception: return text
def pick_backend(a):
    b = a.backend
    if b == "auto": b = "scaledown" if os.environ.get("SCALEDOWN_API_KEY") else "openai" if os.environ.get("LLM_BASE_URL") else "heuristic"
    return None if b == "heuristic" else SD() if b == "scaledown" else OA()
def merge_llm(h, o, digest):
    r = dict(h); r["source"] = h["source"]
    if not o: return r
    r["source"] = "+".join([h["source"], o.get("_n", "llm")]) if "_n" in o else h["source"] + "+llm"
    if o.get("kind"): r["kind"] = o["kind"]; r["is_project"] = bool(o.get("is_project", o["kind"] in ("project_work", "project_inquiry")))
    for k in ("client", "brand", "project_name", "role"):
        if o.get(k) and grounded(o[k], digest): r[k] = o[k]
    if o.get("status") and o["status"] in STAGES + ["lost", "unknown"]: r["status"] = o["status"]
    if o.get("deliverables"): r["deliverables"] = sorted(set(r["deliverables"]) | {d for d in o["deliverables"] if grounded(d, digest)})
    if o.get("money"):
        ok = [m for m in o["money"] if isinstance(m, dict) and m.get("amount") and re.search(r"\b" + re.escape(f"{int(float(m['amount'])):,}") + r"\b|\b" + str(int(float(m["amount"]))) + r"\b", digest)]
        if ok: r["money"] = [dict(m, evidence=str(m.get("evidence", ""))[:140]) for m in ok]
    if o.get("amounts") and not o.get("money"):
        keep = [m for m in r["money"] if any(re.sub(r"\D", "", str(x)) == str(int(m["amount"])) for x in o["amounts"])]
        r["money"] = keep or r["money"]
    if o.get("people"): r["people"] = [dict(p, email=p.get("email", "")) for p in o["people"] if grounded(p.get("name"), digest)] or r["people"]
    if o.get("summary") and len(o["summary"]) > 20: r["summary"] = o["summary"].strip()
    if o.get("confidence"): r["confidence"] = max(r["confidence"], o["confidence"])
    if o.get("year"): r["year"] = r["year"] or o["year"]
    return r
def classify_threads(db, a, tids=None, quiet=False):
    selfs = set(json.loads(meta_get(db, "self", "[]"))); be = pick_backend(a); bname = be.name if be else "heuristic"
    q = "select id from threads" if tids is None else "select id from threads where id in (%s)" % ",".join(map(str, tids or [0]))
    todo = []; chars = 0
    for (tid,) in db.execute(q).fetchall():
        digest, ms = build_digest(db, tid, selfs)
        if not any(m["text"] for m in ms) and not any(m["att"] not in (None, "[]") for m in ms): continue
        h = heuristic(db, tid, digest, ms, selfs); hh = hashlib.sha1((digest + PV).encode()).hexdigest()
        c0 = db.execute("select hash from tclass where tid=? and backend=?", (tid, bname)).fetchone()
        if c0 and c0[0] == hh and not a.force: continue
        db.execute("insert or replace into tclass values(?,?,?,?,?,?)", (tid, "heuristic", hh, PV, json.dumps(h), time.time()))
        if be and (h["score"] >= a.min_score or a.all): todo.append((tid, digest, h, hh)); chars += len(digest)
        elif be: pass
    db.commit(); n_h = db.execute("select count(*) from tclass where backend='heuristic'").fetchone()[0]
    if not quiet: print(f"classify: heuristic done for {n_h} threads; backend={bname}; {len(todo)} threads queued for the model ({chars:,} digest chars)")
    if a.dry_run or not be: return
    todo = todo[:a.max_threads]
    for i in range(0, len(todo), a.llm_batch):
        b = todo[i:i + a.llm_batch]; res = be.run([(t, d, h) for t, d, h, _ in b])
        for t, d, h, hh in b:
            o = res.get(t) or {}; o["_n"] = bname; db.execute("insert or replace into tclass values(?,?,?,?,?,?)", (t, bname, hh, PV, json.dumps(merge_llm(h, o, d)), time.time()))
        db.commit(); 
        if not quiet: log(f"classify: {min(i + a.llm_batch, len(todo))}/{len(todo)} threads via {bname}")
def get_class(db, tid):
    r = db.execute("select js from tclass where tid=? order by case backend when 'heuristic' then 1 else 0 end limit 1", (tid,)).fetchone()
    return json.loads(r[0]) if r else None
def cmd_classify(a): classify_threads(db := opendb(a.db), a)

# ---------------------------------------------------------------- STAGE 7: projects + exports (Notion-ready CSVs)
STOP = set("re fwd fw update updated final revised revision new the a an and of for to with from on in at v1 v2 v3 quote estimate invoice schedule attached info thanks follow up question request".split())
def ptoks(name): return {w for w in re.findall(r"[a-z]{3,}", (name or "").lower()) if w not in STOP}
NSTATUS = {"inquiry": "Bidding", "bid": "Bidding", "booked": "Booked", "in-progress": "Build", "delivered": "Wrap", "invoiced": "Invoiced", "paid": "Paid", "lost": "Bidding", "unknown": ""}
def build_projects(db):
    items = []
    for r in db.execute("select t.id,t.subj,t.first_ts,t.last_ts,t.n from threads t"):
        c = get_class(db, r["id"])
        if c and c["is_project"] and c["kind"] in ("project_work", "project_inquiry", "other"): items.append((r, c))
    by_client = collections.defaultdict(list)
    for r, c in items: by_client[c["client"] or "(client not found in email)"].append((r, c))
    projects = []
    for client, lst in by_client.items():
        clusters = []
        for r, c in sorted(lst, key=lambda x: x[0]["first_ts"]):
            tk = ptoks(c["project_name"]) or ptoks(" ".join(c["deliverables"]))
            for cl in clusters:
                if tk and cl["tk"] and (len(tk & cl["tk"]) / len(tk | cl["tk"]) >= 0.5 or len(tk & cl["tk"]) >= 3): cl["items"].append((r, c)); cl["tk"] |= tk; break
            else: clusters.append({"tk": tk, "items": [(r, c)]})
        for cl in clusters:
            its = cl["items"]; names = collections.Counter(re.sub(r"\b(19|20)\d\d\b", "", c["project_name"]).strip(" -:") for _, c in its)
            yrs = sorted({year_of(r["first_ts"]) for r, _ in its}); best = max((c["status"] for _, c in its if c["status"] in STAGES + ["lost"]), key=lambda s: STAGES.index(s) if s in STAGES else -1, default="unknown")
            pname = names.most_common(1)[0][0] or its[0][0]["subj"]
            if len(yrs) >= 2 or len({c["project_name"] for _, c in its}) >= 3:
                nm_l = [re.sub(r"\b(19|20)\d\d\b", "", c["project_name"]).split() for _, c in its]; common = [w for w in nm_l[0] if all(w.lower() in [x.lower() for x in n] for n in nm_l[1:])]
                if len(common) >= 2: pname = " ".join(common).strip(" -:")
            occ = []
            for y in yrs:
                sub = [(r, c) for r, c in its if year_of(r["first_ts"]) == y]
                mx = lambda kind: sum(max([m["amount"] for m in c["money"] if m["kind"] == kind] or [0]) for _, c in sub)
                st = max((c["status"] for _, c in sub if c["status"] in STAGES), key=STAGES.index, default="unknown")
                dn = " ".join(w for w in re.sub(r"\b(19|20)\d\d\b", "", sub[0][1]["project_name"]).split() if w.lower() not in pname.lower().split()).strip(" -:") if len(sub) == 1 else ""
                occ.append({"label": (f"{pname} - {dn} {y}" if dn else f"{pname} {y}"), "year": y, "threads": [r["id"] for r, _ in sub], "status": st, "quoted": mx("quote"), "invoiced": mx("invoice"), "paid": mx("payment") + mx("deposit"), "summary": " ".join(c["summary"] for _, c in sub[:2])[:600]})
            ty = collections.Counter(c["type"] for _, c in its if c["type"]).most_common(1)
            projects.append({"name": pname, "client": client, "type": ty[0][0] if ty else "", "status": best, "years": yrs, "recurring": len(yrs) >= 2, "occurrences": occ,
                             "quoted": sum(o["quoted"] for o in occ), "invoiced": sum(o["invoiced"] for o in occ), "paid": sum(o["paid"] for o in occ), "deliverables": sorted({d for _, c in its for d in c["deliverables"]}),
                             "people": sorted({p.get("name") or p.get("email", "") for _, c in its for p in c["people"] if p.get("name") or p.get("email")}), "threads": [(r["id"], r["subj"], r["first_ts"], r["last_ts"], r["n"], c["status"], c["summary"]) for r, c in its]})
    return projects
def cmd_export(a):
    db = opendb(a.db); os.makedirs(a.out, exist_ok=True); selfs = set(json.loads(meta_get(db, "self", "[]"))); projects = build_projects(db)
    known = [x.strip().lower() for x in open(a.clients).read().splitlines() if x.strip()] if a.clients and os.path.exists(a.clients) else []
    W = lambda name, hdr, rows: csv.writer(open(os.path.join(a.out, name), "w", newline="", encoding="utf-8")).writerows([hdr] + rows)
    pc_org = collections.Counter(); proj_by_org = collections.defaultdict(list); pc_person = set()
    for p in projects:
        if p["status"] in ("booked", "in-progress", "delivered", "invoiced", "paid") or p["paid"] or p["invoiced"]: pc_org[p["client"]] += 1
        proj_by_org[p["client"]].append(p)
        for t in p["threads"]:
            for r in db.execute("select e.pid from addrs a join email_person e on e.addr=a.addr where a.mid in (select id from messages where thread=?)", (t[0],)): pc_person.add(r[0])
    orgs = {r["id"]: r for r in db.execute("select * from orgs")}
    ppl = db.execute("select * from people order by last_ts desc").fetchall()
    rel = lambda p: ("Warm" if p["last_two_way"] >= NOW - 730 * 86400 else "Dormant") if p["n_in"] and p["n_out"] else "Cold"
    crows, hrows, orow = [], [], []
    keep = [p for p in ppl if (p["n_in"] + p["n_out"] >= a.min_msgs or p["id"] in pc_person) and not (p["emails"] and all(e in selfs for e in p["emails"].split(";")))]
    org_has = collections.defaultdict(list)
    for p in keep:
        o = orgs.get(p["org_id"]); org_has[o["id"] if o else 0].append(p)
        notes = []; 
        if p["prev_orgs"]: notes.append("Previously: " + p["prev_orgs"])
        if p["moved"]: notes.append("JOB CHANGE detected (re-engagement)")
        if len(p["emails"].split(";")) > 1: notes.append("Other emails: " + ", ".join(p["emails"].split(";")[1:]))
        pj = [pr["name"] for pr in projects if p["name"] in pr["people"]]
        if pj: notes.append("Projects: " + "; ".join(pj[:5]))
        crows.append([p["name"], o["name"] if o else "", p["title"], p["emails"].split(";")[0], p["phone"], "", rel(p), ymd(p["last_ts"]), "Email backfill", " | ".join(notes)])
        hrows.append([p["id"], p["name"], p["emails"], o["name"] if o else "", p["title"], p["phone"], rel(p), p["n_in"], p["n_out"], ymd(p["first_ts"]), ymd(p["last_ts"]), p["prev_orgs"], p["moved"], p["merged_by"]])
    W("contacts.csv", ["Name", "Company", "Role", "Email", "Phone", "LinkedIn", "Relationship", "Last contact", "Source", "Notes"], crows)
    W("contacts_full.csv", ["id", "name", "emails", "company", "title", "phone", "relationship", "msgs_in", "msgs_out", "first", "last", "previous_orgs", "job_change", "merged_by"], hrows)
    comp = []
    for o in orgs.values():
        if o["id"] not in org_has: continue
        past = bool(pc_org.get(o["name"])) or any(k in (o["name"] + " " + (o["domains"] or "")).lower() for k in known)
        n = [f"{o['n']} msgs {year_of(o['first_ts'])}-{year_of(o['last_ts'])}"] + ([o["icp_signal"]] if o["icp_signal"] else []) + ([f"Projects: {', '.join(p['name'] for p in proj_by_org.get(o['name'], [])[:5])}"] if proj_by_org.get(o["name"]) else [])
        comp.append([o["name"], o["segment"], o["market"], "Yes" if past else "No", ("https://" + o["site"]) if o["site"] else "", " | ".join(n)])
    W("companies.csv", ["Name", "Segment", "Market", "Past client", "Website", "Notes"], comp)
    prow, tr = [], []
    for p in projects:
        st = NSTATUS.get(p["status"], ""); par = p["name"]
        base = [p["client"], p["type"], st]
        if p["recurring"]:
            prow.append([par, *base, "", p["quoted"] or "", p["invoiced"] or "", "Yes", "", "", len(p["threads"]), "No", ", ".join(p["deliverables"]), "", "Parent template page (recurring: " + ", ".join(map(str, p["years"])) + ")"])
        for o in p["occurrences"]:
            nm = o["label"] if p["recurring"] else par
            prow.append([nm, *base[:2], NSTATUS.get(o["status"], ""), "", o["quoted"] or "", o["invoiced"] or "", "Yes" if p["recurring"] else "No", o["year"], par if p["recurring"] else "", len(o["threads"]), "No", ", ".join(p["deliverables"]), "", o["summary"]])
        for t in p["threads"]: tr.append([par, year_of(t[2]), t[0], ymd(t[2]), ymd(t[3]), t[4], t[1], t[5], t[6][:300]])
    W("projects.csv", ["Project", "Client", "Type", "Status", "Budget", "Fee", "Total billed", "Recurring", "Occurrence year", "Parent project", "Source threads", "Example template", "Deliverables checklist", "Crew/vendors", "Notes"], prow)
    W("project_threads.csv", ["Project", "Occurrence year", "Thread ID", "First date", "Last date", "Messages", "Subject", "Status", "Summary"], tr)
    # re-engagement queue: who hired/worked with David, scored; neutral context notes, job change, dead address (-> lookup list), do-not-contact (-> excluded)
    SIG = [("payment/invoice discussion", r"overdue|past due|late payment|payment reminder|still (waiting|haven.t received)|not (been )?paid|unpaid|second notice|balance due"), ("wording that may signal a disagreement (David to judge)", r"disput|unhappy|disappointed|unacceptable|not satisfied|refus|won'?t pay|attorney|lawyer|collections|chargeback|stop payment"), ("cancellation mentioned", r"cancel(l)?ed|cancellation")]
    neg = collections.defaultdict(list); dead_ts = {}; email_pid = {r[0]: r[1] for r in db.execute("select addr,pid from email_person")}
    for r in db.execute("select m.subject,m.ts,b.text,a.addr from messages m join bodies b on b.mid=m.id join addrs a on a.mid=m.id where m.decision='keep'"):
        pid = email_pid.get(r["addr"])
        if not pid or len(neg[pid]) >= 3: continue
        for lab, rx in SIG:
            if re.search(rx, (r["text"] or "") + " " + (r["subject"] or ""), re.I):
                note = f"{lab} in {year_of(r['ts'])} thread '{re.sub(r'^\s*((re|fw|fwd)\s*:\s*)+', '', r['subject'] or '', flags=re.I)[:60]}'"
                if not any(n.startswith(lab) and f"{year_of(r['ts'])} thread" in n for n in neg[pid]): neg[pid].append(note)
    for r in db.execute("select m.ts,b.text from messages m join bodies b on b.mid=m.id where m.decision='bounce'"):
        t = r["text"] or ""; ad = re.findall(r"final-recipient:\s*rfc822;\s*([^\s]+)", t, re.I) or re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", t)
        for x in ad:
            x = x.lower().strip("<>.,;")
            if x not in selfs and not re.match(r"(mailer-daemon|postmaster)", x): dead_ts[x] = max(dead_ts.get(x, 0), r["ts"])
    lastin = {r[0]: r[1] for r in db.execute("select from_addr, max(ts) from messages where is_sent=0 and decision='keep' group by from_addr")}
    dnc = set(x.strip().lower() for x in open(os.path.join(a.out, "do-not-contact.csv")).read().replace(",", "\n").splitlines() if x.strip()) if os.path.exists(os.path.join(a.out, "do-not-contact.csv")) else set()
    pp = collections.defaultdict(list)
    for p in projects:
        for oc in p["occurrences"]:
            for tid in oc["threads"]:
                for r in db.execute("select distinct e.pid from addrs a join email_person e on e.addr=a.addr where a.mid in (select id from messages where thread=?)", (tid,)): pp[r[0]].append((p, oc))
    def last_words(pid, tid):
        em = [e for e, q in email_pid.items() if q == pid]
        r = db.execute("select b.text from messages m join bodies b on b.mid=m.id where m.thread=? and m.from_addr in (%s) order by m.ts desc limit 1" % ",".join("?" * len(em)), (tid, *em)).fetchone() if em else None
        return re.sub(r"\s+", " ", (r[0] if r else "") or "")[:150]
    cycle = {}
    for p in projects:
        if p["recurring"]:
            mo = collections.Counter(time.strftime("%B", time.gmtime(t[2])) for t in p["threads"]).most_common(1)[0][0]; cycle[p["name"]] = f"annual project: kicks off ~{mo} ({p['years'][0]}-{p['years'][-1]})"
    re_rows, ex_rows, lookup_rows, orow = [], [], [], []; seen_org = set()
    scored = []
    for p in keep:
        o = orgs.get(p["org_id"]); mine = pp.get(p["id"], []); msgs = p["n_in"] + p["n_out"]; two = p["n_in"] and p["n_out"]
        hired = any(pr["status"] in ("booked", "in-progress", "delivered", "invoiced", "paid") or pr["paid"] or pr["invoiced"] for pr, _ in mine)
        vendor = bool(o and o["segment"] == "Partner/subcontractor"); age = (NOW - p["last_ts"]) / 86400 / 365; span = (p["last_ts"] - p["first_ts"]) / 86400 / 365
        if not (hired or mine or (two and msgs >= 6)): continue
        billed = sum(max(oc["invoiced"], oc["quoted"]) for pr, oc in {(id(pr), id(oc)): (pr, oc) for pr, oc in mine}.values())
        score = min(msgs, 30) + (15 if two else 0) + (20 if age <= 2 else 14 if age <= 5 else 8 if age <= 10 else 3) + min(len({id(pr) for pr, _ in mine}), 5) * 4 + min(billed / 5000, 10) * 2 \
                + (15 if o and o["segment"] and not vendor else 0) + (10 if o and o["icp_signal"] else 0) + (15 if p["moved"] else 0)
        tier = "close" if two and (msgs >= 15 or (span >= 3 and msgs >= 8)) else "past_client" if hired else "lapsed"
        flags = []
        if p["moved"]: flags.append("JOB CHANGE: " + p["prev_orgs"] + " -> " + (o["name"] if o else "?"))
        ems = p["emails"].split(";")
        if ems and all(e in dead_ts and lastin.get(e, 0) <= dead_ts[e] for e in ems): flags.append("DEAD ADDRESS (bounced): find current address")
        if neg.get(p["id"]): flags.append("CONTEXT (neutral, David decides): " + "; ".join(neg[p["id"]]))
        if any(e in dnc or dom_of(e) in dnc for e in ems) or p["name"].lower() in dnc: flags.append("DO NOT CONTACT (owner list)")
        best = max(mine, key=lambda x: (x[1]["year"], max(x[1]["invoiced"], x[1]["quoted"])), default=None)
        mem = ""
        if best:
            pr, oc = best; mem = f"{oc['label']} ({oc['year']})" + (f", ~${max(oc['invoiced'], oc['quoted']):,.0f}" if max(oc["invoiced"], oc["quoted"]) else "") + (f"; their last words in that thread: \"{last_words(p['id'], oc['threads'][0])}\"" if oc["threads"] else "") + (f"; {cycle[pr['name']]}" if pr["name"] in cycle else "")
        row = {"p": p, "o": o, "score": round(score), "tier": tier, "kind": "vendor" if vendor else "client", "pr": "A" if score >= 60 else "B" if score >= 35 else "C", "flags": flags, "mem": mem, "hired": hired, "projects": "; ".join(sorted({f"{oc['label']}" for _, oc in mine}))[:300], "span": span, "cycle": [cycle[pr["name"]] for pr, _ in mine if pr["name"] in cycle][:1]}
        scored.append(row)
    scored.sort(key=lambda r: -r["score"])
    for r in scored:
        p, o = r["p"], r["o"]; bad = [f for f in r["flags"] if f.startswith("DO NOT")]; dead = any(f.startswith("DEAD") for f in r["flags"])
        out = [p["name"], o["name"] if o else "", p["emails"].split(";")[0], r["tier"], r["kind"], r["pr"], r["score"], rel(p), ymd(p["last_ts"]), p["n_in"] + p["n_out"], f"{r['span']:.1f}", "yes" if r["hired"] else "no", r["projects"], r["mem"], " | ".join(r["flags"]), p["emails"].replace(";", " ")]
        if bad: ex_rows.append(out); continue
        if dead: lookup_rows.append(out); continue
        re_rows.append(out); key = o["id"] if o else p["id"]
        if key in seen_org and not p["moved"] and r["tier"] != "close": continue
        seen_org.add(key)
        trig = ("; ".join(r["flags"]) or ("active/recent relationship" if (NOW - p["last_ts"]) < 730 * 86400 else f"quiet since {year_of(p['last_ts'])}")) + (f"; {r['cycle'][0]}" if r["cycle"] else "") + (f"; ICP: {o['icp_signal']}" if o and o["icp_signal"] else "")
        nxt = {"close": "Draft a personal note for David to edit (DRAFT ONLY)", "past_client": "Draft a warm check-in (DRAFT ONLY)", "lapsed": "Draft a light touch (DRAFT ONLY)"}[r["tier"]]
        val = max([max(oc["invoiced"], oc["quoted"]) for pr, oc in pp.get(p["id"], [])] or [0]) or ""
        orow.append([f"Reconnect: {o['name'] if o else p['name']}", o["name"] if o else "", p["name"], "Identified", r["pr"], trig[:240], "", nxt, "", val, ymd(p["last_ts"]), 0])
    hdr = ["Contact", "Company", "Email", "Tier", "Kind", "Priority", "Score", "Relationship", "Last contact", "Msgs", "Years active", "Hired/paid", "Projects", "Memory hint (from email)", "Flags", "All emails"]
    W("reengagement.csv", hdr, re_rows); W("reengagement-excluded-dnc.csv", hdr, ex_rows); W("address-lookup.csv", hdr, lookup_rows)
    W("pipeline.csv", ["Opportunity", "Company", "Contact", "Stage", "Priority", "Trigger", "Source URL", "Next action", "Follow-up date", "Est. value", "Last touch", "Touch count"], orow[:a.max_pipeline])
    segs = collections.Counter(o["segment"] or "(unclassified)" for o in orgs.values() if o["id"] in org_has); rc = collections.Counter(rel(p) for p in keep)
    md = [f"# Mining summary ({time.strftime('%Y-%m-%d')})", "", f"- Messages in db: {db.execute('select count(*) from messages').fetchone()[0]:,}; kept after noise filter: {db.execute("select count(*) from messages where decision in ('keep','seed')").fetchone()[0]:,}; threads: {db.execute('select count(*) from threads').fetchone()[0]:,}",
          f"- Contacts exported: {len(crows)} ({dict(rc)}); companies: {len(comp)}; projects: {len(projects)} ({sum(1 for p in projects if p['recurring'])} recurring); reconnect queue: {len(re_rows)} (do-not-contact excluded: {len(ex_rows)}; dead addresses sent to current-address lookup: {len(lookup_rows)})", "- Companies by segment: " + ", ".join(f"{k} {v}" for k, v in segs.most_common()), "",
          "## Top warm contacts"] + [f"- {r[0]} ({r[1]}) last {r[7]}" for r in crows if r[6] == "Warm"][:25] + ["", "## Top dormant contacts"] + [f"- {r[0]} ({r[1]}) last {r[7]}" for r in crows if r[6] == "Dormant"][:25] + ["", "## Re-engagement (first 25)"] + [f"- [{r[5]}/{r[3]}] {r[0]} @ {r[1]}: {r[13][:110] or r[14]}" for r in re_rows[:25]] + ["", "## Projects found"] + [f"- {p['name']} - {p['client']} - years {p['years']} - {p['status']}" for p in projects[:40]]
    open(os.path.join(a.out, "summary.md"), "w").write("\n".join(md) + "\n")
    # sqlite exports of projects for dossier/other tools
    db.execute("create table if not exists projects(name text, client text, js text)"); db.execute("delete from projects")
    db.executemany("insert into projects values(?,?,?)", [(p["name"], p["client"], json.dumps(p)) for p in projects]); db.commit()
    print(f"export: contacts {len(crows)}, companies {len(comp)}, projects {len(projects)}, pipeline {min(len(orow), a.max_pipeline)} -> {a.out}/ (summary.md first)")

def cmd_voice(a):
    """Learn David's voice from his own sent mail (bodies already fetched). Writes voice-notes.md"""
    db = opendb(a.db); os.makedirs(a.out, exist_ok=True)
    rows = db.execute("select m.subject,m.ts,b.text,b.sig from messages m join bodies b on b.mid=m.id where m.is_sent=1 and m.decision='keep' and length(b.text)>0 order by m.ts").fetchall()
    seen, notes = set(), []
    for r in rows:
        t = r["text"].strip(); w = len(t.split()); k = re.sub(r"\W+", " ", t.lower())[:80]
        if 12 <= w <= 140 and k not in seen and "$" not in t and not re.search(r"invoice|quote|estimate|attached|tracking|crate|schedule|dimension|pickup|\\d+x\\d+", t, re.I): seen.add(k); notes.append((r, t, w))
    if not notes: print("voice: no short personal sent notes found yet"); return
    greet, close, tri = collections.Counter(), collections.Counter(), collections.Counter(); exc = contr = 0; words = []
    for r, t, w in notes:
        words.append(w); fl = t.split("\n")[0].strip()
        if len(fl.split()) <= 6: greet[re.sub(r"\b[A-Z][a-z]+\b(?=[,!.-]*$)", "<Name>", fl)] += 1
        sg = (r["sig"] or "").split("\n")[0].strip()
        if sg and len(sg) < 30: close[sg] += 1
        exc += t.count("!"); contr += len(re.findall(r"\w+'(s|re|ll|ve|d|t|m)\b", t))
        ws = re.findall(r"[a-z']+", t.lower()); tri.update(" ".join(ws[i:i + 3]) for i in range(len(ws) - 2))
    n = len(notes); pick = sorted(notes, key=lambda x: (-(x[1].count("!") + 0), x[2]))[:0]
    sample = [x for x in notes if 15 <= x[2] <= 90][-25:]
    md = ["# David's voice notes (from his sent mail)", "", f"Based on {n} short non-transactional notes of {len(rows)} sent messages with bodies.", "",
          f"- Typical length: median {sorted(words)[n // 2]} words (range {min(words)}-{max(words)}). Keep reconnect notes at or under that.", f"- Exclamation marks per note: {exc / n:.2f}; contractions per note: {contr / n:.2f}",
          "- Common openings: " + (", ".join(f"`{g}` ×{c}" for g, c in greet.most_common(6)) or "n/a"), "- Common sign-offs: " + (", ".join(f"`{g}` ×{c}" for g, c in close.most_common(6)) or "n/a"),
          "- Recurring phrases: " + ", ".join(f"\"{g}\"" for g, c in tri.most_common(40) if c >= 3 and not re.match(r"(the|and|to|of|in|a|i) ", g))[:400], "", "## Samples (real notes; mimic rhythm and warmth, never copy)", ""]
    for r, t, w in sample: md.append(f"**{ymd(r['ts'])} - {r['subject'][:60]}**\n> " + t.replace("\n", "\n> ") + "\n")
    open(os.path.join(a.out, "voice-notes.md"), "w").write("\n".join(md)); print(f"voice: {n} notes analysed -> {a.out}/voice-notes.md")

def cmd_status(a):
    db = opendb(a.db)
    print("survey:", meta_get(db, "survey_done"), "approved:", meta_get(db, "survey_approved") or "NO")
    for r in db.execute("select display,total,done,last_uid,is_sent from folders order by display"): print(f"  {r[0]:35} {r[2]}/{r[1]} last_uid={r[3]}{' [sent]' if r[4] else ''}")
    for t in ("messages", "bodies", "threads", "people", "orgs", "tclass"): print(f"  {t}: {db.execute(f'select count(*) from {t}').fetchone()[0]}")
def cmd_run(a):
    for fn in (cmd_crawl, cmd_filter, cmd_fetch, cmd_thread, cmd_link, cmd_classify, cmd_export, cmd_voice): fn(a)

def build_parser():
    p = argparse.ArgumentParser(prog="dcrmine", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["survey", "approve-survey", "crawl", "filter", "fetch", "thread", "link", "classify", "export", "voice", "run", "status"])
    p.add_argument("--db", default="dcr_mail.sqlite"); p.add_argument("--out", default="mining-out", help="work dir: survey + exports")
    p.add_argument("--host", default="imap.mail.me.com"); p.add_argument("--port", type=int, default=993); p.add_argument("--plain", action="store_true", help="no TLS (test fixture only)")
    p.add_argument("--user", default=os.environ.get("DCR_IMAP_USER")); p.add_argument("--pass-env", default="ICLOUD_APP_PASSWORD"); p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--delay", type=float, default=0.2); p.add_argument("--batch", type=int, default=200)
    p.add_argument("--sample", type=int, default=25, help="survey: headers per folder-year"); p.add_argument("--sent-sample", type=int, default=400, help="survey: headers per Sent folder-year")
    p.add_argument("--seeds"); p.add_argument("--keywords"); p.add_argument("--no-text-search", action="store_true")
    p.add_argument("--include-skipped", action="store_true"); p.add_argument("--body-batch", type=int, default=50); p.add_argument("--max-body", type=int, default=60000)
    p.add_argument("--backend", default="auto", choices=["auto", "heuristic", "scaledown", "openai"]); p.add_argument("--min-score", type=int, default=3); p.add_argument("--all", action="store_true")
    p.add_argument("--max-threads", type=int, default=10 ** 9); p.add_argument("--llm-batch", type=int, default=20); p.add_argument("--force", action="store_true")
    p.add_argument("--clients"); p.add_argument("--min-msgs", type=int, default=2); p.add_argument("--max-pipeline", type=int, default=300)
    return p
def main(argv=None):
    a = build_parser().parse_args(argv)
    return {"survey": cmd_survey, "approve-survey": cmd_approve, "crawl": cmd_crawl, "filter": cmd_filter, "fetch": cmd_fetch, "thread": cmd_thread, "link": cmd_link, "classify": cmd_classify, "export": cmd_export, "voice": cmd_voice, "run": cmd_run, "status": cmd_status}[a.cmd](a)
if __name__ == "__main__": main()
