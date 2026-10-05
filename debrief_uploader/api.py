"""Supabase client: auth, the replay-meta edge function, storage, RPC.

urllib only -- no requests, no supabase-py. The whole surface is six HTTP calls
and keeping the dependency list to Pillow makes the PyInstaller build small and
the install on a gaming PC boring.

The call ORDER in upload_replay() is not stylistic: the replays-data storage
policy requires the replays row to exist before any screenshot can be written
under <replay_id>/.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class ApiError(Exception):
    """Something the caller may want to branch on."""

    def __init__(self, code, message, status=None, terminal=False):
        super().__init__("%s: %s" % (code, message) if code else message)
        self.code = code or ""
        self.message = message or ""
        self.status = status
        self.terminal = terminal
        self.permission = False


# Errors where retrying is pointless and the user must do something.
TERMINAL_CODES = {
    "feature_supporter_early_access",
    "feature_admin_only",
    "replay_quota_exceeded",
    "not_authenticated",
    "bad_magic",
    "bad_file",
    "bad_metadata",
    "decode_failed",
}

USER_MESSAGES = {
    "feature_supporter_early_access":
        "Debrief upload is in supporter early access. Supporters can upload now.",
    "feature_admin_only":
        "Debrief upload is not open to this account yet.",
    "replay_quota_exceeded":
        "This account has used its replay upload allowance.",
    "not_authenticated": "Sign in to upload.",
    "bad_magic": "That file is not a replay.",
    "bad_file": "That replay looks truncated.",
    "bad_metadata": "The site could not read that replay's battle info.",
    "decode_failed": "The site could not read that replay.",
}


def _request(method, url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return r.status, dict(r.headers), body
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except urllib.error.URLError as e:
        raise ApiError("network", str(e.reason))
    except TimeoutError:
        raise ApiError("network", "timed out")


def _json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def _is_overwrite_refusal(status, body):
    """Storage refusing to overwrite an object that is already there."""
    if status not in (400, 401, 403, 409):
        return False
    text = (body or b"").decode("utf-8", "replace").lower()
    return ("row-level security" in text or "already exists" in text
            or "duplicate" in text or status == 409)


def _raise_for(status, body):
    """Turn a Supabase/PostgREST error body into an ApiError."""
    j = _json(body) or {}
    code = ""
    msg = ""
    if isinstance(j, dict):
        err = j.get("error")
        if isinstance(err, dict):
            code, msg = err.get("code", ""), err.get("message", "")
        elif isinstance(err, str):
            code, msg = err, j.get("error_description") or j.get("message") or err
        msg = msg or j.get("message") or j.get("msg") or ""
        # Postgres RAISE EXCEPTION comes back as {"message": "feature_x"}
        if not code and msg in TERMINAL_CODES:
            code = msg
    if not msg:
        msg = (body[:200].decode("utf-8", "replace") if body else "HTTP %d" % status)
    # A permission refusal will refuse again in thirty seconds, and again all
    # night. Surface it instead of hiding it inside a retry loop.
    permission = status in (401, 403) or "row-level security" in msg.lower()
    err = ApiError(code, USER_MESSAGES.get(code, msg), status,
                   terminal=code in TERMINAL_CODES)
    err.permission = permission
    raise err


class Session:
    """Access/refresh tokens, persisted encrypted (DPAPI on Windows)."""

    def __init__(self, access=None, refresh=None, expires_at=0, user_id=None,
                 email=None):
        self.access = access
        self.refresh = refresh
        self.expires_at = expires_at or 0
        self.user_id = user_id
        self.email = email

    @property
    def valid(self):
        return bool(self.access) and time.time() < self.expires_at - 60

    @property
    def signed_in(self):
        return bool(self.refresh or self.access)

    def to_dict(self):
        return {"access": self.access, "refresh": self.refresh,
                "expires_at": self.expires_at, "user_id": self.user_id,
                "email": self.email}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in
                      ("access", "refresh", "expires_at", "user_id", "email")})


def _token_path():
    return os.path.join(config.app_dir(), "session.bin")


def _protect(raw):
    """DPAPI on Windows; plain bytes elsewhere (dev only)."""
    if os.name != "nt":
        return b"P" + raw
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    src = BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw),
                                     ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return b"D" + ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _unprotect(blob):
    if not blob:
        return b""
    tag, raw = blob[:1], blob[1:]
    if tag == b"P":
        return raw
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    src = BLOB(len(raw), ctypes.cast(ctypes.create_string_buffer(raw),
                                     ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def save_session(sess):
    os.makedirs(config.app_dir(), exist_ok=True)
    blob = _protect(json.dumps(sess.to_dict()).encode("utf-8"))
    tmp = _token_path() + ".tmp"
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, _token_path())
    try:
        os.chmod(_token_path(), 0o600)
    except OSError:
        pass


def load_session():
    try:
        with open(_token_path(), "rb") as f:
            return Session.from_dict(json.loads(_unprotect(f.read()).decode("utf-8")))
    except Exception:
        return Session()


def clear_session():
    try:
        os.remove(_token_path())
    except OSError:
        pass


class Client:
    def __init__(self, session=None, url=None, key=None):
        self.url = (url or config.SUPABASE_URL).rstrip("/")
        self.key = key or config.SUPABASE_ANON_KEY
        self.session = session or Session()

    # ---- headers ----------------------------------------------------------
    def _h(self, extra=None, auth=True):
        h = {"apikey": self.key, "User-Agent": "GamingDiver-DebriefUploader/1.0"}
        if auth and self.session.access:
            h["Authorization"] = "Bearer " + self.session.access
        h.update(extra or {})
        return h

    # ---- auth -------------------------------------------------------------
    def sign_in_password(self, email, password):
        url = self.url + "/auth/v1/token?grant_type=password"
        body = json.dumps({"email": email, "password": password}).encode()
        st, _, b = _request("POST", url, body,
                            self._h({"Content-Type": "application/json"}, auth=False))
        if st >= 400:
            _raise_for(st, b)
        self._absorb(_json(b))
        return self.session

    def refresh(self):
        if not self.session.refresh:
            raise ApiError("not_authenticated", USER_MESSAGES["not_authenticated"],
                           terminal=True)
        url = self.url + "/auth/v1/token?grant_type=refresh_token"
        body = json.dumps({"refresh_token": self.session.refresh}).encode()
        st, _, b = _request("POST", url, body,
                            self._h({"Content-Type": "application/json"}, auth=False))
        if st >= 400:
            clear_session()
            raise ApiError("not_authenticated",
                           "Your sign-in expired. Sign in again.", st, terminal=True)
        self._absorb(_json(b))
        return self.session

    def ensure_auth(self):
        if self.session.valid:
            return
        if self.session.refresh:
            self.refresh()
            return
        raise ApiError("not_authenticated", USER_MESSAGES["not_authenticated"],
                       terminal=True)

    def sign_out(self):
        try:
            _request("POST", self.url + "/auth/v1/logout", b"{}",
                     self._h({"Content-Type": "application/json"}))
        except ApiError:
            pass
        self.session = Session()
        clear_session()

    def _absorb(self, j):
        if not j:
            raise ApiError("auth", "no session returned")
        u = j.get("user") or {}
        self.session = Session(
            access=j.get("access_token"), refresh=j.get("refresh_token"),
            expires_at=time.time() + int(j.get("expires_in") or 3600),
            user_id=u.get("id"), email=u.get("email"))
        save_session(self.session)

    # ---- edge function ----------------------------------------------------
    def replay_meta(self, raw):
        """POST the raw replay bytes; the decryption key stays server-side.

        This app never holds that key -- shipping a binary with it would
        publish it.
        """
        self.ensure_auth()
        st, _, b = _request("POST", self.url + "/functions/v1/replay-meta", raw,
                            self._h({"Content-Type": "application/octet-stream"}))
        if st >= 400:
            _raise_for(st, b)
        j = _json(b) or {}
        meta = j.get("metadata")
        if not meta:
            raise ApiError("bad_metadata", USER_MESSAGES["bad_metadata"], st,
                           terminal=True)
        return meta

    # ---- rest -------------------------------------------------------------
    def rpc(self, fn, params=None):
        self.ensure_auth()
        body = json.dumps(params or {}).encode()
        st, _, b = _request("POST", self.url + "/rest/v1/rpc/" + fn, body,
                            self._h({"Content-Type": "application/json"}))
        if st >= 400:
            _raise_for(st, b)
        return _json(b)

    def find_existing(self, player_name, played_at):
        """The browser's pre-upload duplicate check. Advisory -- there is no
        unique constraint server-side -- but it saves storage and a wasted
        decode slot, and lets us attach fresh screenshots to the existing row.
        """
        if not player_name or not played_at:
            return None
        self.ensure_auth()
        q = urllib.parse.urlencode({
            "select": "id,short_code,status",
            "player_name": "eq." + player_name,
            "played_at": "eq." + played_at,
            "status": "in.(ready,processing)",
            "order": "created_at.asc",
            "limit": "1",
        })
        st, _, b = _request("GET", self.url + "/rest/v1/replays?" + q, None, self._h())
        if st >= 400:
            _raise_for(st, b)
        rows = _json(b) or []
        return rows[0] if rows else None

    # ---- storage ----------------------------------------------------------
    def storage_delete(self, bucket, path):
        self.ensure_auth()
        url = "%s/storage/v1/object/%s/%s" % (
            self.url, bucket, urllib.parse.quote(path))
        st, _, b = _request("DELETE", url, None, self._h())
        return st < 400

    def storage_upload(self, bucket, path, data, content_type, upsert=False):
        """Write an object, replacing one already there if asked.

        `x-upsert` makes Storage UPDATE an existing object, and the project
        has no UPDATE policy on storage.objects for these buckets -- only
        INSERT, SELECT and DELETE. So overwriting fails with "new row violates
        row-level security policy" even for the object's own owner.

        Rather than depend on a policy change, replace the way the existing
        policies already allow: delete, then insert. Same end state, no server
        change, and it degrades to a plain error if the delete is refused too.
        """
        self.ensure_auth()
        url = "%s/storage/v1/object/%s/%s" % (
            self.url, bucket, urllib.parse.quote(path))
        h = self._h({"Content-Type": content_type,
                     "x-upsert": "true" if upsert else "false",
                     "Cache-Control": "3600"})
        st, _, b = _request("POST", url, data, h)

        if upsert and _is_overwrite_refusal(st, b):
            self.storage_delete(bucket, path)
            st, _, b = _request("POST", url, data, h)

        if st == 409:
            raise ApiError("conflict", "that path already exists", st)
        if st >= 400:
            _raise_for(st, b)
        return True


# ---- helpers shared with the uploader --------------------------------------

def iso_utc(dt_string):
    """WG writes dateTime as 'DD.MM.YYYY HH:mm:ss' with no zone.

    Converted exactly as the browser does (js/replay-upload.js toIsoUTC):
    reorder the fields and stamp Z. This is deliberately NOT a real timezone
    conversion -- matching the browser keeps played_at aligned with every row
    already in the table, which is what the duplicate check compares on.
    """
    import re
    if not dt_string:
        return None
    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})[ T](\d{1,2}):(\d{2}):(\d{2})",
                 str(dt_string))
    if not m:
        return None
    d, mo, y, hh, mm, ss = m.groups()
    return "%s-%02d-%02dT%02d:%s:%sZ" % (y, int(mo), int(d), int(hh), mm, ss)


def storage_stamp(now=None):
    """The browser's raw-object path stamp: an ISO timestamp with ':' and '.'
    replaced by '-'."""
    t = time.gmtime(now if now is not None else time.time())
    ms = int(((now if now is not None else time.time()) % 1) * 1000)
    return time.strftime("%Y-%m-%dT%H-%M-%S", t) + "-%03dZ" % ms
