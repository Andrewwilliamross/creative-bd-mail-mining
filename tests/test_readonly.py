"""IMAP command audit: fails if any non-read-only command or a non-PEEK body fetch could ever be issued."""
import ast, os, re, unittest
from helpers import ROOT, Workdir, args_for, dcrmine as D, read_only_violations

FORBIDDEN = [("SELECT", ('"INBOX"',)), ("STORE", ("1", "+FLAGS", "(\\Seen)")), ("COPY", ("1", "Archive")), ("MOVE", ("1", "Archive")), ("APPEND", ("INBOX", None, None, b"x")),
             ("EXPUNGE", ()), ("DELETE", ("INBOX",)), ("CREATE", ("X",)), ("RENAME", ("A", "B")), ("SUBSCRIBE", ("A",)), ("UNSUBSCRIBE", ("A",)), ("CLOSE", ()),
             ("SETACL", ("A", "b", "c")), ("FETCH", ("1", "(BODY.PEEK[])")), ("SEARCH", ("ALL",)), ("IDLE", ()), ("AUTHENTICATE", ("XOAUTH2",)), ("ENABLE", ("UTF8=ACCEPT",)),
             ("UID", ("STORE", "1", "+FLAGS", "(\\Seen)")), ("UID", ("COPY", "1", "X")), ("UID", ("MOVE", "1", "X")), ("UID", ("EXPUNGE", "1")),
             ("UID", ("FETCH", "1", "(BODY[])")), ("UID", ("FETCH", "1", "(BODY[1])")), ("UID", ("FETCH", "1", "(BODY[HEADER])")), ("UID", ("FETCH", "1", "(RFC822)")),
             ("UID", ("FETCH", "1", "(RFC822.TEXT)")), ("UID", ("FETCH", "1", "(RFC822.HEADER)")), ("UID", ("FETCH", "1", "(BINARY[1])")), ("UID", ("FETCH", "1:*", "(UID body[text])"))]
ALLOWED = [("CAPABILITY", ()), ("LOGIN", ("u", "p")), ("LIST", ('""', "*")), ("STATUS", ('"INBOX"', "(MESSAGES)")), ("EXAMINE", ('"INBOX"',)), ("NOOP", ()), ("LOGOUT", ()),
           ("UID", ("SEARCH", "ALL")), ("UID", ("FETCH", "1:5", "(UID RFC822.SIZE INTERNALDATE FLAGS BODYSTRUCTURE)")), ("UID", ("FETCH", "1", "(UID BODY.PEEK[1.2]<0.60000>)")),
           ("UID", ("FETCH", "1", f"(UID {D.HDR_ITEMS})")), ("UID", ("FETCH", "1", "(BINARY.PEEK[1])"))]


class Guard(unittest.TestCase):
    def test_forbidden_commands_raise(self):
        for name, args in FORBIDDEN:
            with self.subTest(name=name, args=args), self.assertRaises(D.ReadOnlyViolation): D.check_command(name, args)

    def test_allowed_commands_pass(self):
        for name, args in ALLOWED:
            with self.subTest(name=name, args=args): D.check_command(name, args)

    def test_bytes_args_are_checked_too(self):
        with self.assertRaises(D.ReadOnlyViolation): D.check_command("UID", (b"FETCH", b"1", b"(BODY[])"))


class LiveGuard(unittest.TestCase):
    """drive the real imaplib-based connection against the fake server: every mutating call must die before reaching the wire"""
    def setUp(self):
        self.w = Workdir(self)
        os.environ.setdefault("ICLOUD_APP_PASSWORD", "x")
        self.c = D.Conn(args_for(user="dcr.demo@mac.com", plain=True, host="127.0.0.1", port=self.w.server.port, timeout=10))
        self.addCleanup(lambda: self.c.m and self.c.m.shutdown())
        self.c.select("INBOX")
        self.unseen = self.w.server.box.unseen()

    def test_mutations_never_reach_the_server(self):
        m = self.c.m
        attempts = [lambda: m.select("INBOX"), lambda: m.store("1", "+FLAGS", "\\Seen"), lambda: m.uid("STORE", "1", "+FLAGS", "(\\Seen)"), lambda: m.copy("1", "Archive"),
                    lambda: m.uid("COPY", "1", "Archive"), lambda: m.uid("MOVE", "1", "Archive"), lambda: m.expunge(), lambda: m.append("INBOX", None, None, b"From: x\r\n\r\nx"),
                    lambda: m.delete("Junk"), lambda: m.create("New"), lambda: m.rename("Junk", "J2"), lambda: m.subscribe("Junk"),
                    lambda: m.fetch("1", "(RFC822)"), lambda: m.uid("FETCH", "1", "(BODY[])"), lambda: m.uid("FETCH", "1", "(RFC822.TEXT)"), lambda: m.setacl("INBOX", "a", "lr")]
        for fn in attempts:
            with self.assertRaises(D.ReadOnlyViolation): fn()
        with self.assertRaises(D.ReadOnlyViolation): self.c.fetch([1, 2], "(UID BODY[1])")
        self.assertEqual(self.c.search("ALL")[:3], [1, 2, 3])                    # the connection is still usable
        self.assertTrue(self.c.fetch([1], "(UID BODY.PEEK[1]<0.100>)"))
        with self.assertRaises(D.ReadOnlyViolation): m.close()                  # last: imaplib flips its client-side state to AUTH even when CLOSE is refused
        cmds = self.w.server.commands()
        self.assertEqual(read_only_violations(cmds), []); self.assertEqual(self.w.server.state["violations"], [])
        self.assertEqual(self.w.server.box.unseen(), self.unseen)

    def test_fake_server_would_catch_a_non_peek_fetch(self):
        """sanity check of the audit itself: bypassing the guard, a BODY[] fetch is logged as a violation and flips \\Seen"""
        m = self.c.m; u = self.unseen["INBOX"][0]
        typ, _ = __import__("imaplib").IMAP4._command_complete(m, "UID", super(D._Guard, m)._command("UID", "FETCH", str(u), "(BODY[])"))
        self.assertTrue(read_only_violations(self.w.server.commands()))
        self.assertNotIn(u, self.w.server.box.unseen()["INBOX"])


class SourceScan(unittest.TestCase):
    """static audit of the production file"""
    def src(self, f):
        with open(os.path.join(ROOT, f), encoding="utf-8") as fh: return fh.read()

    def test_no_mutating_imap_or_mail_sending_calls(self):
        for f in ("dcrmine.py",):
            s = self.src(f)
            self.assertFalse(re.search(r"\.(store|copy|move|expunge|setacl|deleteacl|subscribe|unsubscribe|rename|create|setannotation|xatom)\(", s), f)
            self.assertNotRegex(s, r"\bsmtplib\b|\bsendmail\b|\bpoplib\b")
            for line in s.splitlines():
                if re.search(r"\bm\.select\(", line): self.assertIn("readonly=True", line, f"{f}: raw imaplib select without readonly: {line.strip()}")
            for m in re.finditer(r"""["'](\([^"']*BODY\[[^"']*)["']""", s): self.fail(f"{f}: non-PEEK fetch literal {m.group(1)}")
            for node in ast.walk(ast.parse(s)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "uid" and node.args and isinstance(node.args[0], ast.Constant):
                    self.assertIn(node.args[0].value.upper(), ("FETCH", "SEARCH"), f"{f}: m.uid({node.args[0].value!r})")

    def test_connections_go_through_the_guard(self):
        s = self.src("dcrmine.py")
        self.assertNotRegex(s, r"imaplib\.IMAP4(_SSL)?\(")                     # only GuardedIMAP4 / GuardedIMAP4_SSL are instantiated
        self.assertIn("GuardedIMAP4_SSL(a.host", s)


if __name__ == "__main__": unittest.main()
