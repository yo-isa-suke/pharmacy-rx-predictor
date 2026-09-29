# -*- coding: utf-8 -*-
"""
商圏内 調剤薬局リストアップ（薬局ファインダー） ― v2.1
============================================================
住所と商圏半径を入力すると、圏内の調剤薬局を一覧表示する（門前/面・年間処方箋数つき）。
旧名：pharmacy_area_finder（〜v1.5.1）。

v2.1 変更点 (2026-09-28): ―― 「ナビィが止まっても動く」「目視確認を差分だけにする」
（薬局出店分析ツール v2.1〜v2.2 と同じ仕組みを取り込んだ）
1. 厚労省「医療情報ネットのオープンデータ」（全国の薬局・医療機関、緯度経度付き、半年ごと更新）を
   一覧の土台にした。ナビィが止まっていても一覧と座標・門前/面は出せる。
   初回にこのファイルと同じフォルダの rx_tool_data/ へ自動ダウンロードする（1〜2分・初回のみ）。
2. ナビィ詳細ページ（処方箋数）の前回取得分を保存し、取れないときはそれを使う（出典に「前回取得」）。
3. 地方厚生局「コード内容別医療機関一覧表」（保険薬局の公式名簿・毎月更新）と照合する。
   名簿にだけある店は自動追加、休止・名簿に無い店は🟠、同名同住所の重複登録は1件に統合。
   結果は「ログ・取りこぼし診断」タブに差分表として出す（目視確認はこの差分だけでよい）。
4. ナビィ構造チェックをサイドバーに常時表示（ナビィのHTML変更をすぐ検知できる）。
5. Streamlit Cloud 対策：ナビィ用スクレイパーを利用者ごとに分けた（旧版は全利用者で共有しており、
   同時に検索すると結果が混ざるおそれがあった）。
6. 門前判定用の医療機関は公式データの座標を使い、詳細ページを取りに行かない（速くなった）。
7. 検索後にスライダーを動かすと、半径の表示とCSVのファイル名がずれる不具合を修正。
8. 一覧・CSVに「週営業日数」（公式データ）「名簿照合」「データ元」を追加。

旧版の変更履歴は pharmacy_area_finder_v1.5.py を参照。
データソース：厚労省 医療情報ネット（ナビィ）とそのオープンデータ、地方厚生局、OpenStreetMap、国土地理院。
単体で動作します（実行には requirements.txt の依存ライブラリが必要）。
"""
import streamlit as st

st.set_page_config(page_title="商圏内 調剤薬局リストアップ v2.1", page_icon="💊", layout="wide")

# ════════════ 共通部品（薬局出店分析ツール v2.2 と同じコード。単独で動くよう取り込み） ════════════
# ※ 元ファイルのモジュール説明はコメント化（Streamlitのマジック表示で本文に出るのを防ぐため削除）。
import csv
import gzip
import io
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import unicodedata
import urllib.parse
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# ─── 定数 ─────────────────────────────────────────────────────────────────────
MHLW_DOMAIN = "https://www.iryou.teikyouseido.mhlw.go.jp"
MHLW_BASE   = MHLW_DOMAIN + "/znk-web"
WORKING_DAYS = 305

# ─── v1.3: 取得の並列度とデータ品質フラグのしきい値 ───────────────────────────
FETCH_WORKERS_DEFAULT = 8     # ナビィ詳細ページの同時取得数（固定値）
OSM_BUDGET_S = 30             # OSM(Overpass)の待ち時間の上限。超えたらナビィのみで続行

# ─── v1.4: 取りこぼし対策のパラメータ ─────────────────────────────────────────
PAGE_SIZE = 20                # ナビィ一覧の1ページあたり件数
MAX_PAGES_DEFAULT = 40        # 一覧の取得ページ上限（= 800件。実運用ではまず届かない）
DEDUP_GAP_M = 60.0            # 同名施設を「同一」とみなす最大距離

# ─── v2.1: 公式データ（オープンデータ・厚生局名簿）とナビィ前回取得分の保存先 ─────
# このファイルと同じフォルダに自動で作る。消しても次回起動時に取り直すだけ。
DATA_DIR = Path(__file__).resolve().parent / "rx_tool_data"
OD_PAGE_URL = ("https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/"
               "newpage_43373.html")
OD_FILE_BASE = "https://www.mhlw.go.jp/content/11121000/"
OD_RECHECK_DAYS = 30          # この日数ごとに新しい版のオープンデータが出ていないか確認
KB_RECHECK_DAYS = 30          # 厚生局名簿の再取得間隔（名簿は毎月更新）
KB_DOMAIN = "https://kouseikyoku.mhlw.go.jp"
HTTP_UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"),
           "Accept-Language": "ja-JP,ja;q=0.9"}

# ナビィの「中心からの距離」指定コード。"00"=1km以内, "01"=5km以内, ""=指定なし。
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
OP_LOW_THR_DEFAULT    = 10    # 外来患者数がこの人数以下なら要確認（過小・未報告疑い）
OP_HIGH_THR_DEFAULT   = 500   # 外来患者数がこの人数以上なら要確認（月間・年間値の混入疑い）
# ナビィに外来患者数が入力されていない場合の出典表記（"不明"の理由を残す）
OP_SRC_BLANK    = "ナビィ未入力（外来欄が空欄）"
OP_SRC_MISSING  = "ナビィ未入力（該当項目なし）"
OP_SRC_LAYOUT   = "ナビィ未入力（列構成が想定外・要確認）"

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


# v2.2: osm.ch はスイス限定データ（日本の検索が「成功したのに0件」になる）ため削除。
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
# python-requests の既定UAは overpass-api.de に 406 で拒否されるため、識別できるUAを付ける。
OVERPASS_HEADERS = {"User-Agent": "PharmacySiteAnalyzer/2.2 (retail pharmacy analysis tool)"}

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
    # ── v2.1: 公式データ由来 ──
    od_op_days: Optional[float] = None     # オープンデータの週診療日数（ナビィで取れないときの控え）
    review_note: str = ""                  # 厚生局名簿との突き合わせで要確認になった理由
    # ── v2.2: 外来患者数「不明」の参考値 ──
    kb_doctors_ft: Optional[float] = None  # 厚生局名簿の常勤医師数
    kb_doctors_pt: Optional[float] = None  # 厚生局名簿の非常勤医師数
    kb_depts: str = ""                     # 厚生局名簿の診療科（略称を展開したもの）
    ref_op: Optional[int] = None           # 外来患者数の参考値（人/日）
    ref_op_basis: str = ""                 # 参考値の根拠
    op_ref_used: bool = False              # 計算に参考値を採用したか


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
    review_note: str = ""                  # v2.1: 厚生局名簿との突き合わせで要確認になった理由
    od_op_days: Optional[float] = None     # v2.2: 公式オープンデータの週営業日数


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
    r"公益社団法人|一般社団法人|公益財団法人|一般財団法人|株式会社|有限会社|合同会社|"
    r"\((?:株|有|医|医社|医財|社|財|合)\))"     # v2.1: ㈱㈲（有）等の略記（NFKC後は (株) 等）
)


# v2.1: 名簿ごとに旧字・異体字の使い方が違う（例：小沢／小澤）ため、比較前にそろえる
_ITAIJI = str.maketrans({"澤": "沢", "邊": "辺", "邉": "辺", "齋": "斎", "齊": "斉", "髙": "高",
                         "﨑": "崎", "嵜": "崎", "濱": "浜", "廣": "広", "櫻": "桜", "國": "国",
                         "藥": "薬", "醫": "医", "德": "徳", "惠": "恵", "眞": "真", "瀨": "瀬",
                         "龍": "竜", "榮": "栄", "壽": "寿", "實": "実", "縣": "県", "淺": "浅",
                         "ヶ": "ケ", "ヵ": "カ"})


def normalize_name(s: str) -> str:
    """全角/半角・法人格・記号を落として施設名を正規化する。"""
    s = unicodedata.normalize("NFKC", s or "").translate(_ITAIJI)
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
    """機関コードの先頭桁から kikanKbn の候補を優先順に返す。

    v1.4: 旧版は先頭桁ごとに2つしか返さなかった（例: "3"→[3,2]）。区分を外すと
    詳細ページが取れず、住所も座標も得られないまま商圏判定から丸ごと抜け落ちる
    ため、確度の高い順に並べたうえで残りの区分もフォールバックとして必ず付ける。
    """
    prefix = kikan_cd[0] if kikan_cd else "2"
    order = list(KIKAN_KBN_MAP.get(prefix, [2, 1, 3]))
    for k in (2, 1, 3, 4, 5):
        if k not in order:
            order.append(k)
    return order


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
                r = requests.post(url, data={"data": query},
                                  headers=OVERPASS_HEADERS, timeout=timeout)
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
    # v1.4: relation（建物としてマッピングされた大型店）と healthcare=pharmacy /
    # dispensing=yes を追加。旧版は node/way の amenity=pharmacy と shop=chemist
    # だけを見ていたため、これらのタグしか付いていない薬局が丸ごと漏れていた。
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
    # 発行元なので必ず入れる。healthcare=yes を除外していたのもやめた（除外すると
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
    # 薬局キーワードフィルター（名前ベース）。v1.4: 名前だけで判定すると
    # 「くすりの木内科クリニック」のような実在の医院まで落ちるため、
    # 医療機関らしい語を含む場合は除外しない。
    _PHARMA_RE = re.compile(
        r'薬局|ドラッグ|ファーマシー|調剤|drug\s*store|pharmacy', re.IGNORECASE
    )
    _MED_RE = re.compile(
        r'医院|クリニック|診療所|病院|歯科|内科|外科|眼科|皮膚科|小児科|産婦人科|'
        r'耳鼻|泌尿器|整形|心療|精神|リハビリ|医療センター|保健'
    )

    data = _overpass_post(query)
    if not data:
        return []
    facilities: List[MedFacility] = []
    seen_ids: set = set()
    for el in data.get("elements", []):
        key = (el.get("type"), el.get("id"))
        if key in seen_ids:
            continue
        seen_ids.add(key)
        tags = el.get("tags", {})
        name = tags.get("name:ja") or tags.get("name", "")
        if not name:
            continue
        # 薬局・ドラッグストアを除外（まずタグで、次に名前で）
        if (tags.get("amenity") == "pharmacy" or tags.get("shop") == "chemist"
                or tags.get("healthcare") in ("pharmacy", "chemist", "dispensary")):
            continue
        if _PHARMA_RE.search(name) and not _MED_RE.search(name):
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
        elif amenity == "dentist" or healthcare == "dentist" or "dentist" in sp_en:
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



_BLANK_CELL_RE = re.compile(r"^[－\-−—―ー\s\u3000]*$")


def _is_blank_cell(v: str) -> bool:
    """ナビィの「－」「-」「（空欄）」＝未入力セルか。"""
    return not v or bool(_BLANK_CELL_RE.match(v))


def _cell_num(v: str) -> Optional[float]:
    """セル文字列から人数を取り出す。0以下・10,000超は無効（Noneを返す）。"""
    m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", v or "")
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return n if 0 < n <= 10_000 else None


def _extract_outpatient_from_table(table) -> Tuple[Optional[int], str]:
    """患者数統計表から「前年度・1日平均 外来患者数」だけを厳密に取り出す。

    v1.3: 「見出しに“外来患者”列がある」かつ「行ラベルに“前年度”がある」の2条件が
    揃った表・行のセルだけを採用する。該当セルが「－」等の空欄なら (None, "blank")
    を返し、他の列・他の表から数字を拾いに行かない。
    （旧版は任意の表・任意の行から最初の数値を拾うフォールバックがあり、
      ナビィに外来数の記載が無い施設に別項目の数値が入る事故があった。）

    戻り値: (人数, 状態)  状態 "ok"=取得 / "blank"=ナビィ未入力 / "none"=該当表なし
    """
    rows = table.find_all("tr")
    if not rows:
        return None, "none"

    # ── 見出し行（先頭2行まで）から「外来患者」列を探す ──────────────────
    gairai_col: Optional[int] = None
    header_len = 0
    for hrow in rows[:2]:
        cells = hrow.find_all(["th", "td"])
        texts = [re.sub(r"\s+", "", c.get_text(strip=True)) for c in cells]
        for i, h in enumerate(texts):
            if h.startswith("外来患者") and not any(ng in h for ng in ("月平均", "紹介", "延")):
                gairai_col, header_len = i, len(cells)
                break
        if gairai_col is not None:
            break
    if gairai_col is None:
        return None, "none"

    # ── 「前年度」行の外来列セルを読む ────────────────────────────────
    for row in rows:
        cells = row.find_all(["th", "td"])
        if not cells:
            continue
        if "前年度" not in cells[0].get_text(strip=True):
            continue
        col = gairai_col
        if col >= len(cells):
            # 見出し行に空の角セルがある等の列ズレを1段だけ補正（それ以上は推測しない）
            off = header_len - len(cells)
            if off <= 0 or col - off < 1:
                return None, "none"
            col -= off
        val = cells[col].get_text(strip=True)
        if _is_blank_cell(val):
            return None, "blank"      # ★ ナビィに数字が無い → 不明で確定
        n = _cell_num(val)
        if n is not None:
            return int(round(n)), "ok"
        return None, "blank"
    return None, "none"



def _parse_outpatients_from_stats_table(soup: BeautifulSoup) -> Tuple[Optional[int], str]:
    """詳細ページの「患者数」セクションの表だけを対象に外来患者数を読む（v1.3で対象を限定）。"""
    status = "none"
    # ① 見出しが「患者数」の item ブロック内の表（本来の場所）
    for item_div in soup.find_all("div", class_="item"):
        h = item_div.find(["h2", "h3"])
        if not h or "患者数" not in h.get_text(strip=True):
            continue
        for table in item_div.find_all("table"):
            val, st_ = _extract_outpatient_from_table(table)
            if val is not None:
                return val, "ok"
            if st_ == "blank":
                status = "blank"
    if status == "blank":
        return None, "blank"
    # ② 「前年度」行を持つ表（患者数セクションの見出しが変わった場合の保険）
    for data_th in soup.find_all("th", class_="ptn4ItemName"):
        if "前年度" not in data_th.get_text(strip=True):
            continue
        table = data_th.find_parent("table")
        if table is None:
            continue
        val, st_ = _extract_outpatient_from_table(table)
        if val is not None:
            return val, "ok"
        if st_ == "blank":
            status = "blank"
    # 注: v1.2にあった「ページ内の全テーブルを総当たり」は誤取得の温床のため廃止。
    return None, status



def _parse_daily_outpatients(
    fields: Dict[str, str],
    full_text: str,
    soup: Optional[BeautifulSoup] = None,
) -> Tuple[Optional[int], str]:
    """1日平均外来患者数を返す (人数, 出典)。取得できなければ (None, 未入力の理由)。

    v1.3の方針: 「ナビィに数字が入っていない施設は必ず不明」。
    確実に外来患者数を指す3経路（①患者数統計表の外来列 ②前年度フィールドの外来列
    ③外来患者数を明示したキー）だけを見る。列がズレている・空欄・項目が無い場合は
    推測せず不明を返す。旧版の以下のフォールバックは廃止した:
      × ページ内の任意の表から最初の数値を拾う
      × 列数が想定外のとき末尾寄りの列を外来とみなす
      × 3,000超の値を「年間値」とみなして÷305して1日値にする
      × 本文テキストの緩い正規表現（別項目の数値を拾う）
    """
    # ① 患者数統計表（最も信頼できる）
    if soup is not None:
        val, status = _parse_outpatients_from_stats_table(soup)
        if val is not None:
            return val, "ナビィ（実績統計表・外来列）"
        if status == "blank":
            return None, OP_SRC_BLANK

    # ② 「前年度１日平均患者数」フィールド（多列：入院6列 / 外来 / 歯科）の外来列＝index 6
    v_zennen = fields.get("前年度１日平均患者数", "")
    if v_zennen:
        cells = [c.strip() for c in v_zennen.split("/")]
        if len(cells) >= 7:
            cell = cells[6]
            if _is_blank_cell(cell):
                return None, OP_SRC_BLANK
            n = _cell_num(cell)
            if n is not None:
                return int(round(n)), "ナビィ（前年度フィールド・外来列）"
            return None, OP_SRC_BLANK
        return None, OP_SRC_LAYOUT     # 列構成が想定外＝推測しない

    # ③ 外来患者数を明示したキー（完全一致のみ。部分一致は別指標を拾うため使わない）
    exact_keys = [
        "1日あたりの外来患者の平均数", "１日あたりの外来患者の平均数",
        "1日平均外来患者数", "１日平均外来患者数",
        "一日平均外来患者数", "前年度の１日平均外来患者数", "前年度１日平均外来患者数",
        "外来患者数（1日平均）", "外来（1日平均）",
    ]
    for k in exact_keys:
        v = fields.get(k)
        if not v:
            continue
        if _is_blank_cell(v):
            return None, OP_SRC_BLANK
        m = re.search(r"([0-9][0-9,]*\.?[0-9]*)\s*人", v) or re.fullmatch(
            r"\s*([0-9][0-9,]*\.?[0-9]*)\s*", v)
        if m:
            try:
                n = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            if 0 < n <= 10_000:
                return int(round(n)), f"ナビィ（{k}）"

    # ④ 本文テキストは「1日あたりの外来患者の平均数 ○○人」という完全な言い回しのみ許容
    m = re.search(r"[1１]日あたりの外来患者の平均数[^0-9]{0,10}([0-9][0-9,]*\.?[0-9]*)\s*人", full_text)
    if m:
        try:
            n = float(m.group(1).replace(",", ""))
            if 0 < n <= 10_000:
                return int(round(n)), "ナビィ（本文・1日あたりの外来患者の平均数）"
        except ValueError:
            pass

    return None, OP_SRC_MISSING


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
    # v1.2: ナビィ新レイアウトでは診療科が「◆整形外科」のような見出しで
    # 診療時間セクションに載るため、まず ◆科名 を拾う。
    marks = re.findall(r"◆\s*([一-龥ぁ-んァ-ヶーA-Za-z・]{1,15}科)", full_text)
    if marks:
        seen, sp_list = set(), []
        for mname in marks:
            if mname not in seen:
                seen.add(mname)
                sp_list.append(mname)
        return "、".join(sp_list[:6])
    candidate_keys = ["診療科目", "診療科", "標榜診療科", "診療科名"]
    v = _get_field(fields, candidate_keys)
    # v1.2: 「診療時間（診療科目別の）」等が部分一致で拾われ時刻文字列が
    # 混入するようになったため、時刻らしき値は診療科として採用しない。
    if v and not re.search(r"\d{1,2}:\d{2}", v):
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
      - 月間値らしき高値（診療所で500人/日超）
    補正候補は年間値÷305日で算出（週診療日数は誤登録がありうるため固定日数を使う）。
    ※ 「10人以下 / 不明 / 500人以上」の3フラグは clinic_flag() 側で
       サイドバーのしきい値を使って判定する（ここは構造的な異常値のみ）。
    """
    # raw多列フィールドの外来列（キャップで弾かれた大きな生値も見る）
    raw6: Optional[float] = None
    raw = fac.raw_fields.get("前年度１日平均患者数", "") if fac.raw_fields else ""
    if raw:
        cells = [c.strip() for c in raw.split("/")]
        if len(cells) >= 7 and not _is_blank_cell(cells[6]):
            m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", cells[6])
            if m:
                try:
                    raw6 = float(m.group(1).replace(",", ""))
                except ValueError:
                    raw6 = None

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
    return ("", None)


# ─── MHLWスクレイパー ──────────────────────────────────────────────────────────
# ════════════════════ v2.1: 公式データ層 ════════════════════
# ナビィのライブ画面だけに頼らないための3つの部品。
#   ① NaviiSnapshotStore … ナビィ詳細ページの前回取得分（取得失敗時の控え）
#   ② オープンデータ     … 厚労省が公開する全国の施設一覧（リストと座標の土台）
#   ③ 厚生局名簿        … 保険医療機関・保険薬局の公式名簿（漏れの答え合わせ）
_DATA_LOCK = threading.Lock()


def _ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def _http_get(url: str, timeout: int = 60) -> requests.Response:
    r = requests.get(url, headers=HTTP_UA, timeout=timeout)
    r.raise_for_status()
    return r


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, obj: dict) -> None:
    _ensure_data_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def _is_stale(iso_ts: Optional[str], days: int) -> bool:
    try:
        return datetime.now() - datetime.fromisoformat(iso_ts) > timedelta(days=days)
    except Exception:
        return True


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── ① ナビィ詳細ページの前回取得分 ─────────────────────────────────────────
class NaviiSnapshotStore:
    """ナビィ詳細ページのHTMLを保存し、取得に失敗したときに前回分を返す。

    外来患者数・院外処方・薬局の処方箋数はナビィにしか無い。ナビィが止まった日に
    これらが全部「不明」になるのを防ぐため、1度取れたページは控えておく。
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (DATA_DIR / "navii_snapshots.sqlite3")
        self._lock = threading.Lock()
        self._ok = True
        try:
            _ensure_data_dir()
            self._exec("CREATE TABLE IF NOT EXISTS pages "
                       "(url TEXT PRIMARY KEY, fetched_at TEXT, html BLOB)")
        except Exception:
            self._ok = False

    def _exec(self, sql: str, args: tuple = (), fetch: bool = False):
        with self._lock:
            con = sqlite3.connect(str(self.path), timeout=10)
            try:
                cur = con.execute(sql, args)
                row = cur.fetchone() if fetch else None
                con.commit()
                return row
            finally:
                con.close()

    @staticmethod
    def key_of(url: str) -> str:
        """保存キー。URLそのものではなく施設コードで持つ（ナビィのURL体系が変わっても使えるように）。"""
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        if q.get("prefCd") and q.get("kikanCd"):
            return f"{q['prefCd']}:{q['kikanCd']}:{q.get('kikanKbn', '')}"
        return url

    def save(self, url: str, html: str) -> None:
        if not (self._ok and html):
            return
        try:
            self._exec("INSERT OR REPLACE INTO pages VALUES (?,?,?)",
                       (self.key_of(url), _now_iso(), gzip.compress(html.encode("utf-8"))))
        except Exception:
            pass

    def load(self, url: str) -> Optional[Tuple[str, str]]:
        """(html, 取得日時) を返す。無ければ None。"""
        if not self._ok:
            return None
        try:
            row = self._exec("SELECT html, fetched_at FROM pages WHERE url=?",
                             (self.key_of(url),), fetch=True)
            if row:
                return gzip.decompress(row[0]).decode("utf-8"), row[1]
        except Exception:
            pass
        return None

    def count(self) -> int:
        if not self._ok:
            return 0
        try:
            row = self._exec("SELECT COUNT(*) FROM pages", fetch=True)
            return int(row[0]) if row else 0
        except Exception:
            return 0


# ── ② 厚労省オープンデータ ───────────────────────────────────────────────
# 半年ごと（6/1・12/1時点）に全国分のCSVが公開される。IDの構成は
#   都道府県コード(2桁) ＋ 機関区分(1桁: 1病院/2診療所/3歯科/5薬局) ＋ ナビィの機関コード
# で、ナビィの kikanCd と完全一致する（2026-09 に甲府市40施設で全件一致を確認）。
OD_KINDS = {
    "pharmacy":       "05_pharmacy",
    "hospital":       "01-1_hospital_facility_info",
    "clinic":         "02-1_clinic_facility_info",
    "dental":         "03-1_dental_facility_info",
    "hospital_hours": "01-2_hospital_speciality_hours",
    "clinic_hours":   "02-2_clinic_speciality_hours",
    "dental_hours":   "03-2_dental_speciality_hours",
}
OD_MED_KINDS = ["hospital", "clinic", "dental"]
# 施設リストに使う医療機関の種類。ナビィの医療機関検索は歯科を含まない（2026-09に甲府で
# ナビィ＝病院+診療所の件数が1km・5kmとも完全一致を確認）ため、予測がv1.4と変わらない
# よう歯科は含めない。
OD_LIST_MED_KINDS = ["hospital", "clinic"]
_OD_META = "opendata_meta.json"
_WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def od_discover_latest() -> Optional[Tuple[str, Dict[str, str]]]:
    """厚労省のページから最新時点(YYYYMMDD)と各ファイルのURLを得る。"""
    html = _http_get(OD_PAGE_URL, timeout=30).text
    best: Dict[str, Tuple[str, str]] = {}
    for href in re.findall(r'href="([^"]+?\.zip)"', html):
        fn = href.rsplit("/", 1)[-1]
        for key, prefix in OD_KINDS.items():
            m = re.match(re.escape(prefix) + r"_(\d{8})(?:\.csv)?\.zip$", fn)
            if m and (key not in best or m.group(1) > best[key][0]):
                best[key] = (m.group(1), urllib.parse.urljoin(OD_PAGE_URL, href))
    if "pharmacy" not in best:
        return None
    date = best["pharmacy"][0]
    # 施設票と診療時間票は同じ時点のものだけを組み合わせる
    return date, {k: u for k, (d, u) in best.items() if d == date}


def _read_zip_csv(content: bytes, wanted: set, chunksize: Optional[int] = None):
    """zip内のCSVを必要な列だけ読む。chunksize を渡すと分割して読む（メモリ節約）。"""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        with z.open(name) as fh:
            if chunksize is None:
                return pd.read_csv(fh, encoding="utf-8-sig", dtype=str,
                                   usecols=lambda c: c in wanted)
            parts = [c for c in pd.read_csv(fh, encoding="utf-8-sig", dtype=str,
                                            usecols=lambda c: c in wanted,
                                            chunksize=chunksize)]
            return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def od_build(date: str, urls: Dict[str, str], note=None) -> Path:
    """オープンデータをダウンロードし、ツールが使う列だけの軽いファイルにまとめる。"""
    frames = []
    for kind in ["pharmacy"] + OD_MED_KINDS:
        if kind not in urls:
            continue
        if note:
            note(f"公式オープンデータ（{kind}）をダウンロード中…")
        name_col = "名称" if kind == "pharmacy" else "正式名称"
        wanted = {"ID", name_col, "機関区分", "所在地", "所在地座標（緯度）", "所在地座標（経度）"}
        if kind == "pharmacy":
            wanted |= {f"営業日（{d}）" for d in _WEEKDAYS}
        df = _read_zip_csv(_http_get(urls[kind], timeout=300).content, wanted)
        df = df[df["ID"].notna() & (df["ID"].str.len() > 3)]
        out = pd.DataFrame({
            "kind": kind,
            "kbn": df["機関区分"].fillna(""),
            "pref": df["ID"].str[:2],
            "kikan_cd": df["ID"].str[3:],
            "name": df[name_col].fillna(""),
            "address": df["所在地"].fillna(""),
            "lat": pd.to_numeric(df["所在地座標（緯度）"], errors="coerce"),
            "lon": pd.to_numeric(df["所在地座標（経度）"], errors="coerce"),
            "specialties": "",
            "op_days": float("nan"),
        })
        if kind == "pharmacy":
            out["op_days"] = sum((df[f"営業日（{d}）"] == "1").astype(int) for d in _WEEKDAYS).values
        elif f"{kind}_hours" in urls:
            if note:
                note(f"公式オープンデータ（{kind}の診療科・診療時間）をダウンロード中…")
            # 診療時間票は最大130MBあるため分割して読む（Streamlit Cloudのメモリ上限対策）
            h = _read_zip_csv(_http_get(urls[f"{kind}_hours"], timeout=300).content,
                              {"ID", "診療科目名"} | {f"{d}_診療開始時間" for d in _WEEKDAYS},
                              chunksize=200_000)
            spec = h.groupby("ID")["診療科目名"].agg(
                lambda s: "・".join(dict.fromkeys(x for x in s.dropna() if x)))
            day_cols = [f"{d}_診療開始時間" for d in _WEEKDAYS if f"{d}_診療開始時間" in h]
            days = h[day_cols].notna().groupby(h["ID"]).any().sum(axis=1)
            out["specialties"] = df["ID"].map(spec).fillna("").values
            out["op_days"] = pd.to_numeric(df["ID"].map(days), errors="coerce").values
        frames.append(out)
    allf = pd.concat(frames, ignore_index=True)
    # 座標が空・0・国外の行は除く（0,0 で登録されている施設がある）
    allf = allf[allf["lat"].between(20.0, 46.5) & allf["lon"].between(122.0, 154.5)]
    _ensure_data_dir()
    path = DATA_DIR / f"opendata_{date}.csv.gz"
    tmp = path.with_suffix(".tmp")
    allf.to_csv(tmp, index=False, compression="gzip")
    tmp.replace(path)
    for old in DATA_DIR.glob("opendata_*.csv.gz"):     # 古い版は消す（最新1版だけ保持）
        if old != path:
            try:
                old.unlink()
            except Exception:
                pass
    return path


def od_meta() -> dict:
    return _read_json(DATA_DIR / _OD_META)


def od_path() -> Optional[Path]:
    date = od_meta().get("date")
    p = DATA_DIR / f"opendata_{date}.csv.gz" if date else None
    return p if (p and p.exists()) else None


@st.cache_resource(show_spinner=False)
def _od_load(path_str: str, mtime: float) -> pd.DataFrame:
    return pd.read_csv(path_str, compression="gzip", dtype={
        "kind": str, "kbn": str, "pref": str, "kikan_cd": str,
        "name": str, "address": str, "specialties": str}, keep_default_na=False,
        na_values={"lat": [""], "lon": [""], "op_days": [""]})


def od_ensure(force_check: bool = False, note=None) -> Tuple[Optional[pd.DataFrame], str]:
    """オープンデータを用意して返す。ネットが駄目でも保存済みがあればそれを使う。"""
    with _DATA_LOCK:
        meta = od_meta()
        path = od_path()
        msg = ""
        if force_check or path is None or _is_stale(meta.get("checked_at"), OD_RECHECK_DAYS):
            try:
                found = od_discover_latest()
                if found is None:
                    msg = "厚労省ページでオープンデータのファイルが見つかりませんでした（ページ構成の変更の可能性）。"
                else:
                    date, urls = found
                    if path is None or date > str(meta.get("date", "")):
                        path = od_build(date, urls, note)
                        meta = {"date": date, "built_at": _now_iso()}
                    meta["checked_at"] = _now_iso()
                    _write_json(DATA_DIR / _OD_META, meta)
            except Exception as e:
                msg = f"オープンデータの確認・取得に失敗しました（{type(e).__name__}）。"
        if path is None:
            return None, (msg or "オープンデータ未取得。") + " ナビィのみで動作します。"
        df = _od_load(str(path), path.stat().st_mtime)
        d = str(meta.get("date", ""))
        label = f"{d[:4]}年{int(d[4:6])}月{int(d[6:])}日時点版" if len(d) == 8 else d
        return df, (f"公式オープンデータ {label}（{len(df):,}施設）" + (f" ※{msg}保存済みの版を使用" if msg else ""))


def od_nearby(df: pd.DataFrame, lat: float, lon: float, radius_m: float,
              kinds: List[str]) -> pd.DataFrame:
    dlat = radius_m / 111_000.0
    dlon = radius_m / (111_000.0 * max(math.cos(math.radians(lat)), 0.1))
    sub = df[df["kind"].isin(kinds)
             & df["lat"].between(lat - dlat, lat + dlat)
             & df["lon"].between(lon - dlon, lon + dlon)].copy()
    if sub.empty:
        sub["dist"] = []
        return sub
    la1, lo1 = math.radians(lat), math.radians(lon)
    la2 = sub["lat"].map(math.radians)
    lo2 = sub["lon"].map(math.radians)
    a = ((la2 - la1) / 2).map(math.sin) ** 2 + \
        math.cos(la1) * la2.map(math.cos) * ((lo2 - lo1) / 2).map(math.sin) ** 2
    sub["dist"] = 2 * 6_371_000 * a.map(lambda x: math.asin(math.sqrt(min(1.0, x))))
    return sub[sub["dist"] <= radius_m]


def _od_op_days(r) -> Optional[float]:
    try:
        v = float(r.op_days)
    except (TypeError, ValueError):
        return None
    return v if (v == v and 0 < v <= 7) else None


def od_row_to_med(r) -> MedFacility:
    kbn = int(r.kbn) if str(r.kbn) in ("1", "2", "3") else 2
    return MedFacility(
        name=r.name, address=r.address, pref_cd=r.pref, kikan_cd=r.kikan_cd, kikan_kbn=kbn,
        lat=float(r.lat), lon=float(r.lon), distance_m=float(r.dist), source="公式OD",
        specialties=r.specialties or "", weekly_op_days=_od_op_days(r), od_op_days=_od_op_days(r),
        facility_category={1: "病院", 3: "歯科診療所"}.get(kbn, "診療所"),
        coord_source="公式OD座標",
    )


def od_row_to_ph(r) -> PharmacyFacility:
    return PharmacyFacility(
        name=r.name, address=r.address, pref_cd=r.pref, kikan_cd=r.kikan_cd,
        lat=float(r.lat), lon=float(r.lon), distance_m=float(r.dist), source="公式OD",
        od_op_days=_od_op_days(r),
    )


# ── ③ 地方厚生局「コード内容別医療機関一覧表」 ─────────────────────────────
# 保険診療・保険調剤を行う施設の公式名簿（毎月更新）。処方箋を受け付ける薬局は必ず載る。
# ナビィ／オープンデータとは別体系のコード（保険医療機関コード）なので、突き合わせは
# 名称＋住所で行う。ファイルの置き場所は厚生局ごとに違うため、各局のページを辿って探す。
PREF_NAMES = ["北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県", "茨城県",
              "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県", "新潟県", "富山県",
              "石川県", "福井県", "山梨県", "長野県", "岐阜県", "静岡県", "愛知県", "三重県",
              "滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県", "鳥取県", "島根県",
              "岡山県", "広島県", "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
              "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"]
PREF_CODE_BY_NAME = {n: f"{i + 1:02d}" for i, n in enumerate(PREF_NAMES)}
BUREAUS = {   # slug: (名称, 管轄の都道府県コード)
    "hokkaido":       ("北海道厚生局", ["01"]),
    "tohoku":         ("東北厚生局", ["02", "03", "04", "05", "06", "07"]),
    "kantoshinetsu":  ("関東信越厚生局", ["08", "09", "10", "11", "12", "13", "14", "15", "19", "20"]),
    "tokaihokuriku":  ("東海北陸厚生局", ["16", "17", "21", "22", "23", "24"]),
    "kinki":          ("近畿厚生局", ["18", "25", "26", "27", "28", "29", "30"]),
    "chugokushikoku": ("中国四国厚生局", ["31", "32", "33", "34", "35"]),
    "shikoku":        ("四国厚生支局", ["36", "37", "38", "39"]),
    "kyushu":         ("九州厚生局", ["40", "41", "42", "43", "44", "45", "46", "47"]),
}
BUREAU_BY_PREF = {p: slug for slug, (_, prefs) in BUREAUS.items() for p in prefs}
KB_KINDS = {"yakkyoku": "薬局", "ika": "医科"}
KB_SEEDS = ["gyomu/gyomu/hoken_kikan/code_ichiran.html", "gyomu/gyomu/hoken_kikan/itiran.html",
            "gyomu/gyomu/hoken_kikan/index.html", "gyomu/gyomu/hoken_kikan/shitei.html",
            "chousa/shitei.html"]
KB_EXTRA_SEEDS = {    # 局ごとに置き場所が違うもの（2026-09時点で確認）
    "kinki": ["tyousa/shinkishitei.html"],
    "shikoku": ["gyomu/gyomu/hoken_kikan/shitei/index.html"],
    "kyushu": ["gyomu/gyomu/hoken_kikan/index_00006.html"],
}
# 保険医療機関コードの表記は局ごとに違う：01,0054,6 / 01・4067・1 / 010,058,1 / 01-00181 /
# 01-4036-7 / 0240059（区切りなし）
_KB_CODE_RE = re.compile(r"^\s*(?:\d{2,3}\s*[,，・\-－]\s*\d{3,5}\s*(?:[,，・\-－]\s*\d\s*)?|\d{7})$")
_KB_LABEL_RE = re.compile(r"(歯科[（(]医科併設[)）]|医科[（(]歯科併設[)）]|医科|歯科|薬局)")
_KB_DIR = "kouseikyoku"


def pref_code_of_text(s: str) -> Optional[str]:
    s = s or ""
    for n in sorted(PREF_NAMES, key=len, reverse=True):
        if s.startswith(n):
            return PREF_CODE_BY_NAME[n]
    return None


PREF_ROMAJI = ["hokkaido", "aomori", "iwate", "miyagi", "akita", "yamagata", "fukushima", "ibaraki",
               "tochigi", "gunma", "saitama", "chiba", "tokyo", "kanagawa", "niigata", "toyama",
               "ishikawa", "fukui", "yamanashi", "nagano", "gifu", "shizuoka", "aichi", "mie",
               "shiga", "kyoto", "osaka", "hyogo", "nara", "wakayama", "tottori", "shimane",
               "okayama", "hiroshima", "yamaguchi", "tokushima", "kagawa", "ehime", "kochi", "fukuoka",
               "saga", "nagasaki", "kumamoto", "oita", "miyazaki", "kagoshima", "okinawa"]


def _pref_from_filename(fn: str) -> str:
    """ファイル名から都道府県コードを推定する（漢字名・ローマ字名）。分からなければ ""。"""
    for n in sorted(PREF_NAMES, key=len, reverse=True):
        short = n if n == "北海道" else n[:-1]
        if short in fn:
            return PREF_CODE_BY_NAME[n]
    low = fn.lower()
    for i, r in sorted(enumerate(PREF_ROMAJI), key=lambda x: -len(x[1])):
        if re.search(rf"(^|[^a-z]){r}([^a-z]|$)", low):
            return f"{i + 1:02d}"
    return ""


def _kb_kind_of_name(fn: str) -> str:
    """ファイル名が示す名簿の種類。yakkyoku / ika / shika / ""（不明）。"""
    low = unicodedata.normalize("NFKC", fn).lower()
    if "yakkyoku" in low or "薬局" in low:
        return "yakkyoku"
    if "ikaheisetsu" in low or re.search(r"歯科[（(]医科併設", low):
        return "shika"                 # 歯科（医科併設）
    if "shikaheisetsu" in low or re.search(r"医科[（(]歯科併設", low):
        return "ika"                   # 医科（歯科併設）
    if "shika" in low or "歯科" in low:
        return "shika"
    if re.search(r"(^|[^a-z])ika([^a-z]|$)", low) or "医科" in low:
        return "ika"
    return ""


def _kb_classify(url: str, context: str) -> Optional[str]:
    """リンク先ファイルの種類。yakkyoku / ika / mixed（1ファイルに全種類）/ None。"""
    k = _kb_kind_of_name(url.rsplit("/", 1)[-1])
    if k:
        return None if k == "shika" else k
    labels = _KB_LABEL_RE.findall(re.sub(r"[\s　]", "", context))
    kinds = {("yakkyoku" if l == "薬局" else "ika" if l.startswith("医科") else "shika") for l in labels}
    if {"yakkyoku", "ika"} <= kinds:
        return "mixed"                 # 例：九州厚生局（県ごとのzipに医科・歯科・薬局が同梱）
    if not labels:
        return None
    last = labels[-1]
    return "yakkyoku" if last == "薬局" else ("ika" if last.startswith("医科") else None)


def _kb_version(fn: str) -> Tuple[int, int]:
    """ファイル名から版（年月）を読む。(種類, 値)。種類2=年月, 1=通し番号, 0=不明。"""
    for rx, conv in ((r"(?:^|[^a-z])r(\d{1,2})[_\-]?(\d{2})", lambda y, m: (2018 + int(y)) * 100 + int(m)),
                     (r"(?<!\d)(20\d{2})[.\-_]?(\d{1,2})(?:[_\-.]|$)", lambda y, m: int(y) * 100 + int(m)),
                     (r"^(\d{2})(\d{2})-", lambda y, m: (2000 + int(y)) * 100 + int(m))):
        m = re.search(rx, fn.lower())
        if m and 1 <= int(m.group(2)) <= 12:
            return 2, conv(m.group(1), m.group(2))
    m = re.search(r"(\d{6,})", fn)
    return (1, int(m.group(1))) if m else (0, 0)


def kb_discover(slug: str, kind: str) -> List[str]:
    """厚生局のページを辿って、最新版の名簿ファイル（xlsx/zip）のURLを返す。

    医科が「病院」「診療所」「医科（歯科併設）」のように複数ファイルに分かれている局が
    あるため、最新版に属するファイルは全部返す。"""
    found: List[str] = []
    visited: set = set()
    queue = [(f"{KB_DOMAIN}/{slug}/{s}", 0) for s in KB_EXTRA_SEEDS.get(slug, []) + KB_SEEDS]
    while queue and len(visited) < 14:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            r = requests.get(url, headers=HTTP_UA, timeout=20)
            if r.status_code != 200:
                continue
            r.encoding = r.apparent_encoding or "utf-8"
        except Exception:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urllib.parse.urljoin(url, a["href"])
            text = a.get_text(" ", strip=True)
            if re.search(r"\.(xlsx|zip)$", href, re.I):
                # 表の中のリンクは「行見出し（医科/歯科/薬局）」と「表題」で判定する
                # （施設基準の届出受理名簿・申請書様式など、別の名簿を拾わないため）
                tbl, tr = a.find_parent("table"), a.find_parent("tr")
                title = tbl.find("tr").get_text(" ", strip=True) if tbl and tbl.find("tr") else ""
                if re.search(r"届出受理|施設基準|申請|様式|届書|辞退|廃止|取消|新規", title + text):
                    continue
                if tr is not None:
                    ctx = tr.get_text(" ", strip=True)
                else:
                    ctx = " ".join(reversed([x.strip() for x in a.find_all_previous(string=True, limit=8)]))
                if _kb_classify(href, ctx + " " + text) in (kind, "mixed") and href not in found:
                    found.append(href)
            elif (depth == 0 and href.startswith(f"{KB_DOMAIN}/{slug}/") and href.endswith(".html")
                  and re.search(r"コード内容別|指定状況|指定一覧|指定等一覧|指定に関する", text)):
                queue.append((href, 1))
    if not found:
        return []
    vers = {u: _kb_version(u.rsplit("/", 1)[-1]) for u in found}
    # 版の読み方（年月／通し番号）が混在するときは、多いほうの読み方に揃える
    # （例：九州は通し番号の県別zipが並ぶ中に、古い年月付きファイルが1つだけ残っている）
    cnt: Dict[int, int] = {}
    for v in vers.values():
        cnt[v[0]] = cnt.get(v[0], 0) + 1
    best_kind = max(cnt, key=lambda k: (cnt[k], k))
    same = [u for u in found if vers[u][0] == best_kind]
    top = max(vers[u][1] for u in same)
    if best_kind == 2:
        return [u for u in same if vers[u][1] == top]
    if best_kind == 1:                 # 通し番号：最新の掲載分（番号が近いもの）だけ
        return [u for u in same if top - vers[u][1] <= 300]
    return same


def _zip_members(content: bytes) -> List[Tuple[str, bytes]]:
    out = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        for info in z.infolist():
            name = info.filename
            if not (info.flag_bits & 0x800):          # UTF-8フラグなし＝Shift_JISの日本語名
                try:
                    name = name.encode("cp437").decode("cp932")
                except Exception:
                    pass
            if name.lower().endswith(".xlsx"):
                out.append((name.rsplit("/", 1)[-1], z.read(info)))
    return out


# v2.2: 名簿の診療科は略称（内・小・整外・耳い…）なので、診療科判定に使えるよう展開する
_KB_DEPT_ABBR = {
    "内": "内科", "小": "小児科", "外": "外科", "整外": "整形外科", "形外": "形成外科",
    "皮": "皮膚科", "眼": "眼科", "耳い": "耳鼻いんこう科", "精": "精神科", "心内": "心療内科",
    "神": "神経科", "神内": "神経内科", "脳外": "脳神経外科", "産婦": "産婦人科", "産": "産科",
    "婦": "婦人科", "泌": "泌尿器科", "リハ": "リハビリテーション科", "呼": "呼吸器科",
    "呼内": "呼吸器内科", "循": "循環器科", "循内": "循環器内科", "消": "消化器科",
    "消内": "消化器内科", "胃": "胃腸科", "肛": "こう門科", "麻": "麻酔科", "放": "放射線科",
    "アレ": "アレルギー科", "リウ": "リウマチ科", "美外": "美容外科", "美皮": "美容皮膚科",
    "性": "性病科", "気食": "気管食道科", "病理": "病理診断科", "臨検": "臨床検査科",
    "救": "救急科", "歯": "歯科", "矯歯": "矯正歯科", "小歯": "小児歯科", "歯外": "歯科口腔外科",
}
_KB_BED_RE = re.compile(r"(一般|療養|精神|結核|感染)\s*(\d+)")


def _kb_parse_depts(txt: str) -> Tuple[str, Optional[int]]:
    """名簿の診療科セル（略称）を展開し、病床数（一般○床など）を合計する。"""
    txt = txt or ""
    beds = sum(int(m.group(2)) for m in _KB_BED_RE.finditer(txt)) or None
    txt = _KB_BED_RE.sub(" ", txt)
    names = []
    for tok in re.split(r"[\s　]+", txt):
        tok = tok.strip()
        if not tok or tok.isdigit():
            continue
        full = _KB_DEPT_ABBR.get(tok, tok)
        if full not in names:
            names.append(full)
    return "・".join(names), beds


def _kb_doctor_counts(block: str) -> Tuple[Optional[float], Optional[float]]:
    """「常　勤:　1 (医　1)」「非常勤: 3 (医 3)」から常勤・非常勤の医師数を読む。"""
    ft = pt = None
    m = re.search(r"(?<!非)常\s*勤\s*[:：][^（(]*[（(]\s*医\s*(\d+)", block)
    if m:
        ft = float(m.group(1))
    m = re.search(r"非\s*常\s*勤\s*[:：][^（(]*[（(]\s*医\s*(\d+)", block)
    if m:
        pt = float(m.group(1))
    return ft, pt


def _kb_parse_xlsx(content: bytes, file_pref: str) -> Tuple[List[dict], str]:
    """コード内容別一覧表（Excel）を行データにする。レイアウトの細かな差に耐えるよう、
    『NN,NNNN,N』形式の保険医療機関コードのセルを起点に、その右の名称・住所を読む。"""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out: List[dict] = []
    asof = ""
    for ws in wb.worksheets:
        rows = [[("" if v is None else str(v)).strip() for v in row]
                for row in ws.iter_rows(values_only=True)]
        sheet_pref = file_pref
        for i, cells in enumerate(rows):
            # ページ見出しの「[愛知県]」等があれば以後の行はその県（東北のように
            # 1ファイルに複数県が続けて入っている様式に対応）
            if i < 8 or not (cells and cells[0].isdigit()):
                m = re.search(r"\[(北海道|東京都|京都府|大阪府|.{2,3}県)\]", " ".join(cells[:4]))
                if m and m.group(1) in PREF_CODE_BY_NAME:
                    sheet_pref = PREF_CODE_BY_NAME[m.group(1)]
            if not asof and i < 12:
                m = re.search(r"(令和\s*\d+\s*年\s*\d+\s*月\s*\d+\s*日)\s*現在", " ".join(cells))
                if m:
                    asof = re.sub(r"\s+", "", m.group(1))
            idx = next((j for j, c in enumerate(cells)
                        if j >= 1 and cells[j - 1].isdigit() and _KB_CODE_RE.match(c)
                        and 6 <= len(re.sub(r"\D", "", c)) <= 8), None)
            if idx is None:
                continue
            rest = [c for c in cells[idx + 1:] if c]
            if not rest:
                continue
            name = re.sub(r"\s+", " ", rest[0])
            addr = next((c for c in rest[1:] if "〒" in c), rest[1] if len(rest) > 1 else "")
            addr = re.sub(r"^〒\s*[\d０-９]{3}\s*[-－ー―‐]\s*[\d０-９]{4}\s*", "", addr)
            near = " ".join(" ".join(r) for r in rows[i + 1:i + 3])
            status = "休止" if "休止" in near.split("〒")[0] else "現存"
            # v2.2: この施設の行ブロック（次の施設の行の手前まで）から医師数・診療科・病床を読む
            tail = []
            for r2 in rows[i + 1:i + 8]:
                if r2 and r2[0].isdigit():
                    break
                tail.append(r2)
            block = " ".join(" ".join(r) for r in [cells] + tail)
            ft, pt = _kb_doctor_counts(block)
            dcol = idx + 7          # 様式共通：コードの7列右が診療科（例「内　小」「整外　リハ」）
            dept_txt = " ".join(r[dcol] for r in [cells] + tail if len(r) > dcol and r[dcol])
            depts, beds = _kb_parse_depts(dept_txt)
            out.append({"code": re.sub(r"\D", "", cells[idx]), "name": name,
                        "address": addr.replace("\n", " "), "status": status,
                        "file_pref": sheet_pref, "doctors_ft": ft, "doctors_pt": pt,
                        "kb_depts": depts, "beds": beds})
    wb.close()
    return out, asof


KB_COLS = ["code", "name", "address", "status", "file_pref",
           "doctors_ft", "doctors_pt", "kb_depts", "beds"]
KB_SCHEMA = 2      # v2.2: 医師数・診療科・病床の列を追加。古い保存分は取り直す


def kb_parse_file(fn: str, content: bytes, pref: Optional[str] = None,
                  kind: Optional[str] = None) -> Tuple[pd.DataFrame, str]:
    """xlsx 1つ、または xlsx を含む zip を名簿データにする。
    pref / kind を渡すと、ファイル名で別の県・別の種類と分かるものは読まない（速度のため）。"""
    members = _zip_members(content) if fn.lower().endswith(".zip") else [(fn, content)]
    rows: List[dict] = []
    asof = ""
    for name, b in members:
        fp = _pref_from_filename(name)
        mk = _kb_kind_of_name(name)
        if pref and fp and fp != pref:
            continue
        if kind and mk and mk != kind:
            continue
        try:
            r, a = _kb_parse_xlsx(b, fp)
        except Exception:
            continue
        rows += r
        asof = asof or a
    return pd.DataFrame(rows, columns=KB_COLS), asof


def _kb_paths(slug: str, kind: str) -> Tuple[Path, Path]:
    d = DATA_DIR / _KB_DIR
    return d / f"{slug}_{kind}.csv.gz", d / f"{slug}_{kind}.json"


def kb_store(slug: str, kind: str, df: pd.DataFrame, meta: dict) -> None:
    p, m = _kb_paths(slug, kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=False, compression="gzip")
    _write_json(m, meta)


def kb_ensure(pref: str, kind: str, force: bool = False) -> Tuple[Optional[pd.DataFrame], str]:
    """都道府県の名簿を用意する（自動取得＋手動アップロード分）。失敗時は保存済みを使う。"""
    slug = BUREAU_BY_PREF.get(pref)
    if not slug:
        return None, "都道府県を判定できないため厚生局名簿は使いません"
    bname = BUREAUS[slug][0]
    p, m = _kb_paths(f"pref{pref}", kind)
    meta = _read_json(m)
    msg = ""
    if (force or not p.exists() or _is_stale(meta.get("fetched_at"), KB_RECHECK_DAYS)
            or meta.get("schema") != KB_SCHEMA):
        try:
            urls = kb_discover(slug, kind)
            if not urls:
                msg = f"{bname}のページで{KB_KINDS[kind]}の名簿ファイルが見つかりませんでした"
            else:
                frames, asof = [], ""
                for u in urls:
                    fn = u.rsplit("/", 1)[-1]
                    fp = _pref_from_filename(fn)
                    if fp and fp != pref:
                        continue
                    d, a = kb_parse_file(fn, _http_get(u, timeout=300).content, pref, kind)
                    frames.append(d)
                    asof = asof or a
                df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=KB_COLS)
                df = df[(df["file_pref"] == "") | (df["file_pref"] == pref)]
                if len(df):
                    meta = {"fetched_at": _now_iso(), "urls": urls, "asof": asof,
                            "rows": len(df), "bureau": bname, "schema": KB_SCHEMA}
                    kb_store(f"pref{pref}", kind, df, meta)
                else:
                    msg = f"{bname}の{KB_KINDS[kind]}名簿を読み取れませんでした（様式変更の可能性）"
        except Exception as e:
            msg = f"{bname}の{KB_KINDS[kind]}名簿の取得に失敗（{type(e).__name__}）"
    frames, parts = [], []
    if p.exists():
        frames.append(pd.read_csv(p, dtype=str, keep_default_na=False))
        parts.append(f"{bname}（{meta.get('asof') or meta.get('fetched_at', '')[:10]}・{meta.get('rows', '?')}件）")
    mp, mm = _kb_paths(f"manual_{pref}", kind)
    if mp.exists():
        mmeta = _read_json(mm)
        frames.append(pd.read_csv(mp, dtype=str, keep_default_na=False))
        parts.append(f"手動取込（{mmeta.get('asof') or mmeta.get('fetched_at', '')[:10]}）")
    if not frames:
        return None, msg or f"{bname}の{KB_KINDS[kind]}名簿なし"
    df = pd.concat(frames, ignore_index=True)
    for c in KB_COLS:                 # 古い様式の手動取込分にも列をそろえる
        if c not in df:
            df[c] = ""
    df = df.fillna("")
    df = df[(df["file_pref"] == "") | (df["file_pref"] == pref)]
    return df, "・".join(parts) + (f" ※{msg}。保存済みを使用" if msg else "")


def kb_status_all() -> List[Tuple[str, str, str]]:
    """保存済み名簿の一覧（サイドバー表示用）。"""
    out = []
    d = DATA_DIR / _KB_DIR
    if not d.exists():
        return out
    for mj in sorted(d.glob("*.json")):
        meta = _read_json(mj)
        slug, _, kind = mj.stem.rpartition("_")
        pc = slug.replace("manual_", "").replace("pref", "")
        pname = PREF_NAMES[int(pc) - 1] if pc.isdigit() and 0 < int(pc) <= 47 else pc
        who = f"{pname}（手動取込）" if slug.startswith("manual_") else pname
        out.append((who, KB_KINDS.get(kind, kind), meta.get("asof") or meta.get("fetched_at", "")[:10]))
    return out


# ── ④ 突き合わせ（名称＋住所） ─────────────────────────────────────────────
_KNUM = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _kanji_to_int(s: str) -> str:
    if "十" not in s:
        return "".join(str(_KNUM[c]) for c in s)
    hi, _, lo = s.partition("十")
    return str((_KNUM.get(hi, 1) if hi else 1) * 10 + (_KNUM.get(lo, 0) if lo else 0))


def normalize_address(s: str) -> str:
    """住所を比較用に正規化する（都道府県・郵便番号・建物名を落とし、番地を数字-数字に）。"""
    s = unicodedata.normalize("NFKC", s or "").translate(_ITAIJI)
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"^〒?\d{3}-?\d{4}", "", s)
    for n in sorted(PREF_NAMES, key=len, reverse=True):
        if s.startswith(n):
            s = s[len(n):]
            break
    s = re.sub(r"[〇一二三四五六七八九十]+(?=丁目|番|号|条|線|地割)",
               lambda m: _kanji_to_int(m.group()), s)
    s = re.sub(r"(?<=\d)[ー−‐―の](?=\d)", "-", s)
    s = re.sub(r"(丁目|番地|番|号)", "-", s)
    s = re.sub(r"-+", "-", s)
    m = re.match(r"^(.*?\d+(?:-\d+)*)", s)              # 番地までで切る（建物名を落とす）
    return (m.group(1) if m else s).strip("-")


_CITY_RE = re.compile(r"^(.{1,6}?郡.{1,6}?[町村]|.{1,5}?市.{1,4}?区|.{1,6}?[市区町村])")


def city_tokens(addr_norm: str) -> List[str]:
    """市区町村の頭部分（名簿との照合範囲を絞る用）。郡を省いた表記にも当たるよう2通り返す。"""
    m = _CITY_RE.match(addr_norm or "")
    if not m:
        return []
    tok = m.group(1)
    out = [tok]
    if "郡" in tok:
        out.append(tok.split("郡", 1)[1])
    return out


def _is_dental_name(name: str) -> bool:
    """「○○歯科医院」「○○歯科クリニック」等、歯科だけの施設名か。"""
    if "歯科" not in (name or ""):
        return False
    rest = name.replace("歯科口腔外科", "").replace("口腔外科", "").replace("歯科", "")
    return not re.search(r"科|病院", rest)


def _addr_same(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 6 and long_.endswith(short)       # 片方だけ郡名が付く等


def _name_close(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 3 and short in long_


def duplicate_registration_groups(items: list) -> List[list]:
    """同名・同住所で機関コードだけ違う施設のグループ（2件以上のもの）を返す。"""
    groups: Dict[Tuple[str, str], list] = {}
    for f in items:
        key = (normalize_name(f.name), normalize_address(f.address))
        if f.kikan_cd and key[0] and key[1]:
            groups.setdefault(key, []).append(f)
    return [g for g in groups.values() if len(g) >= 2]


def merge_duplicate_registrations(items: list, prefer_codes: set) -> Tuple[list, List[Tuple]]:
    """同名・同住所で機関コードだけが違う施設（ナビィ／ODの重複登録）を1件にまとめる。

    例：甲府「サンロード調剤丸の内店」が2つのコードで登録されている。両方残すと競合を
    二重に数えて予測が下がるため、ナビィの検索に出てくるほうのコードを残す。
    戻り値：(まとめた後のリスト, [(除いた施設, 残した施設), ...])"""
    keep_by_key: Dict[Tuple[str, str], int] = {}
    out: list = []
    removed: List[Tuple] = []
    for f in items:
        key = (normalize_name(f.name), normalize_address(f.address))
        if not (f.kikan_cd and key[0] and key[1]):
            out.append(f)
            continue
        if key not in keep_by_key:
            keep_by_key[key] = len(out)
            out.append(f)
            continue
        i = keep_by_key[key]
        kept = out[i]
        # 残すほうの優先順：①処方箋数（ナビィ実績）が取れている ②ナビィの検索に出てくるコード
        f_rx = getattr(f, "annual_rx_count", None) is not None
        k_rx = getattr(kept, "annual_rx_count", None) is not None
        if (f_rx and not k_rx) or (f_rx == k_rx and f.kikan_cd in prefer_codes
                                   and kept.kikan_cd not in prefer_codes):
            out[i] = f
            removed.append((kept, f))
        else:
            removed.append((f, kept))
    return out, removed


def _kb_num(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _attach_kb_info(obj, e) -> None:
    """名簿の医師数・診療科・病床を医療機関に付ける（v2.2：外来の参考値に使う）。"""
    if not isinstance(obj, MedFacility):
        return
    obj.kb_doctors_ft = _kb_num(getattr(e, "doctors_ft", None))
    obj.kb_doctors_pt = _kb_num(getattr(e, "doctors_pt", None))
    obj.kb_depts = str(getattr(e, "kb_depts", "") or "")
    if not obj.specialties and obj.kb_depts:
        obj.specialties = obj.kb_depts
    b = _kb_num(getattr(e, "beds", None))
    if obj.beds is None and b:
        obj.beds = int(b)


def crosscheck_kouseikyoku(center_lat: float, center_lon: float, radius_ph: float, radius_med: float,
                           meds: List[MedFacility], phs: List[PharmacyFacility],
                           od_df: Optional[pd.DataFrame], pref: Optional[str], geocoder,
                           log: List[str], max_geocode: int = 150) -> dict:
    """厚生局名簿と、ツールが集めた施設リストを突き合わせる。

    - 名簿にあってリストに無い → 住所から座標を出し、圏内ならリストに追加（要確認扱い）
    - 名簿で「休止」       → 要確認
    - リストにあって名簿に無い → 要確認（保険外の自由診療・OTCのみの店・廃止・名称変更など）
    """
    res = {"status": [], "added": [], "suspended": [], "not_in_kb": []}
    if not pref:
        res["status"].append("都道府県を判定できないため厚生局名簿との突き合わせは行いませんでした")
        return res

    # 照合範囲（市区町村）＝ 圏内にある施設の住所から作る
    area_addrs = [f.address for f in meds + phs if f.address]
    if od_df is not None:
        area_addrs += od_nearby(od_df, center_lat, center_lon, radius_med + 500,
                                ["pharmacy"] + OD_MED_KINDS)["address"].tolist()
    tokens = sorted({t for a in area_addrs for t in city_tokens(normalize_address(a))},
                    key=len, reverse=True)
    if not tokens:
        res["status"].append("圏内の住所から市区町村を特定できず、厚生局名簿との突き合わせを省略しました")
        return res

    def _in_area(addr_n: str) -> bool:
        return any(addr_n.startswith(t) for t in tokens)

    groups = (("yakkyoku", phs, ["pharmacy"], radius_ph),
              ("ika", meds, ["hospital", "clinic"], radius_med))
    for kind, flist, od_kinds, radius in groups:
        kb, st_msg = kb_ensure(pref, kind)
        res["status"].append(f"{KB_KINDS[kind]}：{st_msg}")
        if kb is None or kb.empty:
            continue
        kb = kb.assign(addr_n=kb["address"].map(normalize_address),
                       name_n=kb["name"].map(normalize_name))
        kb = kb[kb["addr_n"].map(_in_area)]

        # 照合相手＝ツールのリスト（圏内）＋ 同じ市区町村のオープンデータ（圏外も含む。
        # 名簿は市区町村単位なので、圏外の施設を「リストに無い」と誤判定しないため）
        pool: List[dict] = []
        for f in flist:
            pool.append({"name_n": normalize_name(f.name), "addr_n": normalize_address(f.address),
                         "obj": f, "hit": False})
        if od_df is not None:
            listed = {f.kikan_cd for f in flist if f.kikan_cd}
            sub = od_df[od_df["kind"].isin(od_kinds) & (od_df["pref"] == pref)]
            for r in sub.itertuples():
                if r.kikan_cd in listed:
                    continue
                an = normalize_address(r.address)
                if _in_area(an):
                    pool.append({"name_n": normalize_name(r.name), "addr_n": an, "obj": None, "hit": False})
        by_name: Dict[str, List[dict]] = {}
        by_addr: Dict[str, List[dict]] = {}
        for c in pool:
            by_name.setdefault(c["name_n"], []).append(c)
            by_addr.setdefault(c["addr_n"], []).append(c)

        unmatched = []
        for e in kb.itertuples():
            cand = by_name.get(e.name_n, [])
            hit = next((c for c in cand if _addr_same(c["addr_n"], e.addr_n)), None) or \
                (cand[0] if len(cand) == 1 else None)
            if hit is None:
                hit = next((c for c in by_addr.get(e.addr_n, [])
                            if _name_close(c["name_n"], e.name_n)
                            or name_similarity(c["name_n"], e.name_n) >= 0.6), None)
            if hit is None:
                hit = next((c for c in pool if _addr_same(c["addr_n"], e.addr_n)
                            and _name_close(c["name_n"], e.name_n)), None)
            if hit is not None:
                hit["hit"] = True
                if hit["obj"] is not None:
                    _attach_kb_info(hit["obj"], e)
                if e.status == "休止" and hit["obj"] is not None:
                    hit["obj"].review_note = "厚生局名簿で『休止』"
                    res["suspended"].append({"種別": KB_KINDS[kind], "施設名": hit["obj"].name,
                                             "住所": e.address, "内容": "厚生局名簿で『休止』"})
                continue
            if e.status == "休止":
                continue
            unmatched.append(e)

        # 名簿にしか無い施設 → 住所から座標を出して圏内か確認
        if len(unmatched) > max_geocode:
            log.append(f"[名簿照合] ⚠️ {KB_KINDS[kind]}：名簿とリストの不一致が{len(unmatched)}件と多いため、"
                       f"先頭{max_geocode}件だけ位置を確認しました（名簿の様式変更の可能性）。")
            unmatched = unmatched[:max_geocode]
        pref_name = PREF_NAMES[int(pref) - 1]
        coords: Dict[int, Optional[Tuple[float, float]]] = {}

        def _gc(i_e):
            i, e = i_e
            coords[i] = geocoder.geocode(pref_name + e.address)
        _parallel(list(enumerate(unmatched)), _gc, 4)
        n_add = 0
        for i, e in enumerate(unmatched):
            gc = coords.get(i)
            if not gc:
                continue
            d = haversine(center_lat, center_lon, gc[0], gc[1])
            if d > radius:
                continue
            note = "厚生局名簿のみ（ナビィ・公式ODに無し＝新規開設/名称変更の可能性）"
            if kind == "yakkyoku":
                obj = PharmacyFacility(name=e.name, address=e.address, lat=gc[0], lon=gc[1],
                                       distance_m=d, source="厚生局名簿のみ（要確認）")
            else:
                obj = MedFacility(name=e.name, address=e.address, lat=gc[0], lon=gc[1], distance_m=d,
                                  source="厚生局名簿のみ（要確認）", rx_summary="不明",
                                  facility_category="病院" if "病院" in e.name else "診療所",
                                  coord_source="住所ジオコーディング")
            obj.review_note = note
            _attach_kb_info(obj, e)
            flist.append(obj)
            n_add += 1
            res["added"].append({"種別": KB_KINDS[kind], "施設名": e.name, "住所": e.address,
                                 "距離(m)": round(d), "内容": note})

        # リストにあるのに名簿に無い（圏内だけ報告）
        n_miss = 0
        hit_keys = {(c["name_n"], c["addr_n"]) for c in pool if c["hit"]}
        for c in pool:
            f = c["obj"]
            if c["hit"] or f is None or f.review_note or not f.kikan_cd:
                continue      # OSM由来（コード・住所なし）は照合精度が低いので報告しない
            if kind == "ika" and (getattr(f, "kikan_kbn", 2) == 3 or _is_dental_name(f.name)):
                continue          # 歯科は医科の名簿に載らない
            if f.distance_m is not None and f.distance_m > radius:
                continue
            if (c["name_n"], c["addr_n"]) in hit_keys:
                # 同名・同住所の施設が別コードでもう1件リストにある＝ナビィ側の重複登録。
                # 放置すると競合を二重に数えるので、要確認にする。
                f.review_note = "同名・同住所の施設が別コードでも登録（重複登録の可能性・二重計上に注意）"
            else:
                f.review_note = "厚生局名簿に無し（自由診療のみ・保険外・廃止・名称変更の可能性）"
            n_miss += 1
            res["not_in_kb"].append({"種別": KB_KINDS[kind], "施設名": f.name, "住所": f.address,
                                     "距離(m)": round(f.distance_m) if f.distance_m is not None else None,
                                     "内容": f.review_note})
        log.append(f"[名簿照合] {KB_KINDS[kind]}：名簿{len(kb)}件（照合範囲の市区町村）と照合 → "
                   f"名簿のみで圏内 {n_add}件を追加 / リストのみ {n_miss}件")
        if n_add:
            log.append(f"[名簿照合] ⚠️ {KB_KINDS[kind]}：ナビィ・公式ODに無く厚生局名簿にだけある施設を"
                       f"{n_add}件追加しました（出典『厚生局名簿のみ（要確認）』）。")
    return res


# ── ⑤ 外来患者数「不明」の参考値（v2.2） ─────────────────────────────────
REF_PT_WEIGHT_DEFAULT = 0.2   # 非常勤医師1人を常勤の何人分とみなすか
REF_MIN_SAMPLES = 3           # 診療科ごとの中央値を使う最低件数
REF_SKIP_BUCKETS = ("病院", "美容")   # 医師数と外来数の関係が違う／自由診療中心のため出さない


def _doctor_fte(f, pt_w: float) -> Optional[float]:
    ft, pt = getattr(f, "kb_doctors_ft", None), getattr(f, "kb_doctors_pt", None)
    if ft is None and pt is None:
        return None
    v = (ft or 0.0) + (pt or 0.0) * pt_w
    return v if v > 0 else None


def _fmt_num(x: float) -> str:
    return str(int(x)) if abs(x - round(x)) < 1e-9 else f"{x:.1f}"


def compute_reference_outpatients(meds: list, pt_w: float = REF_PT_WEIGHT_DEFAULT
                                  ) -> Dict[str, Tuple[int, str]]:
    """外来患者数が不明な医療機関の参考値を出す。{facility_key: (参考値, 根拠)}

    参考値 ＝ 常勤換算の医師数 × 同じ診療科の「医師1人あたり外来数」の中央値。
    中央値は同じ分析の周辺施設のうち、外来数（ナビィ）と医師数（厚生局名簿）が
    両方分かっている施設から取る。外部の仮定値は使わない。"""
    samples: Dict[str, List[float]] = {}
    allv: List[float] = []
    ops: Dict[str, List[int]] = {}        # 実際の外来数（上限の頭打ちに使う）
    for f in meds:
        op = f.daily_outpatients
        if op is None or op <= 0 or getattr(f, "op_ref_used", False):
            continue
        if op >= OP_HIGH_THR_DEFAULT or getattr(f, "op_flag", ""):
            continue                      # 年間値の混入疑いなどは除く
        b = bucket_of_med(f)
        if b in REF_SKIP_BUCKETS:
            continue
        fte = _doctor_fte(f, pt_w)
        if not fte:
            continue
        per = op / fte
        samples.setdefault(b, []).append(per)
        allv.append(per)
        ops.setdefault(b, []).append(op)
        ops.setdefault("*", []).append(op)
    med_by = {b: statistics.median(v) for b, v in samples.items() if len(v) >= REF_MIN_SAMPLES}
    all_med = statistics.median(allv) if len(allv) >= REF_MIN_SAMPLES else None
    out: Dict[str, Tuple[int, str]] = {}
    for f in meds:
        if f.daily_outpatients is not None and not getattr(f, "op_ref_used", False):
            continue
        b = bucket_of_med(f)
        if b in REF_SKIP_BUCKETS:
            continue
        if b in med_by:
            per, n, lab, cap = med_by[b], len(samples[b]), b, max(ops[b])
        elif all_med is not None:
            per, n, lab, cap = all_med, len(allv), "全診療科", max(ops["*"])
        else:
            continue
        fte = _doctor_fte(f, pt_w)
        if fte:
            ft, pt = getattr(f, "kb_doctors_ft", None) or 0, getattr(f, "kb_doctors_pt", None) or 0
            who = f"常勤{_fmt_num(ft)}人" + (f"＋非常勤{_fmt_num(pt)}人×{pt_w:g}" if pt else "")
            basis = f"医師{_fmt_num(fte)}人（{who}）× {lab}の中央値{per:.0f}人/医師（周辺n={n}）"
        else:
            fte = 1.0
            basis = f"医師数不明→1人と仮定 × {lab}の中央値{per:.0f}人/医師（周辺n={n}）"
        ref = max(1, int(round(fte * per)))
        if ref > cap:
            # 非常勤医が数十人登録されている施設などで過大にならないよう、
            # 周辺で実際に観測された最大値で頭打ちにする
            basis += f"＝{ref}人 → 周辺の{lab}の最大値{cap}人で頭打ち"
            ref = cap
        out[facility_key(f)] = (ref, basis)
    return out


# ── ⑥ ナビィ構造チェック（旧 navii_health_check.py を内蔵） ─────────────────
def navii_quick_check(lat: float = 35.6644, lon: float = 138.5686) -> List[Tuple[str, str]]:
    """ナビィに実際にアクセスし、ツールの前提（API応答・一覧のHTML・詳細ページ）が
    今も成り立っているかを確認する。(判定, 内容) のリストを返す。判定は ok/ng/warn。"""
    out: List[Tuple[str, str]] = []
    sc = MHLWScraper(use_snapshots=False)
    # 確認は1ページ目だけ読むので、スクレイパーの「取りこぼしあり」注記は表示しない
    _clean = lambda m: m.replace(" ※取りこぼしあり", "").replace("取得20件", "1ページ目20件を読取")
    if not sc._init():
        return [("ng", "ナビィに接続できません（サイト停止・メンテナンス・通信障害）")]
    out.append(("ok", "ナビィに接続できる"))
    meds, msg = sc.search_medical_by_latlon(lat, lon, 1000, max_pages=1)
    if meds:
        out.append(("ok", f"医療機関の検索・一覧の読み取り（{_clean(msg)}）"))
        n_xy = sum(1 for f in meds if f.lat is not None)
        out.append(("ok" if n_xy else "warn", f"一覧の地図座標 {n_xy}/{len(meds)}件"))
        f0 = meds[0]
        if sc.get_facility_detail(f0) and f0.address:
            out.append(("ok", f"医療機関の詳細ページ（例：{f0.name[:14]} 外来={f0.daily_outpatients}）"))
        else:
            out.append(("ng", "医療機関の詳細ページを読み取れない（詳細画面のHTML変更の可能性）"))
    else:
        out.append(("ng", f"医療機関の一覧を読み取れない（{msg}）。検索APIか一覧HTMLの変更の可能性"))
    phs, pmsg = sc.search_pharmacies_by_latlon(lat, lon, 1000, max_pages=1)
    if phs:
        out.append(("ok", f"薬局の検索・一覧の読み取り（{_clean(pmsg)}）"))
        p0 = phs[0]
        ok = sc.get_pharmacy_detail(p0)
        out.append(("ok" if ok else "ng",
                    f"薬局の詳細ページ（例：{p0.name[:14]} 処方箋数={p0.annual_rx_count}）" if ok
                    else "薬局の詳細ページを読み取れない（詳細画面のHTML変更の可能性）"))
    else:
        out.append(("ng", f"薬局の一覧を読み取れない（{pmsg}）"))
    return out


class MHLWScraper:
    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "ja-JP,ja;q=0.9",
    }

    # v2.1: 通信失敗がこの回数続いたら「ナビィ停止中」とみなし、以後は前回取得分だけを使う
    # （止まったナビィに1施設ずつタイムアウトまで待つと、1店舗の分析に数分かかるため）
    DOWN_AFTER_FAILS = 6

    def __init__(self, use_snapshots: bool = True):
        self.session = self._new_session()
        self._ready = False
        self.snapshots: Optional[NaviiSnapshotStore] = NaviiSnapshotStore() if use_snapshots else None
        self.snapshot_used: Dict[str, str] = {}   # url → 前回取得日時（今回その控えを使ったもの）
        self._fail_streak = 0
        self.down = False
        # v1.3: 詳細ページの並列取得用（requests.Session はスレッド安全ではないので
        # スレッドごとに独立したSessionを持つ）＋取得済みHTMLのキャッシュ。
        self._local = threading.local()
        self._cache: Dict[str, Optional[str]] = {}
        self._cache_lock = threading.Lock()
        # v1.4: 直近の検索で取りこぼしが起きたかの記録（「取りこぼし診断」タブ用）
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
        """このスレッド専用のSessionを返す。

        接続プールはスレッドごとに分けるが、CookieはメインSessionのジャーを共有する。
        ナビィは検索結果をセッションに紐づけて保持するため、別Cookieのセッションから
        一覧ページ(S2400)を叩くと結果が取れなくなるため。
        （http.cookiejar.CookieJar はロック付きでスレッド間共有できる）
        """
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = self._new_session()
            sess.cookies = self.session.cookies      # ★セッション同一性を維持
            if not self._ready and not self.down:
                try:
                    sess.get(f"{MHLW_BASE}/juminkanja/S2320/initialize", timeout=15)
                except Exception:
                    pass
            self._local.session = sess
        return sess

    def reset_health(self) -> None:
        """分析のたびに呼ぶ。前回「停止中」と判定していても、もう一度ナビィを試す。"""
        self._fail_streak = 0
        self.down = False
        self.snapshot_used = {}

    def _note_result(self, ok: bool) -> None:
        with self._cache_lock:
            if ok:
                self._fail_streak = 0
            else:
                self._fail_streak += 1
                if self._fail_streak >= self.DOWN_AFTER_FAILS:
                    self.down = True

    @staticmethod
    def _looks_broken(html: str) -> bool:
        """200で返ってきても中身がメンテナンス画面・エラー画面のもの。"""
        head = html[:6000]
        return len(html) < 1500 or ("メンテナンス中" in head and "<table" not in html)

    def _get_html(self, url: str, timeout: int = 12) -> Optional[str]:
        """詳細ページHTMLをキャッシュ付きで取得する（同じURLは1回しか取りに行かない）。

        v2.1: 取れたページは保存し、取れなかったとき（停止・メンテナンス・通信エラー）は
        前回取得分を返す。どのURLで控えを使ったかは snapshot_used に残す。"""
        with self._cache_lock:
            if url in self._cache:
                return self._cache[url]
        html: Optional[str] = None
        if not self.down:
            try:
                r = self._sess().get(url, timeout=timeout)
                if r.status_code == 200 and not self._looks_broken(r.text):
                    html = r.text
            except Exception:
                html = None
            self._note_result(html is not None)
        if html is not None:
            if self.snapshots and "E-0109" not in html and "データは存在しません" not in html:
                self.snapshots.save(url, html)
        elif self.snapshots:
            snap = self.snapshots.load(url)
            if snap:
                html = snap[0]
                with self._cache_lock:
                    self.snapshot_used[url] = snap[1]
        with self._cache_lock:
            if len(self._cache) > 4000:      # 際限なく増えないよう上限（実用上まず届かない）
                self._cache.clear()
            self._cache[url] = html
        return html

    def cache_size(self) -> int:
        with self._cache_lock:
            return len(self._cache)

    def _collect_pages(self, page_fn, parse_fn, max_pages: int):
        """1ページ目で総件数を確定し、必要なページ数だけ並列で取得する。

        v1.4: 旧版は max_pages を固定の小さな値（薬局8=160件 / 医療機関6=120件）で
        打ち切り、しかもそれを画面に一切出していなかった。件数の多いエリアでは
        ここで静かに切り捨てが起き、目視で見つかる「漏れ」の主因になっていた。
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
        if self.down:
            return False
        try:
            r = self.session.get(f"{MHLW_BASE}/juminkanja/S2320/initialize", timeout=15)
            self._ready = r.status_code == 200 and len(r.text) >= 1500
        except Exception:
            self._ready = False
        if not self._ready:
            self.down = True          # 入口から入れない＝停止中。以後は前回取得分で動く
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

        # v1.3: 1ページ目で総件数を得てから、残りページを並列取得（旧版は逐次＋0.3秒待ち）
        # v1.4: ページ上限を実質撤廃し、取りきれなかった場合は警告を残す。
        def _page(p: int) -> Optional[str]:
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
                f"薬局が全{total}件中{len(all_ph)}件しか取得できませんでした"
                f"（ページ上限{max_pages}）。取りこぼしの可能性があります。")
            msg += " ※取りこぼしあり"
        return all_ph, msg

    @staticmethod
    def _find_name_link(item):
        """一覧itemから施設名リンクを取得する。
        v1.2修正: ナビィのHTML変更(2026-07頃)で施設名が <h3 class="name"> から
        <h2 class="name"> になったため、h2/h3両対応＋リンク直探しのフォールバック付き。"""
        head = item.find(["h2", "h3"], class_="name")
        if head:
            link = head.find("a", href=True)
            if link:
                return link
        return item.select_one('a[href*="kikanCd"]')

    @staticmethod
    def _extract_maplink_coords(item):
        """一覧itemのGoogleマップリンク(data-url="...maps?q=lat,lon")から座標を抽出する。
        v1.2追加: 新HTMLでは一覧に座標が埋め込まれるようになった。"""
        a = item.find("a", class_="mapLink")
        if a:
            m = re.search(r"q=(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", a.get("data-url", "") or "")
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                if 24.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0:
                    return lat, lon
        return None

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
            pref_cd = re.search(r"prefCd=(\d+)", href)
            kikan_cd = re.search(r"kikanCd=(\w+)", href)
            pref_cd  = pref_cd.group(1)  if pref_cd  else ""
            kikan_cd = kikan_cd.group(1) if kikan_cd else ""
            raw_text = item.get_text(separator=" ", strip=True)
            addr_m = re.search(r"〒\s*[\d-]+\s+(.+?)(?:Googleマップ|$)", raw_text)
            address = addr_m.group(1).strip() if addr_m else ""
            ph = PharmacyFacility(
                name=name, address=address, href=href,
                pref_cd=pref_cd, kikan_cd=kikan_cd, source="mhlw",
            )
            coords = self._extract_maplink_coords(item)
            if coords:
                ph.lat, ph.lon = coords
            results.append(ph)
        # v1.4: 総件数は「検索結果N件」等を優先して読む（旧版は本文で最初に出た
        # 「N件」を採用していたため、新HTMLの「20件表示」を総件数と誤認し、
        # 21件目以降を取りに行かないことがあった）。
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

        # v1.3: 1ページ目で総件数を得てから、残りページを並列取得（旧版は逐次＋0.3秒待ち）
        # v1.4: ページ上限を実質撤廃し、取りきれなかった場合は警告を残す。
        sep = "&" if "?" in redirect_url else "?"

        def _page(p: int) -> Optional[str]:
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
                f"医療機関が全{total}件中{len(all_facs)}件しか取得できませんでした"
                f"（ページ上限{max_pages}）。取りこぼしの可能性があります。")
            msg += " ※取りこぼしあり"
        return all_facs, msg

    def _parse_med_list(self, html: str) -> Tuple[List[MedFacility], int]:
        """S2400 医療機関一覧HTMLからMedFacilityリストを生成する（hrefからpref_cd/kikan_cd/kikan_kbn抽出）。"""
        soup = BeautifulSoup(html, "html.parser")
        results: List[MedFacility] = []
        for item in soup.find_all("div", class_="item"):
            link = self._find_name_link(item)   # v1.2: h2/h3両対応（ナビィHTML変更対応）
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
            try:
                kikan_kbn = int(qp.get("kikanKbn", "2"))
            except ValueError:
                kikan_kbn = 2
            fac = MedFacility(
                name=name, source="mhlw",
                pref_cd=pref_cd, kikan_cd=kikan_cd, kikan_kbn=kikan_kbn,
            )
            coords = self._extract_maplink_coords(item)  # v1.2: 一覧埋込座標を初期値に
            if coords:
                fac.lat, fac.lon = coords
                fac.coord_source = "ナビィ一覧埋込"
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
        raw_html = None
        # v1.4: 名前が一致するページを最優先しつつ、一致しなくても「エラーではない
        # ページ」は控えとして保持し、最後まで一致が無ければそれを採用する。
        # 旧版は `fac.name[:4] in text` に一致しない限り不採用だったため、一覧と
        # 詳細で表記が違う施設（法人格の有無・全角半角・スペース）は詳細を取れず、
        # 住所も座標も得られないまま商圏判定から抜け落ちていた。
        fb_soup = fb_html = fb_kbn = fb_url = None
        norm_name = normalize_name(fac.name)
        for kbn in kbn_list:
            url = (f"{MHLW_BASE}/juminkanja/S2430/initialize"
                   f"?prefCd={fac.pref_cd}&kikanCd={fac.kikan_cd}&kikanKbn={kbn}")
            html = self._get_html(url)     # v1.3: スレッド別Session＋キャッシュ
            if not html:
                continue
            candidate_soup = BeautifulSoup(html, "html.parser")
            text = candidate_soup.get_text()
            if "E-0109" in text or "データは存在しません" in text:
                continue
            matched = (fac.name[:4] in text
                       or (norm_name and norm_name[:4] in normalize_name(text))
                       or len(text) > 50_000)
            if matched:
                soup, used_kbn, raw_html = candidate_soup, kbn, html
                fac.detail_url = url
                fac.kikan_kbn = kbn
                break
            if fb_soup is None:
                fb_soup, fb_html, fb_kbn, fb_url = candidate_soup, html, kbn, url
        if soup is None and fb_soup is not None:
            soup, used_kbn, raw_html = fb_soup, fb_kbn, fb_html
            fac.detail_url = fb_url
            fac.kikan_kbn = fb_kbn
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
            if len(cells) >= 8 and not _is_blank_cell(cells[7]):
                n = _cell_num(cells[7])
                if n is not None and n <= 1_000:
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
        if fac.weekly_op_days is None and fac.od_op_days:
            fac.weekly_op_days = fac.od_op_days     # v2.1: 公式ODの診療時間表から数えた日数

        # ── 診療科目 ─────────────────────────────────────────────────────
        if not fac.specialties:
            fac.specialties = _parse_specialties(all_fields, full_text)

        # ── データ品質検証 ────────────────────────────────────────────────
        fac.beds = _parse_total_beds(all_fields)
        fac.is_cosmetic = _detect_cosmetic(fac.name, fac.specialties)
        fac.op_flag, fac.op_suggested = _validate_outpatients(fac)

        fac.detail_fetched = True
        snap_at = self.snapshot_used.get(fac.detail_url)
        if snap_at:     # v2.1: ナビィに繋がらず前回取得分で読んだ
            fac.daily_outpatients_source = f"{fac.daily_outpatients_source}（前回取得 {snap_at[:10]}）"
        return True

    def get_pharmacy_detail(self, ph: PharmacyFacility) -> bool:
        """ナビィ薬局詳細ページから総取扱処方箋数を取得する。"""
        self._init()
        if not (ph.pref_cd and ph.kikan_cd):
            return False
        url = (f"{MHLW_BASE}/juminkanja/S2430/initialize"
               f"?prefCd={ph.pref_cd}&kikanCd={ph.kikan_cd}&kikanKbn=5")
        html = self._get_html(url)         # v1.3: スレッド別Session＋キャッシュ
        if not html:
            return False
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(separator="\n", strip=True)
        if "E-0109" in text or "データは存在しません" in text:
            return False

        ph.detail_url = url

        # 座標（ナビィ埋込の正確な緯度経度があればジオコーディング値より優先）
        coords = _extract_coords_from_html(html)
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
        snap_at = self.snapshot_used.get(url)
        if snap_at:     # v2.1: ナビィに繋がらず前回取得分で読んだ
            ph.annual_rx_source = f"{ph.annual_rx_source}（前回取得 {snap_at[:10]}）"
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

def _parallel(items, fn, workers: int, on_done=None):
    """items を並列に fn へ流す。完了ごとに on_done(i, item) を呼ぶ（進捗表示用・呼び出しは
    メインスレッド）。Streamlitのst.*はワーカースレッドから呼ばないこと。"""
    if not items:
        return
    if workers <= 1 or len(items) == 1:
        for i, it in enumerate(items):
            try:
                fn(it)
            except Exception:
                pass
            if on_done:
                on_done(i, it)
        return
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for i, fut in enumerate(as_completed(futs)):
            try:
                fut.result()
            except Exception:
                pass
            if on_done:
                on_done(i, futs[fut])


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
    workers: int = FETCH_WORKERS_DEFAULT,
    verify_pass: bool = False,
    use_osm: bool = True,
    od_df: Optional[pd.DataFrame] = None,
    use_kouseikyoku: bool = True,
    xcheck_out: Optional[dict] = None,
    fetch_med_detail: bool = True,
    detail_max_m: Optional[float] = None,
) -> Tuple[List[MedFacility], List[PharmacyFacility], float, float]:
    """1候補地ぶんのデータ収集。v1.3で詳細ページ取得を並列化し、重複していた再検索を任意化した。

    v2.1: 公式オープンデータ(od_df)を土台にし、ナビィは「OD公開後の新規施設」と
    「外来患者数・処方箋数などの詳細」を足す役割にした。最後に厚生局名簿と突き合わせ、
    結果を xcheck_out に入れて返す（取りこぼし診断タブで表示）。"""
    scraper = get_scraper()
    scraper.reset_health()
    geocoder = get_geocoder()
    t_all = time.time()

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
    med_radius = radius_m + gate_m

    # Step 1.5 (v2.1): 公式オープンデータから圏内の施設を取り出す（リストの土台）
    od_ph: List[PharmacyFacility] = []
    od_med: List[MedFacility] = []
    od_codes: set = set()
    if od_df is not None:
        od_codes = set(od_df["kikan_cd"])
        # 薬局はナビィ経由と同じ「半径×1.1」、医療機関はナビィの検索範囲
        # （距離コード 1km / 5km）と同じ範囲を取る。
        # ナビィが止まっていても、動いているときと同じ施設リストで計算するため。
        _code = dist_code_for(med_radius)
        od_med_r = 1_000 if _code == "00" else (5_000 if _code == "01" else med_radius)
        od_ph = [od_row_to_ph(r) for r in
                 od_nearby(od_df, center_lat, center_lon, radius_m * 1.1, ["pharmacy"]).itertuples()]
        od_med = [od_row_to_med(r) for r in
                  od_nearby(od_df, center_lat, center_lon, max(od_med_r, med_radius),
                            OD_LIST_MED_KINDS).itertuples()]
        log.append(f"[Step1.5] 公式オープンデータ: 薬局={len(od_ph)}件 / 医療機関"
                   f"({max(od_med_r, med_radius)}m圏)={len(od_med)}件"
                   "（リストの土台。ナビィ停止中でもここまでは確定）")
    else:
        log.append("[Step1.5] ⚠️ 公式オープンデータが使えないため、ナビィのみでリストを作ります"
                   "（ナビィが止まっているとリストが空になります）。")
    od_used = od_df is not None

    # Step 2+3: OSM(Overpass)は応答が遅い/落ちていることが多いので、
    # ここでバックグラウンドに投げてナビィ検索と並走させ、あとで回収する（v1.3）。
    prog.progress(8, text="Step2-3: OSM検索を開始（ナビィ検索と並行）…")
    t_osm = time.time()
    osm_ex = None
    f_ph_osm = f_med_osm = None
    if use_osm:
        osm_ex = ThreadPoolExecutor(max_workers=2)
        f_ph_osm = osm_ex.submit(search_osm_pharmacies, center_lat, center_lon, radius_m)
        f_med_osm = osm_ex.submit(search_osm_medical, center_lat, center_lon, med_radius)

    # Step 4: ナビィ薬局リスト取得（OSMの応答を待たずに進める）
    prog.progress(16, text="Step4: ナビィから薬局リストを取得中…")
    t0 = time.time()
    scraper.last_warnings = []
    navvi_phs, navvi_ph_msg = scraper.search_pharmacies_by_latlon(
        center_lat, center_lon, radius_m=radius_m,
        center_name=address[:20],
    )
    log.append(f"[Step4] {navvi_ph_msg}")
    if not navvi_phs:
        log.append("[Step4] ⚠️ ナビィ薬局が0件でした。"
                   "ナビィ側の仕様変更・通信エラーの可能性があります。")

    # Step 2+3 の回収（上限 OSM_BUDGET_S 秒。超えたらナビィのみで続行）
    prog.progress(20, text="Step2-3: OSMの結果を回収中…")
    ph_osm: List[PharmacyFacility] = []
    med_osm: List[MedFacility] = []
    if use_osm:
        for fut, sink in ((f_ph_osm, "ph"), (f_med_osm, "med")):
            left = OSM_BUDGET_S - (time.time() - t_osm)
            try:
                res = fut.result(timeout=max(1.0, left))
            except Exception:
                res = None
            if res:
                if sink == "ph":
                    ph_osm = res
                else:
                    med_osm = res
        osm_ex.shutdown(wait=False)
        log.append(f"[Step2-3] OSM薬局={len(ph_osm)}件 / OSM医療機関({med_radius}m圏)={len(med_osm)}件 "
                   f"({time.time()-t_osm:.1f}s・ナビィ検索と並行実行)")
        if not med_osm:
            log.append("[Step2-3] " + ("" if od_df is not None else "⚠️ ")
                       + "OSM医療機関0件（応答なし/圏内になし）→ "
                       + ("公式オープンデータ＋ナビィで処理します" if od_df is not None
                          else "ナビィデータのみで処理します"))
    else:
        log.append("[Step2-3] OSM併用はOFF → ナビィデータのみで処理します")
    # v2.1: 公式OD → OSM → ナビィ の順に重ねる（同一判定は機関コード優先の same_facility）
    ph_merged: List[PharmacyFacility] = list(od_ph)
    for p in ph_osm:
        if not is_duplicate_of_any(p, ph_merged, DEDUP_GAP_M):
            ph_merged.append(p)
    # v1.4: 重複判定を name_similarity>=0.65（文字集合の重なり）から、
    # 機関コード優先の same_facility() に変更。旧判定は「さくら薬局中央店」と
    # 「さくら薬局東町店」のような別店舗まで同一視して消していた。
    seen_ph_cd: set = set()
    new_phs: List[PharmacyFacility] = []
    for nph in navvi_phs:
        if nph.kikan_cd and nph.kikan_cd in seen_ph_cd:
            continue                                    # ページ間の重複のみ除去
        dup = next((p for p in ph_merged if same_facility(nph, p, DEDUP_GAP_M)), None)
        if dup is not None:
            if not dup.pref_cd:                         # OSM側に機関コードを補完
                dup.pref_cd  = nph.pref_cd
                dup.kikan_cd = nph.kikan_cd
                dup.href     = nph.href
            continue
        if nph.kikan_cd:
            seen_ph_cd.add(nph.kikan_cd)
        if od_used and nph.kikan_cd not in od_codes:
            nph.source = "ナビィ（公式OD未収載）"
        new_phs.append(nph)

    need_gc = [p for p in new_phs if p.lat is None and p.address]

    def _gc_ph(p):
        gc = geocoder.geocode(p.address)
        if gc:
            p.lat, p.lon = gc
    _parallel(need_gc, _gc_ph, min(workers, 4))

    added_navvi_ph = 0
    for nph in new_phs:
        if nph.lat is not None:
            nph.distance_m = haversine(center_lat, center_lon, nph.lat, nph.lon)
            if nph.distance_m > radius_m * 1.1:
                continue
        ph_merged.append(nph)
        added_navvi_ph += 1
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    no_coord_ph = sum(1 for p in ph_merged if p.lat is None)
    log.append(
        f"[Step4] ナビィ固有追加: {added_navvi_ph}件 合計: {len(ph_merged)}件 "
        f"（座標なし: {no_coord_ph}件 / 住所ジオコーディング: {len(need_gc)}件） "
        f"({time.time()-t0:.1f}s)"
    )

    # Step 5: ナビィ医療機関リスト → 詳細（住所・外来患者数・院内外処方）を並列取得
    prog.progress(30, text="Step5: ナビィから医療機関リストを取得中…")
    t0 = time.time()
    navvi_meds, med_msg = scraper.search_medical_by_latlon(
        center_lat, center_lon, radius_m=med_radius,
        center_name=address[:20],
    )
    log.append(f"[Step5] {med_msg}")
    if not navvi_meds:
        log.append("[Step5] ⚠️ ナビィ医療機関が0件でした。"
                   "ナビィ側の仕様変更・通信エラーの可能性があります。")

    # 薬局の混入除外。v1.4: 名前だけで判定すると「くすりの木内科クリニック」のような
    # 実在の医院まで落ちるため、機関区分(kikanKbn=5)を主、名前を従にした。
    _PHARMA_NAME_RE = re.compile(
        r'薬局|ドラッグ|ファーマシー|調剤|drug\s*store|pharmacy', re.IGNORECASE
    )
    _MED_NAME_RE = re.compile(
        r'医院|クリニック|診療所|病院|歯科|内科|外科|眼科|皮膚科|小児科|産婦人科|'
        r'耳鼻|泌尿器|整形|心療|精神|リハビリ|医療センター|保健'
    )

    def _is_pharmacy_row(f) -> bool:
        if f.kikan_kbn == 5:
            return True
        return bool(_PHARMA_NAME_RE.search(f.name)) and not _MED_NAME_RE.search(f.name)

    # v1.4: 旧版はここに [:50] の上限があり、医療機関が50件を超えるエリアでは
    # 51件目以降が画面にもログにも出ないまま消えていた（＝漏れの最大の原因）。
    # 上限を撤廃し、重複判定も機関コード優先の same_facility() に変更した。
    # v2.1: 公式ODの医療機関を土台にし、OSMはODと重複しないものだけ重ねる
    osm_only = [f for f in med_osm if not is_duplicate_of_any(f, od_med, DEDUP_GAP_M)]
    med_osm = list(od_med) + osm_only
    # fetch_med_detail=False（薬局ファインダー等、医療機関は門前判定の座標だけ要る場合）は、
    # 座標が既に分かっている医療機関の詳細ページを取りに行かない（大幅に速くなる）
    # detail_max_m を渡すと、その距離より遠い医療機関の詳細は取らない（医療機関ファインダー用：
    # ナビィの検索範囲は1km/5km単位で、表示範囲より広いことが多いため）
    od_targets = [f for f in od_med if f.pref_cd and f.kikan_cd
                  and (fetch_med_detail or f.lat is None)
                  and (detail_max_m is None or f.distance_m is None or f.distance_m <= detail_max_m)]
    med_existing_kikan_cds = {f.kikan_cd for f in med_osm if f.kikan_cd}
    med_targets: List[MedFacility] = []
    for f in navvi_meds:
        if not (f.pref_cd and f.kikan_cd):
            continue
        if _is_pharmacy_row(f):
            continue
        if f.kikan_cd in med_existing_kikan_cds:        # ページ間の重複のみ除去
            continue
        if is_duplicate_of_any(f, med_osm, DEDUP_GAP_M):
            continue
        med_existing_kikan_cds.add(f.kikan_cd)
        if od_used and f.kikan_cd not in od_codes:
            f.source = "ナビィ（公式OD未収載）"
        med_targets.append(f)
    log.append(f"[Step5] 詳細取得対象: 公式OD由来{len(od_targets)}件 ＋ ナビィのみ{len(med_targets)}件（上限なし）")

    stats = {"ok": 0, "gc_fail": 0, "detail_fail": 0}
    stats_lock = threading.Lock()

    def _fetch_med(nmf):
        ok = scraper.get_facility_detail(nmf)
        if nmf.lat is None and nmf.address:
            gc = geocoder.geocode(nmf.address)   # 詳細に埋込座標が無い場合のみ
            if gc:
                nmf.lat, nmf.lon = gc
        with stats_lock:
            if not ok:
                stats["detail_fail"] += 1
            if nmf.lat is not None:
                stats["ok"] += 1
            else:
                stats["gc_fail"] += 1

    n_med = len(od_targets) + len(med_targets if fetch_med_detail else
                                   [f for f in med_targets if f.lat is None])

    def _med_prog(i, item):
        prog.progress(30 + int(20 * (i + 1) / max(n_med, 1)),
                      text=f"Step5: 医療機関の詳細を並列取得中 {i+1}/{n_med}件…")

    if not fetch_med_detail:
        med_targets_fetch = [f for f in med_targets if f.lat is None]
        log.append(f"[Step5] 医療機関の詳細取得は省略（座標のみ使用）。座標が無い{len(med_targets_fetch)}件だけ取得")
    else:
        med_targets_fetch = med_targets
    _parallel(od_targets + med_targets_fetch, _fetch_med, workers, _med_prog)

    for nmf in med_targets:
        if nmf.lat is not None:
            nmf.distance_m = haversine(center_lat, center_lon, nmf.lat, nmf.lon)
        med_osm.append(nmf)

    med_osm.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(
        f"[Step5] 医療機関詳細+住所取得（{workers}並列）: 成功={stats['ok']}件 "
        f"詳細失敗={stats['detail_fail']}件 座標なし={stats['gc_fail']}件 "
        f"合計={len(med_osm)}件 ({time.time()-t0:.1f}s)"
    )
    if stats["gc_fail"]:
        log.append(
            f"[Step5] ⚠️ 座標を確定できなかった医療機関が{stats['gc_fail']}件あります。"
            "この施設は門前判定・按分に使われないため、周辺薬局の評価がずれる場合があります。"
        )

    # ─────────────────────────────────────────────────────────────────────
    # Phase 2: 推考フェーズ（v1.3: 既定OFF。同一条件の再検索のため成果がほぼ無く時間だけ増えるため）
    # ─────────────────────────────────────────────────────────────────────
    # v1.4: 旧版（v1.3）はまったく同じ条件で再検索していたため、原理的に新しい
    # 施設は1件も出てこなかった（時間だけを消費していたので既定OFFにされていた）。
    # v1.4では「1段広い距離コードで検索し、こちら側で実距離を測り直して圏内の
    # ものだけ拾う」方式に変えた。ナビィの距離絞り込みは施設の登録座標に依存する
    # ため、登録座標がずれている施設はこの方式でしか拾えない。実際に取りこぼしを
    # 回収できるようになったので既定ONに戻している。
    if not verify_pass:
        log.append("[Step6-7] 広域再検索（漏れ確認）はスキップしました"
                   "（サイドバーでONにできます）。")
    else:
        # Step 6: 【医療機関 漏れ確認】1段広い距離コードで再検索
        prog.progress(52, text="Step6（漏れ確認①）: 医療機関を広域再検索中…")
        t0 = time.time()
        existing_med_kikan_cds = {f.kikan_cd for f in med_osm if f.kikan_cd}
        wide_code = wider_dist_code(dist_code_for(med_radius))
        verify_meds, verify_msg = scraper.search_medical_by_latlon(
            center_lat, center_lon, radius_m=med_radius,
            center_name=address[:20], dist_code_override=wide_code,
        )
        log.append(f"[Step6] 広域再検索（距離コード'{wide_code}'）: {verify_msg}")
        add_meds = []
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
            # v2.1: 一覧に埋め込まれた座標で明らかに圏外（+1km超）のものは詳細を取りに行かない
            # （広域検索は件数が数倍になり、ここが1店舗あたりの所要時間の大半を占めていた）
            if (vf.lat is not None
                    and haversine(center_lat, center_lon, vf.lat, vf.lon) > med_radius + 1_000):
                continue
            add_meds.append(vf)
        _parallel(add_meds, _fetch_med, workers)
        added_med, med_out_of_range = 0, 0
        for vf in add_meds:
            if vf.lat is not None:
                vf.distance_m = haversine(center_lat, center_lon, vf.lat, vf.lon)
                if vf.distance_m > med_radius:      # 広めに取ったぶんを実距離で切る
                    med_out_of_range += 1
                    continue
            vf.source = "mhlw(推考①追加)"
            med_osm.append(vf)
            added_med += 1
        med_osm.sort(key=lambda x: x.distance_m or 9_999_999)
        log.append(
            f"[Step6] 漏れ確認①: 医療機関 {added_med}件を追加で発見 "
            f"（広域再検索{len(verify_meds)}件を確認 / 実距離で圏外だった{med_out_of_range}件は除外） "
            f"({time.time()-t0:.1f}s)"
        )
        if added_med:
            log.append(
                f"[Step6] ⚠️ 通常検索で取りきれていなかった医療機関が{added_med}件ありました"
                "（広域再検索で回収済み。出典が「推考①追加」の行です）。"
            )

        # Step 7: 【薬局 漏れ確認】1段広い距離コードで再検索
        prog.progress(62, text="Step7（漏れ確認②）: 薬局を広域再検索中…")
        t0 = time.time()
        existing_ph_kikan_cds = {p.kikan_cd for p in ph_merged if p.kikan_cd}
        wide_ph_code = wider_dist_code(dist_code_for(radius_m))
        verify_phs, verify_ph_msg = scraper.search_pharmacies_by_latlon(
            center_lat, center_lon, radius_m=radius_m,
            center_name=address[:20], dist_code_override=wide_ph_code,
        )
        log.append(f"[Step7] 広域再検索（距離コード'{wide_ph_code}'）: {verify_ph_msg}")
        add_phs = []
        for vph in verify_phs:
            if vph.kikan_cd and vph.kikan_cd in existing_ph_kikan_cds:
                continue
            if is_duplicate_of_any(vph, ph_merged, DEDUP_GAP_M):
                continue
            if vph.kikan_cd:
                existing_ph_kikan_cds.add(vph.kikan_cd)
            if (vph.lat is not None
                    and haversine(center_lat, center_lon, vph.lat, vph.lon) > radius_m + 1_000):
                continue     # v2.1: 一覧座標で明らかに圏外のものは除く
            add_phs.append(vph)
        _parallel([p for p in add_phs if p.lat is None and p.address], _gc_ph, min(workers, 4))
        added_ph, ph_out_of_range = 0, 0
        for vph in add_phs:
            if vph.lat is not None:
                vph.distance_m = haversine(center_lat, center_lon, vph.lat, vph.lon)
                if vph.distance_m > radius_m * 1.1:   # 広めに取ったぶんを実距離で切る
                    ph_out_of_range += 1
                    continue
            vph.source = "mhlw(推考②追加)"
            ph_merged.append(vph)
            added_ph += 1
        ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
        log.append(
            f"[Step7] 漏れ確認②: 薬局 {added_ph}件を追加で発見 "
            f"（広域再検索{len(verify_phs)}件を確認 / 実距離で圏外だった{ph_out_of_range}件は除外） "
            f"({time.time()-t0:.1f}s)"
        )
        if added_ph:
            log.append(
                f"[Step7] ⚠️ 通常検索で取りきれていなかった薬局が{added_ph}件ありました"
                "（広域再検索で回収済み。出典が「推考②追加」の行です）。"
            )

    # Step 8: 【距離整合性チェック】
    prog.progress(70, text="Step8: 距離整合性チェック中…")
    t0 = time.time()
    warnings_med = 0
    for fac in med_osm:
        if fac.lat is not None and fac.lon is not None:
            actual_dist = haversine(center_lat, center_lon, fac.lat, fac.lon)
            fac.distance_m = actual_dist
            if actual_dist > med_radius + 500:
                warnings_med += 1
                # v2.1: ⚠️を外した。ナビィの医療機関検索は1km/5km単位なので、半径より遠い
                # 施設が入るのは正常（ハフ按分では距離減衰でほぼ0になる）。v1.4ではこの行が
                # 取りこぼし診断を埋め尽くし、本当に見るべき警告が埋もれていた。
                log.append(
                    f"  [Step8] 半径外(医療・ナビィ検索範囲内のため正常): {fac.name[:20]} "
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
        f"[Step8] 距離整合性チェック完了 "
        f"医療機関警告={warnings_med}件 薬局除外={removed_ph}件 ({time.time()-t0:.1f}s)"
    )

    # ─────────────────────────────────────────────────────────────────────
    # Phase 3: 詳細取得 & 判定
    # ─────────────────────────────────────────────────────────────────────

    # Step 8.4 (v2.1): 同名・同住所の重複登録を1件にまとめる（競合の二重計上を防ぐ）
    _navii_codes = {x.kikan_cd for x in (list(navvi_phs) + list(navvi_meds)
                                        + list(locals().get("verify_phs", []))
                                        + list(locals().get("verify_meds", []))) if x.kikan_cd}
    # 薬局は、重複しているコードの両方の詳細を先に取り、処方箋数が取れるほうを残す
    _dup_ph = [p for grp in duplicate_registration_groups(ph_merged) for p in grp
               if p.pref_cd and p.kikan_cd and not p.detail_fetched]
    _parallel(_dup_ph, scraper.get_pharmacy_detail, workers)
    ph_merged, _rm_ph = merge_duplicate_registrations(ph_merged, _navii_codes)
    med_osm, _rm_med = merge_duplicate_registrations(med_osm, _navii_codes)
    merged_rows = [{"種別": kind, "施設名": kept.name, "住所": kept.address,
                    "距離(m)": round(kept.distance_m) if kept.distance_m is not None else None,
                    "内容": f"同名・同住所の重複登録を1件に統合（除外コード{gone.kikan_cd}／残したコード{kept.kikan_cd}）"}
                   for kind, pairs in (("薬局", _rm_ph), ("医科", _rm_med)) for gone, kept in pairs]
    if merged_rows:
        log.append(f"[Step8.4] ⚠️ 同名・同住所で別コードの重複登録を{len(merged_rows)}件統合しました"
                   "（競合の二重計上を防止。取りこぼし診断に一覧を表示）。")

    # Step 8.5 (v2.1): 厚生局名簿（保険医療機関・保険薬局の公式名簿）と突き合わせる
    if use_kouseikyoku:
        prog.progress(72, text="Step8.5: 厚生局名簿と突き合わせ中…")
        t0 = time.time()
        pref = None
        if od_df is not None:
            near = od_nearby(od_df, center_lat, center_lon, med_radius, ["pharmacy"] + OD_MED_KINDS)
            if not near.empty:
                pref = near["pref"].mode().iloc[0]
        pref = pref or pref_code_of_text(address) or next(
            (f.pref_cd for f in med_osm + ph_merged if f.pref_cd), None)
        try:
            xc = crosscheck_kouseikyoku(center_lat, center_lon, radius_m * 1.1, med_radius,
                                        med_osm, ph_merged, od_df, pref, geocoder, log)
        except Exception as e:
            xc = {"status": [f"厚生局名簿との突き合わせでエラー（{type(e).__name__}: {e}）"],
                  "added": [], "suspended": [], "not_in_kb": []}
            log.append(f"[名簿照合] ⚠️ 突き合わせを完了できませんでした（{type(e).__name__}）。")
        for line in xc["status"]:
            log.append(f"[名簿照合] 使用名簿 {line}")
        log.append(f"[Step8.5] 厚生局名簿の突き合わせ完了 ({time.time()-t0:.1f}s)")
    else:
        xc = {"status": ["厚生局名簿との突き合わせはOFF"], "added": [], "suspended": [], "not_in_kb": []}
    xc["merged"] = merged_rows
    if xcheck_out is not None:
        xcheck_out.update(xc)

    # Step 9: 薬局詳細ページから年間処方箋数を並列取得（v1.3。旧版は1件ごとに0.5秒待ちで逐次）
    prog.progress(74, text="Step9: 薬局詳細（年間処方箋数）を取得中…")
    t0 = time.time()
    ph_targets = [p for p in ph_merged if p.pref_cd and p.kikan_cd and not p.detail_fetched][:max_detail]
    n_ph = len(ph_targets)
    log.append(f"[Step9] 薬局詳細取得対象: {n_ph}件")

    def _ph_prog(i, item):
        prog.progress(74 + int(18 * (i + 1) / max(n_ph, 1)),
                      text=f"Step9: 薬局詳細を並列取得中 {i+1}/{n_ph}件…")

    _parallel(ph_targets, scraper.get_pharmacy_detail, workers, _ph_prog)
    fetched_ph = sum(1 for p in ph_merged if p.detail_fetched)
    # 詳細ページで座標が更新された薬局の距離を再計算（門前判定の精度向上）
    for ph in ph_merged:
        if ph.lat is not None and ph.lon is not None:
            ph.distance_m = haversine(center_lat, center_lon, ph.lat, ph.lon)
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    log.append(f"[Step9] 薬局詳細取得完了（{workers}並列）: {fetched_ph}件 ({time.time()-t0:.1f}s)")

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

    # v2.1: ナビィ停止・前回取得分の使用状況
    if scraper.down:
        log.append("[ナビィ] ⚠️ ナビィに接続できませんでした（停止・メンテナンス・仕様変更の可能性）。"
                   "施設リストは公式オープンデータ、外来患者数・処方箋数は前回取得分で計算しています。")
    if scraper.snapshot_used:
        dates = sorted(v[:10] for v in scraper.snapshot_used.values())
        log.append(f"[ナビィ] ⚠️ {len(scraper.snapshot_used)}ページはナビィから取れず、前回取得分"
                   f"（{dates[0]}〜{dates[-1]}）を使いました。出典に『前回取得』と表示しています。")

    # v1.4: スクレイパー側で記録した打ち切り警告をログに合流させる
    for w in getattr(scraper, "last_warnings", []):
        log.append(f"[取りこぼし] ⚠️ {w}")

    med_osm.sort(key=lambda x: x.distance_m or 9_999_999)
    ph_merged.sort(key=lambda x: x.distance_m or 9_999_999)
    n_op_unknown = sum(1 for f in med_osm if f.daily_outpatients is None)
    log.append(
        f"[完了] 医療機関={len(med_osm)}件 薬局={len(ph_merged)}件 "
        f"（座標あり医療機関: {sum(1 for f in med_osm if f.lat is not None)}件 / "
        f"外来患者数が不明: {n_op_unknown}件） 所要 {time.time()-t_all:.1f}s"
    )
    return med_osm, ph_merged, center_lat, center_lon



# ════════════ 画面側で使う補助関数（薬局出店分析ツール v2.2 と同じ） ════════════
from streamlit_folium import st_folium


def _num(v):
    """pandas由来のNaN/None/空 を None に、数値は float にする。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # NaN 除外


_EXT_CATS = ["院外のみ", "院内外どちらも", "院内のみ", "不明"]

# ── 診療科別の処方箋発行率（＝そもそも処方箋を出す割合。院外率とは別物） ──────────────
# 【根拠】
#  ・院外“処方率”は厚労省・社会医療診療行為別統計 令和6年で全体81.4%（＝院外率0.8313に反映済）。
#  ・受診のうち投薬に至る割合（発行率）の診療科差は、日医総研RR113「診療所の診療科特性」表5.3.1
#    （厚労省 社会医療診療行為別統計より作成）の“診療科別・入院外・投薬の点数構成比(2020)”で裏づけ：
#      内科20.4% 皮膚22.0% 産婦21.1% 外科16.3% 精神15.1% 耳鼻14.9% 泌尿14.8% 整形12.8% 眼科11.8% 小児11.1%
#    整形外科は投薬が低く代わりに「その他(リハビリ等)」28.8%・画像13.4%、眼科は検査43.6%＋手術20.0%が主体。
#  ・ただし点数構成比は“収益の内訳”で発行「頻度」とは別物（小児=医学管理料主体/精神=精神療法主体で
#    点数は低いが投薬頻度は高い）。そこで点数構成比を方向性の根拠とし、臨床実態で補正した初期値とした。
#  ・下表の件数加重平均は約0.803で、再較正時のフラット値0.8054とほぼ一致（＝全体水準は保ちつつ科別に再配分）。
#  ・値はサイドバー（科ごと）・医療機関表（施設ごと）で編集可能。
DEFAULT_ISSUE = 0.8054  # その他・不明・大病院はこの一律値（従来値）
DEPT_DEFAULTS = [
    ("内科系", 0.90), ("精神科", 0.92), ("小児科", 0.88), ("耳鼻咽喉科", 0.85),
    ("皮膚科", 0.85), ("泌尿器科", 0.75), ("産婦人科", 0.60), ("眼科", 0.65),
    ("整形外科", 0.55), ("外科", 0.60), ("リハビリ科", 0.20), ("美容", 0.00),
    ("病院", DEFAULT_ISSUE), ("その他", DEFAULT_ISSUE),
]
DEPT_OPTIONS = [k for k, _ in DEPT_DEFAULTS]
# 判定キーワード（上から順に最初に一致したバケットを採用。整形は外科より前・内科系は外科より前）
DEPT_KWS = [
    ("美容", ["美容"]),
    ("整形外科", ["整形"]),
    ("リハビリ科", ["リハビリ", "リハビリテーション"]),
    ("眼科", ["眼科"]),
    ("耳鼻咽喉科", ["耳鼻", "咽喉"]),
    ("皮膚科", ["皮膚", "スキン"]),
    ("泌尿器科", ["泌尿"]),
    ("産婦人科", ["産婦", "婦人科", "産科"]),
    ("精神科", ["精神", "心療", "メンタル"]),
    ("小児科", ["小児"]),
    ("内科系", ["内科", "糖尿", "代謝", "循環器", "呼吸器", "消化器", "胃腸", "腎臓",
              "内分泌", "血液", "神経内科", "アレルギー", "リウマチ", "感染症", "ペイン", "在宅"]),
    ("外科", ["外科"]),
]


def get_dept_rates():
    """診療科→発行率の現在値（サイドバーで編集可能。session_stateに保持）。"""
    dr = st.session_state.get("dept_rates")
    if not dr:
        dr = {k: v for k, v in DEPT_DEFAULTS}
        st.session_state["dept_rates"] = dr
    return dr


def bucket_of_med(fac):
    """医療機関の診療科バケットを判定。大病院(20床以上/病院)は『病院』、美容は『美容』。"""
    if getattr(fac, "is_cosmetic", False):
        return "美容"
    cat = getattr(fac, "facility_category", "") or ""
    beds = getattr(fac, "beds", 0) or 0
    if ("病院" in cat and "診療所" not in cat) or (beds and beds >= 20):
        return "病院"  # 大病院は外来が全科合算のため一律（=DEFAULT_ISSUE）
    specs = getattr(fac, "specialties", "") or ""
    if not isinstance(specs, str):     # 念のためリスト等でも文字列化
        specs = " ".join(str(x) for x in specs)
    name = getattr(fac, "name", "") or ""
    for probe in (specs, name):  # まず標榜診療科、無ければ名称から推定
        if not probe:
            continue
        for bname, kws in DEPT_KWS:
            if any(kw in probe for kw in kws):
                return bname
    return "その他"


def eff_issue_rate(dept, override, dept_rates):
    """施設の実効発行率＝手入力override（あれば）／無ければ診療科バケットの現在値。"""
    if override is not None:
        return override
    return dept_rates.get(dept, DEFAULT_ISSUE)


# ── 診療（開局）曜日・時間の抽出（ナビィ詳細ページの raw_fields から） ─────────────────
# ナビィは月〜日＋祝の8列を "/" 連結した時間割を持つ。医療機関＝「診療時間（診療科目別の）…」、
# 薬局＝「時間帯１」。曜日・時間は必ずこの8列時間割から index で導出する
# （"営業日""診療日" 等の直接キーは、他項目やツールチップ文言への部分一致で誤取得するため使わない）。
_WEEK_LABELS = ["月", "火", "水", "木", "金", "土", "日", "祝"]
_SCHEDULE_KEYS = [
    "時間帯１", "時間帯1",                 # 薬局（開局時間）
    "診療時間（診療科目別の）",             # 医療機関（診療科目別の診療時間）
    "外来受付時間（診療科目別の）",         # 医療機関（外来受付時間）
]


def parse_open_schedule(fields):
    """raw_fields から (開いている曜日, 代表的な時間帯) を作る。取得できなければ ("", "")。
    例: 内山皮膚科→("月火水金土","09:00-12:00") / マルヤマ薬局→("月火水木金土","09:00-19:00")。"""
    if not fields:
        return "", ""
    sv = None
    for key in _SCHEDULE_KEYS:                 # 8列の時間割フィールドを探す
        for fk, fv in fields.items():
            if key in fk and "/" in fv and re.search(r"\d{1,2}:\d{2}", fv):
                sv = fv
                break
        if sv:
            break
    if not sv:
        return "", ""
    parts = [p.strip() for p in sv.split("/")]
    open_days, times = [], []
    for i, p in enumerate(parts[:8]):          # 0..7 = 月火水木金土日祝
        if p and re.search(r"\d{1,2}:\d{2}", p):
            if i < len(_WEEK_LABELS):
                open_days.append(_WEEK_LABELS[i])
            times.append(re.sub(r"\s+", "", p))
    days_str = "".join(open_days)
    hours_str = max(set(times), key=times.count) if times else ""   # 代表＝最頻の時間帯
    return days_str, hours_str


def rx_category(fac):
    """ナビィの院内/院外処方フィールドから、院外のみ/院内外どちらも/院内のみ/不明を判定。"""
    ih = (getattr(fac, "inhouse_rx", "") or "")
    op = (getattr(fac, "outpatient_rx", "") or "")
    has_in, has_out = ("有" in ih), ("有" in op)
    if has_out and has_in:
        return "院内外どちらも"
    if has_out and not has_in:
        return "院外のみ"
    if has_in and not has_out:
        return "院内のみ"
    s = getattr(fac, "rx_summary", "") or ""
    if s.startswith("院外処方あり"):
        return "院内外どちらも"
    if s == "院内処方のみ":
        return "院内のみ"
    return "不明"




# ════════════════════════════════ 薬局ファインダー 本体 ════════════════════════════════
import traceback

APP_VERSION = "2.1"


def get_scraper() -> MHLWScraper:
    """v2.1: スクレイパーは利用者（セッション）ごとに1つ持つ。
    旧版は全利用者で1つを共有していたため、Streamlit Cloud で同時に検索すると
    ナビィの検索セッションが混ざるおそれがあった。前回取得分の保存（sqlite）だけは共有する。"""
    sc = st.session_state.get("_scraper")
    if sc is None:
        sc = MHLWScraper()
        st.session_state["_scraper"] = sc
    return sc


@st.cache_data(ttl=900, show_spinner=False)
def cached_navii_check():
    """ナビィ構造チェック。15分に1回だけ実際にアクセスする。"""
    try:
        return navii_quick_check()
    except Exception as e:
        return [("ng", f"チェック中にエラー（{type(e).__name__}）")]


class _Prog:
    """run_analysis の進捗(0〜100の整数)を st.progress に流す。"""
    def __init__(self, bar):
        self.bar = bar

    def progress(self, v, text=""):
        try:
            self.bar.progress(min(100, max(0, int(v))), text=text)
        except Exception:
            pass


st.title(f"💊 商圏内 調剤薬局リストアップ（薬局ファインダー）v{APP_VERSION}")
st.caption("住所と商圏半径を入力すると、圏内の調剤薬局を一覧表示します。"
           "厚労省の公式データを土台にし、ナビィが止まっていても一覧を出せます。")

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
        help=("ナビィから処方箋数を取得する上限件数（時間に影響します）。"
              "※この上限を超えても薬局そのものは一覧に出ます（処方箋数が空欄になるだけです）"),
    )
    use_osm = st.checkbox("OSM(OpenStreetMap)も併用する", value=False,
                          help="公式データ・ナビィに無い店を補います。公式データが土台になったため既定OFF"
                               "（OSMのサーバが遅いと最大30秒待つため）。")
    run_btn = st.button("🔍 検索実行", type="primary", use_container_width=True)

    st.divider()
    st.subheader("🗂 データ源")
    with st.spinner("ナビィの状態を確認中…"):
        _hc = cached_navii_check()
    _hc_ng = [m for s_, m in _hc if s_ == "ng"]
    if _hc_ng:
        st.error("🔴 ナビィ：異常あり → 公式データ＋前回取得分で動きます")
    elif any(s_ == "warn" for s_, _ in _hc):
        st.warning("🟡 ナビィ：一部注意")
    else:
        st.success("🟢 ナビィ：正常")
    with st.expander("ナビィ構造チェックの詳細", expanded=bool(_hc_ng)):
        _hc_icon = {"ok": "✅", "ng": "❌", "warn": "⚠️"}
        for s_, m in _hc:
            st.markdown(f"{_hc_icon.get(s_, '・')} {m}")
        if st.button("再チェック", key="navii_recheck"):
            cached_navii_check.clear()
            st.rerun()
    use_od = st.checkbox("公式オープンデータを土台にする（推奨）", value=True,
                         help="厚労省が公開する全国の薬局一覧（緯度経度付き・半年ごと更新）を土台にします。")
    _odm = od_meta()
    if od_path():
        _d = str(_odm.get("date", ""))
        st.caption(f"保存済み：{_d[:4]}/{_d[4:6]}/{_d[6:]}時点版")
    else:
        st.caption("未取得です。最初の検索時に自動でダウンロードします（1〜2分・初回のみ）。")
    use_kb = st.checkbox("厚生局名簿と突き合わせる（推奨）", value=True,
                         help="保険薬局の公式名簿（地方厚生局・毎月更新）と照合し、"
                              "一覧に無い店を自動で追加、休止・名簿に無い店に🟠を付けます。")
    st.caption("データソース: 厚労省 オープンデータ・ナビィ / 地方厚生局 / OpenStreetMap / 国土地理院")

# ── セッション初期化 ──────────────────────────────────────────────────────────
for _k, _v in {"ph_results": [], "med_results": [], "center_lat": None, "center_lon": None,
               "search_log": [], "last_address": "", "last_radius": None, "last_gate": None,
               "xcheck": {}, "od_msg": ""}.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── 検索実行 ──────────────────────────────────────────────────────────────────
if run_btn and address_input.strip():
    log: List[str] = []
    bar = st.progress(0, text="検索を開始しています…")
    try:
        od_df, od_msg = None, "公式オープンデータはOFF"
        if use_od:
            bar.progress(1, text="公式オープンデータを準備中…（初回のみ1〜2分）")
            od_df, od_msg = od_ensure()
        log.append(f"[データ源] {od_msg}")
        xc: dict = {}
        med_list, ph_list, clat, clon = run_analysis(
            address_input.strip(), int(radius_m), int(gate_m), int(max_detail), log, _Prog(bar),
            workers=FETCH_WORKERS_DEFAULT, verify_pass=False, use_osm=bool(use_osm),
            od_df=od_df, use_kouseikyoku=bool(use_kb), xcheck_out=xc,
            fetch_med_detail=False,      # 医療機関は門前判定の座標だけ使う
        )
        st.session_state.ph_results = ph_list
        st.session_state.med_results = med_list
        st.session_state.center_lat = clat
        st.session_state.center_lon = clon
        st.session_state.search_log = log
        st.session_state.last_address = address_input.strip()
        st.session_state.last_radius = int(radius_m)     # v2.1: 検索時の半径を保存（表示ずれ防止）
        st.session_state.last_gate = int(gate_m)
        st.session_state.xcheck = xc
        st.session_state.od_msg = od_msg
        bar.progress(100, text="✅ 完了!")
        time.sleep(0.3)
        bar.empty()
    except Exception as e:
        bar.empty()
        if e.__class__.__name__ in ("StopException", "RerunException"):
            raise
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
    med_facs: List[MedFacility] = st.session_state.med_results
    center_lat = st.session_state.center_lat
    center_lon = st.session_state.center_lon
    s_radius = st.session_state.last_radius or radius_m
    s_gate = st.session_state.last_gate or gate_m

    n_total = len(pharmacies)
    n_monzen = sum(1 for p in pharmacies if p.pharmacy_type == "門前薬局")
    n_men = sum(1 for p in pharmacies if p.pharmacy_type == "面薬局")
    rx_vals = [p.annual_rx_count for p in pharmacies if p.annual_rx_count]
    avg_rx = int(sum(rx_vals) / len(rx_vals)) if rx_vals else None
    n_review = sum(1 for p in pharmacies if p.review_note)

    st.success(
        f"**{st.session_state.last_address}** の {s_radius:,}m 商圏内: "
        f"**{n_total} 件** の調剤薬局が見つかりました（門前判定閾値: {s_gate}m）"
    )
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("調剤薬局 合計", f"{n_total} 件")
    col2.metric("🔴 門前薬局", f"{n_monzen} 件",
                delta=f"{n_monzen / n_total * 100:.0f}%" if n_total else "0%", delta_color="off")
    col3.metric("🔵 面薬局", f"{n_men} 件",
                delta=f"{n_men / n_total * 100:.0f}%" if n_total else "0%", delta_color="off")
    col4.metric("年間処方箋数 平均", f"{avg_rx:,} 件" if avg_rx else "— 件",
                help="処方箋数取得済み薬局の平均")
    if n_review:
        st.warning(f"🟠 **{n_review}件** は公式名簿との照合で要確認です（一覧の『確認』列・ログタブの取りこぼし診断）。")

    st.divider()
    tab_list, tab_map, tab_log = st.tabs(["📋 薬局一覧", "🗺️ 地図", "📝 ログ・取りこぼし診断"])

    with tab_list:
        TYPE_ICONS = {"門前薬局": "🔴", "面薬局": "🔵", "不明": "⚪"}
        filter_col1, filter_col2 = st.columns([2, 2])
        with filter_col1:
            type_filter = st.multiselect("種別フィルタ", options=["門前薬局", "面薬局", "不明"],
                                         default=["門前薬局", "面薬局", "不明"])
        with filter_col2:
            sort_by = st.selectbox("並び替え", options=["中心からの距離", "年間処方箋数（多い順）", "種別"], index=0)

        filtered = [p for p in pharmacies if p.pharmacy_type in type_filter]
        if sort_by == "年間処方箋数（多い順）":
            filtered.sort(key=lambda p: p.annual_rx_count or 0, reverse=True)
        elif sort_by == "種別":
            order = {"門前薬局": 0, "面薬局": 1, "不明": 2}
            filtered.sort(key=lambda p: (order.get(p.pharmacy_type, 9), p.distance_m or 9_999_999))
        else:
            filtered.sort(key=lambda p: p.distance_m or 9_999_999)

        def _link(p):
            return p.detail_url or p.href or ""

        rows = []
        for i, ph in enumerate(filtered, 1):
            days, hours = parse_open_schedule(getattr(ph, "raw_fields", None))
            rows.append({
                "No": i,
                "確認": ("🟠 " + ph.review_note) if ph.review_note else "",
                "薬局名": ph.name,
                "住所": ph.address or "—",
                "中心からの距離": f"{int(ph.distance_m)}m" if ph.distance_m is not None else "—",
                "種別": f"{TYPE_ICONS.get(ph.pharmacy_type, '⚪')} {ph.pharmacy_type}",
                "最近接医療機関": ph.nearest_clinic_name,
                "最近接距離": (f"{int(ph.nearest_clinic_dist_m)}m"
                            if ph.nearest_clinic_dist_m is not None else "—"),
                "年間処方箋数": f"{ph.annual_rx_count:,}" if ph.annual_rx_count else "—",
                "処方箋数出典": ph.annual_rx_source if ph.annual_rx_count else "—",
                "週営業日数": ph.od_op_days,
                "開局日": days,
                "データ元": ph.source,
                "ナビィ": _link(ph) or None,
            })
        st.dataframe(
            pd.DataFrame(rows), use_container_width=True, hide_index=True,
            column_config={
                "ナビィ": st.column_config.LinkColumn("ナビィ", display_text="開く"),
                "No": st.column_config.NumberColumn(width="small"),
                "確認": st.column_config.TextColumn(width="medium"),
                "種別": st.column_config.TextColumn(width="medium"),
                "中心からの距離": st.column_config.TextColumn(width="small"),
                "最近接距離": st.column_config.TextColumn(width="small"),
                "週営業日数": st.column_config.NumberColumn("週営業日数", format="%d 日",
                                                        help="公式オープンデータの営業曜日数"),
            },
        )

        csv_rows = []
        for ph in filtered:
            csv_rows.append({
                "薬局名": ph.name,
                "住所": ph.address,
                "中心からの距離_m": int(ph.distance_m) if ph.distance_m else "",
                "種別": ph.pharmacy_type,
                "最近接医療機関": ph.nearest_clinic_name,
                "最近接距離_m": int(ph.nearest_clinic_dist_m) if ph.nearest_clinic_dist_m else "",
                "年間処方箋数": ph.annual_rx_count or "",
                "処方箋数出典": ph.annual_rx_source if ph.annual_rx_count else "",
                "週営業日数": ph.od_op_days or "",
                "名簿照合": ph.review_note,
                "ナビィURL": _link(ph),
                "データソース": ph.source,
                "緯度": ph.lat or "",
                "経度": ph.lon or "",
            })
        st.download_button(
            "⬇️ CSVダウンロード",
            data=pd.DataFrame(csv_rows).to_csv(index=False).encode("utf-8-sig"),
            file_name=f"薬局_{st.session_state.last_address[:15]}_{s_radius}m.csv",
            mime="text/csv",
        )

    with tab_map:
        m = folium.Map(location=[center_lat, center_lon], zoom_start=15)
        folium.Circle(location=[center_lat, center_lon], radius=s_radius,
                      color="gray", fill=True, fill_opacity=0.05,
                      tooltip=f"商圏: {s_radius:,}m").add_to(m)
        folium.Marker(location=[center_lat, center_lon], tooltip=st.session_state.last_address,
                      icon=folium.Icon(color="blue", icon="home", prefix="fa")).add_to(m)
        color_map = {"門前薬局": "red", "面薬局": "green", "不明": "gray"}
        for ph in pharmacies:
            if ph.lat is None or ph.lon is None:
                continue
            col = "orange" if ph.review_note else color_map.get(ph.pharmacy_type, "gray")
            rx_txt = f"{ph.annual_rx_count:,} 枚/年" if ph.annual_rx_count else "処方箋数 不明"
            nearest_txt = (f"{ph.nearest_clinic_name}（{int(ph.nearest_clinic_dist_m)}m）"
                           if ph.nearest_clinic_dist_m is not None else "—")
            popup_html = (f"<b>{ph.name}</b><br>種別: {ph.pharmacy_type}<br>"
                          f"最近接医療機関: {nearest_txt}<br>年間処方箋数: {rx_txt}<br>"
                          f"住所: {ph.address or '—'}"
                          + (f"<br><span style='color:#c2410c'>🟠 {ph.review_note}</span>"
                             if ph.review_note else ""))
            folium.CircleMarker(location=[ph.lat, ph.lon], radius=8,
                                color=col, fill=True, fill_color=col, fill_opacity=0.8,
                                popup=folium.Popup(popup_html, max_width=280),
                                tooltip=f"{ph.name}（{ph.pharmacy_type}）").add_to(m)
        for fac in med_facs:
            if fac.lat is None or fac.lon is None:
                continue
            if fac.distance_m is not None and fac.distance_m > s_radius + 1000:
                continue       # 地図が重くならないよう、商圏から遠い医療機関は描かない
            folium.CircleMarker(location=[fac.lat, fac.lon], radius=5,
                                color="purple", fill=True, fill_color="purple", fill_opacity=0.6,
                                tooltip=f"🏥 {fac.name}").add_to(m)
        legend_html = """
        <div style="position:fixed; bottom:30px; left:30px; z-index:1000;
                    background:white; padding:10px; border-radius:8px;
                    border:1px solid #ccc; font-size:13px;">
          <b>凡例</b><br>🔴 門前薬局<br>🟢 面薬局<br>⚪ 不明<br>🟠 名簿照合で要確認<br>🟣 医療機関
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))
        st_folium(m, use_container_width=True, height=600)

    with tab_log:
        st.subheader("🩺 取りこぼし診断")
        st.caption(st.session_state.od_msg)
        xc = st.session_state.xcheck or {}
        xrows = []
        for grp in ("added", "suspended", "not_in_kb", "merged"):
            for r in xc.get(grp, []):
                if r.get("種別") != "薬局":
                    continue
                xrows.append({"内容": r.get("内容", ""), "施設名": r.get("施設名", ""),
                              "距離(m)": r.get("距離(m)"), "住所": r.get("住所", "")})
        st.markdown("**① 公式名簿との差分（目視確認はこの表の薬局だけで済みます）**")
        if xrows:
            st.caption("『名簿のみ』は一覧に自動追加済み、『重複登録を統合』は1件にまとめ済み、"
                       "『休止』『名簿に無し』は一覧に残したまま🟠を付けています。")
            st.dataframe(pd.DataFrame(xrows), hide_index=True, use_container_width=True)
        else:
            st.success("公式名簿との食い違いはありません。")
        for line in xc.get("status", []):
            if line.startswith("薬局"):
                st.caption(f"使用名簿 {line}")
        st.markdown("**② 取得処理の警告**")
        alerts = [l for l in st.session_state.search_log
                  if "⚠️" in l and not l.startswith("  ") and "医科" not in l]
        if alerts:
            for a in alerts:
                st.warning(a)
        else:
            st.success("取得処理の警告はありません。")
        st.divider()
        st.subheader("検索ログ")
        for line in st.session_state.search_log:
            st.text(line)

else:
    st.info("← 左のサイドバーで住所と条件を設定し、「検索実行」を押してください。")
