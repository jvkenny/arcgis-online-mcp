"""ArcGIS REST client: HTTP, authentication, and token handling.

Auth modes, picked from the environment (first match wins):

  ARCGIS_TOKEN                     a token you already have
  ARCGIS_API_KEY                   an API key / developer credential
  ARCGIS_USERNAME + ARCGIS_PASSWORD  built-in ArcGIS account (not SAML/enterprise logins)
  ARCGIS_CLIENT_ID                 OAuth 2.0 PKCE sign-in in the browser (works with SAML)
  (nothing)                        anonymous: public content only
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import __version__

USER_AGENT = f"arcgis-online-mcp/{__version__}"
REFERER = "https://github.com/jvkenny/arcgis-online-mcp"
TOKEN_EXPIRED_CODES = {498, 499}


def log(*parts):
    print("[arcgis-online-mcp]", *parts, file=sys.stderr, flush=True)


def env(name, default=None):
    """Read an env var, treating blanks and unsubstituted ${...} templates as unset."""
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    if not value or (value.startswith("${") and value.endswith("}")):
        return default
    return value


def env_flag(name):
    return (env(name, "") or "").lower() in {"1", "true", "yes", "on"}


class ArcGISError(Exception):
    def __init__(self, message, code=None, details=None):
        self.code = code
        self.details = details or []
        text = message if code is None else f"{message} (code {code})"
        if self.details:
            text += ": " + "; ".join(str(d) for d in self.details[:5])
        super().__init__(text)


def _ssl_context():
    # python.org macOS builds ship without system roots until "Install Certificates"
    # is run; prefer certifi when it happens to be installed.
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


_SSL = _ssl_context()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _encode_multipart(fields, files):
    boundary = uuid.uuid4().hex
    lines = []
    for key, value in fields.items():
        lines += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{key}"'.encode(),
            b"",
            str(value).encode("utf-8"),
        ]
    for key, (filename, content, ctype) in files.items():
        lines += [
            f"--{boundary}".encode(),
            f'Content-Disposition: form-data; name="{key}"; filename="{filename}"'.encode(),
            f"Content-Type: {ctype}".encode(),
            b"",
            content,
        ]
    lines += [f"--{boundary}--".encode(), b""]
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


class ArcGISClient:
    def __init__(self):
        portal = env("ARCGIS_PORTAL_URL", "https://www.arcgis.com").rstrip("/")
        if portal.endswith("/sharing/rest"):
            portal = portal[: -len("/sharing/rest")]
        if not portal.startswith("http"):
            portal = "https://" + portal
        self.portal = portal
        self.rest = f"{portal}/sharing/rest"
        self.portal_host = urllib.parse.urlparse(portal).hostname or ""
        self.allow_writes = env_flag("ARCGIS_ALLOW_WRITES")
        self.timeout = int(env("ARCGIS_TIMEOUT", "60"))
        extra = env("ARCGIS_TOKEN_HOSTS", "") or ""
        self.token_hosts = {h.strip().lower() for h in extra.split(",") if h.strip()}

        self._token = None
        self._token_expires = 0.0
        self._lock = threading.RLock()
        self._self_cache = None
        self._layer_cache = {}

        self.client_id = env("ARCGIS_CLIENT_ID")
        if env("ARCGIS_TOKEN"):
            self.mode = "token"
            self._token = env("ARCGIS_TOKEN")
            self._token_expires = float("inf")
        elif env("ARCGIS_API_KEY"):
            self.mode = "api_key"
            self._token = env("ARCGIS_API_KEY")
            self._token_expires = float("inf")
        elif env("ARCGIS_USERNAME") and env("ARCGIS_PASSWORD"):
            self.mode = "password"
        elif self.client_id:
            self.mode = "oauth"
            self.redirect_uri = env("ARCGIS_REDIRECT_URI", "http://127.0.0.1:7071/callback")
            self._load_oauth_cache()
        else:
            self.mode = "anonymous"
        self._pending_signin = None

    # ------------------------------------------------------------------ tokens

    def _token_allowed_for(self, url):
        parts = urllib.parse.urlparse(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https":
            return False
        return (
            host == self.portal_host.lower()
            or host == "arcgis.com"
            or host.endswith(".arcgis.com")
            or host in self.token_hosts
        )

    def token(self):
        with self._lock:
            if self._token and time.time() < self._token_expires - 120:
                return self._token
            if self.mode == "password":
                self._generate_token()
                return self._token
            if self.mode == "oauth":
                if self._refresh_oauth():
                    return self._token
                return None
            return self._token

    def invalidate_token(self):
        with self._lock:
            if self.mode in ("password", "oauth"):
                self._token = None
                self._token_expires = 0.0

    def _generate_token(self):
        data = self._raw_request(
            f"{self.rest}/generateToken",
            {
                "username": env("ARCGIS_USERNAME"),
                "password": env("ARCGIS_PASSWORD"),
                "client": "referer",
                "referer": REFERER,
                "expiration": 120,
                "f": "json",
            },
            token=None,
        )
        if "token" not in data:
            raise ArcGISError("generateToken returned no token")
        self._token = data["token"]
        self._token_expires = data.get("expires", 0) / 1000.0 or time.time() + 7200

    # ------------------------------------------------------------------ OAuth

    def _oauth_cache_path(self):
        base = Path(env("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        key = hashlib.sha256(f"{self.portal}|{self.client_id}".encode()).hexdigest()[:16]
        return base / "arcgis-online-mcp" / f"oauth-{key}.json"

    def _load_oauth_cache(self):
        path = self._oauth_cache_path()
        try:
            cached = json.loads(path.read_text())
        except Exception:
            self._oauth = {}
            return
        self._oauth = cached
        if cached.get("access_token") and cached.get("expires_at", 0) > time.time():
            self._token = cached["access_token"]
            self._token_expires = cached["expires_at"]

    def _save_oauth_cache(self):
        path = self._oauth_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(self._oauth, fh)
        os.replace(tmp, path)

    def _store_oauth_response(self, data):
        now = time.time()
        self._token = data["access_token"]
        self._token_expires = now + int(data.get("expires_in", 1800))
        self._oauth["access_token"] = self._token
        self._oauth["expires_at"] = self._token_expires
        if data.get("username"):
            self._oauth["username"] = data["username"]
        if data.get("refresh_token"):
            self._oauth["refresh_token"] = data["refresh_token"]
            ttl = int(data.get("refresh_token_expires_in", 1209600))
            self._oauth["refresh_expires_at"] = now + ttl
        self._save_oauth_cache()

    def _refresh_oauth(self):
        refresh = self._oauth.get("refresh_token")
        if not refresh or self._oauth.get("refresh_expires_at", 0) < time.time():
            return False
        try:
            data = self._raw_request(
                f"{self.rest}/oauth2/token",
                {
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "f": "json",
                },
                token=None,
            )
        except ArcGISError as exc:
            log("OAuth refresh failed:", exc)
            return False
        self._store_oauth_response(data)
        return True

    def sign_in(self, wait_seconds=90):
        """Run the OAuth 2.0 authorization-code + PKCE flow through a localhost redirect."""
        if self.mode != "oauth":
            raise ArcGISError("sign_in needs ARCGIS_CLIENT_ID (OAuth). Current auth mode: " + self.mode)
        if self.token():
            return {"status": "already_signed_in", "username": self._oauth.get("username")}

        redirect = urllib.parse.urlparse(self.redirect_uri)
        if redirect.hostname not in ("127.0.0.1", "localhost") or not redirect.port:
            raise ArcGISError("ARCGIS_REDIRECT_URI must be http://127.0.0.1:<port>/<path> or http://localhost:<port>/<path>")

        if self._pending_signin and self._pending_signin["thread"].is_alive():
            state = self._pending_signin
        else:
            verifier = _b64url(secrets.token_bytes(48))
            challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
            nonce = secrets.token_urlsafe(16)
            auth_url = f"{self.rest}/oauth2/authorize?" + urllib.parse.urlencode(
                {
                    "client_id": self.client_id,
                    "response_type": "code",
                    "redirect_uri": self.redirect_uri,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": nonce,
                    "expiration": 20160,
                }
            )
            state = {"verifier": verifier, "nonce": nonce, "url": auth_url, "done": threading.Event(), "error": None}
            client = self

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    parsed = urllib.parse.urlparse(self.path)
                    if parsed.path != redirect.path:
                        self.send_response(404)
                        self.end_headers()
                        return
                    qs = urllib.parse.parse_qs(parsed.query)
                    message = "Signed in to ArcGIS. You can close this tab."
                    try:
                        if state.get("cancelled"):
                            raise ArcGISError("Sign-in was cancelled by sign_out")
                        if qs.get("state", [None])[0] != state["nonce"]:
                            raise ArcGISError("OAuth state mismatch")
                        if "error" in qs:
                            raise ArcGISError(qs.get("error_description", qs["error"])[0])
                        data = client._raw_request(
                            f"{client.rest}/oauth2/token",
                            {
                                "client_id": client.client_id,
                                "grant_type": "authorization_code",
                                "code": qs["code"][0],
                                "redirect_uri": client.redirect_uri,
                                "code_verifier": state["verifier"],
                                "f": "json",
                            },
                            token=None,
                        )
                        with client._lock:
                            client._store_oauth_response(data)
                    except Exception as exc:  # report in the browser and to the tool
                        state["error"] = str(exc)
                        message = f"Sign-in failed: {exc}"
                    body = f"<html><body style='font-family:sans-serif;padding:2em'><p>{message}</p></body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(body.encode())
                    state["done"].set()

            try:
                httpd = HTTPServer((redirect.hostname, redirect.port), Handler)
            except OSError as exc:
                raise ArcGISError(f"Could not listen on {self.redirect_uri}: {exc}")
            httpd.timeout = 1

            def serve():
                deadline = time.time() + 600
                while not state["done"].is_set() and time.time() < deadline:
                    httpd.handle_request()
                httpd.server_close()

            state["thread"] = threading.Thread(target=serve, daemon=True)
            state["thread"].start()
            self._pending_signin = state
            try:
                webbrowser.open(auth_url)
            except Exception:
                pass

        state["done"].wait(timeout=max(0, wait_seconds))
        if not state["done"].is_set():
            return {
                "status": "waiting_for_browser",
                "message": "Finish signing in using the browser window (or open sign_in_url yourself), then call whoami.",
                "sign_in_url": state["url"],
            }
        if state["error"]:
            raise ArcGISError(state["error"])
        self._self_cache = None
        return {"status": "signed_in", "username": self._oauth.get("username")}

    def sign_out(self):
        if self.mode != "oauth":
            return {"status": "not_oauth", "mode": self.mode}
        if self._pending_signin:
            self._pending_signin["cancelled"] = True
            self._pending_signin["done"].set()
            self._pending_signin = None
        try:
            self._oauth_cache_path().unlink()
        except FileNotFoundError:
            pass
        self._oauth = {}
        self._token = None
        self._token_expires = 0.0
        self._self_cache = None
        return {"status": "signed_out"}

    # ------------------------------------------------------------------ HTTP

    def _raw_request(self, url, params, token, files=None):
        params = {k: v for k, v in params.items() if v is not None}
        for key, value in list(params.items()):
            if isinstance(value, (dict, list)):
                params[key] = json.dumps(value, separators=(",", ":"))
            elif isinstance(value, bool):
                params[key] = "true" if value else "false"
        if token:
            params["token"] = token
        headers = {"User-Agent": USER_AGENT, "Referer": REFERER}
        if files:
            body, ctype = _encode_multipart(params, files)
        else:
            body, ctype = urllib.parse.urlencode(params).encode(), "application/x-www-form-urlencoded"
        headers["Content-Type"] = ctype
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=_SSL) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", "replace")
            raise ArcGISError(f"HTTP {exc.code} from {url}", code=exc.code, details=[detail] if detail else None)
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                reason += (
                    " — Python can't find root certificates. On macOS python.org builds, run "
                    "'Install Certificates.command' from your Python folder, or `pip install certifi`."
                )
            raise ArcGISError(f"Could not reach {url}: {reason}")
        try:
            data = json.loads(raw)
        except ValueError:
            raise ArcGISError(f"Non-JSON response from {url}: {raw[:200]!r}")
        if isinstance(data, dict) and "error" in data:
            err = data["error"] or {}
            raise ArcGISError(
                err.get("message") or "ArcGIS error", code=err.get("code"), details=err.get("details")
            )
        return data

    def request(self, url, params=None, files=None, auth=True):
        """POST to an ArcGIS REST endpoint, attaching a token only to trusted hosts."""
        params = dict(params or {})
        params.setdefault("f", "json")
        use_token = auth and self._token_allowed_for(url)
        token = self.token() if use_token else None
        try:
            return self._raw_request(url, params, token, files)
        except ArcGISError as exc:
            if token and exc.code in TOKEN_EXPIRED_CODES and self.mode in ("password", "oauth"):
                self.invalidate_token()
                token = self.token()
                return self._raw_request(url, params, token, files)
            if exc.code in TOKEN_EXPIRED_CODES | {403} and not token:
                hint = None
                if auth and not use_token and self.mode != "anonymous":
                    hint = (
                        f"Token not sent: {urllib.parse.urlparse(url).hostname} isn't a trusted host. "
                        "Add it to ARCGIS_TOKEN_HOSTS (comma-separated) if it's a federated server you trust."
                    )
                elif self.mode == "oauth":
                    hint = "Not signed in — call the sign_in tool first."
                elif self.mode == "anonymous":
                    hint = (
                        "This content needs a login. Configure ARCGIS_API_KEY, ARCGIS_CLIENT_ID, "
                        "or ARCGIS_USERNAME/ARCGIS_PASSWORD (see README)."
                    )
                if hint:
                    err = ArcGISError(f"{exc} — {hint}")
                    err.code = exc.code
                    raise err
            raise

    # ------------------------------------------------------------------ portal helpers

    def portal_self(self, refresh=False):
        if self._self_cache is None or refresh:
            self._self_cache = self.request(f"{self.rest}/portals/self")
        return self._self_cache

    def username(self):
        if self.mode == "password":
            return env("ARCGIS_USERNAME")
        user = (self.portal_self().get("user") or {}).get("username")
        if not user and self.mode == "oauth":
            user = self._oauth.get("username")
        return user

    def require_user(self):
        user = self.username()
        if not user:
            raise ArcGISError(
                "This tool needs a signed-in user. Configure credentials (see README) "
                "or call sign_in if OAuth is set up. API keys aren't tied to a user, so they can't own content."
            )
        return user

    def org_home(self):
        info = self.portal_self()
        key, base = info.get("urlKey"), info.get("customBaseUrl")
        if key and base:
            return f"https://{key}.{base}"
        return self.portal

    def layer_info(self, url):
        url = url.rstrip("/")
        if url not in self._layer_cache:
            self._layer_cache[url] = self.request(url)
        return self._layer_cache[url]
