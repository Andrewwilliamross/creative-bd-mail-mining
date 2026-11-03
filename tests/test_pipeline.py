"""End-to-end: survey -> gate -> run against the synthetic mailbox, then check every stage's output and the read-only guarantee."""
import csv, json, os, re, shutil, unittest
from helpers import ENV, ROOT, Workdir, read_only_violations

CONTACTS = ["Name", "Email", "Company", "Title", "Phone", "Domain", "Name source", "First contact", "Last contact", "Messages from them", "Messages to them", "Threads", "Projects",
            "Two-way", "Sample subjects", "Attachment types", "Same name, other addresses", "Decision", "Role", "Triage note", "Outreach status"]
COMPANIES = ["Company", "Domain", "Contacts", "Shared inboxes", "Threads", "First contact", "Last contact", "People", "Decision", "Role", "Triage note"]
THREADS = ["Thread ID", "Subject", "First date", "Last date", "Messages", "Messages from David", "Contacts", "Attachments", "Project ID"]
PROJECTS = ["Project ID", "Project", "First date", "Last date", "Years", "Threads", "Messages", "Contacts", "Companies", "Thread IDs", "Why included"]
OUTPUTS = ["survey-report.md", "mining-plan.json", "summary.md", "contacts.csv", "companies.csv", "threads.csv", "projects.csv", "bounces.csv", "decisions.csv"]


def load(path):
    with open(path, encoding="utf-8") as fh: return json.load(fh) if path.endswith(".json") else fh.read()


def rows(path):
    with open(path, newline="", encoding="utf-8") as fh: return list(csv.reader(fh))


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        w = cls.w = Workdir(cls)
        shutil.copy(os.path.join(ROOT, "staple-seeds.example.json"), os.path.join(w.tmp, "seeds.json"))
        cls.unseen_before = w.server.box.unseen()
        cls.log = {}
        cls.log["baseline"] = w.mine("seen-audit").stdout
        cls.log["survey"] = w.mine("--seeds", "seeds.json", "survey")
        cls.log["refused"] = w.mine("crawl", check=False)
        w.mine("approve-survey")
        w.server.state["drop_after"] = 8                       # server hangs up mid-run; crawl must reconnect and finish
        cls.log["run"] = w.mine("run")
        cls.log["audit"] = w.mine("seen-audit").stdout
        cls.unseen_after = w.server.box.unseen()

    # ---------------- survey + gate
    def test_survey_outputs_and_gate(self):
        w = self.w
        for f in ("survey-report.md", "survey.json", "mining-plan.json"): self.assertTrue(os.path.exists(w.path(f)), f)
        r = self.log["refused"]; self.assertNotEqual(r.returncode, 0); self.assertIn("approve-survey", r.stderr + r.stdout)

    def test_survey_report_has_no_cost_or_time_estimate(self):
        rep = load(self.w.path("survey-report.md"))
        tail = rep[rep.index("## 9."):]
        self.assertFalse(re.search(r"\d+\s*(hours?|hrs?|minutes?|mins?|days?)\b|\$\s?\d|\bUSD\b|tokens?\b", tail, re.I), tail[:400])
        self.assertNotIn("elapsed", rep.lower())

    def test_survey_folder_quirks(self):
        S = load(self.w.path("survey.json"))
        names = {f["display"]: f for f in S["folders"]}
        self.assertNotIn("Clients", names)                                      # \Noselect parent skipped
        self.assertIn("Clients/Müller Studio", names)                           # modified UTF-7 name sent as LIST literal
        arch = names["Archive/Sets and Props Unlimited (imported)"]
        self.assertTrue(arch["imported_archive"]); self.assertEqual(arch["date_basis"], "SENTSINCE")
        self.assertEqual(arch["first_date_hdr"], 1998)
        self.assertIsNotNone(names["INBOX"]["unseen"])
        self.assertIn("Annual Rookie of the Year Trophy", S["seeds"])          # list-format seeds file accepted by survey
        self.assertGreater(S["seeds"]["Annual Rookie of the Year Trophy"]["header_hits"], 0)
        P = load(self.w.path("mining-plan.json"))
        acts = {f["display"]: (f["action"], f["priority"]) for f in P["folders"]}
        self.assertEqual(acts["Sent Messages"], ("deep", 1)); self.assertEqual(acts["Junk"][0], "skip"); self.assertEqual(acts["Drafts"][0], "skip")

    # ---------------- crawl / read-only
    def test_crawl_complete_despite_disconnect(self):
        box = self.w.server.box
        expected = sum(len(v) for k, v in box.f.items() if k not in ("Junk", "Trash", "Drafts"))
        n, u = self.w.sql("select count(*), count(distinct folder||'/'||uid) from messages ")[0]
        self.assertEqual(n, u)
        self.assertEqual(self.w.sql("select count(*) from messages where folder not in ('Junk','Trash','Drafts')")[0][0], expected)
        self.assertTrue(any(l.startswith("INJECTED DISCONNECT") for l in self.w.server.commands()))

    def test_wire_log_is_read_only(self):
        cmds = self.w.server.commands()
        self.assertEqual(read_only_violations(cmds), []); self.assertEqual(self.w.server.state["violations"], [])
        verbs = {l.split(" ")[1].upper() for l in cmds if not l.startswith("INJECTED") and " " in l}
        self.assertNotIn("SELECT", verbs)
        self.assertTrue(any("BODY.PEEK[" in l and "HEADER.FIELDS" not in l for l in cmds), "body fetches happened (via PEEK)")

    def test_no_flag_changed(self):
        self.assertEqual(self.unseen_before, self.unseen_after)
        self.assertIn("RESULT: no message changed from unread to read", self.log["audit"])
        self.assertIn("baseline saved", self.log["baseline"])

    def test_secret_never_written(self):
        pw = ENV["ICLOUD_APP_PASSWORD"].encode()
        for root, _, files in os.walk(self.w.out):
            for f in files:
                with open(os.path.join(root, f), "rb") as fh: self.assertNotIn(pw, fh.read(), f)
        for r in self.log.values():
            if not isinstance(r, str): self.assertNotIn(pw.decode(), r.stdout + r.stderr)

    # ---------------- filter
    def test_filter_decisions(self):
        dec = dict((a, d) for a, d in self.w.sql("select from_addr, decision from messages where folder != 'Sent Messages' and folder != 'Sent Items' group by from_addr"))
        for noisy in ("news@designweekly.example", "no-reply@amazon-demo.example", "deals@homedepot-demo.example"): self.assertEqual(dec[noisy], "noise", noisy)
        self.assertEqual(dec["mailer-daemon@mail.example"], "noise")
        for human in ("maria.lopez@pepsi-demo.example", "charlie.ward@paramount-demo.example", "tom@reyesmetalworks.example"): self.assertIn(dec[human], ("keep", "seed"), human)
        self.assertEqual(dec["nina@bulkbuy-demo.example"], "keep")              # folder named 'Bulk orders' is not junk
        self.assertGreater(self.w.sql("select count(*) from messages where decision='dup'")[0][0], 0)
        self.assertEqual(self.w.sql("select count(*) from messages where folder in ('Junk','Trash','Drafts')")[0][0], 0)

    def test_bodies_only_for_survivors_and_decoded(self):
        bad = self.w.sql("select count(*) from bodies b join messages m on m.id=b.mid where m.decision != 'keep' and m.dreason != 'delivery failure notice'")[0][0]
        self.assertEqual(bad, 0)                                                # the only non-kept bodies are bounce notices (read by `bounces`)
        txt = self.w.sql("select b.text, m.subject, m.from_name from bodies b join messages m on m.id=b.mid where m.from_addr='rene@pariscreative.example' and m.ts < 946684800")[0]
        self.assertIn("décor du café", txt[0]); self.assertEqual(txt[1], "Devis décor - café set"); self.assertEqual(txt[2], "René Dupont")
        subj = self.w.sql("select subject from messages where from_addr='ko@studioko.example'")[0][0]
        self.assertNotIn("=?", subj)

    # ---------------- threading
    def test_threads(self):
        t = dict(self.w.sql("select subj, n from threads"))
        for y in range(2019, 2026): self.assertEqual(t[f"Rookie of the Year trophy {y}"], 7, y)
        self.assertEqual(t["Fall catalog set build"], 5); self.assertEqual(t["Coke spot set - bid"], 5)
        first = self.w.sql("select first_ts from threads where subj='Coke spot set - bid'")[0][0]
        self.assertEqual(__import__("time").gmtime(first).tm_year, 1998)       # imported archive dated by its Date header

    # ---------------- contacts
    def contacts(self): return {r[1]: dict(zip(CONTACTS, r)) for r in rows(self.w.path("contacts.csv"))[1:]}

    def test_contacts_are_unique_addresses(self):
        c = self.contacts()
        self.assertEqual(len(c), len(rows(self.w.path("contacts.csv"))) - 1)                     # one row per address
        self.assertEqual(c["maria.lopez@pepsi-demo.example"]["Name"], "Maria Lopez")
        self.assertEqual(c["maria.lopez@pepsi-demo.example"]["Name source"], "header")
        self.assertIn("dana.moore@brightline-demo.example", c); self.assertIn("dana.moore@halobev-demo.example", c)   # no merging: each address is its own row
        self.assertIn(c["rene@pariscreative.example"]["Name"], ("René Dupont", "Rene Dupont"))   # 1998 raw 8-bit header vs 2010 ASCII spelling: same address, one row
        self.assertIn("pat.kim@oldagency-demo.example", c)                                       # David wrote to them: a contact even though it bounced
        for noisy in ("news@designweekly.example", "no-reply@amazon-demo.example", "deals@homedepot-demo.example", "mailer-daemon@mail.example", "dcr.demo@mac.com"): self.assertNotIn(noisy, c)
        self.assertFalse(any(e.endswith("@spam.example") for e in c))                            # Junk folder never crawled
        m = c["maria.lopez@pepsi-demo.example"]
        self.assertEqual(m["Company"], "PepsiCo"); self.assertIn("Manager", m["Title"]); self.assertEqual(m["Phone"], "914-555-0188")
        self.assertEqual(m["First contact"][:4], "2019"); self.assertGreater(int(m["Messages from them"]), 10); self.assertGreater(int(m["Messages to them"]), 10)
        self.assertIn("Rookie of the Year trophy", m["Projects"])

    def test_name_from_signature_when_header_has_none(self):
        k = self.contacts()["kwells@wellsscenic.example"]
        self.assertEqual((k["Name"], k["Name source"]), ("Karen Wells", "signature"))
        self.assertEqual(k["Company"], "Wells Scenic"); self.assertEqual(k["Title"], "Charge Artist")

    def test_name_from_address_shape_as_last_resort(self):
        j = self.contacts()["jamie.rivera@riveraprops.example"]
        self.assertEqual((j["Name"], j["Name source"]), ("Jamie Rivera", "address"))

    def test_owner_other_addresses_found_by_survey_and_excluded(self):
        P = load(self.w.path("mining-plan.json"))
        self.assertEqual(P["self_addresses"], ["david@dcrstudio.example"])
        self.assertIn("Possibly your own other addresses", load(self.w.path("survey-report.md")))
        self.assertNotIn("david@dcrstudio.example", self.contacts())

    def test_clutter_is_not_a_contact(self):
        c = self.contacts()
        for ad in ("hello@westsidelumber.example", "unsubscribe@lists.westsidelumber.example", "leave-8f3a91c2@leave.e.shopdeals.example", "rewards@mail.acmebank.example"): self.assertNotIn(ad, c)
        self.assertFalse(any(e.endswith("@partyguests.example") for e in c))                     # other recipients of a mass email
        self.assertIn("paul@grantevents.example", c)                                              # ...but its sender wrote to David
        self.assertFalse(any("_at_" in e for e in c))
        self.assertEqual(c["kim.ng@ngstudio.example"]["Name"], "Kim Ng")                         # Hide-My-Email relay decoded to the real address
        co = {r[1]: r for r in rows(self.w.path("companies.csv"))[1:]}
        self.assertEqual(co["westsidelumber.example"][3], "hello@westsidelumber.example")        # shared inbox David wrote to: listed under its company
        self.assertEqual(co["westsidelumber.example"][2], "0")
        dec = dict(self.w.sql("select subject, decision from messages where from_addr='hello@westsidelumber.example'"))
        self.assertEqual(dec["Re: Plywood order for the gallery build"], "keep"); self.assertEqual(dec["Westside Lumber weekly picks #0"], "noise")
        self.assertEqual({r[0] for r in self.w.sql("select decision from messages where subject='unsubscribe'")}, {"noise"})

    def test_companies(self):
        co = {r[1]: r for r in rows(self.w.path("companies.csv"))[1:]}
        self.assertEqual(co["pepsi-demo.example"][0], "PepsiCo")
        self.assertNotIn("mac.com", co); self.assertNotIn("gmail.com", co)

    # ---------------- threads -> contacts -> projects
    def test_threads_link_to_contacts(self):
        th = {r[1]: r for r in rows(self.w.path("threads.csv"))[1:]}
        t = th["Rookie of the Year trophy 2021"]
        self.assertIn("maria.lopez@pepsi-demo.example", t[6]); self.assertIn("tom@reyesmetalworks.example", t[6])   # cc'd fabricator linked too
        self.assertEqual(t[4], "7"); self.assertTrue(t[8])
        self.assertEqual(th["Hi"][8], "")                                                        # a non-work thread is listed but not a project

    def test_projects(self):
        pr = {r[1]: dict(zip(PROJECTS, r)) for r in rows(self.w.path("projects.csv"))[1:]}
        roy = pr["Rookie of the Year trophy"]                                                    # one project across 2019-2025
        self.assertEqual(roy["Years"], "2019-2025"); self.assertEqual(roy["Threads"], "7"); self.assertEqual(roy["Messages"], "49")
        self.assertIn("maria.lopez@pepsi-demo.example", roy["Contacts"]); self.assertIn("PepsiCo", roy["Companies"]); self.assertTrue(roy["Why included"])
        self.assertIn("ROY trophy base fabrication quote", pr)                                        # Tom's yearly quote threads are their own project
        rooms = [n for n in pr if n.endswith("executive conference room")]
        self.assertEqual(len(rooms), 4, rooms)                                                   # same template subject, different films: not merged
        self.assertNotIn("Happy holidays", pr)                                                   # personal note: no project
        self.assertIn("Scenic paint quote for the showroom backdrop", pr)
        ids = [r[0] for r in rows(self.w.path("projects.csv"))[1:]]; self.assertEqual(len(ids), len(set(ids)))

    def test_output_contract(self):
        for f in OUTPUTS: self.assertTrue(os.path.exists(self.w.path(f)), f)
        for f, hdr in (("contacts.csv", CONTACTS), ("companies.csv", COMPANIES), ("threads.csv", THREADS), ("projects.csv", PROJECTS)): self.assertEqual(rows(self.w.path(f))[0], hdr, f)
        for gone in ("pipeline.csv", "reengagement.csv", "address-lookup.csv", "voice-notes.md", "dossiers"): self.assertFalse(os.path.exists(self.w.path(gone)), gone)
        for f in os.listdir(self.w.out):
            if f.endswith(".csv"):
                for r in rows(self.w.path(f)):
                    for c in r: self.assertNotIn("\n", c, f); self.assertLessEqual(len(c), 1000, f)

    def test_rerun_is_stable(self):
        before = {f: load(self.w.path(f)) for f in ("contacts.csv", "threads.csv", "projects.csv", "companies.csv")}
        for st in ("filter", "thread", "link", "projects", "export"): self.w.mine(st)
        for f, v in before.items(): self.assertEqual(load(self.w.path(f)), v, f)

    def test_status_reports_progress(self):
        out = self.w.mine("status").stdout
        self.assertIn("running: no", out); self.assertIn("crawl", out); self.assertIn("approved:", out)


if __name__ == "__main__": unittest.main()
