#!/usr/bin/env python3
"""dcrmine - READ-ONLY deep mailbox miner (stdlib only, Python 3.9+).  Stages:
 survey -> approve-survey -> crawl -> filter -> fetch -> thread -> link (contacts) -> projects -> export   (or: run)
Extraction only: unique contacts, the threads they are in, and the projects those threads form. No scoring or labels.
Mail safety: every IMAP command passes a wire-level allow-list (CAPABILITY LOGIN LIST STATUS EXAMINE UID SEARCH/FETCH NOOP LOGOUT);
folders are opened with EXAMINE only; FETCH may only use BODY.PEEK / BODYSTRUCTURE / headers / sizes / dates / flags.
Secrets: password only from env (default ICLOUD_APP_PASSWORD); never printed or stored.  All dates are written in UTC."""
import argparse, base64, codecs, collections, csv, datetime as dt, email, email.utils, hashlib, imaplib, json, os, quopri
import re, socket, sqlite3, ssl, sys, time, unicodedata
from email.header import decode_header, make_header
from html.parser import HTMLParser
try: import fcntl
except ImportError: fcntl = None   # non-POSIX: run lock disabled

VERSION = "2.0"
NOW = time.time(); THIS_YEAR = dt.datetime.now(dt.timezone.utc).year
imaplib._MAXLINE = 64 * 1024 * 1024   # stdlib default (1 MB) breaks UID SEARCH on folders with >~140k messages
def utcnow(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def log(*a): print(time.strftime("%H:%M:%SZ", time.gmtime()), *a, file=sys.stderr, flush=True)

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
CREATE TABLE IF NOT EXISTS contacts(addr TEXT PRIMARY KEY, name TEXT, name_src TEXT, company TEXT, title TEXT, phone TEXT, domain TEXT, first_ts INTEGER, last_ts INTEGER, n_from INTEGER, n_to INTEGER, n_threads INTEGER);
CREATE TABLE IF NOT EXISTS thread_contacts(tid INTEGER, addr TEXT, PRIMARY KEY(tid, addr));
CREATE INDEX IF NOT EXISTS ix_tc_addr ON thread_contacts(addr);
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, name TEXT, first_ts INTEGER, last_ts INTEGER, n_threads INTEGER, n_msgs INTEGER, contacts TEXT, why TEXT);
CREATE TABLE IF NOT EXISTS project_threads(pid INTEGER, tid INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS survey_cache(k TEXT PRIMARY KEY, v TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS crawl_skips(folder TEXT, uid INTEGER, uidvalidity INTEGER, tries INTEGER, why TEXT, ts REAL, PRIMARY KEY(folder,uid));
CREATE INDEX IF NOT EXISTS ix_msg_folder ON messages(folder, uid); CREATE INDEX IF NOT EXISTS ix_msg_decision ON messages(decision);
CREATE INDEX IF NOT EXISTS ix_sigs_addr ON sigs(addr);
"""
MIGRATIONS = [("folders", "is_junk", "INTEGER DEFAULT 0"), ("folders", "crit", "TEXT"), ("contacts", "kind", "TEXT DEFAULT 'person'")]
def opendb(path):
    d = os.path.dirname(os.path.abspath(path)); os.makedirs(d, exist_ok=True)
    db = sqlite3.connect(path, timeout=60); db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    try: db.execute("PRAGMA journal_mode=WAL")   # lets `status` read while a long stage writes
    except sqlite3.DatabaseError: pass
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    for t, col, decl in MIGRATIONS:
        if col not in {r[1] for r in db.execute(f"pragma table_info({t})")}: db.execute(f"alter table {t} add column {col} {decl}")
    db.commit(); return db
class RunLock:
    """One writer at a time per database: re-running a stage while an old nohup copy is still alive must not corrupt progress."""
    def __init__(s, dbpath): s.path = os.path.abspath(dbpath) + ".lock"; s.fh = None
    def __enter__(s):
        if fcntl is None: return s
        s.fh = open(s.path, "a+")
        try: fcntl.flock(s.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            s.fh.seek(0); who = s.fh.read().strip()
            raise SystemExit(f"another dcrmine process is already working on this database ({who or 'pid unknown'}); "
                             "wait for it to finish (check `status`) or kill it, then re-run this command - it resumes where it stopped.")
        s.fh.seek(0); s.fh.truncate(); s.fh.write(f"pid {os.getpid()} since {utcnow()}"); s.fh.flush(); return s
    def __exit__(s, *e):
        if s.fh:
            try: s.fh.seek(0); s.fh.truncate(); fcntl.flock(s.fh, fcntl.LOCK_UN); s.fh.close()
            except Exception: pass
def lock_holder(dbpath):
    p = os.path.abspath(dbpath) + ".lock"
    if fcntl is None or not os.path.exists(p): return ""
    with open(p, "a+") as fh:
        try: fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(fh, fcntl.LOCK_UN); return ""
        except OSError: fh.seek(0); return fh.read().strip() or "running"
def meta_get(db, k, d=None):
    r = db.execute("select v from meta where k=?", (k,)).fetchone(); return r[0] if r else d
def meta_set(db, k, v):
    db.execute("insert or replace into meta values(?,?)", (k, v)); db.commit()
def stage(db, name, state, note=""):
    """progress breadcrumbs for `status`: stage:<name> = '<state> <utc time> <note>'"""
    meta_set(db, "stage:" + name, f"{state} {utcnow()} {note}".strip()); meta_set(db, "stage:current", f"{name} {state} {utcnow()}")

# ---------------------------------------------------------------- text helpers
CHARSET_ALIASES = {"unknown-8bit": "windows-1252", "x-unknown": "windows-1252", "unknown": "windows-1252", "iso-8859-8-i": "iso-8859-8", "x-user-defined": "windows-1252",
                   "ks_c_5601-1987": "cp949", "x-mac-roman": "mac-roman", "macintosh": "mac-roman", "us-ascii": "ascii", "ansi_x3.4-1968": "ascii", "utf8": "utf-8", "x-sjis": "shift_jis", "gb2312": "gbk"}
def norm_charset(cs):
    cs = (cs or "").strip().strip('"\'').lower()
    if not cs: return ""
    cs = CHARSET_ALIASES.get(cs, cs)
    try: return codecs.lookup(cs).name
    except LookupError: return ""
def decode_bytes(raw, *charsets, truncated=False):
    """Best-effort bytes -> str for old mail: declared charset, then utf-8, then windows-1252.
    truncated=True (a capped partial fetch) tolerates a multibyte character cut off at the very end."""
    if isinstance(raw, str): return raw
    raw = raw or b""
    for cs in [norm_charset(c) for c in charsets if c] + ["utf-8", "windows-1252"]:
        if not cs: continue
        try: return raw.decode(cs)
        except UnicodeDecodeError as e:
            if truncated and cs == "utf-8" and e.start >= len(raw) - 3 and e.reason == "unexpected end of data":
                try: return raw[:e.start].decode(cs)
                except UnicodeDecodeError: pass
        except LookupError: continue
    return raw.decode("latin-1", "replace")
def dh(v):
    """RFC 2047 header -> clean one-line str. Unknown/broken charsets fall back per encoded word instead of leaving =?...?= garbage."""
    if v is None: return ""
    v = str(v)
    if any("\udc80" <= ch <= "\udcff" for ch in v):    # surrogateescaped raw 8-bit from the email package
        try: v = decode_bytes(v.encode("utf-8", "surrogateescape"))
        except UnicodeError: v = v.encode("utf-8", "replace").decode("utf-8")
    try: out = str(make_header(decode_header(v)))
    except Exception:
        try: out = "".join(decode_bytes(b, c) if isinstance(b, bytes) else b for b, c in decode_header(v))
        except Exception: out = v
    return re.sub(r"\s+", " ", out.replace("\r", "").replace("\n", " ")).strip()
def header_text(hb):
    """raw header bytes -> str. 8-bit headers (common before ~2005) are decoded as utf-8, else windows-1252, never with U+FFFD."""
    return decode_bytes(hb or b"")
def to_ascii(t):
    """IMAP SEARCH args go out as ASCII; transliterate accents (Café -> Cafe) rather than crash."""
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
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
HME_RELAY = re.compile(r"^(.+?)_at_([a-z0-9][a-z0-9_\-]*?)_[a-z0-9]{8,}_[a-z0-9]{6,}@(?:icloud\.com|privaterelay\.appleid\.com)$")
def real_addr(ad):
    """iCloud Hide-My-Email (and Sign in with Apple) delivers mail from 'jo.smith@acme.com' as 'jo.smith_at_acme_com_<id>_<id>@icloud.com'; undo that"""
    m = HME_RELAY.match(ad or "")
    return f"{m.group(1)}@{m.group(2).replace('_', '.')}" if m else ad
def parse_addrs(v):
    out = []
    for nm, ad in email.utils.getaddresses([dh(v)] if v else []):
        ad = real_addr((ad or "").strip().lower())
        if "@" in ad and " " not in ad: out.append((ad, norm_name(nm)))
    return out
FREE = set("gmail.com googlemail.com yahoo.com ymail.com aol.com hotmail.com outlook.com live.com msn.com icloud.com me.com mac.com comcast.net att.net sbcglobal.net earthlink.net verizon.net bellsouth.net cox.net charter.net protonmail.com proton.me pm.me fastmail.com fastmail.fm hey.com zoho.com tutanota.com yandex.com rr.com gmx.com mail.com juno.com netscape.net roadrunner.com rocketmail.com".split())
FREE_STEM = set("gmail googlemail hotmail ymail rocketmail protonmail fastmail zohomail gmx yahoo mail email aol outlook live msn".split())   # hotmail.co.uk etc.
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
ESP = set("""mailchimp.com mcsv.net mcdlv.net rsgsv.net sendgrid.net sendgrid.com constantcontact.com ccsend.com rs6.net mailgun.org mailgun.com amazonses.com sparkpostmail.com sparkpost.com hubspot.com hubspotemail.net hs-send.com klaviyo.com klaviyomail.com exacttarget.com et.com salesforce.com marketo.com mktomail.com campaign-archive.com createsend.com cmail19.com cmail20.com campaignmonitor.com mandrillapp.com mailjet.com sendinblue.com brevo.com convertkit.com substack.com beehiiv.com eventbrite.com e.eventbrite.com linkedin.com facebookmail.com twitter.com x.com pinterest.com instagram.com quora.com meetup.com nextdoor.com youtube.com paypal.com ebay.com etsy.com shopify.com squarespace.com wix.com godaddy.com groupon.com zoom.us docusign.net myworkday.com intercom-mail.com intercom.io zendesk.com freshdesk.com helpdesk.com customeremailnotifications.com""".split())
NOISE_LOCAL = re.compile(r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|noreply|notifications?|notify|alerts?|newsletter|news|mailer[-_.]?daemon|postmaster|bounce[sd]?|bounces?|updates?|marketing|promo(tions?)?|offers?|deals?|info@|billing|receipts?|orders?|support|customerservice|automated|system|auto[-_.]?reply)\b", re.I)
NOREPLY_ANY = re.compile(r"no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer[-_.]?daemon|^robot\b|^bounce", re.I)
LISTCMD_LOCAL = re.compile(r"(^|[-_.+])(unsubscribe|unsub|listunsubscribe|leave|optout|opt-out|remove)([-_.+]|$)|-request$", re.I)
LISTCMD_SUBJ = re.compile(r"^\s*(unsubscribe|unsub|remove me|stop|opt[- ]?out)\b", re.I)
BULK_LABEL = re.compile(r"^(e\d*|em\d*|eg|cm|mc|m|t|go|mg|mail\w*|\w+mail|emails?|news\w*|communications?|comms|marketing|mktg|\w*notifications?|alerts?|highlights|\w*updates|offers|promo\w*|info|reply|replies|send|txn|engage|leave|unsub\w*|bounces?|digitalshopcard|workflow|orders?|care|customer\w*|service|ticketing|app|property|tickets)$", re.I)
BULK_REG = re.compile(r"(^|[-.])e?mails?[-.]|\we?mail\.|-mail\.", re.I)
SHARED_LOCAL = re.compile(r"^(info|hello|hi|hey|team|support|assistance|help|helpdesk|service|services|care|customercare|customerservice|guestservices|concierge|contact|contactus|membership|members|reservations?|rsvp|events?|office|sales|press|media|admin|accounts?|accounting|billing|booking|bookings|orders?|hr|jobs|careers|recruiting|talent|enquiries|inquiries|enquiry|inquiry|feedback|community|partners?|partnerships|marketing|studio|general|mail|email|ar|ap|payments|leasing|sell|shop|store|webmaster|legal|privacy|security|newsroom|ops|operations|confirmations?|itinerary|boardingpass|identification|forms|tickets|groups|inquire|alumni|management|mgmt|guestrelations|visitorservices|\w*feedback|\w*support|\w*info|\w*services?|\w*notifications?|\w*marketing\d*|printandgo)(?:[-_.+]\w*)?$", re.I)
def is_listcmd(ad):
    loc, _, dom = (ad or "").partition("@")
    return bool(LISTCMD_LOCAL.search(loc) or re.search(r"(^|\.)(unsub\w*|leave)\.", dom))
def is_bulk_domain(ad):
    """mail.x.com / e.x.com / news.x.com style sending subdomains, and x-mail.com style sending domains"""
    dom = (ad or "").partition("@")[2]; reg = reg_domain(dom)
    if reg in FREE: return False
    subs = dom[:-len(reg)].strip(".").split(".") if dom != reg else []
    return any(BULK_LABEL.match(x) for x in subs if x) or (reg.split(".")[0] not in FREE_STEM and bool(BULK_REG.search(reg)))
def is_shared_inbox(ad): return bool(SHARED_LOCAL.match((ad or "").partition("@")[0]))
def is_automated(ad):
    loc = (ad or "").partition("@")[0]
    if len(loc) > 40 or re.match(r"^\d{6,}[-+.]", loc): return True          # per-message relay / ticket ids
    return bool(NOISE_LOCAL.match(loc + ("@" if loc == "info" else "")) or NOREPLY_ANY.search(loc) or is_listcmd(ad) or is_bulk_domain(ad)
                or reg_domain(ad.partition("@")[2]) in ESP or ad.endswith("@privaterelay.appleid.com"))
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
        if NOISE_LOCAL.match(loc + ("@" if loc == "info" else "")) or NOREPLY_ANY.search(loc): hs.append("noreply-local")
        if reg_domain(dom) in ESP or dom in ESP: hs.append("esp-domain")
    return (1 if hs else 0), hs

# ---------------------------------------------------------------- IMAP layer (read-only, enforced at the wire)
READONLY_VERBS = {"CAPABILITY", "LOGIN", "LIST", "STATUS", "EXAMINE", "UID", "NOOP", "LOGOUT"}
READONLY_UID = {"FETCH", "SEARCH"}
NONPEEK = re.compile(r"BODY\[|BINARY\[|RFC822(?!\.SIZE)")
class ReadOnlyViolation(AssertionError): pass
class UidValidityChanged(Exception): pass
def check_fetch_items(items):
    if NONPEEK.search(str(items).upper().replace("BODY.PEEK[", "PEEK(").replace("BINARY.PEEK[", "PEEK(")):
        raise ReadOnlyViolation(f"non-PEEK fetch refused (would set \\Seen): {str(items)[:120]}")
def check_command(name, args):
    """Called for every command imaplib is about to send. Anything that could change the mailbox raises before it reaches the server."""
    n = str(name).upper(); a = [x.decode("latin-1") if isinstance(x, bytes) else str(x) for x in args if x is not None]
    if n not in READONLY_VERBS: raise ReadOnlyViolation(f"IMAP command {n} refused (read-only miner)")
    if n == "UID":
        sub = a[0].upper() if a else ""
        if sub not in READONLY_UID: raise ReadOnlyViolation(f"UID {sub} refused (read-only miner)")
        if sub == "FETCH": check_fetch_items(" ".join(a[2:]))
IMAP_AUDIT = None   # tests set this to a list to record every command sent
class _Guard:
    def _command(self, name, *args):
        try: check_command(name, args)
        except ReadOnlyViolation:
            self.literal = None          # e.g. a refused APPEND leaves imaplib's pending literal set; never let it ride on the next command
            raise
        if IMAP_AUDIT is not None: IMAP_AUDIT.append(" ".join([str(name)] + ["***" if str(name).upper() == "LOGIN" and i == 1 else (x.decode("latin-1") if isinstance(x, bytes) else str(x)) for i, x in enumerate(args) if x is not None]))
        return super()._command(name, *args)
class GuardedIMAP4(_Guard, imaplib.IMAP4): pass
class GuardedIMAP4_SSL(_Guard, imaplib.IMAP4_SSL): pass
TRANSIENT_NO = re.compile(r"UNAVAILABLE|THROTTL|TRY ?AGAIN|LIMIT|TOO MANY|SERVERBUG|INUSE|TEMPORAR|BUSY|OVERQUOTA|EXPUNGEISSUED", re.I)
def uid_set(uids):
    """[1,2,3,7,9,10] -> '1:3,7,9:10' (keeps FETCH command lines short)"""
    u = sorted(set(uids)); out = []; i = 0
    while i < len(u):
        j = i
        while j + 1 < len(u) and u[j + 1] == u[j] + 1: j += 1
        out.append(str(u[i]) if i == j else f"{u[i]}:{u[j]}"); i = j + 1
    return ",".join(out)
class Conn:
    def __init__(s, a): s.a = a; s.m = None; s.folder = None; s.uv = {}; s.reconnects = 0; s.no_responses = 0
    def connect(s):
        a = s.a
        if s.m:
            try: s.m.logout()
            except Exception: pass
        s.m = None
        pw = os.environ.get(a.pass_env, "")
        if not a.user or not pw: raise SystemExit(f"Set --user (or DCR_IMAP_USER) and env {a.pass_env} (app-specific password).")
        m = GuardedIMAP4(a.host, a.port, timeout=a.timeout) if a.plain else GuardedIMAP4_SSL(a.host, a.port, ssl_context=ssl.create_default_context(), timeout=a.timeout)
        try: m.login(a.user, pw)
        except imaplib.IMAP4.error as e:
            raise SystemExit(f"IMAP login refused for {a.user.split('@')[-1]} account: {str(e)[:120]}. Check DCR_IMAP_USER and that {a.pass_env} is an Apple app-specific password.")
        s.m = m
        if s.folder:
            old = s.uv.get(s.folder); s._examine(s.folder)
            if old and s.uv.get(s.folder) and s.uv[s.folder] != old: raise UidValidityChanged(s.folder)
    def retry(s, fn, retries=8):
        delay = 2
        for i in range(retries):
            try:
                if s.m is None: s.connect()
                return fn(s.m)
            except (imaplib.IMAP4.abort, ConnectionError, socket.timeout, ssl.SSLError, OSError, EOFError) as e:
                why = f"{type(e).__name__}: {str(e)[:80]}"
            except imaplib.IMAP4.error as e:     # BAD/NO raised as error: only retry the server-side "come back later" kinds
                if not TRANSIENT_NO.search(str(e)): raise
                why = f"server busy: {str(e)[:80]}"
            s.reconnects += 1
            log(f"IMAP link problem ({why}); reconnect in {delay}s ({i+1}/{retries})")
            try:
                if s.m: s.m.shutdown()
            except Exception: pass
            s.m = None; time.sleep(delay); delay = min(delay * 2, 120)
        raise RuntimeError("IMAP: giving up after repeated disconnects (progress is checkpointed; re-run the same command to resume)")
    @staticmethod
    def q(name): return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    def _examine(s, name):
        typ, d = s.m.select(s.q(name), readonly=True)   # imaplib sends EXAMINE: the server cannot change flags
        if typ != "OK": raise ValueError(f"cannot examine {name}: {d}")
        _, v = s.m.response("UIDVALIDITY")
        try: s.uv[name] = int(v[0]) if v and v[0] else s.uv.get(name, 0)
        except (TypeError, ValueError): pass
        return d
    def select(s, name, retry=True):
        s.folder = name
        d = s.retry(lambda m: s._examine(name)) if retry else s._examine(name)
        return d
    def list(s):
        typ, d = s.retry(lambda m: m.list()); return d
    def status(s, name, items):
        typ, d = s.retry(lambda m: m.status(s.q(name), items)); return d
    def status_counts(s, name, items="(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)"):
        st = " ".join(x.decode("latin-1") if isinstance(x, bytes) else str(x) for x in (s.status(name, items) or []) if x is not None)
        return {k: int(v) for k, v in re.findall(r"(MESSAGES|UNSEEN|UIDNEXT|UIDVALIDITY|RECENT) (\d+)", st)}
    def uidvalidity(s):
        uv = s.uv.get(s.folder, 0)
        if not uv and s.folder: uv = s.status_counts(s.folder, "(UIDVALIDITY)").get("UIDVALIDITY", 0)
        return uv
    def search(s, *crit, retries=8):
        typ, d = s.retry(lambda m: m.uid("SEARCH", *crit), retries)
        if typ != "OK": raise RuntimeError(f"SEARCH {' '.join(map(str, crit))[:60]} refused: {d}")
        return [int(x) for x in b" ".join(x for x in d if isinstance(x, bytes)).split() if x.isdigit()]
    def fetch(s, uids, items, retries=8, _split=False):
        """UID FETCH with bisection: a NO for one bad message (seen on large hosted mailboxes) must not lose its whole batch."""
        check_fetch_items(items)
        uids = sorted(set(uids))
        if not uids: return {}
        typ, d = s.retry(lambda m: m.uid("FETCH", uid_set(uids), items), retries)
        if typ == "OK": return parse_fetch(d)
        s.no_responses += 1; txt = " ".join(x.decode("latin-1", "replace") if isinstance(x, bytes) else str(x) for x in (d or []) if x is not None)[:120]
        if TRANSIENT_NO.search(txt) and not _split: time.sleep(3)      # back off once per refused batch, not per bisection level
        if len(uids) == 1: log(f"  FETCH UID {uids[0]} refused by server ({typ} {txt}); recorded, will retry on a later run"); return {}
        log(f"  FETCH of {len(uids)} messages got {typ} ({txt}); splitting batch")
        h = len(uids) // 2; out = s.fetch(uids[:h], items, retries, True); out.update(s.fetch(uids[h:], items, retries, True)); return out

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
        if f and not re.search(r"\\(noselect|nonexistent)\b", f["flags"], re.I): out.append(f)
    seen = set(); return [f for f in out if not (f["name"] in seen or seen.add(f["name"]))]
def is_sent_folder(f):
    return "\\sent" in f["flags"].lower() or bool(re.search(r"(^|[/.])(sent( (messages|items|mail))?|outbox)$", f["display"], re.I))
def is_junk_folder(f): return bool(re.search(r"\\(junk|trash)", f["flags"].lower())) or bool(re.search(r"(^|[/.])(junk|spam|trash|deleted( items| messages)?|bulk mail)$", f["display"], re.I))
def is_drafts_folder(f): return "\\drafts" in f["flags"].lower() or bool(re.search(r"(^|[/.])drafts?$", f["display"], re.I))
def is_nonmail_folder(f): return bool(re.search(r"(^|[/.])(notes|calendar|contacts|tasks|journal|conversation history|rss.*)$", f["display"], re.I))

LITS = None   # literal payloads referenced as \x00n\x00 markers inside a FETCH response being parsed
def sexp(s, i=0):
    while s[i] == " ": i += 1
    c = s[i]
    if c == "\x00":                                   # an IMAP literal (e.g. a non-ASCII filename) inside BODYSTRUCTURE
        j = s.index("\x00", i + 1); n = int(s[i + 1:j])
        return (decode_bytes(LITS[n]) if LITS and n < len(LITS) else ""), j + 1
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
            global LITS
            LITS = lits
            try: r["struct"] = sexp(text, mm.end())[0]
            except Exception: r["struct"] = None
            finally: LITS = None
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
def parse_date_header(raw):
    """-> (ts or None, issue or None). Handles 2-digit years, missing zone (taken as UTC), junk after the zone, and dates like 'Mon, 4 May 1998 10:00 EST'."""
    raw = re.sub(r"\s+", " ", str(raw)).strip()
    for cand in (raw, re.sub(r"\s*\([^)]*\)\s*$", "", raw), re.sub(r"^[A-Za-z]{3},?\s*", "", raw)):
        try:
            d = email.utils.parsedate_to_datetime(cand)
            if d is None: continue
            if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
            return int(d.timestamp()), None
        except Exception: continue
    t = email.utils.parsedate_tz(raw)
    if t and t[0]:
        try: return int(email.utils.mktime_tz(t if t[9] is not None else t[:9] + (0,))), None
        except Exception: pass
    return None, "bad-date"
def parse_headers(hb, idate=None):
    issues = []
    if hb and any(b > 127 for b in hb): issues.append("raw-8bit-header")
    msg = email.message_from_string(header_text(hb))
    raw_date = msg.get("Date"); ts = None
    if not raw_date: issues.append("no-date")
    else:
        ts, why = parse_date_header(raw_date)
        if why: issues.append(why)
    idts = parse_idate(idate) if idate else None; date_bad = 0
    if ts is not None and (year_of(ts) < 1980 or ts > NOW + 400 * 86400): issues.append("date-out-of-range"); ts = None
    if ts is None: ts = idts or 0; date_bad = 1
    elif idts and abs(ts - idts) > 400 * 86400: issues.append("date-vs-internal")
    for k in ("Subject", "From", "To"):
        v = msg.get(k)
        if v:
            try: str(make_header(decode_header(str(v))))
            except Exception: issues.append("header-encoding"); break
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
def qs(x): return '"' + to_ascii(x).replace('"', "").replace("\\", "") + '"'
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
def load_seed_terms(path):
    """--seeds accepts the list format ([{"name","terms",...}]) or a plain {"name": [terms]} map."""
    if not path: return DEFAULT_SEEDS
    with open(path, encoding="utf-8") as fh: js = json.load(fh)
    if isinstance(js, list): return {x["name"]: list(x["terms"]) for x in js if x.get("terms")}
    return {k: (v["terms"] if isinstance(v, dict) else list(v)) for k, v in js.items()}
def survey_accumulate(info, recs, W, selfaddr, sent_corr, recv_from, att_subj):
    for h in recs:
        b = band_of(year_of(h["ts"]) if h["ts"] else h["win"]); w = h["w"]; fa = h["from"][0]
        if info["sent"]:
            selfaddr[fa] += w
            if h["from"][1]: W["self_name"][re.sub(r"\s+", " ", h["from"][1].strip().lower())] += w
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
def survey_collect(a, db=None):
    c = Conn(a); t0 = time.time(); S = {"generated": utcnow(), "host": a.host, "user_domain": (a.user or "").split("@")[-1], "folders": [], "seeds": {}, "keywords": {}, "quirks": []}
    seeds = load_seed_terms(a.seeds)
    for nm_, terms in seeds.items():
        for t in terms:
            if to_ascii(t) != t: S["quirks"].append(f"seed term '{nm_}' contains non-ASCII; searched as '{to_ascii(t)}'")
    cache = (lambda k: (lambda r: json.loads(r[0]) if r else None)(db.execute("select v from survey_cache where k=?", (k,)).fetchone())) if db else (lambda k: None)
    def cache_put(k, v):
        if db: db.execute("insert or replace into survey_cache values(?,?,?)", (k, json.dumps(v, default=sorted), time.time())); db.commit()
    kws = [k.strip() for k in a.keywords.split(",")] if a.keywords else SURVEY_KW
    fl = list_folders(c); log(f"survey: {len(fl)} selectable folders")
    W = collections.defaultdict(lambda: collections.defaultdict(float))   # weighted counters
    folder_data = {}; selfaddr = collections.Counter(); sent_corr = {}; recv_from = collections.Counter(); att_subj = {}; msgid_sets = {}
    for f in fl:
        nm = f["name"]; info = {"name": nm, "display": f["display"], "flags": f["flags"], "depth": f["display"].count(f["delim"]) if f["delim"] not in ("NIL", "") else 0,
                                "sent": is_sent_folder(f), "junk": is_junk_folder(f), "drafts": is_drafts_folder(f), "nonmail": is_nonmail_folder(f)}
        try: st = c.status_counts(nm)
        except Exception as e: st = {}; info["status_error"] = str(e)[:80]
        info["messages"], info["unseen"], info["uidnext"], info["uidvalidity"] = st.get("MESSAGES"), st.get("UNSEEN"), st.get("UIDNEXT"), st.get("UIDVALIDITY")
        S["folders"].append(info)
        if not info["messages"] or info["nonmail"]: continue
        ck = f"folder:{nm}:{info['uidvalidity']}:{info['uidnext']}:{info['messages']}:{a.sample}:{a.sent_sample}"
        hit = cache(ck)
        if hit:
            info.update(hit["info"]); ys = {int(k): set(v) for k, v in hit["ys"].items()}; recs = hit["recs"]
            log(f"survey: {f['display']} (cached from an earlier interrupted survey)")
            msgid_sets[nm] = {h["msgid"] for h in recs if h["msgid"]}; folder_data[nm] = (info, recs, ys); survey_accumulate(info, recs, W, selfaddr, sent_corr, recv_from, att_subj); continue
        try: c.select(nm)
        except (ValueError, imaplib.IMAP4.error) as e:
            info["examine_error"] = str(e)[:100]; S["quirks"].append(f"folder '{f['display']}' could not be opened read-only: {str(e)[:80]}"); log(f"survey: cannot EXAMINE {f['display']}: {e}"); continue
        log(f"survey: {f['display']} ({info['messages']} msgs)")
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
        cache_put(ck, {"info": info, "ys": {str(k): sorted(v) for k, v in ys.items()}, "recs": recs})
        survey_accumulate(info, recs, W, selfaddr, sent_corr, recv_from, att_subj)
    # keyword + seed searches (server-side SEARCH; no data transfer)
    log("survey: keyword/seed counts")
    searchable = [(i, folder_data[i["name"]]) for i in S["folders"] if i["name"] in folder_data and not (i["junk"] or i["drafts"])]
    for k in kws: S["keywords"][k] = {"total": 0, "by_folder": {}, "noisy": k.lower() in NOISY_KW}
    seed_hits = {}; qkey = hashlib.sha1(json.dumps([kws, seeds, bool(a.no_text_search)], sort_keys=True).encode()).hexdigest()[:12]
    for info, (_i, recs, ys) in searchable:
        ck = f"search:{info['name']}:{info['uidvalidity']}:{info['uidnext']}:{info['messages']}:{qkey}"; hit = cache(ck)
        if hit is None:
            try: c.select(info["name"])
            except (ValueError, imaplib.IMAP4.error) as e: log(f"survey: cannot EXAMINE {info['display']}: {e}"); continue
            hit = {"kw": {}, "seeds": {}}; sent_years = None
            for k in kws:
                hit["kw"][k] = len(c.search("SUBJECT", qs(k)))
            for sname, terms in seeds.items():
                hh, tt = set(), set(); err = ""
                for t in terms:
                    hh |= set(c.search("OR", "SUBJECT", qs(t), "OR", "FROM", qs(t), "OR", "TO", qs(t), "CC", qs(t)))
                    if not a.no_text_search:
                        try: tt |= set(c.search("TEXT", qs(t), retries=1))
                        except Exception as e: err = type(e).__name__; c.m = None
                tt |= hh; by_year = {}
                if tt:
                    if info["imported_archive"] and sent_years is None: sent_years = folder_years(c, "sent")
                    for y, u in (sent_years if info["imported_archive"] else ys).items():
                        n = len(set(u) & tt)
                        if n: by_year[str(y)] = n
                hit["seeds"][sname] = {"hh": len(hh), "tt": sorted(tt), "by_year": by_year, "err": err}
            cache_put(ck, hit)
        for k, n in hit["kw"].items():
            if n: S["keywords"][k]["by_folder"][info["display"]] = n; S["keywords"][k]["total"] += n
        for sname, terms in seeds.items():
            sd = S["seeds"].setdefault(sname, {"terms": terms, "header_hits": 0, "text_hits": 0, "by_folder": {}, "by_year": {}, "contacts": {}, "subjects": []})
            x = hit["seeds"].get(sname) or {"hh": 0, "tt": [], "by_year": {}, "err": ""}
            if x["err"]: sd["text_search_error"] = x["err"]
            if x["hh"] or x["tt"]:
                sd["header_hits"] += x["hh"]; sd["text_hits"] += len(x["tt"]); sd["by_folder"][info["display"]] = {"header": x["hh"], "text": len(x["tt"])}
                for y, n in x["by_year"].items(): sd["by_year"][y] = sd["by_year"].get(y, 0) + n
                seed_hits.setdefault(sname, []).append((info["name"], pick(x["tt"], 15)))
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
    owner = max(W["self_name"], key=W["self_name"].get) if W["self_name"] else ""; S["owner_name"] = owner
    S["possible_self_addresses"] = sorted(k for k, v in sent_corr.items() if owner and len(owner.split()) >= 2 and k not in selfaddr and re.sub(r"\s+", " ", (v["name"] or "").strip().lower()) == owner)
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
    S["imap_stats"] = {"reconnects": c.reconnects, "no_responses": c.no_responses}
    if c.reconnects or c.no_responses: S["quirks"].append(f"IMAP connection: {c.reconnects} reconnect(s), {c.no_responses} refused FETCH batch(es) during the survey (handled; counts above are still exact)")
    S["elapsed_s"] = round(time.time() - t0)
    try: c.m and c.m.logout()
    except Exception: pass
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
            "self_addresses": S.get("possible_self_addresses", []),
            "phases": ["A: deep-mine Sent folders (all years)", "B: remaining deep folders by priority, newest year-band first", "C: headers-only folders (contacts table completeness)"]}

def survey_report(S, P):
    L = []; w = L.append; tot = sum(f.get("messages") or 0 for f in S["folders"])
    w(f"# Mailbox survey report\n\nGenerated {S['generated']} (UTC) · host `{S['host']}` · {len(S['folders'])} selectable folders · **{tot:,} messages** · survey used only LIST / STATUS / EXAMINE / SEARCH counts and sampled header peeks (no bodies fetched). Sample: up to {S['sample_sizes']['per_year_per_folder']} headers per folder-year ({S['sample_sizes']['sent_per_year']} for Sent). Percentages and domain volumes are *estimates scaled from samples*; counts from SEARCH are exact.\n")
    w("**Review required:** read sections 1-9, edit `mining-plan.json` if needed, then run `approve-survey`. Mining will not start before that.\n")
    w("## 1. Folders\n\n| Folder | Msgs | Unread | Date range (INTERNALDATE) | Noise | Attach | Flags |\n|---|---:|---:|---|---:|---:|---|")
    for i in S["folders"]:
        fl = [x for x in ("sent", "junk", "drafts", "nonmail") if i.get(x)] + (["IMPORTED-ARCHIVE"] if i.get("imported_archive") else [])
        rng = (f"{i.get('first_internal','')}-{i.get('last_internal','')}" + (f" (Date hdr {i.get('first_date_hdr')}-{i.get('last_date_hdr')})" if i.get("imported_archive") else "")) if i.get("years_internal") else ""
        w(f"| {i['display']} | {i.get('messages') or 0:,} | {i.get('unseen') if i.get('unseen') is not None else ''} | {rng} | {int(i.get('noise_share',0)*100)}% | {int(i.get('att_share',0)*100)}% | {', '.join(fl)} |")
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
    if P.get("self_addresses"): w(f"\n**Possibly your own other addresses** (sent to under your own name): {', '.join(P['self_addresses'])}. They are listed in `mining-plan.json` → `self_addresses` and will be treated as you, not as contacts. Remove any that are not you, and add any other addresses of yours, before `approve-survey`.\n")
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
    for q in S.get("quirks", []): w(f"- {q}")
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
    w(f"\n## 9. Estimated volumes for the mining stages (messages, not time or cost)\n\n- Header crawl (stage 1): ~{est_all:,} messages in folders not skipped.\n- Body fetch after the noise filter (stage 3): ~{est_deep:,} messages in deep folders (sample-based; real number known after `filter`).\n- Threads to group: roughly {est_threads:,} (non-reply share of kept messages; real number known after `thread`).")
    w("\n## 10. Recommended mining plan (also in `mining-plan.json`)\n\n| Order | Folder | Action | Msgs | Dates by | Years | Why |\n|---:|---|---|---:|---|---|---|")
    for n, f in enumerate(P["folders"], 1): w(f"| {n} | {f['display']} | **{f['action']}** | {f['messages']:,} | {f['date_basis']} | {'-'.join(map(str, f['years'])) if f['years'] else ''} | {f['why']} |")
    w("\nPhases: " + " → ".join(P["phases"]))
    w("\n**Mine first (priority senders/domains):** " + (", ".join(P["priority_domains"][:20]) or "none identified") + "\n\n**Skip as noise:** " + (", ".join(P["skip_domains"][:25]) or "none identified") + "\n\nSkipped/headers-only folders still get a header crawl if you choose, so contacts stay complete; bodies are never fetched for them.")
    return "\n".join(L) + "\n"

def write_text(path, text):
    """atomic write: a reader (the bot) never sees a half-written report/CSV"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh: fh.write(text)
    os.replace(tmp, path)
def write_csv(path, rows):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh: csv.writer(fh).writerows(rows)
    os.replace(tmp, path)
def cmd_survey(a):
    db = opendb(a.db); stage(db, "survey", "running")
    S = survey_collect(a, db); P = survey_plan(S); os.makedirs(a.out, exist_ok=True)
    write_text(os.path.join(a.out, "survey.json"), json.dumps(S, indent=1, default=sorted))
    write_text(os.path.join(a.out, "mining-plan.json"), json.dumps(P, indent=1))
    write_text(os.path.join(a.out, "survey-report.md"), survey_report(S, P))
    meta_set(db, "survey_done", S["generated"]); meta_set(db, "survey_approved", ""); stage(db, "survey", "done")
    db.execute("delete from survey_cache"); db.commit()
    print(f"survey written to {a.out}/ (survey-report.md, survey.json, mining-plan.json). Review, then: approve-survey")
def cmd_approve(a):
    db = opendb(a.db)
    if not meta_get(db, "survey_done"): raise SystemExit("run `survey` first")
    meta_set(db, "survey_approved", utcnow()); print("survey approved; crawl may start")

# ---------------------------------------------------------------- STAGE 1: crawl (headers + structure, all folders, resumable)
def need_survey(db):
    if not meta_get(db, "survey_approved"):
        raise SystemExit("Stage 0 not done: run `survey`, review survey-report.md with the owner, then `approve-survey`. Mining refuses to start before that.")
def load_plan(a):
    p = os.path.join(a.out, "mining-plan.json"); return json.load(open(p)) if os.path.exists(p) else None
MSG_COLS = "msgid,irt,refs,from_addr,from_name,subject,ts,date_bad,size,has_att,att,parts,bulk,hints,is_sent"
def store_message(db, folder, is_sent, uid, x):
    """Upsert by (folder, uid) keeping the row id, so bodies/sigs/decisions stay attached when a message is seen again."""
    h = parse_headers(x["sections"].get(f"HEADER.FIELDS ({HDR_FIELDS})", b""), x.get("idate"))
    parts, atts = struct_info(x.get("struct"))
    vals = (h["msgid"], h["irt"], " ".join(h["refs"]), h["from"][0], h["from"][1], h["subject"], h["ts"], h["date_bad"], int(x.get("size") or 0), 1 if atts else 0,
            json.dumps(atts), json.dumps(parts), h["bulk"], ",".join(h["hints"]), is_sent)
    row = db.execute("select id from messages where folder=? and uid=?", (folder, uid)).fetchone()
    if row:
        mid = row[0]; db.execute("update messages set " + ",".join(c + "=?" for c in MSG_COLS.split(",")) + " where id=?", vals + (mid,))
    else:
        mid = db.execute(f"insert into messages(folder,uid,{MSG_COLS}) values(?,?,{','.join('?' * 15)})", (folder, uid) + vals).lastrowid
    db.execute("delete from addrs where mid=?", (mid,))
    rows = [(mid, "from", *h["from"])] + [(mid, "to", *t) for t in h["to"]] + [(mid, "cc", *t) for t in h["cc"]]
    db.executemany("insert into addrs values(?,?,?,?)", [r for r in rows if r[2]])
    return mid
def forget_folder(db, nm):
    ids = "select id from messages where folder=?"
    for t, col in (("addrs", "mid"), ("bodies", "mid"), ("sigs", "mid")): db.execute(f"delete from {t} where {col} in ({ids})", (nm,))
    db.execute("delete from messages where folder=?", (nm,)); db.execute("delete from crawl_skips where folder=?", (nm,))
def imap_date(v):
    """'2019' | 2019 | '2019-06-01' -> IMAP date '1-Jan-2019' / '1-Jun-2019'"""
    v = str(v).strip(); m = re.match(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?$", v)
    if not m: raise SystemExit(f"bad date {v!r}: use YYYY or YYYY-MM-DD")
    y, mo, d = int(m.group(1)), int(m.group(2) or 1), int(m.group(3) or 1)
    return f"{d}-{'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split()[mo - 1]}-{y}"
def dstr(y): return imap_date(y)
def crawl_criteria(a, p):
    pre = "SENT" if p and p.get("date_basis") == "SENTSINCE" else ""; crit = []
    since = getattr(a, "since", None) or (p or {}).get("crawl_since"); before = getattr(a, "before", None) or (p or {}).get("crawl_before")
    if since: crit += [pre + "SINCE", imap_date(since)]
    if before: crit += [pre + "BEFORE", imap_date(before)]
    return crit or ["ALL"]
MAX_SKIP_TRIES = 3
def crawl_folder(c, db, f, a, p):
    nm = f["name"]; c.select(nm); uv = c.uidvalidity(); sent = 1 if is_sent_folder(f) else 0; junk = 1 if is_junk_folder(f) else 0
    row = db.execute("select * from folders where name=?", (nm,)).fetchone()
    if row and uv and row["uidvalidity"] and row["uidvalidity"] != uv:
        log(f"{f['display']}: UIDVALIDITY changed ({row['uidvalidity']} -> {uv}); the server renumbered this folder, re-crawling it")
        forget_folder(db, nm); db.commit()
    crit = crawl_criteria(a, p)
    match = c.search(*crit)
    have = {r[0] for r in db.execute("select uid from messages where folder=?", (nm,))}
    gave_up = {r[0] for r in db.execute("select uid from crawl_skips where folder=? and tries>=?", (nm, MAX_SKIP_TRIES))}
    todo = sorted(set(match) - have - gave_up)
    if getattr(a, "limit", None) and len(todo) > a.limit: todo = todo[-a.limit:]     # newest UIDs first when bounded; the rest are picked up by the next run
    n_have = len(set(match) & have)
    db.execute("insert or replace into folders(name,display,flags,uidvalidity,last_uid,total,done,is_sent,updated,is_junk,crit) values(?,?,?,?,?,?,?,?,?,?,?)",
               (nm, f["display"], f["flags"], uv or (row["uidvalidity"] if row else 0), max(have) if have else 0, len(match), n_have, sent, time.time(), junk, " ".join(crit)))
    db.commit(); log(f"{f['display']}: {len(match)} match {' '.join(crit)}; {n_have} already stored; {len(todo)} to crawl now" + (f"; {len(gave_up)} unfetchable after {MAX_SKIP_TRIES} tries" if gave_up else ""))
    if a.dry_run: return len(todo)
    done = n_have; items = f"(UID RFC822.SIZE INTERNALDATE FLAGS {HDR_ITEMS} BODYSTRUCTURE)"
    for i in range(0, len(todo), a.batch):
        b = todo[i:i + a.batch]; r = c.fetch(b, items)
        for u in b:
            if u in r: store_message(db, nm, sent, u, r[u])
            else: db.execute("insert into crawl_skips values(?,?,?,1,?,?) on conflict(folder,uid) do update set tries=tries+1, ts=excluded.ts", (nm, u, uv, "no FETCH data returned", time.time()))
        got = [u for u in b if u in r]
        if got: db.execute("delete from crawl_skips where folder=? and uid in (%s)" % ",".join(map(str, got)), (nm,))
        done += len(got)
        db.execute("update folders set last_uid=max(last_uid, ?), done=?, updated=? where name=?", (max(got) if got else 0, done, time.time(), nm)); db.commit()
        if (i // a.batch) % 10 == 0: log(f"  {f['display']}: {done}/{len(match)}")
        time.sleep(a.delay)
    return len(todo)
def cmd_crawl(a):
    db = opendb(a.db); need_survey(db); c = Conn(a); plan = load_plan(a); pf = {f["name"]: f for f in plan["folders"]} if plan else {}
    stage(db, "crawl", "running")
    fl = [f for f in list_folders(c) if not is_nonmail_folder(f)]
    fl.sort(key=lambda f: (pf.get(f["name"], {}).get("priority", 5 if not is_junk_folder(f) else 9)))
    total = 0; failed = []
    for f in fl:
        p = pf.get(f["name"])
        if p and p["action"] == "skip" and not a.include_skipped: log(f"skip {f['display']} (plan)"); continue
        if not p and (is_junk_folder(f) or is_drafts_folder(f)) and not a.include_skipped: continue
        meta_set(db, "action:" + f["name"], p["action"] if p else "deep")
        for attempt in (1, 2):
            try: total += crawl_folder(c, db, f, a, p); break
            except UidValidityChanged:
                log(f"{f['display']}: UIDVALIDITY changed during the crawl; restarting this folder"); c.uv.pop(f["name"], None); db.execute("update folders set uidvalidity=-1 where name=?", (f["name"],)); db.commit()
            except (ValueError, imaplib.IMAP4.error) as e:
                log(f"{f['display']}: cannot crawl ({str(e)[:100]}); continuing with other folders"); failed.append(f["display"]); break
    n = db.execute('select count(*) from messages').fetchone()[0]
    stage(db, "crawl", "done", f"{n} messages" + (f"; failed folders: {', '.join(failed)}" if failed else ""))
    print(f"crawl complete: {n} messages in db ({total} new this run)" + (f"; could not open: {', '.join(failed)}" if failed else "") + (" [dry-run]" if a.dry_run else ""))

# ---------------------------------------------------------------- STAGE 2: pre-filter + per-sender decision table
def self_set(db, a):
    s = {x.strip().lower() for x in os.environ.get("DCR_SELF", "").split(",") if x.strip()} | ({a.user.lower()} if getattr(a, "user", None) else set())
    s |= {x.strip().lower() for x in (load_plan(a) or {}).get("self_addresses", []) if isinstance(x, str) and "@" in x}   # owner-confirmed list from the survey plan
    db.execute("update messages set is_sent=1 where folder in (select name from folders where is_sent=1)")
    for r in db.execute("select from_addr, count(*) n from messages where is_sent=1 group by from_addr having n>=2"): s.add(r[0])
    db.execute("update messages set is_sent=1 where from_addr in (%s)" % ",".join("?" * len(s)), tuple(s)) if s else None
    return s
def cmd_filter(a):
    db = opendb(a.db); stage(db, "filter", "running"); selfs = self_set(db, a); meta_set(db, "self", json.dumps(sorted(selfs)))
    junk_folders = {r["name"] for r in db.execute("select name, flags, display from folders") if is_junk_folder({"flags": r["flags"] or "", "display": r["display"] or r["name"]})}
    plan = load_plan(a) or {}; pri = set(plan.get("priority_domains", [])); skipd = set(plan.get("skip_domains", []))
    ov = {}
    ovf = os.path.join(a.out, "sender-overrides.csv")
    if os.path.exists(ovf):
        with open(ovf, encoding="utf-8-sig") as fh:
            for r in csv.reader(fh):
                if len(r) >= 2 and r[1].strip().lower() in ("keep", "noise"): ov[r[0].strip().lower()] = r[1].strip().lower()
    # duplicates across folders (same Message-ID)
    db.execute("update messages set decision=null, dreason=null")
    for t, col in (("addrs", "addr"), ("messages", "from_addr"), ("sigs", "addr")):          # rows stored before Hide-My-Email decoding existed
        for (ad,) in db.execute(f"select distinct {col} from {t} where {col} like '%\\_at\\_%' escape '\\' and ({col} like '%@icloud.com' or {col} like '%@privaterelay.appleid.com')").fetchall():
            if real_addr(ad) != ad: db.execute(f"update {t} set {col}=? where {col}=?", (real_addr(ad), ad))
    lc = [r[0] for r in db.execute("select id, subject from messages where is_sent=1").fetchall() if LISTCMD_SUBJ.match(r[1] or "")]
    lc += [r[0] for r in db.execute("select m.id, group_concat(a.addr, ' ') from messages m join addrs a on a.mid=m.id and a.role in ('to','cc') where m.is_sent=1 group by m.id").fetchall()
           if r[1] and all(is_listcmd(x) for x in r[1].split())]
    db.executemany("update messages set decision='noise', dreason='unsubscribe request sent by owner' where id=?", [(i,) for i in set(lc)])
    db.execute("update messages set decision='dup', dreason='duplicate message-id' where id in (select m.id from messages m join messages k on k.msgid=m.msgid and k.msgid!='' and k.id<m.id)")
    sent_refs = set(); written = collections.Counter(); sent_dom = collections.Counter()
    for r in db.execute("select irt, refs, id from messages where is_sent=1 and decision is null"):
        if r[0]: sent_refs.add(r[0])
        sent_refs.update((r[1] or "").split())
    for r in db.execute("select a.addr from addrs a join messages m on m.id=a.mid where m.is_sent=1 and m.decision is null and a.role in ('to','cc')"):
        if is_listcmd(r[0]): continue
        written[r[0]] += 1
        if not is_automated(r[0]): sent_dom[dom_of(r[0])] += 1
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
        elif is_automated(ad): k, why = "noise", "automated or bulk-mail sending address"
        elif d in ESP or d in skipd and s["rep"] == 0: k, why = "noise", "ESP / survey noise domain"
        elif s["bulk"] / s["n"] >= 0.5: k, why = "noise", "bulk/list headers"
        elif s["n"] >= 6 and s["rep"] == 0 and s["proj"] / s["n"] < 0.2 and d not in FREE: k, why = "noise", "one-way blast (>=6 msgs, never replied, no project signals)"
        elif sent_dom.get(d) and d not in FREE: k, why = "keep", "known organisation domain (David wrote to a colleague)"
        else: k, why = "keep", "unknown human sender, low volume"
        dec[ad] = (k, why)
        db.execute("insert or replace into senders values(?,?,?,?,?,?,?,?,?,?,?)", (ad, s["name"], d, s["n"], s["rep"], s["bulk"], wt, s["first"], s["last"], k, why))
    n_up = 0
    for r in db.execute("select id,from_addr,subject,folder,has_att,att,bulk,msgid from messages where is_sent=0 and decision is null").fetchall():
        k, why = dec.get(r["from_addr"], ("keep", "no sender"))
        if re.match(r"(mailer-daemon|postmaster)", r["from_addr"]) and re.search(r"undeliver|delivery status|failure notice|returned mail|delivery failed|undelivered|could not be delivered", r["subject"] or "", re.I):
            db.execute("update messages set decision='noise', dreason='delivery failure notice' where id=?", (r["id"],)); continue
        junk = r["folder"] in junk_folders and not written.get(r["from_addr"])
        if junk: k, why = "noise", "junk/trash folder"
        elif k == "keep" and r["bulk"] and r["msgid"] not in sent_refs and r["from_addr"] not in ov and dom_of(r["from_addr"]) not in ov: k, why = "noise", "mailing-list/bulk message David did not reply to"
        elif k == "keep" and SUBJ_NOISE.search(r["subject"] or "") and not written.get(r["from_addr"]) and not SUBJ_PROJECT.search(r["subject"] or ""): k, why = "noise", "promotional/receipt subject"
        elif k == "noise" and not r["bulk"] and SUBJ_PROJECT.search(r["subject"] or "") and r["has_att"] and not NOISE_LOCAL.match(r["from_addr"].split("@")[0]) and r["from_addr"] not in ov and dom_of(r["from_addr"]) not in ESP:
            k, why = "keep", "project-like subject + attachment from human (rescued from sender-level noise)"; n_up += 1
        db.execute("update messages set decision=?, dreason=? where id=?", (k, why, r["id"]))
    db.execute("update messages set decision='keep', dreason='sent by owner' where is_sent=1 and decision is null")
    db.commit()
    cnt = db.execute("select decision, count(*) from messages group by decision").fetchall()
    print("filter decisions:", {r[0]: r[1] for r in cnt}, f"| rescued {n_up} project-like messages from noise senders")
    top = db.execute("select addr,n,reason from senders where decision='noise' order by n desc limit 15").fetchall()
    print("top noise senders:", "; ".join(f"{r[0]}({r[1]})" for r in top)); stage(db, "filter", "done")

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
def decode_part(raw, enc, charset, ctype, truncated=False):
    try:
        if enc == "base64": raw = base64.b64decode(raw + b"=" * (-len(raw.strip()) % 4))
        elif enc == "quoted-printable": raw = quopri.decodestring(raw)
    except Exception: pass
    t = decode_bytes(raw, charset, truncated=truncated)
    if ctype == "text/html" and not charset:
        mm = re.search(rb"<meta[^>]+charset=[\"']?([\w.:-]+)", raw[:4000], re.I)
        if mm: t = decode_bytes(raw, mm.group(1).decode("ascii", "ignore"))
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
TITLE = re.compile(r"\b(vp|vice president|svp|evp|president|ceo|coo|founder|co-?founder|owner|partner|principal|director|head of [\w ]+|executive producer|producer|line producer|production manager|manager|coordinator|supervisor|art director|creative director|production designer|set designer|set decorator|designer|buyer|art buyer|producer|editor|photographer|stylist|agent|rep|assistant|associate|lead|chief [\w ]+|artist|carpenter|fabricator|builder|painter|estimator|planner|account (?:manager|executive)|professor|lecturer|dean|attorney|counsel|analyst|engineer|architect|consultant|recruiter|broker|headhunter)\b", re.I)
def split_sig(t):
    L = t.rstrip().split("\n")
    for i in range(len(L) - 1, max(-1, len(L) - 16), -1):
        if L[i].strip() in ("--", "-- ") or CLOSER.match(L[i].strip()):
            return "\n".join(L[:i]).strip(), "\n".join(L[i:]).strip()
    paras = re.split(r"\n\s*\n", t.rstrip())
    if len(paras) >= 2 and len(paras[-1].split("\n")) <= 8 and (PHONE.search(paras[-1]) or SITE.search(paras[-1])): return "\n\n".join(paras[:-1]).strip(), paras[-1].strip()
    return t.strip(), ""
def parse_sig(sig, addr=""):
    if not sig: return None
    lines = [l.strip(" \t|·•") for l in sig.split("\n") if l.strip()][:10]; r = {"name": sig_name(sig, addr) if addr else "", "title": "", "company": "", "phone": "", "site": ""}
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
        try: c.select(folder)
        except (ValueError, imaplib.IMAP4.error) as e: log(f"fetch: cannot open {folder} ({str(e)[:80]}); skipped this run"); continue
        r0 = db.execute("select uidvalidity from folders where name=?", (folder,)).fetchone()
        if r0 and r0[0] and c.uv.get(folder) and r0[0] != c.uv[folder]:
            log(f"fetch: {folder} UIDVALIDITY changed since the crawl ({r0[0]} -> {c.uv[folder]}); UIDs no longer match. Re-run `crawl` first; skipped."); continue
        for i in range(0, len(lst), batch):
            if i and (i // batch) % 20 == 0: log(f"  fetch: {folder} {i}/{len(lst)}")
            chunk = lst[i:i + batch]; groups = collections.defaultdict(list); meta = {}
            for r in chunk:
                parts = json.loads(r["parts"] or "[]"); pick_ = next((p for p in parts if p["type"] == "text/plain"), None) or next((p for p in parts if p["type"] == "text/html"), None)
                if not pick_: db.execute("insert or replace into bodies values(?,?,?,?,?)", (r["id"], "", "", "", 0)); continue
                groups[pick_["path"]].append(r["uid"]); meta[r["uid"]] = (r, pick_)
            for path, uids in groups.items():
                got = c.fetch(uids, f"(UID BODY.PEEK[{path}]<0.{cap}>)")
                for u in uids:
                    if u not in got: continue          # server returned nothing: leave it for the next run instead of storing an empty body
                    r, p_ = meta[u]; raw = got[u].get("sections", {}).get(path.upper(), b"") or b""
                    t = decode_part(raw, p_["enc"], p_["charset"], p_["type"], truncated=len(raw) >= cap); new, quoted = split_quote(t); body, sig = split_sig(new)
                    db.execute("insert or replace into bodies values(?,?,?,?,?)", (r["id"], body[:20000], quoted[:3000], sig[:1500], 1 if len(raw) >= cap else 0))
                    ps = parse_sig(sig, r["from_addr"]) if not r["is_sent"] else None
                    if ps and r["from_addr"]: db.execute("insert into sigs values(?,?,?,?,?,?,?,?)", (r["id"], r["from_addr"], ps["name"], ps["title"], ps["company"], ps["phone"], ps["site"], r["ts"]))
            db.commit()
        log(f"fetch: {folder} done")
def body_candidates(db):
    """messages whose body we may fetch: survived the filter AND live in a folder the plan marks deep"""
    rows = db.execute("select id,folder,uid,parts,from_addr,ts,decision,is_sent from messages where decision='keep' and id not in (select mid from bodies) order by folder, uid").fetchall()
    act = {}
    def deep(f):
        if f not in act: act[f] = meta_get(db, "action:" + f, "deep") == "deep"
        return act[f]
    return [r for r in rows if deep(r["folder"])]
def cmd_fetch(a):
    db = opendb(a.db); need_survey(db); stage(db, "fetch", "running")
    rows = body_candidates(db)
    log(f"fetch: {len(rows)} messages need bodies")
    if a.dry_run: print(f"[dry-run] would fetch {len(rows)} bodies (text parts only, capped at {a.max_body} bytes each)"); return
    if rows:
        try: fetch_bodies(db, Conn(a), rows, a.max_body, a.body_batch)
        except UidValidityChanged as e: raise SystemExit(f"fetch: folder {e} was renumbered by the server (UIDVALIDITY changed). Run `crawl` again, then `fetch`.")
    n = db.execute("select count(*) from bodies").fetchone()[0]; left = len(body_candidates(db))
    stage(db, "fetch", "done", f"{n} bodies" + (f"; {left} still missing (server returned nothing; re-run to retry)" if left else ""))
    print("fetch complete:", n, "bodies stored" + (f"; {left} could not be fetched this run (re-run to retry)" if left else ""))

# ---------------------------------------------------------------- STAGE 4: thread reconstruction
GENERIC_SUBJ = {"", "hi", "hello", "hey", "(no subject)", "no subject", "fwd", "photos", "pics", "question", "update", "thanks", "thank you", "info", "following up", "quick question"}
def cmd_thread(a):
    db = opendb(a.db); stage(db, "thread", "running"); selfs = set(json.loads(meta_get(db, "self", "[]"))); par = {}
    def find(x):
        while par.setdefault(x, x) != x: par[x] = par[par[x]]; x = par[x]
        return x
    def union(x, y): par[find(x)] = find(y)
    rows = db.execute("select id,msgid,irt,refs,subject,ts,from_addr from messages where decision='keep'").fetchall()
    byid = {}
    for r in rows:
        find(r["id"])
        if r["msgid"]: byid.setdefault(r["msgid"], r["id"])
    for r in rows:
        for ref in ([r["irt"]] if r["irt"] else []) + (r["refs"] or "").split():
            if ref in byid: union(r["id"], byid[ref])
    parts = collections.defaultdict(set)
    for r in db.execute("select mid, addr from addrs where mid in (select id from messages where decision='keep')"):
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
    db.execute("delete from threads"); db.execute("update messages set thread=null where thread is not null")
    for root, g in comp.items():
        tid = min(r["id"] for r in g); g.sort(key=lambda r: r["ts"]); ppl = sorted(set().union(*[parts[r["id"]] for r in g]))
        subj = re.sub(r"^\s*((re|fw|fwd|aw)\s*:\s*)+", "", g[0]["subject"] or "", flags=re.I).strip() or norm_subject(g[0]["subject"])
        db.execute("insert into threads values(?,?,?,?,?,?)", (tid, subj, g[0]["ts"], g[-1]["ts"], len(g), json.dumps(ppl)))
        db.executemany("update messages set thread=? where id=?", [(tid, r["id"]) for r in g])
    db.commit(); n = db.execute("select count(*), max(n) from threads").fetchone()
    stage(db, "thread", "done", f"{n[0]} threads"); print(f"threads: {n[0]} (largest has {n[1]} messages) from {len(rows)} kept messages")

# ---------------------------------------------------------------- STAGE 5: contacts (one row per unique email address) + which threads they are in
SIG_NAME = re.compile(r"^[A-Z][A-Za-z'’\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][A-Za-z'’\-]+){1,2}$")
COMPANYISH = re.compile(r"\b(inc|llc|ltd|corp|co|company|studios?|productions?|pictures|films|group|agency|design|media|entertainment|records|events|creative|partners|associates|\w*works|scenic|shop|props|rentals|supply|lighting|construction|fabrication|photography|gallery|labs?)\b\.?$", re.I)
WORK_ATT = re.compile(r"\.(pdf|xlsx?|docx?|psd|ai|eps|dwg|dxf|skp|3dm|rvt|indd|key|pptx?)$", re.I)
def clean_name(n, addr=""):
    n = re.sub(r"\s+", " ", (n or "").strip().strip("\"'")).strip()
    if not n or "@" in n or n.lower() == addr.lower() or not re.search(r"[A-Za-z]", n): return ""
    m = re.match(r"^([^,]+),\s*([^,]+)$", n)                       # "Lopez, Maria" -> "Maria Lopez"
    return f"{m.group(2)} {m.group(1)}" if m and " " not in m.group(1).strip() else n
def is_full_name(n): return len(n.split()) >= 2
ROLE_LOCAL = set("info sales team hello hi office admin accounts accounting billing studio contact press hr jobs careers booking bookings events print marketing support help service orders mail email reply news media design production producer creative art photo".split())
def addr_name(ad):
    """'patrick.casani@x' -> 'Patrick Casani' (only first.last / first_last shapes, never role mailboxes)"""
    m = re.match(r"^([a-z]{2,})[._]([a-z]{2,})$", ad.split("@")[0].lower())
    return "" if not m or m.group(1) in ROLE_LOCAL or m.group(2) in ROLE_LOCAL else f"{m.group(1).title()} {m.group(2).title()}"
def sig_name(sig, addr):
    """the sender's name line from their signature block; must match the address or sit right under the sign-off"""
    loc = re.sub(r"[^a-z]", "", addr.split("@")[0].lower()); first = None
    for i, l in enumerate([x.strip(" \t|·•-–—,") for x in (sig or "").split("\n") if x.strip()][:6]):
        if CLOSER.match(l): continue
        if first is None: first = i
        if TITLE.search(l) or PHONE.search(l) or "@" in l or SITE.search(l) or len(l) > 40 or COMPANYISH.search(l) or not SIG_NAME.match(l): continue
        toks = [re.sub(r"[^a-z]", "", t.lower()) for t in l.split()]
        if any(len(t) >= 3 and t in loc for t in toks) or i == first: return l
    return ""
def is_contact_addr(ad):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", ad or "")) and not NOISE_LOCAL.match(ad.split("@")[0]) and dom_of(ad) not in ESP and not NOREPLY_ANY.search(ad.split("@")[0]) and not is_listcmd(ad) and not ad.endswith("@privaterelay.appleid.com")
SIG_JUNK = re.compile(r"pronoun|\b(he|she|they)/(him|her|them)\b|privacy|confidential|disclaimer|unsubscribe|intended recipient|this (e-?mail|message)|sent from|view (in|this)|click|copyright|©|all rights reserved|[:\[\]{}<>]|\.\s+\w", re.I)
def clean_org(x):
    """a signature company value, or '' when it is a footer line or really a job title"""
    x = re.sub(r"\s+", " ", (x or "").strip(" *_,.|")).strip()
    if not x or len(x) > 60 or SIG_JUNK.search(x) or (TITLE.search(x) and not COMPANYISH.search(x)): return ""
    return x
def clean_title(x):
    x = re.sub(r"\s+", " ", (x or "").strip(" *_,.|")).strip()
    parts = [p.strip() for p in re.split(r"\s+\|\s+", x)]
    if len(parts) == 2 and SIG_NAME.match(parts[0]) and not TITLE.search(parts[0]): x = parts[1]     # "Jo Smith | SVP, Sales" -> "SVP, Sales"
    return "" if not x or len(x) > 70 or SIG_JUNK.search(x) or x[0].islower() and len(x) > 25 else x
CC_MAX = 10   # people who were only CC'd count as contacts only on conversations this small
def org_label(d): return d.split(".")[0].replace("-", " ").title() if d and d not in FREE else ""
def cmd_link(a):
    """Contacts = people. An address counts if they wrote to David, David wrote to them, or they were CC'd on a small
    conversation that David or a two-way contact took part in. Shared inboxes (info@, hello@ ...) David wrote to are kept
    as kind='shared' so they show up under their company; other shared inboxes and automated senders are dropped."""
    db = opendb(a.db); stage(db, "link", "running"); selfs = set(json.loads(meta_get(db, "self", "[]")))
    C = {}; T = collections.defaultdict(set); t_david = set(); t_from = collections.defaultdict(set)
    for r in db.execute("select a.addr,a.name,a.role,m.ts,m.is_sent,m.thread from addrs a join messages m on m.id=a.mid where m.decision='keep' and m.thread is not null"):
        ad = r["addr"]
        if r["is_sent"]: t_david.add(r["thread"])
        if ad in selfs or re.sub(r"\+[^@]*@", "@", ad) in selfs or not is_contact_addr(ad): continue
        x = C.setdefault(ad, {"names": collections.Counter(), "name_ts": {}, "first": None, "last": None, "from": 0, "to": 0, "threads": set()})
        nm = clean_name(r["name"], ad)
        if nm: x["names"][nm] += 1; x["name_ts"][nm] = max(x["name_ts"].get(nm, 0), r["ts"] or 0)   # ties go to the most recent spelling
        if r["ts"]: x["first"] = min(x["first"] or r["ts"], r["ts"]); x["last"] = max(x["last"] or 0, r["ts"])
        x["threads"].add(r["thread"]); T[r["thread"]].add(ad)
        if r["role"] == "from" and not r["is_sent"]: x["from"] += 1; t_from[r["thread"]].add(ad)
        elif r["role"] in ("to", "cc") and r["is_sent"]: x["to"] += 1
    two_way = {ad for ad, x in C.items() if x["from"] and x["to"]}
    def why_drop(ad, x):
        if is_automated(ad): return "automated or bulk-mail address"
        if is_shared_inbox(ad): return "" if x["to"] else "shared inbox David never wrote to"
        if x["from"] or x["to"]: return ""
        ok = [t for t in x["threads"] if len(T[t]) <= CC_MAX and (t in t_david or t_from[t] & two_way)]
        if not ok: return "only CC'd on large or one-way mail"
        x["threads"] = set(ok); return ""
    drops = collections.Counter()
    for ad in list(C):
        w = why_drop(ad, C[ad])
        if w: drops[w] += 1; del C[ad]
    latest = collections.defaultdict(dict)                              # newest usable signature value per field
    for r in db.execute("select addr,name,title,company,phone,ts from sigs order by ts"):
        for k, v in (("name", r["name"]), ("title", clean_title(r["title"])), ("company", clean_org(r["company"])), ("phone", r["phone"])):
            if v: latest[r["addr"]][k] = v
    db.execute("delete from contacts"); db.execute("delete from thread_contacts")
    full = 0
    for ad, x in C.items():
        s = latest.get(ad, {}); kind = "shared" if is_shared_inbox(ad) else "person"
        hdr = max(x["names"], key=lambda n: (is_full_name(n), x["names"][n], x["name_ts"][n])) if x["names"] else ""; sg = clean_name(s.get("name", ""), ad)
        cands = ((hdr, "header"), (sg, "signature"), (addr_name(ad), "address"))
        name, src = next(((n, k) for n, k in cands if n and is_full_name(n)), None) or next(((n, k) for n, k in cands if n), ("", ""))
        full += bool(name) and is_full_name(name) and kind == "person"
        d = dom_of(ad)
        db.execute("insert into contacts(addr,name,name_src,company,title,phone,domain,first_ts,last_ts,n_from,n_to,n_threads,kind) values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (ad, name, src, s.get("company", "") or org_label(d), s.get("title", ""), s.get("phone", ""), d, x["first"], x["last"], x["from"], x["to"], len(x["threads"]), kind))
        db.executemany("insert into thread_contacts values(?,?)", [(t, ad) for t in x["threads"]])
    db.commit(); src_n = dict(db.execute("select name_src, count(*) from contacts where kind='person' group by 1").fetchall())
    n_p = sum(src_n.values()); n_s = len(C) - n_p
    stage(db, "link", "done", f"{n_p} contacts")
    print(f"contacts: {n_p} people ({full} with a full name; names from headers {src_n.get('header', 0)}, signatures {src_n.get('signature', 0)}, address shape {src_n.get('address', 0)}; no name {src_n.get('', 0)})"
          f" + {n_s} shared company inboxes David wrote to | left out: " + (", ".join(f"{n} {w}" for w, n in drops.most_common()) or "none"))

# ---------------------------------------------------------------- STAGE 6: projects = threads about the same job, with the same people
STOP = set("re fwd fw aw update updated final revised revision new the a an and of for to with from on in at v1 v2 v3 attached info thanks thank you follow up question request hi hello hey please quick call today tomorrow quote estimate invoice schedule bid".split())
WORK_WORDS = re.compile(r"\b(?:" + SUBJ_PROJECT.pattern[1:-1] + ")", re.I)
def ptoks(name): return {w for w in re.findall(r"[a-z]{3,}", (name or "").lower()) if w not in STOP}
def clean_subj(s): return re.sub(r"^\s*((re|fw|fwd|aw)\s*:\s*)+", "", s or "", flags=re.I).strip()
def cmd_projects(a):
    """A thread can start a project only if David took part and it shows work (work words in subject/body, or a document/drawing attachment).
    Threads that share a contact and (nearly) the same subject words, years ignored, are one project (so an annual job is one project across years)."""
    db = opendb(a.db); stage(db, "projects", "running")
    tc = collections.defaultdict(set)
    for r in db.execute("select tid, addr from thread_contacts"): tc[r[0]].add(r[1])
    T = {}
    for r in db.execute("select t.id, t.subj, t.first_ts, t.last_ts, t.n, sum(m.is_sent) sent, group_concat(m.att, '\x1f') atts from threads t join messages m on m.thread=t.id group by t.id"):
        if r["id"] not in tc or not r["sent"]: continue
        words = {w.lower() for w in WORK_WORDS.findall(r["subj"] or "")}; files = set()
        for js in (r["atts"] or "").split("\x1f"):
            for x in json.loads(js or "[]"):
                if WORK_ATT.search(x.get("name") or ""): files.add(x["name"].rsplit(".", 1)[-1].lower())
        if not words:
            for (txt,) in db.execute("select substr(b.text,1,4000) from bodies b join messages m on m.id=b.mid where m.thread=?", (r["id"],)):
                words |= {w.lower() for w in WORK_WORDS.findall(txt or "")}
        if words or files: T[r["id"]] = {"r": r, "tk": ptoks(re.sub(r"\b(19|20)\d\d\b", "", clean_subj(r["subj"]))), "why": sorted(words)[:8] + [f".{f} file" for f in sorted(files)][:4]}
    par = {t: t for t in T}
    def find(x):
        while par[x] != x: par[x] = par[par[x]]; x = par[x]
        return x
    post = collections.defaultdict(list)
    for t, v in T.items():
        for w in v["tk"]: post[w].append(t)
    for w, ts in post.items():
        if len(ts) > 300: continue                                       # very common word: pairs are found through rarer words
        for i, p in enumerate(ts):
            for q in ts[i + 1:]:
                if find(p) == find(q) or not (tc[p] & tc[q]): continue
                A, B = T[p]["tk"], T[q]["tk"]; sh = len(A & B)
                if sh / len(A | B) >= 0.75 or (sh >= 2 and sh == min(len(A), len(B))): par[find(p)] = find(q)
    groups = collections.defaultdict(list)
    for t in T: groups[find(t)].append(t)
    db.execute("delete from projects"); db.execute("delete from project_threads")
    for g in groups.values():
        g.sort(key=lambda t: T[t]["r"]["first_ts"]); rs = [T[t]["r"] for t in g]
        top = max(rs, key=lambda r: (r["n"], -r["first_ts"])); name = clean_subj(top["subj"])
        if len({year_of(r["first_ts"]) for r in rs}) > 1: name = re.sub(r"\s{2,}", " ", re.sub(r"\b(19|20)\d\d\b", "", name)).strip(" -:") or name
        why = sorted({w for t in g for w in T[t]["why"]})
        pid = min(g); ppl = sorted(set().union(*[tc[t] for t in g]))
        db.execute("insert into projects values(?,?,?,?,?,?,?,?)", (pid, name or "(no subject)", min(r["first_ts"] for r in rs), max(r["last_ts"] for r in rs), len(g), sum(r["n"] for r in rs), ";".join(ppl), ", ".join(why[:12])))
        db.executemany("insert into project_threads values(?,?)", [(pid, t) for t in g])
    db.commit(); stage(db, "projects", "done", f"{len(groups)} projects"); print(f"projects: {len(groups)} (from {len(T)} work threads)")

# ---------------------------------------------------------------- STAGE 7: exports (CRM-ready CSVs)
def cell(x, cap=1000):
    """CSV cells stay readable for the bot: one line, capped length"""
    if x is None: return ""
    if isinstance(x, float) and x.is_integer(): x = int(x)
    t = re.sub(r"\s+", " ", str(x)).strip()
    return t if len(t) <= cap else t[:cap - 1] + "…"
CONTACT_COLS = ["Name", "Email", "Company", "Title", "Phone", "Domain", "Name source", "First contact", "Last contact", "Messages from them", "Messages to them", "Threads", "Projects"]
COMPANY_COLS = ["Company", "Domain", "Contacts", "Shared inboxes", "Threads", "First contact", "Last contact"]
THREAD_COLS = ["Thread ID", "Subject", "First date", "Last date", "Messages", "Messages from David", "Contacts", "Attachments", "Project ID"]
PROJECT_COLS = ["Project ID", "Project", "First date", "Last date", "Years", "Threads", "Messages", "Contacts", "Companies", "Thread IDs", "Why included"]
def cmd_export(a):
    db = opendb(a.db); stage(db, "export", "running"); os.makedirs(a.out, exist_ok=True)
    W = lambda name, hdr, rows: write_csv(os.path.join(a.out, name), [hdr] + [[cell(x) for x in r] for r in rows])
    proj = {r["id"]: r for r in db.execute("select * from projects")}; t2p = {r[1]: r[0] for r in db.execute("select pid, tid from project_threads")}
    con = {r["addr"]: r for r in db.execute("select * from contacts")}
    c_proj = collections.defaultdict(set)
    for r in db.execute("select tid, addr from thread_contacts"):
        if r[0] in t2p: c_proj[r[1]].add(t2p[r[0]])
    allc = sorted(con.values(), key=lambda r: -(r["last_ts"] or 0)); rows = [r for r in allc if (r["kind"] or "person") == "person"]
    W("contacts.csv", CONTACT_COLS, [[r["name"], r["addr"], r["company"], r["title"], r["phone"], r["domain"], r["name_src"], ymd(r["first_ts"]), ymd(r["last_ts"]), r["n_from"], r["n_to"], r["n_threads"],
                                       "; ".join(proj[p]["name"] for p in sorted(c_proj[r["addr"]], key=lambda p: -proj[p]["last_ts"])[:10])] for r in rows])
    co = {}
    for r in allc:
        key = r["domain"] if r["domain"] not in FREE else ("co:" + re.sub(r"[^a-z0-9]", "", r["company"].lower()) if r["company"] else "")
        if not key or key == "co:": continue
        e = co.setdefault(key, {"names": collections.Counter(), "dom": r["domain"] if r["domain"] not in FREE else "", "ppl": 0, "shared": [], "thr": set(), "first": r["first_ts"], "last": r["last_ts"]})
        if r["company"]: e["names"][r["company"]] += 1
        if (r["kind"] or "person") == "person": e["ppl"] += 1
        else: e["shared"].append(r["addr"])
        e["first"] = min(x for x in (e["first"], r["first_ts"]) if x) if (e["first"] or r["first_ts"]) else None; e["last"] = max(e["last"] or 0, r["last_ts"] or 0)
    for r in db.execute("select tid, addr from thread_contacts"):
        c = con.get(r[1])
        if c:
            k = c["domain"] if c["domain"] not in FREE else ("co:" + re.sub(r"[^a-z0-9]", "", c["company"].lower()) if c["company"] else "")
            if k in co: co[k]["thr"].add(r[0])
    W("companies.csv", COMPANY_COLS, [[e["names"].most_common(1)[0][0] if e["names"] else org_label(e["dom"]), e["dom"], e["ppl"], "; ".join(sorted(e["shared"])[:10]), len(e["thr"]), ymd(e["first"]), ymd(e["last"])] for e in sorted(co.values(), key=lambda e: (-e["ppl"], -(e["last"] or 0)))])
    tc = collections.defaultdict(list)
    for r in db.execute("select tid, addr from thread_contacts order by addr"): tc[r[0]].append(r[1])
    trows = []
    for r in db.execute("select t.id, t.subj, t.first_ts, t.last_ts, t.n, sum(m.is_sent) sent, sum(m.has_att) att from threads t join messages m on m.thread=t.id group by t.id order by t.last_ts desc"):
        if r["id"] in tc: trows.append([r["id"], r["subj"], ymd(r["first_ts"]), ymd(r["last_ts"]), r["n"], r["sent"], "; ".join(tc[r["id"]][:30]), r["att"], t2p.get(r["id"], "")])
    W("threads.csv", THREAD_COLS, trows)
    p_thr = collections.defaultdict(list)
    for r in db.execute("select pid, tid from project_threads order by tid"): p_thr[r[0]].append(r[1])
    prow = []
    for p in sorted(proj.values(), key=lambda p: -p["last_ts"]):
        ppl = [x for x in (p["contacts"] or "").split(";") if x]
        cos = sorted({con[x]["company"] for x in ppl if x in con and con[x]["company"]})
        yrs = sorted({year_of(p["first_ts"]), year_of(p["last_ts"])}); prow.append([p["id"], p["name"], ymd(p["first_ts"]), ymd(p["last_ts"]), "-".join(map(str, yrs)) if len(yrs) > 1 else yrs[0], p["n_threads"], p["n_msgs"], "; ".join(ppl[:30]), "; ".join(cos[:15]), " ".join(map(str, p_thr[p["id"]][:50])), p["why"]])
    W("projects.csv", PROJECT_COLS, prow)
    n_msg = db.execute("select count(*) from messages").fetchone()[0]; dec = dict(db.execute("select decision, count(*) from messages group by decision").fetchall())
    yrs = collections.Counter(year_of(r["last_ts"]) for r in rows if r["last_ts"])
    md = [f"# Mining summary ({utcnow()})", "", "Extraction only: contacts, the threads they are in, and the projects those threads form. No scoring or labels.", "",
          f"- Messages read (headers): {n_msg:,}; kept: {dec.get('keep', 0):,}; junk/automated: {dec.get('noise', 0):,}; duplicates: {dec.get('dup', 0):,}",
          f"- Contacts (unique addresses of people): {len(rows):,}; shared company inboxes David wrote to (listed in companies.csv): {len(allc) - len(rows):,}; with a full name: {sum(1 for r in rows if is_full_name(r['name'] or '')):,}; name from signature: {sum(1 for r in rows if r['name_src'] == 'signature'):,}",
          f"- Threads with contacts: {len(trows):,}; projects: {len(prow):,} (covering {len(t2p):,} threads)", f"- Companies: {len(co):,}", "",
          "## Contacts by year of last contact", ""] + [f"- {y}: {n:,}" for y, n in sorted(yrs.items())] + ["", "## Files", "",
          "- contacts.csv: one row per unique email address", "- companies.csv: company domains (and signature company names for personal addresses)",
          "- threads.csv: every conversation with at least one contact; Project ID when it belongs to a project", "- projects.csv: groups of work threads; `Why included` lists the words/files that qualified it",
          f"- {os.path.basename(a.db)}: SQLite with all headers, kept bodies, threads, contacts and projects (tables: messages, addrs, bodies, sigs, threads, contacts, thread_contacts, projects, project_threads)"]
    write_text(os.path.join(a.out, "summary.md"), "\n".join(md) + "\n")
    stage(db, "export", "done"); print(f"export: contacts {len(rows)}, companies {len(co)}, threads {len(trows)}, projects {len(prow)} -> {a.out}/ (summary.md first)")

def cmd_status(a):
    if not os.path.exists(a.db): print(f"no database at {a.db} yet: start with `survey`"); return
    db = opendb(a.db); who = lock_holder(a.db)
    print("dcrmine", VERSION, "| now", utcnow(), "| running:", who or "no (safe to start or re-run any stage)")
    print("survey:", meta_get(db, "survey_done") or "not run", "| approved:", meta_get(db, "survey_approved") or "NO")
    cur = meta_get(db, "stage:current")
    if cur: print("last stage event:", cur)
    for st in ("survey", "crawl", "filter", "fetch", "thread", "link", "projects", "export"):
        v = meta_get(db, "stage:" + st)
        if v: print(f"  {st:9} {v}")
    print("folders (stored/matching):")
    for r in db.execute("select display,total,done,last_uid,is_sent,updated,crit from folders order by display"):
        print(f"  {r[0][:40]:40} {r[2] or 0}/{r[1] or 0}" + (" [sent]" if r[4] else "") + (f" [{r[6]}]" if r[6] and r[6] != "ALL" else "") + (f" updated {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(r[5]))}" if r[5] else ""))
    sk = db.execute("select count(*), sum(tries>=?) from crawl_skips", (MAX_SKIP_TRIES,)).fetchone()
    if sk[0]: print(f"  messages the server would not return: {sk[0]} ({sk[1] or 0} given up after {MAX_SKIP_TRIES} tries)")
    for t in ("messages", "bodies", "threads", "projects"): print(f"  {t}: {db.execute(f'select count(*) from {t}').fetchone()[0]}")
    n_people = db.execute("select count(*) from contacts where coalesce(kind,'person')='person'").fetchone()[0]; print(f"  contacts (people): {n_people}")
    left = len(body_candidates(db)) if meta_get(db, "stage:filter") else 0
    if left: print(f"  bodies still to fetch: {left}")
def cmd_seen_audit(a):
    """Read-only proof that mining did not mark mail read: snapshot UID SEARCH UNSEEN per folder, compare with the earlier snapshot."""
    c = Conn(a); path = os.path.join(a.out, "seen-audit.json"); os.makedirs(a.out, exist_ok=True)
    snap = {"taken": utcnow(), "folders": {}}
    for f in list_folders(c):
        try: c.select(f["name"])
        except (ValueError, imaplib.IMAP4.error): continue
        snap["folders"][f["name"]] = {"uidvalidity": c.uv.get(f["name"], 0), "unseen": c.search("UNSEEN"), "messages": c.status_counts(f["name"], "(MESSAGES)").get("MESSAGES")}
    if not os.path.exists(path) or a.force:
        write_text(path, json.dumps(snap)); n = sum(len(v["unseen"]) for v in snap["folders"].values())
        print(f"seen-audit: baseline saved ({n} unread messages in {len(snap['folders'])} folders). Run mining, then `seen-audit` again to compare."); return
    with open(path, encoding="utf-8") as fh: old = json.load(fh)
    changed = 0; lines = []
    for nm, v in old["folders"].items():
        nw = snap["folders"].get(nm)
        if not nw or nw["uidvalidity"] != v["uidvalidity"]: lines.append(f"  {utf7d(nm)}: not comparable (folder gone or renumbered)"); continue
        became_read = sorted(set(v["unseen"]) - set(nw["unseen"])); changed += len(became_read)
        lines.append(f"  {utf7d(nm)}: unread {len(v['unseen'])} -> {len(nw['unseen'])}; previously-unread now read: {len(became_read)}" + (f" (UIDs {became_read[:10]})" if became_read else ""))
    print(f"seen-audit vs baseline {old['taken']}:"); print("\n".join(lines))
    print("RESULT: no message changed from unread to read" if not changed else f"RESULT: {changed} message(s) changed unread -> read since the baseline (if the owner read mail meanwhile this is expected; dcrmine only uses EXAMINE + BODY.PEEK)")
def cmd_run(a):
    for fn in (cmd_crawl, cmd_filter, cmd_fetch, cmd_thread, cmd_link, cmd_projects, cmd_export): fn(a)
    print("run complete: read summary.md first")
def build_parser():
    p = argparse.ArgumentParser(prog="dcrmine", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["survey", "approve-survey", "crawl", "filter", "fetch", "thread", "link", "projects", "export", "run", "status", "seen-audit"])
    p.add_argument("--db", default="dcr_mail.sqlite"); p.add_argument("--out", default="mining-out", help="work dir: survey + exports")
    p.add_argument("--host", default="imap.mail.me.com"); p.add_argument("--port", type=int, default=993); p.add_argument("--plain", action="store_true", help="no TLS (test fixture only)")
    p.add_argument("--user", default=os.environ.get("DCR_IMAP_USER")); p.add_argument("--pass-env", default="ICLOUD_APP_PASSWORD"); p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--dry-run", action="store_true"); p.add_argument("--delay", type=float, default=0.2); p.add_argument("--batch", type=int, default=200)
    p.add_argument("--sample", type=int, default=25, help="survey: headers per folder-year"); p.add_argument("--sent-sample", type=int, default=400, help="survey: headers per Sent folder-year")
    p.add_argument("--seeds"); p.add_argument("--keywords"); p.add_argument("--no-text-search", action="store_true")
    p.add_argument("--include-skipped", action="store_true"); p.add_argument("--body-batch", type=int, default=50); p.add_argument("--max-body", type=int, default=60000)
    p.add_argument("--force", action="store_true", help="seen-audit: replace the saved baseline")
    p.add_argument("--since", help="crawl: only messages dated on/after YYYY or YYYY-MM-DD (bounded first run)"); p.add_argument("--before", help="crawl: only messages dated before YYYY or YYYY-MM-DD")
    p.add_argument("--limit", type=int, help="crawl: at most N new messages per folder this run (newest first); re-run to continue")
    p.add_argument("--version", action="version", version="dcrmine " + VERSION)
    return p
READ_ONLY_CMDS = {"status"}
def main(argv=None):
    a = build_parser().parse_args(argv)
    fn = {"survey": cmd_survey, "approve-survey": cmd_approve, "crawl": cmd_crawl, "filter": cmd_filter, "fetch": cmd_fetch, "thread": cmd_thread, "link": cmd_link, "projects": cmd_projects,
          "export": cmd_export, "run": cmd_run, "status": cmd_status, "seen-audit": cmd_seen_audit}[a.cmd]
    if a.cmd in READ_ONLY_CMDS or a.cmd == "seen-audit": return fn(a)
    with RunLock(a.db):
        try: return fn(a)
        except KeyboardInterrupt: raise SystemExit("interrupted; progress is saved - re-run the same command to resume")
if __name__ == "__main__": main()
