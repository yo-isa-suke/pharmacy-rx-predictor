"""
薬局 年間処方箋枚数 多面的予測ツール v2.0
==========================================
v1の厚生労働省実データ取得に加え、2種類の独立した予測アプローチを組み合わせて
実績値との乖離を評価します。

【方法①】近隣医療機関アプローチ
  - OpenStreetMap (Overpass API) で近隣クリニック・病院を検索
  - 厚生労働省ポータルで各施設の外来患者数を照会
  - 診療科別処方箋発行率 × 院外処方率 × 競合薬局シェアから予測

【方法②】商圏人口動態アプローチ
  - 商圏内人口推計（国勢調査ベース）
  - 性年齢別有病率・受診率 × 処方箋発行率 × 薬局シェアから予測

【競合マップ】Folium (OpenStreetMap) で近隣医療施設・薬局を可視化
"""

import math
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import folium
import requests
import streamlit as st
from bs4 import BeautifulSoup
from streamlit_folium import st_folium

# ---------------------------------------------------------------------------
# 定数・統計データ
# ---------------------------------------------------------------------------

PREFECTURES = [
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県",
    "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県",
    "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県",
    "福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
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

# 全国統計（厚生労働省「調剤医療費の動向」2022年度）
NATIONAL_STATS = {
    "total_prescriptions": 885_000_000,
    "total_pharmacies": 61_860,
    "average_per_year": 14_305,
    "median_estimate": 8_000,
    "daily_average": 44,
    "working_days": 305,
    "outpatient_rx_rate": 0.745,   # 院外処方率（全国平均）
    "prescription_per_visit": 0.65, # 外来1受診あたり処方箋発行率
    "source": "厚生労働省「調剤医療費の動向」2022年度",
}

# 診療科別 処方箋発行率（外来患者1人あたり）
# 出典: 厚生労働省「受療行動調査」「薬局・薬剤師機能に関する調査」等より推計
SPECIALTY_RX_RATES: Dict[str, Tuple[float, str]] = {
    "一般内科":     (0.76, "内科系全般（慢性疾患が多く高処方率）"),
    "循環器内科":   (0.88, "高血圧・心疾患は継続処方が多い"),
    "消化器内科":   (0.74, "胃腸疾患は薬物療法が主体"),
    "糖尿病内科":   (0.90, "インスリン・経口血糖降下薬の継続処方"),
    "神経内科":     (0.82, "神経疾患は薬物療法依存度高"),
    "呼吸器内科":   (0.78, "喘息・COPD等の継続薬多い"),
    "外科":         (0.58, "術後フォローの処方は比較的少ない"),
    "整形外科":     (0.72, "鎮痛薬・湿布等の処方多い"),
    "皮膚科":       (0.64, "外用薬・抗アレルギー薬など"),
    "眼科":         (0.52, "点眼薬は院内交付も多い"),
    "耳鼻咽喉科":   (0.58, "抗菌薬等の短期処方が多い"),
    "精神科":       (0.85, "向精神薬は継続処方がほぼ必須"),
    "小児科":       (0.62, "急性疾患が多く処方は比較的少ない"),
    "産婦人科":     (0.44, "健診・分娩が多く薬処方は少ない"),
    "泌尿器科":     (0.70, "前立腺疾患・過活動膀胱等の継続薬"),
    "リハビリ科":   (0.40, "リハビリ中心で処方は少ない"),
    "不明/その他":  (0.68, "全診療科平均値を使用"),
}

# OSM の healthcare:speciality タグ → 日本語診療科マッピング
OSM_SPECIALTY_MAP: Dict[str, str] = {
    "general": "一般内科", "general_practitioner": "一般内科", "internal": "一般内科",
    "cardiology": "循環器内科",
    "gastroenterology": "消化器内科",
    "diabetes": "糖尿病内科", "endocrinology": "糖尿病内科",
    "neurology": "神経内科",
    "pulmonology": "呼吸器内科", "respiratory": "呼吸器内科",
    "surgery": "外科",
    "orthopaedics": "整形外科", "orthopedics": "整形外科",
    "dermatology": "皮膚科",
    "ophthalmology": "眼科",
    "otolaryngology": "耳鼻咽喉科", "ent": "耳鼻咽喉科",
    "psychiatry": "精神科", "mental_health": "精神科",
    "paediatrics": "小児科", "pediatrics": "小児科",
    "gynaecology": "産婦人科", "obstetrics": "産婦人科",
    "urology": "泌尿器科",
    "rehabilitation": "リハビリ科",
}

# 年齢層別 外来受診率（回/年/人）厚生労働省「患者調査」2020年
VISIT_RATE_BY_AGE: Dict[str, float] = {
    "0-14歳":  9.8,
    "15-44歳": 7.2,
    "45-64歳": 11.3,
    "65-74歳": 19.2,
    "75歳以上": 22.1,
}

# 日本の年齢分布（2020年国勢調査）
AGE_DISTRIBUTION: Dict[str, float] = {
    "0-14歳":  0.119,
    "15-44歳": 0.342,
    "45-64歳": 0.256,
    "65-74歳": 0.145,
    "75歳以上": 0.138,
}

# 商圏の人口密度選択肢（人/km²）
AREA_DENSITY_OPTIONS: Dict[str, int] = {
    "超高密度（東京23区・大阪市中心等）": 15_000,
    "高密度（政令指定都市中心部）":      8_000,
    "中高密度（地方都市中心部）":        3_000,
    "中密度（郊外住宅地）":             1_500,
    "低密度（地方市街地）":             500,
    "超低密度（農村・山間部）":          100,
}

# 大手チェーン薬局（競合判定に使用）
MAJOR_CHAINS = [
    "ウエルシア", "ツルハ", "マツモトキヨシ", "マツキヨ", "スギ薬局",
    "コスモス薬品", "クリエイト", "サンドラッグ", "カワチ薬品",
    "日本調剤", "クオール", "アイン", "ファーマライズ", "総合メディカル",
]

# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class PharmacyCandidate:
    name: str
    address: str
    href: str
    pref_cd: str = ""
    kikan_cd: str = ""

@dataclass
class NearbyFacility:
    name: str
    facility_type: str       # "hospital" | "clinic" | "pharmacy"
    lat: float
    lon: float
    distance_m: float        # 対象薬局からの直線距離（m）
    specialty: str = "不明/その他"
    daily_outpatients: int = 0
    beds: int = 0
    has_inhouse_pharmacy: bool = False
    has_gate_pharmacy: bool = False  # 門前薬局が存在するか
    osm_tags: Dict = field(default_factory=dict)
    mhlw_annual_outpatients: Optional[int] = None

@dataclass
class PredictionResult:
    method_name: str
    annual_rx: int
    min_val: int
    max_val: int
    confidence: str
    daily_rx: int
    breakdown: List[Dict] = field(default_factory=list)
    methodology: List[str] = field(default_factory=list)
    references: List[Dict] = field(default_factory=list)

@dataclass
class FullAnalysis:
    pharmacy_name: str
    pharmacy_address: str
    pharmacy_lat: float
    pharmacy_lon: float
    mhlw_annual_rx: Optional[int]
    mhlw_source_url: str
    method1: Optional[PredictionResult]
    method2: Optional[PredictionResult]
    nearby_medical: List[NearbyFacility]
    nearby_pharmacies: List[NearbyFacility]
    search_log: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. ジオコーダー（Nominatim / OpenStreetMap）
# ---------------------------------------------------------------------------

class GeocoderService:
    """住所 → 緯度経度変換（Nominatim APIを使用・無料・APIキー不要）"""

    URL = "https://nominatim.openstreetmap.org/search"

    def geocode(self, address: str) -> Tuple[Optional[float], Optional[float], str]:
        """
        住所を緯度経度に変換する。

        Returns:
            (lat, lon, normalized_address) or (None, None, error_message)
        """
        if not address:
            return None, None, "住所が空です"

        # 「Googleマップで見る」などの余分なテキストを除去
        clean_address = re.sub(r"Googleマップ.*|Google Map.*", "", address).strip()
        clean_address = re.sub(r"\s+", " ", clean_address)

        try:
            # 日本語住所はそのまま渡す（Nominatimは対応済み）
            params = {
                "q": clean_address + ", 日本",
                "format": "json",
                "limit": 1,
                "addressdetails": 1,
            }
            headers = {"User-Agent": "PharmacyRxPredictorV2/1.0 (research tool)"}
            r = requests.get(self.URL, params=params, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()

            if not data:
                # 住所を短縮して再試行
                parts = clean_address.split()
                if len(parts) > 2:
                    short_addr = " ".join(parts[:3])
                    params["q"] = short_addr + ", 日本"
                    r = requests.get(self.URL, params=params, headers=headers, timeout=10)
                    data = r.json()

            if data:
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                display = data[0].get("display_name", clean_address)
                return lat, lon, display

            return None, None, f"住所が見つかりません: {clean_address}"

        except requests.Timeout:
            return None, None, "ジオコーダーへの接続タイムアウト"
        except Exception as e:
            return None, None, f"ジオコーダーエラー: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# 2. 近隣施設検索（Overpass API / OpenStreetMap）
# ---------------------------------------------------------------------------

class OverpassSearcher:
    """OpenStreetMap の Overpass API で近隣医療施設・薬局を検索（無料・APIキー不要）"""

    URL = "https://overpass-api.de/api/interpreter"

    def search_nearby(
        self, lat: float, lon: float, radius: int = 500
    ) -> Tuple[List[NearbyFacility], List[NearbyFacility], str]:
        """
        指定座標の半径radius(m)内の医療施設と薬局を取得する。

        Returns:
            (medical_facilities, pharmacies, status_message)
        """
        query = f"""
[out:json][timeout:30];
(
  node["amenity"~"^(hospital|clinic|doctors)$"](around:{radius},{lat},{lon});
  way["amenity"~"^(hospital|clinic|doctors)$"](around:{radius},{lat},{lon});
  node["healthcare"~"^(hospital|clinic|doctor|centre)$"](around:{radius},{lat},{lon});
  way["healthcare"~"^(hospital|clinic|doctor|centre)$"](around:{radius},{lat},{lon});
  node["amenity"="pharmacy"](around:{radius},{lat},{lon});
  way["amenity"="pharmacy"](around:{radius},{lat},{lon});
);
out center tags;
"""
        try:
            r = requests.post(self.URL, data={"data": query}, timeout=30)
            r.raise_for_status()
            data = r.json()
        except requests.Timeout:
            return [], [], "Overpass APIタイムアウト（30秒）"
        except Exception as e:
            return [], [], f"Overpass APIエラー: {e}"

        medical: List[NearbyFacility] = []
        pharmacies: List[NearbyFacility] = []

        for elem in data.get("elements", []):
            tags = elem.get("tags", {})
            name = tags.get("name", tags.get("name:ja", "名称不明"))
            if not name or name == "名称不明":
                continue

            # 座標
            if elem["type"] == "node":
                e_lat, e_lon = elem.get("lat", 0), elem.get("lon", 0)
            else:
                center = elem.get("center", {})
                e_lat, e_lon = center.get("lat", 0), center.get("lon", 0)

            if not (e_lat and e_lon):
                continue

            dist = self._haversine(lat, lon, e_lat, e_lon)

            amenity = tags.get("amenity", "")
            healthcare = tags.get("healthcare", "")
            is_pharmacy = amenity == "pharmacy"

            if is_pharmacy:
                pharmacies.append(NearbyFacility(
                    name=name,
                    facility_type="pharmacy",
                    lat=e_lat, lon=e_lon,
                    distance_m=dist,
                    osm_tags=tags,
                ))
                continue

            # 医療機関
            ftype = "hospital" if (
                amenity == "hospital"
                or healthcare == "hospital"
                or int(tags.get("beds", 0) or 0) >= 20
            ) else "clinic"

            # 診療科
            specialty_raw = tags.get("healthcare:speciality", tags.get("specialty", ""))
            specialty = OSM_SPECIALTY_MAP.get(specialty_raw.lower(), "一般内科") if specialty_raw else "一般内科"

            # 院内薬局フラグ
            has_inhouse = tags.get("pharmacy", "") in ["yes", "dispensing"]

            # 病床数
            beds = int(tags.get("beds", 0) or 0)

            # 日次外来患者推計
            daily_op = self._estimate_daily_outpatients(ftype, beds, tags)

            medical.append(NearbyFacility(
                name=name,
                facility_type=ftype,
                lat=e_lat, lon=e_lon,
                distance_m=dist,
                specialty=specialty,
                daily_outpatients=daily_op,
                beds=beds,
                has_inhouse_pharmacy=has_inhouse,
                osm_tags=tags,
            ))

        medical.sort(key=lambda x: x.distance_m)
        pharmacies.sort(key=lambda x: x.distance_m)

        return medical, pharmacies, f"医療機関{len(medical)}件・薬局{len(pharmacies)}件を取得"

    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """2点間の距離をメートルで計算"""
        R = 6_371_000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlam = math.radians(lon2 - lon1)
        a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def _estimate_daily_outpatients(ftype: str, beds: int, tags: Dict) -> int:
        """施設規模から1日外来患者数を推計"""
        if ftype == "hospital":
            if beds >= 300:   return 1_000
            if beds >= 100:   return 400
            if beds >= 20:    return 150
            return 150
        else:  # clinic
            # 診療科や規模から推計
            doctors = int(tags.get("staff:count", 0) or 0)
            if doctors >= 3:  return 100
            if doctors >= 2:  return 60
            return 35  # 一般的な個人クリニック


# ---------------------------------------------------------------------------
# 3. 厚生労働省スクレイパー（v1から継承・医療機関検索を追加）
# ---------------------------------------------------------------------------

class MHLWScraper:
    """厚生労働省 医療情報ネット（ナビイ）スクレイパー"""

    DOMAIN = "https://www.iryou.teikyouseido.mhlw.go.jp"
    BASE   = DOMAIN + "/znk-web"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        self._initialized = False

    def initialize_session(self) -> bool:
        try:
            r = self.session.get(
                f"{self.BASE}/juminkanja/S2300/initialize",
                timeout=15, allow_redirects=True,
            )
            self._initialized = r.status_code == 200
            return self._initialized
        except Exception:
            return False

    def search_pharmacy_candidates(
        self, keyword: str, pref_code: str = "", max_pages: int = 3
    ) -> Tuple[List[PharmacyCandidate], int, str]:
        """薬局候補を検索（sjk=2）"""
        return self._search_candidates(keyword, pref_code, max_pages, sjk="2")

    def search_medical_candidates(
        self, keyword: str, pref_code: str = ""
    ) -> Tuple[List[PharmacyCandidate], int, str]:
        """医療機関候補を検索（sjk=1）"""
        return self._search_candidates(keyword, pref_code, max_pages=1, sjk="1")

    def _search_candidates(
        self, keyword: str, pref_code: str, max_pages: int, sjk: str
    ) -> Tuple[List[PharmacyCandidate], int, str]:
        if not self._initialized:
            self.initialize_session()

        try:
            r = self.session.get(
                f"{self.BASE}/juminkanja/S2300/yakkyokuSearch",
                params={"yakkyokuKeyword": keyword, "yakkyokuKeyword2": "", "searchJudgeKbn": "2"},
                headers={"ajaxFlag": "true"}, timeout=12,
            )
            if r.status_code != 200:
                return [], 0, f"検索失敗 HTTP {r.status_code}"
            j = r.json()
            if j.get("code") != "0":
                return [], 0, "検索バリデーション失敗"
        except Exception as e:
            return [], 0, f"検索エラー: {e}"

        all_candidates: List[PharmacyCandidate] = []
        total_count = 0
        encoded = urllib.parse.quote(keyword)

        for page in range(max_pages):
            params = {"sjk": sjk, "page": str(page), "size": "20", "sortNo": "1"}
            if pref_code:
                params["prefCd"] = pref_code
            try:
                r2 = self.session.get(
                    f"{self.BASE}/juminkanja/S2400/initialize/{encoded}/",
                    params=params, timeout=15,
                )
                if r2.status_code != 200:
                    break
                candidates, total = self._parse_candidate_list(r2.text, sjk)
                if page == 0:
                    total_count = total
                all_candidates.extend(candidates)
                if len(candidates) == 0 or len(all_candidates) >= total_count:
                    break
                time.sleep(0.3)
            except Exception:
                break

        return all_candidates, total_count, f"{len(all_candidates)}件取得（全{total_count}件）"

    def _parse_candidate_list(
        self, html: str, sjk: str = "2"
    ) -> Tuple[List[PharmacyCandidate], int]:
        soup = BeautifulSoup(html, "html.parser")
        candidates = []
        total = 0
        cnt_match = re.search(r"(\d{1,6})\s*件", soup.get_text())
        if cnt_match:
            total = int(cnt_match.group(1))

        for item in soup.find_all("div", class_="item"):
            h3 = item.find("h3", class_="name")
            if not h3:
                continue
            link = h3.find("a", href=True)
            if not link:
                continue
            name = link.get_text(strip=True)
            href = link.get("href", "")
            if not href:
                continue
            if href.startswith("/"):
                href = self.DOMAIN + href

            parsed = urllib.parse.urlparse(href)
            qp = dict(urllib.parse.parse_qsl(parsed.query))
            pref_cd = qp.get("prefCd", "")
            kikan_cd = qp.get("kikanCd", "")

            address = ""
            for dl in item.find_all("dl"):
                dt = dl.find("dt")
                if not dt:
                    continue
                img = dt.find("img")
                dt_text = dt.get_text(strip=True)
                if (img and "住所" in img.get("alt", "")) or any(
                    kw in dt_text for kw in ["住所", "所在地"]
                ):
                    dd = dl.find("dd")
                    if dd:
                        for a in dd.find_all("a"):
                            a.decompose()
                        raw = dd.get_text(strip=True)
                        cleaned = re.sub(r"〒\s*\d{3}[-－]\d{4}\s*", "", raw)
                        address = re.sub(r"\s+", " ", cleaned).strip()[:60]
                    break

            if name:
                candidates.append(PharmacyCandidate(
                    name=name, address=address, href=href,
                    pref_cd=pref_cd, kikan_cd=kikan_cd,
                ))

        return candidates, max(total, len(candidates))

    def get_pharmacy_detail(
        self, candidate: PharmacyCandidate
    ) -> Tuple[Optional[Dict], str]:
        """薬局詳細（処方箋数・住所）を取得"""
        if not self._initialized:
            self.initialize_session()

        if candidate.pref_cd and candidate.kikan_cd:
            url = (
                f"{self.BASE}/juminkanja/S2430/initialize"
                f"?prefCd={candidate.pref_cd}&kikanCd={candidate.kikan_cd}&kikanKbn=5"
            )
        else:
            url = candidate.href

        try:
            r = self.session.get(url, timeout=15)
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            data = self._parse_pharmacy_detail(r.text)
            data["source_url"] = url
            return data, "OK"
        except Exception as e:
            return None, str(e)

    def _parse_pharmacy_detail(self, html: str) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        data: Dict = {}
        fields: Dict[str, str] = {}

        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key:
                    fields[key] = val

        for dl in soup.find_all("dl"):
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            for dt, dd in zip(dts, dds):
                key = dt.get_text(strip=True)
                val = dd.get_text(strip=True)
                if key:
                    fields[key] = val

        data["all_fields"] = fields

        # 住所取得
        for k, v in fields.items():
            if "所在地" in k and "フリガナ" not in k and "英語" not in k:
                clean = re.sub(r"Googleマップ.*", "", v).strip()
                data["address"] = clean
                break

        # 総取扱処方箋数（最優先）
        rx_annual = None
        rx_period = None
        for field_key, field_val in fields.items():
            if "総取扱処方箋数" in field_key:
                nums = re.findall(r"[\d,]+", field_val)
                if nums:
                    try:
                        n = int(nums[0].replace(",", ""))
                        if n > 0:
                            rx_annual = n
                            rx_period = "年間実績（前年1年間の取扱処方箋枚数）"
                            break
                    except (ValueError, OverflowError):
                        pass

        # フォールバック
        if rx_annual is None:
            full_text = soup.get_text(separator=" ")
            for pat, period, mult in [
                (r"総取扱処方箋数[^\d]*(\d{1,3}(?:,\d{3})*|\d{4,})\s*件", "annual", 1.0),
                (r"週\s*平均[^\d]{0,15}(\d{1,4})\s*(?:回|枚)", "weekly", 52.14),
                (r"年間[^\d]{0,15}(\d{1,6}(?:,\d{3})*)\s*(?:回|件)", "annual", 1.0),
            ]:
                m = re.search(pat, full_text, re.DOTALL)
                if m:
                    try:
                        n = int(m.group(1).replace(",", ""))
                        if n > 0:
                            rx_annual = int(n * mult)
                            rx_period = "週平均→年換算" if period == "weekly" else "年間実績"
                            break
                    except (ValueError, OverflowError):
                        pass

        data["prescriptions_annual"] = rx_annual
        data["prescription_period_label"] = rx_period
        return data

    def get_medical_outpatient_data(
        self, facility_name: str, pref_code: str = ""
    ) -> Optional[int]:
        """近隣医療機関の外来患者数を取得（sjk=1）"""
        if not self._initialized:
            self.initialize_session()
        candidates, _, _ = self.search_medical_candidates(facility_name, pref_code)
        if not candidates:
            return None

        # 名称が最も近い候補を選ぶ
        best = None
        for c in candidates[:3]:
            if facility_name[:4] in c.name or c.name[:4] in facility_name:
                best = c
                break
        if best is None and candidates:
            best = candidates[0]

        try:
            url = best.href
            r = self.session.get(url, timeout=12)
            if r.status_code != 200:
                return None
            soup = BeautifulSoup(r.text, "html.parser")
            fields: Dict[str, str] = {}
            for row in soup.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    fields[cells[0].get_text(strip=True)] = cells[1].get_text(strip=True)

            for k, v in fields.items():
                if "外来" in k and ("患者" in k or "数" in k):
                    nums = re.findall(r"[\d,]+", v)
                    if nums:
                        return int(nums[0].replace(",", ""))
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# 4. 方法① 近隣医療機関アプローチ
# ---------------------------------------------------------------------------

class Method1Predictor:
    """
    近隣医療施設の外来患者数から処方箋流入量を予測する。

    ロジック:
      各医療施設について:
        1. 外来患者数 × 診療科別処方箋発行率 = 処方箋発行数
        2. × 院外処方率(0.745) = 院外処方箋数
        3. × 当薬局シェア（距離・競合数に基づく） = 当薬局流入処方箋数
      全施設の合計 × 年間稼働日数 = 年間処方箋受付枚数（推計）
    """

    OUTPATIENT_RX_RATE = NATIONAL_STATS["outpatient_rx_rate"]

    def predict(
        self,
        pharmacy_lat: float,
        pharmacy_lon: float,
        medical_facilities: List[NearbyFacility],
        competing_pharmacies: List[NearbyFacility],
    ) -> PredictionResult:
        breakdown = []
        total_daily_rx = 0.0
        methodology = [
            "### 方法① ロジック: 近隣医療機関アプローチ",
            "",
            "**算出式**:",
            "　各施設の外来患者数 × 診療科別処方箋発行率",
            "　× 院外処方率（0.745）× 当薬局集客シェア",
            "　→ 施設ごとの当薬局への日次流入処方箋数を合計",
            "",
            f"**対象半径**: 500m以内の医療施設 {len(medical_facilities)}件",
            "",
        ]

        for fac in medical_facilities:
            if fac.daily_outpatients == 0:
                continue

            # 院外処方箋発行数/日
            rx_rate, rx_note = SPECIALTY_RX_RATES.get(
                fac.specialty, SPECIALTY_RX_RATES["不明/その他"]
            )
            daily_outpatient_rx = fac.daily_outpatients * rx_rate * self.OUTPATIENT_RX_RATE

            # 院内薬局がある場合は院外処方率を補正
            if fac.has_inhouse_pharmacy:
                daily_outpatient_rx *= 0.6  # 院内処方が増える

            # 当薬局への集客シェア推計
            share, share_reason = self._calc_pharmacy_share(
                fac, pharmacy_lat, pharmacy_lon, competing_pharmacies
            )

            # 門前薬局（他薬局）がいる場合の補正
            if fac.has_gate_pharmacy:
                share *= 0.4
                share_reason += "（既存門前薬局あり → シェア大幅割引）"

            daily_flow = daily_outpatient_rx * share

            breakdown.append({
                "施設名":         fac.name,
                "タイプ":         "病院" if fac.facility_type == "hospital" else "クリニック",
                "距離":           f"{fac.distance_m:.0f}m",
                "診療科":         fac.specialty,
                "外来患者/日":    fac.daily_outpatients,
                "処方箋発行率":   f"{rx_rate:.0%}",
                "院外処方箋/日":  round(daily_outpatient_rx),
                "当薬局シェア":   f"{share:.1%}",
                "シェア根拠":     share_reason,
                "当薬局流入/日":  round(daily_flow),
            })

            total_daily_rx += daily_flow
            methodology.append(
                f"**{fac.name}** ({fac.distance_m:.0f}m): "
                f"外来{fac.daily_outpatients}人/日 × {rx_rate:.0%} × 院外率{self.OUTPATIENT_RX_RATE:.1%} "
                f"× 当薬局{share:.0%} = {daily_flow:.1f}枚/日"
            )

        annual_est = int(total_daily_rx * NATIONAL_STATS["working_days"])
        min_val = int(annual_est * 0.6)
        max_val = int(annual_est * 1.8)

        methodology += [
            "",
            f"**合計**: {total_daily_rx:.1f}枚/日 × {NATIONAL_STATS['working_days']}日 = **{annual_est:,}枚/年**",
            "",
            "**シェア計算の考え方**:",
            "  - 施設から50m以内かつ競合なし: 70-80%（実質的な門前薬局）",
            "  - 施設から200m以内: 30-50%（主要な選択肢）",
            "  - 施設から500m以内: 10-20%（複数の選択肢の一つ）",
            "  - 競合薬局1件ごとにシェアを按分減算",
            "",
            "**注意事項**:",
            "  - 外来患者数は施設規模から推計（OSMタグに基づく）",
            "  - MHLWポータルで実際の外来患者数が取得できた施設は実績値を使用",
            "  - 院内薬局の有無はOSMタグ 'pharmacy=yes' から判定",
        ]

        confidence = "medium" if medical_facilities else "low"
        if not medical_facilities:
            methodology.append("  ⚠ 近隣に医療施設が見つかりませんでした。統計推計値に置き換えます。")
            annual_est = NATIONAL_STATS["median_estimate"]
            min_val = 2_000
            max_val = 20_000

        return PredictionResult(
            method_name="方法①: 近隣医療機関アプローチ",
            annual_rx=annual_est,
            min_val=min_val,
            max_val=max_val,
            confidence=confidence,
            daily_rx=int(total_daily_rx),
            breakdown=breakdown,
            methodology=methodology,
            references=[
                {
                    "name": "厚生労働省「受療行動調査」",
                    "desc": "診療科別処方箋発行率（外来患者1人あたり）の根拠データ",
                    "url": "https://www.mhlw.go.jp/toukei/list/35-34.html",
                },
                {
                    "name": "厚生労働省「薬局・薬剤師機能に関する実態調査」",
                    "desc": f"院外処方率（全国平均 {self.OUTPATIENT_RX_RATE:.1%}）の根拠データ",
                    "url": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iyakuhin/yakkyoku_yakuzaisi/index.html",
                },
                {
                    "name": "OpenStreetMap (Overpass API)",
                    "desc": "近隣医療施設データのソース（名称・位置・タグ情報）",
                    "url": "https://overpass-api.de/",
                },
            ],
        )

    def _calc_pharmacy_share(
        self,
        facility: NearbyFacility,
        pharmacy_lat: float,
        pharmacy_lon: float,
        competing_pharmacies: List[NearbyFacility],
    ) -> Tuple[float, str]:
        """施設から当薬局への処方箋集客シェアを推計"""

        dist = OverpassSearcher._haversine(
            facility.lat, facility.lon, pharmacy_lat, pharmacy_lon
        )

        # 基本シェア（距離ベース）
        if dist <= 50:
            base_share = 0.75
            reason = "50m以内（実質門前）"
        elif dist <= 150:
            base_share = 0.50
            reason = "150m以内（近接立地）"
        elif dist <= 300:
            base_share = 0.30
            reason = "300m以内（徒歩圏）"
        else:
            base_share = 0.15
            reason = "500m以内（自転車圏）"

        # 競合薬局の影響を計算（施設近くの競合ほど影響大）
        competing_near_facility = [
            p for p in competing_pharmacies
            if OverpassSearcher._haversine(facility.lat, facility.lon, p.lat, p.lon) < 300
        ]
        n_competitors = len(competing_near_facility)

        if n_competitors > 0:
            # 競合がいる場合はシェアを按分
            # 距離の逆数で重み付け
            target_weight = 1.0 / max(dist, 10)
            competitor_weights = [
                1.0 / max(
                    OverpassSearcher._haversine(facility.lat, facility.lon, p.lat, p.lon), 10
                )
                for p in competing_near_facility
            ]
            total_weight = target_weight + sum(competitor_weights)
            adjusted_share = base_share * (target_weight / total_weight)
            reason += f"（競合{n_competitors}件で按分）"
        else:
            adjusted_share = base_share
            reason += "（近隣に競合なし）"

        return min(adjusted_share, 0.90), reason


# ---------------------------------------------------------------------------
# 5. 方法② 商圏人口動態アプローチ
# ---------------------------------------------------------------------------

class Method2Predictor:
    """
    商圏内人口 × 性年齢別受診率 × 処方箋発行率 × 薬局市場シェアで予測。

    ロジック:
      1. 商圏人口 = π × 半径² × 人口密度
      2. 年齢層別: 人口 × 受診率/年 × 処方箋発行率 × 院外処方率
      3. 競合薬局との距離重み付きシェアを乗算
      4. → 年間処方箋受付枚数
    """

    def predict(
        self,
        pharmacy_lat: float,
        pharmacy_lon: float,
        competing_pharmacies: List[NearbyFacility],
        area_density: int,
        radius_m: int = 500,
    ) -> PredictionResult:
        # 商圏人口
        area_km2 = math.pi * (radius_m / 1000) ** 2
        total_population = int(area_km2 * area_density)

        # 年齢層別 年間処方箋数
        age_breakdown = []
        total_annual_rx_in_area = 0
        for age_grp, ratio in AGE_DISTRIBUTION.items():
            pop = int(total_population * ratio)
            visit_rate = VISIT_RATE_BY_AGE[age_grp]
            annual_visits = pop * visit_rate
            annual_rx = int(
                annual_visits
                * NATIONAL_STATS["prescription_per_visit"]
                * NATIONAL_STATS["outpatient_rx_rate"]
            )
            age_breakdown.append({
                "年齢層": age_grp,
                "推計人口": f"{pop:,}人",
                "受診率": f"{visit_rate}回/年",
                "年間受診回数": f"{annual_visits:,.0f}回",
                "年間処方箋数": f"{annual_rx:,}枚",
            })
            total_annual_rx_in_area += annual_rx

        # 当薬局の市場シェア（距離重み付き）
        market_share, share_reason = self._calc_market_share(
            pharmacy_lat, pharmacy_lon, competing_pharmacies
        )

        annual_est = int(total_annual_rx_in_area * market_share)
        min_val = int(annual_est * 0.55)
        max_val = int(annual_est * 1.80)

        methodology = [
            "### 方法② ロジック: 商圏人口動態アプローチ",
            "",
            "**算出式**:",
            "　商圏人口 × 年齢層別受診率 × 処方箋発行率(65%)",
            "　× 院外処方率(74.5%) × 当薬局市場シェア",
            "",
            f"**商圏設定**: 半径{radius_m}m（面積: {area_km2:.2f}km²）",
            f"**人口密度**: {area_density:,}人/km²（ユーザー設定）",
            f"**推計商圏人口**: {total_population:,}人",
            "",
            "**年齢層別外来受診率の根拠**:",
            "　（厚生労働省「患者調査」2020年）",
            f"　0-14歳: {VISIT_RATE_BY_AGE['0-14歳']}回/年, "
            f"15-44歳: {VISIT_RATE_BY_AGE['15-44歳']}回/年, "
            f"45-64歳: {VISIT_RATE_BY_AGE['45-64歳']}回/年,",
            f"　65-74歳: {VISIT_RATE_BY_AGE['65-74歳']}回/年, "
            f"75歳以上: {VISIT_RATE_BY_AGE['75歳以上']}回/年",
            "",
            f"**商圏内年間処方箋総数**: {total_annual_rx_in_area:,}枚",
            f"**当薬局推計市場シェア**: {market_share:.1%}",
            f"  根拠: {share_reason}",
            f"**推計年間処方箋枚数**: **{annual_est:,}枚/年**",
        ]

        return PredictionResult(
            method_name="方法②: 商圏人口動態アプローチ",
            annual_rx=annual_est,
            min_val=min_val,
            max_val=max_val,
            confidence="low",
            daily_rx=int(annual_est / NATIONAL_STATS["working_days"]),
            breakdown=age_breakdown,
            methodology=methodology,
            references=[
                {
                    "name": "厚生労働省「患者調査」2020年",
                    "desc": "年齢層別外来受診率（回/年/人）の根拠データ",
                    "url": "https://www.mhlw.go.jp/toukei/saikin/hw/kanja/20/index.html",
                },
                {
                    "name": "総務省「国勢調査」2020年",
                    "desc": "日本の年齢別人口分布（2020年）",
                    "url": "https://www.stat.go.jp/data/kokusei/2020/",
                },
                {
                    "name": "厚生労働省「調剤医療費の動向」2022年度",
                    "desc": f"院外処方率 {NATIONAL_STATS['outpatient_rx_rate']:.1%} の根拠データ",
                    "url": NATIONAL_STATS["source_url"] if "source_url" in NATIONAL_STATS else "https://www.mhlw.go.jp/topics/medias/med/",
                },
            ],
        )

    def _calc_market_share(
        self,
        pharmacy_lat: float,
        pharmacy_lon: float,
        competing_pharmacies: List[NearbyFacility],
    ) -> Tuple[float, str]:
        n = len(competing_pharmacies)
        if n == 0:
            return 0.65, "商圏内に競合薬局なし（独占補正0.65）"

        # 距離の逆数²で重み付けシェアを計算
        target_weight = 1.0  # 当薬局は距離0
        competitor_weights = [
            1.0 / max(p.distance_m, 20) ** 2
            for p in competing_pharmacies
        ]
        total_weight = target_weight + sum(competitor_weights)
        raw_share = target_weight / total_weight

        share = min(raw_share, 0.80)
        return share, f"距離重み付きシェア（競合{n}件）: {share:.1%}"


# ---------------------------------------------------------------------------
# 6. 競合マップ（Folium）
# ---------------------------------------------------------------------------

def build_competitor_map(
    pharmacy_name: str,
    pharmacy_lat: float,
    pharmacy_lon: float,
    medical_facilities: List[NearbyFacility],
    competing_pharmacies: List[NearbyFacility],
    radius_m: int = 500,
) -> folium.Map:
    """近隣施設を表示するFoliumマップを生成"""

    m = folium.Map(
        location=[pharmacy_lat, pharmacy_lon],
        zoom_start=16,
        tiles="OpenStreetMap",
    )

    # 商圏円
    folium.Circle(
        location=[pharmacy_lat, pharmacy_lon],
        radius=radius_m,
        color="#FF4444",
        fill=True,
        fill_opacity=0.05,
        weight=2,
        popup=f"商圏半径 {radius_m}m",
    ).add_to(m)

    # 対象薬局（赤・大マーカー）
    folium.Marker(
        location=[pharmacy_lat, pharmacy_lon],
        popup=folium.Popup(
            f"<b>💊 {pharmacy_name}</b><br>【分析対象薬局】",
            max_width=200,
        ),
        tooltip=f"💊 {pharmacy_name}（分析対象）",
        icon=folium.Icon(color="red", icon="plus-sign", prefix="glyphicon"),
    ).add_to(m)

    # 医療施設
    for fac in medical_facilities:
        color = "blue" if fac.facility_type == "hospital" else "cadetblue"
        icon_name = "h-sign" if fac.facility_type == "hospital" else "user-md"
        ftype_label = "🏥 病院" if fac.facility_type == "hospital" else "🏨 クリニック"
        inhouse_note = "（院内薬局あり）" if fac.has_inhouse_pharmacy else ""
        popup_html = (
            f"<b>{ftype_label} {fac.name}</b>{inhouse_note}<br>"
            f"診療科: {fac.specialty}<br>"
            f"距離: {fac.distance_m:.0f}m<br>"
            f"推計外来: {fac.daily_outpatients}人/日"
        )
        if fac.mhlw_annual_outpatients:
            popup_html += f"<br>MHLW年間外来: {fac.mhlw_annual_outpatients:,}人"

        folium.Marker(
            location=[fac.lat, fac.lon],
            popup=folium.Popup(popup_html, max_width=250),
            tooltip=f"{ftype_label} {fac.name} ({fac.distance_m:.0f}m)",
            icon=folium.Icon(color=color, icon=icon_name, prefix="glyphicon"),
        ).add_to(m)

        # 施設と対象薬局を結ぶ線
        folium.PolyLine(
            [[fac.lat, fac.lon], [pharmacy_lat, pharmacy_lon]],
            color=color, weight=1.5, opacity=0.5,
        ).add_to(m)

    # 競合薬局（緑マーカー）
    for ph in competing_pharmacies:
        is_chain = any(c in ph.name for c in MAJOR_CHAINS)
        chain_note = "（大手チェーン）" if is_chain else ""
        popup_html = (
            f"<b>💊 {ph.name}</b>{chain_note}<br>"
            f"距離: {ph.distance_m:.0f}m"
        )
        folium.Marker(
            location=[ph.lat, ph.lon],
            popup=folium.Popup(popup_html, max_width=200),
            tooltip=f"💊 競合: {ph.name} ({ph.distance_m:.0f}m)",
            icon=folium.Icon(color="green", icon="shopping-cart", prefix="glyphicon"),
        ).add_to(m)

    return m


# ---------------------------------------------------------------------------
# 7. 乖離評価
# ---------------------------------------------------------------------------

def calc_deviation(actual: int, predicted: int) -> Tuple[float, str, str]:
    """実績値と予測値の乖離率を計算"""
    if actual <= 0:
        return 0.0, "N/A", "neutral"
    pct = (predicted - actual) / actual * 100
    label = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
    color = "normal" if abs(pct) < 20 else ("inverse" if abs(pct) < 50 else "off")
    return pct, label, color


# ---------------------------------------------------------------------------
# 8. Streamlit UI
# ---------------------------------------------------------------------------

def render_comparison_banner(analysis: FullAnalysis) -> None:
    """実績値 vs 方法①② の比較バナーを表示"""
    st.markdown("## 📊 予測値 vs 厚労省実績値 比較")

    actual = analysis.mhlw_annual_rx
    m1 = analysis.method1
    m2 = analysis.method2

    cols = st.columns(4)

    # 実績値
    with cols[0]:
        if actual:
            st.metric(
                "🏥 MHLW実績値",
                f"{actual:,} 枚/年",
                help="厚生労働省「薬局機能情報提供制度」の総取扱処方箋数（前年1年間）",
            )
            st.caption("🟢 信頼度: 高（実績値）")
        else:
            st.metric("🏥 MHLW実績値", "記載なし")
            st.caption("⚠ 当該薬局は未報告")

    # 方法①
    with cols[1]:
        if m1:
            st.metric(
                "① 医療機関アプローチ",
                f"{m1.annual_rx:,} 枚/年",
                delta=calc_deviation(actual, m1.annual_rx)[1] if actual else None,
            )
            st.caption(f"レンジ: {m1.min_val:,}〜{m1.max_val:,}")

    # 方法②
    with cols[2]:
        if m2:
            st.metric(
                "② 人口動態アプローチ",
                f"{m2.annual_rx:,} 枚/年",
                delta=calc_deviation(actual, m2.annual_rx)[1] if actual else None,
            )
            st.caption(f"レンジ: {m2.min_val:,}〜{m2.max_val:,}")

    # 乖離サマリー
    with cols[3]:
        if actual and m1 and m2:
            avg_pred = (m1.annual_rx + m2.annual_rx) // 2
            pct, label, _ = calc_deviation(actual, avg_pred)
            st.metric(
                "予測平均 vs 実績",
                f"{avg_pred:,} 枚/年",
                delta=label,
                delta_color="normal" if abs(pct) < 30 else "inverse",
            )
            st.caption("（①と②の単純平均）")


def render_competitor_table(
    medical_facilities: List[NearbyFacility],
    competing_pharmacies: List[NearbyFacility],
) -> None:
    """競合環境テーブルを表示"""
    st.markdown("### 🗺 近隣の医療施設・競合薬局")

    col_med, col_ph = st.columns(2)
    with col_med:
        st.markdown(f"**医療施設（{len(medical_facilities)}件）**")
        if medical_facilities:
            rows = []
            for f in medical_facilities:
                rows.append({
                    "施設名": f.name,
                    "種別": "病院" if f.facility_type == "hospital" else "クリニック",
                    "距離": f"{f.distance_m:.0f}m",
                    "診療科": f.specialty,
                    "外来/日(推計)": f"{f.daily_outpatients}人",
                    "院内薬局": "あり" if f.has_inhouse_pharmacy else "なし",
                })
            import pandas as pd
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("近隣500m以内に医療施設が見つかりませんでした")

    with col_ph:
        st.markdown(f"**競合薬局（{len(competing_pharmacies)}件）**")
        if competing_pharmacies:
            rows = []
            for p in competing_pharmacies:
                rows.append({
                    "薬局名": p.name,
                    "距離": f"{p.distance_m:.0f}m",
                    "チェーン": "はい" if any(c in p.name for c in MAJOR_CHAINS) else "独立",
                })
            import pandas as pd
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("近隣500m以内に競合薬局が見つかりませんでした")


def main() -> None:
    st.set_page_config(
        page_title="薬局 処方箋枚数 多面的予測 v2",
        page_icon="🔬",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    st.title("🔬 薬局 年間処方箋枚数 多面的予測ツール v2.0")
    st.markdown(
        "厚生労働省の実績データと、**近隣医療機関アプローチ（方法①）**・"
        "**商圏人口動態アプローチ（方法②）** の2種類の予測を並列表示し、"
        "実績値との乖離を評価します。競合マップも提供します。"
    )

    # session_state 初期化
    for k, v in [
        ("candidates", []),
        ("selected_idx", 0),
        ("analysis", None),
        ("search_done", False),
    ]:
        if k not in st.session_state:
            st.session_state[k] = v

    # ==============================================================
    # STEP 1: 薬局検索
    # ==============================================================
    st.markdown("### STEP 1 — 薬局名で検索（厚生労働省ポータル）")

    col_kw, col_pref = st.columns([3, 1])
    with col_kw:
        keyword = st.text_input(
            "薬局名（一部でも可）",
            placeholder="例: アイセイ薬局 武蔵小杉 / 日本調剤 新宿",
            key="v2_keyword",
        )
    with col_pref:
        prefecture = st.selectbox("都道府県（任意）", ["（指定なし）"] + PREFECTURES, key="v2_pref")

    if st.button("🔍 候補を検索", type="primary"):
        st.session_state["analysis"] = None
        st.session_state["search_done"] = False
        pref_code = PREFECTURE_CODES.get(prefecture, "")
        with st.spinner("MHLWポータルを検索中…"):
            scraper = MHLWScraper()
            candidates, total, status = scraper.search_pharmacy_candidates(keyword.strip(), pref_code)
        st.session_state["candidates"] = candidates
        if candidates:
            st.success(f"✅ {status}（全{total}件）")
        else:
            st.warning("候補が見つかりませんでした")

    # ==============================================================
    # STEP 2: 候補選択 + 分析パラメータ設定
    # ==============================================================
    candidates: List[PharmacyCandidate] = st.session_state.get("candidates", [])
    analysis: Optional[FullAnalysis] = st.session_state.get("analysis")

    if candidates and analysis is None:
        st.markdown("---")
        st.markdown("### STEP 2 — 薬局選択と分析パラメータ設定")

        options = [
            f"{c.name}　{'（' + c.address[:35] + '）' if c.address else ''}"
            for c in candidates
        ]
        sel_label = st.selectbox("候補一覧", options, key="v2_candidate_select")
        sel_idx = options.index(sel_label)
        sel_candidate = candidates[sel_idx]
        st.caption(f"📍 住所: {sel_candidate.address or '不明'}")

        st.markdown("**分析パラメータ（方法②の商圏設定）**")
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            area_type = st.selectbox(
                "商圏の人口密度",
                list(AREA_DENSITY_OPTIONS.keys()),
                index=2,
                help="薬局周辺の市街地タイプを選択してください",
            )
        with col_b:
            radius = st.slider("商圏半径（m）", 200, 1000, 500, step=100)
        with col_c:
            try_mhlw_medical = st.checkbox(
                "近隣医療施設のMHLWデータ照会", value=False,
                help="ONにすると精度が上がりますが、検索に時間がかかります（各施設ごとにMHLWに問い合わせ）",
            )

        if st.button("🚀 多面的分析を実行", type="primary", use_container_width=True):
            run_analysis(sel_candidate, area_type, radius, try_mhlw_medical)

    # ==============================================================
    # STEP 3: 結果表示
    # ==============================================================
    if analysis:
        st.markdown("---")
        st.markdown(f"## 結果: `{analysis.pharmacy_name}`")
        st.caption(f"住所: {analysis.pharmacy_address}")

        # 比較バナー
        render_comparison_banner(analysis)
        st.markdown("---")

        # メインタブ
        tab_map, tab_m1, tab_m2, tab_mhlw, tab_log = st.tabs([
            "🗺 競合マップ",
            "① 医療機関アプローチ",
            "② 人口動態アプローチ",
            "🏥 厚労省データ",
            "🔍 検索ログ",
        ])

        with tab_map:
            if analysis.pharmacy_lat and analysis.pharmacy_lon:
                st.markdown("**凡例**: 🔴 分析対象薬局　🔵 病院　🔷 クリニック　🟢 競合薬局")
                folium_map = build_competitor_map(
                    analysis.pharmacy_name,
                    analysis.pharmacy_lat,
                    analysis.pharmacy_lon,
                    analysis.nearby_medical,
                    analysis.nearby_pharmacies,
                )
                st_folium(folium_map, width=None, height=500, use_container_width=True)
                render_competitor_table(analysis.nearby_medical, analysis.nearby_pharmacies)
            else:
                st.warning("住所から座標を取得できなかったため、マップを表示できません")
                render_competitor_table(analysis.nearby_medical, analysis.nearby_pharmacies)

        with tab_m1:
            if analysis.method1:
                m1 = analysis.method1
                st.metric("年間推計処方箋枚数（方法①）", f"{m1.annual_rx:,} 枚/年")
                st.caption(f"レンジ: {m1.min_val:,}〜{m1.max_val:,}枚/年 | 1日換算: {m1.daily_rx}枚/日")
                if m1.breakdown:
                    st.markdown("#### 施設別 処方箋流入内訳")
                    import pandas as pd
                    df = pd.DataFrame(m1.breakdown)
                    st.dataframe(df, use_container_width=True, hide_index=True)
                st.markdown("#### 推計ロジック")
                for line in m1.methodology:
                    st.markdown(line)
                st.markdown("#### 参照ソース")
                for ref in m1.references:
                    with st.expander(ref["name"]):
                        st.write(ref.get("desc", ""))
                        if ref.get("url"):
                            st.markdown(f"🔗 [{ref['url']}]({ref['url']})")
            else:
                st.info("方法①の分析データがありません")

        with tab_m2:
            if analysis.method2:
                m2 = analysis.method2
                st.metric("年間推計処方箋枚数（方法②）", f"{m2.annual_rx:,} 枚/年")
                st.caption(f"レンジ: {m2.min_val:,}〜{m2.max_val:,}枚/年 | 1日換算: {m2.daily_rx}枚/日")
                if m2.breakdown:
                    st.markdown("#### 年齢層別 処方箋数内訳")
                    import pandas as pd
                    df = pd.DataFrame(m2.breakdown)
                    st.dataframe(df, use_container_width=True, hide_index=True)
                st.markdown("#### 推計ロジック")
                for line in m2.methodology:
                    st.markdown(line)
                st.markdown("#### 参照ソース")
                for ref in m2.references:
                    with st.expander(ref["name"]):
                        st.write(ref.get("desc", ""))
                        if ref.get("url"):
                            st.markdown(f"🔗 [{ref['url']}]({ref['url']})")
            else:
                st.info("方法②の分析データがありません")

        with tab_mhlw:
            if analysis.mhlw_annual_rx:
                st.success(f"✅ 厚労省実績値: **{analysis.mhlw_annual_rx:,}枚/年**")
                st.markdown(f"🔗 [MHLWポータルで確認]({analysis.mhlw_source_url})")
            else:
                st.warning("厚労省ポータルに処方箋枚数の記載がありませんでした（未報告）")
                if analysis.mhlw_source_url:
                    st.markdown(f"🔗 [MHLWポータルで確認]({analysis.mhlw_source_url})")

        with tab_log:
            st.code("\n".join(analysis.search_log))

    # 初期画面
    if not candidates and analysis is None:
        st.markdown("---")
        col1, col2, col3 = st.columns(3)
        col1.markdown(
            "**① 厚労省実データ取得**\n\n"
            "薬局機能情報提供制度から\n「総取扱処方箋数」を直接取得"
        )
        col2.markdown(
            "**② 近隣医療機関アプローチ**\n\n"
            "OSMで近隣クリニック・病院を検索し\n診療科別処方箋発行率から予測"
        )
        col3.markdown(
            "**③ 商圏人口動態アプローチ**\n\n"
            "商圏人口 × 年齢別受診率 ×\n処方箋発行率 × 市場シェアで予測"
        )


def run_analysis(
    candidate: PharmacyCandidate,
    area_type: str,
    radius: int,
    try_mhlw_medical: bool,
) -> None:
    """フル分析を実行して session_state に保存"""
    log = []
    progress = st.progress(0, text="分析開始…")

    # ── STEP A: MHLWから薬局詳細を取得
    progress.progress(10, text="[1/5] MHLW: 薬局詳細データを取得中…")
    scraper = MHLWScraper()
    scraper.initialize_session()
    detail, detail_msg = scraper.get_pharmacy_detail(candidate)
    log.append(f"[MHLW 薬局詳細] {detail_msg}")

    mhlw_annual_rx = None
    pharmacy_address = candidate.address
    mhlw_source_url = candidate.href

    if detail:
        mhlw_annual_rx = detail.get("prescriptions_annual")
        if detail.get("address"):
            pharmacy_address = detail["address"]
        mhlw_source_url = detail.get("source_url", candidate.href)
        log.append(f"  処方箋数: {mhlw_annual_rx}枚/年" if mhlw_annual_rx else "  処方箋数: 記載なし")

    # ── STEP B: ジオコーディング
    progress.progress(25, text="[2/5] 住所を座標に変換中（Nominatim）…")
    geocoder = GeocoderService()
    lat, lon, geo_msg = geocoder.geocode(pharmacy_address)
    log.append(f"[Geocoding] {geo_msg}")
    if lat:
        log.append(f"  座標: ({lat:.5f}, {lon:.5f})")
    else:
        log.append("  ⚠ 座標取得失敗 → 空間分析をスキップ")

    # ── STEP C: 近隣施設検索（OSM）
    nearby_medical: List[NearbyFacility] = []
    nearby_pharmacies: List[NearbyFacility] = []

    if lat and lon:
        progress.progress(45, text="[3/5] 近隣施設をOSMから検索中…")
        time.sleep(1)  # Nominatim利用規約: 1秒以上の間隔
        overpass = OverpassSearcher()
        nearby_medical, nearby_pharmacies, osm_msg = overpass.search_nearby(lat, lon, radius)
        log.append(f"[OSM Overpass] {osm_msg}")

        # MHLWで各医療施設の外来患者数を取得（オプション）
        if try_mhlw_medical and nearby_medical:
            progress.progress(55, text="[3.5/5] 近隣医療施設のMHLWデータを照会中…")
            for i, fac in enumerate(nearby_medical[:5]):  # 最大5施設
                annual_op = scraper.get_medical_outpatient_data(fac.name)
                if annual_op:
                    fac.mhlw_annual_outpatients = annual_op
                    fac.daily_outpatients = annual_op // NATIONAL_STATS["working_days"]
                    log.append(f"  [MHLW医療機関] {fac.name}: 年間外来{annual_op:,}人")
                time.sleep(0.5)

    # ── STEP D: 方法①予測
    progress.progress(70, text="[4/5] 方法①: 近隣医療機関アプローチで予測中…")
    m1_predictor = Method1Predictor()
    method1 = m1_predictor.predict(
        lat or 0.0, lon or 0.0, nearby_medical, nearby_pharmacies
    ) if lat else None
    log.append(f"[方法①] 推計: {method1.annual_rx:,}枚/年" if method1 else "[方法①] スキップ（座標なし）")

    # ── STEP E: 方法②予測
    progress.progress(85, text="[5/5] 方法②: 商圏人口動態アプローチで予測中…")
    area_density = AREA_DENSITY_OPTIONS[area_type]
    m2_predictor = Method2Predictor()
    method2 = m2_predictor.predict(
        lat or 0.0, lon or 0.0, nearby_pharmacies, area_density, radius
    )
    log.append(f"[方法②] 推計: {method2.annual_rx:,}枚/年")

    progress.progress(100, text="完了！")
    progress.empty()

    # 結果を session_state に保存
    st.session_state["analysis"] = FullAnalysis(
        pharmacy_name=candidate.name,
        pharmacy_address=pharmacy_address,
        pharmacy_lat=lat or 0.0,
        pharmacy_lon=lon or 0.0,
        mhlw_annual_rx=mhlw_annual_rx,
        mhlw_source_url=mhlw_source_url,
        method1=method1,
        method2=method2,
        nearby_medical=nearby_medical,
        nearby_pharmacies=nearby_pharmacies,
        search_log=log,
    )
    st.session_state["search_done"] = True
    st.rerun()


if __name__ == "__main__":
    main()
