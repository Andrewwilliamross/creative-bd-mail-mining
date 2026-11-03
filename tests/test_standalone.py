"""Production contract: one standalone stdlib-only file (dcrmine.py), Python 3.9+, run from any directory."""
import ast, os, py_compile, shutil, subprocess, sys, tempfile, unittest
from helpers import ENV, ROOT

STDLIB_39 = set("argparse ast base64 codecs collections contextlib csv datetime email fcntl hashlib html http imaplib io json os quopri re shutil socket sqlite3 ssl "
                "subprocess sys tempfile threading time unicodedata".split())
COMMANDS = ["survey", "approve-survey", "crawl", "filter", "fetch", "thread", "link", "projects", "export", "run", "status", "seen-audit"]


class Standalone(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="dcr-install-"); self.addCleanup(shutil.rmtree, self.d, True)
        for f in ("dcrmine.py",): shutil.copy(os.path.join(ROOT, f), self.d)
        self.elsewhere = tempfile.mkdtemp(prefix="dcr-cwd-"); self.addCleanup(shutil.rmtree, self.elsewhere, True)

    def run_iso(self, *args):
        return subprocess.run([sys.executable, "-I"] + list(args), cwd=self.elsewhere, env=ENV, capture_output=True, text=True, timeout=60)

    def test_compiles(self):
        for f in ("dcrmine.py",): py_compile.compile(os.path.join(self.d, f), doraise=True)

    def test_runs_isolated_from_another_directory(self):
        r = self.run_iso(os.path.join(self.d, "dcrmine.py"), "--version"); self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("dcrmine 2.", r.stdout + r.stderr)
        r = self.run_iso(os.path.join(self.d, "dcrmine.py"), "--help"); self.assertEqual(r.returncode, 0, r.stderr)
        for c in COMMANDS: self.assertIn(c, r.stdout)
        for flag in ("--since", "--before", "--limit", "--db", "--out", "--seeds", "--dry-run"): self.assertIn(flag, r.stdout)
        db = os.path.join(self.elsewhere, "x.sqlite")
        r = self.run_iso(os.path.join(self.d, "dcrmine.py"), "--db", db, "status"); self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("start with `survey`", r.stdout)

    def test_standard_library_only(self):
        mods = set()
        for f in ("dcrmine.py",):
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh: tree = ast.parse(fh.read())
            for n in ast.walk(tree):
                if isinstance(n, ast.Import): mods |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.module: mods.add(n.module.split(".")[0])
        std = getattr(sys, "stdlib_module_names", STDLIB_39)
        self.assertEqual(sorted(mods - set(std)), [])

    def test_no_secret_env_values_in_source(self):
        for f in ("dcrmine.py",):
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh: s = fh.read()
            self.assertNotRegex(s, r"(?i)password\s*=\s*['\"][^'\"]{4,}")


if __name__ == "__main__": unittest.main()
