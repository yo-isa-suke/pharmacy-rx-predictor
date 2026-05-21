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
    page_title="商圏内 調剤薬局リストアップ",
    page_icon="💊",
    layout="wide",
)

# ─── 定数 ─────────────────────────────────────────────────────────────────────
MHLW_DOMAIN = "https://www.iryou.teikyouseido.mhlw.go.jp"
MHLW_BASE   = MHLW_DOMAIN + "/znk-web"

OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]


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


# ─── OSM 医療機関検索（門前判定用） ───────────────────────────────────────────
def search_osm_medical(lat: float, lon: float, radius_m: int) -> List[MedFacility]:
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
    data = _overpass_post(query)
    if not data:
        return []
    facilities: List[MedFacility] = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name:ja") or tags.get("name", "")
        if not name:
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

        dist_str = f"{radius_m//1000}km" if radius_m >= 1000 else f"{radius_m}m"
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
        max_pages: int = 5,
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
                r3 = self.session.get(f"{redirect_url}{sep}page={page}&size=20&sortNo=2", timeout=15)
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
        dist_str = f"{radius_m//1000}km" if radius_m >= 1000 else f"{radius_m}m"
        return all_facs, f"MHLW医療機関: {dist_str}圏内 全{total}件/取得{len(all_facs)}件"

    def _parse_med_list(self, html: str) -> Tuple[List[MedFacility], int]:
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
            # 住所を抽出（ジオコーディングで座標付与するために必要）
            address = ""
            for p_tag in item.find_all("p"):
                raw = p_tag.get_text(" ", strip=True)
                if "〒" in raw or re.search(r"[都道府県市区町村]", raw):
                    cleaned = re.sub(r"〒\s*\d{3}[-－]\d{4}", "", raw)
                    cleaned = re.sub(r"Googleマップで見る", "", cleaned)
                    address = re.sub(r"\s+", " ", cleaned).strip()[:120]
                    break
            results.append(MedFacility(name=name, address=address, source="mhlw"))
        return results, max(total, len(results))

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
    time.sleep(2)
    ph_osm = search_osm_pharmacies(center_lat, center_lon, radius_m)
    log.append(f"💊 OSM薬局: {len(ph_osm)}件取得")
    ph_merged = list(ph_osm)

    # Step3: ナビィ 薬局検索
    prog.progress(30, text="💊 ナビィから薬局リストを取得中…")
    scraper = get_scraper()
    navvi_phs, navvi_msg = scraper.search_pharmacies_by_latlon(
        center_lat, center_lon, radius_m=radius_m,
        center_name=address[:20], max_pages=8,
    )
    log.append(f"💊 {navvi_msg}")

    # マージ: ナビィ固有薬局をジオコーディングして追加
    existing_names = [p.name for p in ph_merged]
    added_navvi = 0
    prog.progress(40, text=f"💊 ナビィ薬局 {len(navvi_phs)}件の座標を取得中…")
    for i, nph in enumerate(navvi_phs):
        if i % 5 == 0:
            prog.progress(40, text=f"💊 薬局座標取得中 {i+1}/{len(navvi_phs)}件…")
        is_dup = any(name_similarity(nph.name, en) >= 0.65 for en in existing_names)
        if is_dup:
            # OSM既存エントリにpref_cd/kikan_cdを補完
            for osm_ph in ph_merged:
                if name_similarity(nph.name, osm_ph.name) >= 0.65 and not osm_ph.pref_cd:
                    osm_ph.pref_cd  = nph.pref_cd
                    osm_ph.kikan_cd = nph.kikan_cd
                    osm_ph.href     = nph.href
            continue
        if nph.address:
            gc = _geocoder.geocode(nph.address)
            if gc:
                nph.lat, nph.lon = gc
                nph.distance_m = haversine(center_lat, center_lon, nph.lat, nph.lon)
                if nph.distance_m > radius_m * 1.1:
                    time.sleep(0.15)
                    continue  # 商圏外
            time.sleep(0.15)  # GSIレート制限対策
        ph_merged.append(nph)
        existing_names.append(nph.name)
        added_navvi += 1

    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    no_coord = sum(1 for p in ph_merged if p.lat is None)
    log.append(f"💊 ナビィ固有追加: {added_navvi}件  合計: {len(ph_merged)}件（座標なし: {no_coord}件）")

    # Step4: OSM 医療機関検索（門前判定用）
    prog.progress(55, text="🏥 医療機関（門前判定用）を取得中…")
    med_radius = radius_m + gate_m
    time.sleep(2)
    med_osm = search_osm_medical(center_lat, center_lon, med_radius)
    log.append(f"🏥 OSM医療機関: {med_radius}m圏内 {len(med_osm)}件")
    if len(med_osm) == 0:
        log.append("⚠️ OSM医療機関が0件 → ナビィ医療機関のみで判定します")

    # ナビィ医療機関を取得してジオコーディングで座標付与（OSMの疎な地方部をカバー）
    prog.progress(60, text="🏥 ナビィ医療機関リストを取得中…")
    navvi_meds, med_msg = scraper.search_medical_by_latlon(
        center_lat, center_lon, radius_m=med_radius, max_pages=5
    )
    log.append(f"🏥 {med_msg}")
    med_existing_names = [f.name for f in med_osm]
    geocode_ok, geocode_fail = 0, 0
    for i, nmf in enumerate(navvi_meds):
        if any(name_similarity(nmf.name, en) >= 0.65 for en in med_existing_names):
            # OSM既存エントリと重複 → スキップ（OSMの座標を使う）
            continue
        # 住所からジオコーディングして座標を付与
        if nmf.address:
            gc = _geocoder.geocode(nmf.address)
            if gc:
                nmf.lat, nmf.lon = gc
                nmf.distance_m = haversine(center_lat, center_lon, nmf.lat, nmf.lon)
                geocode_ok += 1
            else:
                geocode_fail += 1
            time.sleep(0.15)  # GSIレート制限対策
        med_osm.append(nmf)
        med_existing_names.append(nmf.name)
    log.append(
        f"🏥 ナビィ医療機関ジオコーディング: 成功={geocode_ok}件 失敗={geocode_fail}件"
        f"  合計（座標あり）: {sum(1 for f in med_osm if f.lat is not None)}件"
    )

    # Step5: 薬局詳細取得（処方箋数）
    ph_targets = [p for p in ph_merged if p.pref_cd and p.kikan_cd][:max_detail]
    log.append(f"💊 ナビィ詳細取得対象: {len(ph_targets)}件")
    for i, ph in enumerate(ph_targets):
        prog.progress(
            65 + int(25 * i / max(len(ph_targets), 1)),
            text=f"💊 詳細取得中 ({i+1}/{len(ph_targets)}): {ph.name[:20]}…",
        )
        scraper.get_pharmacy_detail(ph)
        time.sleep(0.5)
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
st.title("💊 商圏内 調剤薬局リストアップ")
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
        "詳細取得件数（処方箋数）", min_value=5, max_value=50, value=30, step=5,
        help="ナビィから処方箋数を取得する上限件数（時間に影響します）",
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
        st.error(f"検索中にエラーが発生しました: {e}")
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
        st.subheader("検索ログ")
        for line in st.session_state.search_log:
            st.text(line)

else:
    st.info("← 左のサイドバーで住所と条件を設定し、「検索実行」を押してください。")
