"""Browser sign-in (PKCE with a loopback redirect).

The app never sees a password: it opens the system browser, the user signs in
with the same account they use on the site, and the authorization code comes
back to a listener on 127.0.0.1.

REQUIRES the loopback redirect in the site's Auth allowlist (Supabase ->
Authentication -> URL Configuration -> Redirect URLs): http://127.0.0.1:*/cb,
or the exact LOOPBACK_PORTS below. The listener takes the first free one of
those ports so an exact allowlist works too; a random port is only the last
resort. Without the entry Supabase sends the browser to the site instead, and
this raises a clear error after the timeout rather than hanging.
"""
import base64
import hashlib
import http.server
import json
import os
import threading
import urllib.parse
import webbrowser

from . import config
from .api import ApiError, _json, _request

PROVIDERS = ("google", "discord")
LOOPBACK_PORTS = (53682, 53683, 53684)


def _listen():
    for port in LOOPBACK_PORTS + (0,):
        try:
            return http.server.HTTPServer(("127.0.0.1", port), _Handler)
        except OSError:
            continue
    raise ApiError("oauth", "could not open a local port for the sign-in reply")


def _verifier():
    return base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")[:96]


def _challenge(verifier):
    d = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


class _Handler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Handler.code = (q.get("code") or [None])[0]
        _Handler.error = (q.get("error_description") or q.get("error") or [None])[0]
        body = (b"<!doctype html><meta charset=utf-8>"
                b"<title>GamingDiver</title>"
                b"<body style='font-family:system-ui;background:#0b1620;color:#e4eef4;"
                b"display:grid;place-items:center;height:100vh;margin:0'>"
                b"<div style='text-align:center'><h1 style='font-weight:600'>Signed in</h1>"
                b"<p style='color:#8aa2b1'>You can close this tab and go back to "
                b"the uploader.</p></div>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def sign_in_browser(client, provider="discord", timeout=300):
    if provider not in PROVIDERS:
        raise ApiError("bad_provider", "provider must be one of: %s"
                       % ", ".join(PROVIDERS))
    _Handler.code = _Handler.error = None
    srv = _listen()
    port = srv.server_address[1]
    redirect = "http://127.0.0.1:%d/cb" % port

    verifier = _verifier()
    params = {
        "provider": provider,
        "redirect_to": redirect,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "s256",
    }
    url = "%s/auth/v1/authorize?%s" % (config.SUPABASE_URL,
                                       urllib.parse.urlencode(params))

    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    print("Opening your browser to sign in with %s..." % provider.title())
    print("If it doesn't open, paste this into your browser:\n  %s\n" % url)
    webbrowser.open(url)

    srv.timeout = timeout
    srv.handle_request()          # blocks until the redirect arrives (or times out)
    srv.shutdown()

    if _Handler.error:
        raise ApiError("oauth", _Handler.error)
    if not _Handler.code:
        raise ApiError("oauth",
                       "no sign-in came back from the browser. If it landed on "
                       "gamingdiver.com instead of a 'Signed in' page, the "
                       "site does not allow this app's sign-in redirect yet; "
                       "email + password still works meanwhile.")

    body = json.dumps({"auth_code": _Handler.code,
                       "code_verifier": verifier}).encode()
    st, _, b = _request("POST", config.SUPABASE_URL + "/auth/v1/token?grant_type=pkce",
                        body, {"apikey": client.key,
                               "Content-Type": "application/json"})
    if st >= 400:
        j = _json(b) or {}
        raise ApiError("oauth", j.get("error_description") or j.get("msg")
                       or "sign-in could not be completed")
    client._absorb(_json(b))
    return client.session
