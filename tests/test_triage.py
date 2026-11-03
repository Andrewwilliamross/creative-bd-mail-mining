"""Bot triage tools: show / search / decide / decisions / bounces, and the evidence + decision columns in the exports.
All of them work from the SQLite file only: none may open an IMAP connection."""
import csv, hashlib, json, os, sqlite3, tempfile, unittest
from helpers import ROOT, Workdir, read_only_violations
import dcrmine


def rows(path):
    with open(path, newline="", encoding="utf-8") as fh: return list(csv.DictReader(fh))


def by(rs, key): return {r[key]: r for r in rs}


class Tools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        w = cls.w = Workdir(cls)
        w.mine("survey"); w.mine("approve-survey"); w.mine("run")

    def sha(self):
        with open(self.w.db, "rb") as fh: return hashlib.sha256(fh.read()).hexdigest()
    def contacts(self): return by(rows(self.w.path("contacts.csv")), "Email")
    def reexport(self):
        for st in ("filter", "thread", "link", "projects", "export"): self.w.mine(st)

    # ---------------- evidence columns for triage
    def test_contacts_carry_triage_evidence(self):
        c = self.contacts(); m = c["maria.lopez@pepsi-demo.example"]
        self.assertEqual(m["Two-way"], "two-way"); self.assertIn("Rookie of the Year trophy 2025", m["Sample subjects"]); self.assertIn("pdf x", m["Attachment types"])
        self.assertEqual(c["paul@grantevents.example"]["Two-way"], "they wrote only"); self.assertEqual(c["jamie.rivera@riveraprops.example"]["Two-way"], "David wrote only")
        self.assertIn("dana.moore@brightline-demo.example", c["dana.moore@halobev-demo.example"]["Same name, other addresses"])   # same person, two addresses: bot decides whether to merge
        self.assertEqual(c["charlie.ward@paramount-demo.example"]["Title"], "VP, Creative Advertising")
        co = by(rows(self.w.path("companies.csv")), "Domain"); self.assertIn("Charlie Ward (VP, Creative Advertising)", co["paramount-demo.example"]["People"])

    # ---------------- bounces
    def test_bounce_is_found_exactly_and_flagged(self):
        b = by(rows(self.w.path("bounces.csv")), "Email"); self.assertEqual(list(b), ["pat.kim@oldagency-demo.example"])
        r = b["pat.kim@oldagency-demo.example"]
        self.assertEqual((r["Kind"], r["Status code"], r["Subject David had sent"], r["Found by"]), ("hard", "5.1.1", "Catching up", "failure notice body"))
        self.assertIn("current-address lookup", r["Next step"]); self.assertIn("User unknown", r["Reason"]); self.assertEqual(r["Last bounce"], "2016-04-02")
        c = self.contacts()
        self.assertEqual(c["pat.kim@oldagency-demo.example"]["Outreach status"], "bounced: find current address"); self.assertEqual(c["maria.lopez@pepsi-demo.example"]["Outreach status"], "ok")
        self.assertEqual(self.w.sql("select count(*) from bodies b join messages m on m.id=b.mid where m.dreason='delivery failure notice'")[0][0], 1)

    def test_bounce_without_body_falls_back_to_sent_mail_by_subject(self):
        db = dcrmine.opendb(self.w.db)
        try:
            body = db.execute("select * from bodies where mid=(select id from messages where dreason='delivery failure notice')").fetchone()
            db.execute("delete from bodies where mid=?", (body["mid"],)); db.commit()
            self.assertEqual(dcrmine.build_bounces(db), 1)
            r = dcrmine.bounce_rows(db)[0]; self.assertEqual((r["addr"], r["how"]), ("pat.kim@oldagency-demo.example", "matched to David's sent mail by subject"))
            db.execute("insert or replace into bodies values(?,?,?,?,?)", tuple(body)); db.commit()
        finally: db.close()

    def test_bounce_parsing_variants(self):
        with tempfile.TemporaryDirectory() as d:
            db = dcrmine.opendb(os.path.join(d, "t.sqlite")); dcrmine.meta_set(db, "self", json.dumps(["me@mac.com"]))
            def msg(i, ts, subj, sent, frm, to, decision="keep", reason=None):
                db.execute("insert into messages(id,folder,uid,subject,ts,from_addr,is_sent,decision,dreason) values(?,?,?,?,?,?,?,?,?)", (i, "F", i, subj, ts, frm, sent, decision, reason))
                for ad in to: db.execute("insert into addrs values(?,?,?,?)", (i, "to", ad, ""))
            msg(1, 1000, "Hello", 1, "me@mac.com", ["gone@old.example", "fine@ok.example"])
            msg(2, 2000, "Hi again", 1, "me@mac.com", ["slow@later.example"])
            msg(3, 3000, "Delivery Status Notification (Failure)", 0, "mailer-daemon@googlemail.com", ["me@mac.com"], "noise", dcrmine.DSN_REASON)
            db.execute("insert into bodies values(3,?,?,?,0)", ("Your message wasn't delivered to gone@old.example because the address couldn't be found.\n550 5.1.1 The email account that you tried to reach does not exist.\n\nOriginal message:\nTo: gone@old.example, fine@ok.example\nSubject: Hello", "", ""))
            msg(4, 4000, "Delivery Status Notification (Delay)", 0, "mailer-daemon@googlemail.com", ["me@mac.com"], "noise", dcrmine.DSN_REASON)
            db.execute("insert into bodies values(4,?,?,?,0)", ("Delivery to slow@later.example has been delayed. Status: 4.4.1 Will keep trying.", "", ""))
            msg(5, 5000, "Hello back", 0, "gone@old.example", ["me@mac.com"])                        # wrote to David after the bounce: may be alive
            db.commit(); self.assertEqual(dcrmine.build_bounces(db), 2); rs = {r["addr"]: r for r in dcrmine.bounce_rows(db)}
            self.assertEqual(set(rs), {"gone@old.example", "slow@later.example"})                    # the cc'd recipient quoted in the original is NOT flagged
            self.assertEqual((rs["gone@old.example"]["kind"], rs["gone@old.example"]["status"]), ("hard", "5.1.1")); self.assertTrue(rs["gone@old.example"]["heard_after"]); self.assertFalse(rs["gone@old.example"]["dead"])
            self.assertEqual(rs["slow@later.example"]["kind"], "soft"); self.assertFalse(rs["slow@later.example"]["dead"])
            db.close()

    # ---------------- show
    def test_show_thread_in_order_and_truncates(self):
        before = self.sha()
        tid = self.w.sql("select id from threads where subj='The Ebenezer Movie executive conference room'")[0][0]
        out = self.w.mine("show", "--thread", str(tid), "--chars", "40").stdout
        i1, i2, i3 = out.index("| 2024-10-03 "), out.index("| 2024-10-06 "), out.index("| 2024-11-02 ")
        self.assertLess(i1, i2); self.assertLess(i2, i3)                                           # oldest message first
        self.assertIn("more characters", out); self.assertIn("from David", out); self.assertIn("attachments:", out)
        full = self.w.mine("show", "--thread", str(tid), "--chars", "0", "--sig", "--quoted").stdout
        self.assertIn("Budget around $12,500", full); self.assertIn("[signature]", full); self.assertNotIn("more characters", full)
        self.assertEqual(self.sha(), before)                                                       # read-only: the database file is byte-identical

    def test_show_contact_domain_project_message_and_limits(self):
        out = self.w.mine("show", "--email", "charlie.ward@paramount-demo.example", "--limit", "3").stdout
        self.assertIn("VP, Creative Advertising at Paramount Pictures", out); self.assertIn("triage: undecided", out); self.assertIn("signature 2024", out)
        self.assertIn("more message(s) not shown", out); self.assertEqual(out.count("--- message"), 3)
        self.assertEqual(self.w.mine("show", "--domain", "paramount-demo.example", "--list").stdout.count("--- message"), 0)
        newest = self.w.mine("show", "--email", "charlie.ward@paramount-demo.example", "--newest", "--limit", "1").stdout
        self.assertIn("Ebenezer", newest.split("=== thread")[1])
        pid = self.w.sql("select id from projects where name like 'NOBL Travel%'")[0][0]
        po = self.w.mine("show", "--project", str(pid)).stdout; self.assertIn("NOBL Travel airport set", po); self.assertIn("sam@noblgear.example", po)
        mid = self.w.sql("select id from messages where subject like 'Devis%'")[0][0]
        self.assertIn("décor du café", self.w.mine("show", "--message", str(mid)).stdout)
        since = self.w.mine("show", "--email", "charlie.ward@paramount-demo.example", "--since", "2024", "--list").stdout; self.assertIn("1 thread(s) between 2024", since)
        self.assertNotEqual(self.w.mine("show", check=False).returncode, 0); self.assertNotEqual(self.w.mine("show", "--message", "99999999", check=False).returncode, 0)

    def test_fetch_ids_downloads_a_dropped_message_on_demand_read_only(self):
        mid = self.w.sql("select id from messages where decision='noise' and dreason!='delivery failure notice' order by id desc limit 1")[0][0]
        self.assertIn("no body stored", self.w.mine("show", "--message", str(mid)).stdout)
        r = self.w.mine("fetch", "--ids", str(mid)); self.assertIn("fetch complete", r.stdout)
        self.assertNotIn("no body stored", self.w.mine("show", "--message", str(mid)).stdout)
        self.assertEqual(self.w.mine("fetch", "--ids", "99999999").returncode, 0)
        self.assertEqual(read_only_violations(self.w.server.commands()), [])

    def test_show_says_when_no_body_is_stored(self):
        mid = self.w.sql("select id from messages where decision='noise' and dreason!='delivery failure notice' order by id limit 1")[0][0]
        self.assertIn("no body stored", self.w.mine("show", "--message", str(mid)).stdout)

    # ---------------- search
    def test_search(self):
        n = len(self.w.server.commands())
        o = self.w.mine("search", "BUDGET", "Executive").stdout
        self.assertIn("thread", o); self.assertIn("Ebenezer", o); self.assertNotIn("Rookie of the Year", o)         # AND + case-insensitive
        self.assertIn("matching", o)
        self.assertIn("Müller", self.w.mine("search", "MÜLLER", "--field", "subject").stdout)                         # non-ASCII case folding
        self.assertIn("-- 0 of 0", self.w.mine("search", "zzzqqq").stdout)
        sig = self.w.mine("search", "creative advertising").stdout; self.assertIn("charlie.ward@paramount-demo.example", sig)  # signatures are searched too
        self.assertNotIn("sam@noblgear", self.w.mine("search", "luggage", "--email", "jordan@fieldhouse-demo.example").stdout.replace("jordan@fieldhouse-demo.example", ""))
        o_since = self.w.mine("search", "trophy", "--since", "2025").stdout; self.assertNotIn("2024-", o_since)
        self.assertNotIn("Undeliverable", self.w.mine("search", "undeliverable", "--field", "subject").stdout)
        self.assertIn("Undeliverable", self.w.mine("search", "undeliverable", "--field", "subject", "--include-noise").stdout)
        self.assertEqual(self.w.mine("search", check=False).returncode == 0, False)
        self.assertEqual(len(self.w.server.commands()), n)                                                            # none of these touched the mailbox

    # ---------------- decisions
    def test_decisions_lifecycle(self):
        w = self.w; M = w.mine
        M("decide", "--email", "tom@reyesmetalworks.example", "--decision", "junk", "--role", "vendor", "--note", "David buys fabrication from him", "--evidence", "threads quote TO David")
        M("decide", "--email", "charlie.ward@paramount-demo.example", "--decision", "keep", "--role", "past-client", "--evidence", "VP Creative Advertising, 5 threads")
        M("decide", "--email", "charlie.ward@paramount-demo.example", "--decision", "keep", "--note", "neutral context only")          # leaving role out keeps the old role
        M("decide", "--domain", "westsidelumber.example", "--decision", "junk", "--role", "vendor")
        M("decide", "--email", "gary@finchevents.example", "--decision", "keep", "--role", "past-client", "--note", "invoice dispute 2015 (thread: Invoice 1188); David to decide")
        for bad in (("--email", "nope", "--decision", "keep"), ("--email", "a@b.example", "--decision", "maybe"), ("--email", "a@b.example", "--decision", "keep", "--role", "boss"), ("--decision", "keep")):
            self.assertNotEqual(M("decide", *bad, check=False).returncode, 0, bad)
        with open(w.path("do-not-contact.csv"), "w") as fh: fh.write("# David's list\nemail,note\nkim.ng@ngstudio.example,asked not to be contacted\n")
        self.reexport()                                                                                         # filter/link rebuild everything else: decisions must survive
        c = self.contacts(); ch = c["charlie.ward@paramount-demo.example"]
        self.assertEqual((ch["Decision"], ch["Role"], ch["Triage note"], ch["Outreach status"]), ("keep", "past-client", "neutral context only", "ok"))
        self.assertEqual(c["tom@reyesmetalworks.example"]["Decision"], "junk")
        self.assertEqual(c["gary@finchevents.example"]["Decision"], "keep"); self.assertEqual(c["gary@finchevents.example"]["Outreach status"], "ok")   # rocky history is a note, never an exclusion
        self.assertIn("invoice dispute", c["gary@finchevents.example"]["Triage note"])
        self.assertEqual(c["kim.ng@ngstudio.example"]["Outreach status"], "do-not-contact")
        keep = rows(w.path("crm-contacts.csv")); self.assertEqual({r["Email"] for r in keep}, {"charlie.ward@paramount-demo.example", "gary@finchevents.example"})
        self.assertEqual(len(rows(w.path("decisions.csv"))), 4)
        co = by(rows(w.path("companies.csv")), "Domain"); self.assertEqual((co["westsidelumber.example"]["Decision"], co["westsidelumber.example"]["Role"]), ("junk", "vendor"))
        pend = M("decisions", "--pending", "--limit", "100").stdout
        self.assertNotIn("charlie.ward@", pend); self.assertNotIn("kim.ng@", pend); self.assertIn("maria.lopez@", pend); self.assertIn("without a decision", pend)
        self.assertIn("domain  westsidelumber.example", M("decisions").stdout)
        csvp = os.path.join(w.tmp, "batch.csv")
        with open(csvp, "w") as fh: fh.write("Email,Domain,Decision,Role,Note,Evidence\nmaria.lopez@pepsi-demo.example,,keep,buyer,,annual Pepsi trophy\n,reyesmetalworks.example,junk,vendor,,\n")
        M("decide", "--from-csv", csvp)
        M("decide", "--email", "tom@reyesmetalworks.example", "--remove")
        M("export"); c = self.contacts()
        self.assertEqual((c["maria.lopez@pepsi-demo.example"]["Decision"], c["maria.lopez@pepsi-demo.example"]["Role"]), ("keep", "buyer"))
        self.assertEqual(c["tom@reyesmetalworks.example"]["Decision"], "junk")                                  # contact row removed -> domain decision applies
        M("decide", "--email", "gary@finchevents.example", "--decision", "do-not-contact", "--note", "David said so"); M("export")
        self.assertEqual(self.contacts()["gary@finchevents.example"]["Outreach status"], "do-not-contact")
        # cleanup so no other test sees these
        for k in ("charlie.ward@paramount-demo.example", "gary@finchevents.example", "maria.lopez@pepsi-demo.example"): M("decide", "--email", k, "--remove")
        M("decide", "--domain", "westsidelumber.example", "--domain", "reyesmetalworks.example", "--remove"); os.remove(w.path("do-not-contact.csv")); M("export")
        self.assertFalse(os.path.exists(w.path("crm-contacts.csv"))); self.assertEqual(self.contacts()["kim.ng@ngstudio.example"]["Outreach status"], "ok")

    def test_the_wire_stays_read_only_with_dsn_bodies(self):
        self.assertEqual(read_only_violations(self.w.server.commands()), [])


if __name__ == "__main__": unittest.main()
