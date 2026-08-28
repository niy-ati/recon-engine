"""
Render entry point for the review site -- the counterpart to
api/index.py's Vercel entry point, for the one thing Vercel's serverless
functions structurally can't do: install a real system binary. This is
what lets document_qa.py's photo/screenshot OCR actually work (Tesseract),
not just degrade to the honest "OCR isn't available" fallback.

Same substitution api/index.py uses -- plant db_pg (Postgres, via
DATABASE_URL) into sys.modules["db"] before review_server.py or
settlement_qa.py ever run their own `import db` -- so this deployment
reads and writes the exact same live data as the Vercel one, not a
separate, drifted copy. Point this at the same Neon DATABASE_URL the
Vercel project already uses.

Unlike api/index.py, this runs as a real, persistent process (Render's
free Web Service, not a serverless function), so it uses
review_server.py's own ThreadingHTTPServer directly instead of the
per-request rewrite trick api/index.py needs to work around Vercel's
routing. Binds 0.0.0.0 on Render's assigned $PORT, not localhost:8000 --
Render routes external traffic to whatever port this actually listens on,
supplied via the PORT environment variable it sets itself.
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(REPO_ROOT / "api"))

import db_pg  # noqa: E402
sys.modules["db"] = db_pg

from http.server import ThreadingHTTPServer  # noqa: E402
from review_server import Handler  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving on 0.0.0.0:{port} (Postgres-backed, OCR-capable)")
    server.serve_forever()
