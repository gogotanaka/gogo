#!/usr/bin/env python3
"""Money Forward Cloud OAuth 2.0 helper for the accounting API.

クラウド会計 API は APIキー非対応（OAuth 2.0 のみ）のため、認可コードフローを使う。
複数の会社（事業者）に対応するため、トークンはプロファイル別に保存する。

- 初回（会社ごと）: `python3 auth.py <profile>` — ブラウザの認可画面で対象の会社を
  選んで同意 → config/tokens.db (SQLite) の tokens テーブルに保存
- 以降: get_access_token(profile) がリフレッシュトークン（540日有効）で自動更新

クライアント設定は共通の config/oauth_client.json を使う。
会社が別テナントで別アプリ登録が必要な場合は config/oauth_client-<profile>.json が優先される。
  {"client_id": "...", "client_secret": "...", "redirect_uri": "http://localhost:8384/callback"}
"""
import base64
import http.server
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

AUTH_BASE = "https://api.biz.moneyforward.com"
SCOPE = "mfc/accounting/report.read mfc/accounting/offices.read"
# 認証情報・トークンDBはアプリ配下の config/ に集約（.gitignore 済み）
CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
DEFAULT_REDIRECT = "http://localhost:8384/callback"


def _client_path(profile):
    specific = os.path.join(CONF_DIR, f"oauth_client-{profile}.json")
    return specific if os.path.exists(specific) else os.path.join(CONF_DIR, "oauth_client.json")


DB_PATH = os.path.join(CONF_DIR, "tokens.db")


def _db():
    os.makedirs(CONF_DIR, mode=0o700, exist_ok=True)
    existed = os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tokens ("
        " profile TEXT PRIMARY KEY,"
        " access_token TEXT NOT NULL,"
        " refresh_token TEXT,"
        " expires_at REAL NOT NULL,"
        " updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    if not existed:
        os.chmod(DB_PATH, 0o600)
    return conn


def list_profiles():
    """tokens.db に登録されたプロファイル一覧を返す。"""
    with _db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT profile FROM tokens ORDER BY profile")]


def load_client(profile):
    path = _client_path(profile)
    try:
        with open(path) as f:
            c = json.load(f)
        assert c.get("client_id") and c.get("client_secret")
        c.setdefault("redirect_uri", DEFAULT_REDIRECT)
        return c
    except (FileNotFoundError, AssertionError, json.JSONDecodeError):
        sys.exit(
            f"[mf-pl] OAuthクライアント設定がありません: {path}\n"
            "アプリポータル (https://biz.moneyforward.com/app_portal/) で連携用アプリを登録し\n"
            f"（リダイレクトURI: {DEFAULT_REDIRECT}）、以下の形式で保存してください:\n"
            '  {"client_id": "...", "client_secret": "..."}'
        )


def _token_request(body, client):
    """POST /token — CLIENT_SECRET_BASIC を試し、401/403 なら CLIENT_SECRET_POST。"""
    data = dict(body)
    basic = base64.b64encode(
        f"{client['client_id']}:{client['client_secret']}".encode()).decode()
    attempts = [
        ({"Authorization": f"Basic {basic}"}, data),
        ({}, {**data, "client_id": client["client_id"],
              "client_secret": client["client_secret"]}),
    ]
    last = None
    for headers, payload in attempts:
        req = urllib.request.Request(
            f"{AUTH_BASE}/token",
            data=urllib.parse.urlencode(payload).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded", **headers},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
            if e.code not in (401, 403):
                break
    sys.exit(f"[mf-pl] トークン取得に失敗しました\n{last}")


def _save_tokens(profile, resp, old=None):
    tokens = {
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token") or (old or {}).get("refresh_token"),
        "expires_at": time.time() + int(resp.get("expires_in", 3600)),
    }
    with _db() as conn:
        conn.execute(
            "INSERT INTO tokens (profile, access_token, refresh_token, expires_at,"
            " updated_at) VALUES (?, ?, ?, ?, datetime('now'))"
            " ON CONFLICT(profile) DO UPDATE SET access_token=excluded.access_token,"
            " refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,"
            " updated_at=excluded.updated_at",
            (profile, tokens["access_token"], tokens["refresh_token"],
             tokens["expires_at"]))
    return tokens


def get_access_token(profile):
    """有効なアクセストークンを返す。期限切れならリフレッシュ。"""
    with _db() as conn:
        row = conn.execute(
            "SELECT access_token, refresh_token, expires_at FROM tokens"
            " WHERE profile = ?", (profile,)).fetchone()
    if row is None:
        sys.exit(f"[mf-pl] プロファイル '{profile}' のトークンがありません。"
                 f"初回認可を実行してください: python3 auth.py {profile}")
    tokens = {"access_token": row[0], "refresh_token": row[1], "expires_at": row[2]}
    if time.time() < tokens["expires_at"] - 60:
        return tokens["access_token"]
    client = load_client(profile)
    resp = _token_request(
        {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        client)
    return _save_tokens(profile, resp, old=tokens)["access_token"]


def authorize(profile):
    """初回のみ: ブラウザで認可（対象の会社を選ぶ）→ code を受け取りトークン保存。"""
    client = load_client(profile)
    redirect = client["redirect_uri"]
    # redirect_uri が https のトンネル (trycloudflare 等) の場合もローカルの
    # 待ち受けは 8384（トンネルの転送先）。local_port で明示上書きも可能
    port = (client.get("local_port")
            or urllib.parse.urlparse(redirect).port or 8384)
    state = secrets.token_urlsafe(16)
    result = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (q.get("code") or [None])[0]
            error = (q.get("error") or [None])[0]
            # トンネルURLは公開されるため、クローラー等の無関係なGET
            # （code も error も無い）は無視して待ち続ける
            if not code and not error:
                self.send_response(404)
                self.end_headers()
                return
            result["code"] = code
            result["state"] = (q.get("state") or [None])[0]
            result["error"] = error
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("認可を受け付けました。ターミナルに戻ってください。".encode())

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("localhost", port), Handler)

    def serve():
        while "code" not in result and "error" not in result:
            server.handle_request()

    threading.Thread(target=serve, daemon=True).start()

    url = f"{AUTH_BASE}/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "scope": SCOPE,
        "state": state,
    })
    print(f"[{profile}] ブラウザで認可してください（対象の会社を選択）:\n{url}",
          file=sys.stderr)
    webbrowser.open(url)

    deadline = time.time() + 600
    while "code" not in result and time.time() < deadline:
        time.sleep(0.5)
    server.server_close()

    if result.get("error"):
        sys.exit(f"[mf-pl] 認可が拒否されました: {result['error']}")
    if not result.get("code"):
        sys.exit("[mf-pl] 認可コードを受け取れませんでした（10分でタイムアウト）")
    if result.get("state") != state:
        sys.exit("[mf-pl] state が一致しません。やり直してください")

    resp = _token_request({
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": redirect,
    }, client)
    _save_tokens(profile, resp)
    print(f"トークンを保存しました: {DB_PATH} (profile={profile})", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        existing = ", ".join(list_profiles()) or "(なし)"
        sys.exit(f"Usage: python3 auth.py <profile>\n登録済みプロファイル: {existing}")
    authorize(sys.argv[1])
