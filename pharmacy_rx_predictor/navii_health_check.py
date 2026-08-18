# -*- coding: utf-8 -*-
"""ナビィ構造チェッカー ―― 「またツールが動かなくなった」ときに最初に走らせるもの。

厚労省ナビィ（医療情報ネット）に実際にアクセスし、ツールが前提にしている
HTMLの構造・APIの応答が今も成立しているかを1つずつ確認して表示する。

2026年7月にナビィの検索結果ページで施設名の見出しが <h3 class="name"> から
<h2 class="name"> に変わり、リストアップ系ツールが「検索は通るのに0件」に
なった。この種の変更を、コードを読まずに数十秒で特定できるようにするための
スクリプト。

実行:  python3 navii_health_check.py
       python3 navii_health_check.py --lat 35.6644 --lon 138.5686   # 場所を指定
"""
import argparse
import re
import sys
import urllib.parse

import requests
from bs4 import BeautifulSoup

MHLW_DOMAIN = "https://www.iryou.teikyouseido.mhlw.go.jp"
MHLW_BASE = MHLW_DOMAIN + "/znk-web"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

OK, NG, WARN = "✅", "❌", "⚠️ "
problems = []


def report(status, label, detail=""):
    print(f"{status} {label}" + (f"\n     {detail}" if detail else ""))
    if status is NG:
        problems.append(label)


def check_list_page(html, kind):
    """一覧ページのHTMLが、ツールの前提どおりかを確認する。"""
    soup = BeautifulSoup(html, "html.parser")

    items = soup.select("div.resultItems div.item") or soup.find_all("div", class_="item")
    if items:
        report(OK, f"[{kind}] 結果カード div.item が見つかる（{len(items)}件）")
    else:
        report(NG, f"[{kind}] 結果カード div.item が見つからない",
               "一覧のマークアップが変わった可能性。_parse_*_list の select を要修正。")
        return

    # 施設名の見出しタグ（ここが2026-07に h3 → h2 になった箇所）
    tags = {}
    for it in items:
        for t in ("h2", "h3", "h4"):
            if it.find(t, class_="name"):
                tags[t] = tags.get(t, 0) + 1
    if tags:
        detail = " / ".join(f"{t}.name × {n}" for t, n in sorted(tags.items()))
        if set(tags) <= {"h2", "h3", "h4"}:
            report(OK, f"[{kind}] 施設名の見出しタグを検出", detail)
        if "h3" not in tags and "h2" not in tags:
            report(WARN, f"[{kind}] h2/h3 以外の見出しが使われている", detail)
    else:
        report(WARN, f"[{kind}] <hN class=\"name\"> が無い",
               "kikanCdリンクのフォールバックで拾えているか、次の項目を確認。")

    links = [a for it in items for a in it.select('a[href*="kikanCd"]')]
    if links:
        report(OK, f"[{kind}] 詳細リンク（kikanCd付き）が見つかる（{len(links)}本）")
        qp = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(links[0]["href"]).query))
        missing = [k for k in ("prefCd", "kikanCd") if k not in qp]
        if missing:
            report(NG, f"[{kind}] 詳細リンクに {', '.join(missing)} が無い", str(qp))
        else:
            report(OK, f"[{kind}] 詳細リンクのパラメータ", str(qp))
    else:
        report(NG, f"[{kind}] 詳細リンク（kikanCd）が1本も無い",
               "施設名リンクの探索方法を要修正。")

    # 一覧に埋め込まれた座標
    coords = 0
    for it in items:
        for a in it.find_all("a"):
            for attr in ("data-url", "href"):
                if re.search(r"q=-?\d+\.\d+\s*,\s*-?\d+\.\d+", a.get(attr, "") or ""):
                    coords += 1
                    break
            else:
                continue
            break
    if coords:
        report(OK, f"[{kind}] 一覧に地図座標が埋め込まれている（{coords}/{len(items)}件）")
    else:
        report(WARN, f"[{kind}] 一覧に座標が無い",
               "住所からのジオコーディングに頼るため、座標を取れない施設が増える可能性。")

    # 総件数の読み取り
    text = soup.get_text(" ", strip=True)
    first = re.search(r"([\d,]{1,9})\s*件", text)
    pats = [r"検索結果[^0-9]{0,12}([\d,]{1,9})\s*件", r"全\s*([\d,]{1,9})\s*件",
            r"([\d,]{1,9})\s*件\s*中", r"該当\s*([\d,]{1,9})\s*件"]
    hit = next((m.group(1) for p in pats for m in [re.search(p, text)] if m), None)
    if hit:
        report(OK, f"[{kind}] 総件数を読み取れた: {hit}件",
               f"（本文で最初に出る「N件」は {first.group(1) if first else '—'}件。"
               "これを総件数と誤読すると2ページ目以降を取りに行かなくなる）")
    else:
        report(NG, f"[{kind}] 総件数が読み取れない",
               "parse_total_count の正規表現を要更新。ページング打ち切り＝取りこぼしになる。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=35.6644, help="検索中心の緯度")
    ap.add_argument("--lon", type=float, default=138.5686, help="検索中心の経度")
    ap.add_argument("--timeout", type=int, default=20)
    a = ap.parse_args()

    s = requests.Session()
    s.headers.update(HEADERS)
    print(f"ナビィ構造チェック  中心=({a.lat}, {a.lon})\n" + "─" * 60)

    # ── 到達性 ──────────────────────────────────────────────────────────
    try:
        r = s.get(f"{MHLW_BASE}/juminkanja/S2320/initialize", timeout=a.timeout)
        if r.status_code == 200:
            report(OK, f"ナビィに接続できる（S2320/initialize {r.status_code}）")
        else:
            report(NG, f"ナビィの応答が {r.status_code}")
            return 1
    except Exception as e:
        report(NG, "ナビィに接続できない", str(e))
        return 1

    # ── 医療機関検索 ────────────────────────────────────────────────────
    try:
        s.get(f"{MHLW_BASE}/juminkanja/S2320/initsearch", timeout=a.timeout)
        r = s.get(f"{MHLW_BASE}/juminkanja/S2320/search", timeout=a.timeout, params={
            "specifyDateAndTime": "01",
            "centerPointName": urllib.parse.quote("検索地点"),
            "latitude": str(a.lat), "longitude": str(a.lon),
            "selectCenterPoint": "", "distanceFromCenterPoint": "01",
            "medicalCare": ["1", "2"], "searchTypes": "01-2",
        })
        j = r.json()
        if j.get("code") == "0" and j.get("result", {}).get("redirectUrl"):
            report(OK, "医療機関検索API（S2320/search）が期待どおりのJSONを返す")
            url = j["result"]["redirectUrl"]
            sep = "&" if "?" in url else "?"
            page = s.get(f"{url}{sep}page=0&size=20&sortNo=2", timeout=a.timeout)
            check_list_page(page.text, "医療機関")
        else:
            report(NG, "医療機関検索APIの応答形式が変わった", str(j)[:300])
    except Exception as e:
        report(NG, "医療機関検索で例外", f"{type(e).__name__}: {e}")

    print()

    # ── 薬局検索 ────────────────────────────────────────────────────────
    try:
        s.get(f"{MHLW_BASE}/juminkanja/S2300/initializeYakk", timeout=a.timeout)
        r = s.get(f"{MHLW_BASE}/juminkanja/S2300/yakkyokuSearch", timeout=a.timeout, params={
            "iyakuKbn": "2", "lang": "ja",
            "latitude": str(a.lat), "longitude": str(a.lon),
            "distanceFromCenterPoint": "01",
            "centerPointName": urllib.parse.quote("検索地点"),
            "selectCenterPoint": "3", "specifyDateAndTime": "01", "XCHARSET": "utf-8",
        })
        j = r.json()
        sid = j.get("result", {}).get("id")
        if j.get("code") == "0" and sid:
            report(OK, "薬局検索API（S2300/yakkyokuSearch）が期待どおりのJSONを返す")
            s.get(f"{MHLW_BASE}/juminkanja/S2300/yakkyokuSearch", timeout=a.timeout, params={
                "id": sid, "latitude": str(a.lat), "longitude": str(a.lon),
                "distanceFromCenterPoint": "01", "selectCenterPoint": "3",
                "specifyDateAndTime": "01", "XCHARSET": "utf-8"})
            page = s.get(f"{MHLW_BASE}/juminkanja/S2400/initialize", timeout=a.timeout,
                         params={"id": sid, "page": 0, "size": 20, "sortNo": 2})
            check_list_page(page.text, "薬局")
        else:
            report(NG, "薬局検索APIの応答形式が変わった", str(j)[:300])
    except Exception as e:
        report(NG, "薬局検索で例外", f"{type(e).__name__}: {e}")

    print("─" * 60)
    if problems:
        print(f"{NG} 前提が崩れている項目が {len(problems)} 件あります:")
        for p in problems:
            print(f"   ・{p}")
        print("\nツールが0件になる／件数が減る場合、上の項目に対応するパーサを直してください。")
        return 1
    print(f"{OK} ナビィ側の構造は、ツールの前提どおりです。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
