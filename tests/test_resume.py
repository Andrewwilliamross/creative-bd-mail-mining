"""Resumability: SIGKILL mid-crawl, concurrent re-run, servers that drop or refuse mid-FETCH, UIDVALIDITY changes, bounded runs."""
import os, signal, time, unittest
from helpers import Workdir


def expected_total(box): return sum(len(v) for k, v in box.f.items() if k not in ("Junk", "Trash", "Drafts"))


class Resume(unittest.TestCase):
    def setUp(self):
        self.w = Workdir(self); self.w.approve_without_survey()

    def count(self): return self.w.sql("select count(*), count(distinct folder||'/'||uid) from messages")[0]

    def test_sigkill_mid_crawl_then_resume(self):
        w = self.w; w.server.state["fetch_delay"] = 0.12
        p = w.popen("--batch", "5", "--delay", "0", "crawl")
        try:
            deadline = time.time() + 60
            while time.time() < deadline:
                n = w.sql("select count(*) from messages")[0][0] if os.path.exists(w.db) else 0
                if n >= 25: break
                time.sleep(0.1)
            # a second copy (the agent re-running the command while nohup is still going) must refuse, not interleave
            r = w.mine("crawl", check=False); self.assertNotEqual(r.returncode, 0); self.assertIn("already working on this database", r.stderr + r.stdout)
            st = w.mine("status").stdout; self.assertIn("running: pid", st)             # status works during a crawl (WAL)
        finally:
            p.send_signal(signal.SIGKILL); p.wait(timeout=10); p.stdout.close(); p.stderr.close()
        n, u = self.count(); total = expected_total(w.server.box)
        self.assertTrue(0 < n < total, (n, total)); self.assertEqual(n, u)
        before = dict(((f, uid), i) for i, f, uid in w.sql("select id, folder, uid from messages"))
        w.server.state["fetch_delay"] = 0
        dry = w.mine("--dry-run", "crawl").stdout; self.assertIn("[dry-run]", dry)
        self.assertEqual(self.count()[0], n)                                            # dry-run stores nothing
        w.mine("crawl")
        n2, u2 = self.count(); self.assertEqual((n2, u2), (total, total))
        after = dict(((f, uid), i) for i, f, uid in w.sql("select id, folder, uid from messages"))
        self.assertTrue(all(after[k] == v for k, v in before.items()), "row ids preserved across resume")
        r = w.mine("crawl"); self.assertIn("(0 new this run)", r.stdout)                # idempotent

    def test_disconnect_in_the_middle_of_a_fetch_response(self):
        w = self.w; w.server.state["drop_mid_at"] = 3
        r = w.mine("--batch", "20", "crawl")
        self.assertIn("IMAP link problem", r.stderr)
        self.assertTrue(any("MID-FETCH" in l for l in w.server.commands()))
        self.assertEqual(self.count(), (expected_total(w.server.box),) * 2)

    def test_server_refuses_one_message(self):
        w = self.w; w.server.state["no_uids"] = {("INBOX", 7)}
        w.mine("--batch", "20", "crawl")
        total = expected_total(w.server.box)
        self.assertEqual(self.count()[0], total - 1)
        self.assertEqual(w.sql("select folder, uid, tries from crawl_skips"), [("INBOX", 7, 1)])
        self.assertIn("would not return: 1", w.mine("status").stdout)
        w.server.state["no_uids"] = set()
        w.mine("crawl")
        self.assertEqual(self.count()[0], total); self.assertEqual(w.sql("select count(*) from crawl_skips")[0][0], 0)

    def test_uidvalidity_change_recrawls_folder(self):
        w = self.w; w.mine("crawl")
        ids = {i for (i,) in w.sql("select id from messages where folder='INBOX'")}
        w.server.state["uidvalidity"]["INBOX"] = 2
        r = w.mine("crawl"); self.assertIn("UIDVALIDITY changed", r.stderr)
        ids2 = {i for (i,) in w.sql("select id from messages where folder='INBOX'")}
        self.assertEqual(len(ids2), len(ids)); self.assertFalse(ids & ids2, "old rows replaced, nothing stale kept")
        self.assertEqual(w.sql("select uidvalidity from folders where name='INBOX'")[0][0], 2)
        self.assertEqual(self.count()[0], expected_total(w.server.box))

    def test_missing_uidvalidity_does_not_wipe(self):
        w = self.w; w.mine("crawl"); n = self.count()[0]
        w.server.state["omit_uidvalidity"] = True
        r = w.mine("crawl"); self.assertNotIn("UIDVALIDITY changed", r.stderr); self.assertIn("(0 new this run)", r.stdout)
        self.assertEqual(self.count()[0], n)

    def test_since_then_widen_and_limit(self):
        w = self.w
        w.mine("--since", "2024", "crawl")
        recent = self.count()[0]; self.assertGreater(recent, 0)
        old = w.sql("select count(*) from messages where folder != 'Archive/Sets and Props Unlimited (imported)' and ts < strftime('%s','2023-12-25')")[0][0]
        self.assertEqual(old, 0)
        w.mine("--limit", "10", "crawl")
        per = dict(w.sql("select folder, count(*) from messages group by folder"))
        self.assertLessEqual(per["INBOX"], recent + 10)
        for _ in range(40):
            if "(0 new this run)" in w.mine("--limit", "60", "crawl").stdout: break
        self.assertEqual(self.count()[0], expected_total(w.server.box))                 # widening a bounded run never loses older mail

    def test_fetch_resumes_and_never_stores_empty_on_failure(self):
        w = self.w; w.mine("crawl"); w.mine("filter")
        w.server.state["no_uids"] = {("INBOX", 2), ("INBOX", 3)}
        w.mine("fetch")
        missing = w.sql("select count(*) from messages m where m.folder='INBOX' and m.uid in (2,3) and m.decision='keep' and m.id in (select mid from bodies)")[0][0]
        self.assertEqual(missing, 0)
        w.server.state["no_uids"] = set(); r = w.mine("fetch")
        self.assertNotIn("could not be fetched", r.stdout)


if __name__ == "__main__": unittest.main()
