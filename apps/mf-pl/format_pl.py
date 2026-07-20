#!/usr/bin/env python3
"""Format Money Forward trial_balance_pl/bs JSON (stdin) into a Slack message.

残高試算表のネストされた rows から主要項目を抜き出し、
等幅コードブロックで整形した Slack 投稿用テキストを標準出力に出す。
values の並びは [前期残高, 借方, 貸方, 期末残高, 構成比] 固定。
注意: PL の期末残高は期首からの累計。当月発生額 = 期末残高 - 前期残高
（月次推移表 transition_pl の当月列と一致することを実データで確認済み）。
BS は期末残高がそのまま月末時点残高。構成比も PL は累計ベースで返るため、
当月売上高を分母に再計算する。
"""
import json
import sys
import unicodedata

OPENING = 0
CLOSING = 3
RATIO = 4

# 表示したい主要項目（この順）。API の name は「〜合計」が付くことがある
PL_HEADLINES = [
    "売上高",
    "売上原価",
    "売上総利益",
    "販売費及び一般管理費",
    "営業利益",
    "営業外収益",
    "営業外費用",
    "経常利益",
    "特別利益",
    "特別損失",
    "税引前当期純利益",
    "法人税等",
    "当期純利益",
]
BS_HEADLINES = [
    "流動資産",
    "固定資産",
    "繰延資産",
    "資産の部",
    "流動負債",
    "固定負債",
    "負債の部",
    "純資産の部",
    "負債・純資産の部",
]


# 累計が赤字の場合、MF は行名を「〜利益」から「〜損失」に変える（値は負で入る）。
# 特別損失は独立した項目なのでエイリアスに含めない
LOSS_ALIASES = {
    "売上総損失": "売上総利益",
    "営業損失": "営業利益",
    "経常損失": "経常利益",
    "税引前当期純損失": "税引前当期純利益",
    "当期純損失": "当期純利益",
}


def norm(name):
    n = unicodedata.normalize("NFKC", name or "").replace(" ", "").replace("　", "")
    for suffix in ("合計",):
        if n.endswith(suffix) and n != suffix:
            n = n[: -len(suffix)]
    return LOSS_ALIASES.get(n, n)


def walk(rows, depth=0):
    for row in rows or []:
        yield depth, row
        for item in walk(row.get("rows"), depth + 1):
            yield item


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s, w):
    return s + " " * max(0, w - width(s))


def yen(v):
    if v is None:
        return "-"
    return f"{v:,.0f}"


def amount(row, kind):
    """PL: 当月発生額 (期末残高 - 前期残高) / BS: 月末時点残高"""
    v = row.get("values") or []
    if len(v) <= CLOSING or v[CLOSING] is None:
        return None
    if kind == "pl":
        return v[CLOSING] - (v[OPENING] or 0)
    return v[CLOSING]


def row_line(row, kind):
    v = row.get("values") or []
    ratio = v[RATIO] if len(v) > RATIO else None
    return (row.get("name", "?"), amount(row, kind), ratio)


def render(lines):
    """金額（ASCII）を左に右詰め、名前を右に置く。
    Slack のコードブロックでは全角文字幅が半角2文字分にならないため、
    全角の名前でパディングすると桁が崩れる。数字側だけで列を作る。"""
    amount_w = max((len(yen(c)) for _, c, _ in lines), default=0)
    has_ratio = any(isinstance(r, (int, float)) for _, _, r in lines)
    out = []
    for name, value, ratio in lines:
        ratio_s = (f" {ratio:5.1f}%" if isinstance(ratio, (int, float))
                   else "       " if has_ratio else "")
        out.append(f"{yen(value):>{amount_w}}{ratio_s}  {name}")
    return "\n".join(out)


def main():
    report = json.load(sys.stdin)
    kind = report.get("_report") or (
        "bs" if "bs" in (report.get("report_type") or "") else "pl")
    month = report.get("_month", "")
    office = report.get("_office_name", "")
    try:
        y, m = month.split("-")
        month_label = f"{int(y)}年{int(m)}月"
    except ValueError:
        month_label = month or "月次"

    flat = list(walk(report.get("rows")))

    # 主要項目: HEADLINES の順で最初に一致した行を採用（名前は「〜合計」を正規化）
    headlines = PL_HEADLINES if kind == "pl" else BS_HEADLINES
    lines = []
    for headline in headlines:
        for depth, row in flat:
            if norm(row.get("name")) == headline:
                name, value, ratio = row_line(row, kind)
                lines.append((headline, value, ratio))
                break

    # 該当が少なければ階層上位をそのまま出す（科目体系が想定と違う場合の保険）
    if len(lines) < 3:
        lines = [row_line(row, kind) for depth, row in flat if depth <= 1]

    # PL の構成比は累計ベースで返るため、当月売上高を分母に再計算
    if kind == "pl":
        revenue = next((v for n, v, _ in lines if n == "売上高"), None)
        def recalc(items):
            return [(n, v, round(v / revenue * 100, 1)
                     if revenue and v is not None else None)
                    for n, v, _ in items]
        lines = recalc(lines)

    office_label = f"{office} " if office else ""
    if kind == "pl":
        title = f":bar_chart: *{office_label}{month_label} 月次PL*"
        if report.get("start_date") and report.get("end_date"):
            title += f"（{report['start_date']} 〜 {report['end_date']}）"
    else:
        asof = report.get("end_date", "")
        title = f":bank: *{office_label}{month_label}末 BS*"
        if asof:
            title += f"（{asof} 時点）"

    msg = title + "\n```\n" + render(lines) + "\n```"

    # PL のみ: 販管費の内訳 TOP5（当月発生額の大きい勘定科目）
    if kind == "pl":
        sga = []
        for depth, row in flat:
            if norm(row.get("name")) == "販売費及び一般管理費":
                sga = [(r, amount(r, kind)) for _, r in walk(row.get("rows"))
                       if r.get("type") == "account"]
                sga = [(r, a) for r, a in sga if a]
                break
        sga.sort(key=lambda ra: abs(ra[1]), reverse=True)
        if sga:
            top = recalc([(r.get("name", "?"), a, None) for r, a in sga[:5]])
            msg += "\n*販管費 内訳 TOP5*\n```\n" + render(top) + "\n```"

    print(msg)


if __name__ == "__main__":
    main()
