"""HTTP-level behaviour of the Supabase client.

The one thing here that is not plumbing: replacing a storage object. Storage
has no UPDATE policy on the replay buckets, only INSERT/SELECT/DELETE, so
`x-upsert` on an object that already exists is refused with "new row violates
row-level security policy" even for its owner. We replace the way the existing
policies do allow -- delete, then insert.
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from debrief_uploader import api
from debrief_uploader.api import ApiError, Client, Session

RLS = json.dumps({"statusCode": "403", "error": "Unauthorized",
                  "message": "new row violates row-level security policy"}).encode()


class FakeHttp:
    """Stands in for _request. `existing` names object paths already stored."""

    def __init__(self, existing=()):
        self.existing = set(existing)
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=60):
        path = url.split("/storage/v1/object/", 1)[-1]
        self.calls.append((method, path))
        if method == "DELETE":
            self.existing.discard(path)
            return 200, {}, b"{}"
        if method == "POST":
            if path in self.existing:
                return 403, {}, RLS
            self.existing.add(path)
            return 200, {}, b"{}"
        return 200, {}, b"{}"


class TestStorageReplace(unittest.TestCase):

    def setUp(self):
        self.client = Client(Session(access="a", refresh="r",
                                     expires_at=time.time() + 3600,
                                     user_id="uid"))
        self._real = api._request

    def tearDown(self):
        api._request = self._real

    def test_plain_insert_needs_no_delete(self):
        http = FakeHttp()
        api._request = http
        self.client.storage_upload("replays-data", "rid/shot1.jpg", b"x",
                                   "image/jpeg", upsert=True)
        self.assertEqual([m for m, _ in http.calls], ["POST"])

    def test_overwrite_deletes_then_inserts(self):
        http = FakeHttp(existing={"replays-data/rid/shot1.jpg"})
        api._request = http
        self.client.storage_upload("replays-data", "rid/shot1.jpg", b"x",
                                   "image/jpeg", upsert=True)
        self.assertEqual([m for m, _ in http.calls],
                         ["POST", "DELETE", "POST"],
                         "refused overwrite -> delete -> insert")
        self.assertIn("replays-data/rid/shot1.jpg", http.existing)

    def test_without_upsert_the_refusal_is_raised(self):
        http = FakeHttp(existing={"replays/uid/a.wowsreplay"})
        api._request = http
        with self.assertRaises(ApiError) as cm:
            self.client.storage_upload("replays", "uid/a.wowsreplay", b"x",
                                       "application/octet-stream", upsert=False)
        self.assertTrue(cm.exception.permission)
        self.assertEqual([m for m, _ in http.calls], ["POST"],
                         "a raw replay upload must never delete anything")

    def test_a_refusal_that_survives_the_delete_is_raised(self):
        class Stubborn(FakeHttp):
            def __call__(self, method, url, data=None, headers=None, timeout=60):
                path = url.split("/storage/v1/object/", 1)[-1]
                self.calls.append((method, path))
                if method == "DELETE":
                    return 403, {}, RLS      # not ours to delete either
                return 403, {}, RLS

        http = Stubborn()
        api._request = http
        with self.assertRaises(ApiError) as cm:
            self.client.storage_upload("replays-data", "rid/shot1.jpg", b"x",
                                       "image/jpeg", upsert=True)
        self.assertTrue(cm.exception.permission,
                        "callers branch on this to stop retrying")

    def test_permission_errors_are_flagged(self):
        for status in (401, 403):
            with self.assertRaises(ApiError) as cm:
                api._raise_for(status, RLS)
            self.assertTrue(cm.exception.permission)

    def test_a_server_error_is_not_a_permission_error(self):
        with self.assertRaises(ApiError) as cm:
            api._raise_for(500, b'{"message":"boom"}')
        self.assertFalse(cm.exception.permission)


class TestOverwriteDetection(unittest.TestCase):

    def test_rls_message_is_an_overwrite_refusal(self):
        self.assertTrue(api._is_overwrite_refusal(403, RLS))

    def test_conflict_is_an_overwrite_refusal(self):
        self.assertTrue(api._is_overwrite_refusal(409, b"{}"))

    def test_server_error_is_not(self):
        self.assertFalse(api._is_overwrite_refusal(500, b"boom"))

    def test_not_found_is_not(self):
        self.assertFalse(api._is_overwrite_refusal(404, b"{}"))


class TestOAuthLoopback(unittest.TestCase):
    """The redirect must land on a port the site's allowlist can name."""

    def test_prefers_the_fixed_ports(self):
        from debrief_uploader import oauth
        srv = oauth._listen()
        try:
            self.assertIn(srv.server_address[1], oauth.LOOPBACK_PORTS)
        finally:
            srv.server_close()

    def test_falls_through_when_a_port_is_taken(self):
        from debrief_uploader import oauth
        held = [oauth._listen()]
        try:
            nxt = oauth._listen()
            held.append(nxt)
            self.assertNotEqual(nxt.server_address[1], held[0].server_address[1])
        finally:
            for h in held:
                h.server_close()


class TestOAuthRoundTrip(unittest.TestCase):
    """The browser's reply must be exchanged for a session, promptly.

    Regression, 2026-10-05: the listener ran serve_forever() on a thread and
    handle_request() on the caller; the thread answered the browser while the
    caller waited out the full timeout, so the code was never exchanged.
    """

    def _run(self, paths, timeout=20):
        import threading
        import time
        import urllib.parse
        import urllib.request
        from debrief_uploader import oauth
        calls = []

        def fake_open(url):
            redirect = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_to"][0]
            base = redirect.rsplit("/", 1)[0]

            def hit():
                time.sleep(0.2)
                for pth in paths:
                    try:
                        urllib.request.urlopen(base + pth, timeout=5).read()
                    except Exception:
                        pass
            threading.Thread(target=hit, daemon=True).start()
            return True

        def fake_request(method, url, body, headers):
            calls.append(url)
            return 200, {}, json.dumps({"access_token": "a", "refresh_token": "r",
                                        "expires_in": 3600,
                                        "user": {"id": "u", "email": "e@x"}}).encode()

        class Client:
            key = "k"
            session = None

            def _absorb(self, j):
                self.session = j

        real = (oauth.webbrowser.open, oauth._request)
        oauth.webbrowser.open, oauth._request = fake_open, fake_request
        try:
            t0 = time.time()
            c = Client()
            oauth.sign_in_browser(c, "google", timeout=timeout)
            return time.time() - t0, calls, c
        finally:
            oauth.webbrowser.open, oauth._request = real

    def test_code_is_exchanged_without_waiting_out_the_timeout(self):
        took, calls, c = self._run(["/cb?code=AUTH"])
        self.assertLess(took, 5)
        self.assertEqual(len(calls), 1)
        self.assertIn("grant_type=pkce", calls[0])
        self.assertEqual(c.session["access_token"], "a")

    def test_a_stray_request_first_does_not_eat_the_reply(self):
        took, calls, _ = self._run(["/favicon.ico", "/cb?code=AUTH"])
        self.assertLess(took, 5)
        self.assertEqual(len(calls), 1)

    def test_an_error_reply_is_reported(self):
        from debrief_uploader.api import ApiError
        with self.assertRaises(ApiError) as cm:
            self._run(["/cb?error=access_denied&error_description=User+cancelled"])
        self.assertIn("User cancelled", cm.exception.message)

