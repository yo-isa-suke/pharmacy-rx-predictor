"""
商圏分析ツール — 医療機関 + 調剤薬局 統合版

medical_finder.py と pharmacy_area_finder.py を統合し、
商圏内の医療機関・薬局を一括収集・分析するツール。

データソース:
  - 厚生労働省「医療情報ネット（ナビィ）」— 医療機関・薬局リスト・詳細
  - OpenStreetMap Overpass API — 施設の座標
  - 国土地理院（GSI）/ Nominatim — ジオコーディング

v1.4 変更点 (2026-08-18):
- 【重要・不具合修正】医療機関も薬局も1件も出てこなくなっていたのを修正。
  ナビィの検索結果ページのHTML変更（施設名の見出しが h3.name → h2.name）に追随した。
  検索リクエスト自体は成功していたが、一覧のパースが h3 決め打ちだったため
  常に0件になっていた。h2/h3両対応＋「kikanCdを含むリンクを直接探す」
  フォールバックを追加し、今後の同種の変更にも壊れにくくした。
  詳細ページの「患者数」見出し（h3→h2）も同様に両対応にした。
- 【漏れ対策】静かな打ち切りを廃止。総件数から必要ページ数を計算して全件取得し、
  医療機関詳細の「先頭50件だけ」上限を撤廃。取りきれない場合は画面に警告を出す。
- 【漏れ対策】重複排除を機関コード優先の厳密判定に変更（別法人・別店舗を消さない）。
- 【漏れ対策】OSM検索に歯科（amenity=dentist）・relation・healthcare=pharmacy を追加。
- 一覧に埋め込まれたGoogleマップ座標を利用し、ジオコーディング失敗による漏れを削減。
- 詳細ページ取得を並列化（従来の逐次＋sleep から。件数上限を外しても時間が伸びない）。
"""

import math
import re
import threading
import time
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
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
    page_title="商圏分析ツール",
    page_icon="🏥",
    layout="wide",
)

# ─── 定数 ─────────────────────────────────────────────────────────────────────
MHLW_DOMAIN = "https://www.iryou.teikyouseido.mhlw.go.jp"
MHLW_BASE   = MHLW_DOMAIN + "/znk-web"
WORKING_DAYS = 305

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# v1.4: 取得の上限と並列度
PAGE_SIZE = 20                 # ナビィ一覧の1ページあたり件数
MAX_PAGES_DEFAULT = 40         # 一覧の取得ページ上限（= 800件）
FETCH_WORKERS = 8              # 詳細ページの同時取得数
DEDUP_GAP_M = 60.0             # 同名施設を「同一」とみなす最大距離

# ナビィの「中心からの距離」指定コード。"00"=1km以内, "01"=5km以内, ""=指定なし。
# ナビィ側の距離判定は施設の登録座標に基づくため、登録座標がずれている施設は
# 本来の圏内なのに絞り込みから外れる。v1.4の「推考」フェーズでは1段広いコードで
# 再検索し、こちらで実距離を測り直して拾い直す。
DIST_CODES = ["00", "01", ""]


def dist_code_for(radius_m: int) -> str:
    return "00" if radius_m <= 1_000 else ("01" if radius_m <= 5_000 else "")


def wider_dist_code(code: str) -> str:
    """1段広い距離コードを返す（すでに最大なら同じものを返す）。"""
    try:
        i = DIST_CODES.index(code)
    except ValueError:
        return ""
    return DIST_CODES[min(i + 1, len(DIST_CODES) - 1)]

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


# ─── 施設の同一判定（v1.4: 「漏れ」対策の中核） ────────────────────────────────
# 旧版は name_similarity()（文字集合の重なり率）>= 0.65 を「同じ施設」とみなして
# 後から来たほうを捨てていた。この指標は文字の順序も出現回数も見ないため、
#   「田中内科クリニック」と「中田内科クリニック」→ 1.00（別医院なのに同一扱い）
#   「さくら薬局中央店」と「さくら薬局東町店」    → 0.8前後（別店舗なのに同一扱い）
# のように、実在する別施設をリストから消してしまう。これが取りこぼしの主因だった。
# v1.4では「機関コードが違えば必ず別施設」を最優先し、コードが無い相手（OSM）とだけ
# 名前＋座標で照合する。
_NAME_NOISE_RE  = re.compile(r"[\s　・（）()\[\]「」【】,，.。／/\-－―ー~〜]")
_NAME_CORP_RE   = re.compile(
    r"(医療法人社団|医療法人財団|社会医療法人|特定医療法人|医療法人|社会福祉法人|"
    r"公益社団法人|一般社団法人|公益財団法人|一般財団法人|株式会社|有限会社|合同会社)"
)


def normalize_name(s: str) -> str:
    """全角/半角・法人格・記号を落として施設名を正規化する。"""
    s = unicodedata.normalize("NFKC", s or "")
    s = _NAME_CORP_RE.sub("", s)
    return _NAME_NOISE_RE.sub("", s).lower()


def same_facility(a, b, max_gap_m: float = 60.0) -> bool:
    """a と b が同一施設かを判定する（重複排除用）。

    判定順:
      ① 双方に機関コード（ナビィ）がある → コードの一致だけで決める。
         別コードなら、名前がどれだけ似ていても必ず「別施設」として両方残す。
      ② 正規化した名前が完全一致 → 座標が近い（または座標不明）なら同一。
      ③ 片方の名前がもう片方の先頭に含まれる（「○○薬局」vs「○○薬局本町店」）
         → 座標が {max_gap_m}m 以内のときだけ同一。
    """
    a_cd = getattr(a, "kikan_cd", "") or ""
    b_cd = getattr(b, "kikan_cd", "") or ""
    if a_cd and b_cd:
        return a_cd == b_cd

    na, nb = normalize_name(getattr(a, "name", "")), normalize_name(getattr(b, "name", ""))
    if not na or not nb:
        return False

    a_lat, a_lon = getattr(a, "lat", None), getattr(a, "lon", None)
    b_lat, b_lon = getattr(b, "lat", None), getattr(b, "lon", None)
    gap = (haversine(a_lat, a_lon, b_lat, b_lon)
           if None not in (a_lat, a_lon, b_lat, b_lon) else None)

    if na == nb:
        return gap is None or gap <= max_gap_m
    if (na.startswith(nb) or nb.startswith(na)) and gap is not None and gap <= max_gap_m:
        return True
    return False


def is_duplicate_of_any(fac, others, max_gap_m: float = 60.0) -> bool:
    return any(same_facility(fac, o, max_gap_m) for o in others)


# ─── 一覧ページの総件数パース（v1.4） ─────────────────────────────────────────
# 旧版は本文中で最初に現れた「N件」を総件数として採用していた。ナビィの新HTMLでは
# 「20件表示」のような表示件数が先に現れることがあり、その場合 total=20 と誤認して
# 2ページ目以降を取りに行かなくなる（＝21件目以降が全部漏れる）。
_TOTAL_PATTERNS = [
    re.compile(r"検索結果[^0-9]{0,12}([\d,]{1,9})\s*件"),
    re.compile(r"全\s*([\d,]{1,9})\s*件"),
    re.compile(r"([\d,]{1,9})\s*件\s*中"),
    re.compile(r"該当\s*([\d,]{1,9})\s*件"),
]


def parse_total_count(soup, n_items: int = 0) -> int:
    """一覧ページHTMLから総件数を読む。読めない場合はこのページの件数を返す。"""
    text = soup.get_text(" ", strip=True)
    for rx in _TOTAL_PATTERNS:
        m = rx.search(text)
        if m:
            try:
                return int(m.group(1).replace(",", ""))
            except ValueError:
                pass
    nums = []
    for x in re.findall(r"([\d,]{1,9})\s*件", text):
        try:
            v = int(x.replace(",", ""))
        except ValueError:
            continue
        if 0 < v < 1_000_000:
            nums.append(v)
    # 「20件表示」に引っ張られないよう、最初ではなく最大値を採用する。
    return max(nums) if nums else n_items


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
    # v1.4: relation と healthcare=pharmacy / dispensing=yes を追加。
    # 旧版は node/way の amenity=pharmacy と shop=chemist だけを見ていたため、
    # これらのタグしか付いていない薬局が丸ごと漏れていた。
    query = f"""
[out:json][timeout:60];
(
  nwr["amenity"="pharmacy"](around:{radius_m},{lat},{lon});
  nwr["shop"="chemist"](around:{radius_m},{lat},{lon});
  nwr["healthcare"="pharmacy"](around:{radius_m},{lat},{lon});
  nwr["dispensing"="yes"](around:{radius_m},{lat},{lon});
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
    # v1.4: 歯科（amenity=dentist）が旧クエリの列挙から漏れていた。歯科も処方箋の
    # 発行元なので追加する。healthcare=yes を除外していたのもやめた（除外すると
    # 「healthcare=yes しか付いていない実在の診療所」が丸ごと落ちるため）。
    # 薬局の除外は下のタグ判定で行う。relation にも対応（nwr）。
    query = f"""
[out:json][timeout:60];
(
  nwr["amenity"~"^(clinic|hospital|doctors|dentist|medical_centre)$"](around:{radius_m},{lat},{lon});
  nwr["healthcare"]["healthcare"!~"^(pharmacy|chemist|dispensary)$"](around:{radius_m},{lat},{lon});
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
        # v1.4: ナビィのHTML変更で見出しが h3 → h2 になったため両対応にする。
        h3 = item_div.find(["h2", "h3"])
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
        patient_vals = re.findall(r"(\d+\.?\d*)人", v_zennen)
        if len(patient_vals) >= 2:
            n = float(patient_vals[-2])
            if 0 < n <= 10_000:
                return int(round(n)), "ナビィ（前年度フィールド）"
        elif len(patient_vals) == 1:
            n = float(patient_vals[0])
            if 0 < n <= 10_000:
                return int(round(n)), "ナビィ（前年度フィールド）"
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
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja-JP,ja;q=0.9",
    }

    def __init__(self):
        self.session = self._new_session()
        self._ready = False
        # v1.4: 詳細ページの並列取得用。requests.Session はスレッド安全ではないので
        # スレッドごとに独立したSessionを持ち、Cookieだけ共有する。
        self._local = threading.local()
        self._cache: Dict[str, Optional[str]] = {}
        self._cache_lock = threading.Lock()
        # 直近の検索で取りこぼしが起きたかの記録（UIの「取りこぼし診断」用）
        self.last_warnings: List[str] = []

    @classmethod
    def _new_session(cls) -> requests.Session:
        sess = requests.Session()
        sess.headers.update(cls._HEADERS)
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        return sess

    def _sess(self) -> requests.Session:
        """このスレッド専用のSessionを返す（Cookieはメインと共有）。

        ナビィは検索結果をセッションに紐づけて保持するため、別Cookieのセッションから
        一覧ページ(S2400)を叩くと結果が取れない。
        """
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = self._new_session()
            sess.cookies = self.session.cookies
            self._local.session = sess
        return sess

    def _get_html(self, url: str, timeout: int = 12) -> Optional[str]:
        """詳細ページHTMLをキャッシュ付きで取得する（同じURLは1回しか取りに行かない）。"""
        with self._cache_lock:
            if url in self._cache:
                return self._cache[url]
        html: Optional[str] = None
        try:
            r = self._sess().get(url, timeout=timeout)
            if r.status_code == 200:
                html = r.text
        except Exception:
            html = None
        with self._cache_lock:
            if len(self._cache) > 4000:
                self._cache.clear()
            self._cache[url] = html
        return html

    # ── 一覧HTMLの読み取り（ナビィのHTML変更に強い形にした） ──────────────────
    @staticmethod
    def _find_name_link(item):
        """一覧itemから施設名リンクを取得する。

        v1.4: ナビィのHTML変更(2026-07頃)で施設名の見出しが <h3 class="name"> から
        <h2 class="name"> に変わった。h2/h3の両対応にしたうえで、見出しタグが
        さらに変わっても拾えるよう「kikanCd を含むリンクを直接探す」
        フォールバックを付けてある。
        """
        head = item.find(["h2", "h3", "h4"], class_="name")
        if head:
            link = head.find("a", href=True)
            if link:
                return link
        head = item.find(["h2", "h3", "h4"])
        if head:
            link = head.find("a", href=True)
            if link and "kikanCd" in (link.get("href") or ""):
                return link
        return item.select_one('a[href*="kikanCd"]')

    @staticmethod
    def _extract_maplink_coords(item):
        """一覧itemのGoogleマップリンク(data-url="...maps?q=lat,lon")から座標を抽出する。

        新HTMLでは一覧に座標が埋め込まれる。これを使うと住所のジオコーディングが
        不要になり、「住所は取れたが座標にできず商圏判定からこぼれる」漏れが減る。
        """
        for a in item.find_all("a"):
            for attr in ("data-url", "href"):
                m = re.search(r"q=(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", a.get(attr, "") or "")
                if m:
                    lat, lon = float(m.group(1)), float(m.group(2))
                    if 24.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0:
                        return lat, lon
        return None

    def _collect_pages(self, page_fn, parse_fn, max_pages: int):
        """1ページ目で総件数を確定し、必要なページ数だけ並列で取得する。

        v1.4: 旧版は max_pages を固定の小さな値（薬局8=160件 / 医療機関6=120件）で
        打ち切り、しかもそれを画面に一切出していなかった。件数の多いエリアでは
        ここで静かに切り捨てが起き、目視で見つかる「漏れ」になっていた。
        """
        results: List = []
        total = 0
        first = page_fn(0)
        if first:
            items, total = parse_fn(first)
            results.extend(items)
        if not results:
            return results, total
        need_pages = math.ceil(total / PAGE_SIZE) if total else 1
        n_pages = min(max_pages, need_pages)
        if n_pages > 1:
            with ThreadPoolExecutor(max_workers=min(6, n_pages - 1)) as ex:
                for html in ex.map(page_fn, range(1, n_pages)):
                    if html:
                        items, _ = parse_fn(html)
                        results.extend(items)
        return results, total

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
        max_pages: int = MAX_PAGES_DEFAULT,
        dist_code_override: Optional[str] = None,
    ) -> Tuple[List[PharmacyFacility], str]:
        """ナビィ薬局タブ（S2300/yakkyokuSearch）で薬局を緯度経度検索する。"""
        if not self._init():
            return [], "MHLW接続エラー"
        dist_code = (dist_code_override if dist_code_override is not None
                     else dist_code_for(radius_m))
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

        def _page(p: int):
            try:
                r = self._sess().get(
                    f"{MHLW_BASE}/juminkanja/S2400/initialize",
                    params={"id": search_id, "page": p, "size": PAGE_SIZE, "sortNo": 2},
                    timeout=15,
                )
                return r.text if r.status_code == 200 else None
            except Exception:
                return None

        all_ph, total = self._collect_pages(_page, self._parse_pharmacy_list, max_pages)
        dist_str = f"{radius_m // 1000}km" if radius_m >= 1000 else f"{radius_m}m"
        msg = f"ナビィ薬局: {dist_str}圏内 全{total}件 / 取得{len(all_ph)}件"
        if total > len(all_ph):
            self.last_warnings.append(
                f"⚠️ 薬局が全{total}件中{len(all_ph)}件しか取得できませんでした"
                f"（ページ上限{max_pages}）。取りこぼしの可能性があります。")
            msg += " ※取りこぼしあり"
        return all_ph, msg

    def _parse_pharmacy_list(self, html: str) -> Tuple[List[PharmacyFacility], int]:
        soup = BeautifulSoup(html, "html.parser")
        results: List[PharmacyFacility] = []
        for item in soup.select("div.resultItems div.item") or soup.find_all("div", class_="item"):
            link = self._find_name_link(item)
            if not link:
                continue
            name = link.get_text(strip=True)
            if not name:
                continue
            href = link.get("href", "")
            if href.startswith("/"):
                href = MHLW_DOMAIN + href
            qp = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(href).query))
            raw_text = item.get_text(separator=" ", strip=True)
            addr_m = re.search(r"〒\s*[\d-]+\s+(.+?)(?:Googleマップ|$)", raw_text)
            address = addr_m.group(1).strip() if addr_m else ""
            ph = PharmacyFacility(
                name=name, address=address, href=href,
                pref_cd=qp.get("prefCd", ""), kikan_cd=qp.get("kikanCd", ""),
                source="mhlw",
            )
            coords = self._extract_maplink_coords(item)
            if coords:
                ph.lat, ph.lon = coords
            results.append(ph)
        return results, parse_total_count(soup, len(results))

    def search_medical_by_latlon(
        self,
        lat: float, lon: float,
        radius_m: int,
        center_name: str = "",
        max_pages: int = MAX_PAGES_DEFAULT,
        dist_code_override: Optional[str] = None,
    ) -> Tuple[List[MedFacility], str]:
        """ナビィ S2320 → S2400 で医療機関（病院・診療所）を検索する。"""
        if not self._init():
            return [], "MHLW接続エラー"
        dist_code = (dist_code_override if dist_code_override is not None
                     else dist_code_for(radius_m))
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

        sep = "&" if "?" in redirect_url else "?"

        def _page(p: int):
            try:
                r = self._sess().get(
                    f"{redirect_url}{sep}page={p}&size={PAGE_SIZE}&sortNo=2", timeout=15)
                return r.text if r.status_code == 200 else None
            except Exception:
                return None

        all_facs, total = self._collect_pages(_page, self._parse_med_list, max_pages)
        dist_str = f"{radius_m // 1000}km" if radius_m >= 1000 else f"{radius_m}m"
        msg = f"MHLW医療機関: {dist_str}圏内 全{total}件/取得{len(all_facs)}件"
        if total > len(all_facs):
            self.last_warnings.append(
                f"⚠️ 医療機関が全{total}件中{len(all_facs)}件しか取得できませんでした"
                f"（ページ上限{max_pages}）。取りこぼしの可能性があります。")
            msg += " ※取りこぼしあり"
        return all_facs, msg

    def _parse_med_list(self, html: str) -> Tuple[List[MedFacility], int]:
        """S2400 医療機関一覧HTMLからMedFacilityリストを生成する（hrefからpref_cd/kikan_cd/kikan_kbn抽出）。"""
        soup = BeautifulSoup(html, "html.parser")
        results: List[MedFacility] = []
        for item in soup.find_all("div", class_="item"):
            link = self._find_name_link(item)
            if not link:
                continue
            name = link.get_text(strip=True)
            if not name:
                continue
            href = link.get("href", "")
            if href.startswith("/"):
                href = MHLW_DOMAIN + href
            qp = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(href).query))
            try:
                kikan_kbn = int(qp.get("kikanKbn", "2"))
            except ValueError:
                kikan_kbn = 2
            fac = MedFacility(
                name=name, source="mhlw",
                pref_cd=qp.get("prefCd", ""), kikan_cd=qp.get("kikanCd", ""),
                kikan_kbn=kikan_kbn,
            )
            coords = self._extract_maplink_coords(item)
            if coords:
                fac.lat, fac.lon = coords
            results.append(fac)
        return results, max(parse_total_count(soup, len(results)), len(results))

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
    ph_osm = search_osm_pharmacies(center_lat, center_lon, radius_m)
    ph_merged: List[PharmacyFacility] = list(ph_osm)
    log.append(f"[Step2] OSM薬局: {len(ph_osm)}件取得 ({time.time()-t0:.1f}s)")

    # Step 3: OSM 医療機関検索
    prog.progress(14, text="Step3: OSMから医療機関を取得中…")
    t0 = time.time()
    med_radius = radius_m + gate_m
    med_osm = search_osm_medical(center_lat, center_lon, med_radius)
    log.append(f"[Step3] OSM医療機関: {med_radius}m圏内 {len(med_osm)}件 ({time.time()-t0:.1f}s)")
    if len(med_osm) == 0:
        log.append("[Step3] ⚠️ OSM医療機関0件 → ナビィデータのみで処理します")

    # Step 4: ナビィ薬局リスト取得 → 住所geocoding → OSMとマージ
    prog.progress(20, text="Step4: ナビィから薬局リストを取得中…")
    t0 = time.time()
    scraper.last_warnings = []
    navvi_phs, navvi_ph_msg = scraper.search_pharmacies_by_latlon(
        center_lat, center_lon, radius_m=radius_m,
        center_name=address[:20],
    )
    log.append(f"[Step4] {navvi_ph_msg}")
    if not navvi_phs:
        log.append("[Step4] ⚠️ ナビィ薬局が0件。ナビィ側の仕様変更・通信エラーの可能性があります。")

    # v1.4: 重複判定を機関コード優先の厳密判定に変更（別法人・別店舗を消さない）
    added_navvi_ph = 0
    seen_ph_cd: set = set()
    need_gc: List[PharmacyFacility] = []
    for nph in navvi_phs:
        if nph.kikan_cd and nph.kikan_cd in seen_ph_cd:
            continue                                   # ページ間の重複のみ除去
        dup = next((p for p in ph_merged if same_facility(nph, p, DEDUP_GAP_M)), None)
        if dup is not None:
            if not dup.pref_cd:                        # OSM側に機関コードを補完
                dup.pref_cd, dup.kikan_cd, dup.href = nph.pref_cd, nph.kikan_cd, nph.href
            continue
        if nph.kikan_cd:
            seen_ph_cd.add(nph.kikan_cd)
        ph_merged.append(nph)
        added_navvi_ph += 1
        if nph.lat is None and nph.address:
            need_gc.append(nph)

    # 一覧に座標が埋め込まれていなかったぶんだけジオコーディングする
    for i, p in enumerate(need_gc):
        if i % 5 == 0:
            prog.progress(20, text=f"Step4: ナビィ薬局 座標取得中 {i+1}/{len(need_gc)}件…")
        gc = geocoder.geocode(p.address)
        if gc:
            p.lat, p.lon = gc
        time.sleep(0.15)

    # 商圏外（半径の1.1倍超）だけを落とす。座標不明の薬局は落とさず残す。
    # v1.4: 旧版は座標が取れないと商圏外扱いで消える経路があり、これも漏れの一因だった。
    kept: List[PharmacyFacility] = []
    dropped_far = 0
    for p in ph_merged:
        if p.lat is not None:
            p.distance_m = haversine(center_lat, center_lon, p.lat, p.lon)
            if p.distance_m > radius_m * 1.1:
                dropped_far += 1
                continue
        kept.append(p)
    ph_merged = kept
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    no_coord_ph = sum(1 for p in ph_merged if p.lat is None)
    log.append(
        f"[Step4] ナビィ固有追加: {added_navvi_ph}件 合計: {len(ph_merged)}件 "
        f"（商圏外除外: {dropped_far}件 / 座標なし: {no_coord_ph}件） ({time.time()-t0:.1f}s)"
    )
    if no_coord_ph:
        log.append(
            f"[Step4] ⚠️ 座標が特定できない薬局が{no_coord_ph}件あります"
            "（門前/面の判定は「不明」になります）。"
        )

    # Step 5: ナビィ医療機関リスト取得 → get_facility_detail で住所+詳細取得 → geocoding → OSMとマージ
    prog.progress(30, text="Step5: ナビィから医療機関リストを取得中…")
    t0 = time.time()
    navvi_meds, med_msg = scraper.search_medical_by_latlon(
        center_lat, center_lon, radius_m=med_radius,
        center_name=address[:20],
    )
    log.append(f"[Step5] {med_msg}")
    if not navvi_meds:
        log.append("[Step5] ⚠️ ナビィ医療機関が0件。ナビィ側の仕様変更・通信エラーの可能性があります。")

    # 薬局の混入除外。v1.4: 名前だけで判定すると「くすりの木内科クリニック」のような
    # 実在の医院まで落ちるため、機関区分(kikanKbn=5)を主、名前を従にした。
    # 名前判定は「医療機関らしい語」を含まない場合にのみ効かせる。
    _PHARMA_NAME_RE = re.compile(
        r'薬局|ドラッグ|ファーマシー|調剤|drug\s*store|pharmacy', re.IGNORECASE
    )
    _MED_NAME_RE = re.compile(
        r'医院|クリニック|診療所|病院|歯科|内科|外科|眼科|皮膚科|小児科|産婦人科|'
        r'耳鼻|泌尿器|整形|心療|精神|リハビリ|クリニツク|医療センター|保健'
    )

    def _is_pharmacy_row(f) -> bool:
        if f.kikan_kbn == 5:
            return True
        return bool(_PHARMA_NAME_RE.search(f.name)) and not _MED_NAME_RE.search(f.name)

    # v1.4: 旧版の「先頭50件だけ詳細を取る」上限を撤廃した。件数の多いエリアでは
    # 51件目以降が黙って捨てられ、目視で見つかる「漏れ」の主因になっていた。
    seen_cd: set = set()
    med_targets: List[MedFacility] = []
    for f in navvi_meds:
        if not (f.pref_cd and f.kikan_cd):
            continue
        if _is_pharmacy_row(f):
            continue
        if f.kikan_cd in seen_cd:                  # ページ間の重複のみ除去
            continue
        if is_duplicate_of_any(f, med_osm, DEDUP_GAP_M):
            continue
        seen_cd.add(f.kikan_cd)
        med_targets.append(f)
    log.append(f"[Step5] ナビィ医療機関の詳細取得対象: {len(med_targets)}件（上限なし）")

    geocode_ok, geocode_fail, detail_fail = 0, 0, 0
    stats_lock = threading.Lock()
    n_med = len(med_targets)
    done = [0]

    def _fetch_med(nmf):
        nonlocal geocode_ok, geocode_fail, detail_fail
        ok = scraper.get_facility_detail(nmf)
        if nmf.lat is None and nmf.address:
            gc = geocoder.geocode(nmf.address)     # 詳細に座標が無いときだけ住所から
            if gc:
                nmf.lat, nmf.lon = gc
        with stats_lock:
            if not ok:
                detail_fail += 1
            if nmf.lat is not None:
                geocode_ok += 1
            else:
                geocode_fail += 1
            done[0] += 1
            if done[0] % 3 == 0 or done[0] == n_med:
                prog.progress(
                    30 + int(20 * done[0] / max(n_med, 1)),
                    text=f"Step5: 医療機関の詳細を並列取得中 {done[0]}/{n_med}件…",
                )

    if med_targets:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            list(ex.map(_fetch_med, med_targets))

    for nmf in med_targets:
        if nmf.lat is not None:
            nmf.distance_m = haversine(center_lat, center_lon, nmf.lat, nmf.lon)
        med_osm.append(nmf)

    med_osm.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(
        f"[Step5] 医療機関詳細+住所取得（{FETCH_WORKERS}並列）: 成功={geocode_ok}件 "
        f"詳細失敗={detail_fail}件 座標なし={geocode_fail}件 "
        f"合計={len(med_osm)}件 ({time.time()-t0:.1f}s)"
    )
    if geocode_fail:
        log.append(
            f"[Step5] ⚠️ 座標を確定できなかった医療機関が{geocode_fail}件あります。"
            "この施設は門前判定に使われないため、近くの薬局が「面」と判定される場合があります。"
        )

    # ─────────────────────────────────────────────────────────────────────
    # Phase 2: 推考フェーズ（必須・スキップ不可）
    # ─────────────────────────────────────────────────────────────────────

    # Step 6: 【医療機関 漏れ確認】ナビィ再検索
    prog.progress(52, text="Step6（推考①）: 医療機関 漏れ確認中…")
    t0 = time.time()
    # v1.4: 旧版はまったく同じ条件で再検索していたため、原理的に新しい施設は
    # 1件も出てこなかった（時間だけを消費していた）。ここでは1段広い距離コードで
    # 検索し、こちら側で実距離を測り直して圏内のものだけ拾い直す。
    # ナビィの距離絞り込みは施設の登録座標に依存するため、登録座標がずれている
    # 施設はこの「広めに取って測り直す」でしか拾えない。
    existing_med_kikan_cds = {f.kikan_cd for f in med_osm if f.kikan_cd}
    wide_code = wider_dist_code(dist_code_for(med_radius))
    verify_meds, verify_msg = scraper.search_medical_by_latlon(
        center_lat, center_lon, radius_m=med_radius,
        center_name=address[:20], dist_code_override=wide_code,
    )
    log.append(f"[Step6] 広域再検索（距離コード'{wide_code}'）: {verify_msg}")

    add_meds: List[MedFacility] = []
    for vf in verify_meds:
        if not (vf.pref_cd and vf.kikan_cd):
            continue
        if vf.kikan_cd in existing_med_kikan_cds:
            continue
        if _is_pharmacy_row(vf):
            continue
        if is_duplicate_of_any(vf, med_osm, DEDUP_GAP_M):
            continue
        existing_med_kikan_cds.add(vf.kikan_cd)
        add_meds.append(vf)

    def _fetch_verify_med(vf):
        scraper.get_facility_detail(vf)
        if vf.lat is None and vf.address:
            gc = geocoder.geocode(vf.address)
            if gc:
                vf.lat, vf.lon = gc

    if add_meds:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            list(ex.map(_fetch_verify_med, add_meds))

    added_med, out_of_range = 0, 0
    for vf in add_meds:
        if vf.lat is not None:
            vf.distance_m = haversine(center_lat, center_lon, vf.lat, vf.lon)
            if vf.distance_m > med_radius:      # 広めに取ったぶんを実距離で切る
                out_of_range += 1
                continue
        vf.source = "mhlw(推考①追加)"
        med_osm.append(vf)
        added_med += 1
    log.append(
        f"[Step6] 推考①: 医療機関 {added_med}件を追加で発見 "
        f"（広域再検索{len(verify_meds)}件を確認 / 実距離で圏外だった{out_of_range}件は除外） "
        f"({time.time()-t0:.1f}s)"
    )
    if added_med:
        log.append(
            f"[Step6] ⚠️ 通常検索で取りきれていなかった医療機関が{added_med}件ありました"
            "（広域再検索で回収済み。source列が「推考①追加」の行です）。"
        )

    # Step 7: 【薬局 漏れ確認】ナビィ再検索
    prog.progress(62, text="Step7（推考②）: 薬局 漏れ確認中…")
    t0 = time.time()
    # v1.4: 医療機関と同じく、1段広い距離コードで検索して実距離で絞り直す。
    existing_ph_kikan_cds = {p.kikan_cd for p in ph_merged if p.kikan_cd}
    wide_ph_code = wider_dist_code(dist_code_for(radius_m))
    verify_phs, verify_ph_msg = scraper.search_pharmacies_by_latlon(
        center_lat, center_lon, radius_m=radius_m,
        center_name=address[:20], dist_code_override=wide_ph_code,
    )
    log.append(f"[Step7] 広域再検索（距離コード'{wide_ph_code}'）: {verify_ph_msg}")

    add_phs: List[PharmacyFacility] = []
    for vph in verify_phs:
        if vph.kikan_cd and vph.kikan_cd in existing_ph_kikan_cds:
            continue
        if is_duplicate_of_any(vph, ph_merged, DEDUP_GAP_M):
            continue
        if vph.kikan_cd:
            existing_ph_kikan_cds.add(vph.kikan_cd)
        add_phs.append(vph)

    for vph in add_phs:
        if vph.lat is None and vph.address:
            gc = geocoder.geocode(vph.address)
            if gc:
                vph.lat, vph.lon = gc
            time.sleep(0.15)

    added_ph, ph_out_of_range = 0, 0
    for vph in add_phs:
        if vph.lat is not None:
            vph.distance_m = haversine(center_lat, center_lon, vph.lat, vph.lon)
            if vph.distance_m > radius_m * 1.1:     # 広めに取ったぶんを実距離で切る
                ph_out_of_range += 1
                continue
        vph.source = "mhlw(推考②追加)"
        ph_merged.append(vph)
        added_ph += 1
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(
        f"[Step7] 推考②: 薬局 {added_ph}件を追加で発見 "
        f"（広域再検索{len(verify_phs)}件を確認 / 実距離で圏外だった{ph_out_of_range}件は除外） "
        f"({time.time()-t0:.1f}s)"
    )
    if added_ph:
        log.append(
            f"[Step7] ⚠️ 通常検索で取りきれていなかった薬局が{added_ph}件ありました"
            "（広域再検索で回収済み。source列が「推考②追加」の行です）。"
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
    all_ph_targets = [p for p in ph_merged if p.pref_cd and p.kikan_cd and not p.detail_fetched]
    ph_targets = all_ph_targets[:max_detail]
    log.append(f"[Step9] 薬局詳細取得対象: {len(ph_targets)}件 / 対象候補{len(all_ph_targets)}件")
    if len(all_ph_targets) > len(ph_targets):
        log.append(
            f"[Step9] ⚠️ 「詳細取得件数」の設定により"
            f"{len(all_ph_targets) - len(ph_targets)}件の薬局の処方箋数を取得していません"
            "（薬局そのものは一覧に残ります）。サイドバーで上限を上げてください。"
        )
    n_t = len(ph_targets)
    done_ph = [0]

    def _run_ph(ph):
        scraper.get_pharmacy_detail(ph)
        done_ph[0] += 1
        if done_ph[0] % 3 == 0 or done_ph[0] == n_t:
            prog.progress(
                74 + int(18 * done_ph[0] / max(n_t, 1)),
                text=f"Step9: 薬局詳細を並列取得中 {done_ph[0]}/{n_t}件…",
            )

    if ph_targets:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            list(ex.map(_run_ph, ph_targets))
    # 詳細ページで座標が判明したぶんの距離を測り直す
    for p in ph_merged:
        if p.lat is not None:
            p.distance_m = haversine(center_lat, center_lon, p.lat, p.lon)
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    fetched_ph = sum(1 for p in ph_merged if p.detail_fetched)
    log.append(f"[Step9] 薬局詳細取得完了（{FETCH_WORKERS}並列）: {fetched_ph}件 ({time.time()-t0:.1f}s)")

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

    # Step 11: 結果をsession_stateに保存（呼び出し元で保存）
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
st.title("🏥 商圏分析ツール")
st.caption(
    "住所と商圏半径を入力すると、圏内の医療機関・薬局を一覧表示します。  \n"
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
        "詳細取得件数（薬局）", min_value=5, max_value=300, value=150, step=5,
        help="ナビィから年間処方箋数を取得する薬局の上限件数（時間に影響します）",
    )
    run_btn = st.button("検索実行", type="primary", use_container_width=True)
    st.divider()
    st.caption(
        "データソース: 厚労省ナビィ / OpenStreetMap / 国土地理院\n\n"
        "※ 初回検索は2〜5分かかります"
    )

# ── 検索実行 ──────────────────────────────────────────────────────────────────
if run_btn and address_input.strip():
    log: List[str] = []
    prog = st.progress(0, text="検索を開始しています…")
    try:
        med_list, ph_list, clat, clon = run_analysis(
            address_input.strip(), radius_m, gate_m, max_detail, log, prog
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

    tab_med, tab_ph, tab_map, tab_log = st.tabs(
        ["🏥 医療機関", "💊 薬局", "🗺️ 統合地図", "📝 ログ"]
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
        # v1.4: 「漏れているかどうか」を目視で確認する前に、機械側で言い切るための欄。
        # 打ち切り・座標未確定・広域再検索での回収など、リストが実態より少なくなる
        # 要因と、その回収結果をここに集約する。
        st.subheader("🩺 取りこぼし診断")
        alerts = [l for l in st.session_state.search_log if "⚠️" in l]
        if alerts:
            for a in alerts:
                st.warning(a)
        else:
            st.success(
                "取りこぼしの兆候は検出されませんでした"
                "（ナビィの全件数ぶんを取得し、座標も全件確定しています）。"
            )
        st.caption(
            "※ ここで警告が出ていない場合でも、ナビィに未登録の施設・開設直後の施設は"
            "原理的に取得できません。気になる場合はOSM由来の行（source=osm）や、"
            "地図タブの表示もあわせてご確認ください。"
        )
        st.divider()
        st.subheader("処理ログ")
        for line in st.session_state.search_log:
            st.text(line)

else:
    st.info("← 左のサイドバーで住所と条件を設定し、「検索実行」を押してください。")
