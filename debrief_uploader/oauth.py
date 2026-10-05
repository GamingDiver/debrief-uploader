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
import html
import http.server
import json
import os
import time
import urllib.parse
import webbrowser

from . import config
from .api import ApiError, _json, _request

PROVIDERS = ("google", "discord")
LOOPBACK_PORTS = (53682, 53683, 53684)


class _Server(http.server.HTTPServer):
    # HTTPServer turns SO_REUSEADDR on, and on Windows that lets a second
    # socket bind a port someone else already holds -- the sign-in reply
    # could then reach the other listener. Exclusive use, so a taken port
    # fails and _listen moves on to the next one.
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _listen():
    for port in LOOPBACK_PORTS + (0,):
        try:
            return _Server(("127.0.0.1", port), _Handler)
        except OSError:
            continue
    raise ApiError("oauth", "could not open a local port for the sign-in reply")


def _verifier():
    return base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")[:96]


def _challenge(verifier):
    d = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def _page(title, line):
    return ("<!doctype html><meta charset=utf-8><title>GamingDiver</title>"
            "<body style='font-family:system-ui;background:#0b1620;color:#e4eef4;"
            "display:grid;place-items:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h1 style='font-weight:600'>%s</h1>"
            "<p style='color:#8aa2b1'>%s</p></div>" % (title, line)).encode()


class _Handler(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/cb":
            # the browser's own extras (favicon.ico): not the sign-in reply
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        q = urllib.parse.parse_qs(u.query)
        _Handler.code = (q.get("code") or [None])[0]
        _Handler.error = (q.get("error_description") or q.get("error") or [None])[0]
        if _Handler.code:
            body = _page("Signed in", "You can close this tab and go back to the uploader.")
        else:
            body = _page("Sign-in did not finish",
                         "Go back to the uploader and try again. %s"
                         % (html.escape(_Handler.error) if _Handler.error else ""))
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

    print("Opening your browser to sign in with %s..." % provider.title())
    print("If it doesn't open, paste this into your browser:\n  %s\n" % url)
    webbrowser.open(url)

    # ONE loop on the listener. It used to run serve_forever() on a thread AND
    # handle_request() here: the thread answered the browser ("Signed in")
    # while this call kept waiting up to `timeout` for a second request, so
    # the code was never exchanged (tester 2026-10-05; Supabase logged the
    # Google logins but no grant_type=pkce call).
    deadline = time.time() + timeout
    try:
        while _Handler.code is None and _Handler.error is None:
            left = deadline - time.time()
            if left <= 0:
                break
            srv.timeout = min(left, 1.0)
            srv.handle_request()
    finally:
        srv.server_close()

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
