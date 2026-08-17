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
- パスキーは自動化しない（技術的にもできないし、すべきでもない）。パスキーは
  普段使っているブラウザ側のプロファイルに紐づくため、Playwright専用の
  まっさらなブラウザではなく、ユーザーが普段ログインに使っているブラウザに
  CDP (Chrome DevTools Protocol) 経由で接続する（ログイン自体はブラウザ側で
  人間が行う。apps/mf-pl/send_slack.py の Slack CDP フォールバックと同じ発想）

事前準備: 普段使っているChromeを一度完全終了し、リモートデバッグを有効にして
起動し直す必要がある（既存プロセスに後からは付けられない）:

    open -a "Google Chrome" --args --remote-debugging-port=9223 --remote-allow-origins=*

そのChromeでSBIに（パスキーで）ログインしておけば、このクライアントがそのタブに
接続して以降の操作を行う。
"""
from playwright.sync_api import sync_playwright

from config import ENV

LOGIN_URL = "https://site1.sbisec.co.jp/ETGate/"

# NEEDS_SELECTORS: 実際の注文入力画面・注文照会画面・個別銘柄画面のURLに置き換える。
ORDER_ENTRY_URL = "https://site1.sbisec.co.jp/ETGate/?OutSide=on&_ControlID=WPLETsiR001Control"
ORDER_INQUIRY_URL = "https://site1.sbisec.co.jp/ETGate/?OutSide=on&_ControlID=WPLETorR001Control"
QUOTE_URL_TEMPLATE = (
    "https://site1.sbisec.co.jp/ETGate/?OutSide=on&_ControlID=WPLETmgR001Control"
    "&_PageID=WPLETmgR001Mdtl20&i_stock_sec=stock&s_rkbn=2&i_dom_flg=1&i_exchange_code=JPN"
    "&i_output_type=1&stock_sec_code_mul={ticker}"
)


class HumanInterventionRequired(Exception):
    """想定外の画面が出た。headed ブラウザで人間が対応してから再実行する。"""


class SBIClient:
    def __init__(self):
        self.env = ENV
        self.cdp_url = self.env.get("SBI_CDP_URL", "http://localhost:9223")
        self._pw = None
        self._browser = None
        self._context = None
        self.page = None

    # --- lifecycle ---

    def start(self):
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
        except Exception as e:
            self._pw.stop()
            self._pw = None
            raise HumanInterventionRequired(
                f"普段使っているブラウザ ({self.cdp_url}) に接続できません。"
                " 一度完全終了してから、次のコマンドで起動し直してください:\n"
                '  open -a "Google Chrome" --args'
                f" --remote-debugging-port={self.cdp_url.rsplit(':', 1)[-1]}"
                " --remote-allow-origins=*\n"
                f"（元のエラー: {e}）"
            )
        self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self.page = next(
            (p for p in self._context.pages if "sbisec.co.jp" in p.url), None
        ) or (self._context.pages[0] if self._context.pages else self._context.new_page())
        return self

    def stop(self):
        # CDP接続は既存ブラウザを間借りしているだけなので、context/browser を
        # close() してはいけない（ユーザーの実ブラウザが閉じてしまう）。
        if self._pw:
            self._pw.stop()

    # --- login ---

    def ensure_logged_in(self):
        if "sbisec.co.jp" not in self.page.url:
            self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        if self._is_logged_in():
            return
        raise HumanInterventionRequired(
            "SBIにログインしていません。普段使っているブラウザ側でパスキーログインを"
            "行ってください。ログインが完了すれば次回のチェックで自動的に検知します。"
        )

    def _is_logged_in(self):
        # NEEDS_SELECTORS: ログイン済みのときだけ出る要素（口座番号・ログアウトリンク等）
        # に置き換える。ひとまず「ログアウト」というテキストの有無で仮判定している。
        return self.page.get_by_text("ログアウト").count() > 0

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

    # --- price watch ---

    def get_price(self, ticker):
        """指定銘柄の現在値（文字列, 例: "736"）を返す。"""
        # NEEDS_SELECTORS: 個別銘柄ページの実際のURL・現在値要素に置き換える。
        self.page.goto(QUOTE_URL_TEMPLATE.format(ticker=ticker), wait_until="domcontentloaded")
        try:
            price_text = self.page.locator(".stock_price").first.inner_text()
            return price_text.strip()
        except Exception as e:
            raise HumanInterventionRequired(
                f"銘柄 {ticker} の株価取得に失敗しました（セレクタが実際の画面と"
                f"合っていない可能性が高い。get_price を codegen の出力で差し替えてください）: {e}"
            )
