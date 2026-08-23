"""
Unit tests for ingest.py's retry/backoff logic on live API calls. Simulates
network failures and HTTP error codes by monkeypatching urllib.request.urlopen
directly -- no real network call in these tests, but no mocked business
logic either: the actual _live_get retry loop runs for real against a fake
transport, only the transport is substituted.
"""
import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

import ingest  # noqa: E402


def http_error(code, body=b'{"error": "boom"}'):
    return urllib.error.HTTPError(
        url="https://api.razorpay.com/v1/x", code=code, msg="error",
        hdrs=None, fp=io.BytesIO(body),
    )


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestRetryBehavior(unittest.TestCase):
    def test_succeeds_immediately_with_no_retry_needed(self):
        with patch("ingest.urllib.request.urlopen", return_value=FakeResponse(b'{"items": []}')) as mock_open:
            result = ingest._live_get("x", "key", "secret", sleep=lambda s: None)
        self.assertEqual(result, {"items": []})
        self.assertEqual(mock_open.call_count, 1)

    def test_retries_on_server_error_then_succeeds(self):
        calls = [http_error(503), http_error(503), FakeResponse(b'{"items": [1]}')]

        def fake_urlopen(req, timeout):
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        sleeps = []
        with patch("ingest.urllib.request.urlopen", side_effect=fake_urlopen):
            result = ingest._live_get("x", "key", "secret", sleep=sleeps.append)
        self.assertEqual(result, {"items": [1]})
        self.assertEqual(len(sleeps), 2)  # slept before the 2nd and 3rd attempts
        self.assertEqual(sleeps, [1, 2])  # exponential backoff: 1s, then 2s

    def test_exhausts_retries_and_raises(self):
        with patch("ingest.urllib.request.urlopen", side_effect=http_error(500)):
            with self.assertRaises(RuntimeError):
                ingest._live_get("x", "key", "secret", max_retries=2, sleep=lambda s: None)

    def test_client_error_fails_immediately_no_retry(self):
        with patch("ingest.urllib.request.urlopen", side_effect=http_error(401)) as mock_open:
            with self.assertRaises(RuntimeError):
                ingest._live_get("x", "key", "secret", sleep=lambda s: (_ for _ in ()).throw(AssertionError("should not sleep/retry on 401")))
        self.assertEqual(mock_open.call_count, 1)

    def test_network_error_is_retried(self):
        calls = [urllib.error.URLError("connection refused"), FakeResponse(b'{"items": []}')]

        def fake_urlopen(req, timeout):
            result = calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        with patch("ingest.urllib.request.urlopen", side_effect=fake_urlopen):
            result = ingest._live_get("x", "key", "secret", sleep=lambda s: None)
        self.assertEqual(result, {"items": []})


if __name__ == "__main__":
    unittest.main()
