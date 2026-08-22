"""
Minimal stdlib .env loader. Reads KEY=VALUE lines from the repo-root .env
file into os.environ without overwriting a variable already set in the
real environment. Never prints or logs credential values.
"""
import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv():
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def get_razorpay_credentials():
    """Returns (key_id, key_secret). Raises a clear, specific error if
    either is missing -- never a generic KeyError, and never prints the
    value of whichever one IS set."""
    load_dotenv()
    key_id = os.environ.get("RAZORPAY_KEY_ID", "").strip()
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()

    if not key_id or not key_secret:
        missing = []
        if not key_id:
            missing.append("RAZORPAY_KEY_ID")
        if not key_secret:
            missing.append("RAZORPAY_KEY_SECRET")
        raise RuntimeError(
            f"Missing {', '.join(missing)} -- set them in the repo-root "
            f".env file (never pass them on the command line or commit "
            f"them; .env is already in .gitignore)."
        )
    return key_id, key_secret
