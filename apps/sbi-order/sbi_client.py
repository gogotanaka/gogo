#!/usr/bin/env python3
"""SBI証券 ブラウザ自動化クライアント (Playwright)。

ログイン判定・株価取得・板情報取得・注文照会・発注（確認画面〜最終発注〜注文番号取得
まで）は、実際にログイン済みのブラウザに接続してエンドツーエンドで動作確認済み
（2026-08-17、はてな(3930) 現物買 200株 指値742円、注文番号487で実際に発注）。

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
import re

from playwright.sync_api import sync_playwright

from config import ENV

# ログイン前は site1、ログイン後の画面は site2 で提供される。
LOGIN_URL = "https://site1.sbisec.co.jp/ETGate/"
HOME_URL = "https://site2.sbisec.co.jp/ETGate/"
# ID/PWでの自動ログイン用（パスキーとは別の専用ドメイン。実画面で確認済み）。
LOGIN_ENTRY_URL = "https://login.sbisec.co.jp/login/entry"

# このサイトは各ページのURLにセッション固有のトークン（_SeqNo等）が含まれる
# 昔ながらの作りで、URLを直接 goto() しても再現できない。そのため各操作は
# 毎回 HOME_URL から実際にリンクをクリックして辿る（下記の各メソッド参照）。


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

    def _page_is_alive(self):
        """タブがクラッシュ（Aw, Snap!／Target crashed）していないか確認する。"""
        try:
            self.page.evaluate("1", timeout=2000)
            return True
        except Exception:
            return False

    def _recover_crashed_page(self):
        """クラッシュしたタブを、同じブラウザ（＝同じログインセッション）内の
        新しいタブに差し替える。連続操作でタブがクラッシュした場合、直さないと
        以後の全操作が同じエラーで無限に失敗し続けてしまうため、自動復旧する。
        """
        try:
            new_page = self._context.new_page()
        except Exception as e:
            raise HumanInterventionRequired(
                f"ブラウザのタブがクラッシュし、新しいタブも開けませんでした。"
                f"手動でブラウザを確認してください: {e}"
            )
        try:
            self.page.close()
        except Exception:
            pass  # クラッシュ済みのタブなのでclose自体が失敗しても無視してよい
        self.page = new_page

    def ensure_logged_in(self):
        if not self._page_is_alive():
            self._recover_crashed_page()
        # ログアウト直後の画面など、遷移済みの古いDOMには「ログアウト」という
        # 文字列自体が残っていて _is_logged_in() が誤って True を返すことが
        # 実際に確認された（2026-08-18）。必ず HOME_URL に一度遷移してから
        # 判定することで、現在の実際のセッション状態を見るようにする。
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        if self._is_logged_in():
            return
        self._login()

    def _login(self):
        """ID・パスワードでの自動ログインを試みる。

        パスキーとは別に、ID/PWでのログインには登録メールアドレス宛のOTP
        （追加認証コード）が挟まることがある（2026-08-18、実画面で確認済み。
        docs/adr/0012参照）。このOTPはメール受信箱へのアクセスが必要で、
        自動化の対象外という方針（パスキー同様、突破しようとせず人間に委ねる）
        のため、OTP画面が出た場合はそこで止めて HumanInterventionRequired を
        投げる。呼び出し元(ensure_logged_in経由)が既存のSlack/mac通知経路で
        アラートするので、人間がブラウザの画面でメールのコードを入力すれば
        次回のチェックで自動的にログイン済みとして検知される。
        """
        user_id = self.env.get("SBI_USER_ID")
        login_password = self.env.get("SBI_LOGIN_PASSWORD")
        if not user_id or not login_password:
            raise HumanInterventionRequired(
                "SBIにログインしていません。config/.env に SBI_USER_ID / "
                "SBI_LOGIN_PASSWORD が未設定のため自動ログインできません。"
                "普段使っているブラウザ側でパスキーログインを行ってください。"
            )
        self.page.goto(LOGIN_ENTRY_URL, wait_until="domcontentloaded")
        self.page.locator("input[name='username']").fill(user_id)
        self.page.locator("input[name='password']").fill(login_password)
        self.page.locator("#pw-btn").click(timeout=10000)
        self.page.wait_for_load_state("domcontentloaded")
        self.page.wait_for_timeout(2000)

        if self._is_logged_in():
            return
        if "otp/entry" in self.page.url:
            raise HumanInterventionRequired(
                "自動ログイン(ID/PW)は成功しましたが、登録メールアドレス宛の"
                "追加認証(OTP)が必要です。メールに届いたコードをブラウザの"
                "画面に入力してログインを完了してください。完了すれば次回の"
                "チェックで自動的に検知します。"
            )
        snippet = self.page.locator("body").inner_text(timeout=1000)[:300]
        raise HumanInterventionRequired(
            f"自動ログインで想定外の画面になりました。ブラウザの状態を"
            f"確認してください: {snippet!r}"
        )

    def _is_logged_in(self):
        # NEEDS_SELECTORS: ログイン済みのときだけ出る要素（口座番号・ログアウトリンク等）
        # に置き換える。ひとまず「ログアウト」というテキストの有無で仮判定している。
        return self.page.get_by_text("ログアウト").count() > 0

    def _click_visible_text(self, text, exact=True, timeout=10000):
        """このサイトはナビゲーション項目が同じテキストで複数箇所（非表示の
        テンプレート複製やメガメニューの畳まれた項目含む）に存在するため、role/name
        でのアクセシブルネーム一致は当てにならない。get_by_text で全マッチを取り、
        実際に画面上に表示されている（bounding boxを持つ）ものだけをクリックする。
        """
        loc = self.page.get_by_text(text, exact=exact)
        for i in range(loc.count()):
            item = loc.nth(i)
            try:
                if item.bounding_box(timeout=1000):
                    item.click(timeout=timeout)
                    return
            except Exception:
                continue
        raise HumanInterventionRequired(
            f"「{text}」という表示中の要素が見つかりませんでした。"
            "画面構成が変わった可能性があります。"
        )

    # --- orders ---

    def _open_order_entry(self):
        """「取引」をクリックし、現物買/売の埋め込みフォームがある画面を開く。"""
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)  # ホーム画面の動的ウィジェットが揃うのを待つ
        self._click_visible_text("取引")
        self.page.wait_for_load_state("domcontentloaded")

    def place_order(self, ticker, side, qty, price):
        """指値注文を発注し、SBI側の注文番号を返す。

        現物取引・特定預り・当日中の指値注文を仮定している。side は 'buy' または 'sell'。

        全ステップ実画面で動作確認済み（2026-08-17/18、はてな(3930)で複数回実発注）。
        フォームの各フィールド名:
          stock_sec_code(銘柄コード) / trade_kbn(0=現物買,1=現物売,2=信用買,3=信用売) /
          input_quantity(株数) / in_sasinari_kbn(' '=指値,'N'=成行,'G'=逆指値) /
          input_price(価格) / hitokutei_trade_kbn(0=特定預り,1=一般預り,H=NISA預り) /
          selected_limit_in(this_day=当日中) / trade_pwd(id=pwd3, 取引パスワード。
          同名で隠しダミーの pwd1/pwd2/pwd4 が並んでいるので id で指定すること)。

          「注文確認画面を省略」(id=shouryaku) をチェックすると、「注文確認画面へ」
          ボタン(id=botton1)が非表示になり、代わりに最終発注ボタン(id=botton2)が
          その場で有効になる。確認画面は元々このコードが機械的にエラー有無を
          判定するだけの中間チェックポイントで、人間が内容を見て判断している
          わけではなかった（誤発注防止の実効性はほぼ無かった）ため、
          ラウンドトリップを1回減らすためにチェックする方針にした。
        """
        if not self.env.get("SBI_TRADE_PASSWORD"):
            raise RuntimeError(
                "config/.env に SBI_TRADE_PASSWORD がありません。取引パスワード無しでは"
                "発注できません。"
            )
        try:
            self._open_order_entry()
            self.page.locator("input[name='stock_sec_code']").fill(str(ticker))
            trade_kbn_value = "0" if side == "buy" else "1"
            self.page.locator(
                f"input[name='trade_kbn'][value='{trade_kbn_value}']"
            ).check()
            self.page.locator("input[name='input_quantity']").fill(str(qty))
            self.page.locator("input[name='in_sasinari_kbn']").nth(0).check()  # 指値
            self.page.locator("input[name='input_price']").fill(str(price))
            self.page.locator(
                "input[name='hitokutei_trade_kbn'][value='0']"
            ).check()  # 特定預り
            self.page.locator(
                "input[name='selected_limit_in'][value='this_day']"
            ).check()  # 当日中
            self.page.locator("#shouryaku").check()  # 注文確認画面を省略
            self.page.locator("#pwd3").fill(self.env["SBI_TRADE_PASSWORD"])
            self.page.locator("#botton2").click(timeout=10000)
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(1500)

            if not self.page.get_by_text("ご注文を受け付けました", exact=False).count():
                # 発注入力画面に留まったままの場合、値幅制限超過等のエラーが
                # 赤字で表示される（例:「注文価格が制限値幅を超えています」）。
                # 正確な位置は不定なので、ページ本文の先頭付近をそのまま添える。
                snippet = self.page.locator("body").inner_text(timeout=1000)[:300]
                raise RuntimeError(f"発注が受け付けられませんでした: {snippet!r}")
            order_no_row = self.page.locator("tr:has-text('注文番号')").last
            return order_no_row.locator("td").last.inner_text().strip()
        except HumanInterventionRequired:
            raise
        except Exception as e:
            raise HumanInterventionRequired(
                f"発注処理中にエラーが発生しました（想定外の画面の可能性があるため、"
                f"安全のためここで停止します。ブラウザの状態を確認してください）: {e}"
            )

    def check_order_status(self, sbi_order_id):
        """注文照会画面を開き、指定注文番号の状態を返す。

        戻り値は 'submitted' | 'filled' | 'cancelled' | 'unknown' のいずれか。
        列: 注文番号 / 注文状況 / 注文種別 / 銘柄コード市場 / ... / 約定 / 約定日時 /
        約定株数 / 約定単価（実画面で確認済み）。約定済みかどうかは「全部約定」等、
        行のテキストに「約定」が含まれるかで判定している（未約定の行は「注文中」）。
        """
        self._open_order_inquiry()

        # tr:has-text() は入れ子テーブルだと外側の大きな行にもマッチしてしまうため、
        # 一番内側（＝実際のデータ行）である最後のマッチを使う。
        row = self.page.locator(f"tr:has-text('{sbi_order_id}')")
        if row.count() == 0:
            return "unknown"
        # 列構成（実画面で確認済み）: 0=注文番号 1=注文状況 2=注文種別 3=銘柄コード市場
        # 4=利用ポイント 5=取消/訂正リンク 6=関連番号 ...
        # 5列目に「取消」という文字列が常に出る（取消操作へのリンクのため）ので、
        # そこを状態文字列と誤認しないよう、必ず1列目（注文状況）だけを見る。
        status_text = row.last.locator("td").nth(1).inner_text().strip()
        if "取消" in status_text:
            return "cancelled"
        if "約定" in status_text:
            return "filled"
        return "submitted"

    def _open_order_inquiry(self):
        """「取引」→「注文照会」と辿り、注文一覧テーブルの画面を開く。"""
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        self.page.wait_for_timeout(1000)
        self._click_visible_text("取引")
        self.page.wait_for_load_state("domcontentloaded")
        self._click_visible_text("注文照会")
        self.page.wait_for_load_state("domcontentloaded")

    def read_order_table(self):
        """注文照会の全論理行を構造化して返す。

        戻り値: [{'order_id': str, 'status': str, 'qty': int|None, 'unfilled': int|None}]
        （qty/unfilled が読めなければ None。約定株数 = qty - unfilled。）

        1論理行は2つの<tr>にまたがる（実画面のダンプで確認済み、2026-08-20）:
          1つ目: 注文番号(rowspan=2) / 注文状況 / 注文種別 / 銘柄 / 利用ポイント / 取消訂正 / 関連番号
          2つ目: 取引・預り / 注文日・期間 / 注文株数（未約定）例 '600 (600)' / 執行条件 / 注文単価
        外側テーブルの querySelectorAll は入れ子の同じ<tr>要素を重複して返さない
        （locatorは要素単位でユニーク）ため、文書順で「注文番号の行→直後の詳細行」
        のペアとして読める。
        """
        self._open_order_inquiry()
        rows = self.page.locator("table tr").all()
        orders = []
        i = 0
        while i < len(rows):
            cells = rows[i].locator("td").all()
            head = []
            for c in cells[:2]:
                try:
                    head.append(c.inner_text(timeout=300).strip())
                except Exception:
                    head.append("")
            if len(head) == 2 and head[0].isdigit() and any(
                    k in head[1] for k in ("注文", "約定", "取消", "失効", "待機")):
                entry = {"order_id": head[0], "status": head[1],
                         "qty": None, "unfilled": None}
                if i + 1 < len(rows):
                    detail = rows[i + 1].locator("td").all()
                    if len(detail) >= 3:
                        try:
                            text = detail[2].inner_text(timeout=300).strip()
                            m = re.match(r"([\d,]+)\s*[（(]([\d,]+)[)）]", text)
                            if m:
                                entry["qty"] = int(m.group(1).replace(",", ""))
                                entry["unfilled"] = int(m.group(2).replace(",", ""))
                        except Exception:
                            pass
                orders.append(entry)
                i += 2
                continue
            i += 1
        return orders

    def list_pending_order_ids(self):
        """現在「注文中」（未約定・未取消）の全注文番号を返す。

        判定基準（td[0]が数字の注文番号・td[1]が「注文中」）は従来のまま
        （実画面で確認済み）。read_order_table() の絞り込みとして実装する。
        """
        return [o["order_id"] for o in self.read_order_table()
                if o["status"] == "注文中"]

    def cancel_order(self, sbi_order_id):
        """指定注文番号を取消する。取消完了（受付済み）を確認できなければ
        HumanInterventionRequired を投げる。

        画面は発注の確認画面と違って1段階（内容確認＋取引パスワード入力＋
        「注文取消」ボタンのみ）。取引パスワード欄は #pwd3（発注時と同じ、
        隠しダミーpwd1/pwd2/pwd4が並ぶ）、ボタンは
        input[name='ACT_place'][value='注文取消']。実画面で確認済み。
        """
        try:
            self._open_order_inquiry()
            row = self.page.locator(f"tr:has-text('{sbi_order_id}')").last
            row.get_by_text("取消", exact=True).click(timeout=10000)
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(1000)

            trade_password = self.env.get("SBI_TRADE_PASSWORD")
            if not trade_password:
                raise RuntimeError("SBI_TRADE_PASSWORD が未設定です")
            self.page.locator("#pwd3").fill(trade_password)
            self.page.locator("input[name='ACT_place']").click(timeout=10000)
            self.page.wait_for_load_state("domcontentloaded")
            self.page.wait_for_timeout(1000)

            if not self.page.get_by_text("ご注文を受け付けました", exact=False).count():
                raise RuntimeError("取消の受付確認が画面に見つかりませんでした")
        except HumanInterventionRequired:
            raise
        except Exception as e:
            raise HumanInterventionRequired(
                f"注文{sbi_order_id}の取消に失敗しました（セレクタが実際の画面と"
                f"合っていない可能性があります。ブラウザの状態を確認してください）: {e}"
            )

    # --- price watch ---

    def get_price(self, ticker):
        """指定銘柄の現在値（文字列, 例: "734"）を返す。

        トップページの銘柄検索ボックス（#brand-search-text）に銘柄コードを入力して
        Enterで検索し、遷移先の個別銘柄ページの現在値セル（id="MTB0_0" 内の
        span.fxx01）から読み取る。このid構造はページテンプレート側の行番号なので
        銘柄によらず共通のはず（3930で確認済み）。
        """
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        try:
            search = self.page.locator("#brand-search-text")
            search.fill(str(ticker))
            search.press("Enter")
            self.page.wait_for_load_state("domcontentloaded")
            # このページは自動更新の常時接続があり networkidle 待ちが使えないため、
            # 現在値セルが "--"（未取得のプレースホルダ）から実際の値に変わるまで
            # 短い間隔でポーリングする。
            cell = self.page.locator("#MTB0_0 .fxx01").first
            price_text = ""
            for _ in range(20):
                price_text = cell.inner_text(timeout=1000).strip()
                if price_text and price_text != "--":
                    break
                self.page.wait_for_timeout(300)
            if not price_text or price_text == "--":
                raise RuntimeError(f"現在値が取得できませんでした（表示: {price_text!r}）")
            return price_text
        except Exception as e:
            raise HumanInterventionRequired(
                f"銘柄 {ticker} の株価取得に失敗しました（セレクタが実際の画面と"
                f"合っていない可能性が高い。get_price を確認してください）: {e}"
            )

    def get_order_book(self, ticker):
        """指定銘柄の気配（板）と本日の出来高を取得する。実画面で確認済み。

        個別銘柄ページの「売気配株数／気配値／買気配株数」の3列テーブルを
        そのまま構造化して返す。価格は基本的に高い方が先頭（降順）。
        同じページ内にある出来高（#MTB0_5、株価テーブルのid構造は銘柄によらず
        共通のはず。3930で確認済み。get_priceの#MTB0_0と同じ発想）も、
        追加のページ遷移なしにあわせて読む。
        戻り値: {'rows': [{'ask_qty': int|None, 'price': str, 'bid_qty': int|None}, ...],
                 'volume': str|None}
        price は "755.0" のような文字列、または "OVER"/"UNDER"/"成行" の場合がある。
        volume は出来高が読めなければ None（板情報自体は取れているので、これだけで
        全体を失敗させない）。
        """
        self.page.goto(HOME_URL, wait_until="domcontentloaded")
        try:
            search = self.page.locator("#brand-search-text")
            search.fill(str(ticker))
            search.press("Enter")
            self.page.wait_for_load_state("domcontentloaded")
            header = self.page.get_by_text("売気配株数", exact=True).first
            table = header.locator("xpath=ancestor::table[1]")

            def _read_rows():
                return table.evaluate(
                    """t => Array.from(t.querySelectorAll('tbody tr')).map(tr =>
                        Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim())
                    )"""
                )

            # get_price 同様、値が "--" プレースホルダから実データに変わるまで
            # 短い間隔でポーリングする（networkidle待ちはこのページでは使えない）。
            raw_rows = _read_rows()
            for _ in range(20):
                if any(c and c not in ("成行", "OVER", "UNDER") and "--" not in c
                       for row in raw_rows for c in row):
                    break
                self.page.wait_for_timeout(300)
                raw_rows = _read_rows()

            book = []
            for cells in raw_rows:
                if len(cells) != 3:
                    continue
                ask, price, bid = cells
                if not price:
                    continue
                book.append({
                    "ask_qty": _to_int(ask),
                    "price": price,
                    "bid_qty": _to_int(bid),
                })
            if not book:
                raise RuntimeError("板データが空でした")
            return {"rows": book, "volume": self._read_volume()}
        except Exception as e:
            raise HumanInterventionRequired(
                f"銘柄 {ticker} の板情報取得に失敗しました（セレクタが実際の画面と"
                f"合っていない可能性が高い。get_order_book を確認してください）: {e}"
            )

    def _read_volume(self):
        """個別銘柄ページの出来高セル(#MTB0_5)を読む。取れなければNoneを返す
        （板情報の取得自体は成功しているので、出来高だけで全体を失敗させない）。
        """
        cell = self.page.locator("#MTB0_5 .fm01").first
        if cell.count() == 0:
            return None
        text = ""
        for _ in range(5):
            try:
                text = cell.inner_text(timeout=1000).strip()
            except Exception:
                return None
            if text and text != "--":
                return text
            self.page.wait_for_timeout(300)
        return text or None

    def best_bid(self, ticker):
        """買い板の一番（最良買気配）の (価格, 数量) を返す。無ければ None。"""
        for row in self.get_order_book(ticker)["rows"]:
            if row["bid_qty"]:
                return row["price"], row["bid_qty"]
        return None


def _to_int(text):
    text = text.replace(",", "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def format_order_book(ticker, book):
    """板データをSlack投稿向けの等幅テキストに整形する。
    book は get_order_book() が返す {'rows': [...], 'volume': str|None} の形。
    """
    header = f"*{ticker} 板*"
    if book.get("volume"):
        header += f"（本日出来高: {book['volume']}株）"
    lines = [header, "```", "売数量    気配値  買数量"]
    for row in book["rows"]:
        ask = f"{row['ask_qty']:>6,}" if row["ask_qty"] else " " * 6
        bid = f"{row['bid_qty']:>6,}" if row["bid_qty"] else ""
        lines.append(f"{ask}  {row['price']:>6}  {bid}")
    lines.append("```")
    return "\n".join(lines)
