"""Scale: huge folders (200k messages) over IMAP (fast, always runs) and a 120k-message memory benchmark of the offline stages (set DCR_SCALE=1)."""
import datetime as dt, imaplib, json, os, random, resource, subprocess, sys, time, unittest
from helpers import ENV, ROOT, Workdir, args_for, dcrmine as D


class HugeFolder(unittest.TestCase):
    def setUp(self):
        self.w = Workdir(self); os.environ.setdefault("ICLOUD_APP_PASSWORD", "x")
        d = dt.datetime(2015, 1, 1)
        self.w.server.box.f["Big"] = [{"uid": i, "flags": set(), "raw": b"", "d": d} for i in range(1, 200001)]
        self.c = D.Conn(args_for(user="u@mac.com", plain=True, host="127.0.0.1", port=self.w.server.port, timeout=30))
        self.addCleanup(lambda: self.c.m and self.c.m.shutdown())

    def test_search_reply_over_1mb(self):
        self.c.select("Big")
        self.assertEqual(len(self.c.search("ALL")), 200000)                        # ~1.29 MB single response line

    def test_stdlib_default_line_limit_would_have_failed(self):
        old = imaplib._MAXLINE; imaplib._MAXLINE = 1000000
        try:
            self.c.select("Big")
            with self.assertRaises(imaplib.IMAP4.error): self.c.m.uid("SEARCH", "ALL")
        finally: imaplib._MAXLINE = old


BENCH = r'''
import json, os, random, resource, sys, time
sys.path.insert(0, sys.argv[1]); import dcrmine as D
db_path, out, n = sys.argv[2], sys.argv[3], int(sys.argv[4]); R = random.Random(1)
db = D.opendb(db_path); D.meta_set(db, "survey_done", "x"); D.meta_set(db, "survey_approved", "x")
db.execute("insert into folders(name,display,flags,uidvalidity,is_sent) values('INBOX','INBOX','',1,0),('Sent Messages','Sent Messages','\\Sent',1,1)")
people = [f"person{i}@client{i % 900}.example" for i in range(3000)]; noise = [f"news@esp{i}.example" for i in range(200)]
words = "quote estimate invoice set build trophy backdrop render shoot props deposit schedule load-in strike delivery".split()
t0 = time.time(); mid = 0
for i in range(n):
    sent = i % 5 == 0; bulk = (not sent) and i % 3 == 0; p = R.choice(people); fr = "dcr@mac.com" if sent else (R.choice(noise) if bulk else p)
    subj = ("Re: " if i % 2 else "") + f"{R.choice(words)} job {i % 7000}"; ts = 946684800 + R.randint(0, 25 * 365 * 86400)
    h = f"Message-ID: <m{i}@x>\r\nFrom: {fr}\r\nTo: {'dcr@mac.com' if not sent else p}\r\nSubject: {subj}\r\nDate: {time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime(ts))}\r\n"
    if i % 2: h += f"In-Reply-To: <m{i - 1}@x>\r\nReferences: <m{i - 1}@x>\r\n"
    if bulk: h += "List-Unsubscribe: <mailto:u@x>\r\nPrecedence: bulk\r\n"
    x = {"sections": {f"HEADER.FIELDS ({D.HDR_FIELDS})": (h + "\r\n").encode()}, "idate": None, "size": 4000,
         "struct": ["TEXT", "PLAIN", ["CHARSET", "UTF-8"], None, None, "7BIT", "400", "10"]}
    m = D.store_message(db, "Sent Messages" if sent else "INBOX", 1 if sent else 0, i + 1, x)
    if not bulk: db.execute("insert into bodies values(?,?,?,?,?)", (m, f"Hi David, {R.choice(words)} for the {R.choice(words)} is ${R.randint(1, 40) * 500:,}. Thanks.", "", "", 0))
    if i % 5000 == 4999: db.commit()
db.commit(); res = {"messages": n, "ingest_s": round(time.time() - t0, 1)}
class A: pass
a = D.build_parser().parse_args(["status", "--db", db_path, "--out", out])
for name, fn in (("filter", D.cmd_filter), ("thread", D.cmd_thread), ("link", D.cmd_link), ("projects", D.cmd_projects), ("export", D.cmd_export)):
    t = time.time(); fn(a); rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    res[name] = {"s": round(time.time() - t, 1), "peak_rss_mb": round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024))}
print("BENCH " + json.dumps(res))
'''


@unittest.skipUnless(os.environ.get("DCR_SCALE"), "set DCR_SCALE=1 (or a message count) to run the 120k-message benchmark")
class MemoryBenchmark(unittest.TestCase):
    def test_offline_stages_stay_small(self):
        import tempfile, shutil
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        n = int(os.environ["DCR_SCALE"]) if os.environ["DCR_SCALE"].isdigit() and int(os.environ["DCR_SCALE"]) > 1 else 120000
        r = subprocess.run([sys.executable, "-c", BENCH, ROOT, os.path.join(d, "m.sqlite"), os.path.join(d, "out"), str(n)], capture_output=True, text=True, env=ENV, timeout=3600)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        res = json.loads([l for l in r.stdout.splitlines() if l.startswith("BENCH ")][-1][6:]); print("\n" + json.dumps(res, indent=1))
        self.assertLess(max(v["peak_rss_mb"] for v in res.values() if isinstance(v, dict)), 2500)


if __name__ == "__main__": unittest.main()
