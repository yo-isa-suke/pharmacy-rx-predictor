"""
商圏分析ツール v2 — 医療機関 + 調剤薬局 統合版 ＋ 処方箋獲得予測

medical_finder.py と pharmacy_area_finder.py を統合し、
商圏内の医療機関・薬局を一括収集・分析するツール。

v2 追加機能（処方箋獲得予測モデル）:
  - 候補薬局地点（＝商圏中心）が獲得できる年間処方箋枚数を予測
  - 各医療機関 → 候補地点の「距離帯別 流入率」を掛け合わせて積み上げ
        獲得処方箋 = Σ_医療機関( 年間外来延べ数 × 院外処方係数 × 流入率(距離) )
  - 流入率・診療日数・院外処方係数などのアサンプションをUIで調整可能
  - 外来患者数抽出の潜在バグ（多列フィールドで入院列を誤取得）を修正

データソース:
  - 厚生労働省「医療情報ネット（ナビィ）」— 医療機関・薬局リスト・詳細
  - OpenStreetMap Overpass API — 施設の座標
  - 国土地理院（GSI）/ Nominatim — ジオコーディング

【外来患者数の精度に関する検証メモ（2026-07 実データ確認済み）】
  - ナビィ詳細ページの「前年度１日平均患者数」は多列
    （例: 相澤病院 396.3人/-/-/-/-/-/824人/-）。7列目が外来。
  - 実績統計表パーサー（_extract_outpatient_from_table）はヘッダ
    「外来患者」を先頭一致で正しく外来列を選ぶため、診療所〜大病院まで
    正確に取得できることをライブ確認（相澤病院=824人/日 は公表水準と整合）。
  - 大病院で外来値が「-」（未報告）の場合のみ、病院HP/病院年報からの
    補完を推奨（UIで手動上書き可能）。
"""

import math
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import folium
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
from streamlit_folium import st_folium

# ─── ページ設定（必ずファイル先頭のstコマンドより前に置く） ─────────────────────
st.set_page_config(
    page_title="商圏分析ツール v2（処方箋獲得予測）",
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
            fac.inflow_rate = inflow_rate_for_distance(fac.distance_m, a.bands)
            fac.inflow_band = inflow_band_label(fac.distance_m, a.bands)
            fac.captured_rx = None
            n_no_op += 1
            continue

        annual_visits = eff_op * days
        factor = _external_factor(fac.rx_summary, a)
        rate = inflow_rate_for_distance(fac.distance_m, a.bands)

        # 門前競合クリニックは、任意で面レートへ引下げ（デフォルトは引下げず表示のみ）
        if fac.monzen_contested and a.discount_contested_monzen:
            rate = men_rate

        captured = annual_visits * factor * a.issue_rate * rate

        fac.annual_op_visits = annual_visits
        fac.external_rx_factor = factor
        fac.inflow_rate = rate
        fac.inflow_band = inflow_band_label(fac.distance_m, a.bands)
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
        for cell in cells[1:]:
            val = cell.get_text(strip=True)
            m = re.search(r"(\d+\.?\d*)", val)
            if m:
                n = float(m.group(1))
                if 0 < n <= 10_000:
                    return int(round(n))
    for row in rows:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True)
        if "前年度" not in label:
            continue
        for cell in cells[1:]:
            val = cell.get_text(strip=True)
            if re.fullmatch(r"[－\-−—―\s]*", val):
                continue
            m = re.search(r"(\d+\.?\d*)", val)
            if m:
                n = float(m.group(1))
                if 0 < n <= 5_000:
                    return int(round(n))
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
        v = _get_field(fields, [k_target])
        if v:
            m_person = re.search(r"(\d+\.?\d*)\s*人", v)
            if m_person:
                n = float(m_person.group(1))
                if 0 < n <= 10_000:
                    return int(round(n)), f"ナビィ（{k_target}）"
            nums = re.findall(r"[\d,]+\.?\d*", v)
            for n_str in nums:
                try:
                    n = float(n_str.replace(",", ""))
                    if 1 <= n <= 3_000:
                        return int(round(n)), f"ナビィ（{k_target}）"
                    if n > 3_000:
                        return max(1, int(n / WORKING_DAYS)), f"ナビィ（{k_target}・年間÷305）"
                except ValueError:
                    pass
    for pat, label in [
        (r"1日あたりの外来患者の平均数[^\d]{0,20}(\d{1,4})", "ナビィ（テキスト解析）"),
        (r"前年度の?１?日平均外来患者数[^\d]{0,15}(\d{1,4})", "ナビィ（テキスト解析）"),
        (r"外来患者の平均数[^\d]{0,15}(\d{1,4})", "ナビィ（テキスト解析）"),
        (r"1日平均外来患者数[^\d]{0,15}(\d{1,4})", "ナビィ（テキスト解析）"),
        (r"外来患者[^\d]{0,10}1日平均[^\d]{0,10}(\d{1,4})", "ナビィ（テキスト解析）"),
        (r"外来[^\d]{0,8}(\d{1,3})\s*人[/／]日", "ナビィ（テキスト解析）"),
        (r"前年度[^\d]{0,20}外来[^\d]{0,10}(\d{1,4}\.?\d*)\s*人", "ナビィ（テキスト解析）"),
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
            m = re.search(r"([\d,]+)", v)
            if m:
                try:
                    n = int(m.group(1).replace(",", ""))
                    if 100 <= n <= 10_000_000:
                        return n, f"ナビィ（{k}）"
                except ValueError:
                    pass
    for pat, label in [
        (r"処方箋受付(?:回数|枚数)[^\d]{0,10}([\d,]+)", "ナビィ（テキスト解析）"),
        (r"取扱処方箋(?:数|枚)[^\d]{0,10}([\d,]+)", "ナビィ（テキスト解析）"),
        (r"処方箋[^\d]{0,8}([\d,]+)\s*(?:回|枚|件)", "ナビィ（テキスト解析）"),
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
                    break
            except Exception:
                continue
        if soup is None:
            return False

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

        # ── 週診療日数 ───────────────────────────────────────────────────
        fac.weekly_op_days = _parse_weekly_days(all_fields, full_text, soup)

        # ── 診療科目 ─────────────────────────────────────────────────────
        if not fac.specialties:
            fac.specialties = _parse_specialties(all_fields, full_text)

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
        if nmf.address:
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
        if vf.address:
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
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─── UI ───────────────────────────────────────────────────────────────────────
st.title("🎯 商圏分析ツール v2 — 処方箋獲得予測")
st.caption(
    "住所（＝出店候補地）と商圏半径を入力すると、圏内の医療機関・薬局を一覧表示し、  \n"
    "**その地点に薬局を出した場合に獲得できる年間処方箋枚数を予測**します。  \n"
    "データソース: **厚生労働省ナビィ** + **OpenStreetMap** + **国土地理院**"
)

# ── サイドバー ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("検索条件")
    address_input = st.text_input(
        "住所（商圏の中心）",
        placeholder="例：山梨県中央市若宮50-1",
        help="丁目・番地まで入力すると精度が上がります",
    )
    radius_m = st.slider(
        "商圏半径 (m)", min_value=200, max_value=5000, value=1000, step=100,
        help="この半径内の施設を検索します",
    )
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
)
st.session_state["assumptions"] = _assumptions

# ── 検索実行 ──────────────────────────────────────────────────────────────────
if run_btn and address_input.strip():
    log: List[str] = []
    prog = st.progress(0, text="検索を開始しています…")
    try:
        med_list, ph_list, clat, clon = run_analysis(
            address_input.strip(), radius_m, gate_m, max_detail, log, prog,
            assumptions=_assumptions,
        )
        st.session_state.med_results  = med_list
        st.session_state.ph_results   = ph_list
        st.session_state.center_lat   = clat
        st.session_state.center_lon   = clon
        st.session_state.search_log   = log
        st.session_state.last_address = address_input.strip()
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
    med_facs:  List[MedFacility]      = st.session_state.med_results
    pharmacies: List[PharmacyFacility] = st.session_state.ph_results
    center_lat = st.session_state.center_lat
    center_lon = st.session_state.center_lon

    st.success(
        f"**{st.session_state.last_address}** の {radius_m:,}m 商圏内: "
        f"医療機関 **{len(med_facs)}件** / 薬局 **{len(pharmacies)}件** が見つかりました"
    )

    tab_pred, tab_med, tab_ph, tab_map, tab_log = st.tabs(
        ["🎯 処方箋獲得予測", "🏥 医療機関", "💊 薬局", "🗺️ 統合地図", "📝 ログ"]
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
                    "上書き外来(人/日)": st.session_state.get("op_overrides", {}).get(facility_key(f)),
                    "_key": facility_key(f),
                })
            df_edit = pd.DataFrame(edit_rows)
            edited = st.data_editor(
                df_edit, hide_index=True, use_container_width=True, key="op_editor",
                disabled=["医療機関", "距離(m)", "ナビィ外来(人/日)", "_key"],
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

        # サイドバーのアサンプション＋手動上書きで常に再計算（調整に即応）
        summary = compute_capture_prediction(
            med_facs, a, op_override=st.session_state.get("op_overrides", {})
        )

        total_rx = summary["total_annual_rx"]
        c1, c2, c3 = st.columns(3)
        c1.metric("予測 年間獲得処方箋数", f"{total_rx:,.0f} 枚/年")
        c2.metric("1日あたり換算", f"{total_rx / a.fixed_annual_days:,.0f} 枚/日" if a.fixed_annual_days else "—")
        c3.metric("寄与する医療機関数", f"{summary['n_contributing']} 件")

        st.caption(
            "計算式： **獲得処方箋 = Σ 医療機関( 1日平均外来患者数 × 年間診療日数 "
            "× 院外処方係数 × 発行率 × 流入率(距離) )**  \n"
            "流入率・係数は左サイドバーの「予測アサンプション」で調整できます（即時再計算）。"
        )
        if summary["n_no_outpatient"] > 0:
            st.warning(
                f"⚠️ 外来患者数が取得できなかった医療機関が {summary['n_no_outpatient']} 件あります"
                "（予測に未算入）。大病院で「なし」の場合は病院HP/年報から手動補完を検討してください。"
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
        csv_pred = pd.DataFrame(contrib_rows)
        st.download_button(
            "⬇️ 予測内訳CSVダウンロード",
            data=csv_pred.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"処方箋獲得予測_{st.session_state.last_address[:15]}_{radius_m}m.csv",
            mime="text/csv",
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
        n_total_ph = len(pharmacies)
        n_monzen   = sum(1 for p in pharmacies if p.pharmacy_type == "門前薬局")
        n_men      = sum(1 for p in pharmacies if p.pharmacy_type == "面薬局")
        rx_vals_ph = [p.annual_rx_count for p in pharmacies if p.annual_rx_count]
        avg_rx     = int(sum(rx_vals_ph) / len(rx_vals_ph)) if rx_vals_ph else None

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("薬局 合計",        f"{n_total_ph} 件")
        col2.metric("門前薬局",          f"{n_monzen} 件")
        col3.metric("面薬局",            f"{n_men} 件")
        col4.metric("年間処方箋数（平均）", f"{avg_rx:,} 件" if avg_rx else "— 件")

        rows_ph = []
        for ph in pharmacies:
            rows_ph.append({
                "薬局名":        ph.name,
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

        # 商圏円
        folium.Circle(
            location=[center_lat, center_lon],
            radius=radius_m,
            color="gray", fill=True, fill_opacity=0.05,
            tooltip=f"商圏: {radius_m:,}m",
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
            if fac.rx_summary == "院外処方あり":
                col_med = "red"
            elif fac.rx_summary == "院内処方のみ":
                col_med = "orange"
            else:
                col_med = "gray"
            popup_html = (
                f"<b>{fac.name}</b><br>"
                f"種別: {fac.facility_category}<br>"
                f"処方: {fac.rx_summary}<br>"
                f"外来患者数: {fac.daily_outpatients or '—'} 人/日<br>"
                f"週診療日数: {fac.weekly_op_days or '—'} 日/週<br>"
                f"診療科: {fac.specialties or '—'}<br>"
                f"住所: {fac.address or '—'}"
            )
            folium.CircleMarker(
                location=[fac.lat, fac.lon],
                radius=7,
                color=col_med, fill=True, fill_color=col_med, fill_opacity=0.75,
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=f"🏥 {fac.name}（{fac.facility_category}）",
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
                f"種別: {ph.pharmacy_type}<br>"
                f"最近接医療機関: {nearest_txt}<br>"
                f"年間処方箋数: {rx_txt}<br>"
                f"住所: {ph.address or '—'}"
            )
            folium.CircleMarker(
                location=[ph.lat, ph.lon],
                radius=8,
                color=col_ph, fill=True, fill_color=col_ph, fill_opacity=0.8,
                popup=folium.Popup(popup_html_ph, max_width=280),
                tooltip=f"💊 {ph.name}（{ph.pharmacy_type}）",
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
