"""Shared test plumbing. Tests run from a fresh clone: the production file lives in the repo root, fixtures in tests/."""
import contextlib, os, shutil, sqlite3, subprocess, sys, tempfile, threading

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
for p in (ROOT, TESTS):
    if p not in sys.path: sys.path.insert(0, p)
import dcrmine  # noqa: E402
import fake_imap  # noqa: E402

USER = "dcr.demo@mac.com"
ENV = dict(os.environ, DCR_IMAP_USER=USER, ICLOUD_APP_PASSWORD="fixture-only-password", PYTHONDONTWRITEBYTECODE="1")
ENV.pop("DCR_SELF", None)


class FakeServer:
    """fake_imap on an ephemeral port in a daemon thread; .log is the wire log, .state the knobs (drop, NO, uidvalidity...)."""
    def __init__(self, tmp):
        self.log = os.path.join(tmp, "imap-commands.log")
        self.srv = fake_imap.serve(0, None, self.log, quiet=True)
        self.port = self.srv.server_address[1]
        self.state = fake_imap.STATE
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True); self.t.start()
    @property
    def box(self): return fake_imap.BOX
    def close(self): self.srv.shutdown(); self.srv.server_close()
    def commands(self):
        with open(self.log) as fh: return [l.rstrip("\n") for l in fh if l.strip()]


class Workdir:
    """temp out/ + db, fake server, and a CLI runner that invokes the repo-root files exactly as production does (python3 dcrmine.py ...)."""
    def __init__(self, test):
        """test: a TestCase instance (per-test) or class (shared by the class, cleaned up after it)"""
        cleanup = test.addClassCleanup if isinstance(test, type) else test.addCleanup
        self.tmp = tempfile.mkdtemp(prefix="dcrtest-"); cleanup(shutil.rmtree, self.tmp, True)
        self.out = os.path.join(self.tmp, "out"); self.db = os.path.join(self.out, "mail.sqlite")
        self.server = FakeServer(self.tmp); cleanup(self.server.close)

    def conn_args(self):
        return ["--plain", "--host", "127.0.0.1", "--port", str(self.server.port), "--db", self.db, "--out", self.out, "--timeout", "10"]

    def mine(self, *args, check=True, env=None, timeout=240):
        cmd = [sys.executable, os.path.join(ROOT, "dcrmine.py")] + self.conn_args() + list(args)
        r = subprocess.run(cmd, cwd=self.tmp, env=env or ENV, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode != 0: raise AssertionError(f"{' '.join(args)} failed rc={r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")
        return r

    def popen(self, *args, env=None):
        cmd = [sys.executable, os.path.join(ROOT, "dcrmine.py")] + self.conn_args() + list(args)
        return subprocess.Popen(cmd, cwd=self.tmp, env=env or ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def sql(self, q, *p):
        db = sqlite3.connect(self.db, timeout=30)
        try: return db.execute(q, p).fetchall()
        finally: db.close()

    def approve_without_survey(self):
        """for tests that start at crawl: mark the survey gate passed (the gate itself is tested in test_survey)"""
        os.makedirs(self.out, exist_ok=True)
        db = dcrmine.opendb(self.db); dcrmine.meta_set(db, "survey_done", "test"); dcrmine.meta_set(db, "survey_approved", "test"); db.close()

    def path(self, *p): return os.path.join(self.out, *p)


def read_only_violations(commands):
    """every command the fake server received that is not on the read-only allow-list, plus any non-PEEK body fetch"""
    import re
    allowed = {"CAPABILITY", "LOGIN", "LIST", "STATUS", "EXAMINE", "UID FETCH", "UID SEARCH", "LOGOUT", "NOOP"}
    bad = []
    for l in commands:
        if l.startswith("INJECTED"): continue
        if l.startswith("VIOLATION"): bad.append(l); continue
        parts = l.split(" ", 2); cmd = parts[1].upper() if len(parts) > 1 else ""
        if cmd == "UID" and len(parts) > 2: cmd = "UID " + parts[2].split(" ", 1)[0].upper()
        if cmd not in allowed: bad.append(l)
        if cmd == "UID FETCH" and re.search(r"BODY\[|BINARY\[|RFC822(?!\.SIZE)", l.upper().replace("BODY.PEEK[", "PEEK(").replace("BINARY.PEEK[", "PEEK(")): bad.append("non-PEEK: " + l)
    return bad


def args_for(**kw):
    """argparse Namespace like the CLI builds, for in-process calls"""
    a = dcrmine.build_parser().parse_args(["status"])
    for k, v in kw.items(): setattr(a, k, v)
    return a


@contextlib.contextmanager
def quiet_stdio():
    import io
    o, e = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try: yield
    finally: sys.stdout, sys.stderr = o, e
