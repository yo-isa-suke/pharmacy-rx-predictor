"""
商圏内 調剤薬局リストアップツール

住所 + 商圏半径を入力 → 圏内の調剤薬局を一覧表示。
各薬局について以下を表示:
  ① 薬局名
  ② 門前/面 の判定（閾値以内に医療機関があるか）
  ③ 門前医療機関名（あれば）
  ④ 年間総取扱処方箋数（ナビィ）
  ⑤ 商圏内の 門前薬局数 / 面薬局数 サマリー

データソース:
  - 厚生労働省「医療情報ネット（ナビィ）」 — 薬局リスト・処方箋数
  - OpenStreetMap Overpass API — 薬局・医療機関の座標
  - 国土地理院（GSI）/ Nominatim — ジオコーディング

v1.5 変更点 (2026-09-03):
- 【不具合修正】OSMの薬局・医療機関が常に0件になっていたのを修正。原因は2つ:
  ① python-requests のデフォルトUAが本家 overpass-api.de に 406 で拒否される
    → ツールを識別できる User-Agent を付与。
  ② ミラーの overpass.osm.ch はスイス限定データのため、日本の検索が
    「通信成功・結果0件」になり、それが正解として採用されていた
    → osm.ch と停止中の private.coffee をミラーリストから削除。
- 【不具合修正】ナビィ詳細ページ（S2430）の取得にリトライを追加。
  混雑時間帯はタイムアウトが多発し、しかも一度失敗すると失敗が
  キャッシュされて再試行されず、処方箋数が全滅することがあった。
  失敗はキャッシュせず、タイムアウトを延ばしながら最大3回試すようにした。
- 一覧ページ（S2400）の取得にも同様の軽いリトライを追加。
- 【不具合修正】エラー発生時に「検索中にエラーが発生しました: 」と空欄で表示され
  原因が分からなかったのを修正。例外の種類・トレースバック・実行ログを画面に出す。
  （st.stop() 由来の StopException まで誤って捕まえていたのも修正）
- タイトルにバージョン番号を表示（クラウド版とローカル版の取り違え防止）。

v1.5.1 変更点 (2026-09-03):
- 【不具合修正・Streamlit Cloud対応】並列取得のワーカースレッド内から
  進捗バー（st.progress）を更新していたため、新しめの Streamlit
  （Streamlit Cloud 含む）で NoSessionContext 例外になり検索が落ちていた。
  進捗更新をメインスレッド側（as_completed ループ）に移して解消。
  ローカルの古い Streamlit では警告止まりだったため気づけていなかった。

v1.4 変更点 (2026-08-18):
- 【重要・不具合修正】薬局も医療機関も1件も出てこなくなっていたのを修正。
  ナビィの検索結果ページのHTML変更（施設名の見出しが h3.name → h2.name）に追随した。
  検索リクエスト自体は成功していたが、一覧のパースが h3 決め打ちだったため
  常に0件になっていた（＝ツールが「動かない」状態の原因）。
  h2/h3の両対応にしたうえで、見出しタグがさらに変わっても拾えるよう
  「kikanCd を含むリンクを直接探す」フォールバックを追加した。
- 【漏れ対策】静かな打ち切りを廃止した。
  ・一覧の総件数を正しく読み、必要なページ数を計算して全ページ取得する
    （旧: 薬局8ページ=160件 / 医療機関5ページ=100件で無警告に打ち切り）。
  ・医療機関の詳細取得にあった「先頭50件だけ」の上限を撤廃した。
  ・それでも取りきれない場合は画面に警告を出す（黙って減らさない）。
- 【漏れ対策】重複排除を厳密化。旧版の「文字集合の重なり65%以上なら同じ施設」は
  別法人・別店舗まで同一視して消していたため、機関コード優先の判定に変更した。
- 【漏れ対策】OSM検索に歯科（amenity=dentist）・relation・healthcare=pharmacy 等を追加。
- 一覧に埋め込まれたGoogleマップ座標を直接利用し、ジオコーディング失敗による
  取りこぼしを削減（あわせて高速化）。
"""

import math
import re
import threading
import time
import traceback
import unicodedata
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    page_title="商圏内 調剤薬局リストアップ",
    page_icon="💊",
    layout="wide",
)

# ─── 定数 ─────────────────────────────────────────────────────────────────────
MHLW_DOMAIN = "https://www.iryou.teikyouseido.mhlw.go.jp"
MHLW_BASE   = MHLW_DOMAIN + "/znk-web"

# v1.5: osm.ch はスイス限定データ（日本の検索が「成功したのに0件」になる）、
# private.coffee は停止しているため削除。
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
# v1.5: python-requests のデフォルトUAは overpass-api.de に 406 で拒否されるため、
# ツールを識別できる User-Agent を必ず付ける（Overpass利用ポリシー準拠）。
OVERPASS_HEADERS = {"User-Agent": "PharmacyAreaFinder/1.5 (retail pharmacy analysis tool)"}

# v1.4: 取得の上限と並列度
PAGE_SIZE = 20                 # ナビィ一覧の1ページあたり件数
MAX_PAGES_DEFAULT = 40         # 一覧の取得ページ上限（= 800件。実運用ではまず届かない）
FETCH_WORKERS = 8              # 詳細ページの同時取得数
DEDUP_GAP_M = 60.0             # 同名施設を「同一」とみなす最大距離


# ─── データクラス ──────────────────────────────────────────────────────────────
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
    pharmacy_type: str = "不明"           # "門前薬局" / "面薬局" / "不明"
    nearest_clinic_name: str = "—"
    nearest_clinic_dist_m: Optional[float] = None
    annual_rx_count: Optional[int] = None
    annual_rx_source: str = "—"
    detail_fetched: bool = False
    detail_url: str = ""
    raw_fields: Dict[str, str] = field(default_factory=dict)


@dataclass
class MedFacility:
    name: str
    address: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    distance_m: Optional[float] = None
    source: str = "osm"
    pref_cd: str = ""
    kikan_cd: str = ""
    kikan_kbn: int = 2


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


# ─── Overpass ─────────────────────────────────────────────────────────────────
def _overpass_post(query: str, timeout: int = 40, retries: int = 2) -> Optional[dict]:
    for attempt in range(retries + 1):
        for url in OVERPASS_MIRRORS:
            try:
                r = requests.post(url, data={"data": query},
                                  headers=OVERPASS_HEADERS, timeout=timeout)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 503):
                    time.sleep(5 + attempt * 5)
                    break
                # 406/403 = UAブロック等。このミラーは諦めて次へ
            except requests.exceptions.Timeout:
                continue
            except Exception:
                continue
        if attempt < retries:
            time.sleep(3 + attempt * 3)
    return None


# ─── ジオコーダー ──────────────────────────────────────────────────────────────
class GeocoderService:
    GSI_URL       = "https://msearch.gsi.go.jp/address-search/AddressSearch"
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    HEADERS       = {"User-Agent": "PharmacyAreaFinder/1.0"}
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

    def geocode_by_name(
        self, name: str, near_lat: float, near_lon: float, radius_km: float = 25
    ) -> Optional[Tuple[float, float]]:
        """施設名でNominatimをバウンディングボックス付き検索（住所不明時のフォールバック用）。
        GSIは住所専用のため施設名検索には使わない。"""
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


_geocoder = GeocoderService()


# ─── OSM 薬局検索 ──────────────────────────────────────────────────────────────
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
    # v1.4: relation（建物としてマッピングされた大型店）と healthcare=pharmacy /
    # dispensing=yes を追加。旧版は node/way の amenity=pharmacy と shop=chemist だけで、
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


# ─── OSM 医療機関検索（門前判定用） ───────────────────────────────────────────
def search_osm_medical(lat: float, lon: float, radius_m: int) -> List[MedFacility]:
    # v1.4: 歯科（amenity=dentist）が旧クエリの列挙から漏れていた。歯科は処方箋の
    # 発行元として無視できないため追加。あわせて relation にも対応した（nwr）。
    query = f"""
[out:json][timeout:60];
(
  nwr["amenity"~"^(clinic|hospital|doctors|dentist|medical_centre)$"](around:{radius_m},{lat},{lon});
  nwr["healthcare"](around:{radius_m},{lat},{lon});
);
out center;
"""
    data = _overpass_post(query)
    if not data:
        return []
    facilities: List[MedFacility] = []
    seen_ids = set()
    for el in data.get("elements", []):
        key = (el.get("type"), el.get("id"))
        if key in seen_ids:
            continue
        seen_ids.add(key)
        tags = el.get("tags", {})
        name = tags.get("name:ja") or tags.get("name", "")
        if not name:
            continue
        # 薬局・ドラッグストアは医療機関ではないので除外（タグで判定）
        if (tags.get("amenity") == "pharmacy" or tags.get("shop") == "chemist"
                or tags.get("healthcare") in ("pharmacy", "chemist", "dispensary")):
            continue
        if el["type"] == "node":
            f_lat, f_lon = el.get("lat"), el.get("lon")
        else:
            c = el.get("center", {})
            f_lat, f_lon = c.get("lat"), c.get("lon")
        if f_lat is None or f_lon is None:
            continue
        dist = haversine(lat, lon, f_lat, f_lon)
        facilities.append(MedFacility(name=name, lat=f_lat, lon=f_lon, distance_m=dist, source="osm"))
    facilities.sort(key=lambda x: x.distance_m or 9_999_999)
    return facilities


# ─── MHLWスクレイパー ──────────────────────────────────────────────────────────
class MHLWScraper:
    """厚労省「医療情報ネット（ナビィ）」スクレイパー。

    v1.4: 2026年7月頃のナビィHTML変更（施設名タグ h3.name → h2.name）に追随。
    旧実装は h3 決め打ちだったため、検索は成功しているのに一覧のパースで
    0件になり、ツール全体が「動かない」状態になっていた。
    """

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
        # 詳細ページの並列取得用。requests.Session はスレッド安全ではないので
        # スレッドごとに独立したSessionを持ち、Cookieだけ共有する。
        self._local = threading.local()
        self._cache: Dict[str, Optional[str]] = {}
        self._cache_lock = threading.Lock()
        # 直近の検索で取りこぼしが起きたかどうかの記録（UIの「取りこぼし診断」用）
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
            sess.cookies = self.session.cookies      # セッション同一性を維持
            self._local.session = sess
        return sess

    def _get_html(self, url: str, timeout: int = 12) -> Optional[str]:
        """詳細ページHTMLをキャッシュ付きで取得する（同じURLは1回しか取りに行かない）。

        v1.5: ナビィは混雑時間帯にタイムアウトが多発する。旧版は1回失敗すると
        None をキャッシュして二度と再試行しなかったため、処方箋数が全滅する
        ことがあった。タイムアウトを延ばしながら最大3回試し、失敗は
        キャッシュしない（成功したHTMLだけキャッシュする）。
        """
        with self._cache_lock:
            if url in self._cache:
                return self._cache[url]
        html: Optional[str] = None
        for attempt in range(3):
            try:
                r = self._sess().get(url, timeout=timeout + attempt * 10)
                if r.status_code == 200:
                    html = r.text
                    break
                if r.status_code in (429, 503):
                    time.sleep(2 + attempt * 2)
                    continue
                break                     # 404等は再試行しても無駄
            except requests.exceptions.Timeout:
                continue
            except Exception:
                break
        if html is not None:
            with self._cache_lock:
                if len(self._cache) > 4000:
                    self._cache.clear()
                self._cache[url] = html
        return html

    def _init(self) -> bool:
        if self._ready:
            return True
        try:
            r = self.session.get(f"{MHLW_BASE}/juminkanja/S2320/initialize", timeout=15)
            self._ready = r.status_code == 200
        except Exception:
            self._ready = False
        return self._ready

    # ── 一覧HTMLの読み取り（ナビィのHTML変更に強い形にした） ──────────────────
    @staticmethod
    def _find_name_link(item):
        """一覧itemから施設名リンクを取得する。

        v1.4: ナビィのHTML変更(2026-07頃)で施設名の見出しが <h3 class="name"> から
        <h2 class="name"> に変わった。h2/h3の両対応にしたうえで、見出しタグが
        さらに変わっても壊れないよう「kikanCd を含むリンク」を直接探す
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

        新HTMLでは一覧に座標が埋め込まれるようになった。これを使うと住所の
        ジオコーディングが不要になり、「住所は取れたが座標にできず商圏判定から
        こぼれ落ちる」タイプの漏れがそのぶん減る。
        """
        for a in item.find_all("a"):
            for attr in ("data-url", "href"):
                m = re.search(r"q=(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", a.get(attr, "") or "")
                if m:
                    lat, lon = float(m.group(1)), float(m.group(2))
                    if 24.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0:
                        return lat, lon
        return None

    # ── 薬局検索 ──────────────────────────────────────────────────────────────
    def search_pharmacies_by_latlon(
        self,
        lat: float, lon: float,
        radius_m: int,
        center_name: str = "",
        max_pages: int = MAX_PAGES_DEFAULT,
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

        def _page(p: int) -> Optional[str]:
            for attempt in range(3):        # v1.5: タイムアウト時はリトライ
                try:
                    r = self._sess().get(
                        f"{MHLW_BASE}/juminkanja/S2400/initialize",
                        params={"id": search_id, "page": p, "size": PAGE_SIZE, "sortNo": 2},
                        timeout=15 + attempt * 10,
                    )
                    if r.status_code == 200:
                        return r.text
                except requests.exceptions.Timeout:
                    continue
                except Exception:
                    return None
            return None

        all_ph, total = self._collect_pages(_page, self._parse_pharmacy_list, max_pages)
        dist_str = f"{radius_m // 1000}km" if radius_m >= 1000 else f"{radius_m}m"
        msg = f"ナビィ薬局: {dist_str}圏内 全{total}件 / 取得{len(all_ph)}件"
        if total > len(all_ph):
            w = (f"⚠️ 薬局が全{total}件中{len(all_ph)}件しか取得できませんでした"
                 f"（ページ上限{max_pages}）。取りこぼしの可能性があります。")
            self.last_warnings.append(w)
            msg += " ※取りこぼしあり"
        return all_ph, msg

    def _collect_pages(self, page_fn, parse_fn, max_pages: int):
        """1ページ目で総件数を確定し、必要なページ数だけ並列で取得する。

        v1.4: 旧版は max_pages を固定の小さな値（薬局8＝160件, 医療機関5＝100件）で
        打ち切り、しかもそれを画面に一切出していなかった。件数の多いエリアでは
        ここで静かに切り捨てが起きる＝目視で見つかる「漏れ」になっていた。
        総件数から必要ページ数を計算し、足りなければ警告を出す。
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

    # ── 医療機関検索 ──────────────────────────────────────────────────────────
    def search_medical_by_latlon(
        self,
        lat: float, lon: float,
        radius_m: int,
        center_name: str = "",
        max_pages: int = MAX_PAGES_DEFAULT,
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

        sep = "&" if "?" in redirect_url else "?"

        def _page(p: int) -> Optional[str]:
            for attempt in range(3):        # v1.5: タイムアウト時はリトライ
                try:
                    r = self._sess().get(
                        f"{redirect_url}{sep}page={p}&size={PAGE_SIZE}&sortNo=2",
                        timeout=15 + attempt * 10)
                    if r.status_code == 200:
                        return r.text
                except requests.exceptions.Timeout:
                    continue
                except Exception:
                    return None
            return None

        all_facs, total = self._collect_pages(_page, self._parse_med_list, max_pages)
        dist_str = f"{radius_m // 1000}km" if radius_m >= 1000 else f"{radius_m}m"
        msg = f"MHLW医療機関: {dist_str}圏内 全{total}件/取得{len(all_facs)}件"
        if total > len(all_facs):
            w = (f"⚠️ 医療機関が全{total}件中{len(all_facs)}件しか取得できませんでした"
                 f"（ページ上限{max_pages}）。取りこぼしの可能性があります。")
            self.last_warnings.append(w)
            msg += " ※取りこぼしあり"
        return all_facs, msg

    def _parse_med_list(self, html: str) -> Tuple[List[MedFacility], int]:
        """S2400 医療機関一覧HTMLからMedFacilityリストを生成する。"""
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

    # ── 詳細ページ ────────────────────────────────────────────────────────────
    @staticmethod
    def _extract_coords_from_html(html: str):
        """詳細ページに埋め込まれたGoogleマップ座標を拾う（ジオコーディング不要にする）。"""
        for m in re.finditer(r"q=(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", html or ""):
            lat, lon = float(m.group(1)), float(m.group(2))
            if 24.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0:
                return lat, lon
        return None

    @staticmethod
    def _address_from_soup(soup, text: str) -> str:
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                if re.search(r"所在地|住所", key):
                    val = cells[1].get_text(" ", strip=True)
                    val = re.sub(r"〒\s*\d{3}[-－]\d{4}\s*", "", val).strip()
                    val = re.sub(r"\s+", " ", val).strip()
                    if val:
                        return val[:120]
        m = re.search(r"〒\s*[\d-]+\s+(.+?)(?:Tel|TEL|電話|Googleマップ|\n|$)", text)
        if m:
            addr = re.sub(r"\s+", " ", m.group(1)).strip()
            if addr:
                return addr[:120]
        return ""

    def fetch_medical_location(self, fac: "MedFacility") -> bool:
        """ナビィ医療機関詳細ページ（S2430）から住所と座標を取得して fac に書き込む。

        v1.4: 旧版は kikanKbn を1つだけ試し、外すと住所が取れず＝座標も出ず、
        商圏内にあるのに門前判定から丸ごと抜け落ちていた。病院(1)/診療所(2)の
        両方を順に試すようにして、この取りこぼしを防ぐ。
        """
        if not (fac.pref_cd and fac.kikan_cd):
            return False
        kbns = [fac.kikan_kbn] + [k for k in (2, 1) if k != fac.kikan_kbn]
        for kbn in kbns:
            url = (f"{MHLW_BASE}/juminkanja/S2430/initialize"
                   f"?prefCd={fac.pref_cd}&kikanCd={fac.kikan_cd}&kikanKbn={kbn}")
            html = self._get_html(url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
            if "E-0109" in text or "データは存在しません" in text:
                continue
            fac.kikan_kbn = kbn
            addr = self._address_from_soup(soup, text)
            if addr:
                fac.address = addr
            coords = self._extract_coords_from_html(html)
            if coords:
                fac.lat, fac.lon = coords
            return bool(addr or coords)
        return False

    def get_medical_address(self, pref_cd: str, kikan_cd: str, kikan_kbn: int = 2) -> str:
        """住所だけが欲しい場合の薄いラッパ（旧APIの互換用）。"""
        tmp = MedFacility(name="", pref_cd=pref_cd, kikan_cd=kikan_cd, kikan_kbn=kikan_kbn)
        self.fetch_medical_location(tmp)
        return tmp.address

    def get_pharmacy_detail(self, ph: PharmacyFacility) -> bool:
        """ナビィ薬局詳細ページから総取扱処方箋数（と座標）を取得する。"""
        self._init()
        if not (ph.pref_cd and ph.kikan_cd):
            return False
        url = (f"{MHLW_BASE}/juminkanja/S2430/initialize"
               f"?prefCd={ph.pref_cd}&kikanCd={ph.kikan_cd}&kikanKbn=5")
        html = self._get_html(url)
        if not html:
            return False
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        if "E-0109" in text or "データは存在しません" in text:
            return False

        ph.detail_url = url
        if ph.lat is None:
            coords = self._extract_coords_from_html(html)
            if coords:
                ph.lat, ph.lon = coords
        if not ph.address:
            ph.address = self._address_from_soup(soup, text)

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



# ─── 処方箋数パーサ ────────────────────────────────────────────────────────────
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
        v = fields.get(k)
        if v is None:
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


# ─── 門前/面 判定（薬局視点） ──────────────────────────────────────────────────
def assign_monzen_to_pharmacies(
    pharmacies: List[PharmacyFacility],
    med_facilities: List[MedFacility],
    threshold_m: float = 50.0,
) -> List[str]:
    """
    各薬局に最近接の医療機関を割り当て、閾値以内なら門前薬局と判定する。
    デバッグ用ログ行のリストを返す。
    """
    debug: List[str] = []
    facs_with_coords = [f for f in med_facilities if f.lat is not None and f.lon is not None]
    debug.append(
        f"🔍 門前判定: 薬局={len(pharmacies)}件 "
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
            debug.append(f"  ✅ {ph.name[:20]} → 門前: {best_fac.name[:20]} ({best_dist:.0f}m)")
        elif best_fac:
            ph.pharmacy_type = "面薬局"
            debug.append(
                f"  ➖ {ph.name[:20]} → 面（最近接: {best_fac.name[:20]} {best_dist:.0f}m > {threshold_m:.0f}m）"
            )
        else:
            ph.pharmacy_type = "不明"
            debug.append(f"  ❓ {ph.name[:20]} → 不明（医療機関データなし）")

    return debug


# ─── メイン検索処理 ────────────────────────────────────────────────────────────
@st.cache_resource
def get_scraper():
    return MHLWScraper()


def run_search(
    address: str,
    radius_m: int,
    gate_m: int,
    max_detail: int,
    log: List[str],
    prog,
) -> Tuple[List[PharmacyFacility], List[MedFacility], float, float]:

    # Step1: ジオコーディング
    prog.progress(5, text="📍 住所をジオコーディング中…")
    coords = _geocoder.geocode(address)
    if not coords:
        st.error(f"住所「{address}」の座標取得に失敗しました。より詳細な住所を入力してください。")
        st.stop()
    center_lat, center_lon = coords
    log.append(f"📍 住所: {address} → lat={center_lat:.5f}, lon={center_lon:.5f}")

    # Step2: OSM 薬局検索
    prog.progress(15, text="💊 OSMから薬局を取得中…")
    ph_osm = search_osm_pharmacies(center_lat, center_lon, radius_m)
    log.append(f"💊 OSM薬局: {len(ph_osm)}件取得")
    ph_merged = list(ph_osm)

    # Step3: ナビィ 薬局検索
    prog.progress(30, text="💊 ナビィから薬局リストを取得中…")
    scraper = get_scraper()
    scraper.last_warnings = []
    navvi_phs, navvi_msg = scraper.search_pharmacies_by_latlon(
        center_lat, center_lon, radius_m=radius_m,
        center_name=address[:20],
    )
    log.append(f"💊 {navvi_msg}")
    if not navvi_phs:
        log.append("⚠️ ナビィ薬局が0件。ナビィ側の仕様変更・通信エラーの可能性があります。")

    # マージ: ナビィ固有薬局を追加（v1.4: 重複判定を機関コード優先の厳密判定に変更）
    added_navvi = 0
    need_gc: List[PharmacyFacility] = []
    for nph in navvi_phs:
        dup = next((p for p in ph_merged if same_facility(nph, p, DEDUP_GAP_M)), None)
        if dup is not None:
            # OSM側エントリにナビィの機関コードを補完（詳細取得できるようにする）
            if not dup.pref_cd:
                dup.pref_cd, dup.kikan_cd, dup.href = nph.pref_cd, nph.kikan_cd, nph.href
            continue
        ph_merged.append(nph)
        added_navvi += 1
        if nph.lat is None and nph.address:
            need_gc.append(nph)

    # 一覧に座標が埋まっていなかったぶんだけジオコーディングする
    if need_gc:
        prog.progress(40, text=f"💊 薬局座標を取得中 {len(need_gc)}件…")
        for i, p in enumerate(need_gc):
            if i % 5 == 0:
                prog.progress(40, text=f"💊 薬局座標取得中 {i+1}/{len(need_gc)}件…")
            gc = _geocoder.geocode(p.address)
            if gc:
                p.lat, p.lon = gc
            time.sleep(0.15)  # GSIレート制限対策

    # 商圏外（半径の1.1倍超）だけを落とす。座標不明の薬局は「落とさず残す」。
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
    no_coord = sum(1 for p in ph_merged if p.lat is None)
    log.append(
        f"💊 ナビィ固有追加: {added_navvi}件  合計: {len(ph_merged)}件"
        f"（商圏外除外: {dropped_far}件 / 座標なし: {no_coord}件）"
    )
    if no_coord:
        log.append(f"⚠️ 座標が特定できない薬局が{no_coord}件あります（門前/面の判定は「不明」になります）。")

    # Step4: OSM 医療機関検索（門前判定用）
    prog.progress(55, text="🏥 医療機関（門前判定用）を取得中…")
    med_radius = radius_m + gate_m
    med_osm = search_osm_medical(center_lat, center_lon, med_radius)
    log.append(f"🏥 OSM医療機関: {med_radius}m圏内 {len(med_osm)}件")
    if len(med_osm) == 0:
        log.append("⚠️ OSM医療機関が0件 → ナビィ医療機関のみで判定します")

    # ナビィ医療機関を取得 → 詳細ページ（S2430）から住所取得 → ジオコーディング
    prog.progress(60, text="🏥 ナビィ医療機関リストを取得中…")
    navvi_meds, med_msg = scraper.search_medical_by_latlon(
        center_lat, center_lon, radius_m=med_radius, center_name=address[:20],
    )
    log.append(f"🏥 {med_msg}")

    # v1.4: 旧版の「先頭50件だけ詳細を取る」上限を撤廃した。件数の多いエリアでは
    # 51件目以降が黙って捨てられ、目視で見つかる「漏れ」の主因になっていた。
    seen_cd: set = set()
    med_targets = []
    for f in navvi_meds:
        if not (f.pref_cd and f.kikan_cd):
            continue
        if f.kikan_kbn == 5:                       # kikanKbn=5 は薬局
            continue
        if f.kikan_cd in seen_cd:                  # ページ間の重複のみ除去
            continue
        if is_duplicate_of_any(f, med_osm, DEDUP_GAP_M):
            continue
        seen_cd.add(f.kikan_cd)
        med_targets.append(f)
    log.append(f"🏥 ナビィ医療機関の詳細取得対象: {len(med_targets)}件（上限なし）")

    geocode_ok, geocode_fail, addr_fail = 0, 0, 0
    stats_lock = threading.Lock()

    def _fetch_med(nmf):
        nonlocal geocode_ok, geocode_fail, addr_fail
        got = scraper.fetch_medical_location(nmf)
        if nmf.lat is None and nmf.address:
            gc = _geocoder.geocode(nmf.address)     # 詳細に座標が無いときだけ住所から
            if gc:
                nmf.lat, nmf.lon = gc
        with stats_lock:
            if nmf.lat is not None:
                geocode_ok += 1
            elif not got and not nmf.address:
                addr_fail += 1
            else:
                geocode_fail += 1

    n_med = len(med_targets)

    # v1.5.1: 進捗バー（prog.progress）の更新はメインスレッドで行う。
    # ワーカースレッド内から st API を呼ぶと、新しめの Streamlit
    # （Streamlit Cloud 含む）では NoSessionContext 例外になり検索全体が落ちる。
    if med_targets:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            futures = [ex.submit(_fetch_med, nmf) for nmf in med_targets]
            for i, fut in enumerate(as_completed(futures), start=1):
                fut.result()
                if i % 3 == 0 or i == n_med:
                    prog.progress(
                        62 + int(13 * i / max(n_med, 1)),
                        text=f"🏥 医療機関の詳細を並列取得中 {i}/{n_med}件…",
                    )

    for nmf in med_targets:
        if nmf.lat is not None:
            nmf.distance_m = haversine(center_lat, center_lon, nmf.lat, nmf.lon)
        med_osm.append(nmf)

    log.append(
        f"🏥 医療機関住所取得: 成功={geocode_ok}件（座標確定）"
        f" / 住所取得失敗={addr_fail}件 / geocoding失敗={geocode_fail}件"
        f"  合計（座標あり）: {sum(1 for f in med_osm if f.lat is not None)}件"
    )
    if geocode_fail or addr_fail:
        log.append(
            f"⚠️ 座標を確定できなかった医療機関が{geocode_fail + addr_fail}件あります。"
            "この施設は門前判定に使われないため、近くの薬局が「面」と判定される場合があります。"
        )

    # Step5: 薬局詳細取得（処方箋数）
    all_ph_targets = [p for p in ph_merged if p.pref_cd and p.kikan_cd]
    ph_targets = all_ph_targets[:max_detail]
    log.append(f"💊 ナビィ詳細取得対象: {len(ph_targets)}件 / 対象候補{len(all_ph_targets)}件")
    if len(all_ph_targets) > len(ph_targets):
        skipped = len(all_ph_targets) - len(ph_targets)
        log.append(
            f"⚠️ 「詳細取得件数」の設定により{skipped}件の薬局の処方箋数を取得していません"
            "（薬局自体は一覧に残っています）。サイドバーで上限を上げてください。"
        )
    n_t = len(ph_targets)

    # v1.5.1: こちらも進捗更新はメインスレッドで（NoSessionContext対策）
    if ph_targets:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
            futures = [ex.submit(scraper.get_pharmacy_detail, ph) for ph in ph_targets]
            for i, fut in enumerate(as_completed(futures), start=1):
                fut.result()
                if i % 3 == 0 or i == n_t:
                    prog.progress(
                        77 + int(15 * i / max(n_t, 1)),
                        text=f"💊 処方箋数を並列取得中 {i}/{n_t}件…",
                    )
    # 詳細ページで座標が判明したぶんの距離を再計算
    for p in ph_merged:
        if p.lat is not None:
            p.distance_m = haversine(center_lat, center_lon, p.lat, p.lon)
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    fetched = sum(1 for p in ph_merged if p.detail_fetched)
    log.append(f"💊 詳細取得完了: {fetched}件（処方箋数あり）")

    # Step6: 門前/面 判定
    prog.progress(92, text="🔍 門前/面 判定中…")
    debug_lines = assign_monzen_to_pharmacies(ph_merged, med_osm, threshold_m=float(gate_m))
    log.extend(debug_lines)

    n_monzen = sum(1 for p in ph_merged if p.pharmacy_type == "門前薬局")
    n_men    = sum(1 for p in ph_merged if p.pharmacy_type == "面薬局")
    log.append(f"🏁 判定完了: 門前={n_monzen}件 / 面={n_men}件 / 不明={len(ph_merged)-n_monzen-n_men}件")

    return ph_merged, med_osm, center_lat, center_lon


# ─── UI ───────────────────────────────────────────────────────────────────────
st.title("💊 商圏内 調剤薬局リストアップ v1.5.1")
st.caption("住所と商圏半径を入力すると、圏内の調剤薬局を一覧表示します。")

# ── サイドバー ────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔎 検索条件")
    address_input = st.text_input(
        "住所（商圏の中心）",
        placeholder="例：山梨県中央市若宮50-1",
        help="丁目・番地まで入力すると精度が上がります",
    )
    radius_m = st.slider(
        "商圏半径 (m)", min_value=200, max_value=5000, value=1000, step=100,
        help="この半径内の調剤薬局を検索します",
    )
    gate_m = st.slider(
        "門前判定距離 (m)", min_value=10, max_value=300, value=50, step=10,
        help="薬局から医療機関までの距離がこの値以内なら「門前薬局」と判定します",
    )
    max_detail = st.slider(
        "詳細取得件数（処方箋数）", min_value=5, max_value=300, value=150, step=5,
        help=(
            "ナビィから処方箋数を取得する上限件数（時間に影響します）。"
            "v1.4で並列取得にしたため、150件でも1分程度で終わります。"
            "※この上限を超えても薬局そのものは一覧に出ます（処方箋数が空欄になるだけです）"
        ),
    )
    run_btn = st.button("🔍 検索実行", type="primary", use_container_width=True)
    st.divider()
    st.caption(
        "データソース: 厚労省ナビィ / OpenStreetMap / 国土地理院\n\n"
        "※ 初回検索は30〜60秒かかります"
    )

# ── セッション初期化 ──────────────────────────────────────────────────────────
if "ph_results" not in st.session_state:
    st.session_state.ph_results  = []
    st.session_state.med_results = []
    st.session_state.center_lat  = None
    st.session_state.center_lon  = None
    st.session_state.search_log  = []
    st.session_state.last_address = ""

# ── 検索実行 ──────────────────────────────────────────────────────────────────
if run_btn and address_input.strip():
    log: List[str] = []
    prog = st.progress(0, text="検索を開始しています…")
    try:
        ph_list, med_list, clat, clon = run_search(
            address_input.strip(), radius_m, gate_m, max_detail, log, prog
        )
        st.session_state.ph_results   = ph_list
        st.session_state.med_results  = med_list
        st.session_state.center_lat   = clat
        st.session_state.center_lon   = clon
        st.session_state.search_log   = log
        st.session_state.last_address = address_input.strip()
        prog.progress(100, text="✅ 完了!")
        time.sleep(0.3)
        prog.empty()
    except Exception as e:
        prog.empty()
        # v1.5: st.stop() 由来の StopException まで握りつぶして「エラー: （空欄）」
        # と表示していたのを修正。StopException はそのまま通す。
        if e.__class__.__name__ in ("StopException", "RerunException"):
            raise
        # v1.5: 原因調査ができるよう、例外の種類・トレースバック・実行ログを表示する。
        st.error(f"検索中にエラーが発生しました: {type(e).__name__}: {e}")
        with st.expander("🔧 エラー詳細（開発者に伝える用）", expanded=True):
            st.code(traceback.format_exc())
        if log:
            with st.expander("📋 実行ログ（どこまで進んだか）", expanded=True):
                st.text("\n".join(log))
elif run_btn:
    st.warning("住所を入力してください。")

# ── 結果表示 ──────────────────────────────────────────────────────────────────
if st.session_state.ph_results:
    pharmacies: List[PharmacyFacility] = st.session_state.ph_results
    med_facs:   List[MedFacility]      = st.session_state.med_results
    center_lat = st.session_state.center_lat
    center_lon = st.session_state.center_lon

    # ── サマリーメトリクス ────────────────────────────────────────────────
    n_total  = len(pharmacies)
    n_monzen = sum(1 for p in pharmacies if p.pharmacy_type == "門前薬局")
    n_men    = sum(1 for p in pharmacies if p.pharmacy_type == "面薬局")
    n_unkn   = n_total - n_monzen - n_men
    rx_vals  = [p.annual_rx_count for p in pharmacies if p.annual_rx_count]
    avg_rx   = int(sum(rx_vals) / len(rx_vals)) if rx_vals else None

    st.success(
        f"**{st.session_state.last_address}** の {radius_m:,}m 商圏内: "
        f"**{n_total} 件** の調剤薬局が見つかりました"
        f"（門前判定閾値: {gate_m}m）"
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("調剤薬局 合計", f"{n_total} 件")
    col2.metric(
        "🔴 門前薬局",
        f"{n_monzen} 件",
        delta=f"{n_monzen/n_total*100:.0f}%" if n_total else "0%",
        delta_color="off",
    )
    col3.metric(
        "🔵 面薬局",
        f"{n_men} 件",
        delta=f"{n_men/n_total*100:.0f}%" if n_total else "0%",
        delta_color="off",
    )
    col4.metric(
        "年間処方箋数 平均",
        f"{avg_rx:,} 件" if avg_rx else "— 件",
        help="処方箋数取得済み薬局の平均",
    )

    st.divider()

    # ── タブ ─────────────────────────────────────────────────────────────
    tab_list, tab_map, tab_log = st.tabs(["📋 薬局一覧", "🗺️ 地図", "📝 ログ"])

    # ── 薬局一覧タブ ──────────────────────────────────────────────────────
    with tab_list:
        TYPE_ICONS = {"門前薬局": "🔴", "面薬局": "🔵", "不明": "⚪"}

        # フィルタ
        filter_col1, filter_col2 = st.columns([2, 2])
        with filter_col1:
            type_filter = st.multiselect(
                "種別フィルタ",
                options=["門前薬局", "面薬局", "不明"],
                default=["門前薬局", "面薬局", "不明"],
            )
        with filter_col2:
            sort_by = st.selectbox(
                "並び替え",
                options=["中心からの距離", "年間処方箋数（多い順）", "種別"],
                index=0,
            )

        filtered = [p for p in pharmacies if p.pharmacy_type in type_filter]
        if sort_by == "年間処方箋数（多い順）":
            filtered.sort(key=lambda p: p.annual_rx_count or 0, reverse=True)
        elif sort_by == "種別":
            order = {"門前薬局": 0, "面薬局": 1, "不明": 2}
            filtered.sort(key=lambda p: (order.get(p.pharmacy_type, 9), p.distance_m or 9_999_999))
        else:
            filtered.sort(key=lambda p: p.distance_m or 9_999_999)

        # テーブル構築
        rows = []
        for i, ph in enumerate(filtered, 1):
            icon = TYPE_ICONS.get(ph.pharmacy_type, "⚪")
            rx_str = f"{ph.annual_rx_count:,}" if ph.annual_rx_count else "—"
            nearest_dist_str = (
                f"{int(ph.nearest_clinic_dist_m)}m"
                if ph.nearest_clinic_dist_m is not None else "—"
            )
            dist_str = f"{int(ph.distance_m)}m" if ph.distance_m is not None else "—"
            link_str = f"[ナビィ]({ph.href})" if ph.href else "—"
            rows.append({
                "No": i,
                "薬局名": ph.name,
                "住所": ph.address or "—",
                "中心からの距離": dist_str,
                "種別": f"{icon} {ph.pharmacy_type}",
                "最近接医療機関": ph.nearest_clinic_name,
                "最近接距離": nearest_dist_str,
                "年間処方箋数": rx_str,
                "処方箋数出典": ph.annual_rx_source if ph.annual_rx_count else "—",
                "ナビィ": link_str,
            })

        df = pd.DataFrame(rows)

        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "ナビィ": st.column_config.LinkColumn("ナビィ"),
                "No": st.column_config.NumberColumn(width="small"),
                "種別": st.column_config.TextColumn(width="medium"),
                "中心からの距離": st.column_config.TextColumn(width="small"),
                "最近接距離": st.column_config.TextColumn(width="small"),
            },
        )

        # CSV ダウンロード
        csv_rows = []
        for ph in filtered:
            csv_rows.append({
                "薬局名":         ph.name,
                "住所":           ph.address,
                "中心からの距離_m": int(ph.distance_m) if ph.distance_m else "",
                "種別":           ph.pharmacy_type,
                "最近接医療機関":  ph.nearest_clinic_name,
                "最近接距離_m":   int(ph.nearest_clinic_dist_m) if ph.nearest_clinic_dist_m else "",
                "年間処方箋数":   ph.annual_rx_count or "",
                "処方箋数出典":   ph.annual_rx_source if ph.annual_rx_count else "",
                "ナビィURL":      ph.href,
                "データソース":   ph.source,
                "緯度":           ph.lat or "",
                "経度":           ph.lon or "",
            })
        csv_df = pd.DataFrame(csv_rows)
        csv_bytes = csv_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "⬇️ CSVダウンロード",
            data=csv_bytes,
            file_name=f"薬局_{st.session_state.last_address[:15]}_{radius_m}m.csv",
            mime="text/csv",
        )

    # ── 地図タブ ──────────────────────────────────────────────────────────
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

        # 薬局マーカー
        color_map = {"門前薬局": "red", "面薬局": "green", "不明": "gray"}
        for ph in pharmacies:
            if ph.lat is None or ph.lon is None:
                continue
            col = color_map.get(ph.pharmacy_type, "gray")
            rx_txt = f"{ph.annual_rx_count:,} 枚/年" if ph.annual_rx_count else "処方箋数 不明"
            nearest_txt = (
                f"{ph.nearest_clinic_name}（{int(ph.nearest_clinic_dist_m)}m）"
                if ph.nearest_clinic_dist_m is not None else "—"
            )
            popup_html = (
                f"<b>{ph.name}</b><br>"
                f"種別: {ph.pharmacy_type}<br>"
                f"最近接医療機関: {nearest_txt}<br>"
                f"年間処方箋数: {rx_txt}<br>"
                f"住所: {ph.address or '—'}"
            )
            folium.CircleMarker(
                location=[ph.lat, ph.lon],
                radius=8,
                color=col, fill=True, fill_color=col, fill_opacity=0.8,
                popup=folium.Popup(popup_html, max_width=280),
                tooltip=f"{ph.name}（{ph.pharmacy_type}）",
            ).add_to(m)

        # 医療機関マーカー（座標あるもの）
        for fac in med_facs:
            if fac.lat is None or fac.lon is None:
                continue
            folium.CircleMarker(
                location=[fac.lat, fac.lon],
                radius=5,
                color="purple", fill=True, fill_color="purple", fill_opacity=0.6,
                tooltip=f"🏥 {fac.name}",
            ).add_to(m)

        # 凡例
        legend_html = """
        <div style="position:fixed; bottom:30px; left:30px; z-index:1000;
                    background:white; padding:10px; border-radius:8px;
                    border:1px solid #ccc; font-size:13px;">
          <b>凡例</b><br>
          🔴 門前薬局<br>
          🟢 面薬局<br>
          ⚪ 不明<br>
          🟣 医療機関
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))

        st_folium(m, use_container_width=True, height=600)

    # ── ログタブ ──────────────────────────────────────────────────────────
    with tab_log:
        # v1.4: 「漏れているかどうか」を目視確認する前に機械側で言い切るための欄。
        # 打ち切り・座標未確定など、リストが実態より少なくなる要因をここに集約する。
        st.subheader("🩺 取りこぼし診断")
        alerts = [l for l in st.session_state.search_log if l.startswith("⚠️")]
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
            "原理的に取得できません。その場合はOSM側の結果（source=osm）もあわせてご確認ください。"
        )
        st.divider()
        st.subheader("検索ログ")
        for line in st.session_state.search_log:
            st.text(line)

else:
    st.info("← 左のサイドバーで住所と条件を設定し、「検索実行」を押してください。")
