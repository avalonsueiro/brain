#!/usr/bin/env python3
"""Google OAuth for the rig's read-only Gmail and Calendar skills.

One refresh token covers both, so `login` runs once and `gmail.py` / `gcal.py`
share it. Stdlib only -- urllib against the REST endpoints, no
google-api-python-client, so these stay plain CLIs that work under any harness.

    auth.py login     # one-time, opens a browser
    auth.py status
    auth.py revoke

Credentials live in $RIG_ROOT/state/google.env (0600), never in the repo.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_RIG_ROOT = "/opt/agent-rig"

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Read-only, deliberately. The rig's standing rule is that outward actions need
# explicit go-ahead; a skill that cannot send or modify anything cannot break
# that rule by accident.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
)


def env_file() -> Path:
    return Path(os.environ.get("RIG_ROOT", DEFAULT_RIG_ROOT)) / "state" / "google.env"


def _read_env() -> dict:
    path = env_file()
    if not path.exists():
        return {}
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def _write_env(values: dict) -> None:
    path = env_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"{k}={v}" for k, v in sorted(values.items())) + "\n"
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o600)          # before the rename, so it is never briefly 0644
    os.replace(tmp, path)


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RuntimeError(f"{url} returned {exc.code}: {detail}") from None


# --- the token the callers actually want -----------------------------------


def access_token() -> str:
    """A valid access token, refreshing if the cached one has expired."""
    env = _read_env()
    missing = [k for k in ("CLIENT_ID", "CLIENT_SECRET", "REFRESH_TOKEN") if not env.get(k)]
    if missing:
        raise RuntimeError(
            f"google.env is missing {', '.join(missing)} — run "
            f"{Path(__file__).parent}/auth.py login"
        )

    # 60s of slack, so a token cannot expire between this check and the call
    # that uses it.
    if env.get("ACCESS_TOKEN") and float(env.get("EXPIRES_AT", 0)) > time.time() + 60:
        return env["ACCESS_TOKEN"]

    payload = _post(TOKEN_URL, {
        "client_id": env["CLIENT_ID"],
        "client_secret": env["CLIENT_SECRET"],
        "refresh_token": env["REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    })
    env["ACCESS_TOKEN"] = payload["access_token"]
    env["EXPIRES_AT"] = str(int(time.time() + payload.get("expires_in", 3600)))
    _write_env(env)
    return env["ACCESS_TOKEN"]


def api_get(url: str, params: dict | None = None) -> dict:
    """Authorized GET. Retries once on 401 in case the token died early."""
    for attempt in (1, 2):
        # doseq: a list value must become repeated params. Without it urlencode
        # serializes the list's repr, Gmail silently ignores the malformed
        # metadataHeaders, and every message comes back with no From or Subject.
        full = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        req = urllib.request.Request(full)
        req.add_header("Authorization", f"Bearer {access_token()}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1:
                env = _read_env()
                env.pop("ACCESS_TOKEN", None)   # force a refresh, then retry
                env.pop("EXPIRES_AT", None)
                _write_env(env)
                continue
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise RuntimeError(f"Google API {exc.code}: {detail}") from None
    raise RuntimeError("unreachable")


# --- one-time login --------------------------------------------------------


class _Catcher(http.server.BaseHTTPRequestHandler):
    code = None
    error = None

    def do_GET(self):                                    # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _Catcher.code = (query.get("code") or [None])[0]
        _Catcher.error = (query.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        done = "You can close this tab and go back to the terminal."
        self.wfile.write(
            f"<html><body style='font:16px system-ui;padding:3rem'>"
            f"<h2>{'Authorized' if _Catcher.code else 'Authorization failed'}</h2>"
            f"<p>{done if _Catcher.code else _Catcher.error}</p></body></html>".encode()
        )

    def log_message(self, *a):                           # keep the console clean
        pass


def login(client_id: str | None = None, client_secret: str | None = None) -> None:
    env = _read_env()
    client_id = client_id or env.get("CLIENT_ID")
    client_secret = client_secret or env.get("CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "need a client id and secret: auth.py login --client-id ... --client-secret ..."
        )

    # Loopback redirect. Google retired the paste-a-code (OOB) flow in 2022, so
    # a desktop client has to catch the redirect itself.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    redirect = f"http://127.0.0.1:{port}"

    # PKCE. Not strictly required when a client secret is present, but a
    # desktop secret is public by design, so the verifier is what actually
    # protects the exchange.
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",          # force a refresh token even on re-auth
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })

    print("Opening your browser to authorize read-only Gmail and Calendar access.")
    print(f"If it does not open, visit:\n\n{url}\n")
    server = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    server.timeout = 300
    webbrowser.open(url)
    while _Catcher.code is None and _Catcher.error is None:
        server.handle_request()
    server.server_close()

    if _Catcher.error:
        raise RuntimeError(f"authorization declined: {_Catcher.error}")

    payload = _post(TOKEN_URL, {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _Catcher.code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect,
    })
    if "refresh_token" not in payload:
        raise RuntimeError(
            "Google returned no refresh token. Revoke the app at "
            "myaccount.google.com/permissions and run login again."
        )

    env.update({
        "CLIENT_ID": client_id,
        "CLIENT_SECRET": client_secret,
        "REFRESH_TOKEN": payload["refresh_token"],
        "ACCESS_TOKEN": payload["access_token"],
        "EXPIRES_AT": str(int(time.time() + payload.get("expires_in", 3600))),
    })
    _write_env(env)
    print(f"\nAuthorized. Credentials written to {env_file()} (0600).")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="auth", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="one-time authorization")
    p.add_argument("--client-id")
    p.add_argument("--client-secret")
    sub.add_parser("status", help="is this rig authorized?")
    sub.add_parser("revoke", help="invalidate the refresh token")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "login":
            login(args.client_id, args.client_secret)
            return 0
        if args.cmd == "status":
            env = _read_env()
            if not env.get("REFRESH_TOKEN"):
                print(f"not authorized — run: {Path(__file__)} login")
                return 1
            profile = api_get("https://gmail.googleapis.com/gmail/v1/users/me/profile")
            print(f"authorized as {profile.get('emailAddress')}")
            print(f"  {profile.get('messagesTotal', 0):,} messages, read-only scopes")
            print(f"  credentials: {env_file()}")
            return 0
        if args.cmd == "revoke":
            env = _read_env()
            if env.get("REFRESH_TOKEN"):
                try:
                    _post(REVOKE_URL, {"token": env["REFRESH_TOKEN"]})
                except RuntimeError:
                    pass          # already invalid upstream; clear it locally anyway
            _write_env({k: v for k, v in env.items()
                        if k in ("CLIENT_ID", "CLIENT_SECRET")})
            print("revoked locally. Also check myaccount.google.com/permissions.")
            return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
