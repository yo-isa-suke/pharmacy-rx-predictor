"""
260702_Prescription Analysis v2 — 処方箋獲得予測ツール（商圏形状: 円形／ポリゴン両対応）

住所（＝出店候補地）を入力し、商圏を「円形（半径指定）」または
「ポリゴン（地図をなぞって手描き。クライアントにもらった商圏図の転記を想定）」で
指定して、その地点に薬局を出した場合に獲得できる年間処方箋枚数を予測する。

【v2: 商圏形状の指定】
  - 円形: 従来通り半径スライダーで指定
  - ポリゴン: 地図上で多角形を手描き（複数可）。施設の収集は住所を中心に
    ポリゴンを包含する円で行い、収集後にポリゴン内外を判定してフィルタ。
    「ポリゴン外の医療機関を寄与から除外」で、川・線路等で分断された
    エリアのクリニックを予測から外せる（門前≤50mは実質影響なし。
    効くのは500m〜3kmの面の獲得と競合薬局の集計）。
    ポリゴンは検索後も描き直し可能（収集済み円の内側なら再検索不要で即再計算）。

【予測モデル】
    獲得処方箋 = Σ_医療機関( 1日平均外来患者数 × 年間診療日数
                              × 院外処方係数 × 発行率 × 流入率(距離) )
  距離帯別 流入率（自社薬局の実績ベンチマーク・発行率織込済）:
    〜50m（門前）0.570 / 〜500m 0.070 / 〜1km 0.050 / 〜2km 0.012 / 〜3km 0.004 / 3km超 0
  すべてのアサンプションはサイドバーで調整可能（検索後も即時再計算）。

【主な機能】
  - 🎯 処方箋獲得予測: 総獲得枚数＋医療機関ごとの寄与内訳＋CSV出力
  - 🧪 実績照合: 既存薬局の位置に同モデルを当て、ナビィ実績と答え合わせ（校正ヒント表示）
  - 門前占有チェック: 候補地の門前クリニックに既存薬局が張り付いている場合のみ⚠️アラート
    （オプションで面レートへ自動引下げ）
  - 外来患者数の異常値検出: 年間値/月間値の誤登録疑いを自動フラグ＋補正候補提示
  - 手動補完: 大病院の未報告等はHP/年報の値で上書き可能（ナビィ原値は保持）

【データ品質検証（2026-07 実データ監査: 3エリア46医療機関+32薬局）】
  - 外来患者数はナビィ「前年度１日平均患者数」多列の外来列（7列目）から取得。
    診療所〜大病院まで正確（相澤病院=824人/日 は公表水準と整合）を確認済み。
  - 座標はナビィ詳細ページ埋込の緯度経度を最優先（住所ジオコーディングの
    ±30〜80m誤差を回避し、門前50m判定の信頼性を確保）。
  - 年間値の誤登録実例（外来列=13,736人）を確認 → 検証層で自動検出・補正候補提示。
  - 美容・自由診療クリニック（保険処方箋ほぼ0）は自動判定し係数0で算入。
  - 歯科診療所は歯科患者列から取得し係数0.05（発行率が医科より大幅に低いため）。
  - 全角数字ツールチップの誤検出（外来=1人）、週診療日数の不能値（週8日等）、
    薬局「報告0件」と取得失敗の混同、をそれぞれ修正済み。

データソース:
  - 厚生労働省「医療情報ネット（ナビィ）」— 医療機関・薬局リスト・詳細・座標
  - OpenStreetMap Overpass API — 施設の座標（補助）
  - 国土地理院（GSI）/ Nominatim — ジオコーディング（フォールバック）
"""

import csv
import io
import math
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import folium
from folium.plugins import Draw
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from streamlit_folium import st_folium

# ─── ページ設定（必ずファイル先頭のstコマンドより前に置く） ─────────────────────
st.set_page_config(
    page_title="Prescription Analysis v2（処方箋獲得予測）",
    page_icon="🎯",
    layout="wide",
)

# ─── 定数 ─────────────────────────────────────────────────────────────────────
MHLW_DOMAIN = "https://www.iryou.teikyouseido.mhlw.go.jp"
MHLW_BASE   = MHLW_DOMAIN + "/znk-web"
WORKING_DAYS = 305

# ─── 処方箋獲得予測モデル（流入率アサンプション） ──────────────────────────────
# 各医療機関 → 候補薬局地点までの直線距離帯ごとの「流入率」。
# 医療機関の外来患者のうち、この地点の薬局に処方箋を持ち込む割合を表す
# 経験値。(上限距離m, 流入率) の昇順リスト。最初に dist<=上限 に合致した帯を採用。
DEFAULT_INFLOW_BANDS: List[Tuple[float, float]] = [
    (50.0,   0.570),   # 〜50m（門前）
    (500.0,  0.070),   # 50m〜500m
    (1000.0, 0.050),   # 500m〜1km
    (2000.0, 0.012),   # 1km〜2km
    (3000.0, 0.004),   # 2km〜3km
    # 3km超は 0%（帯に該当しなければ流入0）
]


@dataclass
class PredictionAssumptions:
    """処方箋獲得予測で使う調整可能なアサンプション一式。"""
    bands: List[Tuple[float, float]] = field(
        default_factory=lambda: list(DEFAULT_INFLOW_BANDS)
    )
    annual_days_mode: str = "weekly"       # "weekly"=週診療日数×52 / "fixed"=固定日数
    fixed_annual_days: int = WORKING_DAYS  # weekly不明時のフォールバック日数
    external_factor_gairai: float = 1.0    # 院外処方あり の寄与係数
    external_factor_inhouse: float = 0.0   # 院内処方のみ の寄与係数（原則0）
    external_factor_unknown: float = 1.0   # 院内外不明 の寄与係数
    issue_rate: float = 1.0                # 処方箋発行率（流入率に織込済なら1.0）
    discount_contested_monzen: bool = False  # 門前競合クリニックを面レートに引下げるか
    cosmetic_factor: float = 0.0           # 美容・自由診療クリニックの寄与係数（保険処方箋ほぼ0）
    dental_factor: float = 0.05            # 歯科診療所の寄与係数（外来1回あたり発行率が医科より大幅に低い）


def inflow_rate_for_distance(
    dist_m: Optional[float], bands: List[Tuple[float, float]]
) -> float:
    """距離(m)に対応する流入率を返す。どの帯にも該当しなければ0。"""
    if dist_m is None:
        return 0.0
    for upper, rate in sorted(bands, key=lambda b: b[0]):
        if dist_m <= upper:
            return rate
    return 0.0


def inflow_band_label(dist_m: Optional[float], bands: List[Tuple[float, float]]) -> str:
    """距離が属する帯の人間可読ラベル（例「〜50m（門前）」）を返す。"""
    if dist_m is None:
        return "座標なし"
    sorted_bands = sorted(bands, key=lambda b: b[0])
    prev = 0.0
    for upper, _rate in sorted_bands:
        if dist_m <= upper:
            if upper <= 50:
                return f"〜{int(upper)}m（門前）"
            lo = f"{int(prev)}m" if prev < 1000 else f"{prev/1000:.0f}km"
            hi = f"{int(upper)}m" if upper < 1000 else f"{upper/1000:.0f}km"
            return f"{lo}〜{hi}"
        prev = upper
    top = sorted_bands[-1][0]
    return f"{top/1000:.0f}km超（流入0）"


def _external_factor(rx_summary: str, a: PredictionAssumptions) -> float:
    """院内外処方の別から寄与係数を決める。"""
    if rx_summary.startswith("院外処方あり"):
        return a.external_factor_gairai
    if rx_summary == "院内処方のみ":
        return a.external_factor_inhouse
    return a.external_factor_unknown


def facility_key(fac: "MedFacility") -> str:
    """医療機関を一意に識別するキー（手動上書き辞書のキーに使用）。"""
    return fac.kikan_cd or f"name:{fac.name}"


def compute_pharmacy_proximity(
    med_facs: List["MedFacility"], pharmacies: List["PharmacyFacility"]
) -> None:
    """
    各医療機関について、最も近い既存薬局までの距離を計算して書き戻す。
    門前占有チェック（そのクリニックの門前に既に別薬局が張り付いているか）に使う。
    候補地の新店は pharmacies に含まれないため、全て競合薬局として扱われる。
    """
    ph_coords = [p for p in pharmacies if p.lat is not None and p.lon is not None]
    for fac in med_facs:
        if fac.lat is None or fac.lon is None:
            fac.nearest_pharmacy_dist_m = None
            fac.nearest_pharmacy_name = ""
            continue
        best_d = float("inf")
        best_name = ""
        for p in ph_coords:
            d = haversine(fac.lat, fac.lon, p.lat, p.lon)
            if d < best_d:
                best_d = d
                best_name = p.name
        if best_name:
            fac.nearest_pharmacy_dist_m = best_d
            fac.nearest_pharmacy_name = best_name
        else:
            fac.nearest_pharmacy_dist_m = None
            fac.nearest_pharmacy_name = ""


def compute_capture_prediction(
    med_facs: List["MedFacility"],
    a: PredictionAssumptions,
    op_override: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """
    各医療機関について、候補地点（商圏中心）が獲得できる年間処方箋枚数を計算し、
    MedFacility に結果を書き戻す。合計値等のサマリーdictを返す。

        年間外来延べ数 = 1日平均外来患者数 × 年間診療日数
        獲得処方箋     = 年間外来延べ数 × 院外処方係数 × 発行率 × 流入率(距離)

    op_override: {facility_key: 外来患者数} でナビィ値を手動上書き（大病院のHP値等）。
                 ナビィ原値 fac.daily_outpatients は保持したまま計算にのみ反映する。

    門前占有: 候補地が門前バンド(≤先頭帯上限)に入るクリニックで、既に別の薬局が
             同じ門前圏に張り付いている場合 monzen_contested=True を立てる。
             流入率(門前0.570)は「自店が門前になれる」前提の実績値のため、
             椅子が埋まっているクリニックは過大評価になりうる旨をアラートする。
             discount_contested_monzen=True のときは面レート(第2帯)へ自動引下げ。
    """
    op_override = op_override or {}
    sorted_bands = sorted(a.bands, key=lambda b: b[0])
    monzen_radius = sorted_bands[0][0] if sorted_bands else 50.0
    men_rate = sorted_bands[1][1] if len(sorted_bands) >= 2 else 0.0
    total = 0.0
    n_contrib = 0
    n_no_op = 0
    n_contested = 0
    for fac in med_facs:
        # 門前占有の物理判定（外来数の有無に関わらず算出）
        fac.monzen_occupied = (
            fac.nearest_pharmacy_dist_m is not None
            and fac.nearest_pharmacy_dist_m <= monzen_radius
        )
        # 候補地から見て門前バンドに入るクリニックか
        cand_is_monzen = fac.distance_m is not None and fac.distance_m <= monzen_radius
        fac.monzen_contested = bool(cand_is_monzen and fac.monzen_occupied)

        # 年間診療日数
        if a.annual_days_mode == "weekly" and fac.weekly_op_days:
            days = fac.weekly_op_days * 52.0
        else:
            days = float(a.fixed_annual_days)
        fac.annual_op_days_used = days

        # 外来患者数（手動上書き優先）
        ov = op_override.get(facility_key(fac))
        if ov is not None and ov > 0:
            eff_op: Optional[float] = float(ov)
            fac.outpatient_manual = True
        else:
            eff_op = fac.daily_outpatients
            fac.outpatient_manual = False

        if eff_op is None:
            fac.annual_op_visits = None
            fac.external_rx_factor = _external_factor(fac.rx_summary, a)
            fac.inflow_rate = 0.0 if not fac.in_area else inflow_rate_for_distance(fac.distance_m, a.bands)
            fac.inflow_band = ("商圏外（ポリゴン）" if not fac.in_area
                               else inflow_band_label(fac.distance_m, a.bands))
            fac.captured_rx = None
            if fac.in_area:
                n_no_op += 1
            continue

        annual_visits = eff_op * days
        factor = _external_factor(fac.rx_summary, a)
        # 美容・自由診療は保険処方箋がほぼ発生しない／歯科は発行率が医科より大幅に低い
        if fac.is_cosmetic:
            factor *= a.cosmetic_factor
        elif fac.facility_category == "歯科診療所":
            factor *= a.dental_factor
        rate = inflow_rate_for_distance(fac.distance_m, a.bands)

        # 門前競合クリニックは、任意で面レートへ引下げ（デフォルトは引下げず表示のみ）
        if fac.monzen_contested and a.discount_contested_monzen:
            rate = men_rate

        # 商圏ポリゴン外（川・線路等で分断）のクリニックは寄与から除外
        band_label = inflow_band_label(fac.distance_m, a.bands)
        if not fac.in_area:
            rate = 0.0
            band_label = "商圏外（ポリゴン）"

        captured = annual_visits * factor * a.issue_rate * rate

        fac.annual_op_visits = annual_visits
        fac.external_rx_factor = factor
        fac.inflow_rate = rate
        fac.inflow_band = band_label
        fac.captured_rx = captured
        if captured > 0:
            n_contrib += 1
            total += captured
        # 門前競合かつ実際に門前レートで寄与しているクリニックのみアラート対象
        if fac.monzen_contested and captured > 0:
            n_contested += 1

    return {
        "total_annual_rx": total,
        "total_daily_rx": total / a.fixed_annual_days if a.fixed_annual_days else 0.0,
        "n_contributing": n_contrib,
        "n_no_outpatient": n_no_op,
        "n_contested_monzen": n_contested,
        "n_outside_area": sum(1 for f in med_facs if not f.in_area),
    }


def predict_at_point(
    lat: float,
    lon: float,
    med_facs: List["MedFacility"],
    a: PredictionAssumptions,
    op_override: Optional[Dict[str, float]] = None,
) -> float:
    """
    任意地点の年間獲得処方箋数を計算する（MedFacilityの状態は変更しない純関数）。
    実績照合タブで「既存薬局の位置にモデルを当てたらいくつになるか」を出すのに使う。
    """
    op_override = op_override or {}
    total = 0.0
    for fac in med_facs:
        if fac.lat is None or fac.lon is None:
            continue
        d = haversine(lat, lon, fac.lat, fac.lon)
        rate = inflow_rate_for_distance(d, a.bands)
        if rate <= 0:
            continue
        ov = op_override.get(facility_key(fac))
        op = float(ov) if (ov is not None and ov > 0) else fac.daily_outpatients
        if not op:
            continue
        if a.annual_days_mode == "weekly" and fac.weekly_op_days:
            days = fac.weekly_op_days * 52.0
        else:
            days = float(a.fixed_annual_days)
        factor = _external_factor(fac.rx_summary, a)
        if fac.is_cosmetic:
            factor *= a.cosmetic_factor
        elif fac.facility_category == "歯科診療所":
            factor *= a.dental_factor
        total += op * days * factor * a.issue_rate * rate
    return total


# ─── 2トラック予測：①医療機関ベース（ハフ競合按分）／②集客ベース（来店客数） ────
# 検証（面型275店 vs ナビィ実績）で、加算型は面型を中央値2.75倍過大・実績とほぼ無相関だった。
# 主因は「各クリニック外来の固定割合を、周辺に何店薬局があろうとこの1店に独占計上」していたこと。
# ①はこれを競合薬局との按分（ハフ＝引力×距離）に置き換えて過大を是正する。
# ②はスーパー等の来店客数から直接、面で取れる枚数を見積もる独立トラック。両者を併記する。

@dataclass
class HuffParams:
    """医療機関ベース（ハフ）予測のパラメータ。既定は面型275店の検証で
    過大がほぼ解消した設定（λ=250m・門前×8・純距離＝競合の引力は一律1）。"""
    enabled: bool = True
    lambda_m: float = 250.0                 # 距離減衰の距離定数（小さいほど近距離に集中）
    monzen_boost: float = 8.0               # 門前(≤monzen_radius)の引力ブースト
    monzen_radius: float = 50.0
    candidate_attractiveness: float = 1.0   # 候補店の引力（集客力/規模）。1.0=全国平均並み
    weight_by_power: bool = False           # 競合薬局を実績枚数で引力加重するか（既定OFF＝一律1）
    national_avg_rx: float = 12000.0        # 引力換算の基準（全国平均 年間枚数）
    reach_m: float = 3000.0                 # 商圏（3km円）


@dataclass
class FootfallParams:
    """集客ベース（来店客数）予測のパラメータ。いただいた店舗ファイル式を年齢2区分に拡張。"""
    enabled: bool = False
    store_format: str = "食品スーパー"
    unique_customers_monthly: float = 0.0   # 月間ユニーク客数（会員数 or POS客数÷来店回数で算出）
    ratio_65plus: float = 0.30              # ユニーク客のうち65歳以上の比率
    visits_month_65plus: float = 3.0        # 65歳以上の月平均受診回数
    visits_month_under65: float = 1.3       # 65歳未満の月平均受診回数
    issue_rate: float = 0.8054              # 処方箋発行率
    external_rate: float = 0.8313           # 院外処方率
    use_rate: float = 0.137                 # 当該薬局利用率
    national_avg_rx: float = 12000.0        # 競合パワー換算の基準
    menkata_monzen_dist: float = 50.0       # これ以内にクリニックがある薬局は門前と自動判定(後で目視修正可)
    menkata_main_rx: float = 15000.0        # 年間実績がこれ以上の薬局はメイン薬局とみなし面競合から除外(0=無効)
    competitor_decay_m: float = 1000.0      # 面競合を候補地からの距離で減衰(exp(-d/λ))。0で減衰なし(全店フラット)


# 店舗形態プリセット（来店回数などの既定値）
FORMAT_PRESETS = {
    "大型GMS/モール": {"visit_freq": 4.0, "r65": 0.28},
    "食品スーパー":   {"visit_freq": 4.0, "r65": 0.32},
    "ドラッグ路面":   {"visit_freq": 3.0, "r65": 0.35},
    "独立/その他":     {"visit_freq": 4.0, "r65": 0.30},
}


def _pharmacy_attractiveness(ph: "PharmacyFacility", national_avg: float) -> float:
    """既存薬局の引力＝年間実績枚数÷全国平均。不明は1.0（平均並み）とみなす。"""
    rx = getattr(ph, "annual_rx_count", None)
    if rx and rx > 0:
        return max(rx / national_avg, 0.05)
    return 1.0


def _clinic_annual_rx_pool(
    fac: "MedFacility", a: PredictionAssumptions, op_override: Dict[str, float]
) -> float:
    """クリニックが年間に発生させる院外処方の総量（枚）。predict と同じ係数で算出。"""
    ov = op_override.get(facility_key(fac))
    eff_op = float(ov) if (ov is not None and ov > 0) else fac.daily_outpatients
    if not eff_op:
        return 0.0
    if a.annual_days_mode == "weekly" and fac.weekly_op_days:
        days = fac.weekly_op_days * 52.0
    else:
        days = float(a.fixed_annual_days)
    factor = _external_factor(fac.rx_summary, a)
    if fac.is_cosmetic:
        factor *= a.cosmetic_factor
    elif fac.facility_category == "歯科診療所":
        factor *= a.dental_factor
    return eff_op * days * factor * a.issue_rate


def compute_huff_prediction(
    med_facs: List["MedFacility"],
    pharmacies: List["PharmacyFacility"],
    cand_lat: float,
    cand_lon: float,
    a: PredictionAssumptions,
    hp: HuffParams,
    op_override: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """
    ①医療機関ベース（ハフ）：各クリニックの年間院外処方プールを、周辺の全薬局へ
    「引力×距離減衰」で按分し、候補店の取り分だけを合算する。
        取り分率_自店 = A_自店·w(d_自店) / Σ_全薬局 A_k·w(d_k),  w(d)=exp(−d/λ)·門前boost
    全薬局の取り分は合計1（＝クリニックの総処方箋数）に保存され、独占・二重計上が起きない。
    """
    op_override = op_override or {}
    comps: List[Tuple[float, float, float]] = []
    for p in pharmacies:
        if p.lat is None or p.lon is None:
            continue
        A = _pharmacy_attractiveness(p, hp.national_avg_rx) if hp.weight_by_power else 1.0
        comps.append((p.lat, p.lon, A))

    def bw(d: float, A: float) -> float:
        val = math.exp(-d / hp.lambda_m)
        if d <= hp.monzen_radius:
            val *= hp.monzen_boost
        return A * val

    total = 0.0
    rows: List[Dict[str, object]] = []
    for f in med_facs:
        if f.lat is None or f.lon is None:
            continue
        if not getattr(f, "in_area", True):
            continue
        d_self = haversine(cand_lat, cand_lon, f.lat, f.lon)
        if d_self > hp.reach_m:
            continue
        pool = _clinic_annual_rx_pool(f, a, op_override)
        if pool <= 0:
            continue
        num = bw(d_self, hp.candidate_attractiveness)
        den = num
        for (plat, plon, A) in comps:
            dk = haversine(plat, plon, f.lat, f.lon)
            if dk <= hp.reach_m:
                den += bw(dk, A)
        if den <= 0:
            continue
        share = num / den
        cap = pool * share
        total += cap
        rows.append({
            "clinic": f.name,
            "dist_m": d_self,
            "pool": pool,
            "share": share,
            "captured": cap,
        })
    rows.sort(key=lambda r: r["captured"], reverse=True)
    return {"total": total, "rows": rows, "n_competitors": len(comps)}


def pharmacy_key(p: "PharmacyFacility") -> str:
    """薬局を一意に識別するキー（面/門前の手動修正の保存キーに使用）。"""
    return p.kikan_cd or f"name:{p.name}"


def classify_menkata(
    pharmacies: List["PharmacyFacility"],
    med_facs: List["MedFacility"],
    cand_lat: float,
    cand_lon: float,
    monzen_dist: float = 50.0,
    main_rx_threshold: float = 15000.0,
    reach_m: float = 3000.0,
) -> List[Dict[str, object]]:
    """
    候補地の商圏（reach_m 円）内の各薬局を「面／門前」に自動判定する（目視修正の土台）。
    自動で門前とみなす条件：最寄りクリニックが monzen_dist 以内、または年間実績が
    main_rx_threshold 以上（特定クリニックのメイン薬局。0で無効）。
    戻り値は薬局ごとの情報dict（key/name/候補地距離/最寄りクリニック距離/実績/自動=面か）。
    """
    facs = [f for f in med_facs if f.lat is not None and f.lon is not None]
    out: List[Dict[str, object]] = []
    for p in pharmacies:
        if p.lat is None or p.lon is None:
            continue
        d_cand = haversine(cand_lat, cand_lon, p.lat, p.lon)
        if d_cand > reach_m:
            continue
        nd = min((haversine(p.lat, p.lon, f.lat, f.lon) for f in facs), default=9e9)
        rx = getattr(p, "annual_rx_count", None)
        is_monzen = (nd <= monzen_dist) or bool(main_rx_threshold and rx and rx >= main_rx_threshold)
        out.append({
            "key": pharmacy_key(p), "name": p.name, "d_cand": d_cand,
            "nearest_clinic": nd, "rx": rx, "auto_menkata": (not is_monzen),
        })
    out.sort(key=lambda r: r["d_cand"])
    return out


def footfall_competitor_power(
    classified: List[Dict[str, object]],
    menkata_override: Optional[Dict[str, bool]] = None,
    competitor_decay_m: float = 1000.0,
    national_avg: float = 12000.0,
) -> Tuple[float, int, int]:
    """
    集客ベース②のシェア分母。classify_menkata の結果に手動修正（menkata_override: key→面か）を
    重ね、面と判定された薬局だけを「年間枚数÷全国平均（引力）× 候補地からの距離減衰 exp(-d/λ)」で
    重み付けして合計する（遠い面薬局はスーパー客の選択肢に入りにくいので実効競合を減らす）。
    戻り値: (面競合パワー合計, 面競合店数, 門前扱いで除外した店数)
    """
    menkata_override = menkata_override or {}
    power = 0.0
    n = 0
    excluded = 0
    for r in classified:
        is_menkata = menkata_override.get(r["key"], r["auto_menkata"])
        if not is_menkata:
            excluded += 1
            continue
        rx = r["rx"]
        base = (rx / national_avg) if (rx and rx > 0) else 1.0
        w = (math.exp(-r["d_cand"] / competitor_decay_m)
             if competitor_decay_m and competitor_decay_m > 0 else 1.0)
        power += base * w
        n += 1
    return power, n, excluded


def compute_footfall_prediction(
    fp: FootfallParams, competitor_power: float
) -> Optional[Dict[str, float]]:
    """
    ②集客ベース：スーパー来店客のうち何割が処方箋を持ち込むかで枚数を見積もる。
    いただいた式（商圏人口は分母分子で消える）を年齢2区分に拡張：
        獲得 = [Σ_年齢(ユニーク客数_age × 月受診数_age × 12)] × 発行率 × 院外率
               × 当該薬局利用率 ÷ (競合面薬局パワー + 1)
    """
    if not fp.enabled or fp.unique_customers_monthly <= 0:
        return None
    u = fp.unique_customers_monthly
    u65 = u * fp.ratio_65plus
    u_under = u * (1.0 - fp.ratio_65plus)
    annual_visits = (u65 * fp.visits_month_65plus
                     + u_under * fp.visits_month_under65) * 12.0
    rx_pool = annual_visits * fp.issue_rate * fp.external_rate
    denom = competitor_power + 1.0
    total = rx_pool * fp.use_rate / denom if denom > 0 else 0.0
    return {
        "total": total, "annual_visits": annual_visits, "rx_pool": rx_pool,
        "denom": denom, "u65": u65, "u_under": u_under, "share": fp.use_rate / denom,
    }


OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]

# kikanCd 先頭桁 → kikanKbn の推定マッピング
KIKAN_KBN_MAP = {"1": [1, 2], "2": [2, 1], "3": [3, 2], "4": [4, 2], "5": [5, 2]}

OSM_SPECIALTY_MAP: Dict[str, str] = {
    "general": "一般内科", "general_practitioner": "一般内科",
    "internal_medicine": "一般内科", "internal": "一般内科",
    "cardiology": "循環器内科", "gastroenterology": "消化器内科",
    "diabetes": "糖尿病内科", "endocrinology": "糖尿病内科",
    "neurology": "神経内科", "pulmonology": "呼吸器内科",
    "surgery": "外科", "orthopaedics": "整形外科", "orthopedics": "整形外科",
    "dermatology": "皮膚科", "ophthalmology": "眼科",
    "otolaryngology": "耳鼻咽喉科", "ent": "耳鼻咽喉科",
    "psychiatry": "精神科", "mental_health": "精神科",
    "paediatrics": "小児科", "pediatrics": "小児科",
    "gynaecology": "産婦人科", "obstetrics": "産婦人科",
    "urology": "泌尿器科", "rehabilitation": "リハビリ科",
    "dentist": "歯科", "dental": "歯科",
}


# ─── データクラス ──────────────────────────────────────────────────────────────
@dataclass
class MedFacility:
    name: str
    address: str = ""
    href: str = ""
    pref_cd: str = ""
    kikan_cd: str = ""
    kikan_kbn: int = 2
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_m: Optional[float] = None
    source: str = "osm"
    inhouse_rx: str = "—"
    outpatient_rx: str = "—"
    rx_summary: str = "不明"
    daily_outpatients: Optional[int] = None
    daily_outpatients_source: str = "—"
    weekly_op_days: Optional[float] = None
    specialties: str = ""
    facility_category: str = "診療所"
    detail_fetched: bool = False
    detail_url: str = ""
    distance_note: str = ""
    raw_fields: Dict[str, str] = field(default_factory=dict)
    # ── 処方箋獲得予測（compute_capture_prediction が書き戻す） ──
    outpatient_manual: bool = False        # 外来患者数を手動上書きしたか
    annual_op_days_used: Optional[float] = None
    annual_op_visits: Optional[float] = None
    external_rx_factor: float = 1.0
    inflow_rate: float = 0.0
    inflow_band: str = ""
    captured_rx: Optional[float] = None
    # ── 門前占有チェック（既存の門前薬局に椅子を取られていないか） ──
    nearest_pharmacy_dist_m: Optional[float] = None
    nearest_pharmacy_name: str = ""
    monzen_occupied: bool = False          # このクリニックの門前(≤50m)に既存薬局あり
    monzen_contested: bool = False         # かつ候補地も門前バンド＝獲得が競合する
    # ── データ品質検証（get_facility_detail が書き込む） ──
    beds: Optional[int] = None             # 届出/許可病床数（合計）
    is_cosmetic: bool = False              # 美容・自由診療らしき施設（名称/診療科から判定）
    op_flag: str = ""                      # 外来患者数の異常値フラグ（空=正常）
    op_suggested: Optional[int] = None     # 年間値入力疑い時の補正候補（÷305）
    coord_source: str = ""                 # 座標の出典（ナビィ埋込 or ジオコーディング）
    # ── 商圏ポリゴン判定（apply_area_flags が書き込む） ──
    in_area: bool = True                   # 商圏内か（円形モードは常にTrue）


@dataclass
class PharmacyFacility:
    name: str
    address: str
    href: str = ""
    pref_cd: str = ""
    kikan_cd: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_m: Optional[float] = None
    source: str = "mhlw"
    pharmacy_type: str = "不明"
    nearest_clinic_name: str = "—"
    nearest_clinic_dist_m: Optional[float] = None
    annual_rx_count: Optional[int] = None
    annual_rx_source: str = "—"
    detail_fetched: bool = False
    detail_url: str = ""
    raw_fields: Dict[str, str] = field(default_factory=dict)
    in_area: bool = True                   # 商圏ポリゴン内か（円形モードは常にTrue）


# ─── ユーティリティ ────────────────────────────────────────────────────────────
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def name_similarity(a: str, b: str) -> float:
    a_chars = set(re.sub(r"[　\s・（）()「」]", "", a))
    b_chars = set(re.sub(r"[　\s・（）()「」]", "", b))
    if not a_chars or not b_chars:
        return 0.0
    return len(a_chars & b_chars) / max(len(a_chars), len(b_chars))


def guess_kikan_kbn(kikan_cd: str) -> List[int]:
    prefix = kikan_cd[0] if kikan_cd else "2"
    return KIKAN_KBN_MAP.get(prefix, [2, 1, 3])


# ─── 商圏ポリゴン（手描きエリア）ユーティリティ ─────────────────────────────────
# ポリゴンは [(lat, lon), ...] の頂点リスト。複数ポリゴン＝リストのリストで持つ。

def point_in_polygon(lat: float, lon: float, poly: List[Tuple[float, float]]) -> bool:
    """レイキャスティング法による内外判定（商圏スケールでは平面近似で十分）。"""
    if len(poly) < 3:
        return False
    x, y = lon, lat
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i][0], poly[i][1]
        yj, xj = poly[j][0], poly[j][1]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def point_in_any_polygon(
    lat: Optional[float], lon: Optional[float],
    polygons: List[List[Tuple[float, float]]],
) -> bool:
    if lat is None or lon is None:
        return False
    return any(point_in_polygon(lat, lon, p) for p in polygons)


def polygons_from_map_output(map_output: Optional[dict]) -> List[List[Tuple[float, float]]]:
    """st_folium の戻り値（all_drawings の GeoJSON）から頂点リスト群を取り出す。"""
    polys: List[List[Tuple[float, float]]] = []
    if not map_output:
        return polys
    for feat in (map_output.get("all_drawings") or []):
        try:
            geom = feat.get("geometry", {})
            if geom.get("type") != "Polygon":
                continue
            ring = geom.get("coordinates", [[]])[0]  # GeoJSONは[lon, lat]順
            pts = [(float(c[1]), float(c[0])) for c in ring if len(c) >= 2]
            if len(pts) >= 3:
                polys.append(pts)
        except (TypeError, ValueError, IndexError):
            continue
    return polys


def polygon_max_radius_m(
    center_lat: float, center_lon: float,
    polygons: List[List[Tuple[float, float]]],
) -> float:
    """住所（候補地）から全ポリゴン頂点への最大距離＝収集円の半径。"""
    dmax = 0.0
    for poly in polygons:
        for lat, lon in poly:
            dmax = max(dmax, haversine(center_lat, center_lon, lat, lon))
    return dmax


def polygons_area_km2(polygons: List[List[Tuple[float, float]]]) -> float:
    """ポリゴン群の概算面積（km²）。重心緯度での正距円筒近似＋靴紐公式。"""
    total = 0.0
    for poly in polygons:
        if len(poly) < 3:
            continue
        lat0 = sum(p[0] for p in poly) / len(poly)
        k_lat = 111_320.0                                   # 1度あたりm（南北）
        k_lon = 111_320.0 * math.cos(math.radians(lat0))    # 1度あたりm（東西）
        pts = [((lon * k_lon), (lat * k_lat)) for lat, lon in poly]
        s = 0.0
        j = len(pts) - 1
        for i in range(len(pts)):
            s += pts[j][0] * pts[i][1] - pts[i][0] * pts[j][1]
            j = i
        total += abs(s) / 2.0
    return total / 1_000_000.0


def apply_area_flags(
    med_facs: List["MedFacility"],
    pharmacies: List["PharmacyFacility"],
    polygons: List[List[Tuple[float, float]]],
    exclude_med_outside: bool,
) -> Tuple[int, int]:
    """
    ポリゴンで商圏内外フラグを付け直す。(圏外医療機関数, 圏外薬局数) を返す。
    ポリゴン未指定（円形モード）なら全て圏内。
    座標なしの施設は判定不能のため圏内扱い（予測には距離が必要なので実害なし）。
    """
    if not polygons:
        for f in med_facs:
            f.in_area = True
        for p in pharmacies:
            p.in_area = True
        return 0, 0
    n_med_out = n_ph_out = 0
    for f in med_facs:
        if f.lat is None or f.lon is None:
            f.in_area = True
            continue
        inside = point_in_any_polygon(f.lat, f.lon, polygons)
        f.in_area = inside if exclude_med_outside else True
        if not inside:
            n_med_out += 1
    for p in pharmacies:
        if p.lat is None or p.lon is None:
            p.in_area = True
            continue
        p.in_area = point_in_any_polygon(p.lat, p.lon, polygons)
        if not p.in_area:
            n_ph_out += 1
    return n_med_out, n_ph_out


# ─── Overpass ─────────────────────────────────────────────────────────────────
def _overpass_post(query: str, timeout: int = 40, retries: int = 2) -> Optional[dict]:
    for attempt in range(retries + 1):
        for url in OVERPASS_MIRRORS:
            try:
                r = requests.post(url, data={"data": query}, timeout=timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 503):
                    time.sleep(5 + attempt * 5)
                    break
            except requests.exceptions.Timeout:
                continue
            except Exception:
                continue
        if attempt < retries:
            time.sleep(3 + attempt * 3)
    return None


# ─── OSM 検索 ─────────────────────────────────────────────────────────────────
def _parse_osm_pharmacy_elements(
    elements: list, center_lat: float, center_lon: float
) -> List[PharmacyFacility]:
    pharmacies: List[PharmacyFacility] = []
    seen_ids: set = set()
    for el in elements:
        el_id = el.get("id")
        if el_id in seen_ids:
            continue
        seen_ids.add(el_id)
        tags = el.get("tags", {})
        name = tags.get("name:ja") or tags.get("name", "")
        branch = tags.get("branch", "")
        if branch and branch not in name:
            name = f"{name}{branch}"
        if not name:
            continue
        if el["type"] == "node":
            f_lat, f_lon = el.get("lat"), el.get("lon")
        else:
            center = el.get("center", {})
            f_lat, f_lon = center.get("lat"), center.get("lon")
        if f_lat is None or f_lon is None:
            continue
        addr_parts = [
            tags.get("addr:province", ""),
            tags.get("addr:city", "") or tags.get("addr:district", ""),
            tags.get("addr:suburb", ""),
            tags.get("addr:housenumber", ""),
        ]
        address = re.sub(r"\s+", "", "".join(p for p in addr_parts if p))
        dist = haversine(center_lat, center_lon, f_lat, f_lon)
        pharmacies.append(PharmacyFacility(
            name=name, address=address, source="osm",
            lat=f_lat, lon=f_lon, distance_m=dist,
        ))
    return pharmacies


def search_osm_pharmacies(lat: float, lon: float, radius_m: int) -> List[PharmacyFacility]:
    query = f"""
[out:json][timeout:50];
(
  node["amenity"="pharmacy"](around:{radius_m},{lat},{lon});
  way["amenity"="pharmacy"](around:{radius_m},{lat},{lon});
  node["shop"="chemist"](around:{radius_m},{lat},{lon});
  way["shop"="chemist"](around:{radius_m},{lat},{lon});
);
out center;
"""
    data = _overpass_post(query)
    if not data:
        return []
    result = _parse_osm_pharmacy_elements(data.get("elements", []), lat, lon)
    result.sort(key=lambda x: x.distance_m or 9_999_999)
    return result


def search_osm_medical(lat: float, lon: float, radius_m: int) -> List[MedFacility]:
    query = f"""
[out:json][timeout:40];
(
  node["amenity"~"^(clinic|hospital|doctors|medical_centre)$"](around:{radius_m},{lat},{lon});
  way["amenity"~"^(clinic|hospital|doctors|medical_centre)$"](around:{radius_m},{lat},{lon});
  node["healthcare"]["healthcare"!~"^(pharmacy|chemist|dispensary|yes)$"](around:{radius_m},{lat},{lon});
  way["healthcare"]["healthcare"!~"^(pharmacy|chemist|dispensary|yes)$"](around:{radius_m},{lat},{lon});
);
out center;
"""
    # 薬局キーワードフィルター（名前ベース）
    _PHARMA_RE = re.compile(
        r'薬局|ドラッグ|ファーマ|調剤|くすり|クスリ|drug\s*store|pharmacy', re.IGNORECASE
    )

    data = _overpass_post(query)
    if not data:
        return []
    facilities: List[MedFacility] = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name:ja") or tags.get("name", "")
        if not name:
            continue
        # 薬局・ドラッグストアを除外
        if _PHARMA_RE.search(name):
            continue
        if el["type"] == "node":
            f_lat, f_lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center", {})
            f_lat, f_lon = c.get("lat"), c.get("lon")
        if f_lat is None or f_lon is None:
            continue
        sp_en = (tags.get("healthcare:speciality", "")
                 or tags.get("speciality", "")
                 or tags.get("medical_system:western", "")).lower()
        sp_ja = OSM_SPECIALTY_MAP.get(sp_en, "")
        amenity = tags.get("amenity", "")
        healthcare = tags.get("healthcare", "")
        if amenity == "hospital" or healthcare == "hospital":
            cat = "病院"
        elif healthcare == "dentist" or "dentist" in sp_en:
            cat = "診療所"
            if not sp_ja:
                sp_ja = "歯科"
        else:
            cat = "診療所"

        addr_parts = [
            tags.get("addr:province", ""),
            tags.get("addr:city", "") or tags.get("addr:district", ""),
            tags.get("addr:suburb", ""),
            tags.get("addr:housenumber", ""),
        ]
        address = re.sub(r"\s+", "", "".join(p for p in addr_parts if p))
        dist = haversine(lat, lon, f_lat, f_lon)
        facilities.append(MedFacility(
            name=name, address=address, source="osm",
            lat=f_lat, lon=f_lon, distance_m=dist,
            specialties=sp_ja, facility_category=cat,
        ))
    facilities.sort(key=lambda x: x.distance_m or 9_999_999)
    return facilities


# ─── ジオコーダー ──────────────────────────────────────────────────────────────
class GeocoderService:
    GSI_URL       = "https://msearch.gsi.go.jp/address-search/AddressSearch"
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    HEADERS       = {"User-Agent": "AreaAnalysisTool/1.0"}
    LAT_MIN, LAT_MAX = 24.0, 46.0
    LON_MIN, LON_MAX = 122.0, 154.0

    def _is_japan(self, lat, lon) -> bool:
        return self.LAT_MIN <= lat <= self.LAT_MAX and self.LON_MIN <= lon <= self.LON_MAX

    def _clean(self, address: str) -> str:
        a = re.sub(r"〒\s*\d{3}[-−]\d{4}\s*", "", address)
        a = re.sub(r"Googleマップ.*", "", a).strip()
        trans = str.maketrans(
            "０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
            "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ－−‐",
            "0123456789abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ---",
        )
        a = a.translate(trans).replace("　", " ")
        a = re.sub(r"(\d+)\s*丁目\s*(\d+)\s*番地?\s*(\d+)\s*号?", r"\1-\2-\3", a)
        a = re.sub(r"(\d+)\s*丁目\s*(\d+)\s*番地?", r"\1-\2", a)
        a = re.sub(r"(\d+)\s*番地?\s*(\d+)\s*号", r"\1-\2", a)
        a = re.sub(r"(\d+)\s*番地", r"\1", a)
        return re.sub(r"\s+", " ", a).strip()

    def _shorten(self, address: str) -> str:
        a = re.sub(r"(\d+(?:[-]\d+)+)\s+[　-鿿＀-￯A-Za-z].+$", r"\1", address)
        if a != address:
            return a.strip()
        a = re.sub(r"\s*\d+\s*(?:階|[Ff]|号室|番地)\b.*$", "", address)
        a = re.sub(r"\s+[゠-ヿ]{3,}.*$", "", a)
        return a.strip()

    def _gsi(self, q: str) -> Optional[Tuple[float, float]]:
        try:
            r = requests.get(self.GSI_URL, params={"q": q}, headers=self.HEADERS, timeout=6)
            if r.status_code == 200:
                data = r.json()
                if data:
                    coords = data[0].get("geometry", {}).get("coordinates", [])
                    if len(coords) == 2:
                        lon, lat = float(coords[0]), float(coords[1])
                        if self._is_japan(lat, lon):
                            return lat, lon
        except Exception:
            pass
        return None

    def _nominatim(self, q: str) -> Optional[Tuple[float, float]]:
        try:
            r = requests.get(
                self.NOMINATIM_URL,
                params={"q": q + " 日本", "format": "json", "limit": 1},
                headers=self.HEADERS, timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
                    if self._is_japan(lat, lon):
                        return lat, lon
        except Exception:
            pass
        return None

    def geocode(self, address: str) -> Optional[Tuple[float, float]]:
        clean = self._clean(address)
        short = self._shorten(clean)
        has_short = short and short != clean
        result = self._gsi(clean)
        if result:
            return result
        if has_short:
            result = self._gsi(short)
            if result:
                return result
        time.sleep(1.0)
        result = self._nominatim(clean)
        if result:
            return result
        if has_short:
            time.sleep(0.5)
            result = self._nominatim(short)
            if result:
                return result
        return None

    def geocode_with_verification(
        self, address: str
    ) -> Tuple[Optional[Tuple[float, float]], str]:
        """GSI と Nominatim を両方試して結果を比較し、距離ノートを返す。"""
        clean = self._clean(address)
        short = self._shorten(clean)
        has_short = short and short != clean
        gsi_result = self._gsi(clean) or (self._gsi(short) if has_short else None)
        time.sleep(0.8)
        nom_result = self._nominatim(clean) or (self._nominatim(short) if has_short else None)
        if gsi_result and nom_result:
            diff = haversine(gsi_result[0], gsi_result[1], nom_result[0], nom_result[1])
            if diff <= 150:
                return gsi_result, f"確認済({diff:.0f}m差)"
            elif diff <= 400:
                return gsi_result, f"中程度({diff:.0f}m差)"
            else:
                return gsi_result, f"要確認({diff:.0f}m差・目視推奨)"
        elif gsi_result:
            return gsi_result, "GSIのみ取得"
        elif nom_result:
            return nom_result, "Nominatimのみ取得"
        return None, "取得失敗"

    def geocode_by_name(
        self, name: str, near_lat: float, near_lon: float, radius_km: float = 25
    ) -> Optional[Tuple[float, float]]:
        """施設名でNominatimをバウンディングボックス付き検索（住所不明時のフォールバック用）。"""
        delta = radius_km / 111.0
        viewbox = f"{near_lon - delta},{near_lat + delta},{near_lon + delta},{near_lat - delta}"
        try:
            r = requests.get(
                self.NOMINATIM_URL,
                params={
                    "q": name,
                    "format": "json",
                    "limit": 3,
                    "countrycodes": "jp",
                    "viewbox": viewbox,
                    "bounded": 1,
                },
                headers=self.HEADERS,
                timeout=8,
            )
            if r.status_code == 200:
                for item in r.json():
                    lat, lon = float(item["lat"]), float(item["lon"])
                    if self._is_japan(lat, lon):
                        return lat, lon
        except Exception:
            pass
        return None


# ─── フィールドパーサ群 ────────────────────────────────────────────────────────
def _get_field(fields: Dict[str, str], keys: List[str]) -> Optional[str]:
    for k in keys:
        if k in fields:
            return fields[k]
    for k in keys:
        for fk, fv in fields.items():
            if k in fk:
                return fv
    return None


def _infer_rx_type(full_text: str) -> str:
    lines = [line.strip() for line in full_text.split("\n") if line.strip()]
    for line in lines:
        if "院外処方" in line:
            if any(w in line for w in ["有り", "有", "あり", "可能", "実施"]):
                return "院外処方あり"
            if any(w in line for w in ["無し", "無", "なし", "不可"]):
                return "院内処方のみ"
        if "処方せん" in line or "処方箋" in line:
            if any(w in line for w in ["交付", "発行", "有"]):
                return "院外処方あり"
        if "院内処方" in line:
            if any(w in line for w in ["有り", "有", "あり"]):
                return "院内処方のみ"
    if "院外処方" in full_text or "処方せんの交付" in full_text:
        return "院外処方あり（推定）"
    return "不明"


def _extract_outpatient_from_table(table) -> Optional[int]:
    rows = table.find_all("tr")
    if not rows:
        return None
    header_cells = rows[0].find_all(["th", "td"])
    header_texts = [re.sub(r"\s+", "", c.get_text(strip=True)) for c in header_cells]
    gairaikan_col: Optional[int] = None
    for i, h in enumerate(header_texts):
        if h.startswith("外来患者") and "月平均" not in h:
            gairaikan_col = i
            break
    if gairaikan_col is not None and len(rows) >= 2:
        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            row_label = cells[0].get_text(strip=True)
            if "前年度" not in row_label:
                continue
            if gairaikan_col >= len(cells):
                continue
            val = cells[gairaikan_col].get_text(strip=True)
            if not val or re.fullmatch(r"[－\-−—―\s]*", val):
                continue
            m = re.search(r"(\d+\.?\d*)", val)
            if m:
                n = float(m.group(1))
                if 0 < n <= 10_000:
                    return int(round(n))
    outpatient_row_kw = ["外来患者", "外来数", "１日平均", "1日平均", "日平均外来"]
    for row in rows:
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        label = re.sub(r"\s+", "", cells[0].get_text(strip=True))
        if not any(kw in label for kw in outpatient_row_kw):
            continue
        if any(ex in label for ex in ["紹介", "入院", "在宅"]):
            continue
        # 多列行（label + 入院6列 + 外来 + 歯科 = 9セル以上）は外来列(cells[7])を指定。
        # 先頭から数値を拾うと入院列を誤取得するため（2026-07 実データ検証で確認）。
        scan_cells = [cells[7]] if len(cells) >= 9 else cells[1:]
        for cell in scan_cells:
            val = cell.get_text(strip=True)
            m = re.search(r"(\d+\.?\d*)", val)
            if m:
                n = float(m.group(1))
                if 0 < n <= 10_000:
                    return int(round(n))
    # 注: 旧実装にあった「任意の前年度行から最初の数値を拾う」汎用フォールバックは
    # 入院列を誤取得するリスクがあるため削除（同じ行は raw_fields 経由の
    # 「前年度フィールド・外来列」パスが正しい列位置で処理する）。
    return None


def _parse_outpatients_from_stats_table(soup: BeautifulSoup) -> Optional[int]:
    for item_div in soup.find_all("div", class_="item"):
        h3 = item_div.find("h3")
        if not h3 or "患者数" not in h3.get_text(strip=True):
            continue
        for table in item_div.find_all("table"):
            result = _extract_outpatient_from_table(table)
            if result is not None:
                return result
    for data_th in soup.find_all("th", class_="ptn4ItemName"):
        if "前年度" not in data_th.get_text(strip=True):
            continue
        table = data_th.find_parent("table")
        if table is None:
            continue
        result = _extract_outpatient_from_table(table)
        if result is not None:
            return result
    for table in soup.find_all("table"):
        result = _extract_outpatient_from_table(table)
        if result is not None:
            return result
    return None


def _parse_daily_outpatients(
    fields: Dict[str, str],
    full_text: str,
    soup: Optional[BeautifulSoup] = None,
) -> Tuple[Optional[int], str]:
    if soup is not None:
        result = _parse_outpatients_from_stats_table(soup)
        if result is not None:
            return result, "ナビィ（実績統計表）"
    v_zennen = fields.get("前年度１日平均患者数", "")
    if v_zennen:
        # 「前年度１日平均患者数」は多列（例: 396.3人/-/-/-/-/-/824人/-）。
        # 列は [入院各病床…, 外来, 歯科] の順で、外来は index=6（7列目）。
        # 旧実装の patient_vals[-2] はダッシュ列が findall で欠落すると
        # 入院列を誤取得したため、列位置を保持して外来列を選ぶよう修正。
        cells = [c.strip() for c in v_zennen.split("/")]
        def _num(cell: str) -> Optional[float]:
            m = re.search(r"(\d+\.?\d*)", cell)
            if not m:
                return None
            try:
                x = float(m.group(1))
            except ValueError:
                return None
            return x if 0 < x <= 10_000 else None
        # ① 外来列（index 6）を最優先
        if len(cells) >= 7:
            n = _num(cells[6])
            if n is not None:
                return int(round(n)), "ナビィ（前年度フィールド・外来列）"
        # ② 列数が想定外の場合は、数値を持つ末尾寄りの列を外来とみなす
        numeric = [(i, _num(c)) for i, c in enumerate(cells)]
        numeric = [(i, x) for i, x in numeric if x is not None]
        if numeric:
            # 歯科（最終列）を避けつつ、外来に相当する後方の列を採用
            idx, val = numeric[-1] if len(numeric) == 1 else numeric[-2] if len(cells) == len(numeric) else numeric[-1]
            if val is not None:
                return int(round(val)), "ナビィ（前年度フィールド）"
    candidate_keys = [
        "1日あたりの外来患者の平均数", "外来患者の平均数", "1日平均外来患者数",
        "一日平均外来患者数", "外来患者数（1日平均）", "前年度の１日平均外来患者数",
        "前年度１日平均外来患者数", "１日平均外来患者数", "1日あたり外来患者数",
        "外来（1日平均）", "外来患者の延数", "外来患者数",
    ]
    for k_target in candidate_keys:
        # 部分一致で「紹介を受けた外来患者数（月平均）」等の別指標を誤取得しないよう
        # 完全一致 → 除外語なしの部分一致 の順で自前ルックアップする
        v = fields.get(k_target)
        if v is None:
            for fk, fv in fields.items():
                if k_target in fk and not any(ng in fk for ng in ("紹介", "月平均", "在宅", "入院")):
                    v = fv
                    break
        if v:
            m_person = re.search(r"(\d+\.?\d*)\s*人", v)
            if m_person:
                n = float(m_person.group(1))
                if 0 < n <= 10_000:
                    return int(round(n)), f"ナビィ（{k_target}）"
            nums = re.findall(r"[0-9,]+\.?[0-9]*", v)
            for n_str in nums:
                try:
                    n = float(n_str.replace(",", ""))
                    if 1 <= n <= 3_000:
                        return int(round(n)), f"ナビィ（{k_target}）"
                    if n > 3_000:
                        return max(1, int(n / WORKING_DAYS)), f"ナビィ（{k_target}・年間÷305）"
                except ValueError:
                    pass
    # 注: \d は全角数字（例: ツールチップ内「１日平均」の「１」）にもマッチし
    # 誤って外来=1人を返していたため、テキスト解析は半角 [0-9] に限定する。
    for pat, label in [
        (r"1日あたりの外来患者の平均数[^0-9]{0,20}([0-9]{1,4})", "ナビィ（テキスト解析）"),
        (r"前年度の?１?日平均外来患者数[^0-9]{0,15}([0-9]{1,4})", "ナビィ（テキスト解析）"),
        (r"外来患者の平均数[^0-9]{0,15}([0-9]{1,4})", "ナビィ（テキスト解析）"),
        (r"1日平均外来患者数[^0-9]{0,15}([0-9]{1,4})", "ナビィ（テキスト解析）"),
        (r"外来患者[^0-9]{0,10}1日平均[^0-9]{0,10}([0-9]{1,4})", "ナビィ（テキスト解析）"),
        (r"外来[^0-9]{0,8}([0-9]{1,3})\s*人[/／]日", "ナビィ（テキスト解析）"),
        (r"前年度[^0-9]{0,20}外来[^0-9]{0,10}([0-9]{1,4}\.?[0-9]*)\s*人", "ナビィ（テキスト解析）"),
    ]:
        m = re.search(pat, full_text)
        if m:
            try:
                n = float(m.group(1).replace(",", ""))
                if 1 <= n <= 3_000:
                    return int(round(n)), label
            except ValueError:
                pass
    return None, "—"


def _parse_weekly_days(
    fields: Dict[str, str],
    full_text: str,
    soup: Optional[BeautifulSoup],
) -> Optional[float]:
    candidate_keys = [
        "週の診療日数", "週診療日数", "週あたり診療日数",
        "診療日数（週）", "平均診療日（週）", "診療日（週平均）",
    ]
    v = _get_field(fields, candidate_keys)
    if v:
        m = re.search(r"(\d+\.?\d*)\s*日", v)
        if m:
            n = float(m.group(1))
            if 0.5 <= n <= 7:
                return n
    schedule_keys = [
        "診療時間（診療科目別の）", "診療科目別の診療時間",
        "外来受付時間（診療科目別の）", "診療時間帯",
    ]
    sv = _get_field(fields, schedule_keys)
    if sv and "/" in sv:
        parts = [p.strip() for p in sv.split("/")]
        day_indices = set()
        for i, p in enumerate(parts[:8]):
            if p and p not in ["-", "−", "—", "休", "×"] and re.search(r"\d{1,2}:\d{2}", p):
                day_indices.add(i)
        if day_indices:
            return float(len(day_indices))
    if soup:
        days = _count_open_days_from_hours_table(soup)
        if days:
            return float(days)
    for pat in [
        r"週\s*(\d+\.?\d*)\s*日",
        r"週に平均\s*(\d+\.?\d*)\s*日",
        r"(\d+\.?\d*)\s*日[／/]週",
    ]:
        m = re.search(pat, full_text)
        if m:
            n = float(m.group(1))
            if 0.5 <= n <= 7:
                return n
    return None


def _count_open_days_from_hours_table(soup: BeautifulSoup) -> Optional[int]:
    WEEKDAY_CHARS = ["月", "火", "水", "木", "金", "土", "日"]
    open_days: set = set()
    for table in soup.find_all("table"):
        headers = []
        first_row = table.find("tr")
        if first_row:
            cells = first_row.find_all(["th", "td"])
            headers = [c.get_text(strip=True) for c in cells]
        header_days = []
        for i, h in enumerate(headers):
            for wd in WEEKDAY_CHARS:
                if wd in h:
                    header_days.append((i, wd))
        if header_days:
            for row in table.find_all("tr")[1:]:
                cells = row.find_all(["th", "td"])
                for col_i, wd in header_days:
                    if col_i < len(cells):
                        v = cells[col_i].get_text(strip=True)
                        if v and v not in ["×", "✗", "−", "-", "休", "—", ""]:
                            if not re.fullmatch(r"[×✗−\-休―‐ー]", v):
                                open_days.add(wd)
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            header = cells[0].get_text(strip=True)
            for wd in WEEKDAY_CHARS:
                if wd in header:
                    for cell in cells[1:]:
                        v = cell.get_text(strip=True)
                        if re.search(r"\d{1,2}:\d{2}", v):
                            open_days.add(wd)
    return len(open_days) if open_days else None


def _parse_specialties(fields: Dict[str, str], full_text: str) -> str:
    candidate_keys = ["診療科目", "診療科", "標榜診療科", "診療科名"]
    v = _get_field(fields, candidate_keys)
    if v:
        parts = re.split(r"[、。\n/／・]", v)
        sp_list = [p.strip() for p in parts if p.strip() and len(p.strip()) <= 15]
        return "、".join(sp_list[:6])
    return ""


def _parse_annual_rx_count(
    fields: Dict[str, str], full_text: str
) -> Tuple[Optional[int], str]:
    candidate_keys = [
        "処方箋受付回数（年間）", "処方箋受付枚数（年間）",
        "処方箋受付回数", "処方箋受付枚数",
        "調剤処方箋の受付枚数", "取扱処方箋数",
        "総取扱処方箋数", "年間処方箋受付回数",
        "年間処方箋取扱枚数", "処方箋枚数",
    ]
    for k in candidate_keys:
        v = None
        if k in fields:
            v = fields[k]
        else:
            for fk, fv in fields.items():
                if k in fk:
                    v = fv
                    break
        if v:
            m = re.search(r"([0-9,]+)", v)
            if m:
                try:
                    n = int(m.group(1).replace(",", ""))
                    if 100 <= n <= 10_000_000:
                        return n, f"ナビィ（{k}）"
                    if n == 0:
                        # 明示的な「0件」報告（漢方専門・OTC併設・調剤実績なし等）。
                        # 取得失敗(None)と区別して返す＝競合分析・実績照合で除外できる。
                        return 0, "ナビィ（報告0件）"
                except ValueError:
                    pass
    for pat, label in [
        (r"処方箋受付(?:回数|枚数)[^0-9]{0,10}([0-9,]+)", "ナビィ（テキスト解析）"),
        (r"取扱処方箋(?:数|枚)[^0-9]{0,10}([0-9,]+)", "ナビィ（テキスト解析）"),
        (r"処方箋[^0-9]{0,8}([0-9,]+)\s*(?:回|枚|件)", "ナビィ（テキスト解析）"),
    ]:
        m = re.search(pat, full_text)
        if m:
            try:
                n = int(m.group(1).replace(",", ""))
                if 100 <= n <= 10_000_000:
                    return n, label
            except ValueError:
                pass
    return None, "—"


# ─── データ品質検証ヘルパー ─────────────────────────────────────────────────────
def _extract_coords_from_html(html: str) -> Optional[Tuple[float, float]]:
    """
    ナビィ詳細ページに埋め込まれた正確な緯度経度を抽出する（地図リンク q=lat,lon）。
    住所ジオコーディング（誤差±30〜80m）より高精度で、門前50m判定の信頼性が大きく上がる。
    """
    m = re.search(r"q=([2-4][0-9]\.[0-9]{3,}),\s*(1[23][0-9]\.[0-9]{3,})", html)
    if not m:
        m = re.search(r"([2-4][0-9]\.[0-9]{4,})\s*,\s*(1[23][0-9]\.[0-9]{4,})", html)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if 24.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0:  # 日本国内チェック
            return lat, lon
    return None


def _parse_total_beds(fields: Dict[str, str]) -> Optional[int]:
    """「届出又は許可病床数」フィールドから合計病床数（最終列）を取る。"""
    for k, v in fields.items():
        if k.startswith("届出又は許可病床数"):
            nums = re.findall(r"([0-9]+)床", v)
            if nums:
                return int(nums[-1])
    return None


# 美容・自由診療らしき施設（保険処方箋がほぼ発生しない）の名称/診療科パターン。
# 誤検出を避けるため明白なキーワードに限定（例:「形成外科」単体は保険診療なので含めない）。
_COSMETIC_RE = re.compile(
    r"美容外科|美容皮膚|美容クリニック|美容医療|ＡＧＡ|AGA|スキンクリニック"
    r"|脱毛|アートメイク|植毛|メンズライフ|包茎", re.IGNORECASE
)


def _detect_cosmetic(name: str, specialties: str) -> bool:
    return bool(_COSMETIC_RE.search(name or "") or _COSMETIC_RE.search(specialties or ""))


def _validate_outpatients(fac: "MedFacility") -> Tuple[str, Optional[int]]:
    """
    取得した外来患者数の妥当性を検証し (フラグ文字列, 補正候補値) を返す。空文字=正常。

    実データ検証（2026-07・3エリア46施設）で確認された誤報告パターン:
      - 年間値が1日欄に入力（例: 外来列=13,736人 → 実際は約45人/日）
      - 月間値らしき高値（診療所で700人/日超）
    補正候補は年間値÷305日で算出（週診療日数は誤登録がありうるため固定日数を使う）。
    """
    # raw多列フィールドの外来列（キャップで弾かれた大きな生値も見る）
    raw6: Optional[float] = None
    raw = fac.raw_fields.get("前年度１日平均患者数", "") if fac.raw_fields else ""
    if raw:
        cells = [c.strip() for c in raw.split("/")]
        if len(cells) >= 7:
            m = re.search(r"([0-9]+\.?[0-9]*)", cells[6])
            if m:
                raw6 = float(m.group(1))

    op = fac.daily_outpatients

    # 年間値入力疑い: 生値が2,000超で、÷305が現実的な1日患者数に収まる
    if raw6 is not None and raw6 > 2_000:
        est = raw6 / WORKING_DAYS
        if 5 <= est <= 600:
            return (f"年間値入力疑い（外来列の生値={raw6:,.0f}）", int(round(est)))

    if op is None:
        return ("", None)

    if fac.facility_category == "病院":
        beds = fac.beds or 0
        if op > max(2_500, beds * 6):
            return ("過大値疑い（病床規模と不整合）", None)
        return ("", None)

    # 診療所
    if op >= 1_000:
        est = op / WORKING_DAYS
        if 5 <= est <= 600:
            return ("年間値入力疑い", int(round(est)))
        return ("過大値疑い", None)
    if op >= 700:
        return ("高値・要確認（月間値の可能性）", None)
    return ("", None)


# ─── MHLWスクレイパー ──────────────────────────────────────────────────────────
class MHLWScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        self._ready = False

    def _init(self) -> bool:
        if self._ready:
            return True
        try:
            r = self.session.get(f"{MHLW_BASE}/juminkanja/S2320/initialize", timeout=15)
            self._ready = r.status_code == 200
        except Exception:
            self._ready = False
        return self._ready

    def search_pharmacies_by_latlon(
        self,
        lat: float, lon: float,
        radius_m: int,
        center_name: str = "",
        max_pages: int = 8,
    ) -> Tuple[List[PharmacyFacility], str]:
        """ナビィ薬局タブ（S2300/yakkyokuSearch）で薬局を緯度経度検索する。"""
        if not self._init():
            return [], "MHLW接続エラー"
        dist_code = "00" if radius_m <= 1_000 else ("01" if radius_m <= 5_000 else "")
        cn = urllib.parse.quote(center_name or "検索地点")
        try:
            self.session.get(f"{MHLW_BASE}/juminkanja/S2300/initializeYakk", timeout=12)
            r1 = self.session.get(
                f"{MHLW_BASE}/juminkanja/S2300/yakkyokuSearch",
                params={
                    "iyakuKbn": "2", "lang": "ja",
                    "latitude": str(lat), "longitude": str(lon),
                    "distanceFromCenterPoint": dist_code,
                    "centerPointName": cn,
                    "selectCenterPoint": "3",
                    "specifyDateAndTime": "01",
                    "XCHARSET": "utf-8",
                },
                timeout=15,
            )
            j = r1.json()
            if j.get("code") != "0":
                return [], f"薬局ナビィエラー: {j.get('messages')}"
            search_id = j["result"]["id"]
            self.session.get(
                f"{MHLW_BASE}/juminkanja/S2300/yakkyokuSearch",
                params={
                    "id": search_id,
                    "latitude": str(lat), "longitude": str(lon),
                    "distanceFromCenterPoint": dist_code,
                    "selectCenterPoint": "3",
                    "specifyDateAndTime": "01",
                    "XCHARSET": "utf-8",
                },
                timeout=15,
            )
        except Exception as e:
            return [], f"薬局ナビィ例外: {e}"

        all_ph: List[PharmacyFacility] = []
        total = 0
        for page in range(max_pages):
            try:
                r3 = self.session.get(
                    f"{MHLW_BASE}/juminkanja/S2400/initialize",
                    params={"id": search_id, "page": page, "size": 20, "sortNo": 2},
                    timeout=15,
                )
                if r3.status_code != 200:
                    break
                phs, t = self._parse_pharmacy_list(r3.text)
                if page == 0:
                    total = t
                if not phs:
                    break
                all_ph.extend(phs)
                if len(all_ph) >= total:
                    break
                time.sleep(0.3)
            except Exception:
                break

        dist_str = f"{radius_m // 1000}km" if radius_m >= 1000 else f"{radius_m}m"
        return all_ph, f"ナビィ薬局: {dist_str}圏内 全{total}件 / 取得{len(all_ph)}件"

    def _parse_pharmacy_list(self, html: str) -> Tuple[List[PharmacyFacility], int]:
        soup = BeautifulSoup(html, "html.parser")
        results: List[PharmacyFacility] = []
        total = 0
        m = re.search(r"(\d{1,6})\s*件", soup.get_text())
        if m:
            total = int(m.group(1))
        for item in soup.select("div.resultItems div.item"):
            h3 = item.find("h3", class_="name")
            if not h3:
                continue
            link = h3.find("a", href=True)
            if not link:
                continue
            name = link.get_text(strip=True)
            href = link.get("href", "")
            if href.startswith("/"):
                href = MHLW_DOMAIN + href
            pref_cd = re.search(r"prefCd=(\d+)", href)
            kikan_cd = re.search(r"kikanCd=(\w+)", href)
            pref_cd  = pref_cd.group(1)  if pref_cd  else ""
            kikan_cd = kikan_cd.group(1) if kikan_cd else ""
            raw_text = item.get_text(separator=" ", strip=True)
            addr_m = re.search(r"〒\s*[\d-]+\s+(.+?)(?:Googleマップ|$)", raw_text)
            address = addr_m.group(1).strip() if addr_m else ""
            results.append(PharmacyFacility(
                name=name, address=address, href=href,
                pref_cd=pref_cd, kikan_cd=kikan_cd, source="mhlw",
            ))
        return results, total

    def search_medical_by_latlon(
        self,
        lat: float, lon: float,
        radius_m: int,
        center_name: str = "",
        max_pages: int = 6,
    ) -> Tuple[List[MedFacility], str]:
        """ナビィ S2320 → S2400 で医療機関（病院・診療所）を検索する。"""
        if not self._init():
            return [], "MHLW接続エラー"
        dist_code = "00" if radius_m <= 1_000 else ("01" if radius_m <= 5_000 else "")
        try:
            self.session.get(f"{MHLW_BASE}/juminkanja/S2320/initsearch", timeout=12)
            r2 = self.session.get(
                f"{MHLW_BASE}/juminkanja/S2320/search",
                params={
                    "specifyDateAndTime": "01",
                    "centerPointName": urllib.parse.quote(center_name or "検索地点"),
                    "latitude": str(lat), "longitude": str(lon),
                    "selectCenterPoint": "",
                    "distanceFromCenterPoint": dist_code,
                    "medicalCare": ["1", "2"],
                    "searchTypes": "01-2",
                },
                timeout=15,
            )
            j = r2.json()
            if j.get("code") != "0":
                return [], f"MHLW search エラー: {j.get('messages')}"
            redirect_url = j["result"]["redirectUrl"]
        except Exception as e:
            return [], f"MHLW search 例外: {e}"

        all_facs: List[MedFacility] = []
        total = 0
        for page in range(max_pages):
            try:
                sep = "&" if "?" in redirect_url else "?"
                r3 = self.session.get(
                    f"{redirect_url}{sep}page={page}&size=20&sortNo=2", timeout=15
                )
                if r3.status_code != 200:
                    break
                facs, t = self._parse_med_list(r3.text)
                if page == 0:
                    total = t
                if not facs:
                    break
                all_facs.extend(facs)
                if len(all_facs) >= total:
                    break
                time.sleep(0.3)
            except Exception:
                break
        dist_str = f"{radius_m // 1000}km" if radius_m >= 1000 else f"{radius_m}m"
        return all_facs, f"MHLW医療機関: {dist_str}圏内 全{total}件/取得{len(all_facs)}件"

    def _parse_med_list(self, html: str) -> Tuple[List[MedFacility], int]:
        """S2400 医療機関一覧HTMLからMedFacilityリストを生成する（hrefからpref_cd/kikan_cd/kikan_kbn抽出）。"""
        soup = BeautifulSoup(html, "html.parser")
        results: List[MedFacility] = []
        total = 0
        m = re.search(r"(\d{1,6})\s*件", soup.get_text())
        if m:
            total = int(m.group(1))
        for item in soup.find_all("div", class_="item"):
            h3 = item.find("h3", class_="name")
            if not h3:
                continue
            link = h3.find("a", href=True)
            if not link:
                continue
            name = link.get_text(strip=True)
            if not name:
                continue
            href = link.get("href", "")
            if href.startswith("/"):
                href = MHLW_DOMAIN + href
            qp = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(href).query))
            pref_cd   = qp.get("prefCd", "")
            kikan_cd  = qp.get("kikanCd", "")
            kikan_kbn = int(qp.get("kikanKbn", "2"))
            results.append(MedFacility(
                name=name, source="mhlw",
                pref_cd=pref_cd, kikan_cd=kikan_cd, kikan_kbn=kikan_kbn,
            ))
        return results, max(total, len(results))

    def get_facility_detail(self, fac: MedFacility) -> bool:
        """
        MHLW 詳細ページを取得・パース。
        住所取得 + 院内外処方・外来患者数・診療日数を同時取得。
        """
        self._init()
        if not (fac.pref_cd and fac.kikan_cd):
            return False
        known_kbn = fac.kikan_kbn
        kbn_list = [known_kbn] + [k for k in guess_kikan_kbn(fac.kikan_cd) if k != known_kbn]
        soup = None
        used_kbn = None
        for kbn in kbn_list:
            url = (f"{MHLW_BASE}/juminkanja/S2430/initialize"
                   f"?prefCd={fac.pref_cd}&kikanCd={fac.kikan_cd}&kikanKbn={kbn}")
            try:
                r = self.session.get(url, timeout=12)
                if r.status_code != 200:
                    continue
                candidate_soup = BeautifulSoup(r.text, "html.parser")
                text = candidate_soup.get_text()
                if "E-0109" in text or "データは存在しません" in text:
                    continue
                if fac.name[:4] in text or len(text) > 50_000:
                    soup = candidate_soup
                    used_kbn = kbn
                    fac.detail_url = url
                    fac.kikan_kbn = kbn
                    raw_html = r.text
                    break
            except Exception:
                continue
        if soup is None:
            return False

        # ── 座標（ナビィ埋込の正確な緯度経度を最優先） ─────────────────────
        coords = _extract_coords_from_html(raw_html)
        if coords:
            fac.lat, fac.lon = coords
            fac.coord_source = "ナビィ埋込座標"

        # ── 全 tr/dl フィールドを収集 ──────────────────────────────────────
        all_fields: Dict[str, str] = {}
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True)
                v = " / ".join(c.get_text(strip=True) for c in cells[1:] if c.get_text(strip=True))
                if k and v:
                    all_fields[k] = v
        for dl in soup.find_all("dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                k = dt.get_text(strip=True)
                v = dd.get_text(strip=True)
                if k and v:
                    all_fields[k] = v
        fac.raw_fields = all_fields
        full_text = soup.get_text(separator="\n", strip=True)

        # ── 住所取得（まだ空の場合のみ） ───────────────────────────────────
        if not fac.address:
            for row in soup.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    key = cells[0].get_text(strip=True)
                    if re.search(r"所在地|住所", key):
                        val = cells[1].get_text(" ", strip=True)
                        val = re.sub(r"〒\s*\d{3}[-－]\d{4}\s*", "", val).strip()
                        val = re.sub(r"\s+", " ", val).strip()
                        if val:
                            fac.address = val[:120]
                            break
            if not fac.address:
                m = re.search(r"〒\s*[\d-]+\s+(.+?)(?:Tel|TEL|電話|Googleマップ|\n|$)", full_text)
                if m:
                    addr = re.sub(r"\s+", " ", m.group(1)).strip()
                    if addr:
                        fac.address = addr[:120]

        # ── 施設カテゴリ ──────────────────────────────────────────────────
        if used_kbn == 1:
            fac.facility_category = "病院"
        elif used_kbn == 3:
            fac.facility_category = "歯科診療所"
        elif "病院" in fac.name:
            fac.facility_category = "病院"

        # ── 院内処方 / 院外処方 ───────────────────────────────────────────
        inhouse = _get_field(all_fields, [
            "院内処方の有無", "院内処方", "調剤（院内処方）", "院内調剤",
        ])
        outpatient = _get_field(all_fields, [
            "院外処方の有無", "院外処方", "調剤（院外処方）", "院外調剤",
            "処方せんの交付", "処方箋の交付",
        ])
        fac.inhouse_rx    = inhouse    or "—"
        fac.outpatient_rx = outpatient or "—"
        if outpatient and "有" in outpatient:
            fac.rx_summary = "院外処方あり"
        elif inhouse and "有" in inhouse and (not outpatient or "無" in outpatient or "不可" in outpatient):
            fac.rx_summary = "院内処方のみ"
        elif inhouse or outpatient:
            fac.rx_summary = f"院内:{inhouse or '—'} / 院外:{outpatient or '—'}"
        else:
            fac.rx_summary = _infer_rx_type(full_text)

        # ── 1日平均外来患者数 ────────────────────────────────────────────
        fac.daily_outpatients, fac.daily_outpatients_source = \
            _parse_daily_outpatients(all_fields, full_text, soup)

        # 歯科診療所は外来列(index6)が空で歯科列(index7)に患者数が入る
        if fac.daily_outpatients is None and used_kbn == 3:
            raw = all_fields.get("前年度１日平均患者数", "")
            cells = [c.strip() for c in raw.split("/")] if raw else []
            if len(cells) >= 8:
                m = re.search(r"([0-9]+\.?[0-9]*)", cells[7])
                if m:
                    n = float(m.group(1))
                    if 0 < n <= 1_000:
                        fac.daily_outpatients = int(round(n))
                        fac.daily_outpatients_source = "ナビィ（歯科患者列）"

        # ── 週診療日数 ───────────────────────────────────────────────────
        fac.weekly_op_days = _parse_weekly_days(all_fields, full_text, soup)
        # 妥当性クランプ: 週8日等の解析ミス→7日 / 外来数十人規模なのに週1-2日は
        # 診療時間表の解析ミスの可能性が高い→欠測扱い（固定日数フォールバック）
        if fac.weekly_op_days and fac.weekly_op_days > 7:
            fac.weekly_op_days = 7.0
        if (fac.weekly_op_days and fac.weekly_op_days <= 2
                and (fac.daily_outpatients or 0) >= 30):
            fac.weekly_op_days = None

        # ── 診療科目 ─────────────────────────────────────────────────────
        if not fac.specialties:
            fac.specialties = _parse_specialties(all_fields, full_text)

        # ── データ品質検証 ────────────────────────────────────────────────
        fac.beds = _parse_total_beds(all_fields)
        fac.is_cosmetic = _detect_cosmetic(fac.name, fac.specialties)
        fac.op_flag, fac.op_suggested = _validate_outpatients(fac)

        fac.detail_fetched = True
        return True

    def get_pharmacy_detail(self, ph: PharmacyFacility) -> bool:
        """ナビィ薬局詳細ページから総取扱処方箋数を取得する。"""
        self._init()
        if not (ph.pref_cd and ph.kikan_cd):
            return False
        url = (f"{MHLW_BASE}/juminkanja/S2430/initialize"
               f"?prefCd={ph.pref_cd}&kikanCd={ph.kikan_cd}&kikanKbn=5")
        try:
            r = self.session.get(url, timeout=12)
            if r.status_code != 200:
                return False
            soup = BeautifulSoup(r.text, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
            if "E-0109" in text or "データは存在しません" in text:
                return False
        except Exception:
            return False

        ph.detail_url = url

        # 座標（ナビィ埋込の正確な緯度経度があればジオコーディング値より優先）
        coords = _extract_coords_from_html(r.text)
        if coords:
            ph.lat, ph.lon = coords

        all_fields: Dict[str, str] = {}
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True)
                v = " / ".join(c.get_text(strip=True) for c in cells[1:] if c.get_text(strip=True))
                if k and v:
                    all_fields[k] = v
        for dl in soup.find_all("dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                k, v = dt.get_text(strip=True), dd.get_text(strip=True)
                if k and v:
                    all_fields[k] = v
        ph.raw_fields = all_fields
        ph.annual_rx_count, ph.annual_rx_source = _parse_annual_rx_count(all_fields, text)
        ph.detail_fetched = True
        return True


# ─── 門前/面 判定 ──────────────────────────────────────────────────────────────
def assign_monzen_to_pharmacies(
    pharmacies: List[PharmacyFacility],
    med_facilities: List[MedFacility],
    threshold_m: float = 50.0,
) -> List[str]:
    """各薬局に最近接の医療機関を割り当て、閾値以内なら門前薬局と判定する。"""
    debug: List[str] = []
    facs_with_coords = [f for f in med_facilities if f.lat is not None and f.lon is not None]
    debug.append(
        f"門前判定: 薬局={len(pharmacies)}件 "
        f"医療機関(座標あり)={len(facs_with_coords)}件 閾値={threshold_m:.0f}m"
    )
    for ph in pharmacies:
        if ph.lat is None or ph.lon is None:
            ph.pharmacy_type = "不明"
            continue
        best_dist = float("inf")
        best_fac: Optional[MedFacility] = None
        for fac in facs_with_coords:
            d = haversine(ph.lat, ph.lon, fac.lat, fac.lon)
            if d < best_dist:
                best_dist = d
                best_fac = fac
        ph.nearest_clinic_dist_m = best_dist if best_fac else None
        ph.nearest_clinic_name   = best_fac.name if best_fac else "—"
        if best_fac and best_dist <= threshold_m:
            ph.pharmacy_type = "門前薬局"
            debug.append(f"  [門前] {ph.name[:20]} → {best_fac.name[:20]} ({best_dist:.0f}m)")
        elif best_fac:
            ph.pharmacy_type = "面薬局"
            debug.append(
                f"  [面] {ph.name[:20]} → 最近接: {best_fac.name[:20]} {best_dist:.0f}m > {threshold_m:.0f}m"
            )
        else:
            ph.pharmacy_type = "不明"
            debug.append(f"  [不明] {ph.name[:20]} → 医療機関データなし")
    return debug


# ─── キャッシュ ────────────────────────────────────────────────────────────────
@st.cache_resource
def get_scraper() -> MHLWScraper:
    return MHLWScraper()


@st.cache_resource
def get_geocoder() -> GeocoderService:
    return GeocoderService()


# ─── メイン分析処理 ────────────────────────────────────────────────────────────
def run_analysis(
    address: str,
    radius_m: int,
    gate_m: int,
    max_detail: int,
    log: List[str],
    prog,
    assumptions: Optional[PredictionAssumptions] = None,
    polygons: Optional[List[List[Tuple[float, float]]]] = None,
    exclude_outside_med: bool = True,
) -> Tuple[List[MedFacility], List[PharmacyFacility], float, float]:
    scraper = get_scraper()
    geocoder = get_geocoder()

    # ─────────────────────────────────────────────────────────────────────
    # Phase 1: 初回データ収集
    # ─────────────────────────────────────────────────────────────────────

    # Step 1: 住所ジオコーディング → 中心座標
    prog.progress(3, text="Step1: 住所をジオコーディング中…")
    t0 = time.time()
    coords = geocoder.geocode(address)
    if not coords:
        st.error(f"住所「{address}」の座標取得に失敗しました。より詳細な住所を入力してください。")
        st.stop()
    center_lat, center_lon = coords
    log.append(
        f"[Step1] ジオコーディング完了: lat={center_lat:.5f}, lon={center_lon:.5f} "
        f"({time.time()-t0:.1f}s)"
    )

    # Step 2: OSM 薬局検索
    prog.progress(8, text="Step2: OSMから薬局を取得中…")
    t0 = time.time()
    time.sleep(2)
    ph_osm = search_osm_pharmacies(center_lat, center_lon, radius_m)
    ph_merged: List[PharmacyFacility] = list(ph_osm)
    log.append(f"[Step2] OSM薬局: {len(ph_osm)}件取得 ({time.time()-t0:.1f}s)")

    # Step 3: OSM 医療機関検索
    prog.progress(14, text="Step3: OSMから医療機関を取得中…")
    t0 = time.time()
    med_radius = radius_m + gate_m
    time.sleep(2)
    med_osm = search_osm_medical(center_lat, center_lon, med_radius)
    log.append(f"[Step3] OSM医療機関: {med_radius}m圏内 {len(med_osm)}件 ({time.time()-t0:.1f}s)")
    if len(med_osm) == 0:
        log.append("[Step3] ⚠️ OSM医療機関0件 → ナビィデータのみで処理します")

    # Step 4: ナビィ薬局リスト取得 → 住所geocoding → OSMとマージ
    prog.progress(20, text="Step4: ナビィから薬局リストを取得中…")
    t0 = time.time()
    navvi_phs, navvi_ph_msg = scraper.search_pharmacies_by_latlon(
        center_lat, center_lon, radius_m=radius_m,
        center_name=address[:20], max_pages=8,
    )
    log.append(f"[Step4] {navvi_ph_msg}")
    existing_names = [p.name for p in ph_merged]
    added_navvi_ph = 0
    for i, nph in enumerate(navvi_phs):
        if i % 5 == 0:
            prog.progress(20, text=f"Step4: ナビィ薬局 座標取得中 {i+1}/{len(navvi_phs)}件…")
        is_dup = any(name_similarity(nph.name, en) >= 0.65 for en in existing_names)
        if is_dup:
            for osm_ph in ph_merged:
                if name_similarity(nph.name, osm_ph.name) >= 0.65 and not osm_ph.pref_cd:
                    osm_ph.pref_cd  = nph.pref_cd
                    osm_ph.kikan_cd = nph.kikan_cd
                    osm_ph.href     = nph.href
            continue
        if nph.address:
            gc = geocoder.geocode(nph.address)
            if gc:
                nph.lat, nph.lon = gc
                nph.distance_m = haversine(center_lat, center_lon, nph.lat, nph.lon)
                if nph.distance_m > radius_m * 1.1:
                    time.sleep(0.15)
                    continue
            time.sleep(0.15)
        ph_merged.append(nph)
        existing_names.append(nph.name)
        added_navvi_ph += 1
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    no_coord_ph = sum(1 for p in ph_merged if p.lat is None)
    log.append(
        f"[Step4] ナビィ固有追加: {added_navvi_ph}件 合計: {len(ph_merged)}件 "
        f"（座標なし: {no_coord_ph}件） ({time.time()-t0:.1f}s)"
    )

    # Step 5: ナビィ医療機関リスト取得 → get_facility_detail で住所+詳細取得 → geocoding → OSMとマージ
    prog.progress(30, text="Step5: ナビィから医療機関リストを取得中…")
    t0 = time.time()
    navvi_meds, med_msg = scraper.search_medical_by_latlon(
        center_lat, center_lon, radius_m=med_radius,
        center_name=address[:20], max_pages=6,
    )
    log.append(f"[Step5] {med_msg}")

    # 薬局名フィルター（ナビィ医療機関検索に薬局が混入する場合を除外）
    _PHARMA_NAME_RE = re.compile(
        r'薬局|ドラッグ|ファーマ|調剤|くすり|クスリ|drug\s*store|pharmacy', re.IGNORECASE
    )
    med_existing_names = [f.name for f in med_osm]
    med_existing_kikan_cds = {f.kikan_cd for f in med_osm if f.kikan_cd}
    med_targets = [
        f for f in navvi_meds
        if f.pref_cd and f.kikan_cd
        and f.kikan_kbn != 5                          # kikanKbn=5は薬局
        and not _PHARMA_NAME_RE.search(f.name)        # 名前ベースでも除外
        and f.kikan_cd not in med_existing_kikan_cds
        and not any(name_similarity(f.name, en) >= 0.65 for en in med_existing_names)
    ][:50]

    geocode_ok, geocode_fail, detail_fail = 0, 0, 0
    for i, nmf in enumerate(med_targets):
        prog.progress(
            30 + int(20 * i / max(len(med_targets), 1)),
            text=f"Step5: 医療機関 詳細+住所取得中 {i+1}/{len(med_targets)}件: {nmf.name[:15]}…",
        )
        # get_facility_detail で住所取得 + 詳細データを同時取得
        ok = scraper.get_facility_detail(nmf)
        if not ok:
            detail_fail += 1
        if nmf.lat is not None:
            # 詳細ページのナビィ埋込座標（高精度）を取得済 → ジオコーディング不要
            nmf.distance_m = haversine(center_lat, center_lon, nmf.lat, nmf.lon)
            geocode_ok += 1
        elif nmf.address:
            gc = geocoder.geocode(nmf.address)
            if gc:
                nmf.lat, nmf.lon = gc
                nmf.distance_m = haversine(center_lat, center_lon, nmf.lat, nmf.lon)
                geocode_ok += 1
            else:
                geocode_fail += 1
        else:
            geocode_fail += 1
        med_osm.append(nmf)
        med_existing_names.append(nmf.name)
        if nmf.kikan_cd:
            med_existing_kikan_cds.add(nmf.kikan_cd)
        time.sleep(0.2)

    med_osm.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(
        f"[Step5] 医療機関詳細+住所取得: 成功={geocode_ok}件 "
        f"詳細失敗={detail_fail}件 geocoding失敗={geocode_fail}件 "
        f"合計={len(med_osm)}件 ({time.time()-t0:.1f}s)"
    )

    # ─────────────────────────────────────────────────────────────────────
    # Phase 2: 推考フェーズ（必須・スキップ不可）
    # ─────────────────────────────────────────────────────────────────────

    # Step 6: 【医療機関 漏れ確認】ナビィ再検索
    prog.progress(52, text="Step6（推考①）: 医療機関 漏れ確認中…")
    t0 = time.time()
    existing_med_kikan_cds = {f.kikan_cd for f in med_osm if f.kikan_cd}
    existing_med_names     = [f.name for f in med_osm]
    verify_meds, _ = scraper.search_medical_by_latlon(
        center_lat, center_lon, radius_m=med_radius,
        center_name=address[:20], max_pages=6,
    )
    added_med = 0
    for vf in verify_meds:
        if vf.kikan_cd and vf.kikan_cd in existing_med_kikan_cds:
            continue
        if any(name_similarity(vf.name, en) >= 0.65 for en in existing_med_names):
            continue
        if not (vf.pref_cd and vf.kikan_cd):
            continue
        ok = scraper.get_facility_detail(vf)
        if vf.lat is not None:
            vf.distance_m = haversine(center_lat, center_lon, vf.lat, vf.lon)
        elif vf.address:
            gc = geocoder.geocode(vf.address)
            if gc:
                vf.lat, vf.lon = gc
                vf.distance_m = haversine(center_lat, center_lon, vf.lat, vf.lon)
        vf.source = "mhlw(推考①追加)"
        med_osm.append(vf)
        existing_med_names.append(vf.name)
        if vf.kikan_cd:
            existing_med_kikan_cds.add(vf.kikan_cd)
        added_med += 1
        time.sleep(0.2)
    log.append(
        f"[Step6] 推考①: 医療機関 {added_med}件追加 "
        f"（再検索{len(verify_meds)}件確認） ({time.time()-t0:.1f}s)"
    )

    # Step 7: 【薬局 漏れ確認】ナビィ再検索
    prog.progress(62, text="Step7（推考②）: 薬局 漏れ確認中…")
    t0 = time.time()
    existing_ph_kikan_cds = {p.kikan_cd for p in ph_merged if p.kikan_cd}
    existing_ph_names     = [p.name for p in ph_merged]
    verify_phs, _ = scraper.search_pharmacies_by_latlon(
        center_lat, center_lon, radius_m=radius_m,
        center_name=address[:20], max_pages=8,
    )
    added_ph = 0
    for vph in verify_phs:
        if vph.kikan_cd and vph.kikan_cd in existing_ph_kikan_cds:
            continue
        if any(name_similarity(vph.name, en) >= 0.65 for en in existing_ph_names):
            continue
        if vph.address:
            gc = geocoder.geocode(vph.address)
            if gc:
                vph.lat, vph.lon = gc
                vph.distance_m = haversine(center_lat, center_lon, vph.lat, vph.lon)
                if vph.distance_m > radius_m * 1.1:
                    time.sleep(0.15)
                    continue
            time.sleep(0.15)
        vph.source = "mhlw(推考②追加)"
        ph_merged.append(vph)
        existing_ph_names.append(vph.name)
        if vph.kikan_cd:
            existing_ph_kikan_cds.add(vph.kikan_cd)
        added_ph += 1
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(
        f"[Step7] 推考②: 薬局 {added_ph}件追加 "
        f"（再検索{len(verify_phs)}件確認） ({time.time()-t0:.1f}s)"
    )

    # Step 8: 【距離整合性チェック】
    prog.progress(70, text="Step8（推考③）: 距離整合性チェック中…")
    t0 = time.time()
    warnings_med = 0
    for fac in med_osm:
        if fac.lat is not None and fac.lon is not None:
            actual_dist = haversine(center_lat, center_lon, fac.lat, fac.lon)
            fac.distance_m = actual_dist
            if actual_dist > med_radius + 500:
                warnings_med += 1
                log.append(
                    f"[Step8] ⚠️ 圏外疑い(医療): {fac.name[:20]} "
                    f"距離={actual_dist:.0f}m > {med_radius + 500}m"
                )

    removed_ph = 0
    ph_filtered: List[PharmacyFacility] = []
    for ph in ph_merged:
        if ph.distance_m is not None and ph.distance_m > radius_m * 1.1:
            log.append(f"[Step8] 除外(薬局距離超過): {ph.name[:20]} {ph.distance_m:.0f}m")
            removed_ph += 1
        else:
            ph_filtered.append(ph)
    ph_merged = ph_filtered
    log.append(
        f"[Step8] 推考③: 距離整合性チェック完了 "
        f"医療機関警告={warnings_med}件 薬局除外={removed_ph}件 ({time.time()-t0:.1f}s)"
    )

    # ─────────────────────────────────────────────────────────────────────
    # Phase 3: 詳細取得 & 判定
    # ─────────────────────────────────────────────────────────────────────

    # Step 9: 薬局詳細ページから年間処方箋数取得
    prog.progress(74, text="Step9: 薬局詳細（年間処方箋数）を取得中…")
    t0 = time.time()
    ph_targets = [p for p in ph_merged if p.pref_cd and p.kikan_cd and not p.detail_fetched][:max_detail]
    log.append(f"[Step9] 薬局詳細取得対象: {len(ph_targets)}件")
    for i, ph in enumerate(ph_targets):
        prog.progress(
            74 + int(18 * i / max(len(ph_targets), 1)),
            text=f"Step9: 薬局詳細取得中 ({i+1}/{len(ph_targets)}): {ph.name[:20]}…",
        )
        scraper.get_pharmacy_detail(ph)
        time.sleep(0.5)
    fetched_ph = sum(1 for p in ph_merged if p.detail_fetched)
    # 詳細ページで座標が更新された薬局の距離を再計算（門前判定の精度向上）
    for ph in ph_merged:
        if ph.lat is not None and ph.lon is not None:
            ph.distance_m = haversine(center_lat, center_lon, ph.lat, ph.lon)
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(f"[Step9] 薬局詳細取得完了: {fetched_ph}件 ({time.time()-t0:.1f}s)")

    # Step 10: 門前/面 判定
    prog.progress(93, text="Step10: 門前/面 判定中…")
    t0 = time.time()
    debug_lines = assign_monzen_to_pharmacies(ph_merged, med_osm, threshold_m=float(gate_m))
    log.extend(debug_lines)
    n_monzen = sum(1 for p in ph_merged if p.pharmacy_type == "門前薬局")
    n_men    = sum(1 for p in ph_merged if p.pharmacy_type == "面薬局")
    log.append(
        f"[Step10] 門前/面判定完了: 門前={n_monzen}件 面={n_men}件 "
        f"不明={len(ph_merged)-n_monzen-n_men}件 ({time.time()-t0:.1f}s)"
    )

    # Step 11: 門前占有チェック（各クリニックの最近接 既存薬局距離を計算）
    prog.progress(95, text="Step11: 門前占有チェック中…")
    compute_pharmacy_proximity(med_osm, ph_merged)

    # Step 11.5: 商圏ポリゴンの内外判定（ポリゴンモードのみ）
    if polygons:
        n_med_out, n_ph_out = apply_area_flags(
            med_osm, ph_merged, polygons, exclude_outside_med
        )
        log.append(
            f"[Step11.5] 商圏ポリゴン判定: ポリゴン{len(polygons)}個 "
            f"圏外医療機関={n_med_out}件 圏外薬局={n_ph_out}件 "
            f"（医療機関の寄与除外: {'ON' if exclude_outside_med else 'OFF'}）"
        )

    # Step 12: 処方箋獲得予測（候補地点＝商圏中心 が獲得する年間処方箋枚数）
    prog.progress(96, text="Step12: 処方箋獲得予測を計算中…")
    a = assumptions or PredictionAssumptions()
    summary = compute_capture_prediction(med_osm, a)
    log.append(
        f"[Step12] 処方箋獲得予測: 年間 {summary['total_annual_rx']:,.0f} 枚 "
        f"（寄与医療機関={summary['n_contributing']}件 / "
        f"外来数なし={summary['n_no_outpatient']}件 / "
        f"門前競合={summary.get('n_contested_monzen', 0)}件）"
    )

    # Step 12: 結果をsession_stateに保存（呼び出し元で保存）
    med_osm.sort(key=lambda x: x.distance_m or 9_999_999)
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(
        f"[完了] 医療機関={len(med_osm)}件 薬局={len(ph_merged)}件 "
        f"（座標あり医療機関: {sum(1 for f in med_osm if f.lat is not None)}件）"
    )
    return med_osm, ph_merged, center_lat, center_lon


# ─── セッション初期化 ──────────────────────────────────────────────────────────
_defaults = {
    "med_results":    [],
    "ph_results":     [],
    "center_lat":     None,
    "center_lon":     None,
    "search_log":     [],
    "last_address":   "",
    "area_polygons":  [],
    "collected_radius": None,
    "searched_polygon_mode": False,
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─── UI ───────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .rx-cards{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 4px;}
  .rx-card{flex:1;min-width:220px;background:#ffffff;border:1px solid #e2e8e6;
    border-radius:12px;padding:18px 20px;box-shadow:0 1px 3px rgba(16,26,23,.05);}
  .rx-card.med{border-top:4px solid #0f766e;}
  .rx-card.foot{border-top:4px solid #b45309;}
  .rx-card.range{border-top:4px solid #334155;background:#f8fafa;}
  .rx-label{font-size:13px;font-weight:700;color:#475569;letter-spacing:.02em;margin-bottom:6px;}
  .rx-val{font-size:30px;font-weight:700;color:#16211e;line-height:1.1;
    font-variant-numeric:tabular-nums;}
  .rx-val small{font-size:14px;font-weight:600;color:#64748b;margin-left:4px;}
  .rx-sub{font-size:14px;color:#475569;margin-top:6px;font-variant-numeric:tabular-nums;}
  .rx-muted{color:#94a3b8;font-weight:600;}
  section[data-testid="stSidebar"] .stExpander{border:none;}
  h3{margin-top:.4rem;}
</style>
""", unsafe_allow_html=True)
st.title("🎯 Prescription Analysis v2 — 処方箋獲得予測")
st.caption("Version 260702 v2（商圏: 円形／ポリゴン両対応）")
st.caption(
    "住所（＝出店候補地）を入力し、商圏を「円形（半径指定）」または「ポリゴン（地図をなぞる）」で指定すると、  \n"
    "圏内の医療機関・薬局を一覧表示し、**その地点に薬局を出した場合に獲得できる年間処方箋枚数を予測**します。  \n"
    "データソース: **厚生労働省ナビィ** + **OpenStreetMap** + **国土地理院**"
)

# ── サイドバー ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("検索条件")
    address_input = st.text_input(
        "住所（＝出店候補地）",
        placeholder="例：山梨県中央市若宮50-1",
        help="丁目・番地まで入力すると精度が上がります。予測はこの地点を基準に計算します",
    )
    area_mode = st.radio(
        "商圏の指定方法",
        ["円形（半径指定）", "ポリゴン（地図をなぞる）"],
        index=0,
        help="ポリゴン: クライアントにもらった商圏図を見ながら、地図上で商圏の形を"
             "多角形でなぞって指定します（複数の多角形も可）",
    )
    is_polygon_mode = area_mode.startswith("ポリゴン")
    exclude_outside_med = True
    if not is_polygon_mode:
        radius_m = st.slider(
            "商圏半径 (m)", min_value=200, max_value=5000, value=1000, step=100,
            help="この半径内の施設を検索します",
        )
    else:
        radius_m = None  # ポリゴンから自動算出（検索実行時）
        exclude_outside_med = st.checkbox(
            "ポリゴン外の医療機関を寄与から除外", value=True,
            help="ON: 商圏ポリゴンの外側にあるクリニックは獲得予測に算入しません"
                 "（川・線路等で分断されたエリアを外す用途）。"
                 "OFF: ポリゴンは表示・薬局集計のみに使い、予測は距離のみで計算",
        )
        st.caption("⬇️ 下のメイン画面の地図で商圏をなぞってから「検索実行」")
    gate_m = st.slider(
        "門前判定距離 (m)", min_value=10, max_value=300, value=50, step=10,
        help="薬局から医療機関までの距離がこの値以内なら「門前薬局」と判定します",
    )
    max_detail = st.slider(
        "詳細取得件数（薬局）", min_value=5, max_value=50, value=30, step=5,
        help="ナビィから年間処方箋数を取得する薬局の上限件数（時間に影響します）",
    )

    st.divider()
    st.subheader("🎯 予測アサンプション")
    st.caption("処方箋獲得予測に使う前提。検索後もタブ内で再計算できます。")

    with st.expander("流入率（距離帯別）", expanded=True):
        st.caption("医療機関の外来患者のうち、この地点の薬局に処方箋を持ち込む割合")
        r_monzen = st.number_input("〜50m（門前）",   0.0, 1.0, 0.570, 0.005, format="%.3f")
        r_500    = st.number_input("50m〜500m",       0.0, 1.0, 0.070, 0.005, format="%.3f")
        r_1k     = st.number_input("500m〜1km",       0.0, 1.0, 0.050, 0.005, format="%.3f")
        r_2k     = st.number_input("1km〜2km",        0.0, 1.0, 0.012, 0.001, format="%.3f")
        r_3k     = st.number_input("2km〜3km",        0.0, 1.0, 0.004, 0.001, format="%.3f")
        st.caption("※ 3km超は流入0%")
        b_500 = st.number_input("帯②上限(m)",   100, 900, 500, 50)
        b_1k  = st.number_input("帯③上限(m)",  600, 1500, 1000, 50)
        b_2k  = st.number_input("帯④上限(m)", 1100, 2500, 2000, 100)
        b_3k  = st.number_input("帯⑤上限(m)", 2100, 4000, 3000, 100)

    with st.expander("年間診療日数・処方係数", expanded=False):
        days_mode = st.radio(
            "年間診療日数の算出", ["週診療日数×52", "固定日数"], index=0,
            help="医療機関ごとの週診療日数×52週。取得できない施設は固定日数を使用",
        )
        fixed_days = st.number_input("固定日数（フォールバック）", 200, 365, WORKING_DAYS, 5)
        f_gairai  = st.number_input(
            "院外処方あり 係数", 0.0, 1.0, 1.0, 0.05,
            help=(
                "原則1.0のまま変更不要。\n\n"
                "流入率（門前0.570 等）は、当社が運営する調剤薬局の実績"
                "（＝門前クリニックの外来患者数のうち、実際に何割がうちの薬局に"
                "処方箋を持ち込んだか）をベンチマークに算出した経験値です。\n\n"
                "この0.570に「処方箋が発行される率」も既に織り込まれているため、"
                "外来患者数へ直接掛ければよく、院外処方ありの施設は係数1.0で"
                "二重掛けせずに計算します。"
            ),
        )
        st.caption(
            "💡 院外あり=1.0：流入率は当社薬局の実績（外来患者数→自局への処方箋"
            "持込割合）から算出済のため、外来患者数に直接掛けます（発行率も織込済）。"
        )
        f_inhouse = st.number_input("院内処方のみ 係数", 0.0, 1.0, 0.0, 0.05,
                                    help="院内処方のみ＝外部薬局に処方箋が出ないため原則0")
        f_unknown = st.number_input("院内外 不明 係数", 0.0, 1.0, 1.0, 0.05,
                                    help="院内外が判定できない施設の扱い（安全側なら下げる）")
        f_cosmetic = st.number_input(
            "美容・自由診療 係数", 0.0, 1.0, 0.0, 0.05,
            help="美容外科・AGA・脱毛等（名称/診療科から自動判定）は保険処方箋が"
                 "ほぼ発生しないため原則0。実データ検証で都市部の過大予測要因と確認済み。",
        )
        f_dental = st.number_input(
            "歯科診療所 係数", 0.0, 1.0, 0.05, 0.01,
            help="歯科は外来1回あたりの処方箋発行率が医科より大幅に低い"
                 "（全国統計ベースで概ね5%前後）ため、外来患者数に本係数を掛けて算入。",
        )
        issue_rate = st.number_input(
            "処方箋発行率", 0.0, 1.5, 1.0, 0.05,
            help=(
                "原則1.0。流入率(0.570等)が既に発行率を織り込んだ実績値のため、"
                "ここで別途掛ける必要はありません。多段で分解したい場合のみ調整。"
            ),
        )

    with st.expander("門前占有チェック", expanded=False):
        st.caption(
            "候補地の門前(≤50m)に入るクリニックで、既に別の薬局が門前に張り付いて"
            "いる場合、門前レート0.570は過大評価になりえます。該当時のみ⚠️を表示します。"
        )
        discount_contested = st.checkbox(
            "門前が埋まっているクリニックを面レートに引下げて計算", value=False,
            help=(
                "ONにすると、門前競合クリニックの流入率を門前(0.570)→面(第2帯0.070)に"
                "自動で落として保守的に予測します。OFFなら計算はそのまま・警告表示のみ。"
            ),
        )

    with st.expander("🛒 集客ベース予測（スーパー併設：来店客数から予測）", expanded=False):
        st.caption(
            "スーパー等の来店客のうち何割が処方箋を持ち込むかで枚数を見積もります。"
            "ここに数字を入れると、検索後の「🎯処方箋獲得予測」タブに②集客ベースが併記されます。"
        )
        store_format = st.selectbox("店舗形態", list(FORMAT_PRESETS.keys()), index=1)
        _preset = FORMAT_PRESETS[store_format]
        cust_mode = st.radio(
            "来店客数の入力方法",
            ["会員ユニーク数（月間）", "POSレジ客数（月間）"], index=0,
            help="会員データがあれば会員ユニーク数を、なければPOS客数÷平均来店回数でユニーク化します。",
        )
        if cust_mode.startswith("会員"):
            member_unique = st.number_input("月間ユニーク会員数（人）", 0, 2_000_000, 0, 500)
            ff_visit_freq = st.number_input("平均来店回数（参考・この方法では未使用）",
                                            1.0, 20.0, float(_preset["visit_freq"]), 0.5)
            ff_unique_month = int(member_unique)
        else:
            pos_month = st.number_input("月間POSレジ客数（延べ人数）", 0, 5_000_000, 0, 500)
            ff_visit_freq = st.number_input("平均来店回数（回/月）", 1.0, 20.0,
                                            float(_preset["visit_freq"]), 0.5,
                                            help="店舗により異なるため調整可。既定は4回。")
            ff_unique_month = int(pos_month / ff_visit_freq) if ff_visit_freq > 0 else 0
            st.caption(f"→ 換算 月間ユニーク客数 ≈ **{ff_unique_month:,} 人**")
        ff_r65 = st.number_input("65歳以上の比率", 0.0, 1.0, float(_preset["r65"]), 0.01,
                                 help="会員の年齢構成があればその値、なければ商圏の高齢化率。")
        cc1, cc2 = st.columns(2)
        ff_v65 = cc1.number_input("65+ の月受診回数", 0.0, 6.0, 3.0, 0.1)
        ff_vu65 = cc2.number_input("65− の月受診回数", 0.0, 6.0, 1.3, 0.1)
        st.caption("係数（処方箋発行率／院外処方率／当該薬局利用率）")
        d1, d2, d3 = st.columns(3)
        ff_issue = d1.number_input("発行率", 0.0, 1.0, 0.8054, 0.0001, format="%.4f")
        ff_ext   = d2.number_input("院外率", 0.0, 1.0, 0.8313, 0.0001, format="%.4f")
        ff_use   = d3.number_input("利用率", 0.0, 1.0, 0.137, 0.001, format="%.3f")
        st.caption("面競合の抽出（門前・メイン薬局は分母から除外）")
        e1, e2 = st.columns(2)
        ff_monzen_dist = e1.number_input(
            "門前距離(m)＝自動判定のしきい値", 0, 300, 50, 10,
            help="最寄りクリニックがこの距離以内の薬局を『門前』と自動判定します（面競合から除外）。"
                 "自動判定は後から予測タブの一覧で1店ずつ目視修正できます。",
        )
        ff_main_rx = e2.number_input(
            "メイン薬局しきい値(枚/年)", 0, 100000, 15000, 1000,
            help="年間実績がこの値以上の薬局は特定クリニックのメイン薬局とみなし、面競合から除外します。"
                 "0で無効。実績が取得できている競合だけに適用されます。",
        )
        ff_decay = st.number_input(
            "面競合の距離減衰 λ(m)", 0, 3000, 1000, 100,
            help="面競合を候補地からの距離で減衰させます（exp(−距離/λ)）。近い競合ほど強く、"
                 "遠い面薬局はスーパー客の選択肢に入りにくいので弱く数えます。"
                 "小さいほど遠い競合を無視＝自店シェアが上がる。0で従来どおり全店フラット。",
        )

    with st.expander("⚙️ 詳細設定（ハフ按分・通常は変更不要）", expanded=False):
        st.caption(
            "医療機関ベースの競合按分（ハフ）の内部パラメータです。面型275店の検証で"
            "較正済みの既定値のままで問題ありません。通常は触らなくて構いません。"
        )
        huff_enabled = st.checkbox("ハフ競合按分を使う", value=True)
        huff_lambda = st.slider("距離減衰 λ (m)", 150, 900, 250, 50)
        huff_boost = st.slider("門前ブースト", 1.0, 15.0, 8.0, 0.5)
        huff_cand_A = st.number_input(
            "候補店の引力（集客力/規模）", 0.2, 10.0, 1.0, 0.1,
            help="大型スーパー併設など集客が強い候補は上げると医療機関ベースの取り分が増えます。",
        )
        huff_power = st.checkbox("競合薬局を実績枚数で引力加重する", value=False)
        huff_natl = st.number_input("全国平均 年間処方箋枚数（基準）", 5000, 30000, 12000, 500)

    run_btn = st.button("検索実行", type="primary", use_container_width=True)
    st.divider()
    st.caption(
        "データソース: 厚労省ナビィ / OpenStreetMap / 国土地理院\n\n"
        "※ 初回検索は2〜5分かかります"
    )

# サイドバー入力から予測アサンプションを構築
_assumptions = PredictionAssumptions(
    bands=[
        (50.0,       r_monzen),
        (float(b_500), r_500),
        (float(b_1k),  r_1k),
        (float(b_2k),  r_2k),
        (float(b_3k),  r_3k),
    ],
    annual_days_mode="weekly" if days_mode == "週診療日数×52" else "fixed",
    fixed_annual_days=int(fixed_days),
    external_factor_gairai=f_gairai,
    external_factor_inhouse=f_inhouse,
    external_factor_unknown=f_unknown,
    issue_rate=issue_rate,
    discount_contested_monzen=discount_contested,
    cosmetic_factor=f_cosmetic,
    dental_factor=f_dental,
)
st.session_state["assumptions"] = _assumptions

# 医療機関ベース（ハフ）のパラメータ
_huff = HuffParams(
    enabled=huff_enabled,
    lambda_m=float(huff_lambda),
    monzen_boost=float(huff_boost),
    monzen_radius=float(gate_m),
    candidate_attractiveness=float(huff_cand_A),
    weight_by_power=huff_power,
    national_avg_rx=float(huff_natl),
)
st.session_state["huff"] = _huff

# 集客ベース（来店客数）のパラメータ
_footfall = FootfallParams(
    enabled=(ff_unique_month > 0),
    store_format=store_format,
    unique_customers_monthly=float(ff_unique_month),
    ratio_65plus=float(ff_r65),
    visits_month_65plus=float(ff_v65),
    visits_month_under65=float(ff_vu65),
    issue_rate=float(ff_issue),
    external_rate=float(ff_ext),
    use_rate=float(ff_use),
    national_avg_rx=float(huff_natl),
    menkata_monzen_dist=float(ff_monzen_dist),
    menkata_main_rx=float(ff_main_rx),
    competitor_decay_m=float(ff_decay),
)
st.session_state["footfall"] = _footfall


# ── 商圏ポリゴン描画（ポリゴンモードのみ表示） ─────────────────────────────────
@st.cache_data(show_spinner=False, ttl=3600)
def _geocode_cached(addr: str) -> Optional[Tuple[float, float]]:
    return GeocoderService().geocode(addr)


if is_polygon_mode:
    st.subheader("🖊️ ① 商圏ポリゴンを描く")
    st.caption(
        "クライアントにもらった商圏図を見ながら、下の地図で商圏の形をなぞってください。  \n"
        "左側の **⬟（五角形）アイコン** で頂点をクリックしていき、始点をクリックして閉じます。"
        "複数の多角形も描けます（ゴミ箱アイコンで削除）。描き終えたらサイドバーの「検索実行」へ。"
    )
    draw_center = None
    if st.session_state.get("center_lat") is not None:
        draw_center = (st.session_state.center_lat, st.session_state.center_lon)
    elif address_input.strip():
        draw_center = _geocode_cached(address_input.strip())
        if draw_center is None:
            st.warning("住所の座標が取得できませんでした。より詳細な住所を入力してください。")
    if draw_center:
        dm = folium.Map(location=list(draw_center), zoom_start=14)
        folium.Marker(
            location=list(draw_center),
            tooltip=f"出店候補地: {address_input.strip() or st.session_state.get('last_address','')}",
            icon=folium.Icon(color="blue", icon="home", prefix="fa"),
        ).add_to(dm)
        Draw(
            export=False,
            draw_options={
                "polygon": {"shapeOptions": {"color": "#2E7D32", "fillOpacity": 0.10}},
                "polyline": False, "rectangle": True, "circle": False,
                "marker": False, "circlemarker": False,
            },
            edit_options={"edit": True, "remove": True},
        ).add_to(dm)
        draw_out = st_folium(dm, use_container_width=True, height=430, key="area_draw_map")
        drawn = polygons_from_map_output(draw_out)
        if drawn:
            st.session_state["area_polygons"] = drawn
        polys_now = st.session_state.get("area_polygons", [])
        if polys_now:
            area_km2 = polygons_area_km2(polys_now)
            r_need = polygon_max_radius_m(draw_center[0], draw_center[1], polys_now)
            st.info(
                f"✅ ポリゴン {len(polys_now)}個 / 面積 約{area_km2:.2f} km² / "
                f"収集半径（自動）約{r_need:,.0f}m"
            )
        else:
            st.warning("まだポリゴンが描かれていません。")
    else:
        st.warning(
            "🗺️ 地図を表示するには、まず **左サイドバーの「住所（＝出店候補地）」に住所を入力**してください。  \n"
            "ポリゴンはこの候補地を中心に描くため、住所（座標）が必要です。"
            "住所を入れると、ここに描画用の地図が表示されます。"
        )
else:
    # 円形モードに切り替えたらポリゴンは使わない（データは残すが適用しない）
    pass


# ── 検索実行 ──────────────────────────────────────────────────────────────────
if run_btn and address_input.strip():
    polygons_for_run: List[List[Tuple[float, float]]] = (
        st.session_state.get("area_polygons", []) if is_polygon_mode else []
    )
    if is_polygon_mode and not polygons_for_run:
        st.error("ポリゴンモードです。先に上の地図で商圏をなぞってから検索してください。")
    else:
        # ポリゴンモードは「候補地からポリゴン全頂点を包含する円」で収集する
        radius_use = radius_m
        if is_polygon_mode:
            c = _geocode_cached(address_input.strip())
            if c is None:
                st.error("住所の座標取得に失敗しました。")
                st.stop()
            radius_use = int(min(5_000, max(500, polygon_max_radius_m(c[0], c[1], polygons_for_run) * 1.05)))
        log: List[str] = []
        prog = st.progress(0, text="検索を開始しています…")
        try:
            med_list, ph_list, clat, clon = run_analysis(
                address_input.strip(), radius_use, gate_m, max_detail, log, prog,
                assumptions=_assumptions,
                polygons=polygons_for_run,
                exclude_outside_med=exclude_outside_med,
            )
            st.session_state.med_results  = med_list
            st.session_state.ph_results   = ph_list
            st.session_state.center_lat   = clat
            st.session_state.center_lon   = clon
            st.session_state.search_log   = log
            st.session_state.last_address = address_input.strip()
            st.session_state.collected_radius = radius_use
            st.session_state.searched_polygon_mode = is_polygon_mode
            prog.progress(100, text="完了!")
            time.sleep(0.3)
            prog.empty()
        except Exception as e:
            prog.empty()
            st.error(f"検索中にエラーが発生しました: {e}")
elif run_btn:
    st.warning("住所を入力してください。")

# ── 結果表示 ──────────────────────────────────────────────────────────────────
if st.session_state.med_results or st.session_state.ph_results:
    center_lat = st.session_state.center_lat
    center_lon = st.session_state.center_lon

    # 手動で追加/削除した施設を反映した「実効リスト」を組み立てる（漏れの補完用）
    _manual_med: List[MedFacility]       = st.session_state.setdefault("manual_med", [])
    _manual_ph:  List[PharmacyFacility]  = st.session_state.setdefault("manual_ph", [])
    _del_med: set = st.session_state.setdefault("deleted_med_keys", set())
    _del_ph:  set = st.session_state.setdefault("deleted_ph_keys", set())
    for _f in _manual_med:  # 手動追加分の候補地からの距離を算出
        if _f.lat is not None and center_lat is not None:
            _f.distance_m = haversine(center_lat, center_lon, _f.lat, _f.lon)
    for _p in _manual_ph:
        if _p.lat is not None and center_lat is not None:
            _p.distance_m = haversine(center_lat, center_lon, _p.lat, _p.lon)
    med_facs:  List[MedFacility]      = [
        f for f in st.session_state.med_results if facility_key(f) not in _del_med
    ] + _manual_med
    pharmacies: List[PharmacyFacility] = [
        p for p in st.session_state.ph_results if pharmacy_key(p) not in _del_ph
    ] + _manual_ph
    collected_radius = st.session_state.get("collected_radius") or radius_m or 1000

    # 現在のポリゴンで内外フラグを再適用（描き直しに即応・円形モードなら全て圏内）
    active_polygons = (
        st.session_state.get("area_polygons", []) if is_polygon_mode else []
    )
    apply_area_flags(med_facs, pharmacies, active_polygons, exclude_outside_med)

    # 描き直したポリゴンが収集済みの円をはみ出したら再検索を促す
    if active_polygons and center_lat is not None:
        r_need_now = polygon_max_radius_m(center_lat, center_lon, active_polygons)
        if r_need_now > collected_radius * 1.05:
            st.warning(
                f"⚠️ ポリゴンが収集済み範囲（半径{collected_radius:,.0f}m）を超えています"
                f"（必要半径 約{r_need_now:,.0f}m）。はみ出した部分の施設はデータに含まれて"
                "いないため、「検索実行」で再収集してください。"
            )

    if active_polygons:
        st.success(
            f"**{st.session_state.last_address}** の商圏ポリゴン"
            f"（{len(active_polygons)}個・約{polygons_area_km2(active_polygons):.2f}km²）内: "
            f"医療機関 **{sum(1 for f in med_facs if f.in_area)}件** / "
            f"薬局 **{sum(1 for p in pharmacies if p.in_area)}件** "
            f"（収集半径{collected_radius:,.0f}m・圏外を含む収集総数: "
            f"医療{len(med_facs)}件/薬局{len(pharmacies)}件）"
        )
    else:
        st.success(
            f"**{st.session_state.last_address}** の {collected_radius:,.0f}m 商圏内: "
            f"医療機関 **{len(med_facs)}件** / 薬局 **{len(pharmacies)}件** が見つかりました"
        )

    tab_pred, tab_calib, tab_med, tab_ph, tab_map, tab_log = st.tabs(
        ["🎯 処方箋獲得予測", "🧪 実績照合", "🏥 医療機関", "💊 薬局", "🗺️ 統合地図", "📝 ログ"]
    )

    # ─── タブ⓪「🎯 処方箋獲得予測」 ───────────────────────────────────────
    with tab_pred:
        a: PredictionAssumptions = st.session_state.get("assumptions", PredictionAssumptions())

        # ── 外来患者数の手動上書き（大病院はHP/年報から補完） ──────────────
        with st.expander("✏️ 外来患者数を手動補完（大病院などナビィ未報告・要修正の施設）"):
            st.caption(
                "ナビィで外来患者数が「なし」または実態と乖離する施設は、病院HP・病院年報の"
                "1日平均外来患者数をここで上書きできます（空欄＝ナビィ値を使用）。"
            )
            edit_rows = []
            for f in sorted(med_facs, key=lambda x: x.distance_m or 9e9):
                edit_rows.append({
                    "医療機関": f.name,
                    "距離(m)": int(f.distance_m) if f.distance_m is not None else None,
                    "ナビィ外来(人/日)": f.daily_outpatients,
                    "検証": f.op_flag or "",
                    "補正候補": f.op_suggested,
                    "上書き外来(人/日)": st.session_state.get("op_overrides", {}).get(facility_key(f)),
                    "_key": facility_key(f),
                })
            df_edit = pd.DataFrame(edit_rows)
            edited = st.data_editor(
                df_edit, hide_index=True, use_container_width=True, key="op_editor",
                disabled=["医療機関", "距離(m)", "ナビィ外来(人/日)", "検証", "補正候補", "_key"],
                column_config={
                    "上書き外来(人/日)": st.column_config.NumberColumn(
                        "上書き外来(人/日)", min_value=0, max_value=10000, step=1,
                        help="病院HP等の1日平均外来患者数を入力",
                    ),
                },
            )
            overrides = {}
            for _, row in edited.iterrows():
                v = row.get("上書き外来(人/日)")
                if v is not None and not pd.isna(v) and float(v) > 0:
                    overrides[row["_key"]] = float(v)
            st.session_state["op_overrides"] = overrides

        # ── 医療機関・薬局を手動で追加／削除（ナビィの漏れを補完） ──────────────
        with st.expander("➕ 医療機関・薬局を手動で追加／削除（ナビィの漏れを補完）", expanded=False):
            st.caption(
                "ナビィに無い／誤りの施設を手動で追加・削除できます。"
                "追加・削除は予測（ハフ・集客・面競合の分母）にそのまま反映されます。"
            )
            mtab, ptab = st.tabs(["🏥 医療機関", "💊 薬局"])
            with mtab:
                with st.form("add_med", clear_on_submit=True):
                    st.markdown("**医療機関を追加**")
                    cM = st.columns(3)
                    m_name = cM[0].text_input("名称 *")
                    m_addr = cM[1].text_input("住所（座標を自動取得）")
                    m_rxsum = cM[2].selectbox("院内外区分", ["院外処方あり", "院内処方のみ", "不明"])
                    cM2 = st.columns(3)
                    m_op = cM2[0].number_input("外来患者数(人/日)", 0, 10000, 0, 10)
                    m_wk = cM2[1].number_input("週診療日数(0=固定日数)", 0.0, 7.0, 0.0, 0.5)
                    m_cat = cM2[2].selectbox("種別", ["診療所", "病院", "歯科診療所"])
                    cM3 = st.columns(2)
                    m_lat = cM3[0].number_input("緯度（住所を使わない場合）", 0.0, 46.0, 0.0, format="%.6f")
                    m_lon = cM3[1].number_input("経度（住所を使わない場合）", 0.0, 154.0, 0.0, format="%.6f")
                    if st.form_submit_button("＋ この医療機関を追加"):
                        lat = lon = None
                        if m_lat > 0 and m_lon > 0:
                            lat, lon = m_lat, m_lon
                        elif m_addr.strip():
                            gc = _geocode_cached(m_addr.strip())
                            if gc:
                                lat, lon = gc
                        if not m_name.strip():
                            st.warning("名称を入力してください。")
                        elif lat is None:
                            st.warning("座標が取得できませんでした。住所を詳しくするか、緯度経度を直接入力してください。")
                        else:
                            st.session_state["manual_med"].append(MedFacility(
                                name=m_name.strip(), address=m_addr.strip(), lat=lat, lon=lon,
                                daily_outpatients=int(m_op) if m_op > 0 else None,
                                weekly_op_days=(m_wk if m_wk > 0 else None),
                                rx_summary=m_rxsum, facility_category=m_cat,
                                source="手動追加", detail_fetched=True,
                            ))
                            st.success(f"追加しました：{m_name}")
                            st.rerun()
                _added_med = [f for f in med_facs if f.source == "手動追加"]
                if _added_med:
                    st.caption("手動追加した医療機関： " + " / ".join(f.name for f in _added_med))
                m_del = st.multiselect(
                    "削除する医療機関を選ぶ", [f.name for f in med_facs], key="del_med_sel",
                )
                if st.button("🗑 選択した医療機関を削除", key="del_med_btn") and m_del:
                    for f in list(med_facs):
                        if f.name in m_del:
                            if f in st.session_state["manual_med"]:
                                st.session_state["manual_med"].remove(f)
                            else:
                                st.session_state["deleted_med_keys"].add(facility_key(f))
                    st.rerun()
            with ptab:
                with st.form("add_ph", clear_on_submit=True):
                    st.markdown("**薬局を追加**")
                    cP = st.columns(3)
                    p_name = cP[0].text_input("薬局名 *")
                    p_addr = cP[1].text_input("住所（座標を自動取得）")
                    p_rx = cP[2].number_input("年間実績枚数(0=不明)", 0, 1_000_000, 0, 500)
                    cP2 = st.columns(2)
                    p_lat = cP2[0].number_input("緯度（住所を使わない場合）", 0.0, 46.0, 0.0, format="%.6f", key="pla")
                    p_lon = cP2[1].number_input("経度（住所を使わない場合）", 0.0, 154.0, 0.0, format="%.6f", key="plo")
                    if st.form_submit_button("＋ この薬局を追加"):
                        lat = lon = None
                        if p_lat > 0 and p_lon > 0:
                            lat, lon = p_lat, p_lon
                        elif p_addr.strip():
                            gc = _geocode_cached(p_addr.strip())
                            if gc:
                                lat, lon = gc
                        if not p_name.strip():
                            st.warning("薬局名を入力してください。")
                        elif lat is None:
                            st.warning("座標が取得できませんでした。住所を詳しくするか、緯度経度を直接入力してください。")
                        else:
                            st.session_state["manual_ph"].append(PharmacyFacility(
                                name=p_name.strip(), address=p_addr.strip(), lat=lat, lon=lon,
                                annual_rx_count=int(p_rx) if p_rx > 0 else None,
                                source="手動追加",
                            ))
                            st.success(f"追加しました：{p_name}")
                            st.rerun()
                _added_ph = [p for p in pharmacies if p.source == "手動追加"]
                if _added_ph:
                    st.caption("手動追加した薬局： " + " / ".join(p.name for p in _added_ph))
                p_del = st.multiselect(
                    "削除する薬局を選ぶ", [p.name for p in pharmacies], key="del_ph_sel",
                )
                if st.button("🗑 選択した薬局を削除", key="del_ph_btn") and p_del:
                    for p in list(pharmacies):
                        if p.name in p_del:
                            if p in st.session_state["manual_ph"]:
                                st.session_state["manual_ph"].remove(p)
                            else:
                                st.session_state["deleted_ph_keys"].add(pharmacy_key(p))
                    st.rerun()

        # サイドバーのアサンプション＋手動上書きで常に再計算（調整に即応）
        summary = compute_capture_prediction(
            med_facs, a, op_override=st.session_state.get("op_overrides", {})
        )

        # ===== 2トラック予測（①医療機関ベース ハフ ＋ ②集客ベース 来店客数）=====
        st.markdown("### 📊 2トラック予測（医療機関ベース × 集客ベース）")
        st.caption(
            "同じ「この薬局が受ける年間処方箋枚数」を、**独立した2つのデータ**から別々に見積もります。"
            "スーパーのお客様には両方を示すと納得感が高まります（足し算はしません＝同じ患者の二重計上を避けるため）。"
        )

        _hp: HuffParams = st.session_state.get("huff", HuffParams())
        _fp: FootfallParams = st.session_state.get("footfall", FootfallParams())
        if not _fp.enabled:
            st.caption(
                "🛒 集客ベース（②）は、左サイドバーの「集客ベース予測」に"
                "**POSレジ客数 or 会員ユニーク数**を入れると併記されます。"
            )

        # 面/門前の自動判定（最寄りクリニック≤しきい値）＋一覧での目視修正
        classified = classify_menkata(
            pharmacies, med_facs, center_lat, center_lon,
            monzen_dist=_fp.menkata_monzen_dist,
            main_rx_threshold=_fp.menkata_main_rx,
            reach_m=_hp.reach_m,
        )
        mk_override: Dict[str, bool] = st.session_state.setdefault("ph_menkata_override", {})
        with st.expander(
            f"🔎 面／門前の判定（商圏内 {len(classified)}店・自動判定を目視で修正できます）",
            expanded=False,
        ):
            st.caption(
                f"自動判定：最寄りクリニック ≤ {_fp.menkata_monzen_dist:.0f}m（またはメイン薬局 実績"
                f"≥{_fp.menkata_main_rx:,.0f}枚）を『門前』としています。"
                "『判定』列を面/門前に変えると、集客ベースの面競合カウントに即反映されます"
                "（手修正はしきい値を変えても保持されます）。"
            )
            if st.button("手動修正をクリア（自動判定に戻す）", key="reset_menkata"):
                st.session_state["ph_menkata_override"] = {}
                mk_override = st.session_state["ph_menkata_override"]
            df_mk = pd.DataFrame([{
                "薬局": r["name"],
                "候補地から(m)": int(r["d_cand"]),
                "最寄りクリニック(m)": int(r["nearest_clinic"]) if r["nearest_clinic"] < 1e8 else None,
                "実績(枚)": r["rx"],
                "自動判定": "面" if r["auto_menkata"] else "門前",
                "判定": "面" if mk_override.get(r["key"], r["auto_menkata"]) else "門前",
                "_key": r["key"],
            } for r in classified])
            edited_mk = st.data_editor(
                df_mk, hide_index=True, use_container_width=True, key="menkata_editor",
                disabled=["薬局", "候補地から(m)", "最寄りクリニック(m)", "実績(枚)", "自動判定", "_key"],
                column_config={
                    "判定": st.column_config.SelectboxColumn(
                        "判定", options=["面", "門前"], width="small",
                        help="面＝集客の競合に数える／門前＝競合から外す",
                    ),
                },
            )
            # 自動判定と異なる行だけを override に保存（同じなら削除してしきい値変更に追従）
            auto_map = {r["key"]: r["auto_menkata"] for r in classified}
            for _, row in edited_mk.iterrows():
                k = row["_key"]
                is_men = (row["判定"] == "面")
                if is_men != auto_map.get(k, True):
                    mk_override[k] = is_men
                else:
                    mk_override.pop(k, None)
            st.session_state["ph_menkata_override"] = mk_override

        comp_power, comp_n, comp_excluded = footfall_competitor_power(
            classified, st.session_state.get("ph_menkata_override", {}),
            competitor_decay_m=_fp.competitor_decay_m,
            national_avg=_hp.national_avg_rx,
        )
        huff_res = (compute_huff_prediction(
            med_facs, pharmacies, center_lat, center_lon, a, _hp,
            st.session_state.get("op_overrides", {}),
        ) if _hp.enabled else None)
        foot_res = compute_footfall_prediction(_fp, comp_power)

        huff_total = huff_res["total"] if huff_res else None
        foot_total = foot_res["total"] if foot_res else None

        def _card(cls, label, annual):
            if annual is None:
                return (f'<div class="rx-card {cls}"><div class="rx-label">{label}</div>'
                        f'<div class="rx-val rx-muted">—</div>'
                        f'<div class="rx-sub">未入力</div></div>')
            return (f'<div class="rx-card {cls}"><div class="rx-label">{label}</div>'
                    f'<div class="rx-val">{annual:,.0f}<small>枚/年</small></div>'
                    f'<div class="rx-sub">月間 約 {annual/12:,.0f} 枚/月</div></div>')

        cards = _card("med", "① 医療機関ベース（ハフ按分）", huff_total)
        cards += _card("foot", "② 集客ベース（来店客数）", foot_total)
        vals = [v for v in (huff_total, foot_total) if v is not None]
        if len(vals) == 2:
            lo, hi = min(vals), max(vals)
            cards += (f'<div class="rx-card range"><div class="rx-label">予測レンジ（統合）</div>'
                      f'<div class="rx-val">{lo:,.0f}<small>〜 {hi:,.0f} 枚/年</small></div>'
                      f'<div class="rx-sub">月間 約 {lo/12:,.0f} 〜 {hi/12:,.0f} 枚／中心 約 {(lo+hi)/2:,.0f} 枚/年</div></div>')
        st.markdown(f'<div class="rx-cards">{cards}</div>', unsafe_allow_html=True)
        if len(vals) == 2:
            st.caption("医療機関ベースと集客ベースの2つの独立推計です。相手には両方を根拠として提示できます"
                       "（足し算はしません＝同じ患者の二重計上を避けるため）。")
        elif huff_total is not None:
            st.caption("集客ベース（②）は、左サイドバーの「🛒集客ベース予測」に来店客数（POS/会員）を入れると併記されます。")

        with st.expander("🧮 ハフ競合按分の考え方（相手への説明用）", expanded=False):
            st.markdown(
                "**重力モデルの発想**：買い物客が「近くて大きい店」を選ぶのと同じように、"
                "患者は処方箋を持ち込む薬局を **近さ × 集客力** で確率的に選ぶ、と考えます。\n\n"
                "- 各クリニックが1年に出す処方箋（外来患者数×診療日数×院外率）を「山分けの原資」とし、"
                "周辺の薬局が **取り分率 = 自店の(引力×近さ) ÷ 全薬局の(引力×近さ)の合計** で分け合います。\n"
                "- 近い薬局・大きい薬局ほど取り分が大きく、**全薬局の取り分を足すと必ずそのクリニックの"
                "総処方箋数に一致**します（独占・二重計上が起きない）。\n"
                "- ここが従来の加算型（競合が何店あっても固定割合を独占）との違いで、面型の過大予測を是正します。\n\n"
                f"現在の設定：距離減衰λ={_hp.lambda_m:.0f}m ・門前ブースト×{_hp.monzen_boost:g} ・"
                f"候補店の引力={_hp.candidate_attractiveness:g} ・競合薬局 {huff_res['n_competitors'] if huff_res else 0}店。"
            )
            if huff_res and huff_res["rows"]:
                hr = pd.DataFrame([
                    {"医療機関": r["clinic"], "距離(m)": int(r["dist_m"]),
                     "クリニック院外処方/年": int(round(r["pool"])),
                     "自店の取り分率": round(r["share"], 3),
                     "獲得/年": int(round(r["captured"]))}
                    for r in huff_res["rows"][:15]
                ])
                st.dataframe(hr, hide_index=True, use_container_width=True)

        if foot_res:
            with st.expander("🛒 集客ベースの内訳", expanded=False):
                st.markdown(
                    f"- 月間ユニーク客 {_fp.unique_customers_monthly:,.0f}人"
                    f"（65+ {foot_res['u65']:,.0f} / 65− {foot_res['u_under']:,.0f}）\n"
                    f"- 年間受診延べ {foot_res['annual_visits']:,.0f}回 → "
                    f"院外処方プール {foot_res['rx_pool']:,.0f}枚\n"
                    f"- 当該薬局利用率 {_fp.use_rate:.1%} ÷ (面競合の実効パワー {comp_power:.1f}"
                    f"〔面{comp_n}店を距離減衰λ={_fp.competitor_decay_m:.0f}mで重み付け〕 + 1)"
                    f" = シェア {foot_res['share']:.2%}\n"
                    f"- 面競合の抽出：門前(最寄りクリニック≤{_fp.menkata_monzen_dist:.0f}m)と"
                    f"メイン薬局(実績≥{_fp.menkata_main_rx:,.0f}枚)を除外 → **{comp_excluded}店を除外**\n"
                    f"- **獲得 = {foot_res['total']:,.0f} 枚/年**"
                )
        st.divider()
        st.markdown("#### 🏥 医療機関ごとの寄与内訳（外来数・流入率）")
        st.caption(
            f"商圏内の寄与医療機関 **{summary['n_contributing']}件**。"
            "各医療機関の外来患者数・院内外区分・距離帯別の流入率と、参考の獲得枚数を示します。"
        )
        if summary.get("n_outside_area", 0) > 0:
            st.caption(
                f"🗺️ 商圏ポリゴン外のため寄与から除外した医療機関: "
                f"**{summary['n_outside_area']}件**（内訳テーブルで「商圏外（ポリゴン）」表示）"
            )
        if summary["n_no_outpatient"] > 0:
            st.warning(
                f"⚠️ 外来患者数が取得できなかった医療機関が {summary['n_no_outpatient']} 件あります"
                "（予測に未算入）。大病院で「なし」の場合は病院HP/年報から手動補完を検討してください。"
            )

        # データ品質アラート（異常値疑いの施設が予測に影響する場合のみ表示）
        suspects = [
            f for f in med_facs
            if f.op_flag and f.inflow_rate > 0 and not f.outpatient_manual
        ]
        if suspects:
            s_lines = "\n".join(
                f"- **{f.name}**（{f.op_flag}）: 現在値 {f.daily_outpatients or '—'} 人/日"
                + (f" → 補正候補 **{f.op_suggested} 人/日**（年間値÷305）" if f.op_suggested else "")
                for f in suspects
            )
            st.warning(
                f"🔍 **外来患者数の異常値疑い：{len(suspects)}件** — "
                "ナビィへの誤登録（1日欄に年間値/月間値）の可能性があります。"
                "上の「✏️ 外来患者数を手動補完」で補正候補値を入力すると予測に反映されます。\n\n"
                + s_lines
            )

        # 門前占有アラート（該当クリニックがある時だけ表示）
        contested = [
            f for f in med_facs
            if f.monzen_contested and f.captured_rx and f.captured_rx > 0
        ]
        if contested:
            lines = "\n".join(
                f"- **{f.name}**（候補地から{int(f.distance_m)}m）← 既存門前: "
                f"{f.nearest_pharmacy_name}（クリニックから{int(f.nearest_pharmacy_dist_m)}m）"
                f"／このクリニックの寄与 {int(round(f.captured_rx)):,}枚/年"
                for f in sorted(contested, key=lambda x: x.captured_rx, reverse=True)
            )
            mode_txt = (
                "現在は面レートへ引下げて計算済みです。"
                if a.discount_contested_monzen
                else "現在は門前レート(0.570)のまま計算しています（過大評価の可能性）。"
                     "左サイドバー『門前占有チェック』で面レートへの引下げを選べます。"
            )
            st.error(
                f"⚠️ **門前競合アラート：{len(contested)}件** — "
                f"以下のクリニックは、候補地の門前(≤50m)に入りますが、"
                f"既に別の薬局が門前に張り付いています。{mode_txt}\n\n{lines}"
            )

        # 寄与内訳テーブル（captured_rx 降順）
        contrib_rows = []
        for f in sorted(med_facs, key=lambda x: (x.captured_rx or -1), reverse=True):
            eff_op = None
            if f.annual_op_visits and f.annual_op_days_used:
                eff_op = int(round(f.annual_op_visits / f.annual_op_days_used))
            contrib_rows.append({
                "医療機関": f.name,
                "距離(m)": int(f.distance_m) if f.distance_m is not None else None,
                "流入帯": f.inflow_band or inflow_band_label(f.distance_m, a.bands),
                "流入率": f.inflow_rate,
                "外来(人/日)": eff_op,
                "補完": "手動" if f.outpatient_manual else "",
                "年間外来延べ": int(f.annual_op_visits) if f.annual_op_visits else None,
                "院内外処方": f.rx_summary,
                "係数": f.external_rx_factor,
                "門前競合": "⚠️競合" if f.monzen_contested else (
                    "占有" if f.monzen_occupied else ""),
                "検証": ("🔍" + f.op_flag) if f.op_flag else (
                    "美容(係数減)" if f.is_cosmetic else (
                        "歯科(係数減)" if f.facility_category == "歯科診療所" else "")),
                "獲得処方箋/年": int(round(f.captured_rx)) if f.captured_rx else 0,
                "外来出典": "手動補完" if f.outpatient_manual else f.daily_outpatients_source,
            })
        df_pred = pd.DataFrame(contrib_rows)
        st.dataframe(
            df_pred, use_container_width=True, hide_index=True,
            column_config={
                "距離(m)":      st.column_config.NumberColumn("距離(m)", format="%d m", width="small"),
                "流入率":        st.column_config.NumberColumn("流入率", format="%.3f", width="small"),
                "年間外来延べ":  st.column_config.NumberColumn("年間外来延べ", format="%d 人", width="small"),
                "係数":          st.column_config.NumberColumn("係数", format="%.2f", width="small"),
                "獲得処方箋/年":  st.column_config.NumberColumn("獲得処方箋/年", format="%d 枚", width="medium"),
            },
        )
        # ── 相手に渡せる1枚レポートCSV（サマリー＋前提＋医療機関内訳＋面/門前一覧） ──
        _rep = io.StringIO()
        _w = csv.writer(_rep)
        _w.writerow(["処方箋獲得予測レポート"])
        _w.writerow(["物件（出店候補地）", st.session_state.last_address])
        _w.writerow(["商圏半径(m)", collected_radius])
        _w.writerow(["作成日", time.strftime("%Y-%m-%d")])
        _w.writerow([])
        _w.writerow(["■ 予測サマリー", "年間(枚)", "月間(枚)"])
        if huff_total is not None:
            _w.writerow(["① 医療機関ベース(ハフ按分)", round(huff_total), round(huff_total / 12)])
        if foot_total is not None:
            _w.writerow(["② 集客ベース(来店客数)", round(foot_total), round(foot_total / 12)])
        if huff_total is not None and foot_total is not None:
            _lo, _hi = min(huff_total, foot_total), max(huff_total, foot_total)
            _w.writerow(["予測レンジ", f"{round(_lo):,}〜{round(_hi):,}",
                         f"{round(_lo/12):,}〜{round(_hi/12):,}"])
        _w.writerow([])
        _w.writerow(["■ 主な前提"])
        _w.writerow(["店舗形態", _fp.store_format])
        _w.writerow(["月間ユニーク客数(人)", round(_fp.unique_customers_monthly)])
        _w.writerow(["65歳以上比率", _fp.ratio_65plus])
        _w.writerow(["月受診回数 65+/65-", f"{_fp.visits_month_65plus} / {_fp.visits_month_under65}"])
        _w.writerow(["発行率/院外率/利用率",
                     f"{_fp.issue_rate} / {_fp.external_rate} / {_fp.use_rate}"])
        _w.writerow(["面競合 実効パワー/店数/除外店数", f"{comp_power:.1f} / {comp_n} / {comp_excluded}"])
        _w.writerow(["面競合 距離減衰λ(m)", f"{_fp.competitor_decay_m:.0f}"])
        _w.writerow(["ハフ λ/門前boost/候補店引力",
                     f"{_hp.lambda_m:.0f} / {_hp.monzen_boost:g} / {_hp.candidate_attractiveness:g}"])
        _w.writerow([])
        _w.writerow(["■ 医療機関ごとの寄与内訳"])
        if contrib_rows:
            _cols = list(contrib_rows[0].keys())
            _w.writerow(_cols)
            for _r in contrib_rows:
                _w.writerow([_r.get(c) for c in _cols])
        _w.writerow([])
        _w.writerow(["■ 面／門前の判定一覧"])
        _w.writerow(["薬局", "候補地から(m)", "最寄りクリニック(m)", "実績(枚)", "判定"])
        for _r in classified:
            _is_men = mk_override.get(_r["key"], _r["auto_menkata"])
            _w.writerow([
                _r["name"], round(_r["d_cand"]),
                round(_r["nearest_clinic"]) if _r["nearest_clinic"] < 1e8 else "",
                _r["rx"] or "", "面" if _is_men else "門前",
            ])
        st.download_button(
            "📄 レポートCSVをダウンロード（相手提示用・1枚にまとめ）",
            data=_rep.getvalue().encode("utf-8-sig"),
            file_name=f"処方箋獲得予測レポート_{st.session_state.last_address[:15]}.csv",
            mime="text/csv",
            type="primary",
        )

        with st.expander("使用中のアサンプション"):
            band_txt = " / ".join(
                f"≤{int(u)}m:{r:.3f}" for u, r in sorted(a.bands, key=lambda b: b[0])
            )
            st.markdown(
                f"- 流入率帯: {band_txt}（3km超=0）\n"
                f"- 年間診療日数: {'週診療日数×52' if a.annual_days_mode=='weekly' else '固定'}"
                f"（フォールバック {a.fixed_annual_days}日）\n"
                f"- 院外処方係数: 院外あり={a.external_factor_gairai} / "
                f"院内のみ={a.external_factor_inhouse} / 不明={a.external_factor_unknown}\n"
                f"- 処方箋発行率: {a.issue_rate}"
            )

    # ─── タブ「🧪 実績照合」 ──────────────────────────────────────────────
    with tab_calib:
        st.subheader("モデル予測 × ナビィ実績（既存薬局で答え合わせ）")
        st.caption(
            "商圏内の**既存薬局それぞれの位置**に同じ流入率モデルを当て、"
            "ナビィ報告の年間処方箋実績と比較します。  \n"
            "**比率が1に近い** → このエリアではモデルとナビィデータが整合。  \n"
            "**大きくズレる薬局がある** → 近隣クリニックの外来数誤登録・流入率の地域差・"
            "その薬局固有の事情（在宅/施設調剤など）のシグナルです。"
        )
        a_c: PredictionAssumptions = st.session_state.get("assumptions", PredictionAssumptions())
        ov = st.session_state.get("op_overrides", {})
        calib_rows = []
        for p in pharmacies:
            if not p.annual_rx_count or p.annual_rx_count <= 0:
                continue
            if p.lat is None or p.lon is None:
                continue
            if not p.in_area:
                continue  # 商圏ポリゴン外の薬局は照合対象外
            pred = predict_at_point(p.lat, p.lon, med_facs, a_c, op_override=ov)
            ratio = pred / p.annual_rx_count if p.annual_rx_count else None
            edge = (p.distance_m or 0) > radius_m * 0.6
            calib_rows.append({
                "薬局名": p.name,
                "種別": p.pharmacy_type,
                "実績 処方箋/年": p.annual_rx_count,
                "モデル予測/年": int(round(pred)),
                "予測÷実績": round(ratio, 2) if ratio is not None else None,
                "備考": "商圏端（予測は過小になりがち）" if edge else "",
            })
        if calib_rows:
            ratios = [r["予測÷実績"] for r in calib_rows
                      if r["予測÷実績"] is not None and not r["備考"]]
            cc1, cc2, cc3 = st.columns(3)
            cc1.metric("照合対象の薬局数", f"{len(calib_rows)} 件")
            if ratios:
                med_ratio = sorted(ratios)[len(ratios) // 2]
                cc2.metric("予測÷実績（中央値）", f"{med_ratio:.2f}")
                cc3.metric(
                    "校正のヒント",
                    "整合" if 0.7 <= med_ratio <= 1.4 else ("過大傾向" if med_ratio > 1.4 else "過小傾向"),
                )
                if not (0.7 <= med_ratio <= 1.4):
                    st.info(
                        f"💡 中央値が {med_ratio:.2f} です。このエリアでは流入率アサンプションを"
                        f"{'下げる' if med_ratio > 1.4 else '上げる'}方向の調整、"
                        "または近隣クリニックの外来数の確認を検討してください。"
                    )
            st.dataframe(
                pd.DataFrame(calib_rows).sort_values("実績 処方箋/年", ascending=False),
                use_container_width=True, hide_index=True,
                column_config={
                    "実績 処方箋/年": st.column_config.NumberColumn(format="%d 枚"),
                    "モデル予測/年": st.column_config.NumberColumn(format="%d 枚"),
                },
            )
            st.caption(
                "※ 予測は商圏内で収集済みのクリニックのみで計算するため、商圏の端にある薬局は"
                "（商圏外のクリニックからの流入が見えず）過小に出ます。中央値は端の薬局を除いて算出。  \n"
                "※ 在宅・施設調剤が多い薬局や広域門前（大病院前）は、立地モデルの前提から外れるため"
                "個別にズレることがあります。"
            )
        else:
            st.info("実績処方箋数が取得できた薬局がないため、照合できません。")

    # ─── タブ①「🏥 医療機関」 ────────────────────────────────────────────
    with tab_med:
        n_total_med = len(med_facs)
        n_gaigai    = sum(1 for f in med_facs if f.rx_summary == "院外処方あり")
        op_vals     = [f.daily_outpatients for f in med_facs if f.daily_outpatients]
        wd_vals     = [f.weekly_op_days    for f in med_facs if f.weekly_op_days]
        avg_op = int(sum(op_vals) / len(op_vals)) if op_vals else None
        avg_wd = round(sum(wd_vals) / len(wd_vals), 1) if wd_vals else None

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("医療機関 合計", f"{n_total_med} 件")
        col2.metric("院外処方あり", f"{n_gaigai} 件")
        col3.metric("1日外来患者数（平均）", f"{avg_op:,} 人/日" if avg_op else "— 人/日")
        col4.metric("週診療日数（平均）", f"{avg_wd} 日/週" if avg_wd else "— 日/週")

        rows = []
        for fac in med_facs:
            rows.append({
                "施設名":      fac.name,
                "距離(m)":     int(fac.distance_m) if fac.distance_m is not None else None,
                "種別":        fac.facility_category,
                "院内外処方":  fac.rx_summary,
                "外来患者数":  f"{fac.daily_outpatients:,} 人/日" if fac.daily_outpatients else "なし",
                "週診療日数":  f"{fac.weekly_op_days:.1f} 日" if fac.weekly_op_days else "なし",
                "診療科":      fac.specialties or "—",
                "住所":        fac.address or "—",
                "ナビィURL":   fac.detail_url or "",
            })
        df_med = pd.DataFrame(rows)
        st.dataframe(
            df_med,
            use_container_width=True,
            hide_index=True,
            column_config={
                "距離(m)":   st.column_config.NumberColumn("距離(m)", format="%d m", width="small"),
                "ナビィURL":  st.column_config.LinkColumn("ナビィURL", display_text="🔗 ナビィ", width="small"),
            },
        )
        csv_med = pd.DataFrame([{
            "施設名":        f.name,
            "住所":          f.address,
            "距離_m":        int(f.distance_m) if f.distance_m else "",
            "種別":          f.facility_category,
            "院内外処方":    f.rx_summary,
            "院内処方(有無)": f.inhouse_rx,
            "院外処方(有無)": f.outpatient_rx,
            "1日外来患者数": f.daily_outpatients or "",
            "週診療日数":    f.weekly_op_days or "",
            "診療科":        f.specialties,
            "ナビィURL":     f.detail_url,
            "データソース":  f.source,
            "緯度":          f.lat or "",
            "経度":          f.lon or "",
        } for f in med_facs])
        st.download_button(
            "⬇️ 医療機関CSVダウンロード",
            data=csv_med.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"医療機関_{st.session_state.last_address[:15]}_{radius_m}m.csv",
            mime="text/csv",
        )

    # ─── タブ②「💊 薬局」 ─────────────────────────────────────────────────
    with tab_ph:
        ph_in = [p for p in pharmacies if p.in_area]
        n_total_ph = len(ph_in)
        n_monzen   = sum(1 for p in ph_in if p.pharmacy_type == "門前薬局")
        n_men      = sum(1 for p in ph_in if p.pharmacy_type == "面薬局")
        rx_vals_ph = [p.annual_rx_count for p in ph_in if p.annual_rx_count]
        avg_rx     = int(sum(rx_vals_ph) / len(rx_vals_ph)) if rx_vals_ph else None

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("薬局 合計（商圏内）", f"{n_total_ph} 件")
        col2.metric("門前薬局",          f"{n_monzen} 件")
        col3.metric("面薬局",            f"{n_men} 件")
        col4.metric("年間処方箋数（平均）", f"{avg_rx:,} 件" if avg_rx else "— 件")
        if active_polygons and len(pharmacies) > n_total_ph:
            st.caption(f"※ サマリーは商圏ポリゴン内のみ。圏外 {len(pharmacies)-n_total_ph} 件は表の「商圏」列で確認できます")

        rows_ph = []
        for ph in pharmacies:
            rows_ph.append({
                "薬局名":        ph.name,
                "商圏":          "圏内" if ph.in_area else "圏外",
                "距離(m)":       int(ph.distance_m) if ph.distance_m is not None else None,
                "種別":          ph.pharmacy_type,
                "最近接医療機関": ph.nearest_clinic_name,
                "最近接距離(m)": int(ph.nearest_clinic_dist_m) if ph.nearest_clinic_dist_m is not None else None,
                "年間処方箋数":  ph.annual_rx_count,
                "住所":          ph.address or "—",
                "ナビィURL":     ph.href or "",
            })
        df_ph = pd.DataFrame(rows_ph)
        st.dataframe(
            df_ph,
            use_container_width=True,
            hide_index=True,
            column_config={
                "距離(m)":      st.column_config.NumberColumn("距離(m)", format="%d m", width="small"),
                "最近接距離(m)": st.column_config.NumberColumn("最近接距離", format="%d m", width="small"),
                "年間処方箋数":  st.column_config.NumberColumn("年間処方箋数", format="%d 枚", width="small"),
                "ナビィURL":     st.column_config.LinkColumn("ナビィURL", display_text="🔗 ナビィ", width="small"),
            },
        )
        csv_ph = pd.DataFrame([{
            "薬局名":        p.name,
            "商圏":          "圏内" if p.in_area else "圏外",
            "住所":          p.address,
            "距離_m":        int(p.distance_m) if p.distance_m else "",
            "種別":          p.pharmacy_type,
            "最近接医療機関": p.nearest_clinic_name,
            "最近接距離_m":  int(p.nearest_clinic_dist_m) if p.nearest_clinic_dist_m else "",
            "年間処方箋数":  p.annual_rx_count or "",
            "処方箋数出典":  p.annual_rx_source if p.annual_rx_count else "",
            "ナビィURL":     p.href,
            "データソース":  p.source,
            "緯度":          p.lat or "",
            "経度":          p.lon or "",
        } for p in pharmacies])
        st.download_button(
            "⬇️ 薬局CSVダウンロード",
            data=csv_ph.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"薬局_{st.session_state.last_address[:15]}_{radius_m}m.csv",
            mime="text/csv",
        )

    # ─── タブ③「🗺️ 統合地図」 ────────────────────────────────────────────
    with tab_map:
        m = folium.Map(location=[center_lat, center_lon], zoom_start=15)

        if active_polygons:
            # 商圏ポリゴン（描いた形）を表示。収集円は薄い点線で参考表示
            for i, poly in enumerate(active_polygons):
                folium.Polygon(
                    locations=[[p[0], p[1]] for p in poly],
                    color="#2E7D32", weight=3, fill=True,
                    fill_color="#2E7D32", fill_opacity=0.08,
                    tooltip=f"商圏ポリゴン {i+1}",
                ).add_to(m)
            folium.Circle(
                location=[center_lat, center_lon],
                radius=collected_radius,
                color="gray", weight=1, dash_array="6", fill=False,
                tooltip=f"データ収集範囲: {collected_radius:,.0f}m",
            ).add_to(m)
        else:
            # 商圏円
            folium.Circle(
                location=[center_lat, center_lon],
                radius=collected_radius,
                color="gray", fill=True, fill_opacity=0.05,
                tooltip=f"商圏: {collected_radius:,.0f}m",
            ).add_to(m)

        # 中心マーカー
        folium.Marker(
            location=[center_lat, center_lon],
            tooltip=st.session_state.last_address,
            icon=folium.Icon(color="blue", icon="home", prefix="fa"),
        ).add_to(m)

        # 医療機関マーカー
        for fac in med_facs:
            if fac.lat is None or fac.lon is None:
                continue
            if not fac.in_area:
                col_med = "lightgray"   # 商圏ポリゴン外は薄表示
            elif fac.rx_summary == "院外処方あり":
                col_med = "red"
            elif fac.rx_summary == "院内処方のみ":
                col_med = "orange"
            else:
                col_med = "gray"
            popup_html = (
                f"<b>{fac.name}</b><br>"
                + ("<b>【商圏ポリゴン外】</b><br>" if not fac.in_area else "")
                + f"種別: {fac.facility_category}<br>"
                f"処方: {fac.rx_summary}<br>"
                f"外来患者数: {fac.daily_outpatients or '—'} 人/日<br>"
                f"週診療日数: {fac.weekly_op_days or '—'} 日/週<br>"
                f"診療科: {fac.specialties or '—'}<br>"
                f"住所: {fac.address or '—'}"
            )
            folium.CircleMarker(
                location=[fac.lat, fac.lon],
                radius=5 if not fac.in_area else 7,
                color=col_med, fill=True, fill_color=col_med,
                fill_opacity=0.35 if not fac.in_area else 0.75,
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=f"🏥 {fac.name}（{fac.facility_category}）"
                        + ("【圏外】" if not fac.in_area else ""),
            ).add_to(m)

        # 薬局マーカー
        for ph in pharmacies:
            if ph.lat is None or ph.lon is None:
                continue
            if ph.pharmacy_type == "門前薬局":
                col_ph = "darkred"
            elif ph.pharmacy_type == "面薬局":
                col_ph = "blue"
            else:
                col_ph = "lightgray"
            rx_txt = f"{ph.annual_rx_count:,} 枚/年" if ph.annual_rx_count else "不明"
            nearest_txt = (
                f"{ph.nearest_clinic_name}（{int(ph.nearest_clinic_dist_m)}m）"
                if ph.nearest_clinic_dist_m is not None else "—"
            )
            popup_html_ph = (
                f"<b>{ph.name}</b><br>"
                + ("<b>【商圏ポリゴン外】</b><br>" if not ph.in_area else "")
                + f"種別: {ph.pharmacy_type}<br>"
                f"最近接医療機関: {nearest_txt}<br>"
                f"年間処方箋数: {rx_txt}<br>"
                f"住所: {ph.address or '—'}"
            )
            folium.CircleMarker(
                location=[ph.lat, ph.lon],
                radius=6 if not ph.in_area else 8,
                color=col_ph, fill=True, fill_color=col_ph,
                fill_opacity=0.35 if not ph.in_area else 0.8,
                popup=folium.Popup(popup_html_ph, max_width=280),
                tooltip=f"💊 {ph.name}（{ph.pharmacy_type}）"
                        + ("【圏外】" if not ph.in_area else ""),
            ).add_to(m)

        # 凡例
        legend_html = """
        <div style="position:fixed; bottom:30px; left:30px; z-index:1000;
                    background:white; padding:10px; border-radius:8px;
                    border:1px solid #ccc; font-size:13px; line-height:1.8;">
          <b>凡例</b><br>
          <span style="color:red;">●</span> 医療機関（院外処方あり）<br>
          <span style="color:orange;">●</span> 医療機関（院内処方のみ）<br>
          <span style="color:gray;">●</span> 医療機関（不明）<br>
          <span style="color:darkred;">●</span> 門前薬局<br>
          <span style="color:blue;">●</span> 面薬局<br>
          <span style="color:lightgray;">●</span> 薬局（不明）
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))

        st_folium(m, use_container_width=True, height=650)

    # ─── タブ④「📝 ログ」 ─────────────────────────────────────────────────
    with tab_log:
        st.subheader("処理ログ")
        for line in st.session_state.search_log:
            st.text(line)

else:
    st.info("← 左のサイドバーで住所と条件を設定し、「検索実行」を押してください。")
