#!/usr/bin/env python3
"""SBI証券 ブラウザ自動化クライアント (Playwright)。

重要: ログインフォーム・注文入力フォーム・注文照会画面は認証必須のページのため、
この場では実物を見ずに書いている。`NEEDS_SELECTORS` と書かれた関数・箇所は
すべて仮実装で、実際のSBIの画面構造に合わせて差し替えが必要。

差し替え方（推奨）:
    playwright codegen https://site1.sbisec.co.jp/ETGate
を実行し、自分の手でログイン→買い注文→確認までを一度操作する。生成された
コードに出てくる page.get_by_role(...) / page.locator(...) 等のセレクタを、
このファイルの対応箇所にコピーしてくればよい。

安全に関する方針:
- 想定外の画面（デバイス認証・エラー・見つからない要素等）が出たら、自動で
  突破しようとせず HumanInterventionRequired を投げて処理を止める
- ブラウザは既定で headed（画面表示あり）。人間がその場で操作を引き継げる
  ようにするため。動作が安定してから SBI_HEADLESS=true への切替を検討する
- パスキーは自動化しない（技術的にもできないし、すべきでもない）。ログインは
  ID/パスワードで行う
"""
import os

from playwright.sync_api import sync_playwright

CONF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")
USER_DATA_DIR = os.path.join(CONF_DIR, "browser_data")
LOGIN_URL = "https://site1.sbisec.co.jp/ETGate/"

# NEEDS_SELECTORS: 実際の注文入力画面・注文照会画面のURLに置き換える。
ORDER_ENTRY_URL = "https://site1.sbisec.co.jp/ETGate/?OutSide=on&_ControlID=WPLETsiR001Control"
ORDER_INQUIRY_URL = "https://site1.sbisec.co.jp/ETGate/?OutSide=on&_ControlID=WPLETorR001Control"


class HumanInterventionRequired(Exception):
    """想定外の画面が出た。headed ブラウザで人間が対応してから再実行する。"""


def _load_env():
    env = dict(os.environ)
    path = os.path.join(CONF_DIR, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip())
    return env


class SBIClient:
    def __init__(self):
        self.env = _load_env()
        missing = [k for k in ("SBI_USER_ID", "SBI_LOGIN_PASSWORD") if not self.env.get(k)]
        if missing:
            raise RuntimeError(
                f"config/.env に {', '.join(missing)} がありません。"
                " config/.env.example を config/.env にコピーして値を埋めてください。")
        self._pw = None
        self._context = None
        self.page = None

    # --- lifecycle ---

    def start(self):
        os.makedirs(USER_DATA_DIR, mode=0o700, exist_ok=True)
        headless = self.env.get("SBI_HEADLESS", "false").lower() == "true"
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=headless, locale="ja-JP")
        self.page = (
            self._context.pages[0] if self._context.pages else self._context.new_page()
        )
        return self

    def stop(self):
        if self._context:
            self._context.close()
        if self._pw:
            self._pw.stop()

    # --- login ---

    def ensure_logged_in(self):
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        if self._is_logged_in():
            return
        self._login()
        if not self._is_logged_in():
            raise HumanInterventionRequired(
                "ログイン後、ログイン済み判定に失敗しました。ブラウザの画面を確認し、"
                "デバイス認証・パスキー要求・エラー表示等が出ていれば手動で対応してください。"
                "対応後にもう一度発注を実行すれば続きから進みます。"
            )

    def _is_logged_in(self):
        # NEEDS_SELECTORS: ログイン済みのときだけ出る要素（口座番号・ログアウトリンク等）
        # に置き換える。ひとまず「ログアウト」というテキストの有無で仮判定している。
        return self.page.get_by_text("ログアウト").count() > 0

    def _login(self):
        # NEEDS_SELECTORS: 実際のログインフォームの id/name/placeholder に置き換える。
        # SBIはパスキーを優先表示することがあるため、「ユーザーネームでログイン」等の
        # 切り替えリンクが必要な場合は、ここでそれを先にクリックする処理を足す。
        try:
            self.page.get_by_placeholder("ユーザーネーム").fill(self.env["SBI_USER_ID"])
            self.page.get_by_placeholder("パスワード").fill(self.env["SBI_LOGIN_PASSWORD"])
            self.page.get_by_role("button", name="ログイン").click()
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception as e:
            raise HumanInterventionRequired(
                f"ログインフォームの操作に失敗しました（セレクタが実際の画面と"
                f"合っていない可能性が高い。sbi_client.py の _login を codegen の"
                f"出力で差し替えてください）: {e}"
            )

    # --- orders ---

    def place_order(self, ticker, side, qty, price):
        """指値注文を発注し、SBI側の注文番号を返す。

        現物取引・特定口座・本日中（当日中）の指値注文を仮定している。
        side は 'buy' または 'sell'。
        """
        # NEEDS_SELECTORS: 現物買/現物売の注文入力フォームの構造に置き換える。
        self.page.goto(ORDER_ENTRY_URL, wait_until="domcontentloaded")
        try:
            self.page.get_by_label("銘柄コード").fill(str(ticker))
            self.page.get_by_role("radio", name="買" if side == "buy" else "売").check()
            self.page.get_by_label("株数").fill(str(qty))
            self.page.get_by_label("指値").check()
            self.page.get_by_label("価格").fill(str(price))
            self.page.get_by_role("button", name="注文確認").click()

            trade_password = self.env.get("SBI_TRADE_PASSWORD")
            pw_field = self.page.get_by_label("取引パスワード")
            if trade_password and pw_field.count() > 0:
                pw_field.fill(trade_password)

            self.page.get_by_role("button", name="注文発注").click()
            self.page.wait_for_load_state("networkidle", timeout=15000)

            # NEEDS_SELECTORS: 発注後の確認画面から注文番号を抜き出す部分。
            order_id_text = self.page.get_by_text("注文番号").locator("..").inner_text()
            return order_id_text.strip()
        except HumanInterventionRequired:
            raise
        except Exception as e:
            raise HumanInterventionRequired(
                f"注文入力画面の操作に失敗しました（セレクタが実際の画面と合っていない"
                f"可能性が高い。place_order を codegen の出力で差し替えてください）: {e}"
            )

    def check_order_status(self, sbi_order_id):
        """注文照会画面を開き、指定注文番号の状態を返す。

        戻り値は 'submitted' | 'filled' | 'cancelled' | 'unknown' のいずれか。
        """
        # NEEDS_SELECTORS: 注文照会ページの構造・行から状態文字列を取る方法に置き換える。
        self.page.goto(ORDER_INQUIRY_URL, wait_until="domcontentloaded")
        row = self.page.locator(f"tr:has-text('{sbi_order_id}')")
        if row.count() == 0:
            return "unknown"
        text = row.inner_text()
        if "約定" in text:
            return "filled"
        if "取消" in text:
            return "cancelled"
        return "submitted"
