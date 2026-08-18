# -*- coding: utf-8 -*-
"""統合テスト: フェイクのナビィを相手に、各ツールのデータ収集を最後まで通す。

実際のネットワークには一切アクセスしない。ナビィが「医療機関57件・薬局34件」を
返す状況を作り、ツールがそれを1件も落とさずに取得できるかを確認する。
取りこぼしの再発（ページ打ち切り・重複排除の消しすぎ・座標未確定）はここで落ちる。

実行: python3 tests/test_e2e.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import loader                                    # noqa: E402
from fakenavii import FakeNavii, CLAT, CLON      # noqa: E402

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..") + os.sep
CUT_APP  = "# ════════════════════════════════ サイドバー ════"
CUT_TOOL = "# ─── UI ───"

N_MED, N_PH = 57, 34          # 20件/ページなので、3ページ / 2ページに分かれる

fails = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  " + str(extra)) if extra else ""))
    if not cond:
        fails.append(label)


class Prog:
    def progress(self, *a, **k): pass
    def empty(self): pass


class Geo:
    """ジオコーダのスタブ（住所→中心座標）。"""
    def geocode(self, addr): return (CLAT, CLON)
    def geocode_with_verification(self, *a, **k): return (CLAT, CLON), "ok"
    def geocode_by_name(self, *a, **k): return None


def wire(m, fake):
    """モジュール m の通信部分をフェイクに差し替える。"""
    scraper = m.MHLWScraper()
    scraper.session = fake
    scraper._ready = True
    scraper._sess = lambda: fake
    m.get_scraper = lambda: scraper
    if hasattr(m, "get_geocoder"):
        m.get_geocoder = lambda: Geo()
    m._geocoder = Geo()
    # OSMは別系統なのでこのテストでは無効化し、ナビィ経路だけを見る
    m.search_osm_pharmacies = lambda *a, **k: []
    m.search_osm_medical = lambda *a, **k: []
    return scraper


def main():
    print(f"【条件】ナビィが返す 医療機関={N_MED}件 / 薬局={N_PH}件（すべて商圏内）\n")

    # ── 薬局出店分析ツール v1.4 ─────────────────────────────────────────
    print("== 薬局出店分析ツール_v1.4.py ==")
    m = loader.load(BASE + "薬局出店分析ツール_v1.4.py", "app_v14", cut_marker=CUT_APP)
    fake = FakeNavii(N_MED, N_PH)
    wire(m, fake)
    log = []
    med, ph, clat, clon = m.run_analysis(
        "山梨県中央市若宮50-1", 3000, 50, 9999, log, Prog(),
        polygons=[], workers=4, verify_pass=False, use_osm=False)
    print(f"  取得: 医療機関={len(med)}件 / 薬局={len(ph)}件 "
          f"（一覧ページ 医療機関={sorted(fake.hits['med_pages'])} 薬局={sorted(fake.hits['ph_pages'])}）")
    check("医療機関を全件取得", len(med) == N_MED, f"got={len(med)}")
    check("薬局を全件取得", len(ph) == N_PH, f"got={len(ph)}")
    check("3ページ目まで取りに行く（打ち切りなし）", 2 in fake.hits["med_pages"])
    check("『田中内科』『中田内科』が両方残る（重複排除で消さない）",
          sum(1 for f in med if f.name in ("田中内科クリニック", "中田内科クリニック")) == 2)
    check("『さくら薬局』2店舗が両方残る",
          sum(1 for p in ph if p.name.startswith("さくら薬局")) == 2)
    check("全医療機関の座標が確定", all(f.lat is not None for f in med))
    check("全薬局の年間処方箋数を取得", sum(1 for p in ph if p.annual_rx_count) == N_PH)
    check("全医療機関の外来患者数を取得",
          sum(1 for f in med if f.daily_outpatients) == N_MED,
          f"{sum(1 for f in med if f.daily_outpatients)}/{N_MED}")

    print("\n  [広域再検索ON]")
    m2 = loader.load(BASE + "薬局出店分析ツール_v1.4.py", "app_v14v", cut_marker=CUT_APP)
    fake2 = FakeNavii(N_MED, N_PH)
    wire(m2, fake2)
    med2, ph2, _, _ = m2.run_analysis(
        "山梨県中央市若宮50-1", 3000, 50, 9999, [], Prog(),
        polygons=[], workers=4, verify_pass=True, use_osm=False)
    check("広域再検索ONでも重複が増えない",
          len(med2) == N_MED and len(ph2) == N_PH, f"med={len(med2)} ph={len(ph2)}")

    # ── pharmacy_area_finder.py ─────────────────────────────────────────
    print("\n== pharmacy_area_finder.py ==")
    m = loader.load(BASE + "pharmacy_area_finder.py", "paf", cut_marker=CUT_TOOL)
    wire(m, FakeNavii(N_MED, N_PH))
    ph, med, _, _ = m.run_search("山梨県中央市若宮50-1", 3000, 50, 9999, [], Prog())
    print(f"  取得: 薬局={len(ph)}件 / 医療機関={len(med)}件")
    check("薬局を全件取得", len(ph) == N_PH, f"got={len(ph)}")
    check("医療機関を全件取得", len(med) == N_MED, f"got={len(med)}")
    check("処方箋数を全件取得", sum(1 for p in ph if p.annual_rx_count) == N_PH)
    check("門前/面の判定が全件で確定",
          all(p.pharmacy_type in ("門前薬局", "面薬局") for p in ph))

    # ── area_analysis.py ────────────────────────────────────────────────
    print("\n== area_analysis.py ==")
    m = loader.load(BASE + "area_analysis.py", "aa", cut_marker=CUT_TOOL)
    wire(m, FakeNavii(N_MED, N_PH))
    med, ph, _, _ = m.run_analysis("山梨県中央市若宮50-1", 3000, 50, 9999, [], Prog())
    print(f"  取得: 医療機関={len(med)}件 / 薬局={len(ph)}件")
    check("医療機関を全件取得", len(med) == N_MED, f"got={len(med)}")
    check("薬局を全件取得", len(ph) == N_PH, f"got={len(ph)}")
    check("外来患者数を全件取得", sum(1 for f in med if f.daily_outpatients) == N_MED)

    print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
