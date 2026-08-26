"""
Vercel entry point for the review site demo. Reuses review_server.py's
actual Handler class, render_* functions, and HTML/CSS unchanged -- the
only substitution is the persistence layer: db_pg.py (Postgres, via
Neon's DATABASE_URL) stands in for db.py (local SQLite) because a
serverless function has no writable disk that survives between
invocations.

The substitution happens by planting db_pg into sys.modules["db"] BEFORE
review_server or settlement_qa ever run their own `import db` -- so
neither of those files needed a single line changed to run here.

vercel.json rewrites every path to this one function (it's the only
route), but Vercel's rewrite now hands the handler the REWRITTEN
destination path ("/api/index"), not the path the browser actually
requested -- confirmed directly against review_server.py's do_GET, which
dispatches on self.path and would 404 everything without this fix. The
rewrite smuggles the real path through as a `__path` query param instead;
_fix_path() restores it onto self.path before review_server's own do_GET
ever runs, so review_server.py needed no changes to know the difference.
"""
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import db_pg  # noqa: E402
sys.modules["db"] = db_pg

from review_server import Handler


class handler(Handler):
    def _fix_path(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        real_path = qs.pop("__path", ["/"])[0]
        if qs:
            self.path = f"{real_path}?{urlencode(qs, doseq=True)}"
        else:
            self.path = real_path

    def do_GET(self):
        self._fix_path()
        super().do_GET()

    def do_POST(self):
        self._fix_path()
        super().do_POST()
