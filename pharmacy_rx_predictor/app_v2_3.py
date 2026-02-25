"""
薬局 年間処方箋枚数 多面的予測ツール v2.3
==========================================
v2.2 からの改善点:
  1. 方法②（商圏人口動態アプローチ）市場シェア計算バグ修正 ← 主要修正
     - [修正前の問題] 1/d² 重みを使っていたため、競合薬局の重みが常に極小
       （例: 150m先の競合 → 1/150² = 0.000044）となり自社重み(1.0)に対して
       無視できる大きさだった。結果、競合が何件あってもシェア≈80%（上限）に
       張り付き、面での集客シナリオが常に大幅な上ぶれを起こしていた。
     - [修正後] 距離帯別「実効競合数モデル」に変更:
         ・近接(≤200m) : 実効重み 1.5（強い競争圧力）
         ・中距離(≤500m): 実効重み 1.0（標準競合）
         ・遠距離(>500m) : 実効重み 0.5（弱い競争圧力）
         実効競合数 = Σ重み → 自社シェア = 1 / (実効競合数 + 1)
         上限: 55%（競合ゼロでも患者流出を考慮）/ 下限: 8%（最悪ケース）
     - これにより、競合密度に応じて適切に自社シェアが低下する。
  2. 年間受診率データの検証と根拠注釈の追加
     - VISIT_RATE_BY_AGE の値（9.8〜22.1回/年）は患者調査2020年の
       外来受療率（1日あたり/10万人）× 365日で導出した正当な数値であり、
       OECD統計（日本: 約12.6回/人/年）とも整合している。
     - 歯科受診（全外来の約10%）も含むが、処方箋発行率65%は
       歯科の低い処方率（約20-25%）を加重平均した結果として整合的。

v2.2 の内容（前バージョン）:
  1. 国土地理院（GSI）ジオコーダーを第一優先に採用
  2. 統計数値のデータソース表示
  3. 新規開局予測モード（スーパー内調剤薬局など）
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
    "working_days": 305,
    "outpatient_rx_rate": 0.745,    # 院外処方率（全国平均）
    "prescription_per_visit": 0.65,  # 外来1受診あたり処方箋発行率
}

# 統計数値のデータソース参照（v2.2新規追加）
STAT_REFERENCES: Dict[str, Dict] = {
    "院外処方率": {
        "value": "74.5%",
        "source": "厚生労働省「調剤医療費の動向」2022年度",
        "url": "https://www.mhlw.go.jp/topics/medias/med/",
        "note": "全国薬局における院外処方箋（調剤薬局で調剤）の割合。2000年代から一貫して上昇し2022年に74.5%。病院規模・地域により差異あり。",
    },
    "処方箋発行率（外来1受診あたり）": {
        "value": "65%",
        "source": "厚生労働省「受療行動調査」2020年・「薬局・薬剤師機能に関する実態調査」複数年度より推計",
        "url": "https://www.mhlw.go.jp/toukei/list/35-34.html",
        "note": "外来患者1回の受診あたり処方箋が発行される確率。診療科・疾患により大きく異なる（精神科~90%、眼科~52%）。加重平均で65%を採用。",
    },
    "年間稼働日数": {
        "value": "305日",
        "source": "厚生労働省「薬局・薬剤師実態調査」業界慣行",
        "url": "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iyakuhin/yakkyoku_yakuzaisi/index.html",
        "note": "土日・祝日・年末年始（約7日）を除く年間稼働日数。薬局の規模や立地により240〜320日程度の幅がある。",
    },
    "年齢層別外来受診率": {
        "value": "0-14歳: 9.8回/年, 15-44歳: 7.2回/年, 45-64歳: 11.3回/年, 65-74歳: 19.2回/年, 75歳以上: 22.1回/年",
        "source": "厚生労働省「患者調査」2020年（3年ごと実施）",
        "url": "https://www.mhlw.go.jp/toukei/saikin/hw/kanja/20/index.html",
        "note": (
            "患者調査の外来受療率（1日あたり外来患者数/10万人）× 365日で導出した年間受診回数。"
            "加重平均は約12.4回/人/年で、OECD統計（日本: 12.6回/人/年・2019年）と整合。"
            "歯科受診（全外来の約10%）を含むが、処方箋発行率65%は歯科の低い処方率（約20-25%）を"
            "加重平均した結果として整合的。75歳以上は慢性疾患による継続受診が多く最も高い。"
        ),
    },
    "日本の年齢分布": {
        "value": "0-14歳: 11.9%, 15-44歳: 34.2%, 45-64歳: 25.6%, 65-74歳: 14.5%, 75歳以上: 13.8%",
        "source": "総務省「国勢調査」2020年",
        "url": "https://www.stat.go.jp/data/kokusei/2020/",
        "note": "5年ごとの全数調査。少子高齢化により75歳以上の割合が上昇中（2020年: 13.8% → 2025年推計: 16%超）。",
    },
    "診療科別処方箋発行率": {
        "value": "循環器88%, 糖尿病90%, 精神科85%, 内科76%, 整形外科72%, 皮膚科64%, 小児科62%, 眼科52%",
        "source": "厚生労働省「受療行動調査」「社会医療診療行為別統計」複数年度の分析より推計",
        "url": "https://www.mhlw.go.jp/toukei/list/35-34.html",
        "note": "慢性疾患が多い科（循環器・糖尿病・精神科）は処方率が高く、処置・手術中心の科（眼科・リハビリ）は低い。いずれも院外処方率(74.5%)をさらに乗じる。",
    },
    "人口密度データ": {
        "value": "東京都千代田区: 4,073人/km²〜豊島区: 22,449人/km²（区ごとに異なる）",
        "source": "総務省「国勢調査」2020年（e-Stat掲載の市区町村別人口密度）",
        "url": "https://www.e-stat.go.jp/stat-search/files?page=1&toukei=00200521",
        "note": "ツールでは東京23区・大阪24区（区別）、政令指定都市・主要市（市別）、その他（都道府県別）の3段階で密度を自動判定。",
    },
    "全国薬局統計": {
        "value": "薬局数: 61,860軒, 年間処方箋受付総数: 8.85億枚, 1薬局あたり年間平均: 14,305枚",
        "source": "厚生労働省「調剤医療費の動向」2022年度",
        "url": "https://www.mhlw.go.jp/topics/medias/med/",
        "note": "薬局数・処方箋数は増加傾向。1薬局あたり平均14,305枚だが中央値は約8,000枚と推計（規模格差が大きい）。",
    },
}

# 診療科別 処方箋発行率
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

VISIT_RATE_BY_AGE: Dict[str, float] = {
    "0-14歳":  9.8,
    "15-44歳": 7.2,
    "45-64歳": 11.3,
    "65-74歳": 19.2,
    "75歳以上": 22.1,
}

AGE_DISTRIBUTION: Dict[str, float] = {
    "0-14歳":  0.119,
    "15-44歳": 0.342,
    "45-64歳": 0.256,
    "65-74歳": 0.145,
    "75歳以上": 0.138,
}

MAJOR_CHAINS = [
    "ウエルシア", "ツルハ", "マツモトキヨシ", "マツキヨ", "スギ薬局",
    "コスモス薬品", "クリエイト", "サンドラッグ", "カワチ薬品",
    "日本調剤", "クオール", "アイン", "ファーマライズ", "総合メディカル",
]

# ---------------------------------------------------------------------------
# 人口密度ルックアップテーブル（2020年国勢調査）
# ---------------------------------------------------------------------------

TOKYO_WARD_DENSITY: Dict[str, int] = {
    "千代田区":  4073, "中央区":  13762, "港区":    10649, "新宿区":  18235,
    "文京区":   20105, "台東区":  19419, "墨田区":  19508, "江東区":  13943,
    "品川区":   17617, "目黒区":  18984, "大田区":  12461, "世田谷区": 16006,
    "渋谷区":   15608, "中野区":  20539, "杉並区":  16524, "豊島区":  22449,
    "北区":     17974, "荒川区":  21222, "板橋区":  17598, "練馬区":  14587,
    "足立区":   13752, "葛飾区":  13802, "江戸川区": 13329,
}

OSAKA_WARD_DENSITY: Dict[str, int] = {
    "都島区":  13500, "福島区":  11000, "此花区":   6700, "西区":   12500,
    "港区":    12000, "大正区":  10500, "天王寺区": 15500, "浪速区":  15000,
    "西淀川区":  9500, "東淀川区": 17000, "東成区":  19000, "生野区":  18000,
    "旭区":    15000, "城東区":  18000, "阿倍野区": 15500, "住吉区":  14500,
    "東住吉区": 15500, "西成区":  18000, "淀川区":  15500, "鶴見区":  12000,
    "住之江区":  8500, "平野区":  13500, "北区":     9500, "中央区":   7000,
}

CITY_DENSITY: Dict[str, int] = {
    "札幌市":    1882, "仙台市":    1510, "さいたま市": 5527, "千葉市":    3625,
    "横浜市":    8717, "川崎市":   10235, "相模原市":   2716,
    "新潟市":    1100, "静岡市":     496, "浜松市":      537,
    "名古屋市":  7138, "京都市":    2804, "大阪市":    12110, "堺市":     5219,
    "神戸市":    2799, "岡山市":     942, "広島市":     1625, "北九州市":  1994,
    "福岡市":    4990, "熊本市":    1891,
    "旭川市":     454, "函館市":     566, "青森市":      753, "盛岡市":     757,
    "秋田市":     629, "山形市":     844, "福島市":      619, "郡山市":     768,
    "いわき市":   268, "水戸市":    2122, "宇都宮市":   1255, "前橋市":     966,
    "高崎市":    1062, "川越市":    3017, "船橋市":     7068, "柏市":      4022,
    "八王子市":  2584, "府中市":    7029, "調布市":     8225, "町田市":    4965,
    "藤沢市":    5046, "横須賀市":  3665, "長野市":      648, "岐阜市":    2098,
    "豊橋市":    2031, "豊田市":     989, "岡崎市":     1575, "一宮市":    3030,
    "大津市":    1070, "吹田市":   10267, "高槻市":     4898, "東大阪市":  9267,
    "姫路市":    1150, "尼崎市":    8116, "西宮市":     3796, "奈良市":    1087,
    "和歌山市":  2310, "倉敷市":     849, "福山市":      953, "呉市":       786,
    "下関市":     552, "高松市":    1583, "松山市":     1140, "高知市":    1106,
    "久留米市":  2045, "長崎市":    1641, "佐世保市":    638, "大分市":     861,
    "宮崎市":     849, "鹿児島市":  1439, "那覇市":     8356,
    "川口市":    7230, "越谷市":    5630, "草加市":     8270, "春日部市":  2810,
    "松戸市":    6230, "市川市":    6610, "浦安市":    10490, "市原市":    1090,
    "所沢市":    3640, "平塚市":    3490, "厚木市":     2070, "大和市":    6610,
}

PREFECTURE_DENSITY: Dict[str, int] = {
    "北海道":  64, "青森県": 130, "岩手県":  84, "宮城県": 321, "秋田県":  86,
    "山形県": 116, "福島県": 139, "茨城県": 476, "栃木県": 307, "群馬県": 309,
    "埼玉県": 1927, "千葉県": 1211, "東京都": 6263, "神奈川県": 3810,
    "新潟県": 179, "富山県": 247, "石川県": 277, "福井県": 189, "山梨県": 185,
    "長野県": 155, "岐阜県": 191, "静岡県": 469, "愛知県": 1457, "三重県": 309,
    "滋賀県": 351, "京都府": 566, "大阪府": 4631, "兵庫県": 652, "奈良県": 366,
    "和歌山県": 196, "鳥取県": 162, "島根県": 103, "岡山県": 270, "広島県": 336,
    "山口県": 224, "徳島県": 184, "香川県": 519, "愛媛県": 241, "高知県": 102,
    "福岡県": 1023, "佐賀県": 340, "長崎県": 330, "熊本県": 238, "大分県": 182,
    "宮崎県": 141, "鹿児島県": 179, "沖縄県": 637,
}

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
    facility_type: str
    lat: float
    lon: float
    distance_m: float
    specialty: str = "不明/その他"
    daily_outpatients: int = 0
    beds: int = 0
    has_inhouse_pharmacy: bool = False
    has_gate_pharmacy: bool = False
    osm_tags: Dict = field(default_factory=dict)
    mhlw_annual_outpatients: Optional[int] = None  # 処方箋枚数（薬局）or 年間外来数（医療機関）

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
    """既存薬局分析モード用"""
    pharmacy_name: str
    pharmacy_address: str
    pharmacy_lat: float
    pharmacy_lon: float
    geocode_display: str
    geocoder_source: str
    mhlw_annual_rx: Optional[int]
    mhlw_source_url: str
    method1: Optional[PredictionResult]
    method2: Optional[PredictionResult]
    nearby_medical: List[NearbyFacility]
    nearby_pharmacies: List[NearbyFacility]
    area_density: int = 3000
    area_density_source: str = ""
    commercial_radius: int = 500
    commercial_radius_reason: str = ""
    is_gate_pharmacy: bool = False
    gate_pharmacy_reason: str = ""
    search_log: List[str] = field(default_factory=list)

@dataclass
class NewPharmacyConfig:
    """新規開局予測モード用設定"""
    address: str
    pharmacy_name: str = "開局予定薬局"
    scenario: str = "both"          # "gate_clinic" | "catchment" | "both"
    gate_specialty: str = "一般内科"
    gate_daily_outpatients: int = 50
    gate_has_inhouse: bool = False   # 誘致クリニックが院内薬局を持つか
    fetch_nearby_rx: bool = False    # 近隣薬局のMHLWデータを取得するか

@dataclass
class NewPharmacyResult:
    """新規開局予測モード用結果"""
    config: NewPharmacyConfig
    lat: Optional[float]
    lon: Optional[float]
    geocode_display: str
    geocoder_source: str
    area_density: int
    area_density_source: str
    commercial_radius: int
    commercial_radius_reason: str
    is_gate: bool
    gate_reason: str
    nearby_medical: List[NearbyFacility]
    nearby_pharmacies: List[NearbyFacility]
    method1: Optional[PredictionResult]   # 門前クリニックシナリオ
    method2: Optional[PredictionResult]   # 商圏人口シナリオ
    search_log: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. 人口密度・商圏半径 自動計算
# ---------------------------------------------------------------------------

def get_population_density(address: str) -> Tuple[int, str]:
    if not address:
        return 3000, "住所不明（中高密度デフォルト値）"
    addr = address.strip()
    if "東京都" in addr:
        for ward, density in TOKYO_WARD_DENSITY.items():
            if ward in addr:
                return density, f"{ward}（東京都） 2020年国勢調査"
        return 6263, "東京都平均（区未特定） 2020年国勢調査"
    if "大阪市" in addr:
        for ward, density in OSAKA_WARD_DENSITY.items():
            if ward in addr:
                return density, f"大阪市{ward} 2020年国勢調査"
        return 12110, "大阪市平均（区未特定） 2020年国勢調査"
    for city, density in CITY_DENSITY.items():
        if city in addr:
            return density, f"{city} 平均人口密度 2020年国勢調査"
    for pref, density in PREFECTURE_DENSITY.items():
        if pref in addr:
            return density, f"{pref} 平均人口密度 2020年国勢調査"
    return 1500, "住所解析不能（中密度デフォルト値 1,500人/km²）"


def detect_gate_pharmacy(
    pharmacy_name: str,
    nearby_medical: List[NearbyFacility],
) -> Tuple[bool, str]:
    gate_keywords = ["門前", "病院前", "医院前", "クリニック前", "院前"]
    for kw in gate_keywords:
        if kw in pharmacy_name:
            return True, f"薬局名に「{kw}」が含まれる"
    if nearby_medical:
        for fac in sorted(nearby_medical, key=lambda f: f.distance_m):
            if fac.distance_m <= 80:
                return True, f"「{fac.name}」({fac.distance_m:.0f}m)に隣接"
        for fac in nearby_medical[:5]:
            short = fac.name[:4]
            if len(short) >= 4 and short in pharmacy_name:
                return True, f"「{fac.name}」の名称が薬局名に含まれる"
    return False, "通常商圏型"


def calc_commercial_radius(
    density: int,
    is_gate: bool = False,
    gate_reason: str = "",
) -> Tuple[int, str]:
    if is_gate:
        return 300, f"門前薬局（{gate_reason}）→ 医療機関依存型のため300m固定"
    if density >= 12_000:
        r, note = 300,  "超高密度地域（12,000人/km²以上）徒歩5分圏"
    elif density >= 6_000:
        r, note = 400,  "高密度地域（6,000〜12,000人/km²）徒歩7分圏"
    elif density >= 3_000:
        r, note = 500,  "中高密度地域（3,000〜6,000人/km²）徒歩8分圏"
    elif density >= 1_500:
        r, note = 700,  "中密度地域（1,500〜3,000人/km²）徒歩12分圏"
    elif density >= 500:
        r, note = 1000, "低密度地域（500〜1,500人/km²）徒歩・自転車圏"
    else:
        r, note = 2000, "超低密度地域（500人/km²未満）広域商圏"
    return r, f"{note}（密度: {density:,}人/km²）"


# ---------------------------------------------------------------------------
# 2. ジオコーダー（国土地理院 GSI + Nominatim フォールバック）
# ---------------------------------------------------------------------------

class GeocoderService:
    """
    住所 → 緯度経度変換

    v2.2 変更:
      [1st] 国土地理院（GSI）ジオコーダー — 日本住所に特化・高精度・APIキー不要
            https://msearch.gsi.go.jp/address-search/AddressSearch
      [2nd] Nominatim (OpenStreetMap) — 国際的な地名・施設名に強い
    """

    GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
    NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
    LAT_MIN, LAT_MAX = 24.0, 46.0
    LON_MIN, LON_MAX = 122.0, 154.0

    def _clean(self, address: str) -> str:
        a = re.sub(r"Googleマップ.*|Google Map.*", "", address).strip()
        trans = str.maketrans("０１２３４５６７８９－", "0123456789-")
        a = a.translate(trans).replace("　", " ")
        a = re.sub(r"〒\s*\d{3}[-−]\d{4}\s*", "", a).strip()
        return re.sub(r"\s+", " ", a).strip()

    def _is_japan(self, lat: float, lon: float) -> bool:
        return self.LAT_MIN <= lat <= self.LAT_MAX and self.LON_MIN <= lon <= self.LON_MAX

    def _build_variants(self, address: str) -> List[str]:
        variants: List[str] = [address]
        # 建物名・号室除去
        short = re.sub(r"\d+(?:階|F|棟|号室|号).*$", "", address).strip()
        if short and short != address:
            variants.append(short)
        # スペース分割で短縮
        parts = address.split()
        for n in [4, 3]:
            if len(parts) > n:
                v = " ".join(parts[:n])
                if v not in variants:
                    variants.append(v)
        # 都道府県+市区のみ
        m = re.match(r"((?:東京都|大阪府|京都府|北海道)|.+?[都道府県])(.+?[市区町村])", address)
        if m:
            v = m.group(1) + m.group(2)
            if v not in variants:
                variants.append(v)
        return variants[:6]

    def _try_gsi(self, query: str) -> Optional[Tuple[float, float, str]]:
        headers = {"User-Agent": "PharmacyRxPredictor"}
        try:
            r = requests.get(self.GSI_URL, params={"q": query}, headers=headers, timeout=8)
            if r.status_code == 200:
                data = r.json()
                if data:
                    coords = data[0].get("geometry", {}).get("coordinates", [])
                    if len(coords) == 2:
                        lon, lat = float(coords[0]), float(coords[1])
                        if self._is_japan(lat, lon):
                            title = data[0].get("properties", {}).get("title", query)
                            return lat, lon, title
        except Exception:
            pass
        return None

    def _try_nominatim(self, query: str) -> Optional[Tuple[float, float, str]]:
        headers = {"User-Agent": "PharmacyRxPredictor"}
        try:
            r = requests.get(
                self.NOMINATIM_URL,
                params={"q": query + " 日本", "format": "json", "limit": 1},
                headers=headers, timeout=10,
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
                    if self._is_japan(lat, lon):
                        return lat, lon, data[0].get("display_name", query)
        except Exception:
            pass
        return None

    def geocode(self, address: str) -> Tuple[Optional[float], Optional[float], str, str]:
        """
        Returns: (lat, lon, status_message, geocoder_source_name)
        """
        if not address:
            return None, None, "住所が空です", ""
        clean = self._clean(address)
        variants = self._build_variants(clean)

        # ── GSI Japan を優先
        for i, v in enumerate(variants):
            if i > 0:
                time.sleep(0.15)
            result = self._try_gsi(v)
            if result:
                lat, lon, title = result
                note = f"（短縮クエリ: {v}）" if i > 0 else ""
                return lat, lon, f"緯度: {lat:.5f}, 経度: {lon:.5f} [{title}]{note}", "国土地理院（GSI）"

        # ── Nominatim フォールバック
        time.sleep(1.1)
        for i, v in enumerate(variants):
            if i > 0:
                time.sleep(1.1)
            result = self._try_nominatim(v)
            if result:
                lat, lon, display = result
                note = f"（短縮クエリ: {v}）" if i > 0 else ""
                return lat, lon, f"緯度: {lat:.5f}, 経度: {lon:.5f}{note}", "Nominatim(OpenStreetMap)"

        return None, None, f"座標取得失敗（試行済: {len(variants)}バリアント）: {clean[:40]}", ""


# ---------------------------------------------------------------------------
# 3. 近隣施設検索（Overpass API）
# ---------------------------------------------------------------------------

class OverpassSearcher:
    URL = "https://overpass-api.de/api/interpreter"

    def search_nearby(
        self, lat: float, lon: float, radius: int = 600
    ) -> Tuple[List[NearbyFacility], List[NearbyFacility], str]:
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
            return [], [], "Overpass APIタイムアウト"
        except Exception as e:
            return [], [], f"Overpass APIエラー: {e}"

        medical, pharmacies = [], []
        for elem in data.get("elements", []):
            tags = elem.get("tags", {})
            name = tags.get("name", tags.get("name:ja", ""))
            if not name:
                continue
            if elem["type"] == "node":
                e_lat, e_lon = elem.get("lat", 0), elem.get("lon", 0)
            else:
                c = elem.get("center", {})
                e_lat, e_lon = c.get("lat", 0), c.get("lon", 0)
            if not (e_lat and e_lon):
                continue
            dist = self._haversine(lat, lon, e_lat, e_lon)
            if tags.get("amenity") == "pharmacy":
                pharmacies.append(NearbyFacility(
                    name=name, facility_type="pharmacy",
                    lat=e_lat, lon=e_lon, distance_m=dist, osm_tags=tags,
                ))
                continue
            ftype = "hospital" if (
                tags.get("amenity") == "hospital" or
                tags.get("healthcare") == "hospital" or
                int(tags.get("beds", 0) or 0) >= 20
            ) else "clinic"
            sp_raw = tags.get("healthcare:speciality", tags.get("specialty", ""))
            specialty = OSM_SPECIALTY_MAP.get(sp_raw.lower(), "一般内科") if sp_raw else "一般内科"
            has_inhouse = tags.get("pharmacy", "") in ["yes", "dispensing"]
            beds = int(tags.get("beds", 0) or 0)
            daily_op = self._estimate_outpatients(ftype, beds, tags)
            medical.append(NearbyFacility(
                name=name, facility_type=ftype,
                lat=e_lat, lon=e_lon, distance_m=dist,
                specialty=specialty, daily_outpatients=daily_op,
                beds=beds, has_inhouse_pharmacy=has_inhouse, osm_tags=tags,
            ))
        medical.sort(key=lambda x: x.distance_m)
        pharmacies.sort(key=lambda x: x.distance_m)
        return medical, pharmacies, f"医療機関{len(medical)}件・薬局{len(pharmacies)}件"

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2) -> float:
        R = 6_371_000
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def _estimate_outpatients(ftype: str, beds: int, tags: Dict) -> int:
        if ftype == "hospital":
            if beds >= 300: return 1_000
            if beds >= 100: return 400
            return 150
        doctors = int(tags.get("staff:count", 0) or 0)
        if doctors >= 3: return 100
        if doctors >= 2: return 60
        return 35


# ---------------------------------------------------------------------------
# 4. 厚生労働省スクレイパー
# ---------------------------------------------------------------------------

class MHLWScraper:
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
                f"{self.BASE}/juminkanja/S2300/initialize", timeout=15,
            )
            self._initialized = r.status_code == 200
            return self._initialized
        except Exception:
            return False

    def search_pharmacy_candidates(
        self, keyword: str, pref_code: str = "", max_pages: int = 3
    ) -> Tuple[List[PharmacyCandidate], int, str]:
        return self._search_candidates(keyword, pref_code, max_pages, sjk="2")

    def search_medical_candidates(self, keyword: str, pref_code: str = ""):
        return self._search_candidates(keyword, pref_code, max_pages=1, sjk="1")

    def _search_candidates(self, keyword, pref_code, max_pages, sjk):
        if not self._initialized:
            self.initialize_session()
        try:
            r = self.session.get(
                f"{self.BASE}/juminkanja/S2300/yakkyokuSearch",
                params={"yakkyokuKeyword": keyword, "yakkyokuKeyword2": "", "searchJudgeKbn": "2"},
                headers={"ajaxFlag": "true"}, timeout=12,
            )
            if r.status_code != 200 or r.json().get("code") != "0":
                return [], 0, "検索失敗"
        except Exception as e:
            return [], 0, f"エラー: {e}"

        all_cands, total = [], 0
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
                cands, t = self._parse_candidate_list(r2.text)
                if page == 0:
                    total = t
                all_cands.extend(cands)
                if not cands or len(all_cands) >= total:
                    break
                time.sleep(0.3)
            except Exception:
                break
        return all_cands, total, f"{len(all_cands)}件取得（全{total}件）"

    def _parse_candidate_list(self, html: str) -> Tuple[List[PharmacyCandidate], int]:
        soup = BeautifulSoup(html, "html.parser")
        cands, total = [], 0
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
            if not href:
                continue
            if href.startswith("/"):
                href = self.DOMAIN + href
            qp = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(href).query))
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
                cands.append(PharmacyCandidate(
                    name=name, address=address, href=href,
                    pref_cd=qp.get("prefCd", ""), kikan_cd=qp.get("kikanCd", ""),
                ))
        return cands, max(total, len(cands))

    def get_pharmacy_detail(self, candidate: PharmacyCandidate) -> Tuple[Optional[Dict], str]:
        if not self._initialized:
            self.initialize_session()
        if candidate.pref_cd and candidate.kikan_cd:
            url = (f"{self.BASE}/juminkanja/S2430/initialize"
                   f"?prefCd={candidate.pref_cd}&kikanCd={candidate.kikan_cd}&kikanKbn=5")
        else:
            url = candidate.href
        try:
            r = self.session.get(url, timeout=15)
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            data = self._parse_detail(r.text)
            data["source_url"] = url
            return data, "OK"
        except Exception as e:
            return None, str(e)

    def _parse_detail(self, html: str) -> Dict:
        soup = BeautifulSoup(html, "html.parser")
        fields: Dict[str, str] = {}
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                k = cells[0].get_text(strip=True)
                if k:
                    fields[k] = cells[1].get_text(strip=True)
        for dl in soup.find_all("dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                k = dt.get_text(strip=True)
                if k:
                    fields[k] = dd.get_text(strip=True)
        data: Dict = {"all_fields": fields}
        for k, v in fields.items():
            if "所在地" in k and "フリガナ" not in k and "英語" not in k:
                data["address"] = re.sub(r"Googleマップ.*", "", v).strip()
                break
        rx_annual = None
        for k, v in fields.items():
            if "総取扱処方箋数" in k:
                nums = re.findall(r"[\d,]+", v)
                if nums:
                    try:
                        n = int(nums[0].replace(",", ""))
                        if n > 0:
                            rx_annual = n
                            break
                    except (ValueError, OverflowError):
                        pass
        if rx_annual is None:
            full_text = soup.get_text(separator=" ")
            for pat, mult in [
                (r"総取扱処方箋数[^\d]*(\d{1,3}(?:,\d{3})*|\d{4,})\s*件", 1.0),
                (r"週\s*平均[^\d]{0,15}(\d{1,4})\s*(?:回|枚)", 52.14),
                (r"年間[^\d]{0,15}(\d{1,6}(?:,\d{3})*)\s*(?:回|件)", 1.0),
            ]:
                m = re.search(pat, full_text, re.DOTALL)
                if m:
                    try:
                        n = int(m.group(1).replace(",", ""))
                        if n > 0:
                            rx_annual = int(n * mult)
                            break
                    except (ValueError, OverflowError):
                        pass
        data["prescriptions_annual"] = rx_annual
        return data

    def get_medical_outpatient_data(self, facility_name: str) -> Optional[int]:
        if not self._initialized:
            self.initialize_session()
        cands, _, _ = self.search_medical_candidates(facility_name)
        if not cands:
            return None
        best = cands[0]
        for c in cands[:3]:
            if facility_name[:4] in c.name:
                best = c
                break
        try:
            r = self.session.get(best.href, timeout=12)
            if r.status_code != 200:
                return None
            soup = BeautifulSoup(r.text, "html.parser")
            for row in soup.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    k = cells[0].get_text(strip=True)
                    v = cells[1].get_text(strip=True)
                    if "外来" in k and ("患者" in k or "数" in k):
                        nums = re.findall(r"[\d,]+", v)
                        if nums:
                            return int(nums[0].replace(",", ""))
        except Exception:
            pass
        return None

    def get_rx_for_nearby_pharmacies(
        self,
        pharmacy_names: List[str],
        limit: int = 5,
    ) -> Dict[str, Optional[int]]:
        """近隣薬局の処方箋枚数をMHLWから一括取得（新規開局モード用）"""
        results: Dict[str, Optional[int]] = {}
        for name in pharmacy_names[:limit]:
            try:
                cands, _, _ = self.search_pharmacy_candidates(name, max_pages=1)
                if cands:
                    best = cands[0]
                    for c in cands[:3]:
                        if name[:4] in c.name or c.name[:4] in name:
                            best = c
                            break
                    detail, _ = self.get_pharmacy_detail(best)
                    results[name] = detail.get("prescriptions_annual") if detail else None
                else:
                    results[name] = None
            except Exception:
                results[name] = None
            time.sleep(0.6)
        return results


# ---------------------------------------------------------------------------
# 5. 方法① 近隣医療機関アプローチ
# ---------------------------------------------------------------------------

class Method1Predictor:
    OUTPATIENT_RX_RATE = NATIONAL_STATS["outpatient_rx_rate"]

    def predict(
        self,
        pharmacy_lat: float,
        pharmacy_lon: float,
        medical_facilities: List[NearbyFacility],
        competing_pharmacies: List[NearbyFacility],
        mode_label: str = "方法①: 近隣医療機関アプローチ",
    ) -> PredictionResult:
        breakdown, total_daily = [], 0.0
        methodology = [
            f"### {mode_label} ロジック",
            "",
            "**算出式**: 各施設の外来患者数 × 診療科別処方箋発行率",
            "× 院外処方率（74.5%）× 当薬局集客シェア → 合計 × 年間稼働日数(305日)",
            "",
            f"**対象医療施設**: {len(medical_facilities)}件",
            "",
        ]
        for fac in medical_facilities:
            if fac.daily_outpatients == 0:
                continue
            rx_rate, _ = SPECIALTY_RX_RATES.get(fac.specialty, SPECIALTY_RX_RATES["不明/その他"])
            daily_rx = fac.daily_outpatients * rx_rate * self.OUTPATIENT_RX_RATE
            if fac.has_inhouse_pharmacy:
                daily_rx *= 0.6
            share, share_reason = self._calc_share(fac, pharmacy_lat, pharmacy_lon, competing_pharmacies)
            if fac.has_gate_pharmacy:
                share *= 0.4
                share_reason += "（既存門前薬局あり→割引）"
            flow = daily_rx * share
            breakdown.append({
                "施設名": fac.name,
                "タイプ": "病院" if fac.facility_type == "hospital" else "クリニック",
                "距離": f"{fac.distance_m:.0f}m",
                "診療科": fac.specialty,
                "外来患者/日": fac.daily_outpatients,
                "処方箋発行率": f"{rx_rate:.0%}",
                "院外処方箋/日": round(daily_rx),
                "当薬局シェア": f"{share:.1%}",
                "シェア根拠": share_reason,
                "当薬局流入/日": round(flow),
            })
            total_daily += flow
            methodology.append(
                f"**{fac.name}** ({fac.distance_m:.0f}m): "
                f"{fac.daily_outpatients}人/日 × {rx_rate:.0%} × 74.5% × {share:.0%} = {flow:.1f}枚/日"
            )
        annual = int(total_daily * NATIONAL_STATS["working_days"])
        if not medical_facilities:
            methodology.append("⚠ 近隣に医療施設なし → 全国中央値を使用")
            annual = NATIONAL_STATS["median_estimate"]
        methodology += [
            "", f"**合計**: {total_daily:.1f}枚/日 × 305日 = **{annual:,}枚/年**",
        ]
        return PredictionResult(
            method_name=mode_label,
            annual_rx=annual,
            min_val=int(annual * 0.6),
            max_val=int(annual * 1.8),
            confidence="medium" if medical_facilities else "low",
            daily_rx=int(total_daily),
            breakdown=breakdown,
            methodology=methodology,
            references=[
                {"name": "厚生労働省「受療行動調査」2020年",
                 "desc": "診療科別処方箋発行率の根拠データ",
                 "url": "https://www.mhlw.go.jp/toukei/list/35-34.html"},
                {"name": "厚生労働省「調剤医療費の動向」2022年度",
                 "desc": f"院外処方率（全国平均 {self.OUTPATIENT_RX_RATE:.1%}）の根拠データ",
                 "url": "https://www.mhlw.go.jp/topics/medias/med/"},
                {"name": "OpenStreetMap / Overpass API",
                 "desc": "近隣施設データ（名称・位置・タグ）のソース",
                 "url": "https://overpass-api.de/"},
            ],
        )

    def _calc_share(self, fac, ph_lat, ph_lon, competitors) -> Tuple[float, str]:
        dist = OverpassSearcher._haversine(fac.lat, fac.lon, ph_lat, ph_lon)
        if dist <= 50:
            base, reason = 0.75, "50m以内（実質門前）"
        elif dist <= 150:
            base, reason = 0.50, "150m以内（近接立地）"
        elif dist <= 300:
            base, reason = 0.30, "300m以内（徒歩圏）"
        else:
            base, reason = 0.15, "500m以内（自転車圏）"
        near_comps = [
            p for p in competitors
            if OverpassSearcher._haversine(fac.lat, fac.lon, p.lat, p.lon) < 300
        ]
        if near_comps:
            tw = 1.0 / max(dist, 10)
            cws = [1.0 / max(OverpassSearcher._haversine(fac.lat, fac.lon, p.lat, p.lon), 10)
                   for p in near_comps]
            adj = base * (tw / (tw + sum(cws)))
            reason += f"（競合{len(near_comps)}件で按分）"
        else:
            adj = base
            reason += "（競合なし）"
        return min(adj, 0.90), reason


# ---------------------------------------------------------------------------
# 6. 方法② 商圏人口動態アプローチ
# ---------------------------------------------------------------------------

class Method2Predictor:
    def predict(
        self,
        pharmacy_lat: float,
        pharmacy_lon: float,
        competing_pharmacies: List[NearbyFacility],
        area_density: int,
        radius_m: int,
        density_source: str = "",
        radius_reason: str = "",
    ) -> PredictionResult:
        area_km2 = math.pi * (radius_m / 1000) ** 2
        total_pop = int(area_km2 * area_density)
        age_breakdown, total_rx = [], 0
        for age_grp, ratio in AGE_DISTRIBUTION.items():
            pop = int(total_pop * ratio)
            v_rate = VISIT_RATE_BY_AGE[age_grp]
            annual_rx = int(pop * v_rate * NATIONAL_STATS["prescription_per_visit"]
                           * NATIONAL_STATS["outpatient_rx_rate"])
            age_breakdown.append({
                "年齢層": age_grp,
                "推計人口": f"{pop:,}人",
                "受診率": f"{v_rate}回/年",
                "年間受診回数": f"{pop * v_rate:,.0f}回",
                "年間処方箋数": f"{annual_rx:,}枚",
            })
            total_rx += annual_rx
        share, share_reason = self._market_share(pharmacy_lat, pharmacy_lon, competing_pharmacies)
        annual_est = int(total_rx * share)

        # 加重平均受診率
        avg_visit_rate = sum(
            AGE_DISTRIBUTION[ag] * VISIT_RATE_BY_AGE[ag] for ag in AGE_DISTRIBUTION
        )

        methodology = [
            "### 方法②（商圏人口動態アプローチ）ロジック",
            "",
            "**算出式**: 商圏人口 × 年齢層別受診率 × 処方箋発行率(65%)",
            "× 院外処方率(74.5%) × 当薬局市場シェア",
            "",
            f"**商圏設定**: 半径{radius_m}m（面積: {area_km2:.2f}km²）",
            f"**根拠**: {radius_reason}" if radius_reason else "",
            f"**人口密度**: {area_density:,}人/km²（{density_source}）",
            f"**推計商圏人口**: {total_pop:,}人",
            f"**加重平均受診率**: {avg_visit_rate:.2f}回/人/年 "
            f"（患者調査2020年 外来受療率×365日 / OECD日本: 12.6回/年と整合）",
            "",
            f"**商圏内年間処方箋総数**: {total_rx:,}枚",
            f"**当薬局推計市場シェア**: {share:.1%}（{share_reason}）",
            f"**推計年間処方箋枚数**: **{annual_est:,}枚/年**",
            "",
            "**市場シェアの計算方法（v2.3修正）**: 距離帯別実効競合数モデル",
            "競合≤200m×1.5 + 競合≤500m×1.0 + 競合>500m×0.5 = 実効競合数N",
            "シェア = 1/(N+1)　上限55% / 下限8%",
        ]
        return PredictionResult(
            method_name="方法②: 商圏人口動態アプローチ",
            annual_rx=annual_est,
            min_val=int(annual_est * 0.55),
            max_val=int(annual_est * 1.80),
            confidence="low",
            daily_rx=int(annual_est / NATIONAL_STATS["working_days"]),
            breakdown=age_breakdown,
            methodology=methodology,
            references=[
                {"name": "厚生労働省「患者調査」2020年",
                 "desc": "年齢層別外来受診率（外来受療率×365日で年間受診回数を導出）",
                 "url": "https://www.mhlw.go.jp/toukei/saikin/hw/kanja/20/index.html"},
                {"name": "OECD Health Statistics 2022 (Japan)",
                 "desc": "日本の外来受診回数: 約12.6回/人/年 — 本ツールの加重平均値と整合",
                 "url": "https://www.oecd.org/health/health-statistics.htm"},
                {"name": "総務省「国勢調査」2020年",
                 "desc": "年齢別人口分布・地区別人口密度",
                 "url": "https://www.stat.go.jp/data/kokusei/2020/"},
            ],
        )

    def _market_share(self, lat, lon, competitors) -> Tuple[float, str]:
        """
        距離帯別「実効競合数モデル」による市場シェア推計 (v2.3修正)

        v2.2 の問題: 1/d² 重みを使っていたため競合の重みが常に極小
        （150m先の競合 → 1/150²=0.000044 ≈ 0）となり、自社重み 1.0 に対して
        無視できる大きさになって、事実上シェアは常に上限80%に張り付いていた。

        v2.3 修正: 距離帯別に競合を「実効競合数」に換算して Huff型シェアを計算する。
            ≤200m （近接）: 重み 1.5 — 徒歩数分で行ける最寄り競合、最も影響大
            ≤500m （中距離）: 重み 1.0 — 標準的な競合
            >500m （遠距離）: 重み 0.5 — 影響は出るが半分程度
            シェア = 1 / (実効競合数 + 1)
            上限: 55% （競合ゼロでも患者流出を考慮）
            下限:  8% （極めて競合が多い場合でも最低限を確保）
        """
        if not competitors:
            return 0.55, "商圏内競合なし（上限55%: 商圏外流出・他エリア受診を考慮）"

        near    = sum(1 for p in competitors if p.distance_m <= 200)
        medium  = sum(1 for p in competitors if 200 < p.distance_m <= 500)
        distant = sum(1 for p in competitors if p.distance_m > 500)

        # 実効競合数（距離帯別重み付き）
        effective_n = near * 1.5 + medium * 1.0 + distant * 0.5

        # Huff型シェア: 自社1 / (自社1 + 競合 effective_n)
        raw_share = 1.0 / (effective_n + 1.0)

        # 上限55% / 下限8% でクリップ
        share = max(min(raw_share, 0.55), 0.08)

        detail = (
            f"近接≤200m: {near}件×1.5 + 中距離≤500m: {medium}件×1.0 "
            f"+ 遠距離>500m: {distant}件×0.5 = 実効{effective_n:.1f}件"
        )
        reason = f"競合{len(competitors)}件 ({detail}) → シェア{share:.1%}"
        return share, reason


# ---------------------------------------------------------------------------
# 7. マップ生成
# ---------------------------------------------------------------------------

def build_competitor_map(
    pharmacy_name: str,
    pharmacy_lat: float,
    pharmacy_lon: float,
    medical_facilities: List[NearbyFacility],
    competing_pharmacies: List[NearbyFacility],
    radius_m: int = 500,
    geocoder_source: str = "",
) -> folium.Map:
    """既存薬局分析モード用マップ"""
    gmap_url = f"https://www.google.com/maps/search/?api=1&query={pharmacy_lat},{pharmacy_lon}"
    m = folium.Map(location=[pharmacy_lat, pharmacy_lon], zoom_start=16)
    folium.Circle(
        location=[pharmacy_lat, pharmacy_lon], radius=radius_m,
        color="#FF4444", fill=True, fill_opacity=0.05, weight=2,
        popup=f"商圏半径 {radius_m}m",
    ).add_to(m)
    folium.Marker(
        location=[pharmacy_lat, pharmacy_lon],
        popup=folium.Popup(
            f"<b>💊 {pharmacy_name}</b><br>【分析対象薬局】<br>"
            f"<small>座標ソース: {geocoder_source}</small><br>"
            f'<a href="{gmap_url}" target="_blank">Googleマップで確認</a>',
            max_width=250
        ),
        tooltip=f"💊 {pharmacy_name}",
        icon=folium.Icon(color="red", icon="plus-sign", prefix="glyphicon"),
    ).add_to(m)
    for fac in medical_facilities:
        color = "blue" if fac.facility_type == "hospital" else "cadetblue"
        icon_n = "h-sign" if fac.facility_type == "hospital" else "user-md"
        label = "🏥 病院" if fac.facility_type == "hospital" else "🏨 クリニック"
        inhouse = "（院内薬局あり）" if fac.has_inhouse_pharmacy else ""
        popup_html = (
            f"<b>{label} {fac.name}</b>{inhouse}<br>"
            f"診療科: {fac.specialty}<br>"
            f"距離: {fac.distance_m:.0f}m | 外来(推計): {fac.daily_outpatients}人/日"
        )
        if fac.mhlw_annual_outpatients:
            popup_html += f"<br>MHLW年間外来: {fac.mhlw_annual_outpatients:,}人"
        folium.Marker(
            location=[fac.lat, fac.lon],
            popup=folium.Popup(popup_html, max_width=250),
            tooltip=f"{label} {fac.name} ({fac.distance_m:.0f}m)",
            icon=folium.Icon(color=color, icon=icon_n, prefix="glyphicon"),
        ).add_to(m)
        folium.PolyLine(
            [[fac.lat, fac.lon], [pharmacy_lat, pharmacy_lon]],
            color=color, weight=1.5, opacity=0.5,
        ).add_to(m)
    for ph in competing_pharmacies:
        is_chain = any(c in ph.name for c in MAJOR_CHAINS)
        rx_text = (f"<b style='color:#c00'>処方箋: {ph.mhlw_annual_outpatients:,}枚/年</b>"
                   if ph.mhlw_annual_outpatients else "MHLW: データなし")
        popup_html = (
            f"<b>💊 {ph.name}</b>{'（大手チェーン）' if is_chain else ''}<br>"
            f"距離: {ph.distance_m:.0f}m<br>{rx_text}"
        )
        folium.Marker(
            location=[ph.lat, ph.lon],
            popup=folium.Popup(popup_html, max_width=220),
            tooltip=f"💊 {ph.name} ({ph.distance_m:.0f}m)",
            icon=folium.Icon(color="green", icon="shopping-cart", prefix="glyphicon"),
        ).add_to(m)
    return m


def build_new_pharmacy_map(
    config: NewPharmacyConfig,
    pharmacy_lat: float,
    pharmacy_lon: float,
    medical_facilities: List[NearbyFacility],
    competing_pharmacies: List[NearbyFacility],
    radius_m: int = 500,
    geocoder_source: str = "",
) -> folium.Map:
    """新規開局予測モード用マップ（近隣薬局の処方箋枚数付き）"""
    gmap_url = (f"https://www.google.com/maps/search/?api=1&query="
                + urllib.parse.quote(config.address))
    m = folium.Map(location=[pharmacy_lat, pharmacy_lon], zoom_start=16)
    folium.Circle(
        location=[pharmacy_lat, pharmacy_lon], radius=radius_m,
        color="#FF8C00", fill=True, fill_opacity=0.06, weight=2.5,
        popup=f"商圏半径 {radius_m}m（{config.scenario}シナリオ）",
    ).add_to(m)
    # 開局予定地
    folium.Marker(
        location=[pharmacy_lat, pharmacy_lon],
        popup=folium.Popup(
            f"<b>🏗 {config.pharmacy_name}</b><br>【開局予定地】<br>"
            f"住所: {config.address}<br>"
            f"<small>座標ソース: {geocoder_source}</small><br>"
            f'<a href="{gmap_url}" target="_blank">Googleマップで確認</a>',
            max_width=260
        ),
        tooltip=f"🏗 {config.pharmacy_name}（開局予定）",
        icon=folium.Icon(color="red", icon="star", prefix="glyphicon"),
    ).add_to(m)
    # 誘致予定クリニック（門前シナリオ）
    if config.scenario in ("gate_clinic", "both"):
        clinic_lat = pharmacy_lat + 0.000225  # ~25m北
        folium.Marker(
            location=[clinic_lat, pharmacy_lon],
            popup=folium.Popup(
                f"<b>🏥 [誘致予定] {config.gate_specialty}クリニック</b><br>"
                f"外来: {config.gate_daily_outpatients}人/日（想定）<br>"
                f"院内薬局: {'あり' if config.gate_has_inhouse else 'なし'}",
                max_width=220
            ),
            tooltip=f"[誘致予定] {config.gate_specialty}クリニック",
            icon=folium.Icon(color="orange", icon="plus-sign", prefix="glyphicon"),
        ).add_to(m)
    # 近隣医療施設
    for fac in medical_facilities:
        color = "blue" if fac.facility_type == "hospital" else "cadetblue"
        icon_n = "h-sign" if fac.facility_type == "hospital" else "user-md"
        label = "🏥 病院" if fac.facility_type == "hospital" else "🏨 クリニック"
        popup_html = (
            f"<b>{label} {fac.name}</b><br>"
            f"診療科: {fac.specialty}<br>"
            f"距離: {fac.distance_m:.0f}m | 外来(推計): {fac.daily_outpatients}人/日"
        )
        folium.Marker(
            location=[fac.lat, fac.lon],
            popup=folium.Popup(popup_html, max_width=250),
            tooltip=f"{label} {fac.name} ({fac.distance_m:.0f}m)",
            icon=folium.Icon(color=color, icon=icon_n, prefix="glyphicon"),
        ).add_to(m)
        folium.PolyLine(
            [[fac.lat, fac.lon], [pharmacy_lat, pharmacy_lon]],
            color=color, weight=1.5, opacity=0.4,
        ).add_to(m)
    # 競合薬局（処方箋枚数付き）
    for ph in competing_pharmacies:
        is_chain = any(c in ph.name for c in MAJOR_CHAINS)
        has_rx = ph.mhlw_annual_outpatients is not None
        rx_text = (
            f"<b style='color:#c00'>処方箋: {ph.mhlw_annual_outpatients:,}枚/年</b><br>"
            f"日次換算: {ph.mhlw_annual_outpatients // 305}枚/日"
            if has_rx else "MHLW処方箋: 未取得"
        )
        marker_color = "darkgreen" if has_rx else "green"
        popup_html = (
            f"<b>💊 {ph.name}</b>{'（大手）' if is_chain else ''}<br>"
            f"距離: {ph.distance_m:.0f}m<br>{rx_text}"
        )
        folium.Marker(
            location=[ph.lat, ph.lon],
            popup=folium.Popup(popup_html, max_width=240),
            tooltip=f"💊 {ph.name} ({ph.distance_m:.0f}m)"
                    + (f" {ph.mhlw_annual_outpatients:,}枚/年" if has_rx else ""),
            icon=folium.Icon(color=marker_color, icon="shopping-cart", prefix="glyphicon"),
        ).add_to(m)
    return m


# ---------------------------------------------------------------------------
# 8. 乖離評価
# ---------------------------------------------------------------------------

def calc_deviation(actual: int, predicted: int) -> Tuple[float, str, str]:
    if actual <= 0:
        return 0.0, "N/A", "neutral"
    pct = (predicted - actual) / actual * 100
    label = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
    color = "normal" if abs(pct) < 20 else ("inverse" if abs(pct) < 50 else "off")
    return pct, label, color


# ---------------------------------------------------------------------------
# 9. UI レンダリング関数
# ---------------------------------------------------------------------------

def render_data_sources_panel() -> None:
    """統計数値の根拠・データソースを表示（v2.2新規）"""
    with st.expander("📚 統計数値の根拠・データソース一覧（クリックで展開）", expanded=False):
        st.markdown("本ツールで使用している統計数値の出典と根拠を以下に示します。")
        for stat_name, ref in STAT_REFERENCES.items():
            st.markdown(f"#### {stat_name}")
            col1, col2 = st.columns([1, 2])
            with col1:
                st.metric("値", ref["value"][:40] if len(ref["value"]) > 40 else ref["value"])
            with col2:
                st.markdown(f"**出典**: {ref['source']}")
                st.markdown(f"**補足**: {ref['note']}")
                if ref.get("url"):
                    st.markdown(f"🔗 [{ref['url']}]({ref['url']})")
            st.markdown("---")


def render_auto_params_panel(
    lat: Optional[float],
    lon: Optional[float],
    geocode_display: str,
    geocoder_source: str,
    address: str,
    area_density: int,
    area_density_source: str,
    commercial_radius: int,
    commercial_radius_reason: str,
    is_gate: bool,
    gate_reason: str,
) -> None:
    """座標・人口密度・商圏半径の自動計算パラメータを表示"""
    st.markdown("### 📐 自動計算パラメータ")
    if lat and lon:
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric(
                f"🌐 取得座標 ({geocoder_source[:6]})",
                f"{lat:.5f}",
                delta=f"経度: {lon:.5f}", delta_color="off",
                help=f"ジオコーダー: {geocoder_source}",
            )
            gmap_url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            st.markdown(f'<a href="{gmap_url}" target="_blank">📍 Googleマップで確認</a>',
                        unsafe_allow_html=True)
        with col2:
            st.metric("🏘 人口密度", f"{area_density:,}人/km²", help=area_density_source)
            st.caption(f"📋 {area_density_source[:40]}")
        with col3:
            gate_icon = "🚪" if is_gate else "📏"
            st.metric(f"{gate_icon} 商圏半径", f"{commercial_radius}m")
            st.caption(f"📐 {commercial_radius_reason[:45]}")
        with col4:
            area_km2 = math.pi * (commercial_radius / 1000) ** 2
            catchment_pop = int(area_km2 * area_density)
            st.metric("👥 推計商圏人口", f"{catchment_pop:,}人")
            if is_gate:
                st.caption(f"🚪 門前: {gate_reason[:30]}")
    else:
        st.warning("⚠ 座標取得失敗 → 空間分析をスキップしました")
        st.info(
            "💡 **ヒント**: 建物名・号室を除いた「都道府県+市区町村+丁目番地」の形式で再検索すると改善する場合があります。"
        )
        st.metric("🏘 推計人口密度（住所から）", f"{area_density:,}人/km²")
        st.caption(area_density_source)


def render_comparison_banner(analysis: FullAnalysis) -> None:
    st.markdown("## 📊 予測値 vs 厚労省実績値 比較")
    actual = analysis.mhlw_annual_rx
    m1, m2 = analysis.method1, analysis.method2
    cols = st.columns(4)
    with cols[0]:
        if actual:
            st.metric("🏥 MHLW実績値", f"{actual:,} 枚/年")
            st.caption("🟢 信頼度: 高（実績値）")
        else:
            st.metric("🏥 MHLW実績値", "記載なし")
            st.caption("⚠ 当該薬局は未報告")
    with cols[1]:
        if m1:
            st.metric("① 医療機関アプローチ", f"{m1.annual_rx:,} 枚/年",
                      delta=calc_deviation(actual, m1.annual_rx)[1] if actual else None)
            st.caption(f"レンジ: {m1.min_val:,}〜{m1.max_val:,}")
    with cols[2]:
        if m2:
            st.metric("② 人口動態アプローチ", f"{m2.annual_rx:,} 枚/年",
                      delta=calc_deviation(actual, m2.annual_rx)[1] if actual else None)
            st.caption(f"レンジ: {m2.min_val:,}〜{m2.max_val:,}")
    with cols[3]:
        if actual and m1 and m2:
            avg = (m1.annual_rx + m2.annual_rx) // 2
            pct, label, _ = calc_deviation(actual, avg)
            st.metric("予測平均 vs 実績", f"{avg:,} 枚/年", delta=label,
                      delta_color="normal" if abs(pct) < 30 else "inverse")
            st.caption("（①と②の単純平均）")


def render_new_pharmacy_comparison(result: NewPharmacyResult) -> None:
    """新規開局モードの予測比較バナー"""
    st.markdown("## 📊 開局シナリオ別 処方箋枚数予測")
    m1, m2 = result.method1, result.method2
    cols = st.columns(3)
    with cols[0]:
        if m1:
            st.metric("🚪 門前クリニック誘致シナリオ", f"{m1.annual_rx:,} 枚/年")
            st.caption(f"レンジ: {m1.min_val:,}〜{m1.max_val:,} | {m1.daily_rx}枚/日")
            st.caption(f"誘致科: {result.config.gate_specialty} ({result.config.gate_daily_outpatients}人/日)")
        else:
            st.info("門前クリニックシナリオは非選択")
    with cols[1]:
        if m2:
            st.metric("🌐 面での集客シナリオ", f"{m2.annual_rx:,} 枚/年")
            st.caption(f"レンジ: {m2.min_val:,}〜{m2.max_val:,} | {m2.daily_rx}枚/日")
            st.caption(f"商圏半径: {result.commercial_radius}m / 密度: {result.area_density:,}人/km²")
        else:
            st.info("商圏人口シナリオは非選択")
    with cols[2]:
        if m1 and m2:
            avg = (m1.annual_rx + m2.annual_rx) // 2
            st.metric("📈 2シナリオ平均", f"{avg:,} 枚/年")
            diff = abs(m1.annual_rx - m2.annual_rx)
            pct = diff / max(avg, 1) * 100
            st.caption(f"シナリオ差: {diff:,}枚/年 ({pct:.0f}%)")
            if pct < 30:
                st.caption("✅ シナリオ間の一致度: 高")
            elif pct < 70:
                st.caption("⚠ シナリオ間の一致度: 中")
            else:
                st.caption("❗ シナリオ間の乖離が大きい（立地条件の差が顕著）")


def render_prediction_tabs(m1: Optional[PredictionResult], m2: Optional[PredictionResult]) -> None:
    """予測ロジックタブを表示（既存・新規モード共通）"""
    import pandas as pd
    tab_labels = []
    if m1: tab_labels.append("① 医療機関アプローチ")
    if m2: tab_labels.append("② 人口動態アプローチ")
    tab_labels.append("📚 データソース")
    tabs = st.tabs(tab_labels)
    tab_idx = 0
    if m1:
        with tabs[tab_idx]:
            st.metric("年間推計処方箋枚数", f"{m1.annual_rx:,} 枚/年")
            st.caption(f"レンジ: {m1.min_val:,}〜{m1.max_val:,}枚/年 | {m1.daily_rx}枚/日")
            if m1.breakdown:
                st.markdown("#### 施設別 処方箋流入内訳")
                st.dataframe(pd.DataFrame(m1.breakdown), use_container_width=True, hide_index=True)
            st.markdown("#### 推計ロジック")
            for line in m1.methodology:
                st.markdown(line)
        tab_idx += 1
    if m2:
        with tabs[tab_idx]:
            st.metric("年間推計処方箋枚数", f"{m2.annual_rx:,} 枚/年")
            st.caption(f"レンジ: {m2.min_val:,}〜{m2.max_val:,}枚/年 | {m2.daily_rx}枚/日")
            if m2.breakdown:
                st.markdown("#### 年齢層別 処方箋数内訳")
                st.dataframe(pd.DataFrame(m2.breakdown), use_container_width=True, hide_index=True)
            st.markdown("#### 推計ロジック")
            for line in m2.methodology:
                if line:
                    st.markdown(line)
        tab_idx += 1
    with tabs[tab_idx]:
        render_data_sources_panel()


def render_competitor_table(medical, pharmacies, show_rx: bool = False) -> None:
    import pandas as pd
    st.markdown("### 🗺 近隣の医療施設・競合薬局")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**医療施設（{len(medical)}件）**")
        if medical:
            rows = [{"施設名": f.name,
                     "種別": "病院" if f.facility_type == "hospital" else "クリニック",
                     "距離": f"{f.distance_m:.0f}m",
                     "診療科": f.specialty,
                     "外来/日(推計)": f"{f.daily_outpatients}人",
                     "院内薬局": "あり" if f.has_inhouse_pharmacy else "なし"}
                    for f in medical]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("検索範囲内に医療施設が見つかりませんでした")
    with c2:
        st.markdown(f"**競合薬局（{len(pharmacies)}件）**")
        if pharmacies:
            rows = [{"薬局名": p.name,
                     "距離": f"{p.distance_m:.0f}m",
                     "チェーン": "はい" if any(c in p.name for c in MAJOR_CHAINS) else "独立",
                     "処方箋/年(MHLW)": f"{p.mhlw_annual_outpatients:,}枚" if p.mhlw_annual_outpatients else "未取得"}
                    for p in pharmacies]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("検索範囲内に競合薬局が見つかりませんでした")


# ---------------------------------------------------------------------------
# 10. メイン UI
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="薬局 処方箋枚数 多面的予測 v2.3",
        page_icon="🔬",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.title("🔬 薬局 年間処方箋枚数 多面的予測ツール v2.3")
    st.markdown(
        "🔧 **v2.3の修正点**: 方法②（面での集客）の**市場シェア計算バグを修正** — "
        "競合薬局の距離・件数を正しく反映する「実効競合数モデル」に変更し、過大推計を解消。"
    )

    for k, v in [
        ("candidates", []), ("analysis", None), ("new_result", None),
    ]:
        if k not in st.session_state:
            st.session_state[k] = v

    tab_existing, tab_new = st.tabs(["🏪 既存薬局を分析", "🏗 新規開局を予測"])

    # ================================================================
    # TAB A: 既存薬局分析モード（v2.1と同様）
    # ================================================================
    with tab_existing:
        _render_existing_mode()

    # ================================================================
    # TAB B: 新規開局予測モード
    # ================================================================
    with tab_new:
        _render_new_pharmacy_mode()


def _render_existing_mode() -> None:
    st.markdown("### STEP 1 — 薬局名で検索（厚生労働省ポータル）")
    col_kw, col_pref = st.columns([3, 1])
    with col_kw:
        keyword = st.text_input(
            "薬局名（一部でも可）",
            placeholder="例: アイセイ薬局 武蔵小杉 / 日本調剤 新宿",
            key="ex_keyword",
        )
    with col_pref:
        pref = st.selectbox("都道府県（任意）", ["（指定なし）"] + PREFECTURES, key="ex_pref")

    if st.button("🔍 候補を検索", type="primary", key="ex_search"):
        st.session_state["analysis"] = None
        pref_code = PREFECTURE_CODES.get(pref, "")
        with st.spinner("MHLWポータルを検索中…"):
            scraper = MHLWScraper()
            cands, total, status = scraper.search_pharmacy_candidates(keyword.strip(), pref_code)
        st.session_state["candidates"] = cands
        if cands:
            st.success(f"✅ {status}（全{total}件）")
        else:
            st.warning("候補が見つかりませんでした")

    candidates: List[PharmacyCandidate] = st.session_state.get("candidates", [])
    analysis: Optional[FullAnalysis] = st.session_state.get("analysis")

    if candidates and analysis is None:
        st.markdown("---")
        st.markdown("### STEP 2 — 薬局を選択して分析実行")
        options = [f"{c.name}　{'（' + c.address[:35] + '）' if c.address else ''}" for c in candidates]
        sel_label = st.selectbox("候補一覧", options, key="ex_candidate")
        sel_idx = options.index(sel_label)
        sel = candidates[sel_idx]
        st.caption(f"📍 住所: {sel.address or '不明'}")

        if sel.address:
            dens, dens_src = get_population_density(sel.address)
            r_init, r_reason = calc_commercial_radius(dens, False, "")
            est_pop = int(math.pi * (r_init / 1000) ** 2 * dens)
            with st.expander("📐 自動計算パラメータのプレビュー", expanded=True):
                p1, p2, p3 = st.columns(3)
                p1.metric("🏘 推計人口密度", f"{dens:,}人/km²", help=dens_src)
                p2.metric("📏 初期商圏半径", f"{r_init}m", help=r_reason)
                p3.metric("👥 推計商圏人口", f"{est_pop:,}人")
                st.caption(f"🗂 出典: {dens_src} | 商圏根拠: {r_reason}")

        try_mhlw = st.checkbox("近隣医療施設のMHLWデータ照会", value=False, key="ex_mhlw")
        if st.button("🚀 多面的分析を実行", type="primary", use_container_width=True, key="ex_run"):
            run_analysis(sel, try_mhlw)

    if analysis:
        st.markdown("---")
        st.markdown(f"## 結果: `{analysis.pharmacy_name}`")
        st.caption(f"住所: {analysis.pharmacy_address}")
        render_auto_params_panel(
            analysis.pharmacy_lat, analysis.pharmacy_lon,
            analysis.geocode_display, analysis.geocoder_source,
            analysis.pharmacy_address,
            analysis.area_density, analysis.area_density_source,
            analysis.commercial_radius, analysis.commercial_radius_reason,
            analysis.is_gate_pharmacy, analysis.gate_pharmacy_reason,
        )
        st.markdown("---")
        render_comparison_banner(analysis)
        st.markdown("---")
        tab_map, tab_preds, tab_mhlw, tab_log = st.tabs([
            "🗺 競合マップ", "📊 予測ロジック", "🏥 厚労省データ", "🔍 検索ログ"
        ])
        with tab_map:
            if analysis.pharmacy_lat and analysis.pharmacy_lon:
                st.markdown(
                    "**凡例**: 🔴 分析対象薬局　🔵 病院　🔷 クリニック　🟢 競合薬局"
                    f"　（商圏円: 半径{analysis.commercial_radius}m）"
                )
                m = build_competitor_map(
                    analysis.pharmacy_name,
                    analysis.pharmacy_lat, analysis.pharmacy_lon,
                    analysis.nearby_medical, analysis.nearby_pharmacies,
                    analysis.commercial_radius, analysis.geocoder_source,
                )
                st_folium(m, width=None, height=520, use_container_width=True)
            else:
                st.warning("座標取得失敗のためマップを表示できません")
            render_competitor_table(analysis.nearby_medical, analysis.nearby_pharmacies)
        with tab_preds:
            render_prediction_tabs(analysis.method1, analysis.method2)
        with tab_mhlw:
            if analysis.mhlw_annual_rx:
                st.success(f"✅ 厚労省実績値: **{analysis.mhlw_annual_rx:,}枚/年**")
            else:
                st.warning("厚労省ポータルに処方箋枚数の記載がありませんでした")
            if analysis.mhlw_source_url:
                st.markdown(f"🔗 [MHLWポータルで確認]({analysis.mhlw_source_url})")
        with tab_log:
            st.code("\n".join(analysis.search_log))

    if not candidates and analysis is None:
        st.markdown("---")
        c1, c2, c3 = st.columns(3)
        c1.markdown("**① 厚労省実データ取得**\n\n薬局機能情報提供制度から「総取扱処方箋数」を直接取得")
        c2.markdown("**② 近隣医療機関アプローチ**\n\nOSMで近隣クリニック・病院を検索し診療科別処方箋発行率から予測")
        c3.markdown("**③ 商圏人口動態アプローチ**\n\n住所から人口密度・商圏半径を自動計算し年齢別受診率×市場シェアで予測")


def _render_new_pharmacy_mode() -> None:
    """新規開局予測モード UI"""
    st.markdown(
        "### 🏗 新規開局予測モード\n\n"
        "スーパーマーケット内調剤薬局などの**新規開局**を想定し、"
        "開局予定住所を入力するだけで集客可能な処方箋枚数を予測します。\n\n"
        "**シナリオA**: 門前クリニックを誘致した場合の予測（医療機関依存型）\n"
        "**シナリオB**: 商圏人口から面で集客した場合の予測（地域密着型）"
    )
    st.markdown("---")
    st.markdown("#### STEP 1 — 開局予定地の住所を入力")
    col_name, col_addr = st.columns([1, 2])
    with col_name:
        pharmacy_name = st.text_input(
            "薬局名（任意）",
            value="開局予定薬局",
            placeholder="例: ○○調剤薬局",
            key="new_ph_name",
        )
    with col_addr:
        address = st.text_input(
            "開局予定地の住所",
            placeholder="例: 東京都板橋区成増1丁目12-3 / 神奈川県川崎市中原区新丸子東3丁目",
            key="new_address",
        )
    if address:
        dens, dens_src = get_population_density(address)
        r_init, r_reason = calc_commercial_radius(dens, False, "")
        est_pop = int(math.pi * (r_init / 1000) ** 2 * dens)
        st.info(
            f"📐 **住所から自動計算**: 人口密度 {dens:,}人/km²（{dens_src}）"
            f" → 商圏半径 {r_init}m → 推計商圏人口 {est_pop:,}人"
        )

    st.markdown("---")
    st.markdown("#### STEP 2 — 開局シナリオを選択")
    scenario = st.radio(
        "予測シナリオ",
        options=["gate_clinic", "catchment", "both"],
        format_func=lambda x: {
            "gate_clinic": "🚪 シナリオA: 門前クリニック誘致（医療機関依存型）",
            "catchment":   "🌐 シナリオB: 面での集客（商圏人口動態型）",
            "both":        "🔄 両方同時実行して比較",
        }[x],
        key="new_scenario",
    )

    gate_specialty, gate_daily, gate_inhouse = "一般内科", 50, False
    if scenario in ("gate_clinic", "both"):
        st.markdown("**🚪 門前クリニック設定**")
        col_sp, col_daily, col_inhouse = st.columns(3)
        with col_sp:
            gate_specialty = st.selectbox(
                "誘致する診療科",
                list(SPECIALTY_RX_RATES.keys()),
                index=0,
                key="new_specialty",
                help="診療科によって処方箋発行率が異なります",
            )
            rx_rate = SPECIALTY_RX_RATES[gate_specialty][0]
            st.caption(f"処方箋発行率: {rx_rate:.0%}")
        with col_daily:
            gate_daily = st.slider(
                "想定1日外来患者数",
                min_value=10, max_value=300, value=50, step=5,
                key="new_daily_op",
                help="一般的な個人クリニック: 30〜60人/日, 規模の大きいクリニック: 80〜150人/日",
            )
            daily_rx_est = int(gate_daily * rx_rate * NATIONAL_STATS["outpatient_rx_rate"])
            st.caption(f"推計処方箋/日: {daily_rx_est}枚")
        with col_inhouse:
            gate_inhouse = st.checkbox(
                "院内薬局あり（院外処方率低下）",
                value=False,
                key="new_inhouse",
                help="院内薬局があると院外処方率が60%程度に低下します",
            )

    st.markdown("---")
    st.markdown("#### STEP 3 — オプション設定")
    fetch_rx = st.checkbox(
        "📋 近隣薬局の処方箋枚数をMHLWから取得してマップに表示（上位5薬局、時間がかかります）",
        value=False,
        key="new_fetch_rx",
        help="ONにすると近隣競合薬局の実際の処方箋枚数をMHLWから取得し、マップのポップアップに表示します。取得に1〜3分かかる場合があります。",
    )

    can_run = bool(address.strip())
    if not can_run:
        st.info("住所を入力すると分析を実行できます。")

    if st.button(
        "🚀 新規開局予測を実行", type="primary",
        use_container_width=True, key="new_run",
        disabled=not can_run,
    ):
        config = NewPharmacyConfig(
            address=address.strip(),
            pharmacy_name=pharmacy_name or "開局予定薬局",
            scenario=scenario,
            gate_specialty=gate_specialty,
            gate_daily_outpatients=gate_daily,
            gate_has_inhouse=gate_inhouse,
            fetch_nearby_rx=fetch_rx,
        )
        run_new_pharmacy_analysis(config)

    # 結果表示
    new_result: Optional[NewPharmacyResult] = st.session_state.get("new_result")
    if new_result:
        st.markdown("---")
        st.markdown(f"## 結果: `{new_result.config.pharmacy_name}`")
        st.caption(f"開局予定住所: {new_result.config.address}")

        render_auto_params_panel(
            new_result.lat, new_result.lon,
            new_result.geocode_display, new_result.geocoder_source,
            new_result.config.address,
            new_result.area_density, new_result.area_density_source,
            new_result.commercial_radius, new_result.commercial_radius_reason,
            new_result.is_gate, new_result.gate_reason,
        )
        st.markdown("---")
        render_new_pharmacy_comparison(new_result)
        st.markdown("---")

        tab_map, tab_preds, tab_log = st.tabs(["🗺 競合環境マップ", "📊 予測ロジック", "🔍 分析ログ"])
        with tab_map:
            if new_result.lat and new_result.lon:
                has_rx_data = any(p.mhlw_annual_outpatients for p in new_result.nearby_pharmacies)
                st.markdown(
                    "**凡例**: 🔴⭐ 開局予定地　🟠 誘致予定クリニック（門前シナリオ）　"
                    "🔵 病院　🔷 クリニック　🟢 競合薬局"
                )
                if has_rx_data:
                    st.success("✅ 競合薬局の処方箋枚数（MHLW）をマップに反映しています")
                else:
                    st.info("💡 近隣薬局の処方箋枚数は未取得です（STEP 3でオプションを有効化すると取得できます）")
                m = build_new_pharmacy_map(
                    new_result.config,
                    new_result.lat, new_result.lon,
                    new_result.nearby_medical, new_result.nearby_pharmacies,
                    new_result.commercial_radius, new_result.geocoder_source,
                )
                st_folium(m, width=None, height=520, use_container_width=True)
            else:
                st.warning("座標取得失敗のためマップを表示できません")
            render_competitor_table(new_result.nearby_medical, new_result.nearby_pharmacies, show_rx=True)
        with tab_preds:
            render_prediction_tabs(new_result.method1, new_result.method2)
        with tab_log:
            st.code("\n".join(new_result.search_log))


# ---------------------------------------------------------------------------
# 11. 分析実行関数
# ---------------------------------------------------------------------------

def run_analysis(candidate: PharmacyCandidate, try_mhlw_medical: bool) -> None:
    """既存薬局 フル分析"""
    log: List[str] = []
    progress = st.progress(0, text="分析開始…")

    # A: MHLW
    progress.progress(10, text="[1/6] MHLW: 薬局詳細を取得中…")
    scraper = MHLWScraper()
    scraper.initialize_session()
    detail, dmsg = scraper.get_pharmacy_detail(candidate)
    log.append(f"[MHLW詳細] {dmsg}")
    mhlw_rx = None
    pharmacy_address = candidate.address
    mhlw_url = candidate.href
    if detail:
        mhlw_rx = detail.get("prescriptions_annual")
        if detail.get("address"):
            pharmacy_address = detail["address"]
        mhlw_url = detail.get("source_url", candidate.href)
        log.append(f"  処方箋数: {mhlw_rx:,}枚/年" if mhlw_rx else "  処方箋数: 記載なし")

    # B: 密度
    progress.progress(20, text="[2/6] 住所から人口密度を計算中…")
    area_density, density_source = get_population_density(pharmacy_address)
    initial_r, _ = calc_commercial_radius(area_density, False, "")
    search_r = max(int(initial_r * 1.5), 600)
    log.append(f"[密度] {area_density:,}人/km²（{density_source}）")

    # C: ジオコーディング（GSI優先）
    progress.progress(30, text="[3/6] 座標を取得中（国土地理院）…")
    gc = GeocoderService()
    lat, lon, geo_msg, geo_src = gc.geocode(pharmacy_address)
    log.append(f"[Geocoding({geo_src})] {geo_msg}")

    # D: Overpass
    nearby_medical, nearby_pharmacies = [], []
    if lat and lon:
        progress.progress(45, text=f"[4/6] 近隣施設を検索中（半径{search_r}m）…")
        time.sleep(0.5)
        ov = OverpassSearcher()
        nearby_medical, nearby_pharmacies, ov_msg = ov.search_nearby(lat, lon, search_r)
        log.append(f"[OSM] 半径{search_r}m → {ov_msg}")
        if try_mhlw_medical and nearby_medical:
            progress.progress(55, text="[4.5/6] 医療施設のMHLWデータ照会中…")
            for fac in nearby_medical[:5]:
                aop = scraper.get_medical_outpatient_data(fac.name)
                if aop:
                    fac.mhlw_annual_outpatients = aop
                    fac.daily_outpatients = aop // NATIONAL_STATS["working_days"]
                time.sleep(0.5)

    # E: 門前判定・商圏半径確定
    progress.progress(65, text="[5/6] 門前判定・商圏半径確定…")
    is_gate, gate_reason = detect_gate_pharmacy(candidate.name, nearby_medical)
    commercial_r, r_reason = calc_commercial_radius(area_density, is_gate, gate_reason)
    log.append(f"[門前] {is_gate} ({gate_reason}) → 半径{commercial_r}m")

    # F: 予測
    progress.progress(78, text="[6/6] 方法①②を計算中…")
    m1 = Method1Predictor().predict(lat or 0.0, lon or 0.0, nearby_medical, nearby_pharmacies) if lat else None
    m2 = Method2Predictor().predict(
        lat or 0.0, lon or 0.0, nearby_pharmacies, area_density, commercial_r,
        density_source=density_source, radius_reason=r_reason,
    )
    progress.progress(100, text="完了！")
    progress.empty()

    st.session_state["analysis"] = FullAnalysis(
        pharmacy_name=candidate.name,
        pharmacy_address=pharmacy_address,
        pharmacy_lat=lat or 0.0,
        pharmacy_lon=lon or 0.0,
        geocode_display=geo_msg,
        geocoder_source=geo_src,
        mhlw_annual_rx=mhlw_rx,
        mhlw_source_url=mhlw_url,
        method1=m1, method2=m2,
        nearby_medical=nearby_medical,
        nearby_pharmacies=nearby_pharmacies,
        area_density=area_density,
        area_density_source=density_source,
        commercial_radius=commercial_r,
        commercial_radius_reason=r_reason,
        is_gate_pharmacy=is_gate,
        gate_pharmacy_reason=gate_reason,
        search_log=log,
    )
    st.rerun()


def run_new_pharmacy_analysis(config: NewPharmacyConfig) -> None:
    """新規開局予測 フル分析"""
    log: List[str] = []
    progress = st.progress(0, text="新規開局予測を開始…")

    # A: 密度計算
    progress.progress(10, text="[1/5] 住所から人口密度を計算中…")
    area_density, density_source = get_population_density(config.address)
    initial_r, _ = calc_commercial_radius(area_density, False, "")
    search_r = max(int(initial_r * 1.5), 600)
    log.append(f"[密度] {area_density:,}人/km²（{density_source}）")

    # B: ジオコーディング（GSI優先）
    progress.progress(20, text="[2/5] 座標を取得中（国土地理院）…")
    gc = GeocoderService()
    lat, lon, geo_msg, geo_src = gc.geocode(config.address)
    log.append(f"[Geocoding({geo_src})] {geo_msg}")

    # C: Overpass
    nearby_medical, nearby_pharmacies = [], []
    if lat and lon:
        progress.progress(40, text=f"[3/5] 近隣施設を検索中（半径{search_r}m）…")
        time.sleep(0.5)
        ov = OverpassSearcher()
        nearby_medical, nearby_pharmacies, ov_msg = ov.search_nearby(lat, lon, search_r)
        log.append(f"[OSM] 半径{search_r}m → {ov_msg}")

        # 近隣薬局のMHLWデータ取得（オプション）
        if config.fetch_nearby_rx and nearby_pharmacies:
            progress.progress(55, text="[3.5/5] 近隣薬局のMHLW処方箋枚数を取得中…")
            scraper = MHLWScraper()
            scraper.initialize_session()
            rx_data = scraper.get_rx_for_nearby_pharmacies(
                [p.name for p in nearby_pharmacies], limit=5
            )
            for ph in nearby_pharmacies:
                if ph.name in rx_data and rx_data[ph.name]:
                    ph.mhlw_annual_outpatients = rx_data[ph.name]
                    log.append(f"  [近隣薬局MHLW] {ph.name}: {rx_data[ph.name]:,}枚/年")

    # D: 門前判定・商圏半径確定
    progress.progress(65, text="[4/5] 門前判定・商圏半径を確定中…")
    if config.scenario in ("gate_clinic", "both"):
        # 門前シナリオの場合は強制的に門前扱い
        is_gate, gate_reason = True, f"門前クリニック誘致シナリオを選択"
        commercial_r, r_reason = 300, "門前クリニック誘致 → 医療機関依存型のため300m固定"
    else:
        is_gate, gate_reason = detect_gate_pharmacy(config.pharmacy_name, nearby_medical)
        commercial_r, r_reason = calc_commercial_radius(area_density, is_gate, gate_reason)
    log.append(f"[商圏] 半径{commercial_r}m（{r_reason}）")

    # E: 予測
    progress.progress(80, text="[5/5] 予測計算中…")
    method1 = None
    if config.scenario in ("gate_clinic", "both") and lat:
        # 仮想クリニック（開局予定地の25m北に配置）
        virtual_clinic = NearbyFacility(
            name=f"[誘致予定] {config.gate_specialty}クリニック",
            facility_type="clinic",
            lat=(lat + 0.000225),  # ~25m北
            lon=lon,
            distance_m=25,
            specialty=config.gate_specialty,
            daily_outpatients=config.gate_daily_outpatients,
            has_inhouse_pharmacy=config.gate_has_inhouse,
        )
        # 実際の近隣医療施設も加える（合計シェア計算のため）
        all_medical = [virtual_clinic] + nearby_medical
        method1 = Method1Predictor().predict(
            lat, lon, all_medical, nearby_pharmacies,
            mode_label="シナリオA: 門前クリニック誘致"
        )
        log.append(f"[シナリオA] 推計: {method1.annual_rx:,}枚/年")

    method2 = None
    if config.scenario in ("catchment", "both"):
        method2 = Method2Predictor().predict(
            lat or 0.0, lon or 0.0, nearby_pharmacies,
            area_density, commercial_r,
            density_source=density_source, radius_reason=r_reason,
        )
        log.append(f"[シナリオB] 推計: {method2.annual_rx:,}枚/年")

    progress.progress(100, text="完了！")
    progress.empty()

    st.session_state["new_result"] = NewPharmacyResult(
        config=config,
        lat=lat, lon=lon,
        geocode_display=geo_msg,
        geocoder_source=geo_src,
        area_density=area_density,
        area_density_source=density_source,
        commercial_radius=commercial_r,
        commercial_radius_reason=r_reason,
        is_gate=is_gate,
        gate_reason=gate_reason,
        nearby_medical=nearby_medical,
        nearby_pharmacies=nearby_pharmacies,
        method1=method1,
        method2=method2,
        search_log=log,
    )
    st.rerun()


if __name__ == "__main__":
    main()
