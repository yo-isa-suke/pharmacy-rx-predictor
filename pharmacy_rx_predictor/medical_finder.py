"""
医療機関検索ツール v3.0 — 厚生労働省ナビイ + OpenStreetMap 統合版

住所 + 半径を入力 → 圏内の医療機関を一覧表示

抽出項目:
  ① 医療機関名
  ② 指定住所からの距離（m）
  ③ 院内処方 / 院外処方の有無
  ④ 1日あたり外来患者数
  ⑤ 週あたり平均診療日数
  ⑥ 診療科目

データソース:
  - 厚生労働省「医療機能情報提供システム（ナビイ）」
    https://iryou.teikyouseido.mhlw.go.jp/
  - OpenStreetMap Overpass API (半径圏内の施設を座標付きで高速取得)
  - 国土地理院（GSI）ジオコーダー / Nominatim

v3.0 主な改善点（v2.0 からの追加）:
  - ジオコーディング精度向上（4段階クエリ: 完全住所→建物名除去→Nominatim完全→短縮）
  - 住所正規化の強化（丁目/番/号 → ハイフン変換、全角英数字対応）
  - 建物名・フロア・号室を除去した短縮住所での再試行
  - MHLW 2段階検索（1km確定圏内 + ユーザ指定半径）で取得漏れ防止
  - ジオコーディング失敗の MHLW 施設も1km圏内確定分は結果に含める
  - OSM クエリ拡張（healthcare タグ全種 + medical_centre）
  - 外来患者数: div.item[患者数]セクション優先 + ptn4ItemName クラス + 汎用スキャンの3段階
  - 外来患者列ヘッダを先頭一致で判定しツールチップ内「紹介」による誤除外を解消
  - all_fields["前年度１日平均患者数"] フォールバック追加
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

# ─── ページ設定 ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="医療機関検索ツール v3.0",
    page_icon="🏥",
    layout="wide",
)

# ─── 定数 ─────────────────────────────────────────────────────────────────────
MHLW_DOMAIN  = "https://www.iryou.teikyouseido.mhlw.go.jp"
MHLW_BASE    = MHLW_DOMAIN + "/znk-web"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
WORKING_DAYS = 305

# kikanCd 先頭桁 → kikanKbn の推定マッピング
# 1=病院, 2=診療所（一般）, 3=歯科, 4=助産所, 5=薬局
KIKAN_KBN_MAP = {"1": [1, 2], "2": [2, 1], "3": [3, 2], "4": [4, 2], "5": [5, 2]}

# OSM タグ → 日本語診療科
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

PREFECTURES = [
    "", "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府",
    "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県",
    "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県",
    "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
PREFECTURE_CODES: Dict[str, str] = {
    "北海道": "01", "青森県": "02", "岩手県": "03", "宮城県": "04", "秋田県": "05",
    "山形県": "06", "福島県": "07", "茨城県": "08", "栃木県": "09", "群馬県": "10",
    "埼玉県": "11", "千葉県": "12", "東京都": "13", "神奈川県": "14", "新潟県": "15",
    "富山県": "16", "石川県": "17", "福井県": "18", "山梨県": "19", "長野県": "20",
    "岐阜県": "21", "静岡県": "22", "愛知県": "23", "三重県": "24", "滋賀県": "25",
    "京都府": "26", "大阪府": "27", "兵庫県": "28", "奈良県": "29", "和歌山県": "30",
    "鳥取県": "31", "島根県": "32", "岡山県": "33", "広島県": "34", "山口県": "35",
    "徳島県": "36", "香川県": "37", "愛媛県": "38", "高知県": "39", "福岡県": "40",
    "佐賀県": "41", "長崎県": "42", "熊本県": "43", "大分県": "44", "宮崎県": "45",
    "鹿児島県": "46", "沖縄県": "47",
}


# ─── データクラス ──────────────────────────────────────────────────────────────
@dataclass
class PharmacyFacility:
    """薬局情報（門前/面判定・総取扱処方箋数付き）"""
    name: str
    address: str
    href: str = ""
    pref_cd: str = ""
    kikan_cd: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_m: Optional[float] = None        # 検索中心からの距離
    source: str = "mhlw"
    pharmacy_type: str = "不明"               # "門前薬局" / "面薬局" / "不明"
    nearest_clinic_name: str = "—"           # 最近接の医療機関名
    nearest_clinic_dist_m: Optional[float] = None   # 最近接の医療機関との距離
    annual_rx_count: Optional[int] = None    # 年間総取扱処方箋数
    annual_rx_source: str = "—"
    detail_fetched: bool = False
    detail_url: str = ""
    distance_note: str = ""
    raw_fields: Dict[str, str] = field(default_factory=dict)


@dataclass
class MedFacility:
    name: str
    address: str
    href: str = ""
    pref_cd: str = ""
    kikan_cd: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_m: Optional[float] = None
    source: str = "mhlw"          # "osm" / "mhlw"
    # 詳細データ
    inhouse_rx: str = "—"         # 院内処方の有無
    outpatient_rx: str = "—"      # 院外処方の有無
    rx_summary: str = "不明"       # 総合: "院外処方あり" / "院内処方のみ" / "不明"
    daily_outpatients: Optional[int] = None
    daily_outpatients_source: str = "—"   # 外来患者数の出典
    weekly_op_days: Optional[float] = None
    specialties: str = ""
    facility_category: str = "診療所"
    kikan_kbn_used: int = 2
    detail_fetched: bool = False
    detail_url: str = ""
    distance_note: str = ""        # 距離の信頼度メモ（"確認済" / "要確認(Xm差)" 等）
    # 門前薬局情報（この医療機関の門前に薬局があるか）
    monzen_pharmacy_name: str = "なし"
    monzen_pharmacy_dist_m: Optional[float] = None
    monzen_rx_count: Optional[int] = None      # 年間総取扱処方箋数
    monzen_rx_source: str = "—"
    raw_fields: Dict[str, str] = field(default_factory=dict)   # デバッグ用


# ─── ユーティリティ ────────────────────────────────────────────────────────────
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def extract_area_keyword(address: str) -> str:
    m = re.search(
        r"(東京都|大阪府|京都府|北海道)(?:(.{2,6}?[区市町村]))?|"
        r".+?[都道府県](.{2,6}?[市区町村])",
        address,
    )
    if m:
        if m.group(1) in ("東京都", "大阪府", "京都府", "北海道"):
            return m.group(2) or m.group(1)
        return m.group(3) or m.group(0)
    m2 = re.search(r"[\u4e00-\u9fff]{2,6}[市区町村]", address)
    return m2.group(0) if m2 else address[:8]


def name_similarity(a: str, b: str) -> float:
    """簡易名称類似度（共通文字数 / 長い方の長さ）"""
    a_chars = set(re.sub(r"[　\s・（）()「」]", "", a))
    b_chars = set(re.sub(r"[　\s・（）()「」]", "", b))
    if not a_chars or not b_chars:
        return 0.0
    common = len(a_chars & b_chars)
    return common / max(len(a_chars), len(b_chars))


def guess_kikan_kbn(kikan_cd: str) -> List[int]:
    """kikanCd の先頭桁から試行すべき kikanKbn リストを返す"""
    prefix = kikan_cd[0] if kikan_cd else "2"
    return KIKAN_KBN_MAP.get(prefix, [2, 1, 3])


# ─── ジオコーダー ──────────────────────────────────────────────────────────────
class GeocoderService:
    GSI_URL       = "https://msearch.gsi.go.jp/address-search/AddressSearch"
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    LAT_MIN, LAT_MAX = 24.0, 46.0
    LON_MIN, LON_MAX = 122.0, 154.0
    HEADERS = {"User-Agent": "MedicalFinderTool/3.0"}

    def _is_japan(self, lat, lon):
        return self.LAT_MIN <= lat <= self.LAT_MAX and self.LON_MIN <= lon <= self.LON_MAX

    def _clean(self, address: str) -> str:
        """住所の標準クリーニング + 全角→半角 + 丁目/番/号の正規化"""
        a = re.sub(r"〒\s*\d{3}[-−]\d{4}\s*", "", address)
        a = re.sub(r"Googleマップ.*", "", a).strip()
        # 全角英数字・記号 → 半角
        trans = str.maketrans(
            "０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
            "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ－−‐",
            "0123456789abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ---",
        )
        a = a.translate(trans).replace("　", " ")
        # 丁目/番/号 → ハイフン形式に統一（ジオコーダーの認識率向上）
        a = re.sub(r"(\d+)\s*丁目\s*(\d+)\s*番地?\s*(\d+)\s*号?", r"\1-\2-\3", a)
        a = re.sub(r"(\d+)\s*丁目\s*(\d+)\s*番地?", r"\1-\2", a)
        a = re.sub(r"(\d+)\s*番地?\s*(\d+)\s*号", r"\1-\2", a)
        a = re.sub(r"(\d+)\s*番地", r"\1", a)
        return re.sub(r"\s+", " ", a).strip()

    def _shorten(self, address: str) -> str:
        """
        建物名・フロア・号室を除去した短縮住所を返す。
        「X-Y」または「X-Y-Z」形式の番地の後に続く建物情報（スペース+文字列）を除去。
        例: 御成町15-3 東京ビル3F → 御成町15-3
        例: 高田馬場3-1-5 早稲田アトリエビル6F → 高田馬場3-1-5
        """
        a = address
        # 「数字-数字(-数字)」の後にスペース＋文字（建物名）が続く場合に除去
        a = re.sub(r"(\d+(?:[-]\d+)+)\s+[\u3000-\u9fff\uff00-\uffefA-Za-z].+$", r"\1", a)
        if a != address:
            return a.strip()
        # 階・F・号室など以降を除去（番地形式でない場合のフォールバック）
        a = re.sub(r"\s*\d+\s*(?:階|[Ff]|号室|番地)\b.*$", "", address)
        # カタカナ3文字以上の連続（典型的な建物名）以降を除去
        a = re.sub(r"\s+[\u30A0-\u30FF]{3,}.*$", "", a)
        return a.strip()

    def _gsi(self, q: str) -> Optional[Tuple[float, float]]:
        try:
            r = requests.get(self.GSI_URL, params={"q": q},
                             headers=self.HEADERS, timeout=6)
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
        """
        4段階ジオコーディング（精度優先順）:
          ① GSI: クリーニング済み完全住所
          ② GSI: 建物名・フロア除去の短縮住所
          ③ Nominatim: 完全住所（1秒待機）
          ④ Nominatim: 短縮住所
        """
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
        """
        GSI と Nominatim を両方試して結果を比較し、距離ノートを返す。
        Returns: (coords, note)
          note: "確認済(Xm差)" / "要確認(Xm差)" / "GSIのみ" / "Nominatimのみ" / "取得失敗"
        """
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


_geocoder = GeocoderService()


# ─── OSM Overpass 検索 ────────────────────────────────────────────────────────
def search_osm_medical(lat: float, lon: float, radius_m: int) -> List[MedFacility]:
    """
    OSM Overpass API で半径圏内の医療機関を高速取得。
    正確な座標を持つため、距離計算が即座に可能。
    """
    # amenity=clinic/hospital/doctors/medical_centre +
    # healthcare=* (任意の値 — doctor, hospital, clinic, centre, dentist 等すべて対象)
    # により漏れなく取得する
    query = f"""
[out:json][timeout:40];
(
  node["amenity"~"^(clinic|hospital|doctors|medical_centre)$"](around:{radius_m},{lat},{lon});
  way["amenity"~"^(clinic|hospital|doctors|medical_centre)$"](around:{radius_m},{lat},{lon});
  node["healthcare"](around:{radius_m},{lat},{lon});
  way["healthcare"](around:{radius_m},{lat},{lon});
);
out center;
"""
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, timeout=35)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    facilities: List[MedFacility] = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name:ja") or tags.get("name", "")
        if not name:
            continue

        # 座標取得 (node vs way)
        if el["type"] == "node":
            f_lat, f_lon = el.get("lat"), el.get("lon")
        else:
            center = el.get("center", {})
            f_lat, f_lon = center.get("lat"), center.get("lon")
        if f_lat is None or f_lon is None:
            continue

        # 診療科
        sp_en = (tags.get("healthcare:speciality", "")
                 or tags.get("speciality", "")
                 or tags.get("medical_system:western", "")).lower()
        sp_ja = OSM_SPECIALTY_MAP.get(sp_en, "")
        if not sp_ja:
            amenity = tags.get("amenity", "")
            healthcare = tags.get("healthcare", "")
            if amenity == "hospital" or healthcare == "hospital":
                cat = "病院"
            elif healthcare == "dentist" or "dentist" in sp_en:
                cat = "診療所"
                sp_ja = "歯科"
            else:
                cat = "診療所"
        else:
            cat = "病院" if tags.get("amenity") == "hospital" else "診療所"

        addr_parts = [
            tags.get("addr:province", ""),
            tags.get("addr:city", "") or tags.get("addr:district", ""),
            tags.get("addr:suburb", ""),
            tags.get("addr:quarter", ""),
            tags.get("addr:neighbourhood", ""),
            tags.get("addr:block", ""),
            tags.get("addr:housenumber", ""),
        ]
        address = re.sub(r"\s+", "", "".join(p for p in addr_parts if p))

        dist = haversine(lat, lon, f_lat, f_lon)
        fac = MedFacility(
            name=name, address=address, source="osm",
            lat=f_lat, lon=f_lon, distance_m=dist,
            specialties=sp_ja, facility_category=cat,
        )
        facilities.append(fac)

    facilities.sort(key=lambda x: x.distance_m or 9_999_999)
    return facilities


def search_osm_pharmacies(lat: float, lon: float, radius_m: int) -> List[PharmacyFacility]:
    """OSM Overpass API で半径圏内の薬局を取得。"""
    query = f"""
[out:json][timeout:40];
(
  node["amenity"="pharmacy"](around:{radius_m},{lat},{lon});
  way["amenity"="pharmacy"](around:{radius_m},{lat},{lon});
);
out center;
"""
    try:
        r = requests.post(OVERPASS_URL, data={"data": query}, timeout=35)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []

    pharmacies: List[PharmacyFacility] = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name:ja") or tags.get("name", "")
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
        dist = haversine(lat, lon, f_lat, f_lon)
        pharmacies.append(PharmacyFacility(
            name=name, address=address, source="osm",
            lat=f_lat, lon=f_lon, distance_m=dist,
            distance_note="OSM座標（高精度）",
        ))

    pharmacies.sort(key=lambda x: x.distance_m or 9_999_999)
    return pharmacies


def assign_monzen_to_clinics(
    med_facilities: List[MedFacility],
    pharmacies: List[PharmacyFacility],
    threshold_m: float = 50.0,
) -> None:
    """
    各医療機関の門前50m以内に薬局があるか判定し、MedFacilityに書き込む（in-place）。
    同時に、薬局側にも最近接医療機関を記録する。
    """
    for mf in med_facilities:
        if mf.lat is None or mf.lon is None:
            mf.monzen_pharmacy_name = "不明（座標なし）"
            continue
        best_dist = float("inf")
        best_ph: Optional[PharmacyFacility] = None
        for ph in pharmacies:
            if ph.lat is None or ph.lon is None:
                continue
            d = haversine(mf.lat, mf.lon, ph.lat, ph.lon)
            if d < best_dist:
                best_dist = d
                best_ph = ph
        # 薬局側にも最近接医療機関を記録
        if best_ph is not None:
            if best_ph.nearest_clinic_dist_m is None or best_dist < best_ph.nearest_clinic_dist_m:
                best_ph.nearest_clinic_name = mf.name
                best_ph.nearest_clinic_dist_m = best_dist
        # 医療機関側に門前情報を書き込む
        if best_ph is not None and best_dist <= threshold_m:
            mf.monzen_pharmacy_name = best_ph.name
            mf.monzen_pharmacy_dist_m = best_dist
            mf.monzen_rx_count = best_ph.annual_rx_count
            mf.monzen_rx_source = best_ph.annual_rx_source
        else:
            mf.monzen_pharmacy_name = "なし"
            mf.monzen_pharmacy_dist_m = None

    # 薬局側の門前/面ラベルも付与（薬局タブ用）
    for ph in pharmacies:
        if ph.nearest_clinic_dist_m is not None and ph.nearest_clinic_dist_m <= threshold_m:
            ph.pharmacy_type = "門前薬局"
        elif ph.nearest_clinic_dist_m is not None:
            ph.pharmacy_type = "面薬局"
        else:
            ph.pharmacy_type = "不明"


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
        """S2320 ページをロードしてセッションを確立する"""
        if self._ready:
            return True
        try:
            r = self.session.get(
                f"{MHLW_BASE}/juminkanja/S2320/initialize", timeout=15)
            self._ready = r.status_code == 200
        except Exception:
            self._ready = False
        return self._ready

    # ── 緯度・経度からエリア内医療機関を検索（新APIフロー） ────────────────
    def search_medical_by_latlon(
        self,
        lat: float,
        lon: float,
        radius_m: int,
        center_name: str = "",
        max_pages: int = 10,
    ) -> Tuple[List[MedFacility], str]:
        """
        S2320 → S2400 の3ステップAJAXフローで医療機関を検索する。
          1. GET S2320/initialize  — セッション確立
          2. GET S2320/initsearch  — 検索セッション初期化
          3. GET S2320/search      — 緯度経度・距離で検索 → redirectUrl
          4. GET redirectUrl       — S2400 一覧ページをページングしながら取得
        """
        if not self._init():
            return [], "MHLW接続エラー（セッション確立失敗）"

        # MHLW が対応する距離コード: 00=1km, 01=5km, ""=制限なし
        if radius_m <= 1_000:
            dist_code = "00"
        elif radius_m <= 5_000:
            dist_code = "01"
        else:
            dist_code = ""          # 制限なし（広域検索）

        # ─ Step 1: initsearch ─────────────────────────────────────────────
        try:
            r1 = self.session.get(
                f"{MHLW_BASE}/juminkanja/S2320/initsearch", timeout=12)
            if r1.status_code != 200:
                return [], "MHLW initsearch エラー"
        except Exception as e:
            return [], f"MHLW initsearch 例外: {e}"

        # ─ Step 2: search（JSON レスポンス → redirectUrl） ────────────────
        try:
            r2 = self.session.get(
                f"{MHLW_BASE}/juminkanja/S2320/search",
                params={
                    "specifyDateAndTime": "01",   # 時刻指定なし
                    "centerPointName": urllib.parse.quote(center_name or "検索地点"),
                    "latitude": str(lat),
                    "longitude": str(lon),
                    "selectCenterPoint": "",
                    "distanceFromCenterPoint": dist_code,
                    "medicalCare": ["1", "2"],    # 1=病院, 2=診療所
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

        # ─ Step 3: S2400 一覧ページをページングで取得 ─────────────────────
        all_cands: List[MedFacility] = []
        total = 0
        for page in range(max_pages):
            try:
                sep = "&" if "?" in redirect_url else "?"
                page_url = f"{redirect_url}{sep}page={page}&size=20&sortNo=2"
                r3 = self.session.get(page_url, timeout=15)
                if r3.status_code != 200:
                    break
                cands, t = self._parse_list(r3.text)
                if page == 0:
                    total = t
                if not cands:
                    break
                all_cands.extend(cands)
                if len(all_cands) >= total:
                    break
                time.sleep(0.3)
            except Exception:
                break

        dist_str = f"{radius_m//1000}km" if radius_m >= 1000 else f"{radius_m}m"
        msg = (f"MHLW: 緯度{lat:.4f}/経度{lon:.4f} {dist_str} "
               f"→ 全{total}件 / 取得{len(all_cands)}件")
        return all_cands, msg

    def search_pharmacies_by_latlon(
        self,
        lat: float, lon: float,
        radius_m: int,
        center_name: str = "",
        max_pages: int = 8,
    ) -> Tuple[List[PharmacyFacility], str]:
        """ナビィで薬局（kikanKbn=5）を緯度経度検索する。"""
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
                    "latitude": str(lat),
                    "longitude": str(lon),
                    "selectCenterPoint": "",
                    "distanceFromCenterPoint": dist_code,
                    "medicalCare": ["5"],     # 5=薬局
                    "searchTypes": "01-2",
                },
                timeout=15,
            )
            j = r2.json()
            if j.get("code") != "0":
                return [], f"薬局検索エラー: {j.get('messages')}"
            redirect_url = j["result"]["redirectUrl"]
        except Exception as e:
            return [], f"薬局検索例外: {e}"

        all_ph: List[PharmacyFacility] = []
        total = 0
        for page in range(max_pages):
            try:
                sep = "&" if "?" in redirect_url else "?"
                r3 = self.session.get(
                    f"{redirect_url}{sep}page={page}&size=20&sortNo=2", timeout=15)
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

        dist_str = f"{radius_m//1000}km" if radius_m >= 1000 else f"{radius_m}m"
        return all_ph, f"ナビィ薬局: {dist_str}圏内 全{total}件 / 取得{len(all_ph)}件"

    def _parse_pharmacy_list(self, html: str) -> Tuple[List[PharmacyFacility], int]:
        """S2400 薬局一覧ページからPharmacyFacilityリストを生成する。"""
        soup = BeautifulSoup(html, "html.parser")
        results: List[PharmacyFacility] = []
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
            href = link.get("href", "")
            if href.startswith("/"):
                href = MHLW_DOMAIN + href
            pref_cd = re.search(r"prefCd=(\d+)", href)
            kikan_cd = re.search(r"kikanCd=(\w+)", href)
            pref_cd = pref_cd.group(1) if pref_cd else ""
            kikan_cd = kikan_cd.group(1) if kikan_cd else ""
            addr_tag = item.find("p", class_=re.compile(r"address|addr"))
            address = addr_tag.get_text(strip=True) if addr_tag else ""
            results.append(PharmacyFacility(
                name=name, address=address, href=href,
                pref_cd=pref_cd, kikan_cd=kikan_cd, source="mhlw",
            ))
        return results, total

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

        # 全フィールド収集
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

        # 総取扱処方箋数を抽出
        ph.annual_rx_count, ph.annual_rx_source = _parse_annual_rx_count(all_fields, text)
        ph.detail_fetched = True
        return True

    def _parse_list(self, html: str) -> Tuple[List[MedFacility], int]:
        """S2400 一覧ページから施設情報を抽出する。"""
        soup = BeautifulSoup(html, "html.parser")
        cands: List[MedFacility] = []
        total = 0

        # 総件数を取得
        m = re.search(r"(\d{1,6})\s*件", soup.get_text())
        if m:
            total = int(m.group(1))

        # div.item ── 各施設カード（h3.name > a[href] に kikanCd・kikanKbn が埋め込み済み）
        for item in soup.find_all("div", class_="item"):
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
            qp = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(href).query))
            # kikanKbn はリスト画面の href に既に含まれる
            kikan_kbn = int(qp.get("kikanKbn", "2"))

            # 住所: <p> タグに「〒XXXXXX 住所Googleマップで見る」形式
            address = ""
            for p_tag in item.find_all("p"):
                raw = p_tag.get_text(" ", strip=True)
                if "〒" in raw or re.search(r"[都道府県市区町村]", raw):
                    cleaned = re.sub(r"〒\s*\d{3}[-－]\d{4}", "", raw)
                    cleaned = re.sub(r"Googleマップで見る", "", cleaned)
                    cleaned = re.sub(r"\s+", " ", cleaned).strip()
                    if cleaned:
                        address = cleaned[:120]
                        break

            # 診療科目（テキストから抽出）
            specialty_text = ""
            all_text = item.get_text(" ", strip=True)
            sp_m = re.search(
                r"(?:内科|外科|皮膚科|眼科|耳鼻|小児|精神|産婦|泌尿|整形|循環|消化|呼吸|神経|歯科)",
                all_text,
            )
            if sp_m:
                specialty_text = all_text[sp_m.start():sp_m.start() + 60]
                specialty_text = re.sub(r"\s+", " ", specialty_text).strip()

            if name:
                cands.append(MedFacility(
                    name=name,
                    address=address,
                    href=href,
                    pref_cd=qp.get("prefCd", ""),
                    kikan_cd=qp.get("kikanCd", ""),
                    kikan_kbn_used=kikan_kbn,
                    source="mhlw",
                    specialties=specialty_text[:80],
                ))
        return cands, max(total, len(cands))

    # ── 施設詳細ページ取得・パース ────────────────────────────────────────────
    def get_facility_detail(self, fac: MedFacility) -> bool:
        """
        MHLW 詳細ページ（全タブのコンテンツが1HTMLに含まれる）を取得・パース。
        kikanKbn を自動判定（kikanCd 先頭桁から推定 → 失敗時は別値で再試行）。
        """
        self._init()
        if not (fac.pref_cd and fac.kikan_cd):
            return False

        # リストページ取得時に kikanKbn が確定している場合はそれを優先、
        # 未確定（デフォルト2）の場合は kikanCd 先頭桁から推定してフォールバック付きで試行
        known_kbn = fac.kikan_kbn_used  # リストページ href から取得済み
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
                # エラーページ検出
                if "E-0109" in text or "データは存在しません" in text:
                    continue
                # 有効なページ（施設名が含まれているか / 十分な内容量）
                if fac.name[:4] in text or len(text) > 50_000:
                    soup = candidate_soup
                    used_kbn = kbn
                    fac.detail_url = url
                    fac.kikan_kbn_used = kbn
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

        # ── 施設カテゴリ ─────────────────────────────────────────────────
        if used_kbn == 1:
            fac.facility_category = "病院"
        elif used_kbn == 3:
            fac.facility_category = "歯科診療所"
        elif "病院" in fac.name:
            fac.facility_category = "病院"

        # ── 院内処方 / 院外処方 ──────────────────────────────────────────
        inhouse = _get_field(all_fields, [
            "院内処方の有無", "院内処方", "調剤（院内処方）", "院内調剤",
        ])
        outpatient = _get_field(all_fields, [
            "院外処方の有無", "院外処方", "調剤（院外処方）", "院外調剤",
            "処方せんの交付", "処方箋の交付",
        ])
        fac.inhouse_rx   = inhouse   or "—"
        fac.outpatient_rx = outpatient or "—"

        if outpatient and "有" in outpatient:
            fac.rx_summary = "院外処方あり"
        elif inhouse and "有" in inhouse and (not outpatient or "無" in outpatient or "不可" in outpatient):
            fac.rx_summary = "院内処方のみ"
        elif inhouse or outpatient:
            fac.rx_summary = f"院内:{inhouse or '—'} / 院外:{outpatient or '—'}"
        else:
            # フォールバック: フルテキスト検索
            fac.rx_summary = _infer_rx_type(full_text)

        # ── 1日平均外来患者数（ナビィのみ） ──────────────────────────────────
        fac.daily_outpatients, fac.daily_outpatients_source = \
            _parse_daily_outpatients(all_fields, full_text, soup)

        # ── 週あたり診療日数 ──────────────────────────────────────────────
        fac.weekly_op_days = _parse_weekly_days(all_fields, full_text, soup)

        # ── 診療科目 ──────────────────────────────────────────────────────
        if not fac.specialties:
            fac.specialties = _parse_specialties(all_fields, full_text)

        fac.detail_fetched = True
        return True


# ─── フィールドパーサ群 ────────────────────────────────────────────────────────
def _parse_annual_rx_count(
    fields: Dict[str, str], full_text: str
) -> Tuple[Optional[int], str]:
    """
    薬局の年間総取扱処方箋数を抽出。
    Returns: (count, source_description)
    """
    candidate_keys = [
        "処方箋受付回数（年間）",
        "処方箋受付枚数（年間）",
        "処方箋受付回数",
        "処方箋受付枚数",
        "調剤処方箋の受付枚数",
        "取扱処方箋数",
        "総取扱処方箋数",
        "年間処方箋受付回数",
        "年間処方箋取扱枚数",
        "処方箋枚数",
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


def _get_field(fields: Dict[str, str], keys: List[str]) -> Optional[str]:
    """複数の候補キーのうち、最初にマッチしたフィールド値を返す"""
    for k in keys:
        if k in fields:
            return fields[k]
    # 部分一致フォールバック
    for k in keys:
        for fk, fv in fields.items():
            if k in fk:
                return fv
    return None


def _infer_rx_type(full_text: str) -> str:
    """フルテキストから院内/院外処方を推定（フィールドが無い場合のフォールバック）"""
    lines = [l.strip() for l in full_text.split("\n") if l.strip()]
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


def _parse_daily_outpatients(
    fields: Dict[str, str],
    full_text: str,
    soup: Optional[BeautifulSoup] = None,
) -> Tuple[Optional[int], str]:
    """
    1日平均外来患者数を抽出。ナビィデータのみを対象とする。
    Returns: (value, source_description)
    優先順:
      1. 統計テーブル「前年度１日平均患者数」行の「外来患者」列（最も信頼性高）
      2. フィールド名「前年度１日平均患者数」等
      3. フィールド名「1日あたりの外来患者の平均数」等
      4. フルテキスト正規表現（ナビィページ内）
    """
    # ─ 優先①: 統計テーブルから直接取得（最も正確）────────────────────────
    if soup is not None:
        result = _parse_outpatients_from_stats_table(soup)
        if result is not None:
            return result, "ナビィ（実績統計表）"

    # ─ 優先②: all_fields["前年度１日平均患者数"] ────────────────────────────
    v_zennen = fields.get("前年度１日平均患者数", "")
    if v_zennen:
        patient_vals = re.findall(r"(\d+\.?\d*)人", v_zennen)
        if len(patient_vals) >= 2:
            n = float(patient_vals[-2])
            if 0 < n <= 10_000:
                return int(round(n)), "ナビィ（前年度フィールド）"
        elif len(patient_vals) == 1:
            n = float(patient_vals[0])
            if 0 < n <= 10_000:
                return int(round(n)), "ナビィ（前年度フィールド）"

    # ─ 優先③: フィールド名マッチ ─────────────────────────────────────────
    candidate_keys = [
        "1日あたりの外来患者の平均数",
        "外来患者の平均数",
        "1日平均外来患者数",
        "一日平均外来患者数",
        "外来患者数（1日平均）",
        "前年度の１日平均外来患者数",
        "前年度１日平均外来患者数",
        "１日平均外来患者数",
        "1日あたり外来患者数",
        "外来（1日平均）",
        "外来患者の延数",
        "外来患者数",
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

    # ─ 優先④: フルテキスト正規表現（ナビィページ内テキスト）──────────────
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
    """
    週あたり平均診療日数を抽出。
    優先順:
      1. フィールド「週の診療日数」等
      2. 診療時間テーブルから曜日をカウント
      3. フルテキスト「週X日」パターン
    """
    # ① フィールド直接取得
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

    # ② 診療科目別診療時間フィールドの「/」区切り値から曜日数をカウント
    # 例: "09:30-13:30 / 09:30-13:30 / - / 09:30-13:30 / 09:30-13:00 / - / - / -"
    #      月            火              水  木              金               土  日  祝
    schedule_keys = [
        "診療時間（診療科目別の）", "診療科目別の診療時間",
        "外来受付時間（診療科目別の）", "診療時間帯",
    ]
    sv = _get_field(fields, schedule_keys)
    if sv and "/" in sv:
        parts = [p.strip() for p in sv.split("/")]
        # 最初の7〜8要素が月〜日（〜祝）に対応
        open_slots = [
            p for p in parts[:8]
            if p and p not in ["-", "−", "—", "休", "×"]
            and re.search(r"\d{1,2}:\d{2}", p)
        ]
        # 複数行がある場合（同日に複数時間帯）は重複カウントしないため
        # 同一インデックスの曜日を一度だけカウント
        day_indices = set()
        for i, p in enumerate(parts[:8]):
            if p and p not in ["-", "−", "—", "休", "×"] and re.search(r"\d{1,2}:\d{2}", p):
                day_indices.add(i)
        if day_indices:
            return float(len(day_indices))

    # ③ 診療時間テーブルから曜日をカウント
    if soup:
        days = _count_open_days_from_hours_table(soup)
        if days:
            return float(days)

    # ④ フルテキスト「週X日」
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
    """
    診療時間テーブルから診療している曜日数を数える。
    テーブルの行/列に「月・火・水・木・金・土・日」がある場合にカウント。
    """
    WEEKDAY_CHARS = ["月", "火", "水", "木", "金", "土", "日"]
    open_days: set = set()

    for table in soup.find_all("table"):
        headers = []
        first_row = table.find("tr")
        if first_row:
            cells = first_row.find_all(["th", "td"])
            headers = [c.get_text(strip=True) for c in cells]

        # 列ヘッダが曜日の場合（行: 診療時間帯 / 列: 曜日）
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
                        # 値が「○」「あり」「時間帯記載」の場合はその曜日に診療あり
                        if v and v not in ["×", "✗", "−", "-", "休", "—", ""]:
                            if not re.fullmatch(r"[×✗−\-休―‐ー]", v):
                                open_days.add(wd)

        # 行ヘッダが曜日の場合（行: 曜日 / 列: 診療時間）
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            header = cells[0].get_text(strip=True)
            for wd in WEEKDAY_CHARS:
                if wd in header:
                    # 他のセルに時間が記載されているか
                    for cell in cells[1:]:
                        v = cell.get_text(strip=True)
                        if re.search(r"\d{1,2}:\d{2}", v):
                            open_days.add(wd)

    return len(open_days) if open_days else None


def _extract_outpatient_from_table(table) -> Optional[int]:
    """
    テーブルから外来患者数を抽出するヘルパー。
    病院形式（多列ヘッダ）と診療所形式（2列）の両方に対応。
    """
    rows = table.find_all("tr")
    if not rows:
        return None

    header_cells = rows[0].find_all(["th", "td"])
    header_texts = [re.sub(r"\s+", "", c.get_text(strip=True)) for c in header_cells]

    # ─ 形式A: 病院形式（ヘッダ行に「外来患者」列がある） ──────────────────
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

    # ─ 形式B: 診療所形式（行ラベルに「外来」を含む2列テーブル） ──────────
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

    # ─ 形式C: 「前年度」行に数値が1つだけある（シンプルな診療所フォーマット）
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
    """
    「医療実績、結果に関する事項」タブの「患者数」セクションから
    前年度１日平均外来患者数を抽出する。3段階アプローチ。

    テーブル構造（例）:
      ヘッダ行: ['', '一般病床', '療養病床', '精神病床', '', '結核病床', '感染症病床', '外来患者', '在宅患者']
      中間行:  ['うち指定病床']
      データ行: ['前年度１日平均患者数', '-', '-', '-', '-', '-', '-', '60.4人', '0人']
    """
    # ─ アプローチ①: div.item[患者数] セクション内テーブルを優先スキャン ──
    # 「患者数」見出しを持つ div.item を特定し、その中のテーブルだけを対象にする
    for item_div in soup.find_all("div", class_="item"):
        h3 = item_div.find("h3")
        if not h3 or "患者数" not in h3.get_text(strip=True):
            continue
        for table in item_div.find_all("table"):
            result = _extract_outpatient_from_table(table)
            if result is not None:
                return result

    # ─ アプローチ②: class="ptn4ItemName" の th から前年度行を直接特定 ──
    for data_th in soup.find_all("th", class_="ptn4ItemName"):
        if "前年度" not in data_th.get_text(strip=True):
            continue
        table = data_th.find_parent("table")
        if table is None:
            continue
        result = _extract_outpatient_from_table(table)
        if result is not None:
            return result

    # ─ アプローチ③: 全テーブルを汎用スキャン（フォールバック）──────────
    for table in soup.find_all("table"):
        result = _extract_outpatient_from_table(table)
        if result is not None:
            return result

    return None


def _parse_specialties(fields: Dict[str, str], full_text: str) -> str:
    """
    診療科目を抽出。
    フィールド「診療科目」「診療科」等から取得。
    """
    candidate_keys = ["診療科目", "診療科", "標榜診療科", "診療科名"]
    v = _get_field(fields, candidate_keys)
    if v:
        # 長すぎる場合は最初の部分だけ
        parts = re.split(r"[、。\n/／・]", v)
        sp_list = [p.strip() for p in parts if p.strip() and len(p.strip()) <= 15]
        return "、".join(sp_list[:6])
    return ""


# ─── ゾーン判定 ───────────────────────────────────────────────────────────────
def get_zone_label(distance_m: Optional[float], zones: List[int]) -> str:
    """距離からゾーンラベルを返す。zones は昇順の4要素リスト [z1, z2, z3, z4]"""
    if distance_m is None:
        return "不明"
    for z in zones:
        if distance_m <= z:
            return f"〜{z}m"
    return f"{zones[-1]}m超"


# ─── セッション状態 ────────────────────────────────────────────────────────────
_defaults = {
    "results": [],
    "pharmacies": [],
    "center_lat": None,
    "center_lon": None,
    "search_log": [],
    "last_address": "",
    "last_zones": [50, 500, 1000, 2000, 5000],
}
for k, v in _defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─── UI ────────────────────────────────────────────────────────────────────────
st.title("🏥 医療機関検索ツール v3.2")
st.caption(
    "住所とゾーン境界を入力 → 圏内の医療機関の **院内/院外処方・外来患者数・週診療日数・診療科** をゾーン別に表示。  \n"
    "ナビイ再検索 + OSM再検索の **二重確認**で漏れを防止します。  \n"
    "データソース: **厚生労働省ナビイ** + **OpenStreetMap**"
)

# ── サイドバー ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔍 検索条件")

    address_input = st.text_input(
        "📍 住所",
        placeholder="例: 東京都新宿区高田馬場3丁目",
        help="都道府県から入力してください。丁目・番地まで入力するとより正確です。",
    )

    st.subheader("📏 ゾーン境界設定")
    num_zones = st.radio(
        "ゾーン数", [3, 4, 5], index=1, horizontal=True,
        help="使用するゾーンの数を選択。不要なゾーンは非表示になります。",
    )

    _zone_defaults = [50, 500, 1000, 2000, 5000]
    _zone_labels   = ["ゾーン1", "ゾーン2", "ゾーン3", "ゾーン4", "ゾーン5"]
    _zone_steps    = [10, 50, 100, 100, 500]
    _zone_maxvals  = [5000, 5000, 10000, 10000, 10000]

    raw_zone_vals = []
    cols_row1 = st.columns(min(num_zones, 3))
    cols_row2 = st.columns(num_zones - 3) if num_zones > 3 else []
    all_cols = list(cols_row1) + list(cols_row2)

    for i in range(num_zones):
        v = all_cols[i].number_input(
            f"{_zone_labels[i]} (m)",
            min_value=10, max_value=_zone_maxvals[i],
            value=_zone_defaults[i], step=_zone_steps[i],
        )
        raw_zone_vals.append(int(v))

    zones = sorted(raw_zone_vals)
    radius_m_max = zones[-1]

    _zone_colors_icon = ["🔵", "🟢", "🟡", "🔴", "🟣"]
    zone_summary = " ／ ".join(
        f"{_zone_colors_icon[i]} Z{i+1}: 〜**{z:,}m**" for i, z in enumerate(zones)
    )
    st.caption(zone_summary)

    pref_sel = st.selectbox(
        "🗾 都道府県（任意・検索精度向上）",
        PREFECTURES,
    )

    st.divider()
    st.subheader("⚙️ 詳細設定")

    max_detail = st.slider(
        "詳細取得件数の上限",
        min_value=5, max_value=80, value=30, step=5,
        help="MHLWから詳細データを取得する最大件数（1件≈1秒）。",
    )
    use_osm = st.checkbox(
        "OSM で補完取得",
        value=True,
        help="OpenStreetMap Overpass API で半径圏内の施設を追加取得。\n"
             "MHLWにない施設も含めて網羅的に検索できます。",
    )
    search_pharmacies = st.checkbox(
        "💊 近隣薬局も検索する",
        value=True,
        help="圏内の調剤薬局をOSM + ナビィで検索し、門前/面を自動判定します。\n"
             "ナビィから年間総取扱処方箋数も取得します。",
    )
    pharmacy_gate_m = st.number_input(
        "門前判定距離 (m)",
        min_value=10, max_value=500, value=50, step=10,
        help="医療機関との距離がこの値以内の薬局を「門前薬局」と判定します。",
    )
    verify_distance = st.checkbox(
        "🔍 距離二重確認モード",
        value=True,
        help="GSI と Nominatim の両方でジオコーディングし、結果が大きく異なる施設を"
             "「要確認」フラグで表示します。精度向上しますが検索時間が増加します。",
    )
    show_debug = st.checkbox("🔧 全取得フィールドを表示（デバッグ）", value=False)

    st.divider()
    run_btn = st.button(
        "🚀 検索実行",
        type="primary",
        use_container_width=True,
        disabled=not address_input.strip(),
    )

# ── 検索実行 ──────────────────────────────────────────────────────────────────
if run_btn and address_input.strip():
    log: List[str] = []
    results: List[MedFacility] = []
    scraper = MHLWScraper()
    pref_code = PREFECTURE_CODES.get(pref_sel, "")
    radius_m = radius_m_max

    prog = st.progress(0, text="[1/5] 住所のジオコーディング中…")

    # Step1: ジオコーディング
    coords = _geocoder.geocode(address_input.strip())
    if not coords:
        st.error("❌ 住所のジオコーディングに失敗しました。住所を確認してください。")
        st.stop()
    center_lat, center_lon = coords
    log.append(f"✅ ジオコーディング: lat={center_lat:.5f}, lon={center_lon:.5f}")
    prog.progress(8, text="[2/5] OSM で圏内施設を取得中…")

    # Step2: OSM Overpass（高速・座標付き — OSM座標は地図由来で高精度）
    osm_results: List[MedFacility] = []
    if use_osm:
        osm_results = search_osm_medical(center_lat, center_lon, radius_m)
        for osm_f in osm_results:
            osm_f.distance_note = "OSM座標（高精度）"
        log.append(f"🗺️ OSM取得: {len(osm_results)}件（半径{radius_m}m圏内）")
    prog.progress(20, text=f"[3/5] MHLW からエリア内医療機関を検索中（緯度/経度ベース）…")

    area_kw = extract_area_keyword(address_input.strip())

    # Step3: MHLW 2段階検索（漏れ防止）
    #   ① 1km 確定圏内検索: MHLW が距離を保証するためジオコーディング不要で全件追加
    #   ② ユーザ指定半径検索: ①と重複しない施設についてジオコーディングで実距離確認
    mhlw_1km_cands, msg_1km = scraper.search_medical_by_latlon(
        center_lat, center_lon,
        radius_m=1000,
        center_name=area_kw,
        max_pages=5,
    )
    log.append(f"📋 {msg_1km}")
    mhlw_1km_kikan_cds = {f.kikan_cd for f in mhlw_1km_cands if f.kikan_cd}

    mhlw_extra_cands: List[MedFacility] = []
    if radius_m > 1000:
        mhlw_extra_cands, msg_extra = scraper.search_medical_by_latlon(
            center_lat, center_lon,
            radius_m=radius_m,
            center_name=area_kw,
            max_pages=12,
        )
        log.append(f"📋 {msg_extra}")
        # 1km 検索で既に取得済みの施設は除外（重複排除）
        mhlw_extra_cands = [
            f for f in mhlw_extra_cands if f.kikan_cd not in mhlw_1km_kikan_cds
        ]

    # 合算: 1km確定 + 追加（重複なし）
    mhlw_cands = mhlw_1km_cands + mhlw_extra_cands
    log.append(f"📋 MHLW合算: {len(mhlw_cands)}件（1km={len(mhlw_1km_cands)}件, 追加={len(mhlw_extra_cands)}件）")
    prog.progress(33, text=f"[3/5] {len(mhlw_cands)}件の住所をジオコーディング中…")

    # Step4: MHLW 施設のジオコーディング + 距離フィルタ
    # ・1km 検索結果: MHLW が1km以内を保証 → ジオコーディング失敗でも追加
    # ・追加検索結果: ジオコーディング成功かつ radius_m 以内のみ追加
    mhlw_in_radius: List[MedFacility] = []
    geocode_failed = 0
    for i, fac in enumerate(mhlw_cands):
        pct = 33 + int(17 * (i + 1) / max(len(mhlw_cands), 1))
        prog.progress(pct,
                      text=f"[3/5] ジオコーディング中 {i+1}/{len(mhlw_cands)}: {fac.name[:18]}…")
        is_1km = fac.kikan_cd in mhlw_1km_kikan_cds

        if not fac.address:
            if is_1km:
                # 住所なしでも1km圏内確定のため追加
                fac.distance_m = None
                mhlw_in_radius.append(fac)
            continue

        if verify_distance:
            gc, dist_note = _geocoder.geocode_with_verification(fac.address)
            fac.distance_note = dist_note
        else:
            gc = _geocoder.geocode(fac.address)
            fac.distance_note = ""
        if gc:
            fac.lat, fac.lon = gc
            fac.distance_m = haversine(center_lat, center_lon, fac.lat, fac.lon)
            if fac.distance_m <= radius_m:
                mhlw_in_radius.append(fac)
        else:
            geocode_failed += 1
            if is_1km:
                fac.distance_m = None
                fac.distance_note = "座標取得失敗"
                mhlw_in_radius.append(fac)
        time.sleep(0.10)

    log.append(f"📍 MHLW 圏内（距離フィルタ後）: {len(mhlw_in_radius)}件")
    if geocode_failed:
        log.append(f"⚠️ ジオコーディング失敗: {geocode_failed}件（1km圏内確定分は追加済み）")

    # Step5: OSM + MHLW をマージ（名前重複排除）
    merged: List[MedFacility] = list(osm_results)
    for mf in mhlw_in_radius:
        # OSM と名前が 70% 以上一致する場合は MHLW 情報でOSMを補完（MHLW 優先）
        matched = False
        for i, osm_f in enumerate(merged):
            if name_similarity(mf.name, osm_f.name) >= 0.70:
                merged[i].pref_cd = mf.pref_cd
                merged[i].kikan_cd = mf.kikan_cd
                merged[i].kikan_kbn_used = mf.kikan_kbn_used
                merged[i].href = mf.href
                merged[i].source = "osm+mhlw"
                if not merged[i].address and mf.address:
                    merged[i].address = mf.address
                if not merged[i].specialties and mf.specialties:
                    merged[i].specialties = mf.specialties
                matched = True
                break
        if not matched:
            merged.append(mf)

    merged.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(f"🔗 マージ後: {len(merged)}件")
    prog.progress(55, text=f"[4/5] {min(len(merged), max_detail)}件の詳細データを取得中…")

    # Step6: MHLW 詳細ページ取得
    targets = [f for f in merged if f.pref_cd and f.kikan_cd][:max_detail]
    for i, fac in enumerate(targets):
        pct = 55 + int(40 * (i + 1) / max(len(targets), 1))
        prog.progress(
            pct,
            text=f"[4/5] 詳細取得中 ({i+1}/{len(targets)}): {fac.name[:22]}…"
        )
        ok = scraper.get_facility_detail(fac)
        if ok:
            log.append(f"  ✅ {fac.name[:25]} kbn={fac.kikan_kbn_used} "
                       f"rx={fac.rx_summary} op={fac.daily_outpatients} wd={fac.weekly_op_days}")
        else:
            log.append(f"  ⏸ {fac.name[:25]} 詳細取得失敗")
        time.sleep(0.6)

    log.append(f"✅ 完了: 詳細取得済 {sum(1 for f in targets if f.detail_fetched)}件")

    # ── Step7: 推敲① — ナビイ再検索で漏れ確認 ───────────────────────────────
    prog.progress(96, text="[5/7] 推敲①: ナビイ再検索で漏れ確認中…")
    existing_kikan_cds = {f.kikan_cd for f in merged if f.kikan_cd}
    existing_names     = [f.name for f in merged]

    verify_mhlw, msg_v1 = scraper.search_medical_by_latlon(
        center_lat, center_lon,
        radius_m=radius_m,
        center_name=area_kw,
        max_pages=12,
    )
    added_from_mhlw = 0
    for vf in verify_mhlw:
        # kikanCd で重複チェック
        if vf.kikan_cd and vf.kikan_cd in existing_kikan_cds:
            continue
        # 名前類似度で重複チェック
        if any(name_similarity(vf.name, en) >= 0.70 for en in existing_names):
            continue
        # 距離確認
        if not vf.address:
            continue
        gc = _geocoder.geocode(vf.address)
        if gc:
            vf.lat, vf.lon = gc
            vf.distance_m = haversine(center_lat, center_lon, vf.lat, vf.lon)
            if vf.distance_m <= radius_m:
                vf.source = "mhlw(再確認追加)"
                merged.append(vf)
                existing_names.append(vf.name)
                if vf.kikan_cd:
                    existing_kikan_cds.add(vf.kikan_cd)
                added_from_mhlw += 1
        time.sleep(0.10)

    # Step7で追加されたMHLW施設の詳細をここで取得
    new_mhlw_facs = [f for f in merged
                     if f.source == "mhlw(再確認追加)" and not f.detail_fetched
                     and f.pref_cd and f.kikan_cd]
    for i, fac in enumerate(new_mhlw_facs):
        prog.progress(97, text=f"[5/7] 推敲①: 追加施設の詳細取得中 ({i+1}/{len(new_mhlw_facs)}): {fac.name[:20]}…")
        ok = scraper.get_facility_detail(fac)
        log.append(f"  {'✅' if ok else '⏸'} [推敲①追加] {fac.name[:25]} "
                   f"rx={fac.rx_summary} op={fac.daily_outpatients}")
        time.sleep(0.6)

    log.append(f"🔍 推敲①ナビイ再検索: {len(verify_mhlw)}件確認 → 追加 {added_from_mhlw}件")

    # ── Step8: 推敲② — OSM 再検索で漏れ確認 ────────────────────────────────
    prog.progress(98, text="[7/7] 推敲②: OSM 再検索で漏れ確認中…")
    verify_osm = search_osm_medical(center_lat, center_lon, radius_m)
    added_from_osm = 0
    new_osm_facs = []
    for vf in verify_osm:
        if any(name_similarity(vf.name, en) >= 0.70 for en in existing_names):
            continue
        vf.source = "osm(再確認追加)"
        merged.append(vf)
        new_osm_facs.append(vf)
        existing_names.append(vf.name)
        added_from_osm += 1

    # Step8で追加されたOSM施設もナビィで詳細取得を試みる（名前でkikanCd探索）
    for i, fac in enumerate(new_osm_facs):
        if not fac.lat or not fac.lon:
            continue
        prog.progress(99, text=f"[7/7] 推敲②: 追加施設のナビィ照合中 ({i+1}/{len(new_osm_facs)}): {fac.name[:20]}…")
        # 座標周辺100mでナビィ再検索して名前一致するものの詳細を取得
        nearby, _ = scraper.search_medical_by_latlon(fac.lat, fac.lon, radius_m=200, max_pages=2)
        for nf in nearby:
            if name_similarity(fac.name, nf.name) >= 0.65:
                fac.pref_cd = nf.pref_cd
                fac.kikan_cd = nf.kikan_cd
                fac.kikan_kbn_used = nf.kikan_kbn_used
                fac.href = nf.href
                fac.source = "osm+mhlw(再確認追加)"
                ok = scraper.get_facility_detail(fac)
                log.append(f"  {'✅' if ok else '⏸'} [推敲②追加] {fac.name[:25]} "
                           f"rx={fac.rx_summary} op={fac.daily_outpatients}")
                break
        time.sleep(0.5)

    log.append(f"🔍 推敲②OSM再検索: {len(verify_osm)}件確認 → 追加 {added_from_osm}件")

    merged.sort(key=lambda x: x.distance_m or 9_999_999)

    total_added = added_from_mhlw + added_from_osm
    if total_added > 0:
        log.append(f"⚠️ 二重確認で合計 {total_added}件 を追加しました（推敲①:{added_from_mhlw}件 推敲②:{added_from_osm}件）")
    else:
        log.append("✅ 二重確認: 追加漏れなし（ナビイ・OSM ともに差分ゼロ）")

    results = merged

    # ── Step9: 近隣薬局検索 + 門前/面判定 ────────────────────────────────
    pharmacies: List[PharmacyFacility] = []
    if search_pharmacies:
        prog.progress(99, text="💊 近隣薬局を検索中（OSM）…")
        ph_osm = search_osm_pharmacies(center_lat, center_lon, radius_m)
        log.append(f"💊 OSM薬局: {len(ph_osm)}件")

        prog.progress(99, text="💊 近隣薬局をナビィで検索中…")
        ph_mhlw, msg_ph = scraper.search_pharmacies_by_latlon(
            center_lat, center_lon, radius_m=radius_m,
            center_name=area_kw, max_pages=8,
        )
        log.append(f"💊 {msg_ph}")

        # マージ（名前重複排除）
        ph_merged: List[PharmacyFacility] = list(ph_osm)
        ph_osm_names = [p.name for p in ph_osm]
        for ph in ph_mhlw:
            if any(name_similarity(ph.name, n) >= 0.70 for n in ph_osm_names):
                continue
            ph_merged.append(ph)

        # MHLWのみの薬局をジオコーディング
        prog.progress(99, text="💊 薬局の住所を座標変換中…")
        for ph in ph_merged:
            if ph.lat is None and ph.address:
                gc = _geocoder.geocode(ph.address)
                if gc:
                    ph.lat, ph.lon = gc
                    ph.distance_m = haversine(center_lat, center_lon, ph.lat, ph.lon)
                time.sleep(0.1)

        # 半径外を除外
        ph_merged = [p for p in ph_merged
                     if p.distance_m is not None and p.distance_m <= radius_m]
        ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)

        # 医療機関視点の門前判定（各クリニックに薬局情報を付与）
        assign_monzen_to_clinics(results, ph_merged, threshold_m=float(pharmacy_gate_m))
        log.append(f"💊 薬局確定: {len(ph_merged)}件 "
                   f"（門前:{sum(1 for p in ph_merged if p.pharmacy_type=='門前薬局')}件 "
                   f"面:{sum(1 for p in ph_merged if p.pharmacy_type=='面薬局')}件）")

        # ナビィ詳細取得（処方箋数）
        ph_targets = [p for p in ph_merged if p.pref_cd and p.kikan_cd][:30]
        for i, ph in enumerate(ph_targets):
            prog.progress(99, text=f"💊 薬局詳細取得中 ({i+1}/{len(ph_targets)}): {ph.name[:18]}…")
            scraper.get_pharmacy_detail(ph)
            time.sleep(0.6)
        log.append(f"💊 薬局詳細取得: {sum(1 for p in ph_merged if p.detail_fetched)}件")
        pharmacies = ph_merged

    st.session_state.results = results
    st.session_state.pharmacies = pharmacies
    st.session_state.center_lat = center_lat
    st.session_state.center_lon = center_lon
    st.session_state.search_log = log
    st.session_state.last_address = address_input.strip()
    st.session_state.last_zones = zones
    prog.progress(100, text="✅ 完了!")
    time.sleep(0.3)
    prog.empty()


# ── 結果表示 ──────────────────────────────────────────────────────────────────
if st.session_state.results:
    results: List[MedFacility] = st.session_state.results
    pharmacies: List[PharmacyFacility] = st.session_state.get("pharmacies", [])
    center_lat = st.session_state.center_lat
    center_lon = st.session_state.center_lon
    zones_saved = st.session_state.last_zones   # 可変長リスト（3〜5要素）

    _ZONE_ICONS = ["🔵", "🟢", "🟡", "🔴", "🟣"]
    zone_labels  = [f"〜{z:,}m" for z in zones_saved]
    zone_colors  = _ZONE_ICONS[:len(zones_saved)]
    zone_summary_str = " ／ ".join(
        f"{c}〜{z:,}m" for c, z in zip(zone_colors, zones_saved)
    )
    st.success(
        f"**{st.session_state.last_address}** 周辺 {zone_summary_str} 圏内: "
        f"**{len(results)} 件** が見つかりました"
    )

    # ── ゾーン別サマリーカード ─────────────────────────────────────────
    z_cols = st.columns(len(zones_saved))
    for i, (label, color, col) in enumerate(zip(zone_labels, zone_colors, z_cols)):
        z_facs = [f for f in results
                  if get_zone_label(f.distance_m, zones_saved) == label]
        op_vals = [f.daily_outpatients for f in z_facs if f.daily_outpatients]
        col.metric(
            f"{color} {label}",
            f"{len(z_facs)}件",
            delta=f"外来合計 {sum(op_vals):,}人/日" if op_vals else "外来データなし",
            delta_color="off",
        )

    tab_table, tab_pharmacy, tab_map, tab_debug, tab_log = st.tabs(
        ["📋 医療機関一覧", f"💊 近隣薬局({len(pharmacies)}件)", "🗺️ 地図", "🔬 デバッグ", "📝 ログ"])

    # ── 一覧表 ────────────────────────────────────────────────────────────
    with tab_table:
        col_f1, col_f2, col_f3, col_f4 = st.columns(4)
        zone_options = ["すべて"] + zone_labels + [f"{zones_saved[-1]:,}m超"]
        zone_filter = col_f1.selectbox("🔵 ゾーン", zone_options)
        rx_filter   = col_f2.selectbox("処方タイプ", ["すべて", "院外処方あり", "院内処方のみ", "不明"])
        cat_filter  = col_f3.selectbox("施設種別", ["すべて", "診療所", "病院", "歯科診療所"])
        sort_col    = col_f4.selectbox("並び順",
                                        ["距離（近い順）", "施設名", "外来患者数（多い順）"])

        filtered = list(results)
        if zone_filter != "すべて":
            filtered = [f for f in filtered
                        if get_zone_label(f.distance_m, zones_saved) == zone_filter]
        if rx_filter != "すべて":
            filtered = [f for f in filtered if f.rx_summary == rx_filter]
        if cat_filter != "すべて":
            filtered = [f for f in filtered if f.facility_category == cat_filter]
        if sort_col == "施設名":
            filtered.sort(key=lambda x: x.name)
        elif sort_col == "外来患者数（多い順）":
            filtered.sort(key=lambda x: x.daily_outpatients or 0, reverse=True)
        else:
            filtered.sort(key=lambda x: x.distance_m or 9_999_999)

        # 表示用データ構築
        needs_check_count = 0
        rows = []
        for fac in filtered:
            needs_check = "要確認" in fac.distance_note
            if needs_check:
                needs_check_count += 1
            # 門前薬局表示テキスト
            if fac.monzen_pharmacy_name and fac.monzen_pharmacy_name != "なし":
                monzen_label = (f"あり：{fac.monzen_pharmacy_name}"
                                f"（{int(fac.monzen_pharmacy_dist_m)}m）")
            else:
                monzen_label = fac.monzen_pharmacy_name  # "なし" or "不明（座標なし）"
            rows.append({
                "ゾーン":        get_zone_label(fac.distance_m, zones_saved),
                "施設名":        fac.name,
                "距離(m)":       int(fac.distance_m) if fac.distance_m else None,
                "距離確度":      fac.distance_note or "—",
                "種別":          fac.facility_category,
                "診療科":        fac.specialties or "—",
                "院外処方":      fac.rx_summary,
                "院内処方(有無)":fac.inhouse_rx,
                "院外処方(有無)":fac.outpatient_rx,
                "1日外来患者数": fac.daily_outpatients,
                "外来出典":      fac.daily_outpatients_source,
                "週診療日数":    fac.weekly_op_days,
                "門前薬局":      monzen_label,
                "年間処方箋数":  fac.monzen_rx_count,
                "処方箋数出典":  fac.monzen_rx_source if fac.monzen_rx_count else "—",
                "住所":          fac.address,
            })
        df = pd.DataFrame(rows)

        if needs_check_count > 0:
            st.warning(
                f"⚠️ **距離「要確認」: {needs_check_count}件** があります。"
                "「距離確度」列に詳細を表示しています。目視で確認してください。"
            )

        st.caption(f"表示: {len(rows)}件 / 全{len(results)}件")
        st.dataframe(
            df,
            use_container_width=True,
            height=520,
            column_config={
                "ゾーン":         st.column_config.TextColumn("ゾーン", width="small"),
                "施設名":         st.column_config.TextColumn("施設名", width="medium"),
                "距離(m)":        st.column_config.NumberColumn("距離(m)", format="%d m", width="small"),
                "距離確度":       st.column_config.TextColumn("距離確度", width="medium"),
                "種別":           st.column_config.TextColumn("種別", width="small"),
                "診療科":         st.column_config.TextColumn("診療科", width="medium"),
                "院外処方":       st.column_config.TextColumn("処方タイプ", width="medium"),
                "院内処方(有無)": st.column_config.TextColumn("院内処方", width="small"),
                "院外処方(有無)": st.column_config.TextColumn("院外処方", width="small"),
                "1日外来患者数":  st.column_config.NumberColumn("1日外来", format="%d 人/日", width="small"),
                "外来出典":       st.column_config.TextColumn("外来出典", width="medium"),
                "週診療日数":     st.column_config.NumberColumn("週診療日", format="%.1f 日", width="small"),
                "門前薬局":       st.column_config.TextColumn("門前薬局（50m以内）", width="large"),
                "年間処方箋数":   st.column_config.NumberColumn("年間処方箋数", format="%d 枚", width="small"),
                "処方箋数出典":   st.column_config.TextColumn("処方箋数出典", width="medium"),
                "住所":           st.column_config.TextColumn("住所", width="large"),
            },
        )

        # サマリ
        st.divider()
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("総件数", f"{len(results)}件")
        c2.metric("院外処方あり",
                  f"{sum(1 for f in results if '院外処方あり' in f.rx_summary)}件")
        c3.metric("詳細取得済",
                  f"{sum(1 for f in results if f.detail_fetched)}件")
        avg_op = [f.daily_outpatients for f in results if f.daily_outpatients]
        c4.metric("平均外来患者数",
                  f"{sum(avg_op)/len(avg_op):.0f}人/日" if avg_op else "—")
        avg_wd = [f.weekly_op_days for f in results if f.weekly_op_days]
        c5.metric("平均週診療日数",
                  f"{sum(avg_wd)/len(avg_wd):.1f}日" if avg_wd else "—")

        # CSV ダウンロード
        st.divider()
        area_kw = extract_area_keyword(st.session_state.last_address)
        zone_tag = "_".join(str(z) for z in zones_saved) + "m"
        csv_rows = [
            {
                "ゾーン":           get_zone_label(f.distance_m, zones_saved),
                "施設名":           f.name,
                "住所":             f.address,
                "距離_m":           int(f.distance_m) if f.distance_m else "",
                "距離確度":         f.distance_note or "—",
                "施設種別":         f.facility_category,
                "診療科":           f.specialties,
                "処方タイプ":       f.rx_summary,
                "院内処方の有無":   f.inhouse_rx,
                "院外処方の有無":   f.outpatient_rx,
                "1日外来患者数":    f.daily_outpatients or "",
                "外来患者数出典":   f.daily_outpatients_source,
                "週平均診療日数":   f.weekly_op_days or "",
                "門前薬局":         f.monzen_pharmacy_name,
                "門前薬局距離_m":   int(f.monzen_pharmacy_dist_m) if f.monzen_pharmacy_dist_m else "",
                "年間処方箋数_門前": f.monzen_rx_count or "",
                "処方箋数出典":     f.monzen_rx_source if f.monzen_rx_count else "—",
                "データソース":     f.source,
                "MHLW_URL":         f.detail_url,
            }
            for f in results
        ]
        st.download_button(
            "⬇️ CSV ダウンロード",
            data=pd.DataFrame(csv_rows).to_csv(index=False, encoding="utf-8-sig"),
            file_name=f"medical_{area_kw}_{zone_tag}.csv",
            mime="text/csv",
        )

    # ── 近隣薬局タブ ──────────────────────────────────────────────────────
    with tab_pharmacy:
        if not pharmacies:
            st.info("「💊 近隣薬局も検索する」をONにして再検索すると薬局情報が表示されます。")
        else:
            # サマリーカード
            ph_monzen = [p for p in pharmacies if p.pharmacy_type == "門前薬局"]
            ph_men    = [p for p in pharmacies if p.pharmacy_type == "面薬局"]
            pc1, pc2, pc3 = st.columns(3)
            pc1.metric("薬局 合計", f"{len(pharmacies)}件")
            pc2.metric("🏥 門前薬局", f"{len(ph_monzen)}件",
                       delta=f"医療機関{int(pharmacy_gate_m)}m以内", delta_color="off")
            pc3.metric("🏪 面薬局", f"{len(ph_men)}件")

            # フィルター
            pf1, pf2 = st.columns(2)
            ph_type_filter = pf1.selectbox(
                "薬局種別", ["すべて", "門前薬局", "面薬局", "不明"])
            ph_sort = pf2.selectbox(
                "並び順", ["距離（近い順）", "処方箋数（多い順）", "施設名"])

            ph_filtered = list(pharmacies)
            if ph_type_filter != "すべて":
                ph_filtered = [p for p in ph_filtered if p.pharmacy_type == ph_type_filter]
            if ph_sort == "処方箋数（多い順）":
                ph_filtered.sort(key=lambda x: x.annual_rx_count or 0, reverse=True)
            elif ph_sort == "施設名":
                ph_filtered.sort(key=lambda x: x.name)
            else:
                ph_filtered.sort(key=lambda x: x.distance_m or 9_999_999)

            ph_rows = []
            for p in ph_filtered:
                ph_rows.append({
                    "薬局種別":       p.pharmacy_type,
                    "薬局名":         p.name,
                    "距離(m)":        int(p.distance_m) if p.distance_m else None,
                    "年間処方箋数":   p.annual_rx_count,
                    "処方箋数出典":   p.annual_rx_source,
                    "最近接医療機関": p.nearest_clinic_name,
                    "医療機関距離(m)":int(p.nearest_clinic_dist_m)
                                    if p.nearest_clinic_dist_m else None,
                    "住所":           p.address,
                })
            ph_df = pd.DataFrame(ph_rows)
            st.caption(f"表示: {len(ph_rows)}件 / 全{len(pharmacies)}件")
            st.dataframe(
                ph_df,
                use_container_width=True,
                height=460,
                column_config={
                    "薬局種別":       st.column_config.TextColumn("種別", width="small"),
                    "薬局名":         st.column_config.TextColumn("薬局名", width="medium"),
                    "距離(m)":        st.column_config.NumberColumn("距離(m)", format="%d m", width="small"),
                    "年間処方箋数":   st.column_config.NumberColumn("年間処方箋数", format="%d 枚", width="small"),
                    "処方箋数出典":   st.column_config.TextColumn("処方箋数出典", width="medium"),
                    "最近接医療機関": st.column_config.TextColumn("最近接医療機関", width="medium"),
                    "医療機関距離(m)":st.column_config.NumberColumn("医療機関距離", format="%d m", width="small"),
                    "住所":           st.column_config.TextColumn("住所", width="large"),
                },
            )

            # CSV ダウンロード
            st.divider()
            area_kw_ph = extract_area_keyword(st.session_state.last_address)
            ph_csv_rows = [
                {
                    "薬局種別":           p.pharmacy_type,
                    "薬局名":             p.name,
                    "住所":               p.address,
                    "距離_m":             int(p.distance_m) if p.distance_m else "",
                    "年間総取扱処方箋数": p.annual_rx_count or "",
                    "処方箋数出典":       p.annual_rx_source,
                    "最近接医療機関":     p.nearest_clinic_name,
                    "医療機関距離_m":     int(p.nearest_clinic_dist_m)
                                          if p.nearest_clinic_dist_m else "",
                    "MHLW_URL":           p.detail_url,
                }
                for p in pharmacies
            ]
            st.download_button(
                "⬇️ 薬局リスト CSV ダウンロード",
                data=pd.DataFrame(ph_csv_rows).to_csv(index=False, encoding="utf-8-sig"),
                file_name=f"pharmacies_{area_kw_ph}.csv",
                mime="text/csv",
            )

    # ── 地図 ──────────────────────────────────────────────────────────────
    with tab_map:
        m = folium.Map(location=[center_lat, center_lon], zoom_start=15)
        folium.Marker(
            [center_lat, center_lon],
            popup=st.session_state.last_address,
            tooltip="📍 薬局候補地",
            icon=folium.Icon(color="red", icon="home", prefix="fa"),
        ).add_to(m)
        # ゾーン数可変の同心円（外→内の順で描画）
        _circle_colors  = ["#EF4444", "#A855F7", "#EAB308", "#22C55E", "#3B82F6"]
        _circle_opacity = [0.03, 0.04, 0.05, 0.07, 0.10]
        for i, z in enumerate(reversed(zones_saved)):
            idx = len(zones_saved) - 1 - i
            folium.Circle(
                [center_lat, center_lon],
                radius=z,
                color=_circle_colors[idx],
                fill=True,
                fill_opacity=_circle_opacity[idx],
                weight=2,
                tooltip=f"ゾーン{idx+1} 〜{z:,}m",
            ).add_to(m)

        for fac in results:
            if fac.lat is None or fac.lon is None:
                continue
            zone_lbl = get_zone_label(fac.distance_m, zones_saved)
            color = (
                "green"  if fac.rx_summary == "院外処方あり"
                else "orange" if fac.rx_summary == "院内処方のみ"
                else "purple" if fac.facility_category == "病院"
                else "gray"
            )
            icon_name = "hospital-o" if fac.facility_category == "病院" else "stethoscope"
            popup_html = f"""
<div style="min-width:230px;font-family:sans-serif;font-size:13px">
  <b>{fac.name}</b><br>
  <span style="color:#64748b;font-size:11px">{fac.address or '—'}</span><br>
  <hr style="margin:4px 0">
  📏 <b>{fac.distance_m:,.0f} m</b> ／ {zone_lbl} &nbsp; 🏷 {fac.facility_category}<br>
  💊 <b>{fac.rx_summary}</b><br>
  &nbsp;&nbsp; 院内処方: {fac.inhouse_rx} / 院外処方: {fac.outpatient_rx}<br>
  👥 外来患者: <b>{fac.daily_outpatients or '—'}</b> 人/日<br>
  📅 週診療: <b>{fac.weekly_op_days or '—'}</b> 日<br>
  🔬 診療科: {fac.specialties or '—'}<br>
  {'<a href="' + fac.detail_url + '" target="_blank" style="font-size:11px">🔗 MHLWページ</a>' if fac.detail_url else ''}
</div>"""
            folium.Marker(
                [fac.lat, fac.lon],
                popup=folium.Popup(popup_html, max_width=280),
                tooltip=f"🏥 {fac.name}（{fac.distance_m:,.0f}m / {zone_lbl}）",
                icon=folium.Icon(color=color, icon=icon_name, prefix="fa"),
            ).add_to(m)

        # 薬局マーカー追加
        for ph in pharmacies:
            if ph.lat is None or ph.lon is None:
                continue
            ph_color = "blue" if ph.pharmacy_type == "門前薬局" else "lightblue"
            ph_popup = f"""
<div style="min-width:210px;font-family:sans-serif;font-size:13px">
  <b>💊 {ph.name}</b><br>
  <span style="color:#64748b;font-size:11px">{ph.address or '—'}</span><br>
  <hr style="margin:4px 0">
  📏 <b>{ph.distance_m:,.0f} m</b><br>
  🏷 <b>{ph.pharmacy_type}</b><br>
  🏥 最近接: {ph.nearest_clinic_name[:20]}（{ph.nearest_clinic_dist_m:,.0f}m）<br>
  📋 年間処方箋数: <b>{f"{ph.annual_rx_count:,}枚" if ph.annual_rx_count else "—"}</b><br>
  {'<a href="' + ph.detail_url + '" target="_blank" style="font-size:11px">🔗 MHLWページ</a>' if ph.detail_url else ''}
</div>"""
            folium.Marker(
                [ph.lat, ph.lon],
                popup=folium.Popup(ph_popup, max_width=260),
                tooltip=f"💊 {ph.name}（{ph.pharmacy_type}）",
                icon=folium.Icon(color=ph_color, icon="plus-square", prefix="fa"),
            ).add_to(m)

        st_folium(m, width="stretch", height=560)
        circle_legend = "　".join(
            f"{_ZONE_ICONS[i]} Z{i+1}円" for i in range(len(zones_saved))
        )
        st.markdown(
            f"🟢 院外処方あり　🟠 院内処方のみ　🟣 病院　⚫ 不明　🔴 薬局候補地  \n"
            f"🔵 門前薬局　🩵 面薬局  \n"
            f"{circle_legend}"
        )

    # ── デバッグ: 全フィールド ───────────────────────────────────────────
    with tab_debug:
        st.subheader("🔬 MHLW取得フィールド詳細")
        st.caption("各施設のMHLWページから取得した全フィールドを確認できます。"
                   "データが取れていない場合はここでフィールド名を確認してください。")
        sel_name = st.selectbox(
            "施設を選択",
            [f.name for f in results if f.detail_fetched],
        )
        if sel_name:
            sel_fac = next((f for f in results if f.name == sel_name), None)
            if sel_fac and sel_fac.raw_fields:
                st.info(
                    f"kikanKbn={sel_fac.kikan_kbn_used}  |  "
                    f"URL: {sel_fac.detail_url[:80]}..."
                )
                field_df = pd.DataFrame(
                    [{"フィールド名": k, "値": v}
                     for k, v in sel_fac.raw_fields.items()],
                )
                st.dataframe(field_df, use_container_width=True, height=400)
            else:
                st.warning("詳細データが取得されていません。")

    # ── ログ ──────────────────────────────────────────────────────────────
    with tab_log:
        for line in st.session_state.search_log:
            st.text(line)

elif not run_btn:
    st.info(
        "👈 サイドバーに住所と検索半径を入力して「検索実行」を押してください。\n\n"
        "### 取得データ\n"
        "| 項目 | 取得方法 |\n"
        "|------|----------|\n"
        "| 医療機関名・住所・距離 | OSM Overpass + MHLW |\n"
        "| **院内処方 / 院外処方の有無** | MHLW「院内処方の有無」「院外処方の有無」フィールド |\n"
        "| **1日外来患者数** | MHLW「医療実績、結果に関する事項」タブ |\n"
        "| **週あたり診療日数** | MHLW 診療時間テーブルから曜日カウント |\n"
        "| **診療科目** | MHLW「診療科目」フィールド + OSMタグ |\n\n"
        "**データソース**: 厚生労働省「医療機能情報提供システム（ナビイ）」+ OpenStreetMap"
    )
