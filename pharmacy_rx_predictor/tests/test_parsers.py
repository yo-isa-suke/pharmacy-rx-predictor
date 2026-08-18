# -*- coding: utf-8 -*-
import os, sys, importlib.util
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as F
from bs4 import BeautifulSoup
import loader

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..") + os.sep

v = loader.load(
    BASE + "薬局出店分析ツール_v1.4.py",
    "v14",
    cut_marker="# ════════════════════════════════ サイドバー ════")

fails = []
def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond: fails.append(label)

sc = v.MHLWScraper.__new__(v.MHLWScraper)

print("== 一覧パース（新旧HTML両対応が維持されている） ==")
for label, ph_html, med_html in (("新HTML(h2)", F.pharmacy_page_new(), F.med_page_new()),
                                 ("旧HTML(h3)", F.pharmacy_page_old(), F.med_page_old())):
    phs, _ = sc._parse_pharmacy_list(ph_html)
    meds, _ = sc._parse_med_list(med_html)
    check(f"{label}: 薬局4件 / 医療機関4件", len(phs) == 4 and len(meds) == 4,
          f"ph={len(phs)} med={len(meds)}")

print("\n== 総件数パース（ページング漏れの修正） ==")
soup = BeautifulSoup(F.pharmacy_page_paged(57), "html.parser")
check("『20件表示』に釣られず57を返す", v.parse_total_count(soup, 4) == 57,
      v.parse_total_count(soup, 4))
old_first = __import__("re").search(r"(\d{1,6})\s*件", soup.get_text()).group(1)
check("旧ロジックなら20と誤読していた", old_first == "20", old_first)

print("\n== 必要ページ数の算出 ==")
calls = []
def page_fn(p):
    calls.append(p); return F.pharmacy_page_new(total=57)
items, total = sc._collect_pages(page_fn, sc._parse_pharmacy_list, v.MAX_PAGES_DEFAULT)
check("57件 → 3ページ取得", sorted(calls) == [0, 1, 2], calls)
check("旧v1.3の医療機関上限6ページ=120件を超えて取得可能", v.MAX_PAGES_DEFAULT * 20 >= 800,
      f"上限={v.MAX_PAGES_DEFAULT*20}件")

print("\n== 重複排除（消しすぎ＝漏れ の修正） ==")
M, P = v.MedFacility, v.PharmacyFacility
c = M(name="田中内科クリニック", kikan_cd="1920001", lat=35.600, lon=138.500)
d = M(name="中田内科クリニック", kikan_cd="1920002", lat=35.601, lon=138.501)
check("『田中内科』と『中田内科』は別施設", not v.same_facility(c, d))
check("  ↳ 旧ロジックの類似度は1.00で同一視されていた",
      v.name_similarity(c.name, d.name) == 1.0, f"{v.name_similarity(c.name,d.name):.2f}")
a = P(name="さくら薬局中央店", address="", kikan_cd="1910001", lat=35.600, lon=138.500)
b = P(name="さくら薬局東町店", address="", kikan_cd="1910002", lat=35.602, lon=138.502)
check("同チェーンの別店舗は別施設", not v.same_facility(a, b))
check("  ↳ 旧ロジックの類似度は0.65以上で同一視されていた",
      v.name_similarity(a.name, b.name) >= 0.65, f"{v.name_similarity(a.name,b.name):.2f}")
check("OSM(コード無し)の同一施設は重複扱い",
      v.same_facility(c, M(name="田中内科クリニック", lat=35.6, lon=138.5)))
check("同名でも5km離れていれば別施設",
      not v.same_facility(c, M(name="田中内科クリニック", lat=35.65, lon=138.56)))

print("\n== kikanKbn フォールバック（詳細が取れず座標を失う漏れ） ==")
g = v.guess_kikan_kbn("3123456")
check("先頭桁3でも病院(1)を試す", 1 in g, g)
check("全区分を網羅", set(g) >= {1, 2, 3, 4, 5}, g)

print("\n== 距離コードの1段拡大（広域再検索） ==")
check("1km→5km", v.wider_dist_code(v.dist_code_for(800)) == "01")
check("5km→指定なし", v.wider_dist_code(v.dist_code_for(3000)) == "")
check("頭打ち", v.wider_dist_code("") == "")

print("\n== OSMクエリに歯科が含まれる ==")
import inspect
q = inspect.getsource(v.search_osm_medical)
check("amenity=dentist を検索する", "dentist" in q)
check("relation も対象(nwr)", "nwr[" in q)
qp = inspect.getsource(v.search_osm_pharmacies)
check("薬局クエリに healthcare=pharmacy", "healthcare\"=\"pharmacy" in qp or 'healthcare"="pharmacy' in qp)

print("\n== [:50] の上限が撤廃されている ==")
ra = inspect.getsource(v.run_analysis)
check("med_targets に [:50] が無い", "][:50]" not in ra)
check("薬局詳細は max_detail のみで制御", "[:max_detail]" in ra)

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
