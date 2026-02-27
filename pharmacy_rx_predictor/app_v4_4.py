"""
薬局 年間処方箋枚数 多面的予測ツール v4.4
==========================================
v4.4 主な変更点: SM業態M2過大推計修正 + 密度帯ラベルバグ修正 + ローカルMHLW校正

  【v4.4 改善概要】

  ■ 問題の背景:
    v4.3でスーパーマーケット内薬局の商圏半径を800〜3,000mに拡張したところ、
    M2（商圏人口動態アプローチ）の推計値がM1（医療機関アプローチ）の2倍以上に
    膨れ上がる過大推計が発生した（例: 国立市でM2≈25,000枚 vs M1≈12,000枚）。

  ■ 根本原因の分析:
    1. 密度帯ラベル不一致バグ（v4.2〜v4.3の潜在バグ）
       _density_band() が「高密度(5k-10k)」という長形式を返すが、
       SUPERMARKET_RADIUS_TABLE・DENSITY_AGE_DISTRIBUTION は「高密度」という
       短形式キーを使用していたため、.get() が常にデフォルト値(1,200m / 全国平均)を
       返していた。→ SM半径・年齢分布が密度帯に関わらず固定値になっていた。

    2. SM業態固有の処方箋流入特性を未考慮
       `_inflow_coefficient`（流入係数1.05〜1.60）はクリニック近接型の
       通常薬局を前提に設計されており、SM業態に適用すると過大推計になる。
       SM来客は「処方箋を持った患者」ではなく「買い物客」であるため、
       処方箋化率が通常薬局より低い。

  ■ v4.4 改善内容:

  1. 密度帯ラベル不一致バグ修正 (_density_band_label 追加)
     校正エンジン用の長形式ラベル「高密度(5k-10k)」と、
     テーブルルックアップ用の短形式ラベル「高密度」を分離。
     calc_commercial_radius / Method2Predictor でショートラベルを使用し、
     SM半径・年齢分布が密度帯ごとに正しく適用されるよう修正。

  2. SM業態専用流入係数 (SM_INFLOW_COEFFICIENT_RATIO = 0.40)
     スーパー来客が全て処方箋保有者でないことを考慮した業態補正。
     通常値（1.05〜1.60）に0.40を乗算することで処方箋化率を補正。
     根拠: 慢性処方はかかりつけ薬局（医療機関近隣）に帰属し、SM捕捉は
     主に急性処方（~35%）のみ。実効捕捉率~28%を切り上げ0.40採用。
       通常薬局: 商圏人口×受診率→処方箋（医療機関近接で流入多い）
       SM薬局:   商圏人口×受診率×0.40（慢性処方は習慣薬局へ流出）

  3. SM業態市場シェア上限調整 (SM_MARKET_SHARE_CAP = 0.55)
     広域商圏ではシェアの上限を55%に引き下げる（通常80%から調整）。
     SM業態では処方箋の帰属が「かかりつけ医近辺の薬局」に分散しやすく、
     SM薬局は補完的な位置付けになる。

  4. ローカルMHLW校正エンジン (LocalCalibrationEngine)
     対象エリアの既存MHLW登録薬局を自動収集し、同じM1・M2ロジックで
     予測を実行して実績との乖離を計測。ローカル補正係数を導出して
     新規開局予測に適用。
     「このエリアではM1が X% 過大推計、M2が Y% 過少推計」という地域特性を
     事前検証することで、最終推計の精度を向上させる。

     ワークフロー:
       1. 住所からエリアキーワード抽出（市区町村）
       2. MHLW でエリア内の薬局を検索（最大N件）
       3. 各薬局を住所のみで予測（M1 + M2）
       4. 実績との比較 → ローカル補正係数 α1, α2 を計算
       5. 新規薬局予測に α1, α2, 最適ブレンド重み w* を適用

v4.3 からの継承:
  - 薬局タイプ選択（NORMAL / SUPERMARKET / GATE）
  - スーパー商圏半径テーブル（800〜3,000m）
  - CalibrationEngine（都道府県レベル校正エンジン）

v4.3 主な変更点: 薬局タイプ別商圏半径モデル（スーパー・ドラッグストア対応）

  【v4.3 改善概要】

  ■ 問題の背景:
    旧バージョンでは「近隣に医療機関がある → 門前薬局判定 → 商圏半径300m固定」となっており、
    スーパーマーケット内薬局・ドラッグストア併設薬局のように、薬局単独ではなく
    小売店舗の集客力を背景に広域から患者を集める業態で、M2が過小推計になっていた。

  ■ v4.3 改善内容:

  1. 薬局タイプの選択（PHARMACY_TYPE）
     新規開局予測モードに「薬局タイプ」選択を追加。
       ① 通常の調剤薬局（単独・門前なし）    … 従来どおりの密度ベース商圏
       ② スーパー・ドラッグストア内薬局       … スーパーの商圏（広域）を適用
       ③ 門前薬局（医療機関に隣接・専属）     … 300m固定（医療機関依存型）

  2. スーパー商圏半径テーブル（SUPERMARKET_RADIUS_TABLE）
     国土交通省・経済産業省の商業施設商圏調査と調剤薬局の捕捉圏研究をもとに設計。
     食品スーパーの一次商圏（来客の70〜80%をカバーする圏域）を基準。

     密度帯         通常商圏    スーパー商圏    根拠
     超高密度12k+   300m     →  800m    都市型スーパーは徒歩商圏でも広い
     高密度 6-12k   400m     →  1,000m
     中高密度 3-6k  500m     →  1,200m  ← 国立市はこのゾーン
     中密度 1.5-3k  700m     →  1,500m
     低密度 0.5-1.5k 1,000m  →  2,000m  郊外型スーパーの一般的商圏
     超低密度 <500  2,000m   →  3,000m  ロードサイド大型店

  3. M2のOSM検索半径も商圏に合わせて拡張
     競合薬局・医療機関の検索半径を max(商圏半径×1.5, 600m) に自動調整。

  4. 商圏半径の根拠をUI・ロジック説明に表示

v4.2 からの継承:
  - 診療科別外来患者数テーブル
  - 密度帯別年齢分布
  - スマートブレンド推奨値
  - CalibrationEngine

v4.2 主な変更点: 診療科別精度向上 + 年齢補正 + スマートブレンド

  【v4.2 改善概要】
  下記5点を改善し、未校正状態でのM1/M2精度を引き上げる。

  1. 診療科別外来患者数テーブル (SPECIALTY_OUTPATIENT_TABLE)
     「医師数だけで一律20/40/70人/日」→「診療科ごとに統計的基礎値」へ刷新。
     出典: 厚生労働省「医療施設調査」2020年 無床診療所の診療科別1日平均外来患者数。
     例) 整形外科48/日・眼科54/日・耳鼻科50/日 vs 旧一律20/日。

  2. 名前ベース診療科検出の OSM タグ補完 (search_nearby 改修)
     OSMタグに speciality が無い施設を施設名（日本語）でも診療科推定し、
     外来患者数テーブルへの照合精度を向上。

  3. 診療科別 SPECIALTY_RX_RATES バグ修正
     「歯科」「消化器内科/消化器外科」「精神科/心療内科」「神経内科/脳神経内科」
     「産婦人科/婦人科」が SPECIALTY_RX_RATES に存在せず
     "不明/その他(0.68)" にフォールバックしていた問題を修正。
     例) 歯科: 0.68→0.08、精神科/心療内科: 0.68→0.85。

  4. 人口密度帯別年齢分布 (DENSITY_AGE_DISTRIBUTION) → Method2 改善
     全国固定値ではなく都市部（高密度=若年多め）/農村部（低密度=高齢多め）
     の年齢構成差を反映。M2 の処方箋プール計算精度を向上。
     例) 超高密度: 65+20% ← 旧28.3% / 超低密度: 65+38% ← 旧28.3%。

  5. スマートブレンド推奨値 (calc_smart_blend_weight)
     未校正時の「単純平均」を、データ品質シグナルに基づく動的重みへ変更。
     シグナル: 人口密度・MHLW確認済み施設数・医療機関総数から M1/M2 の
     どちらが信頼できるかを自動判定（校正実施後は v4.1 の w* が優先）。

  6. 医療機関密集補正を滑らか指数減衰に変更 (apply_clinic_congestion_factor)
     段階的ステップ関数（0.85/0.70/0.60/0.50）→指数減衰
     factor = max(0.50, exp(−0.035×max(0, n−5)) で閾値ジャンプを廃止。

v4.1 からの継承:
  - CalibrationEngine（データドリブン校正エンジン）
  - 「🔬 モデル校正」タブ

v4.1 主な変更点: データドリブン校正エンジン

  ★ MHLWの実績処方箋データを使った自動モデル校正（新機能）

  【背景と目的】
    従来バージョンでは、デフォルト外来患者数・流入係数・シェアパラメータを
    統計資料や業界知見から手動設定していた。
    v4.1 では「MHLWに処方箋枚数が記録されている薬局（実績値）」を校正セットとして活用し、
    住所のみの情報から予測した値と実績値を比較することで、最も精度が高い
    補正係数・ブレンド重みを自動導出する。

  1. CalibrationEngine（新設）
     MHLWで都道府県・地域を指定して処方箋実績のある薬局を自動収集し、
     住所のみの情報で方法①・②による予測を実行。
     実績との誤差から以下を導出:
       ・密度帯別補正係数 α1(density), α2(density)  … 幾何平均で乗算バイアスを除去
       ・最適ブレンド重み w*  … MAPE(w)を最小化するM1:M2の混合比率

  2. 校正パラメータの適用
     - 未校正時: デフォルトパラメータで予測（v3.2相当）
     - 校正後:   final = w* × (α1 × M1) + (1-w*) × (α2 × M2)
     校正結果は「🔬 モデル校正」タブで確認・適用・CSV出力可能

  3. 新規タブ「🔬 モデル校正」
     - 都道府県・収集件数を指定してバッチ校正実行
     - MAPE / RMSE / バイアス / 密度帯別誤差 の可視化
     - 推奨補正係数の確認とワンクリック適用

  v3.2 からの継承（医療機関密集補正・デフォルト外来数改善）:
    - デフォルト外来患者数: 無床診療所≈20人/日（厚労省「医療施設調査」2020年）
    - 医療機関密集補正: 未確認施設が6件以上の場合に段階的圧縮（最大0.50×）
    - 残余シェアモデル（既存門前薬局の動的判定）

v3.2 Changelog（参照のみ）:
  1. 競合薬局の処方箋枚数を自動取得（常時実行）
     既存薬局分析モード・新規開局予測モード両方で、近隣薬局の処方箋枚数を
     分析実行時に自動でMHLWから取得（上位10薬局）
  2. 競合薬局のOSM検索タグを拡充（shop/healthcare=pharmacyも対応）
  3. 残余シェアモデル（既存門前薬局の動的判定）
  4. MHLW医療機関エリア自動補填
"""

import csv
import dataclasses
import io
import math
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

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

# 門前薬局処方箋捕捉率（v3.2）
# 厚生労働省データおよび業界実態より: 医療機関50m以内の薬局は同施設の処方箋の
# 概ね60〜80%を捕捉する。中央値として70%を採用。
# 出典: 調剤薬局業界実態調査・門前薬局シェア分析（日本薬剤師会等）
GATE_PHARMACY_CAPTURE_RATE: float = 0.70   # 既存門前薬局の推定処方箋捕捉率

# 全国統計（厚生労働省「調剤医療費の動向」2022年度）
NATIONAL_STATS = {
    "total_prescriptions": 885_000_000,
    "total_pharmacies": 61_860,
    "average_per_year": 14_305,
    "median_estimate": 8_000,
    "working_days": 305,
    "outpatient_rx_rate": 0.790,    # 院外処方率（全国平均）令和4年: 社会医療診療行為別統計
    "prescription_per_visit": 0.69,  # 外来1受診あたり処方箋発行率（医科、歯科除外・診療科加重平均）
}

# 統計数値のデータソース参照（v2.2新規追加）
STAT_REFERENCES: Dict[str, Dict] = {
    "院外処方率": {
        "value": "79.0%（令和4年度）",
        "source": "厚生労働省「社会医療診療行為別統計」令和4（2022）年",
        "url": "https://www.mhlw.go.jp/toukei/saikin/hw/sinryo/tyosa22/index.html",
        "note": "全国薬局における院外処方箋（調剤薬局で調剤）の割合。2000年代から一貫して上昇中。"
               "令和4（2022）年: 79.1%、令和5（2023）年: 80.2%、令和6（2024）年: 81.4%。"
               "本ツールは令和4年度値 79.0% を採用（v3.1更新）。病院規模・地域により差異あり。",
    },
    "処方箋発行率（外来1受診あたり）": {
        "value": "69%（医科外来のみ・歯科除外）",
        "source": "SPECIALTY_RX_RATES（診療科別処方率）の診療科シェア加重平均により算出",
        "url": "https://www.mhlw.go.jp/toukei/list/35-34.html",
        "note": "外来患者1回の受診あたり処方箋が発行される確率（医科のみ）。"
               "内科35%×76%、整形外科12%×72%、眼科9%×52%、皮膚科8%×64%等の加重平均≈69%。"
               "歯科（処方率~22%）を含む全科平均では約65%となるが、調剤薬局への処方箋は"
               "医科由来のみのため、医科加重平均の69%を採用（v3.1更新）。",
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
            "患者調査の外来受療率（1日あたり外来患者数/10万人）× 365日で導出した年間受診回数（医科＋歯科含む）。"
            "加重平均は約12.4回/人/年で、OECD統計（日本: 12.6回/人/年・2019年）と整合。"
            "なお厚生労働省「医療保険に関する基礎資料 令和4年度」の入院外受診率（件/人）は"
            "総計8.22件/年（保険請求件数ベース）だが、1件≒1.5回の換算で12.3回/年となりOECDと整合。"
            "患者調査ベースの現行値を維持（v3.1）。75歳以上は慢性疾患による継続受診が多く最も高い。"
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
        "note": "慢性疾患が多い科（循環器・糖尿病・精神科）は処方率が高く、処置・手術中心の科（眼科・リハビリ）は低い。いずれも院外処方率(79.0%)をさらに乗じる。",
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
# v4.2: _SPECIALTY_KW_MAP が生成するキー（複合名含む）を網羅。
#       バグ修正: 歯科・消化器内科/消化器外科 等が "不明/その他" にフォールバックしていた問題を解消。
SPECIALTY_RX_RATES: Dict[str, Tuple[float, str]] = {
    "一般内科":             (0.76, "内科系全般（慢性疾患が多く高処方率）"),
    "循環器内科":           (0.88, "高血圧・心疾患は継続処方が多い"),
    "消化器内科":           (0.74, "胃腸疾患は薬物療法が主体"),
    "消化器内科/消化器外科":(0.70, "内科・外科混合。外来は薬物療法中心で中程度"),  # v4.2追加
    "糖尿病内科":           (0.90, "インスリン・経口血糖降下薬の継続処方"),
    "神経内科":             (0.82, "神経疾患は薬物療法依存度高"),
    "神経内科/脳神経内科":  (0.82, "脳神経疾患の薬物療法（神経内科と同等）"),       # v4.2追加
    "呼吸器内科":           (0.78, "喘息・COPD等の継続薬多い"),
    "外科":                 (0.58, "術後フォローの処方は比較的少ない"),
    "整形外科":             (0.72, "鎮痛薬・湿布等の処方多い"),
    "皮膚科":               (0.64, "外用薬・抗アレルギー薬など"),
    "眼科":                 (0.52, "点眼薬は院内交付も多い"),
    "耳鼻咽喉科":           (0.58, "抗菌薬等の短期処方が多い"),
    "精神科":               (0.85, "向精神薬は継続処方がほぼ必須"),
    "精神科/心療内科":      (0.85, "心療内科含む向精神薬処方（精神科と同等）"),      # v4.2追加
    "小児科":               (0.62, "急性疾患が多く処方は比較的少ない"),
    "産婦人科":             (0.44, "健診・分娩が多く薬処方は少ない"),
    "産婦人科/婦人科":      (0.44, "婦人科疾患の処方（産婦人科と同等）"),            # v4.2追加
    "泌尿器科":             (0.70, "前立腺疾患・過活動膀胱等の継続薬"),
    "リハビリ科":           (0.40, "リハビリ中心で処方は少ない"),
    "歯科":                 (0.08, "歯科は処方箋薬局への処方がごく少ない（院内投与中心）"),  # v4.2追加
    "不明/その他":          (0.68, "全診療科平均値を使用"),
}

# ---------------------------------------------------------------------------
# v4.2: 診療科別 1日平均外来患者数テーブル（無床診療所）
# ---------------------------------------------------------------------------
# 出典: 厚生労働省「医療施設調査」2020年
#       無床一般診療所における診療科別1日平均外来患者数（全国平均）
#   + 業界実態・先行研究による医師数別補正係数
#
# 使用法:
#   推定外来数 = base + per_dr × max(0, doctors - 1)
#   ただし OSM のみ取得施設は規模が小さめの傾向があるため ×0.85 で控えめ補正
# ---------------------------------------------------------------------------
SPECIALTY_OUTPATIENT_TABLE: Dict[str, Dict] = {
    #                base  per_dr  cap   ← 1 医師時の基礎値 / 追加医師1人あたり増分 / 上限
    "一般内科":             {"base": 34, "per_dr": 10, "cap": 80},
    "循環器内科":           {"base": 36, "per_dr":  9, "cap": 90},
    "消化器内科":           {"base": 32, "per_dr":  8, "cap": 80},
    "消化器内科/消化器外科":{"base": 28, "per_dr":  8, "cap": 70},
    "糖尿病内科":           {"base": 26, "per_dr":  8, "cap": 70},
    "神経内科":             {"base": 28, "per_dr":  8, "cap": 70},
    "神経内科/脳神経内科":  {"base": 28, "per_dr":  8, "cap": 70},
    "呼吸器内科":           {"base": 30, "per_dr":  8, "cap": 75},
    "外科":                 {"base": 18, "per_dr":  6, "cap": 55},
    "整形外科":             {"base": 48, "per_dr": 12, "cap":120},  # 整形は特に多い
    "皮膚科":               {"base": 41, "per_dr":  8, "cap":100},
    "眼科":                 {"base": 54, "per_dr":  8, "cap":130},  # 眼科も特に多い
    "耳鼻咽喉科":           {"base": 50, "per_dr":  8, "cap":120},
    "精神科":               {"base": 23, "per_dr":  8, "cap": 60},
    "精神科/心療内科":      {"base": 23, "per_dr":  8, "cap": 60},
    "小児科":               {"base": 27, "per_dr":  9, "cap": 80},
    "産婦人科":             {"base": 14, "per_dr":  5, "cap": 40},
    "産婦人科/婦人科":      {"base": 14, "per_dr":  5, "cap": 40},
    "泌尿器科":             {"base": 28, "per_dr":  8, "cap": 70},
    "リハビリ科":           {"base": 15, "per_dr":  5, "cap": 40},
    "歯科":                 {"base": 22, "per_dr":  6, "cap": 60},
    "不明/その他":          {"base": 20, "per_dr":  8, "cap": 70},  # 控えめデフォルト
}

# ---------------------------------------------------------------------------
# v4.2: 人口密度帯別 年齢分布テーブル
# ---------------------------------------------------------------------------
# 出典: 総務省「国勢調査」2020年 + 都市部・農村部人口構成の補正推計
#       (都市部ほど15-44歳が多く、農村部ほど65歳以上が多い)
#
# 全国平均（旧固定値）: 65歳以上 28.3%（65-74: 14.5% + 75+: 13.8%）
# 超高密度 都心部:      65歳以上 20%  （若年就労者・単身者が多い）
# 超低密度 農村:        65歳以上 38%  （高齢化が進む過疎地域）
# ---------------------------------------------------------------------------
DENSITY_AGE_DISTRIBUTION: Dict[str, Dict[str, float]] = {
    # 密度帯              0-14歳   15-44歳  45-64歳  65-74歳  75歳以上
    "超高密度": {"0-14歳": 0.100, "15-44歳": 0.400, "45-64歳": 0.260, "65-74歳": 0.130, "75歳以上": 0.110},
    "高密度":   {"0-14歳": 0.112, "15-44歳": 0.368, "45-64歳": 0.260, "65-74歳": 0.140, "75歳以上": 0.120},
    "中密度":   {"0-14歳": 0.119, "15-44歳": 0.342, "45-64歳": 0.256, "65-74歳": 0.145, "75歳以上": 0.138},  # 全国平均値
    "低密度":   {"0-14歳": 0.115, "15-44歳": 0.295, "45-64歳": 0.255, "65-74歳": 0.175, "75歳以上": 0.160},
    "超低密度": {"0-14歳": 0.100, "15-44歳": 0.255, "45-64歳": 0.245, "65-74歳": 0.200, "75歳以上": 0.200},
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
# v4.3: 薬局タイプ定数 & スーパー商圏半径テーブル
# ---------------------------------------------------------------------------

# 薬局タイプ
PHARMACY_TYPE_NORMAL      = "通常の調剤薬局（単独）"
PHARMACY_TYPE_SUPERMARKET = "スーパー・ドラッグストア内薬局"
PHARMACY_TYPE_GATE        = "門前薬局（医療機関に隣接・専属）"
PHARMACY_TYPES = [PHARMACY_TYPE_NORMAL, PHARMACY_TYPE_SUPERMARKET, PHARMACY_TYPE_GATE]

# ---------------------------------------------------------------------------
# v4.4: スーパー内薬局 M2補正パラメータ
# ---------------------------------------------------------------------------
# ■ SM処方箋流入係数比率 (SM_INFLOW_COEFFICIENT_RATIO)
#   スーパーの来客が全員処方箋保有者ではない事実を反映する補正係数。
#   通常薬局の流入係数（1.05〜1.60）に乗算して使用。
#
#   根拠（v4.4 改訂: 0.65 → 0.40）:
#     処方箋を「慢性疾患（継続処方）」と「急性疾患（その場処方）」に分解すると:
#       慢性処方 (~65%): かかりつけ医近辺の薬局で受ける習慣 → SM捕捉率 ~10%
#                        寄与: 0.65 × 0.10 = 0.065
#       急性処方 (~35%): 受診直後に便利な場所で受ける → SM捕捉率 ~60%
#                        寄与: 0.35 × 0.60 = 0.210
#     合計捕捉率 ≈ 0.28、SM習慣利用補正を加味して 0.40 を採用。
#     （0.65では M2/M1 比率≈2.0 となり過大推計。0.40で比率≈1.2〜1.3 に収束）
SM_INFLOW_COEFFICIENT_RATIO: float = 0.40

# ■ SM市場シェア上限 (SM_MARKET_SHARE_CAP)
#   広域商圏を持つSM薬局のシェア上限を通常の80%から55%に引き下げ。
#   理由: 1,000m圏内に競合薬局が無くても「かかりつけ医近辺の薬局」に
#   処方箋が流れるため、SM薬局は残余需要の最大55%程度を捕捉できる。
SM_MARKET_SHARE_CAP: float = 0.55

# スーパーマーケット内薬局の商圏半径テーブル
# 出典: 国土交通省「都市における商業地等の整備及び機能」
#       経済産業省「商業統計」食品スーパーの一次商圏分析
#       調剤薬局業界実態調査（商圏内人口と処方箋枚数の相関）
#
# 食品スーパーの一次商圏（来客の約70〜80%がこの圏内に居住）を基準値に採用。
# 二次商圏（来客の85〜90%カバー）は約1.5〜2倍だが、処方箋獲得は一次商圏に集中するため
# 一次商圏の半径を採用。
SUPERMARKET_RADIUS_TABLE: Dict[str, int] = {
    #  密度帯        スーパー商圏半径
    "超高密度":  800,   # 12,000人/km²以上 / 都市型SM（徒歩商圏）
    "高密度":   1_000,  # 6,000〜12,000人/km²
    "中密度":   1_200,  # 2,000〜6,000人/km² ← 国立市はこの帯域
    "低密度":   1_800,  # 500〜2,000人/km²  / 郊外型SM（自転車・車商圏）
    "超低密度": 3_000,  # 500人/km²未満    / ロードサイド大型店
}

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
    is_manual: bool = False   # v2.4: 手動追加施設フラグ（OSM未収録）
    source: str = "osm"       # v2.6: "osm" | "mhlw" | "manual"


# ---------------------------------------------------------------------------
# v2.6 補助関数: 距離計算・診療科推定・エリアキーワード抽出
# ---------------------------------------------------------------------------

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の大圏距離をメートル単位で計算（Haversine公式）"""
    R = 6_371_000  # 地球半径 [m]
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi    = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# 施設名キーワード → 診療科マッピング（長いキーワードを先に評価）
_SPECIALTY_KW_MAP: List[Tuple[List[str], str]] = [
    (["整形外科"],                    "整形外科"),
    (["循環器"],                      "循環器内科"),
    (["消化器"],                      "消化器内科/消化器外科"),
    (["呼吸器"],                      "呼吸器内科"),
    (["精神科", "心療内科"],          "精神科/心療内科"),
    (["神経内科", "脳神経"],          "神経内科/脳神経内科"),
    (["外科"],                        "外科"),
    (["皮膚科"],                      "皮膚科"),
    (["泌尿器"],                      "泌尿器科"),
    (["眼科"],                        "眼科"),
    (["耳鼻"],                        "耳鼻咽喉科"),
    (["産婦人科", "婦人科"],          "産婦人科/婦人科"),
    (["小児科"],                      "小児科"),
    (["歯科"],                        "歯科"),
    (["内科"],                        "一般内科"),
]


def detect_specialty_from_name(name: str) -> str:
    """施設名から診療科（SPECIALTY_RX_RATESのキー）を推定する"""
    for keywords, specialty in _SPECIALTY_KW_MAP:
        if any(kw in name for kw in keywords):
            return specialty
    return "不明/その他"


def extract_area_keyword(address: str) -> str:
    """
    住所から最小行政区画をMHLW検索キーワードとして抽出する。

    例:
      "東京都板橋区成増1丁目12-3"       → "板橋区"
      "神奈川県川崎市中原区新丸子東3丁目" → "中原区"
      "大阪府大阪市北区梅田1丁目"        → "北区"
      "埼玉県さいたま市浦和区常盤"       → "浦和区"
      "福岡県久留米市通外町10-1"         → "久留米市"
    """
    # 政令市の区: 「市○○区」→ 区名
    m = re.search(r'市([^\d\s・]{1,6}?区)', address)
    if m:
        return m.group(1)
    # 東京23区: 「都○○区」→ 区名
    m = re.search(r'都([^\d\s・]{1,6}?区)', address)
    if m:
        return m.group(1)
    # その他: 都道府県直後の市区町村
    m = re.search(r'[都道府県]([^\d\s・]{2,8}?[市区町村])', address)
    if m:
        return m.group(1)
    return address[:10]


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
    scenario: str = "area_dual"     # "area_dual" | "combined" | "gate_only" | "all"
    pharmacy_type: str = PHARMACY_TYPE_NORMAL  # v4.3: 薬局タイプ
    gate_specialty: str = "一般内科"
    gate_daily_outpatients: int = 50
    gate_has_inhouse: bool = False   # 誘致クリニックが院内薬局を持つか
    fetch_nearby_rx: bool = False    # 近隣薬局のMHLWデータを取得するか
    fetch_mhlw_supplement: bool = False  # v2.6: MHLWから医療機関を自動補填するか

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
    method1_gate: Optional[PredictionResult]   # シナリオA/C: 方法①（門前クリニック込み）
    method1_area: Optional[PredictionResult]   # シナリオB/C: 方法①（既存近隣施設のみ）
    method2: Optional[PredictionResult]        # シナリオB/C: 方法②（商圏人口動態）
    search_log: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 0-b. v4.1: 校正データクラス
# ---------------------------------------------------------------------------

def _density_band(density: int) -> str:
    """人口密度を校正エンジン用バンドラベルに変換（長形式: 閾値付き）
    CalibrationEngine / CalibrationStats で使用。テーブルルックアップには使わないこと。
    """
    if density >= 10_000: return "超高密度(≥10k)"
    if density >= 5_000:  return "高密度(5k-10k)"
    if density >= 2_000:  return "中密度(2k-5k)"
    if density >= 500:    return "低密度(500-2k)"
    return "超低密度(<500)"


def _density_band_label(density: int) -> str:
    """人口密度をテーブルルックアップ用ラベルに変換（短形式）

    v4.4新規: _density_band() の長形式キー「高密度(5k-10k)」と
    SUPERMARKET_RADIUS_TABLE・DENSITY_AGE_DISTRIBUTION の短形式キー「高密度」の
    不一致バグを修正するために導入。

    - SUPERMARKET_RADIUS_TABLE のキー照合に使用
    - DENSITY_AGE_DISTRIBUTION のキー照合に使用
    """
    if density >= 10_000: return "超高密度"
    if density >= 5_000:  return "高密度"
    if density >= 2_000:  return "中密度"
    if density >= 500:    return "低密度"
    return "超低密度"


@dataclass
class CalibrationPoint:
    """校正サンプル1件: 実績処方箋枚数と住所のみ予測値の比較"""
    name: str                              # 薬局名
    address: str                           # 住所
    actual_rx: int                         # MHLWから取得した実績値
    m1_rx: Optional[int] = None            # 方法①（近隣医療機関）予測値
    m2_rx: Optional[int] = None            # 方法②（商圏人口）予測値
    area_density: int = 0                  # 人口密度 (人/km²)
    n_medical: int = 0                     # 検出した医療機関数
    n_pharmacies: int = 0                  # 検出した競合薬局数
    is_gate: bool = False                  # 門前薬局フラグ
    error_log: List[str] = field(default_factory=list)  # エラー・ログ

    # --- 導出プロパティ ---
    @property
    def density_band(self) -> str:
        return _density_band(self.area_density)

    @property
    def error_m1(self) -> Optional[float]:
        """方法①の相対誤差 (predicted/actual - 1)"""
        if self.m1_rx is not None and self.actual_rx > 0:
            return self.m1_rx / self.actual_rx - 1.0
        return None

    @property
    def error_m2(self) -> Optional[float]:
        if self.m2_rx is not None and self.actual_rx > 0:
            return self.m2_rx / self.actual_rx - 1.0
        return None

    @property
    def ape_m1(self) -> Optional[float]:
        """方法①の絶対誤差率 (APE)"""
        e = self.error_m1
        return abs(e) if e is not None else None

    @property
    def ape_m2(self) -> Optional[float]:
        e = self.error_m2
        return abs(e) if e is not None else None


@dataclass
class CalibrationStats:
    """全校正サンプルの統計サマリー"""
    n: int                                   # 有効サンプル数
    mape_m1: float                           # 方法①のMAPE (%)
    mape_m2: float                           # 方法②のMAPE (%)
    mape_optimal: float                      # 最適ブレンドのMAPE (%)
    optimal_m1_weight: float                 # 最適M1重み [0.0–1.0]
    bias_m1: float                           # 方法①の幾何平均バイアス (log scale)
    bias_m2: float                           # 方法②の幾何平均バイアス
    # 密度帯別補正係数 {band: (alpha, n_samples)}
    # alpha = geometric_mean(actual / predicted) → predicted × alpha = 補正後推計
    alpha_m1: Dict[str, Tuple[float, int]] = field(default_factory=dict)
    alpha_m2: Dict[str, Tuple[float, int]] = field(default_factory=dict)
    calibrated_at: str = ""                  # 校正実行時刻


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
    pharmacy_type: str = PHARMACY_TYPE_NORMAL,
) -> Tuple[int, str]:
    """
    v4.3: 薬局タイプ別商圏半径を返す。

    ■ タイプ別ロジック:
      GATE（門前）      → 300m固定（医療機関依存型、居住商圏より来院フローが重要）
      NORMAL（通常）    → 密度帯別の歩行距離商圏
      SUPERMARKET（SM） → スーパー商圏テーブルを適用
                          旧バージョンでの問題:「近隣に医療機関があるとGATE判定→300m固定」
                          を廃止。SM業態では医療機関隣接でも広域商圏を使う。

    ■ 商圏設計の根拠（SM）:
      食品スーパー一次商圏（来客70〜80%カバー）= 密度帯ごとに800〜3000m
      出典: 国土交通省「商業地等整備」/ 経産省「商業統計」
    """
    # ── 門前型: タイプが GATE の場合のみ300m固定 ──────────────────────────
    # v4.3改善: is_gate フラグだけでなく、ユーザー指定の pharmacy_type を優先。
    # 旧バージョンは is_gate=True だと SM でも強制300mになっていた。
    if pharmacy_type == PHARMACY_TYPE_GATE:
        return 300, f"門前薬局（{gate_reason or '医療機関に隣接'}）→ 医療機関依存型のため300m固定"

    # ── スーパー・ドラッグストア内薬局 ────────────────────────────────────
    if pharmacy_type == PHARMACY_TYPE_SUPERMARKET:
        # v4.4バグ修正: _density_band()は「高密度(5k-10k)」の長形式を返すが
        # SUPERMARKET_RADIUS_TABLE は「高密度」の短形式キーを使用するため不一致が発生していた。
        # _density_band_label() を使って短形式ラベルで照合する。
        band_short = _density_band_label(density)
        band_long  = _density_band(density)  # 表示用
        r = SUPERMARKET_RADIUS_TABLE.get(band_short, 1_200)
        return r, (
            f"スーパー・ドラッグストア内薬局（密度帯: {band_long} / {density:,}人/km²）"
            f" → 食品スーパー一次商圏 {r}m"
            f"（国土交通省・経産省商業統計ベース）"
        )

    # ── 通常の調剤薬局: 密度帯別歩行商圏 ────────────────────────────────
    # ※ is_gate=True でも pharmacy_type=NORMAL なら通常商圏を使う
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
        self, lat: float, lon: float, radius: int = 800
    ) -> Tuple[List[NearbyFacility], List[NearbyFacility], str]:
        # v3.2: 日本語タグ・追加 healthcare タグを拡充、薬局タグを拡充
        # (shop=pharmacy / healthcare=pharmacy も追加し、日本OSMの表記ゆれに対応)
        query = f"""
[out:json][timeout:40];
(
  node["amenity"~"^(hospital|clinic|doctors|医院|診療所|クリニック)$"](around:{radius},{lat},{lon});
  way["amenity"~"^(hospital|clinic|doctors|医院|診療所|クリニック)$"](around:{radius},{lat},{lon});
  node["healthcare"~"^(hospital|clinic|doctor|centre|physiotherapist|rehabilitation|dialysis)$"](around:{radius},{lat},{lon});
  way["healthcare"~"^(hospital|clinic|doctor|centre|physiotherapist|rehabilitation|dialysis)$"](around:{radius},{lat},{lon});
  node["medical"](around:{radius},{lat},{lon});
  node["amenity"="pharmacy"](around:{radius},{lat},{lon});
  way["amenity"="pharmacy"](around:{radius},{lat},{lon});
  node["shop"="pharmacy"](around:{radius},{lat},{lon});
  way["shop"="pharmacy"](around:{radius},{lat},{lon});
  node["healthcare"="pharmacy"](around:{radius},{lat},{lon});
  way["healthcare"="pharmacy"](around:{radius},{lat},{lon});
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
            # v3.2: amenity=pharmacy / shop=pharmacy / healthcare=pharmacy 全て薬局として扱う
            is_pharmacy_tag = (
                tags.get("amenity") == "pharmacy"
                or tags.get("shop") == "pharmacy"
                or tags.get("healthcare") == "pharmacy"
            )
            if is_pharmacy_tag:
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
            specialty = OSM_SPECIALTY_MAP.get(sp_raw.lower(), "") if sp_raw else ""
            # v4.2: OSMタグに speciality がない場合、施設名（日本語）から診療科を推定
            if not specialty:
                specialty = detect_specialty_from_name(name)
            has_inhouse = tags.get("pharmacy", "") in ["yes", "dispensing"]
            beds = int(tags.get("beds", 0) or 0)
            # v4.2: 診療科テーブルを使った外来患者数推定（specialty を渡す）
            daily_op = self._estimate_outpatients(ftype, beds, tags, specialty)
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
    def _estimate_outpatients(
        ftype: str, beds: int, tags: Dict, specialty: str = "不明/その他"
    ) -> int:
        """
        v4.2: 診療科別外来患者数テーブル (SPECIALTY_OUTPATIENT_TABLE) を使用。
        旧: 医師数だけで一律 20/40/70 → 新: 診療科ごとの統計的基礎値

        出典: 厚生労働省「医療施設調査」2020年 無床診療所 診療科別1日平均外来患者数
        計算式: base + per_dr × max(0, doctors - 1)
        OSM補正: ×0.85（OSMのみ収録施設は全国平均より規模小の傾向）
        """
        if ftype == "hospital":
            if beds >= 300: return 1_000
            if beds >= 100: return 400
            return 150
        doctors = int(tags.get("staff:count", 0) or 0)
        sp_key = specialty if specialty in SPECIALTY_OUTPATIENT_TABLE else "不明/その他"
        sp_data = SPECIALTY_OUTPATIENT_TABLE[sp_key]
        base    = sp_data["base"]
        per_dr  = sp_data["per_dr"]
        cap     = sp_data["cap"]
        if doctors >= 2:
            estimated = base + per_dr * (doctors - 1)
        else:
            estimated = base  # 医師数不明 or 1名 → 基礎値のみ
        # OSM収録のみ施設は小規模傾向のため控えめ補正（×0.85）
        return max(5, min(cap, int(estimated * 0.85)))


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
                        address = re.sub(r"\s+", " ", cleaned).strip()[:100]
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

    # ── v2.4 新規: MHLW医療機関検索 ────────────────────────────────────────

    def search_clinic_by_keyword(
        self, keyword: str, pref_code: str = ""
    ) -> List[PharmacyCandidate]:
        """
        MHLW医療機関データベースでクリニック名・住所キーワードを検索 (sjk=1: 医療機関)

        OSMで検出されなかったクリニックを名称等で検索するための補助機能。
        """
        cands, _, _ = self._search_candidates(keyword, pref_code, max_pages=2, sjk="1")
        return cands

    def search_medical_by_area(
        self, address: str, pref_code: str = "", max_pages: int = 5
    ) -> Tuple[List[PharmacyCandidate], str]:
        """
        住所からエリアキーワードを自動抽出してMHLW医療機関を一括検索 (v2.6新機能)

        OSMで未収録の医療機関を自動補填するためのエリアベース検索。
        - 市区町村名をキーワードとして全施設を検索（sjk=1: 医療機関）
        - 住所にキーワードが含まれる候補のみを返す（名称一致の誤ヒットを排除）
        - 最大 max_pages × 20 件を取得（デフォルト: 100件）

        Returns:
            (candidates, log_message)
        """
        keyword = extract_area_keyword(address)
        cands, total, msg = self._search_candidates(keyword, pref_code, max_pages, sjk="1")
        # 住所にキーワードを含む候補のみに絞り込む（名前一致の誤ヒットを排除）
        filtered = [c for c in cands if c.address and keyword in c.address]
        return filtered, (
            f"MHLWエリア検索: キーワード='{keyword}' → "
            f"全{total}件中 住所一致{len(filtered)}件取得"
        )

    def get_clinic_daily_outpatients(
        self, candidate: PharmacyCandidate
    ) -> Tuple[Optional[int], str]:
        """
        MHLW医療機関詳細ページから外来患者数（1日平均）を取得する。

        MHLW の報告形式は施設によって異なるため、複数のパターンを試みる。
        - 「外来患者数」「1日平均外来」などのフィールドを探索
        - 年間値が得られた場合は稼働日数(305)で割って日次換算
        """
        if not self._initialized:
            self.initialize_session()
        if candidate.pref_cd and candidate.kikan_cd:
            url = (f"{self.BASE}/juminkanja/S2430/initialize"
                   f"?prefCd={candidate.pref_cd}&kikanCd={candidate.kikan_cd}&kikanKbn=1")
        else:
            url = candidate.href
        try:
            r = self.session.get(url, timeout=12)
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            soup = BeautifulSoup(r.text, "html.parser")
            fields: Dict[str, str] = {}
            for row in soup.find_all("tr"):
                cells = row.find_all(["th", "td"])
                if len(cells) >= 2:
                    k = cells[0].get_text(strip=True)
                    v = cells[1].get_text(strip=True)
                    if k:
                        fields[k] = v
            # 1日平均外来患者数（直接記載）
            for k, v in fields.items():
                if ("1日平均" in k or "一日平均" in k) and "外来" in k:
                    nums = re.findall(r"[\d,]+", v)
                    if nums:
                        try:
                            n = int(nums[0].replace(",", ""))
                            if 1 <= n <= 3000:
                                return n, f"MHLW取得: {k}"
                        except (ValueError, OverflowError):
                            pass
            # 年間外来患者数（305日で割って日次換算）
            for k, v in fields.items():
                if "外来" in k and ("患者" in k or "数" in k) and "年間" in k:
                    nums = re.findall(r"[\d,]+", v)
                    if nums:
                        try:
                            n = int(nums[0].replace(",", ""))
                            if n > 300:
                                daily = int(n / NATIONAL_STATS["working_days"])
                                return daily, f"MHLW年間値から換算({n:,}→{daily}/日)"
                        except (ValueError, OverflowError):
                            pass
            return None, "外来患者数の記載なし"
        except Exception as e:
            return None, f"取得エラー: {e}"


# ---------------------------------------------------------------------------
# 4-b. v4.1: 校正エンジン
# ---------------------------------------------------------------------------

class CalibrationEngine:
    """
    MHLWの実績処方箋データを使ってモデルを校正するエンジン（v4.1）

    ワークフロー:
      1. search_calibration_set()  … MHLW検索 → 実績Rxあり薬局を収集
      2. run_batch()               … 各薬局を住所のみで予測 → CalibrationPoint リスト
      3. calc_stats()              … MAPE・補正係数・最適ブレンドを計算
      4. apply_correction()        … 校正パラメータを実際の予測に適用

    校正パラメータ:
      ■ 密度帯別補正係数 α(density)
           α = geometric_mean(actual / predicted) per density band
           → systematic over/under-estimate を乗算で補正
      ■ 最適ブレンド重み w*
           MAPE(w) = mean(|w×M1 + (1-w)×M2 - actual| / actual) を最小化
    """

    def __init__(self):
        self._scraper = MHLWScraper()
        self._geocoder = GeocoderService()

    # ── Step 1: 校正用薬局セットの収集 ─────────────────────────────────────

    def search_calibration_set(
        self,
        pref_code: str,
        keyword: str = "薬局",
        max_pharmacies: int = 30,
        min_rx: int = 1_000,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> List[Tuple["PharmacyCandidate", int]]:
        """
        MHLWで都道府県・キーワードを検索し、処方箋枚数が取得できた薬局を返す。

        Returns: List of (PharmacyCandidate, annual_rx)
        """
        if not self._scraper._initialized:
            self._scraper.initialize_session()

        # 薬局候補を検索（最大3ページ = 60件）
        candidates, _, _ = self._scraper.search_pharmacy_candidates(
            keyword, pref_code, max_pages=3
        )
        if progress_cb:
            progress_cb(5, f"MHLW検索: {len(candidates)}件の薬局候補を取得")

        result: List[Tuple["PharmacyCandidate", int]] = []
        for i, cand in enumerate(candidates):
            if len(result) >= max_pharmacies:
                break
            if not cand.address:
                continue
            try:
                detail, _ = self._scraper.get_pharmacy_detail(cand)
                rx = detail.get("prescriptions_annual") if detail else None
                if rx and rx >= min_rx:
                    result.append((cand, rx))
            except Exception:
                pass
            time.sleep(0.5)
            if progress_cb:
                pct = int(5 + 40 * (i + 1) / max(len(candidates), 1))
                progress_cb(pct, f"MHLW詳細取得中 ({i+1}/{len(candidates)}) … 有効: {len(result)}件")

        if progress_cb:
            progress_cb(45, f"校正セット収集完了: {len(result)}件")
        return result

    # ── Step 2: 住所のみで予測 ──────────────────────────────────────────────

    def predict_for_candidate(
        self,
        cand: "PharmacyCandidate",
        actual_rx: int,
    ) -> "CalibrationPoint":
        """
        1薬局について住所のみの情報で方法①・②予測を実行し CalibrationPoint を返す。
        """
        pt = CalibrationPoint(name=cand.name, address=cand.address, actual_rx=actual_rx)
        log: List[str] = []

        try:
            # 1. ジオコーディング
            lat, lon, geo_msg, _ = self._geocoder.geocode(cand.address)
            log.append(f"[Geo] {geo_msg}")
            if not (lat and lon):
                log.append("⚠ ジオコーディング失敗 → スキップ")
                pt.error_log = log
                return pt

            # 2. 人口密度・商圏半径
            density, _ = get_population_density(cand.address)
            pt.area_density = density
            is_gate, gate_reason = detect_gate_pharmacy(cand.name, [])
            pt.is_gate = is_gate
            radius, _ = calc_commercial_radius(density, is_gate, gate_reason)
            search_r = max(int(radius * 1.5), 600)

            # 3. OSM検索
            ov = OverpassSearcher()
            medical, pharmacies, ov_msg = ov.search_nearby(lat, lon, search_r)
            log.append(f"[OSM] {ov_msg}")
            pt.n_medical = len(medical)
            pt.n_pharmacies = len(pharmacies)

            # 4. MHLW医療機関補填（軽量版: キーワード検索のみ）
            pref_code = ""
            for pn, pc in PREFECTURE_CODES.items():
                if pn in cand.address:
                    pref_code = pc
                    break
            new_facs, sup_log = fetch_mhlw_medical_supplement(
                pharmacy_lat=lat, pharmacy_lon=lon,
                pharmacy_address=cand.address, pref_code=pref_code,
                existing_osm=medical, search_radius_m=search_r,
            )
            log.extend(sup_log[:3])
            if new_facs:
                medical = medical + new_facs
                pt.n_medical += len(new_facs)

            # 5. 医療機関密集補正
            medical = apply_clinic_congestion_factor(medical)

            # 6. 方法①
            m1_result = Method1Predictor().predict(lat, lon, medical, pharmacies)
            pt.m1_rx = m1_result.annual_rx

            # 7. 方法②
            m2_result = Method2Predictor().predict(
                lat, lon, pharmacies, density, radius,
                nearby_medical=medical,
            )
            pt.m2_rx = m2_result.annual_rx

            log.append(f"[結果] 実績={actual_rx:,} M1={pt.m1_rx:,} M2={pt.m2_rx:,}")

        except Exception as e:
            log.append(f"⚠ 予測エラー: {e}")

        pt.error_log = log
        return pt

    # ── Step 3: バッチ実行 ──────────────────────────────────────────────────

    def run_batch(
        self,
        calibration_set: List[Tuple["PharmacyCandidate", int]],
        progress_cb: Optional[Callable[[int, str], None]] = None,
        delay: float = 0.8,
    ) -> List["CalibrationPoint"]:
        """校正セット全件の予測を実行して CalibrationPoint リストを返す"""
        points: List[CalibrationPoint] = []
        n = len(calibration_set)
        for i, (cand, actual_rx) in enumerate(calibration_set):
            if progress_cb:
                pct = int(45 + 50 * i / max(n, 1))
                progress_cb(pct, f"予測中 ({i+1}/{n}): {cand.name[:20]}…")
            pt = self.predict_for_candidate(cand, actual_rx)
            points.append(pt)
            time.sleep(delay)

        if progress_cb:
            valid = sum(1 for p in points if p.m1_rx is not None)
            progress_cb(95, f"バッチ完了: {valid}/{n}件 予測成功")
        return points

    # ── Step 4: 統計計算 ────────────────────────────────────────────────────

    @staticmethod
    def calc_stats(points: List["CalibrationPoint"]) -> Optional["CalibrationStats"]:
        """
        CalibrationPoint リストから CalibrationStats を計算する。

        補正係数の計算方法:
          α(band) = exp( mean( log(actual / predicted) ) ) for samples in band
          = geometric mean of (actual / predicted) ratios
          → predicted × α = バイアス除去後の推計値

        最適ブレンド重み w* の探索:
          w ∈ {0.0, 0.1, ..., 1.0} で MAPE(w) を計算し最小化
          MAPE(w) = mean(|w×M1 + (1-w)×M2 - actual| / actual)
        """
        # 両方の予測値がある有効サンプルのみ使用
        valid = [p for p in points if p.m1_rx is not None and p.m2_rx is not None and p.actual_rx > 0]
        if len(valid) < 3:
            return None  # サンプル不足

        import math as _math

        # ── MAPE (%) ──
        def mape(preds: List[float], actuals: List[float]) -> float:
            return 100.0 * sum(abs(p - a) / a for p, a in zip(preds, actuals)) / len(actuals)

        actuals = [float(p.actual_rx) for p in valid]
        m1s = [float(p.m1_rx) for p in valid]
        m2s = [float(p.m2_rx) for p in valid]

        mape_m1 = mape(m1s, actuals)
        mape_m2 = mape(m2s, actuals)

        # ── バイアス (log scale: log(predicted/actual) の平均) ──
        bias_m1 = sum(_math.log(max(m, 1) / a) for m, a in zip(m1s, actuals)) / len(valid)
        bias_m2 = sum(_math.log(max(m, 1) / a) for m, a in zip(m2s, actuals)) / len(valid)

        # ── 最適ブレンド重み w* ──
        best_w, best_mape = 0.5, float("inf")
        for w_int in range(0, 11):
            w = w_int / 10.0
            blended = [w * m1 + (1 - w) * m2 for m1, m2 in zip(m1s, m2s)]
            mp = mape(blended, actuals)
            if mp < best_mape:
                best_mape, best_w = mp, w

        # ── 密度帯別補正係数 ──
        def alpha_for_band(preds: List[float]) -> Tuple[float, int]:
            """geometric mean of (actual/predicted) = exp(mean(log(actual/pred)))"""
            ratios = [_math.log(a / max(p, 1)) for p, a in zip(preds, actuals)]
            return _math.exp(sum(ratios) / len(ratios)), len(ratios)

        bands = list({p.density_band for p in valid})
        alpha_m1: Dict[str, Tuple[float, int]] = {}
        alpha_m2: Dict[str, Tuple[float, int]] = {}
        for band in bands:
            band_pts = [p for p in valid if p.density_band == band]
            if len(band_pts) < 2:
                continue
            band_actuals = [float(p.actual_rx) for p in band_pts]
            band_m1 = [float(p.m1_rx) for p in band_pts]
            band_m2 = [float(p.m2_rx) for p in band_pts]

            def alpha_band(preds: List[float], acts: List[float]) -> Tuple[float, int]:
                ratios = [_math.log(a / max(p, 1)) for p, a in zip(preds, acts)]
                return _math.exp(sum(ratios) / len(ratios)), len(ratios)

            alpha_m1[band] = alpha_band(band_m1, band_actuals)
            alpha_m2[band] = alpha_band(band_m2, band_actuals)

        return CalibrationStats(
            n=len(valid),
            mape_m1=round(mape_m1, 1),
            mape_m2=round(mape_m2, 1),
            mape_optimal=round(best_mape, 1),
            optimal_m1_weight=best_w,
            bias_m1=round(bias_m1, 3),
            bias_m2=round(bias_m2, 3),
            alpha_m1=alpha_m1,
            alpha_m2=alpha_m2,
            calibrated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )

    # ── Step 5: 校正パラメータの適用 ────────────────────────────────────────

    @staticmethod
    def apply_correction(
        m1_rx: int,
        m2_rx: int,
        density: int,
        stats: "CalibrationStats",
    ) -> Tuple[int, str]:
        """
        校正パラメータを適用して最終推計値を計算する。

        計算式:
          M1_cal = M1 × α1(density_band)
          M2_cal = M2 × α2(density_band)
          final  = w* × M1_cal + (1-w*) × M2_cal

        Returns: (calibrated_annual_rx, explanation_str)
        """
        band = _density_band(density)
        alpha1 = stats.alpha_m1.get(band, (1.0, 0))[0]
        alpha2 = stats.alpha_m2.get(band, (1.0, 0))[0]
        w = stats.optimal_m1_weight

        m1_cal = int(m1_rx * alpha1)
        m2_cal = int(m2_rx * alpha2)
        final = int(w * m1_cal + (1 - w) * m2_cal)

        n1 = stats.alpha_m1.get(band, (1.0, 0))[1]
        n2 = stats.alpha_m2.get(band, (1.0, 0))[1]
        note = (
            f"密度帯={band} | "
            f"α1={alpha1:.2f}(n={n1}) α2={alpha2:.2f}(n={n2}) | "
            f"w*={w:.1f} (M1:{m1_cal:,} + M2:{m2_cal:,})"
        )
        return final, note

    @staticmethod
    def points_to_csv(points: List["CalibrationPoint"]) -> str:
        """校正データをCSV文字列として出力"""
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "薬局名", "住所", "実績Rx", "M1予測", "M2予測",
            "M1誤差率", "M2誤差率", "人口密度", "密度帯",
            "医療機関数", "競合薬局数", "門前",
        ])
        for p in points:
            writer.writerow([
                p.name, p.address, p.actual_rx,
                p.m1_rx or "", p.m2_rx or "",
                f"{p.error_m1*100:.1f}%" if p.error_m1 is not None else "",
                f"{p.error_m2*100:.1f}%" if p.error_m2 is not None else "",
                p.area_density, p.density_band,
                p.n_medical, p.n_pharmacies,
                "はい" if p.is_gate else "いいえ",
            ])
        return buf.getvalue()


# ---------------------------------------------------------------------------
# 4-c. v4.4: ローカルMHLW校正エンジン
# ---------------------------------------------------------------------------

class LocalCalibrationEngine:
    """
    同一エリア（市区町村）の既存MHLW薬局を使ってM1・M2をローカル校正するエンジン（v4.4）

    CalibrationEngine（都道府県レベル）との違い:
      ・CalibrationEngine: 都道府県全体の薬局サンプルで大域的な補正係数を算出
      ・LocalCalibrationEngine: 予測対象と同じ市区町村の薬局で地域密着の補正係数を算出

    メリット:
      ・エリア固有の医療機関密度・競合状況・人口特性を反映した補正が可能
      ・「このエリアでは M1 が X% 過大推計、M2 が Y% 過少推計」という地域特性を把握

    ワークフロー:
      1. search_local_set()   … 同一エリアの薬局を MHLW から収集
      2. run_local_batch()    … 各薬局を住所のみで M1・M2 予測
      3. calc_local_stats()   … ローカル補正係数 α1, α2 + 最適ブレンド w* を計算
      4. apply_local_correction() … 新規薬局の予測に補正を適用

    注意:
      ・MHLW からの処方箋データが少ない地域は精度が低下する（最低3件必要）
      ・SM業態と通常業態が混在するサンプルには固有の誤差が生じる場合がある
    """

    def __init__(self):
        self._scraper  = MHLWScraper()
        self._geocoder = GeocoderService()

    # ── Step 1: 同一エリアの校正用薬局収集 ──────────────────────────────

    def search_local_set(
        self,
        address: str,
        pref_code: str = "",
        max_pharmacies: int = 10,
        min_rx: int = 1_000,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> Tuple[List[Tuple["PharmacyCandidate", int]], str]:
        """
        対象住所と同一エリア（市区町村）の既存MHLW薬局を収集して返す。

        Returns:
            (calibration_set, area_keyword)
        """
        if not self._scraper._initialized:
            self._scraper.initialize_session()

        area_kw = extract_area_keyword(address)
        if progress_cb:
            progress_cb(5, f"エリアキーワード: '{area_kw}' でMHLW薬局検索中…")

        candidates, total, _ = self._scraper.search_pharmacy_candidates(
            area_kw, pref_code, max_pages=3
        )
        # 同一エリアの薬局のみに絞り込む（住所にエリアキーワードを含むもの）
        local_cands = [c for c in candidates if c.address and area_kw in c.address]

        if progress_cb:
            progress_cb(15, f"エリア内薬局候補: {len(local_cands)}件 / 全{total}件")

        result: List[Tuple["PharmacyCandidate", int]] = []
        for i, cand in enumerate(local_cands):
            if len(result) >= max_pharmacies:
                break
            if not cand.address:
                continue
            try:
                detail, _ = self._scraper.get_pharmacy_detail(cand)
                rx = detail.get("prescriptions_annual") if detail else None
                if rx and rx >= min_rx:
                    result.append((cand, rx))
            except Exception:
                pass
            time.sleep(0.5)
            if progress_cb:
                pct = int(15 + 40 * (i + 1) / max(len(local_cands), 1))
                progress_cb(pct, f"処方箋データ取得中 ({i+1}/{len(local_cands)}) … 有効: {len(result)}件")

        if progress_cb:
            progress_cb(55, f"ローカル校正セット収集完了: {len(result)}件（エリア: {area_kw}）")
        return result, area_kw

    # ── Step 2: バッチ予測（CalibrationEngine と共通ロジック） ─────────

    def run_local_batch(
        self,
        calibration_set: List[Tuple["PharmacyCandidate", int]],
        pharmacy_type: str = PHARMACY_TYPE_NORMAL,
        progress_cb: Optional[Callable[[int, str], None]] = None,
    ) -> List["CalibrationPoint"]:
        """
        ローカル校正セット全件を住所のみで予測し CalibrationPoint リストを返す。
        pharmacy_type を考慮した商圏半径・M2補正を適用する。
        """
        points: List[CalibrationPoint] = []
        n = len(calibration_set)
        for i, (cand, actual_rx) in enumerate(calibration_set):
            if progress_cb:
                pct = int(55 + 40 * i / max(n, 1))
                progress_cb(pct, f"予測中 ({i+1}/{n}): {cand.name[:20]}…")
            pt = self._predict_one(cand, actual_rx, pharmacy_type)
            points.append(pt)
            time.sleep(0.8)
        if progress_cb:
            valid = sum(1 for p in points if p.m1_rx is not None)
            progress_cb(95, f"バッチ完了: {valid}/{n}件 予測成功")
        return points

    def _predict_one(
        self,
        cand: "PharmacyCandidate",
        actual_rx: int,
        pharmacy_type: str = PHARMACY_TYPE_NORMAL,
    ) -> "CalibrationPoint":
        """1薬局を住所のみで予測して CalibrationPoint を返す"""
        pt = CalibrationPoint(name=cand.name, address=cand.address, actual_rx=actual_rx)
        log: List[str] = []
        try:
            lat, lon, geo_msg, _ = self._geocoder.geocode(cand.address)
            log.append(f"[Geo] {geo_msg}")
            if not (lat and lon):
                log.append("⚠ ジオコーディング失敗 → スキップ")
                pt.error_log = log
                return pt

            density, _ = get_population_density(cand.address)
            pt.area_density = density
            is_gate, gate_reason = detect_gate_pharmacy(cand.name, [])
            pt.is_gate = is_gate
            radius, _ = calc_commercial_radius(density, is_gate, gate_reason)
            search_r = max(int(radius * 1.5), 600)

            ov = OverpassSearcher()
            medical, pharmacies, ov_msg = ov.search_nearby(lat, lon, search_r)
            log.append(f"[OSM] {ov_msg}")
            pt.n_medical = len(medical)
            pt.n_pharmacies = len(pharmacies)

            # 密集補正
            medical = apply_clinic_congestion_factor(medical)

            # 方法①
            m1 = Method1Predictor().predict(lat, lon, medical, pharmacies)
            pt.m1_rx = m1.annual_rx

            # 方法② (pharmacy_type を渡して SM業態補正を適用)
            m2 = Method2Predictor().predict(
                lat, lon, pharmacies, density, radius,
                nearby_medical=medical,
                pharmacy_type=pharmacy_type,
            )
            pt.m2_rx = m2.annual_rx

            log.append(f"[結果] 実績={actual_rx:,} M1={pt.m1_rx:,} M2={pt.m2_rx:,}")
        except Exception as e:
            log.append(f"⚠ 予測エラー: {e}")
        pt.error_log = log
        return pt

    # ── Step 3: ローカル統計 ─────────────────────────────────────────────

    @staticmethod
    def calc_local_stats(
        points: List["CalibrationPoint"],
    ) -> Optional["CalibrationStats"]:
        """
        CalibrationPoint リストからローカル補正係数 α1, α2 と最適ブレンド w* を計算。
        CalibrationEngine.calc_stats() と同一ロジック。
        """
        return CalibrationEngine.calc_stats(points)

    # ── Step 4: ローカル校正の適用 ──────────────────────────────────────

    @staticmethod
    def apply_local_correction(
        m1_rx: int,
        m2_rx: int,
        density: int,
        stats: "CalibrationStats",
    ) -> Tuple[int, str]:
        """
        ローカル補正係数を新規薬局の予測に適用する。
        CalibrationEngine.apply_correction() と同一ロジック。
        """
        return CalibrationEngine.apply_correction(m1_rx, m2_rx, density, stats)


# ---------------------------------------------------------------------------
# 5-pre. 医療機関密集補正ユーティリティ + スマートブレンド（v4.2）
# ---------------------------------------------------------------------------

def calc_smart_blend_weight(
    area_density: int,
    n_medical: int,
    n_mhlw_confirmed: int,
) -> Tuple[float, str]:
    """
    v4.2: データ品質シグナルに基づく動的M1/M2ブレンド重み（校正なし時のデフォルト）

    ■ 設計方針:
      M1（医療機関アプローチ）が信頼できるシグナル:
        ・近隣医療機関が多い（データ量↑）
        ・MHLW確認済み施設が多い（M1精度↑）
        ・低密度地域（人口データより施設データが安定）
      M2（商圏人口アプローチ）が信頼できるシグナル:
        ・高密度都市部（人口データ安定）
        ・医療機関が少ない（M1データが乏しい）

    Returns: (w_m1, reason_str)
      w_m1: M1の重み [0.3 〜 0.7]
      最終推奨 = w_m1 × M1 + (1 - w_m1) × M2
    """
    w = 0.50  # ベース重み（等ウェイト）
    reasons = ["ベース50/50"]

    # 人口密度シグナル
    if area_density > 10_000:
        w -= 0.12
        reasons.append("超高密度→M2↑(−12%)")
    elif area_density > 5_000:
        w -= 0.07
        reasons.append("高密度→M2↑(−7%)")
    elif area_density < 500:
        w += 0.08
        reasons.append("超低密度→M1↑(+8%)")

    # 医療機関数シグナル
    if n_medical >= 10:
        w += 0.12
        reasons.append("医療機関10件↑→M1↑(+12%)")
    elif n_medical >= 5:
        w += 0.07
        reasons.append("医療機関5件↑→M1↑(+7%)")
    elif n_medical < 3:
        w -= 0.08
        reasons.append("医療機関少→M2↑(−8%)")

    # MHLW確認済み施設シグナル（外来患者数が実績値で信頼性高い）
    if n_mhlw_confirmed >= 3:
        w += 0.08
        reasons.append(f"MHLW確認{n_mhlw_confirmed}件→M1精度↑(+8%)")
    elif n_mhlw_confirmed >= 1:
        w += 0.04
        reasons.append(f"MHLW確認{n_mhlw_confirmed}件→M1精度↑(+4%)")

    w = max(0.30, min(0.70, w))
    reason = f"スマートブレンド M1:{w:.0%} / M2:{1-w:.0%} （{', '.join(reasons)}）"
    return w, reason


def apply_clinic_congestion_factor(
    facilities: List["NearbyFacility"],
    log: Optional[List[str]] = None,
) -> List["NearbyFacility"]:
    """
    医療機関が密集しているエリアでは、クリニック1件あたりの患者数が分散する。
    MHLWで外来患者数が確認されていない（デフォルト値を持つ）施設に補正係数を適用する。

    ■ 根拠:
      医療機関が多いエリアは「患者を奪い合う競争環境」であり、1件あたりの平均患者数が
      全国平均より少ない。例えば国立市中1丁目のような医療モールエリアでは10件以上の
      クリニックが集中するため、個々の患者数は大きく下がる。

    ■ v4.2: 指数減衰に変更（ステップ関数の閾値ジャンプを廃止）
      factor = max(0.50, exp(−0.035 × max(0, n − 5))
      対照表（旧 → 新）:
        n=5:  1.00 → 1.00
        n=9:  0.85 → 0.87  (+0.02)
        n=14: 0.70 → 0.73  (+0.03)
        n=19: 0.60 → 0.61  (+0.01)
        n=24: 0.50 → 0.51  (≒同等、下限0.50)
    """
    # MHLW外来患者数未確認のデフォルト値施設のみカウント
    # ・OSM取得施設（is_manual=False, source="osm"）→ 対象
    # ・MHLW補填施設（is_manual=True,  source="mhlw"）→ 対象（外来数未確認のデフォルト値）
    # ・ユーザー手動入力施設（is_manual=True, source="osm"）→ 対象外（ユーザー入力値を尊重）
    unconfirmed = [
        f for f in facilities
        if f.mhlw_annual_outpatients is None
        and not (f.is_manual and f.source != "mhlw")
    ]
    n = len(unconfirmed)

    if n < 6:
        return facilities  # 補正不要

    # v4.2: 指数減衰 factor = max(0.50, exp(−0.035 × (n − 5)))
    factor = max(0.50, math.exp(-0.035 * (n - 5)))

    for f in unconfirmed:
        f.daily_outpatients = max(5, int(f.daily_outpatients * factor))

    if log is not None:
        log.append(
            f"[密集補正 v4.2] MHLW外来未確認施設{n}件 → 指数減衰係数×{factor:.3f}を適用"
            f"（外来患者数を約{1/factor:.1f}分の1に圧縮）"
        )
    return facilities


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
            "× 院外処方率（79.0%）× 当薬局集客シェア → 合計 × 年間稼働日数(305日)",
            f"**v3.2シェアロジック（残余シェアモデル）**: 医療機関50m以内に競合薬局があれば",
            f"「既存門前薬局」と判定し処方箋の{GATE_PHARMACY_CAPTURE_RATE:.0%}が捕捉されると仮定。",
            f"残り{1-GATE_PHARMACY_CAPTURE_RATE:.0%}を当薬局と非門前競合でHuff按分（二重割引なし）。",
            "既存門前薬局なし時は距離帯別ベース（≤50m:75%/≤150m:50%/≤300m:30%/300m超:15%）×Huff按分。",
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
            # v3.2: has_gate_pharmacy フラグによる固定×0.4 廃止。
            # 既存門前薬局の有無は _calc_share() 内で競合薬局の実際の立地から動的に判定。
            share, share_reason = self._calc_share(fac, pharmacy_lat, pharmacy_lon, competing_pharmacies)
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
                f"{fac.daily_outpatients}人/日 × {rx_rate:.0%} × 79.0% × {share:.0%} = {flow:.1f}枚/日"
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
        """
        医療機関ごとの当薬局集客シェアを推計する（v3.2: 残余シェアモデルに変更）

        ■ 設計思想（v3.2 大幅改修）:
          旧v3.2では「ベースシェア割引 × Huff按分」の二重割引になっており、
          既存門前薬局あり・競合1件の場合に1〜2%という過小推計が発生していた。

          → 「残余シェアモデル（Available Share Model）」に変更:
              [A] 医療機関50m以内に競合薬局（既存門前薬局）があるか検出
              [B] あれば: 処方箋の GATE_PHARMACY_CAPTURE_RATE(70%) が門前薬局に流れる
                  残り30% を当薬局と非門前競合でHuff按分
              [C] なければ: 距離帯別ベース × Huff按分（旧来通り）

        ■ 数値根拠（GATE_PHARMACY_CAPTURE_RATE = 70%）:
          日本の門前薬局シェア実態（業界調査・厚労省）: 対象施設の処方箋の60〜80%。
          単科クリニックの門前薬局は70〜85%が一般的。
          中央値として70%を採用（定数 GATE_PHARMACY_CAPTURE_RATE で調整可能）。

        ■ 数値例（整合性確認）:
          クリニック100人/日・60Rx/日、既存門前薬局(40m)、当薬局(150m)、競合1件(200m)
            門前薬局が70%捕捉 → 残30%(18Rx/日)を非門前で按分
            当薬局Huff比: (1/150) / (1/150 + 1/200) = 57%
            当薬局シェア: 30% × 57% ≒ 17%  → 18Rx/日 × 305日 ≒ 5,500枚/年
          ← 当薬局14,305枚/年全国平均のうち一施設分として妥当な水準
        """
        dist = OverpassSearcher._haversine(fac.lat, fac.lon, ph_lat, ph_lon)

        # ── [A] 既存門前薬局チェック: 医療機関から50m以内に競合薬局があるか ──
        gate_comps = [
            p for p in competitors
            if OverpassSearcher._haversine(fac.lat, fac.lon, p.lat, p.lon) <= 50
        ]

        if gate_comps:
            # ── [B] 既存門前薬局あり: 残余シェアモデル ────────────────────
            # 複数の門前薬局がある場合は捕捉率を加算（最大85%）
            gate_capture = min(
                GATE_PHARMACY_CAPTURE_RATE + (len(gate_comps) - 1) * 0.05,
                0.85,
            )
            available = 1.0 - gate_capture   # 残余シェア（当薬局+非門前競合で分配）

            # 非門前競合薬局のみを対象にHuff按分（300m以内）
            non_gate_ids = {id(p) for p in gate_comps}
            non_gate_comps = [
                p for p in competitors
                if id(p) not in non_gate_ids
                and OverpassSearcher._haversine(fac.lat, fac.lon, p.lat, p.lon) <= 300
            ]
            tw = 1.0 / max(dist, 10)
            cws = [
                1.0 / max(OverpassSearcher._haversine(fac.lat, fac.lon, p.lat, p.lon), 10)
                for p in non_gate_comps
            ]
            # Huff比: 競合がいなければ残余の100%を取得
            huff_ratio = tw / (tw + sum(cws)) if cws else 1.0
            adj = available * huff_ratio

            gate_names = "・".join(g.name[:10] for g in gate_comps[:2])
            comp_note = f"（非門前競合{len(non_gate_comps)}件でHuff按分）" if non_gate_comps else "（非門前競合なし）"
            reason = (
                f"既存門前薬局あり（{gate_names} 等{len(gate_comps)}件: 推定{gate_capture:.0%}捕捉）"
                f" → 残{available:.0%}を{comp_note}"
            )
        else:
            # ── [C] 既存門前薬局なし: 距離帯別ベース × Huff按分 ─────────
            if dist <= 50:
                base, reason = 0.75, "50m以内（実質門前）"
            elif dist <= 150:
                base, reason = 0.50, "150m以内（近接立地）"
            elif dist <= 300:
                base, reason = 0.30, "300m以内（徒歩圏）"
            else:
                base, reason = 0.15, "300m超（自転車圏）"

            near_comps = [
                p for p in competitors
                if OverpassSearcher._haversine(fac.lat, fac.lon, p.lat, p.lon) < 300
            ]
            if near_comps:
                tw = 1.0 / max(dist, 10)
                cws = [
                    1.0 / max(OverpassSearcher._haversine(fac.lat, fac.lon, p.lat, p.lon), 10)
                    for p in near_comps
                ]
                adj = base * (tw / (tw + sum(cws)))
                reason += f"（競合{len(near_comps)}件で按分）"
            else:
                adj = base
                reason += "（近隣競合なし）"

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
        nearby_medical: Optional[List[NearbyFacility]] = None,  # v3.2: 門前的競合判定用
        pharmacy_type: str = PHARMACY_TYPE_NORMAL,  # v4.4: SM業態補正用
    ) -> PredictionResult:
        area_km2 = math.pi * (radius_m / 1000) ** 2
        total_pop = int(area_km2 * area_density)

        # v4.4バグ修正: _density_band()は長形式「高密度(5k-10k)」を返すが
        # DENSITY_AGE_DISTRIBUTION は短形式「高密度」キーを使用するため不一致が発生していた。
        # _density_band_label()（短形式）でルックアップし、表示には長形式も保持する。
        density_band_short = _density_band_label(area_density)   # テーブル照合用
        density_band_long  = _density_band(area_density)          # 表示・校正用

        # v4.2: 人口密度帯別年齢分布テーブルを使用（都市部は若年多め、農村部は高齢多め）
        age_dist = DENSITY_AGE_DISTRIBUTION.get(density_band_short, AGE_DISTRIBUTION)
        age_breakdown, total_rx = [], 0
        for age_grp, ratio in age_dist.items():
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
        # v4.4: pharmacy_type を渡してSM業態の市場シェア上限を調整
        share, share_reason = self._market_share(
            pharmacy_lat, pharmacy_lon, competing_pharmacies, nearby_medical,
            pharmacy_type=pharmacy_type,
        )
        # v4.4: pharmacy_type を渡してSM業態の流入係数を調整
        inflow_coeff, inflow_reason = self._inflow_coefficient(
            area_density, pharmacy_type=pharmacy_type
        )
        effective_rx = int(total_rx * inflow_coeff)   # 流入補正後の実効処方箋プール
        annual_est = int(effective_rx * share)

        # 加重平均受診率（密度帯補正後）
        avg_visit_rate = sum(
            age_dist[ag] * VISIT_RATE_BY_AGE[ag] for ag in age_dist
        )
        # 65歳以上の割合（表示用）
        elderly_ratio = age_dist.get("65-74歳", 0) + age_dist.get("75歳以上", 0)

        # SM業態注記
        sm_note_lines: List[str] = []
        if pharmacy_type == PHARMACY_TYPE_SUPERMARKET:
            sm_note_lines = [
                "",
                f"**⚠ v4.4 SM業態補正**: 流入係数に×{SM_INFLOW_COEFFICIENT_RATIO}を適用",
                f"  SM来客の処方箋化率は通常薬局より低いため（処方箋は主治医近辺薬局で受ける）",
                f"  市場シェア上限: {SM_MARKET_SHARE_CAP:.0%}（通常80%→SM{SM_MARKET_SHARE_CAP:.0%}）",
            ]

        methodology = [
            "### 方法②（商圏人口動態アプローチ）ロジック",
            "",
            "**算出式（v4.4）**: 商圏人口 × 密度帯別年齢分布 × 年齢層別受診率 × 処方箋発行率(69%)",
            "× 院外処方率(79.0%) × **処方箋流入係数** × 当薬局市場シェア",
            f"**v4.2改善**: 密度帯`{density_band_short}`の年齢分布を適用（65歳以上: {elderly_ratio:.1%}）",
            "旧: 全国固定値（65歳以上28.3%）→ 新: 都市部20%〜農村部38%",
            f"**v4.4バグ修正**: 密度帯ラベル不一致（{density_band_long}→{density_band_short}）を解消",
        ] + sm_note_lines + [
            "",
            f"**商圏設定**: 半径{radius_m}m（面積: {area_km2:.2f}km²）",
            f"**根拠**: {radius_reason}" if radius_reason else "",
            f"**人口密度**: {area_density:,}人/km²（{density_source}）",
            f"**推計商圏人口**: {total_pop:,}人",
            f"**加重平均受診率**: {avg_visit_rate:.2f}回/人/年 "
            f"（患者調査2020年 外来受療率×365日 / OECD日本: 12.6回/年と整合）",
            "",
            f"**商圏居住人口由来の年間処方箋**: {total_rx:,}枚",
            f"**処方箋流入係数**: ×{inflow_coeff:.2f}（{inflow_reason}）",
            f"**実効処方箋プール（流入補正後）**: {effective_rx:,}枚",
            f"**当薬局推計市場シェア**: {share:.1%}（{share_reason}）",
            f"**推計年間処方箋枚数**: **{annual_est:,}枚/年**",
            "",
            "**処方箋流入係数について（v3.2追加）**: 商圏居住人口だけでは捉えられない",
            "流入処方箋（近隣医療機関通院患者・昼間人口等）を密度帯別に補正。",
            f"MHLW全国中央値(8,000枚/年)・平均(14,305枚/年)との整合性検証済み。",
            "",
            "**市場シェアの計算方法（v3.2改善）**: 距離帯別実効競合数モデル（門前的競合を強化）",
            "基本重み: 競合≤200m×1.5 + 競合≤500m×1.0 + 競合>500m×0.5 = 実効競合数N",
            "v3.2追加: 医療機関100m以内の競合薬局（門前的競合）は基本重み×(5/3)で強化計算",
            f"シェア = 1/(N+1)　上限{'55%（SM業態）' if pharmacy_type==PHARMACY_TYPE_SUPERMARKET else '80%（通常業態）'}（競合ゼロ時） / 下限8%",
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

    def _market_share(
        self,
        lat: float,
        lon: float,
        competitors: List[NearbyFacility],
        nearby_medical: Optional[List[NearbyFacility]] = None,
        pharmacy_type: str = PHARMACY_TYPE_NORMAL,  # v4.4追加
    ) -> Tuple[float, str]:
        """
        距離帯別「実効競合数モデル」による市場シェア推計 (v3.2改善)

        v2.3 修正: 距離帯別に競合を「実効競合数」に換算して Huff型シェアを計算。
            ≤200m （近接）: 重み 1.5
            ≤500m （中距離）: 重み 1.0
            >500m （遠距離）: 重み 0.5

        v3.2 追加: 「門前的競合」（医療機関から100m以内に位置する競合薬局）は
            実効競合数の重みを ×1.5→×2.5 に強化。
            門前薬局はその医療機関の処方箋を独占的に集めるため、
            実際の競合力は距離だけの評価より大きい。

        v4.4 追加: pharmacy_type による上限調整。
            通常: 上限80%  SM業態: 上限55%（広域商圏での処方箋帰属分散を反映）

            シェア = 1 / (実効競合数 + 1)
            上限: 通常80% / SM55% （残余は商圏外流出・かかりつけ医近辺薬局へ）
            下限:  8% （極めて競合が多い場合でも最低限を確保）
        """
        # v4.4: SM業態ではシェア上限を引き下げ
        share_cap = SM_MARKET_SHARE_CAP if pharmacy_type == PHARMACY_TYPE_SUPERMARKET else 0.80
        no_comp_reason = (
            f"商圏内競合なし（上限{share_cap:.0%}: "
            + ("SM業態のため処方箋帰属分散を考慮）" if pharmacy_type == PHARMACY_TYPE_SUPERMARKET
               else "門前独占。残20%は商圏外流出・他エリア受診を考慮）")
        )
        if not competitors:
            return share_cap, no_comp_reason

        # 門前的競合の特定: 医療機関から100m以内にいる競合薬局
        gate_comp_names: set = set()
        if nearby_medical:
            for med in nearby_medical:
                for p in competitors:
                    if OverpassSearcher._haversine(med.lat, med.lon, p.lat, p.lon) <= 100:
                        gate_comp_names.add(p.name)

        # 実効競合数の計算（門前的競合は重みを強化）
        effective_n = 0.0
        near_count = medium_count = distant_count = 0
        gate_count = 0
        for p in competitors:
            d = p.distance_m
            is_gate_comp = p.name in gate_comp_names
            # 基本重み（距離帯別）
            if d <= 200:
                w = 1.5
                near_count += 1
            elif d <= 500:
                w = 1.0
                medium_count += 1
            else:
                w = 0.5
                distant_count += 1
            # 門前的競合は重みを ×(5/3) ≈ 1.67 倍 → 近接1.5→2.5、中距離1.0→1.67
            if is_gate_comp:
                w *= (5.0 / 3.0)
                gate_count += 1
            effective_n += w

        # Huff型シェア: 自社1 / (自社1 + 競合 effective_n)
        raw_share = 1.0 / (effective_n + 1.0)

        # v4.4: SM業態は上限55%、通常は上限80% / 下限8% でクリップ
        share = max(min(raw_share, share_cap), 0.08)

        gate_note = f" うち門前的競合{gate_count}件（重み×5/3）" if gate_count else ""
        detail = (
            f"近接≤200m: {near_count}件×1.5 + 中距離≤500m: {medium_count}件×1.0 "
            f"+ 遠距離>500m: {distant_count}件×0.5{gate_note} = 実効{effective_n:.1f}件"
        )
        cap_note = f"（SM上限{share_cap:.0%}適用）" if pharmacy_type == PHARMACY_TYPE_SUPERMARKET and raw_share > share_cap else ""
        reason = f"競合{len(competitors)}件 ({detail}) → シェア{share:.1%}{cap_note}"
        return share, reason

    @staticmethod
    def _inflow_coefficient(
        density: int,
        pharmacy_type: str = PHARMACY_TYPE_NORMAL,
    ) -> Tuple[float, str]:
        """
        処方箋流入係数（v3.2新規 / v4.4改訂）: 商圏居住人口から算出した処方箋総数に対する補正係数。

        ■ 背景
          Method 2 は「商圏内居住者が生む処方箋」を算出するが、
          実際の薬局が受け取る処方箋にはそれ以外の流入分が含まれる。

          【流入要因】
            ① 近隣の医療機関に商圏外から通院する患者が帰宅途中に立ち寄る
            ② 昼間人口（通勤者・来街者）が職場/外出先で受け取った処方箋を持参
            ③ 受療圏が広い地方都市で患者が遠方から来院
          【流出要因】
            ④ 商圏内居住者の一部が職場近くの薬局を利用

          ネット効果（流入 - 流出）は都市部ほど大きい（大病院・オフィス集積地）。

        ■ v4.4 SM業態補正 (pharmacy_type == PHARMACY_TYPE_SUPERMARKET)
          SM内薬局は、来客が「処方箋を持参した患者」ではなく「買い物客」であるため、
          流入係数に SM_INFLOW_COEFFICIENT_RATIO（0.40）を乗算して処方箋化率を補正する。
          根拠: 慢性処方（~65%）はかかりつけ薬局へ、SM捕捉は急性処方（~35%）中心。
                実効捕捉率≈28%、習慣利用補正込みで0.40採用（M2/M1比≈1.2〜1.3に収束）。

          高密度都市(6,000/km²): 1.40 × 0.40 = 0.56 (SM)
          中密度郊外(3,000/km²): 1.25 × 0.40 = 0.50 (SM)

        ■ 校正根拠
          厚生労働省「調剤医療費の動向」2022年度（全国平均14,305枚/年・中央値8,000枚/年）
          と代表的シナリオの推計値が近似するよう密度帯別に設定。
        """
        if density >= 10_000:
            base, note = 1.60, "大都市中心部（昼間人口・大病院集積による流入が大）"
        elif density >= 5_000:
            base, note = 1.40, "都市郊外（住宅・商業・医療の混在による流入）"
        elif density >= 2_000:
            base, note = 1.25, "中密度住宅地（近隣からの受療圏流入）"
        elif density >= 500:
            base, note = 1.12, "地方都市（受療圏が広く流入は中程度）"
        else:
            base, note = 1.05, "農村部（受療圏は基本的に地域内完結）"

        if pharmacy_type == PHARMACY_TYPE_SUPERMARKET:
            coeff = round(base * SM_INFLOW_COEFFICIENT_RATIO, 3)
            note += (
                f" ⚠ SM業態補正×{SM_INFLOW_COEFFICIENT_RATIO}適用"
                f"（来客の処方箋化率補正: {base:.2f}→{coeff:.2f}）"
            )
            return coeff, note
        return base, note


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
        # v2.6: source別に色分け
        if getattr(fac, "source", "osm") == "mhlw":
            color, icon_n = "purple", "certificate"
            label = "🟣 [MHLW補填]"
        elif fac.is_manual:
            color, icon_n = "orange", "asterisk"
            label = "🟠 [手動追加]"
        elif fac.facility_type == "hospital":
            color, icon_n = "blue", "h-sign"
            label = "🏥 病院"
        else:
            color, icon_n = "cadetblue", "user-md"
            label = "🏨 クリニック"
        inhouse = "（院内薬局あり）" if fac.has_inhouse_pharmacy else ""
        src = getattr(fac, "source", "osm")
        source_note = {
            "mhlw":   "<br><b style='color:#8e44ad'>※ MHLW自動補填（OSM未収録）</b>",
            "manual": "<br><b style='color:#e67e00'>※ 手動追加施設（OSM未収録）</b>",
        }.get(src, "")
        popup_html = (
            f"<b>{label} {fac.name}</b>{inhouse}{source_note}<br>"
            f"診療科: {fac.specialty}<br>"
            f"距離: {fac.distance_m:.0f}m | 外来(推計): {fac.daily_outpatients}人/日"
        )
        if fac.mhlw_annual_outpatients:
            popup_html += f"<br>MHLW年間外来: {fac.mhlw_annual_outpatients:,}人"
        folium.Marker(
            location=[fac.lat, fac.lon],
            popup=folium.Popup(popup_html, max_width=260),
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
    # 誘致予定クリニック（門前込みシナリオ: combined / gate_only / all）
    if config.scenario in ("combined", "gate_only", "all"):
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
# 8-b. v2.4: 手動施設追加ヘルパー
# ---------------------------------------------------------------------------

def calc_implied_missing_facility(actual_rx: int, predicted_rx: int) -> Optional[Dict]:
    """
    方法①予測と MHLW 実績の乖離から「未検出医療施設」の規模を逆算する。

    乖離が実績の20%未満の場合は「許容範囲内」として None を返す。
    逆算は一般内科の処方箋発行率(0.76)×院外処方率(0.790)で行う。
    """
    if actual_rx <= 0 or predicted_rx >= actual_rx * 0.8:
        return None
    gap_annual = actual_rx - predicted_rx
    gap_daily  = gap_annual / NATIONAL_STATS["working_days"]
    # 一般内科で逆算（最も一般的な想定）
    rx_rate = SPECIALTY_RX_RATES["一般内科"][0]   # 0.76
    or_rate = NATIONAL_STATS["outpatient_rx_rate"]  # 0.790
    # gap_daily = implied_op × rx_rate × or_rate
    implied_op = int(gap_daily / (rx_rate * or_rate))
    gap_pct    = (actual_rx - predicted_rx) / actual_rx * 100
    return {
        "gap_annual":        gap_annual,
        "gap_daily":         round(gap_daily, 1),
        "implied_outpatients": implied_op,
        "gap_pct":           gap_pct,
    }


def fetch_mhlw_medical_supplement(
    pharmacy_lat: float,
    pharmacy_lon: float,
    pharmacy_address: str,
    pref_code: str,
    existing_osm: List["NearbyFacility"],
    search_radius_m: int,
    progress_bar=None,
    max_candidates: int = 150,
    dedup_threshold_m: float = 80.0,
) -> Tuple[List["NearbyFacility"], List[str]]:
    """
    MHLWエリア検索 → ジオコーディング → OSM重複排除 → 新規NearbyFacilityリストを返す (v2.6)

    処理フロー:
      1. MHLWScraper.search_medical_by_area() でエリア内の医療機関を一括取得
      2. 各施設の住所をGSIジオコーダーで座標変換
      3. 薬局からの距離が search_radius_m 以内のもののみ保持
      4. OSM施設との重複チェック（dedup_threshold_m 以内 = 同一施設とみなしスキップ）
      5. 新規施設を NearbyFacility (source="mhlw") として返す

    Args:
        progress_bar: st.progress() オブジェクト（任意）
        max_candidates: 最大処理候補数（多すぎると時間がかかる）
        dedup_threshold_m: 重複とみなす距離しきい値[m]

    Returns:
        (new_facilities, log_messages)
    """
    log: List[str] = []
    gc = GeocoderService()
    scraper = MHLWScraper()
    scraper.initialize_session()

    # ── Step 1: MHLWエリア検索 ─────────────────────────────────────────────
    if progress_bar:
        progress_bar.progress(5, text="MHLW医療機関データベースをエリア検索中…")
    cands, search_msg = scraper.search_medical_by_area(pharmacy_address, pref_code, max_pages=5)
    log.append(f"[MHLW補填] {search_msg}")
    if not cands:
        log.append("[MHLW補填] 候補なし。補填を終了します。")
        return [], log
    cands = cands[:max_candidates]
    log.append(f"[MHLW補填] 処理対象: {len(cands)}件")

    # ── Step 2: ジオコーディング + 半径フィルタ ───────────────────────────
    geocoded: List[Tuple["PharmacyCandidate", float, float, float]] = []
    for i, cand in enumerate(cands):
        if progress_bar:
            pct = 10 + int(i * 70 / len(cands))
            progress_bar.progress(
                pct,
                text=f"ジオコーディング [{i+1}/{len(cands)}]: {cand.name[:22]}…",
            )
        if not cand.address:
            continue
        lat, lon, _, _ = gc.geocode(cand.address)
        if lat is None or lon is None:
            continue
        dist = haversine_distance(pharmacy_lat, pharmacy_lon, lat, lon)
        if dist <= search_radius_m:
            geocoded.append((cand, lat, lon, dist))
        time.sleep(0.12)   # GSI APIのレート制限への配慮

    log.append(
        f"[MHLW補填] ジオコーディング完了: 半径{search_radius_m}m内 {len(geocoded)}件"
    )

    # ── Step 3: OSM重複排除 ───────────────────────────────────────────────
    if progress_bar:
        progress_bar.progress(85, text="OSMデータと重複チェック中…")

    new_facilities: List["NearbyFacility"] = []
    dup_count = 0
    for cand, lat, lon, dist in geocoded:
        is_dup = any(
            haversine_distance(lat, lon, osm.lat, osm.lon) <= dedup_threshold_m
            for osm in existing_osm
        )
        if is_dup:
            dup_count += 1
            log.append(f"  [重複スキップ] {cand.name} ({dist:.0f}m) ← OSM施設と{dedup_threshold_m:.0f}m以内")
            continue

        # 施設タイプ・診療科・外来患者数のデフォルト推定
        is_hospital = any(kw in cand.name for kw in ["病院", "医療センター", "医療機構", "医院"])
        fac_type    = "hospital" if is_hospital else "clinic"
        specialty   = detect_specialty_from_name(cand.name)
        # v4.2: 診療科別外来患者数テーブルを使用（MHLW補填施設にも適用）
        # MHLWでリストされているが外来患者数の詳細が取れない施設は、OSM収録施設と同様に
        # 診療科別基礎値×0.85 で推定。病院は固定120人/日。
        if fac_type == "hospital":
            default_op = 120
        else:
            sp_key = specialty if specialty in SPECIALTY_OUTPATIENT_TABLE else "不明/その他"
            sp_d = SPECIALTY_OUTPATIENT_TABLE[sp_key]
            default_op = max(5, int(sp_d["base"] * 0.85))

        new_facilities.append(NearbyFacility(
            name=cand.name,
            facility_type=fac_type,
            lat=lat,
            lon=lon,
            distance_m=int(dist),
            specialty=specialty,
            daily_outpatients=default_op,
            has_inhouse_pharmacy=(fac_type == "hospital"),
            is_manual=True,
            source="mhlw",
        ))

    log.append(
        f"[MHLW補填] OSM重複: {dup_count}件 / 新規追加: {len(new_facilities)}件"
    )
    for fac in new_facilities:
        log.append(
            f"  ＋ {fac.name}（診療科: {fac.specialty}、"
            f"{fac.distance_m}m、推計{fac.daily_outpatients}人/日）"
        )

    if progress_bar:
        progress_bar.progress(100, text="MHLW補填完了！")

    return new_facilities, log


def make_manual_facility(
    pharmacy_lat: float,
    pharmacy_lon: float,
    name: str,
    specialty: str,
    daily_outpatients: int,
    distance_m: float,
    has_inhouse: bool = False,
) -> NearbyFacility:
    """
    手動入力パラメータから NearbyFacility オブジェクトを生成する。

    lat/lon は「薬局から distance_m 北方向」という近似値を設定する。
    （_calc_share での haversine 計算に使われるため、実際の位置と多少異なっても
    distance_m を直接入力した値から算出されたものと概ね同じ結果になる）
    """
    delta_lat = distance_m / 111_000  # 1° ≈ 111 km
    return NearbyFacility(
        name=name,
        facility_type="clinic",
        lat=pharmacy_lat + delta_lat,
        lon=pharmacy_lon,
        distance_m=distance_m,
        specialty=specialty,
        daily_outpatients=daily_outpatients,
        has_inhouse_pharmacy=has_inhouse,
        is_manual=True,
    )


def recalculate_with_manual_facilities(analysis: "FullAnalysis") -> None:
    """
    手動追加施設 + MHLW自動補填施設を含めて方法①②を再計算し、session_state を更新する。
    MHLW・OSM の再検索は行わない（既存のデータを再利用）。

    データソース優先順位:
      1. analysis.nearby_medical — 初回OSM検索結果（不変）
      2. session_state["mhlw_supplement"] — MHLW自動補填施設 (v2.6)
      3. session_state["manual_facility_params"] — ユーザー手動追加施設 (v2.4〜)
    """
    # ① OSM施設（元データ）
    osm_facs = list(analysis.nearby_medical)

    # ② MHLW自動補填施設 (v2.6)
    mhlw_facs: List[NearbyFacility] = st.session_state.get("mhlw_supplement", [])

    # ③ 手動追加施設
    params_list: List[Dict] = st.session_state.get("manual_facility_params", [])
    manual_facs: List[NearbyFacility] = [
        make_manual_facility(
            analysis.pharmacy_lat, analysis.pharmacy_lon,
            p["name"], p["specialty"], p["daily_outpatients"],
            p["distance_m"], p["has_inhouse"],
        )
        for p in params_list
    ]

    merged_medical = osm_facs + mhlw_facs + manual_facs

    # ラベル生成
    label_parts = [f"OSM {len(osm_facs)}件"]
    if mhlw_facs:
        label_parts.append(f"MHLW補填 {len(mhlw_facs)}件")
    if manual_facs:
        label_parts.append(f"手動追加 {len(manual_facs)}件")
    label = "方法①: 近隣医療機関（" + "＋".join(label_parts) + "）"

    m1 = Method1Predictor().predict(
        analysis.pharmacy_lat, analysis.pharmacy_lon,
        merged_medical, analysis.nearby_pharmacies,
        mode_label=label,
    ) if analysis.pharmacy_lat else None

    m2 = Method2Predictor().predict(
        analysis.pharmacy_lat, analysis.pharmacy_lon,
        analysis.nearby_pharmacies,
        analysis.area_density, analysis.commercial_radius,
        density_source=analysis.area_density_source,
        radius_reason=analysis.commercial_radius_reason,
        nearby_medical=merged_medical,  # v3.2: 門前的競合判定用
    )

    updated = dataclasses.replace(analysis, method1=m1, method2=m2)
    st.session_state["analysis"] = updated
    st.rerun()


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
            st.caption(f"📐 {commercial_radius_reason[:55]}")
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
    cal_stats: Optional[CalibrationStats] = st.session_state.get("calibration_stats")

    # v4.1: 校正済み予測を計算
    cal_rx: Optional[int] = None
    cal_note: str = ""
    if cal_stats and m1 and m2:
        cal_rx, cal_note = CalibrationEngine.apply_correction(
            m1.annual_rx, m2.annual_rx, analysis.area_density, cal_stats
        )

    n_cols = 5 if cal_rx is not None else 4
    cols = st.columns(n_cols)

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
            st.caption("📌 精度±30〜40%")
    with cols[2]:
        if m2:
            st.metric("② 人口動態アプローチ", f"{m2.annual_rx:,} 枚/年",
                      delta=calc_deviation(actual, m2.annual_rx)[1] if actual else None)
            st.caption(f"レンジ: {m2.min_val:,}〜{m2.max_val:,}")
            st.caption("📌 精度±40〜50%")
    with cols[3]:
        if cal_rx is not None:
            st.metric(
                "🎯 校正済み予測 (v4.x)",
                f"{cal_rx:,} 枚/年",
                delta=calc_deviation(actual, cal_rx)[1] if actual else None,
            )
            st.caption(f"MAPE={cal_stats.mape_optimal:.1f}% / n={cal_stats.n}")
            with st.expander("校正パラメータ詳細"):
                st.caption(cal_note)
        elif m1 and m2:
            # v4.2: 単純平均 → スマートブレンドに変更
            n_med = len(analysis.nearby_medical) if analysis.nearby_medical else 0
            n_conf = sum(
                1 for f in (analysis.nearby_medical or [])
                if f.mhlw_annual_outpatients is not None
            )
            sw, sw_reason = calc_smart_blend_weight(
                analysis.area_density, n_med, n_conf
            )
            smart_rx = int(sw * m1.annual_rx + (1 - sw) * m2.annual_rx)
            pct, label, _ = calc_deviation(actual, smart_rx)
            st.metric("💡 スマートブレンド (v4.2)", f"{smart_rx:,} 枚/年",
                      delta=label if actual else None,
                      delta_color="normal" if abs(pct) < 30 else "inverse")
            with st.expander("ブレンド根拠"):
                st.caption(sw_reason)
            st.caption("（校正実施で🎯校正済み予測に切替）")
    if n_cols == 5:
        with cols[4]:
            if m1 and m2:
                # 校正あり時の5列目: スマートブレンドも表示
                n_med = len(analysis.nearby_medical) if analysis.nearby_medical else 0
                n_conf = sum(
                    1 for f in (analysis.nearby_medical or [])
                    if f.mhlw_annual_outpatients is not None
                )
                sw, sw_reason = calc_smart_blend_weight(
                    analysis.area_density, n_med, n_conf
                )
                smart_rx = int(sw * m1.annual_rx + (1 - sw) * m2.annual_rx)
                pct, label, _ = calc_deviation(actual, smart_rx)
                st.metric("💡 スマートブレンド (v4.2)", f"{smart_rx:,} 枚/年",
                          delta=label if actual else None,
                          delta_color="normal" if abs(pct) < 30 else "inverse")


def render_gap_and_manual_input(analysis: "FullAnalysis") -> None:
    """
    v2.6: 乖離警告 + MHLWエリア自動補填 + 近隣施設手動追加 + 再計算セクション

    乖離が大きい場合の補完方法:
      1. MHLW自動補填 (v2.6新機能) ← 見落としが少ない
      2. 手動追加 (v2.4〜) ← 特定施設を名前で検索・追加
    """
    actual = analysis.mhlw_annual_rx
    m1 = analysis.method1

    implied = calc_implied_missing_facility(actual or 0, m1.annual_rx if m1 else 0)

    # ─── 乖離警告 ───────────────────────────────────────────────────────────
    if implied:
        st.warning(
            f"⚠ **方法①の予測が実績値を大幅に下回っています（乖離: "
            f"{implied['gap_annual']:,}枚/年 / {implied['gap_pct']:.0f}%）**\n\n"
            f"乖離から逆算すると、**外来 約{implied['implied_outpatients']}人/日** の"
            f"クリニック（一般内科想定）が検出されていない可能性があります。\n\n"
            f"MHLW自動補填は分析実行時に自動で行われています。"
            f"下記の補填結果をご確認いただき、施設の手動追加も可能です。"
        )

    # ─── v3.1: MHLW補填結果表示（自動実行済み）────────────────────────────
    mhlw_facs: List[NearbyFacility] = st.session_state.get("mhlw_supplement", [])

    with st.expander(
        f"🟣 MHLW自動補填結果"
        + (f"（✅ {len(mhlw_facs)}件を補填済み・方法①に反映）" if mhlw_facs else "（補填施設なし）"),
        expanded=bool(mhlw_facs),
    ):
        if mhlw_facs:
            st.success(
                f"✅ 分析実行時に厚労省DBから **{len(mhlw_facs)}件** のOSM未収録医療機関を自動補填し、"
                f"方法①の推計に反映済みです。"
            )
            for i, fac in enumerate(mhlw_facs):
                c1, c2 = st.columns([5, 1])
                c1.markdown(
                    f"🟣 **{fac.name}** / {fac.specialty} / "
                    f"推計{fac.daily_outpatients}人/日 / {fac.distance_m:.0f}m"
                )
                if c2.button("削除", key=f"del_mhlw_{i}", use_container_width=True):
                    st.session_state["mhlw_supplement"].pop(i)
                    recalculate_with_manual_facilities(analysis)
            st.markdown("---")
        else:
            st.info(
                "MHLW補填による新規施設はありませんでした。"
                "（OSMとの重複排除後、新規施設なし）"
            )

        # 再実行ボタン（必要な場合のみ）
        if analysis.pharmacy_lat:
            search_r = analysis.commercial_radius + 200
            pref_code = ""
            for pref_name, pc in PREFECTURE_CODES.items():
                if pref_name in analysis.pharmacy_address:
                    pref_code = pc
                    break
            st.caption(
                f"補填条件: エリアキーワード **{extract_area_keyword(analysis.pharmacy_address)}** / "
                f"半径 {search_r}m"
            )
            if st.button(
                "🔄 MHLW補填を再実行", key="mhlw_rerun_btn",
                use_container_width=True,
                help="補填条件を変えて再取得したい場合にクリック"
            ):
                progress = st.progress(0, text="MHLW医療機関データベースを再検索中…")
                new_facs, sup_log = fetch_mhlw_medical_supplement(
                    pharmacy_lat=analysis.pharmacy_lat,
                    pharmacy_lon=analysis.pharmacy_lon,
                    pharmacy_address=analysis.pharmacy_address,
                    pref_code=pref_code,
                    existing_osm=list(analysis.nearby_medical),
                    search_radius_m=search_r,
                    progress_bar=progress,
                )
                st.session_state["mhlw_supplement_log"] = sup_log
                progress.empty()
                st.session_state["mhlw_supplement"] = new_facs
                recalculate_with_manual_facilities(analysis)

        if st.session_state.get("mhlw_supplement_log"):
            with st.expander("📋 補填ログを表示"):
                st.code("\n".join(st.session_state["mhlw_supplement_log"]))

    # ─── 手動追加セクション ──────────────────────────────────────────────────
    params_list: List[Dict] = st.session_state.get("manual_facility_params", [])

    with st.expander(
        f"🏥 近隣施設を手動追加して再計算（現在 {len(params_list)} 件追加済み）"
        + (" ← 乖離の補正はこちら" if implied else ""),
        expanded=bool(implied),
    ):
        # ── 追加済み施設一覧 ──
        if params_list:
            st.markdown("**▶ 手動追加済み施設**")
            for i, p in enumerate(params_list):
                c1, c2 = st.columns([5, 1])
                c1.markdown(
                    f"- **{p['name']}** / {p['specialty']} / {p['daily_outpatients']}人/日 / "
                    f"{p['distance_m']:.0f}m"
                    + ("（院内薬局あり）" if p["has_inhouse"] else "")
                )
                if c2.button("削除", key=f"del_fac_{i}", use_container_width=True):
                    del st.session_state["manual_facility_params"][i]
                    st.rerun()
            st.markdown("---")

        # ── MHLWクリニック検索 ──
        st.markdown("##### 🔍 MHLW医療機関データベースから検索して追加")
        col_kw, col_pref = st.columns([3, 1])
        with col_kw:
            clinic_kw = st.text_input(
                "クリニック名（一部でも可）",
                placeholder="例: 武蔵小杉 内科 / ○○クリニック / △△医院",
                key="clinic_search_kw_v4",
            )
        with col_pref:
            clinic_pref = st.selectbox(
                "都道府県（任意）", ["（指定なし）"] + PREFECTURES,
                key="clinic_search_pref_v4",
            )

        if st.button("🔍 MHLWで検索", key="search_clinic_btn_v4", use_container_width=True):
            if clinic_kw.strip():
                with st.spinner("MHLW医療機関データベースを検索中…"):
                    scraper = MHLWScraper()
                    scraper.initialize_session()
                    cands = scraper.search_clinic_by_keyword(
                        clinic_kw.strip(),
                        PREFECTURE_CODES.get(clinic_pref, ""),
                    )
                st.session_state["clinic_candidates_v4"] = cands
                if not cands:
                    st.info("該当施設が見つかりませんでした。別のキーワードをお試しください。")
            else:
                st.warning("クリニック名を入力してください。")

        clinic_cands: List[PharmacyCandidate] = st.session_state.get("clinic_candidates_v4", [])
        if clinic_cands:
            options = [f"{c.name}　{c.address[:35]}" for c in clinic_cands]
            sel_label = st.selectbox("検索結果から選択", options, key="clinic_sel_v4")
            sel_cand = clinic_cands[options.index(sel_label)]

            col_a, col_b, col_c, col_d = st.columns(4)
            mhlw_dist    = col_a.number_input("距離(m)", 1, 2000, 30, step=5, key="mhlw_dist_v4")
            mhlw_sp      = col_b.selectbox("診療科", list(SPECIALTY_RX_RATES.keys()),
                                           key="mhlw_sp_v4")
            mhlw_op      = col_c.number_input("外来患者数/日", 1, 500, 20, key="mhlw_op_v4",
                                              help="MHLW取得ボタンで自動取得を試みることもできます（デフォルト20人/日: 厚労省「医療施設調査」の無床診療所平均22人/日に準じた値）")
            mhlw_inhouse = col_d.checkbox("院内薬局あり", key="mhlw_inhouse_v4")

            col_fetch, col_add = st.columns(2)
            if col_fetch.button("📥 外来患者数をMHLWから取得", key="fetch_op_btn_v4"):
                with st.spinner("MHLW から外来患者数を取得中…"):
                    scraper2 = MHLWScraper()
                    scraper2.initialize_session()
                    op, msg = scraper2.get_clinic_daily_outpatients(sel_cand)
                if op:
                    st.success(f"取得成功: {op}人/日（{msg}）")
                    st.session_state["mhlw_fetched_op_v4"] = op
                else:
                    st.info(f"自動取得できませんでした（{msg}）。手動で入力してください。")

            fetched_op = st.session_state.get("mhlw_fetched_op_v4")
            if fetched_op:
                st.caption(f"💡 取得値: {fetched_op}人/日（上の入力欄に反映するには手動で上書きを）")

            if col_add.button("✅ この施設を追加", key="add_from_mhlw_v4", type="primary"):
                if "manual_facility_params" not in st.session_state:
                    st.session_state["manual_facility_params"] = []
                st.session_state["manual_facility_params"].append({
                    "name": sel_cand.name,
                    "specialty": mhlw_sp,
                    "daily_outpatients": mhlw_op,
                    "distance_m": float(mhlw_dist),
                    "has_inhouse": mhlw_inhouse,
                })
                st.session_state["clinic_candidates_v4"] = []
                st.session_state.pop("mhlw_fetched_op_v4", None)
                st.rerun()

        st.markdown("---")

        # ── 直接入力 ──
        st.markdown("##### ✍ または施設情報を直接入力")
        col1, col2, col3, col4 = st.columns(4)
        direct_name    = col1.text_input("施設名（任意）",
                                         placeholder="例: ○○クリニック",
                                         key="direct_name_v4")
        direct_sp      = col2.selectbox("診療科", list(SPECIALTY_RX_RATES.keys()),
                                        key="direct_sp_v4")
        direct_op      = col3.number_input("外来患者数/日", 1, 500, 50,
                                           key="direct_op_v4")
        direct_dist    = col4.number_input("距離(m)", 1, 2000, 30, step=5,
                                           key="direct_dist_v4")
        direct_inhouse = st.checkbox("院内薬局あり（→院外処方率が低下）",
                                     key="direct_inhouse_v4")

        if st.button("➕ 施設を直接追加", key="add_direct_v4"):
            if "manual_facility_params" not in st.session_state:
                st.session_state["manual_facility_params"] = []
            n = len(st.session_state["manual_facility_params"])
            st.session_state["manual_facility_params"].append({
                "name": direct_name.strip() or f"手動追加施設 #{n+1}",
                "specialty": direct_sp,
                "daily_outpatients": direct_op,
                "distance_m": float(direct_dist),
                "has_inhouse": direct_inhouse,
            })
            st.rerun()

        # ── 再計算ボタン ──
        updated_params = st.session_state.get("manual_facility_params", [])
        if updated_params:
            st.markdown("---")
            n_manual = len(updated_params)
            st.info(
                f"✅ {n_manual}件の手動追加施設が設定されています。"
                "「再計算」ボタンで予測に反映します。"
            )
            if st.button(
                f"🔄 手動追加施設（{n_manual}件）を含めて予測を再計算",
                type="primary", use_container_width=True, key="recalc_btn_v4",
            ):
                recalculate_with_manual_facilities(analysis)
        else:
            st.caption("施設を追加すると「再計算」ボタンが表示されます。")


def render_new_pharmacy_comparison(result: NewPharmacyResult) -> None:
    """新規開局モードの予測比較バナー（v2.5/v2.6: 3シナリオ対応）"""
    sc = result.config.scenario
    m1g = result.method1_gate   # 方法①（門前込み）
    m1a = result.method1_area   # 方法①（既存近隣のみ）
    m2  = result.method2        # 方法②（商圏人口）

    st.markdown("## 📊 開局シナリオ別 処方箋枚数予測")
    st.info(
        "📌 **推計レンジの見方**：レンジは統計的な不確実性を示します。"
        "方法①は近隣施設情報の精度次第で±30〜40%の誤差、"
        "方法②は商圏人口・市場シェア推計の影響で±40〜50%の誤差が想定されます。"
        "2手法の中間値を参考値として用い、±30%以内に収まる場合は信頼度が高いと判断してください。"
    )

    # ------------------------------------------------------------------
    # シナリオB: 面での集客 → 方法①（既存近隣）+ 方法②（商圏）
    # ------------------------------------------------------------------
    # v4.1: 校正済み予測の取得（シナリオB用）
    cal_stats_new: Optional[CalibrationStats] = st.session_state.get("calibration_stats")

    if sc == "area_dual":
        st.markdown("#### 🌐 シナリオB: 面での集客（方法①既存＋方法②）")
        n_cols_b = 4 if (cal_stats_new and m1a and m2) else 3
        cols = st.columns(n_cols_b)
        with cols[0]:
            if m1a:
                st.metric("① 近隣医療機関アプローチ", f"{m1a.annual_rx:,} 枚/年")
                st.caption(f"レンジ: {m1a.min_val:,}〜{m1a.max_val:,} | {m1a.daily_rx}枚/日")
                st.caption("既存OSM施設からの流入推計")
            else:
                st.info("方法①：近隣施設なし（推計不可）")
        with cols[1]:
            if m2:
                st.metric("② 商圏人口動態アプローチ", f"{m2.annual_rx:,} 枚/年")
                st.caption(f"レンジ: {m2.min_val:,}〜{m2.max_val:,} | {m2.daily_rx}枚/日")
                st.caption(f"商圏半径: {result.commercial_radius}m / 密度: {result.area_density:,}人/km²")
            else:
                st.info("方法②：推計不可")
        with cols[2]:
            if m1a and m2:
                avg = (m1a.annual_rx + m2.annual_rx) // 2
                diff = abs(m1a.annual_rx - m2.annual_rx)
                pct = diff / max(avg, 1) * 100
                st.metric("📈 2手法の中間推計", f"{avg:,} 枚/年")
                st.caption(f"手法間差: {diff:,}枚/年 ({pct:.0f}%)")
                if pct < 30:
                    st.caption("✅ 2手法の一致度: 高（推計精度良好）")
                elif pct < 70:
                    st.caption("⚠ 2手法の一致度: 中（誤差範囲に注意）")
                else:
                    st.caption("❗ 2手法の乖離が大きい（立地条件を要確認）")
        if n_cols_b == 4:
            with cols[3]:
                cal_rx_b, cal_note_b = CalibrationEngine.apply_correction(
                    m1a.annual_rx, m2.annual_rx, result.area_density, cal_stats_new
                )
                st.metric("🎯 校正済み推計 (v4.1)", f"{cal_rx_b:,} 枚/年")
                st.caption(f"MAPE={cal_stats_new.mape_optimal:.1f}% / n={cal_stats_new.n}")
                with st.expander("詳細"):
                    st.caption(cal_note_b)

    # ------------------------------------------------------------------
    # シナリオC: 面＋門前クリニック誘致 → 方法①（門前込み）+ 方法②（商圏）
    # ------------------------------------------------------------------
    elif sc == "combined":
        st.markdown(
            "#### 🏥 シナリオC: 面＋門前クリニック誘致（方法①門前込み＋方法②）\n\n"
            f"> 誘致クリニック: **{result.config.gate_specialty}** "
            f"（{result.config.gate_daily_outpatients}人/日）"
        )
        cols = st.columns(4)
        with cols[0]:
            if m1a:
                st.metric("① 面のみ（既存近隣）", f"{m1a.annual_rx:,} 枚/年")
                st.caption(f"{m1a.min_val:,}〜{m1a.max_val:,} | {m1a.daily_rx}枚/日")
            else:
                st.info("方法①(面のみ): 推計不可")
        with cols[1]:
            if m1g:
                st.metric("① 門前込み（面＋誘致）", f"{m1g.annual_rx:,} 枚/年")
                st.caption(f"{m1g.min_val:,}〜{m1g.max_val:,} | {m1g.daily_rx}枚/日")
            else:
                st.info("方法①(門前込み): 推計不可")
        with cols[2]:
            if m2:
                st.metric("② 商圏人口動態", f"{m2.annual_rx:,} 枚/年")
                st.caption(f"{m2.min_val:,}〜{m2.max_val:,} | {m2.daily_rx}枚/日")
                st.caption(f"商圏半径: {result.commercial_radius}m")
            else:
                st.info("方法②: 推計不可")
        with cols[3]:
            # 門前クリニック誘致の付加価値
            if m1g and m1a:
                gate_add = m1g.annual_rx - m1a.annual_rx
                st.metric(
                    "🚪 門前誘致の付加価値",
                    f"+{gate_add:,} 枚/年" if gate_add >= 0 else f"{gate_add:,} 枚/年",
                    delta=f"{gate_add:,}枚/年",
                )
                st.caption(f"= 門前込み({m1g.annual_rx:,}) − 面のみ({m1a.annual_rx:,})")
            elif m1g and m2:
                avg = (m1g.annual_rx + m2.annual_rx) // 2
                st.metric("📈 総合中間推計", f"{avg:,} 枚/年")
                st.caption("方法①門前込み＋方法②の平均")

    # ------------------------------------------------------------------
    # シナリオA: 門前クリニック誘致のみ → 方法①（門前込み）のみ
    # ------------------------------------------------------------------
    elif sc == "gate_only":
        st.markdown(
            "#### 🚪 シナリオA: 門前クリニック誘致（方法①のみ）\n\n"
            f"> 誘致クリニック: **{result.config.gate_specialty}** "
            f"（{result.config.gate_daily_outpatients}人/日）"
        )
        cols = st.columns(2)
        with cols[0]:
            if m1g:
                st.metric("① 近隣医療機関アプローチ（門前込み）", f"{m1g.annual_rx:,} 枚/年")
                st.caption(f"レンジ: {m1g.min_val:,}〜{m1g.max_val:,}枚/年 | {m1g.daily_rx}枚/日")
                st.caption(f"誘致科: {result.config.gate_specialty} ({result.config.gate_daily_outpatients}人/日)")
            else:
                st.info("方法①: 推計不可")
        with cols[1]:
            st.info(
                "💡 **ヒント**: シナリオCに切り替えると、方法②（商圏人口）と併用でき、\n"
                "門前クリニック誘致の**付加価値**（増枚数）も自動算出します。"
            )

    # ------------------------------------------------------------------
    # 全比較: B・C・Aを横並び表示
    # ------------------------------------------------------------------
    elif sc == "all":
        st.markdown("#### 🔄 全シナリオ一覧比較")
        cols = st.columns(4)
        with cols[0]:
            st.markdown("**シナリオB: 面のみ**")
            if m1a:
                st.metric("① 既存近隣", f"{m1a.annual_rx:,}")
                st.caption(f"{m1a.daily_rx}枚/日")
            if m2:
                st.metric("② 商圏人口", f"{m2.annual_rx:,}")
                st.caption(f"{m2.daily_rx}枚/日")
        with cols[1]:
            st.markdown("**シナリオC: 面＋門前**")
            if m1g:
                st.metric("① 門前込み", f"{m1g.annual_rx:,}")
                st.caption(f"{m1g.daily_rx}枚/日")
            if m2:
                st.metric("② 商圏人口", f"{m2.annual_rx:,}")
                st.caption("（Bと共通）")
        with cols[2]:
            st.markdown("**シナリオA: 門前のみ**")
            if m1g:
                st.metric("① 門前込み", f"{m1g.annual_rx:,}")
                st.caption(f"{m1g.daily_rx}枚/日")
            else:
                st.info("非実行")
        with cols[3]:
            st.markdown("**門前誘致の付加価値**")
            if m1g and m1a:
                gate_add = m1g.annual_rx - m1a.annual_rx
                st.metric(
                    "🚪 増枚数（方法①）",
                    f"+{gate_add:,} 枚/年" if gate_add >= 0 else f"{gate_add:,} 枚/年",
                )
                st.caption(f"門前込み({m1g.annual_rx:,}) − 面のみ({m1a.annual_rx:,})")
            else:
                st.info("比較不可（面のみ or 門前のみ選択）")


def _render_new_pharmacy_prediction_tabs(result: NewPharmacyResult) -> None:
    """新規開局モード専用の予測ロジックタブ（v2.5/v2.6: 3フィールド対応）"""
    import pandas as pd
    sc = result.config.scenario
    m1g = result.method1_gate
    m1a = result.method1_area
    m2  = result.method2

    tab_labels = []
    if m1a:
        tab_labels.append("① 近隣施設アプローチ（面のみ）")
    if m1g:
        tab_labels.append("① 近隣施設アプローチ（門前込み）")
    if m2:
        tab_labels.append("② 商圏人口動態アプローチ")
    tab_labels.append("📚 データソース")

    tabs = st.tabs(tab_labels)
    idx = 0

    if m1a:
        with tabs[idx]:
            st.metric("年間推計処方箋枚数（方法①: 既存近隣のみ）", f"{m1a.annual_rx:,} 枚/年")
            st.caption(f"レンジ: {m1a.min_val:,}〜{m1a.max_val:,}枚/年 | {m1a.daily_rx}枚/日")
            st.caption("OSMで検索された既存の近隣医療施設からの流入のみで推計")
            if m1a.breakdown:
                st.markdown("#### 施設別 処方箋流入内訳")
                st.dataframe(pd.DataFrame(m1a.breakdown), use_container_width=True, hide_index=True)
            st.markdown("#### 推計ロジック")
            for line in m1a.methodology:
                st.markdown(line)
        idx += 1

    if m1g:
        with tabs[idx]:
            st.metric("年間推計処方箋枚数（方法①: 誘致クリニック込み）", f"{m1g.annual_rx:,} 枚/年")
            st.caption(f"レンジ: {m1g.min_val:,}〜{m1g.max_val:,}枚/年 | {m1g.daily_rx}枚/日")
            gate_add_note = ""
            if m1a:
                gate_add = m1g.annual_rx - m1a.annual_rx
                gate_add_note = f"門前クリニック誘致の付加価値: **+{gate_add:,}枚/年**（= {m1g.annual_rx:,} − {m1a.annual_rx:,}）"
                st.info(f"🚪 {gate_add_note}")
            else:
                st.caption(f"誘致クリニック: {result.config.gate_specialty} ({result.config.gate_daily_outpatients}人/日)")
            if m1g.breakdown:
                st.markdown("#### 施設別 処方箋流入内訳（誘致クリニック含む）")
                st.dataframe(pd.DataFrame(m1g.breakdown), use_container_width=True, hide_index=True)
            st.markdown("#### 推計ロジック")
            for line in m1g.methodology:
                st.markdown(line)
        idx += 1

    if m2:
        with tabs[idx]:
            st.metric("年間推計処方箋枚数（方法②）", f"{m2.annual_rx:,} 枚/年")
            st.caption(f"レンジ: {m2.min_val:,}〜{m2.max_val:,}枚/年 | {m2.daily_rx}枚/日")
            if m2.breakdown:
                st.markdown("#### 年齢層別 処方箋数内訳")
                st.dataframe(pd.DataFrame(m2.breakdown), use_container_width=True, hide_index=True)
            st.markdown("#### 推計ロジック")
            for line in m2.methodology:
                if line:
                    st.markdown(line)
        idx += 1

    with tabs[idx]:
        render_data_sources_panel()


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
# v4.1: モデル校正タブ UI
# ---------------------------------------------------------------------------

def _render_local_calibration_section(new_result: "NewPharmacyResult") -> None:
    """
    v4.4: 新規開局予測モード内のローカルMHLW校正セクション

    同一エリアの既存MHLW薬局でM1・M2を検証し、エリア特性に応じた
    ローカル補正値を表示する。既存CalibrationEngine（都道府県レベル）とは別に、
    地域密着の精度向上を実現する。
    """
    with st.expander(
        "🔬 **ローカルMHLW校正（v4.4）: 同一エリア既存薬局でM1・M2を検証**",
        expanded=False,
    ):
        st.markdown(
            "#### 🔬 ローカルMHLW校正とは\n\n"
            f"**対象エリア**: `{extract_area_keyword(new_result.config.address)}`\n\n"
            "同じエリア（市区町村）の既存MHLW登録薬局を収集し、同じM1・M2ロジックで\n"
            "予測→実績と比較することで、**このエリア特有の補正係数**を導出します。\n\n"
            "| ステップ | 内容 |\n"
            "|---|---|\n"
            "| 1. 収集 | MHLW から同一エリアの薬局（処方箋実績あり）を最大N件収集 |\n"
            "| 2. 予測 | 各薬局を住所のみで M1・M2 予測（現在選択中の薬局タイプを適用）|\n"
            "| 3. 検証 | 実績との誤差から ローカルα1, α2, w* を計算 |\n"
            "| 4. 適用 | 新規開局予測にローカル補正を掛け合わせた推定値を表示 |"
        )

        cal_kw_label = extract_area_keyword(new_result.config.address)

        # 設定
        col_n, col_min = st.columns(2)
        with col_n:
            lc_n = st.slider(
                "収集薬局数（多いほど精度↑、時間↑）",
                min_value=3, max_value=15, value=5, step=1,
                key="lc_n",
                help=f"1件あたり約5〜8秒。{5}件で約{5*7//60+1}分。",
            )
        with col_min:
            lc_min_rx = st.number_input(
                "最低処方箋枚数フィルタ",
                min_value=0, max_value=50_000, value=1_000, step=500,
                key="lc_min_rx",
            )

        est_sec = lc_n * 8
        est_min = est_sec // 60
        st.info(
            f"⏱ 推定実行時間: 約 **{est_min}分{est_sec%60}秒**（{lc_n}件 × 約8秒/件）\n"
            f"エリアキーワード: **{cal_kw_label}** / 薬局タイプ: **{new_result.config.pharmacy_type}**"
        )

        col_run, col_clear = st.columns([3, 1])
        with col_run:
            lc_run = st.button(
                f"🔬 {cal_kw_label}エリアのローカル校正を実行",
                type="primary", use_container_width=True, key="lc_run",
            )
        with col_clear:
            if st.button("🗑 リセット", use_container_width=True, key="lc_clear"):
                for k in ["lc_points", "lc_stats", "lc_area_kw"]:
                    st.session_state.pop(k, None)
                st.rerun()

        if lc_run:
            pref_code = ""
            for pn, pc in PREFECTURE_CODES.items():
                if pn in new_result.config.address:
                    pref_code = pc
                    break

            engine = LocalCalibrationEngine()
            progress = st.progress(0, text="ローカル校正を開始…")
            log_ph = st.empty()

            def lc_progress(pct: int, msg: str) -> None:
                progress.progress(pct, text=msg)
                log_ph.caption(msg)

            # Step 1: 収集
            cal_set, area_kw = engine.search_local_set(
                address=new_result.config.address,
                pref_code=pref_code,
                max_pharmacies=lc_n,
                min_rx=lc_min_rx,
                progress_cb=lc_progress,
            )
            if not cal_set:
                progress.empty()
                log_ph.empty()
                st.warning(
                    f"⚠ `{area_kw}` エリアでMHLW実績データが収集できませんでした。\n"
                    "エリアの規模が小さい場合は「🔬 モデル校正タブ」で都道府県レベルの"
                    "校正をお試しください。"
                )
            else:
                st.info(f"📦 ローカル校正セット: {len(cal_set)}件収集（エリア: {area_kw}）")

                # Step 2: バッチ予測
                points = engine.run_local_batch(
                    cal_set,
                    pharmacy_type=new_result.config.pharmacy_type,
                    progress_cb=lc_progress,
                )
                st.session_state["lc_points"] = points
                st.session_state["lc_area_kw"] = area_kw

                # Step 3: 統計
                stats = LocalCalibrationEngine.calc_local_stats(points)
                st.session_state["lc_stats"] = stats

                progress.progress(100, text="ローカル校正完了！")
                progress.empty()
                log_ph.empty()
                if stats:
                    st.success(
                        f"✅ ローカル校正完了 — エリア: {area_kw} / "
                        f"有効サンプル: {stats.n}件 / 最適MAPE: {stats.mape_optimal:.1f}%"
                    )
                else:
                    st.warning("⚠ 有効サンプルが3件未満で統計計算できません。収集件数を増やしてください。")
                st.rerun()

        # 結果表示
        lc_points: List[CalibrationPoint] = st.session_state.get("lc_points", [])
        lc_stats: Optional[CalibrationStats] = st.session_state.get("lc_stats")
        lc_area_kw: str = st.session_state.get("lc_area_kw", "")

        if not lc_points:
            st.caption("▲ 「ローカル校正を実行」をクリックすると結果が表示されます。")
            return

        st.markdown("---")
        st.markdown(f"#### 📊 ローカル校正結果（エリア: **{lc_area_kw}** / {len(lc_points)}件）")

        if lc_stats:
            # 精度指標
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("M1 MAPE（エリア）", f"{lc_stats.mape_m1:.1f}%",
                      help="このエリアでのM1の平均絶対誤差率")
            c2.metric("M2 MAPE（エリア）", f"{lc_stats.mape_m2:.1f}%",
                      help="このエリアでのM2の平均絶対誤差率")
            c3.metric("最適ブレンド MAPE", f"{lc_stats.mape_optimal:.1f}%",
                      delta=f"-{lc_stats.mape_m1-lc_stats.mape_optimal:.1f}pt vs M1",
                      delta_color="inverse")
            c4.metric("最適M1比重 w*", f"{lc_stats.optimal_m1_weight:.0%}",
                      help=f"推奨: {lc_stats.optimal_m1_weight:.0%}×M1 + {1-lc_stats.optimal_m1_weight:.0%}×M2")

            # バイアス表示
            b1, b2 = st.columns(2)
            bias_m1_pct = (math.exp(lc_stats.bias_m1) - 1) * 100
            bias_m2_pct = (math.exp(lc_stats.bias_m2) - 1) * 100
            b1.metric(
                f"M1バイアス（{lc_area_kw}）",
                f"{bias_m1_pct:+.1f}%",
                help="正 = 過大推計, 負 = 過少推計",
                delta_color="off",
            )
            b2.metric(
                f"M2バイアス（{lc_area_kw}）",
                f"{bias_m2_pct:+.1f}%",
                delta_color="off",
            )

            # ローカル補正値の適用
            m1_res = new_result.method1_area
            m2_res = new_result.method2
            if m1_res and m2_res:
                st.markdown("---")
                st.markdown("##### 🎯 ローカル補正を適用した推計値")
                lc_rx, lc_note = LocalCalibrationEngine.apply_local_correction(
                    m1_res.annual_rx, m2_res.annual_rx,
                    new_result.area_density, lc_stats,
                )
                st.metric(
                    f"🎯 ローカル校正済み推計 ({lc_area_kw})",
                    f"{lc_rx:,} 枚/年",
                    help=f"サンプル数: {lc_stats.n}件 / MAPE: {lc_stats.mape_optimal:.1f}%",
                )
                with st.expander("補正パラメータ詳細"):
                    st.caption(lc_note)
                st.info(
                    f"📌 **解釈**: {lc_area_kw}エリアの既存薬局{lc_stats.n}件で検証した結果、\n"
                    f"M1は平均 **{bias_m1_pct:+.1f}%** {'過大' if bias_m1_pct>0 else '過少'}推計、"
                    f"M2は平均 **{bias_m2_pct:+.1f}%** {'過大' if bias_m2_pct>0 else '過少'}推計の傾向があります。\n"
                    f"ローカル補正後の推計値 **{lc_rx:,}枚/年** を参考値としてご利用ください。\n"
                    f"（精度は±{lc_stats.mape_optimal:.0f}%程度）"
                )
        else:
            st.warning("⚠ 有効サンプルが3件未満のため補正係数を計算できません。")

        # サンプル一覧
        with st.expander("📋 校正サンプル一覧"):
            import pandas as pd
            rows = [
                {
                    "薬局名": p.name[:20],
                    "実績Rx": f"{p.actual_rx:,}",
                    "M1予測": f"{p.m1_rx:,}" if p.m1_rx else "-",
                    "M2予測": f"{p.m2_rx:,}" if p.m2_rx else "-",
                    "M1誤差": f"{p.error_m1*100:+.0f}%" if p.error_m1 is not None else "-",
                    "M2誤差": f"{p.error_m2*100:+.0f}%" if p.error_m2 is not None else "-",
                    "人口密度": f"{p.area_density:,}",
                }
                for p in lc_points
            ]
            if rows:
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_calibration_tab() -> None:
    """🔬 モデル校正タブ（v4.1新機能）"""
    st.markdown(
        "### 🔬 データドリブン モデル校正\n\n"
        "MHLWに処方箋枚数が記録されている薬局を自動収集し、"
        "**住所のみの情報**で方法①・②の予測を実施。\n"
        "実績値との比較から**最も精度の高い補正係数と混合比率**を自動導出します。\n\n"
        "| ステップ | 内容 |\n"
        "|---|---|\n"
        "| 1. 校正セット収集 | MHLWで都道府県を指定 → 処方箋実績のある薬局をN件取得 |\n"
        "| 2. バッチ予測 | 各薬局を住所のみで予測（OSM+MHLW補填+方法①②） |\n"
        "| 3. 統計計算 | MAPE / バイアス / 密度帯別補正係数 α / 最適ブレンド重み w* |\n"
        "| 4. 適用 | 校正パラメータを予測結果に自動適用（既存・新規モード両方） |"
    )
    st.markdown("---")

    # ── 設定パネル ────────────────────────────────────────────────────────
    st.markdown("#### ⚙️ 校正設定")
    col_pref, col_n, col_min = st.columns(3)
    with col_pref:
        cal_pref = st.selectbox(
            "対象都道府県",
            PREFECTURES,
            index=12,  # 東京都
            key="cal_pref",
            help="処方箋枚数を収集する都道府県。都市部（東京・大阪等）は高密度サンプルが取りやすい",
        )
    with col_n:
        cal_n = st.slider(
            "収集する薬局数",
            min_value=5, max_value=50, value=20, step=5,
            key="cal_n",
            help="多いほど精度が上がるが時間がかかる（1件あたり約5〜8秒）",
        )
    with col_min:
        cal_min_rx = st.number_input(
            "最低処方箋枚数（フィルタ）",
            min_value=0, max_value=50_000, value=2_000, step=1_000,
            key="cal_min_rx",
            help="この枚数未満の薬局は校正サンプルから除外（外れ値対策）",
        )

    cal_keyword = st.text_input(
        "検索キーワード（任意）",
        value="薬局",
        key="cal_keyword",
        help="MHLWで検索するキーワード。地名を入れると特定エリアに絞れます（例: 新宿 薬局）",
    )

    est_time = int(cal_n * 7 / 60)
    st.info(
        f"⏱ 推定実行時間: 約 {est_time} 分（{cal_n}件 × 約7秒/件）"
        " ― 実行中は他のタブの操作をお控えください"
    )

    col_run, col_clear = st.columns([3, 1])
    with col_run:
        run_btn = st.button(
            "🚀 校正を実行する",
            type="primary",
            use_container_width=True,
            key="cal_run",
        )
    with col_clear:
        if st.button("🗑 校正データをリセット", use_container_width=True, key="cal_clear"):
            st.session_state["calibration_points"] = []
            st.session_state["calibration_stats"] = None
            st.success("校正データをリセットしました")
            st.rerun()

    # ── 校正実行 ──────────────────────────────────────────────────────────
    if run_btn:
        pref_code = PREFECTURE_CODES.get(cal_pref, "")
        engine = CalibrationEngine()
        progress = st.progress(0, text="校正を開始…")
        log_placeholder = st.empty()

        def progress_cb(pct: int, msg: str) -> None:
            progress.progress(pct, text=msg)
            log_placeholder.caption(msg)

        # Step 1: 校正セット収集
        cal_set = engine.search_calibration_set(
            pref_code=pref_code,
            keyword=cal_keyword,
            max_pharmacies=cal_n,
            min_rx=cal_min_rx,
            progress_cb=progress_cb,
        )
        if not cal_set:
            progress.empty()
            log_placeholder.empty()
            st.error("❌ 校正用データを収集できませんでした。キーワードや都道府県を変えて再試行してください。")
        else:
            st.info(f"📦 校正セット: {len(cal_set)}件の薬局を収集しました")

            # Step 2: バッチ予測
            points = engine.run_batch(cal_set, progress_cb=progress_cb)
            st.session_state["calibration_points"] = points

            # Step 3: 統計計算
            stats = CalibrationEngine.calc_stats(points)
            st.session_state["calibration_stats"] = stats

            progress.progress(100, text="校正完了！")
            progress.empty()
            log_placeholder.empty()
            if stats:
                st.success(f"✅ 校正完了 — 最適MAPE={stats.mape_optimal:.1f}% / 有効サンプル={stats.n}件")
            else:
                st.warning("⚠ 有効サンプルが少なく、統計計算を実行できませんでした（3件以上必要）")
            st.rerun()

    # ── 結果表示 ──────────────────────────────────────────────────────────
    points: List[CalibrationPoint] = st.session_state.get("calibration_points", [])
    stats: Optional[CalibrationStats] = st.session_state.get("calibration_stats")

    if not points:
        st.markdown("---")
        st.markdown("#### 📋 校正結果はまだありません")
        st.markdown(
            "上記の設定で「🚀 校正を実行する」をクリックすると、"
            "MHLWから実績データを収集して予測精度を自動最適化します。"
        )
        return

    st.markdown("---")
    st.markdown(f"#### 📊 校正結果（{len(points)}件収集済み）")

    # 統計サマリー
    if stats:
        st.markdown("##### 🎯 精度指標")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("方法① MAPE", f"{stats.mape_m1:.1f}%",
                  help="Mean Absolute Percentage Error（平均絶対誤差率）")
        c2.metric("方法② MAPE", f"{stats.mape_m2:.1f}%")
        c3.metric("最適ブレンド MAPE", f"{stats.mape_optimal:.1f}%",
                  delta=f"-{stats.mape_m1 - stats.mape_optimal:.1f}pt vs M1",
                  delta_color="inverse")
        c4.metric("最適M1重み w*", f"{stats.optimal_m1_weight:.0%}",
                  help=f"最終予測 = {stats.optimal_m1_weight:.0%}×M1 + {1-stats.optimal_m1_weight:.0%}×M2")

        st.markdown("##### 📐 バイアス（推計値/実績値 の幾何平均）")
        b1, b2 = st.columns(2)
        bias_m1_pct = (math.exp(stats.bias_m1) - 1) * 100
        bias_m2_pct = (math.exp(stats.bias_m2) - 1) * 100
        b1.metric(
            "方法① バイアス",
            f"{bias_m1_pct:+.1f}%",
            help="正 = 過大推計, 負 = 過少推計",
            delta_color="off",
        )
        b2.metric("方法② バイアス", f"{bias_m2_pct:+.1f}%", delta_color="off")

        # 密度帯別補正係数
        if stats.alpha_m1 or stats.alpha_m2:
            st.markdown("##### 🗂 密度帯別補正係数 α（actual / predicted の幾何平均）")
            all_bands = sorted(set(list(stats.alpha_m1.keys()) + list(stats.alpha_m2.keys())))
            alpha_rows = []
            for band in all_bands:
                a1, n1 = stats.alpha_m1.get(band, (1.0, 0))
                a2, n2 = stats.alpha_m2.get(band, (1.0, 0))
                alpha_rows.append({
                    "密度帯": band,
                    "M1 補正係数 α1": f"{a1:.3f} (n={n1})",
                    "M2 補正係数 α2": f"{a2:.3f} (n={n2})",
                    "α1の意味": f"M1予測値を{a1:.1f}倍して実績に近づける",
                })
            import pandas as pd
            st.dataframe(pd.DataFrame(alpha_rows), use_container_width=True, hide_index=True)
            st.caption(
                "α>1.0: 予測が実績より低い（過少推計）→ 予測値を引き上げる方向に補正\n"
                "α<1.0: 予測が実績より高い（過大推計）→ 予測値を引き下げる方向に補正"
            )

        # 適用ボタン
        st.markdown("---")
        col_apply, col_info = st.columns([1, 2])
        with col_apply:
            if st.button(
                "✅ この校正パラメータを予測に適用",
                type="primary",
                use_container_width=True,
                key="cal_apply",
            ):
                st.session_state["calibration_stats"] = stats
                st.success("校正パラメータを適用しました。既存薬局分析・新規開局予測タブで確認できます。")
                st.rerun()
        with col_info:
            st.markdown(
                "**適用後の効果**:  \n"
                "既存薬局分析の「📊 予測値 vs 厚労省実績値」に"
                "**🎯 校正済み予測**列が追加されます。"
            )

    else:
        st.warning("⚠ 有効なサンプルが3件未満のため統計計算ができません。収集件数を増やしてください。")

    # 校正サンプル一覧テーブル
    st.markdown("---")
    st.markdown("##### 📋 校正サンプル一覧")
    rows = []
    for p in points:
        rows.append({
            "薬局名": p.name[:25],
            "住所": p.address[:30],
            "実績Rx": f"{p.actual_rx:,}",
            "M1予測": f"{p.m1_rx:,}" if p.m1_rx else "-",
            "M2予測": f"{p.m2_rx:,}" if p.m2_rx else "-",
            "M1誤差": f"{p.error_m1*100:+.0f}%" if p.error_m1 is not None else "-",
            "M2誤差": f"{p.error_m2*100:+.0f}%" if p.error_m2 is not None else "-",
            "密度帯": p.density_band,
            "医療機関数": p.n_medical,
            "競合薬局数": p.n_pharmacies,
        })
    if rows:
        import pandas as pd
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # CSV出力
        csv_data = CalibrationEngine.points_to_csv(points)
        st.download_button(
            "📥 校正データをCSVでダウンロード",
            data=csv_data,
            file_name=f"calibration_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
        )

    # 個別ログ確認
    with st.expander("🔍 個別予測ログ（エラー調査用）"):
        for p in points:
            if p.error_log:
                st.markdown(f"**{p.name}** ({p.address[:30]})")
                st.code("\n".join(p.error_log))
                st.markdown("---")


# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="薬局 処方箋枚数 多面的予測 v4.4",
        page_icon="💊",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.title("💊 薬局 年間処方箋枚数 多面的予測ツール v4.4")

    cal_stats: Optional[CalibrationStats] = st.session_state.get("calibration_stats")
    if cal_stats:
        st.success(
            f"✅ **校正済みモデル適用中** "
            f"（{cal_stats.calibrated_at} 校正 / n={cal_stats.n} / "
            f"最適MAPE={cal_stats.mape_optimal:.1f}% / "
            f"M1重み={cal_stats.optimal_m1_weight:.0%}）"
        )
    else:
        st.info(
            "📌 **v4.4**: 🔬 モデル校正タブ（都道府県レベル）または予測結果内の"
            "「ローカルMHLW校正」（同一エリアレベル）でMHLW実績データを使った"
            "精度自動最適化ができます。"
        )

    for k, v in [
        ("candidates", []), ("analysis", None), ("new_result", None),
        ("manual_facility_params", []),
        ("clinic_candidates_v4", []),
        ("mhlw_supplement", []),
        ("mhlw_supplement_log", []),
        ("calibration_points", []),       # v4.1: 校正サンプルリスト
        ("calibration_stats", None),      # v4.1: 校正統計
        ("lc_points", []),                # v4.4: ローカル校正サンプルリスト
        ("lc_stats", None),               # v4.4: ローカル校正統計
        ("lc_area_kw", ""),               # v4.4: ローカル校正エリアキーワード
    ]:
        if k not in st.session_state:
            st.session_state[k] = v

    tab_existing, tab_new, tab_cal = st.tabs([
        "🏪 既存薬局を分析",
        "🏗 新規開局を予測",
        "🔬 モデル校正",
    ])

    # ================================================================
    # TAB A: 既存薬局分析モード
    # ================================================================
    with tab_existing:
        _render_existing_mode()

    # ================================================================
    # TAB B: 新規開局予測モード
    # ================================================================
    with tab_new:
        _render_new_pharmacy_mode()

    # ================================================================
    # TAB C: モデル校正（v4.1 新機能）
    # ================================================================
    with tab_cal:
        _render_calibration_tab()


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

        if st.button("🚀 多面的分析を実行", type="primary", use_container_width=True, key="ex_run"):
            run_analysis(sel)

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
        # v2.4: 乖離警告 + 手動施設追加 + 再計算セクション
        render_gap_and_manual_input(analysis)
        st.markdown("---")
        tab_map, tab_preds, tab_mhlw, tab_log = st.tabs([
            "🗺 競合マップ", "📊 予測ロジック", "🏥 厚労省データ", "🔍 検索ログ"
        ])
        with tab_map:
            if analysis.pharmacy_lat and analysis.pharmacy_lon:
                st.markdown(
                    "**凡例**: 🔴 分析対象薬局　🔵 病院　🔷 クリニック（OSM）"
                    "　🟣 MHLW自動補填　🟠 手動追加施設　🟢 競合薬局"
                    f"　（商圏円: 半径{analysis.commercial_radius}m）"
                )
                # v2.6: OSM + MHLW補填 + 手動追加をすべてマップに含める
                _mhlw_facs   = st.session_state.get("mhlw_supplement", [])
                _manual_params = st.session_state.get("manual_facility_params", [])
                _manual_for_map = [
                    make_manual_facility(
                        analysis.pharmacy_lat, analysis.pharmacy_lon,
                        p["name"], p["specialty"], p["daily_outpatients"],
                        p["distance_m"], p["has_inhouse"],
                    )
                    for p in _manual_params
                ]
                _all_medical = analysis.nearby_medical + _mhlw_facs + _manual_for_map
                m = build_competitor_map(
                    analysis.pharmacy_name,
                    analysis.pharmacy_lat, analysis.pharmacy_lon,
                    _all_medical,
                    analysis.nearby_pharmacies,
                    analysis.commercial_radius, analysis.geocoder_source,
                )
                st_folium(m, width=None, height=520, use_container_width=True)
            else:
                st.warning("座標取得失敗のためマップを表示できません")
            # v2.6: 補填後の全施設をテーブルに表示
            _mhlw_facs_t   = st.session_state.get("mhlw_supplement", [])
            _manual_params_t = st.session_state.get("manual_facility_params", [])
            _manual_for_table = [
                make_manual_facility(
                    analysis.pharmacy_lat or 0.0, analysis.pharmacy_lon or 0.0,
                    p["name"], p["specialty"], p["daily_outpatients"],
                    p["distance_m"], p["has_inhouse"],
                )
                for p in _manual_params_t
            ] if analysis.pharmacy_lat else []
            render_competitor_table(
                analysis.nearby_medical + _mhlw_facs_t + _manual_for_table,
                analysis.nearby_pharmacies,
            )
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
        "| シナリオ | 内容 | 使用手法 |\n"
        "|---|---|---|\n"
        "| 🌐 **B（デフォルト）** | 面での集客（スーパー敷地内など） | 方法①既存近隣＋方法② |\n"
        "| 🏥 **C（推奨）** | 面＋門前クリニック誘致 | 方法①門前込み＋方法②＋付加価値算出 |\n"
        "| 🚪 **A** | 門前クリニック誘致のみ | 方法①のみ |\n"
        "| 🔄 **全比較** | 全シナリオを横並び比較 | 上記すべて |"
    )
    st.markdown("---")
    st.markdown("#### STEP 1 — 開局予定地の住所と薬局タイプを入力")
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

    # v4.3: 薬局タイプ選択 ─────────────────────────────────────────────────
    st.markdown("**薬局タイプを選択（商圏半径に影響します）**")
    pharmacy_type = st.radio(
        "薬局タイプ",
        options=PHARMACY_TYPES,
        index=0,
        key="new_ph_type",
        format_func=lambda x: {
            PHARMACY_TYPE_NORMAL:      "🏪 通常の調剤薬局（単独・門前なし）  — 密度帯別歩行商圏",
            PHARMACY_TYPE_SUPERMARKET: "🛒 スーパー・ドラッグストア内薬局  — 店舗の広域商圏を使用",
            PHARMACY_TYPE_GATE:        "🏥 門前薬局（医療機関に隣接・専属）— 300m固定",
        }[x],
        help=(
            "スーパー・ドラッグストア内薬局を選ぶと、食品スーパーの一次商圏（800〜3,000m）を"
            "商圏半径として使用します。近隣に医療機関があっても商圏は縮小しません。"
        ),
    )

    if address:
        dens, dens_src = get_population_density(address)
        r_init, r_reason = calc_commercial_radius(dens, False, "", pharmacy_type=pharmacy_type)
        est_pop = int(math.pi * (r_init / 1000) ** 2 * dens)
        ph_type_label = {
            PHARMACY_TYPE_NORMAL:      "通常薬局",
            PHARMACY_TYPE_SUPERMARKET: "スーパー内薬局",
            PHARMACY_TYPE_GATE:        "門前薬局",
        }.get(pharmacy_type, pharmacy_type)
        st.info(
            f"📐 **{ph_type_label}の商圏試算**: 人口密度 {dens:,}人/km²（{dens_src}）"
            f" → 商圏半径 **{r_init}m** → 推計商圏人口 **{est_pop:,}人**"
        )

    st.markdown("---")
    st.markdown("#### STEP 2 — 開局シナリオを選択")
    scenario = st.radio(
        "予測シナリオ",
        options=["area_dual", "combined", "gate_only", "all"],
        format_func=lambda x: {
            "area_dual": "🌐 シナリオB: 面での集客（方法①既存近隣＋方法②）  ← デフォルト",
            "combined":  "🏥 シナリオC: 面＋門前クリニック誘致（方法①門前込み＋方法②）  ← NEW",
            "gate_only": "🚪 シナリオA: 門前クリニック誘致のみ（方法①のみ）",
            "all":       "🔄 全シナリオ比較（B・C・Aを横並び表示）",
        }[x],
        key="new_scenario",
    )

    gate_specialty, gate_daily, gate_inhouse = "一般内科", 50, False
    if scenario in ("combined", "gate_only", "all"):
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
    st.markdown("#### STEP 3 — 分析オプション（自動実行）")
    st.info(
        "🟣 **MHLW自動補填**: 分析実行時に厚労省DBから未収録医療機関を自動補填します（約30秒〜2分）。"
        "OSMで見つからないクリニックが自動的に方法①の推計に反映されます。\n\n"
        "📋 **競合薬局処方箋取得 (v3.2)**: 近隣競合薬局（上位10件）の実際の処方箋枚数をMHLWから"
        "自動取得してマップ・テーブルに表示します（取得に1〜3分かかる場合があります）。"
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
            scenario=scenario,            # "area_dual"|"combined"|"gate_only"|"all"
            pharmacy_type=pharmacy_type,  # v4.3: 薬局タイプ
            gate_specialty=gate_specialty,
            gate_daily_outpatients=gate_daily,
            gate_has_inhouse=gate_inhouse,
            fetch_nearby_rx=True,         # v3.2: 常時自動実行
            fetch_mhlw_supplement=True,   # v3.1: 常時自動実行
        )
        run_new_pharmacy_analysis(config)

    # 結果表示
    new_result: Optional[NewPharmacyResult] = st.session_state.get("new_result")
    if new_result:
        st.markdown("---")
        st.markdown(f"## 結果: `{new_result.config.pharmacy_name}`")
        st.caption(f"開局予定住所: {new_result.config.address}")

        # v4.3: 薬局タイプバッジを表示
        ph_type_disp = new_result.config.pharmacy_type
        ph_type_icon = {
            PHARMACY_TYPE_NORMAL:      "🏪",
            PHARMACY_TYPE_SUPERMARKET: "🛒",
            PHARMACY_TYPE_GATE:        "🏥",
        }.get(ph_type_disp, "🏪")
        st.caption(f"{ph_type_icon} 薬局タイプ: **{ph_type_disp}**")

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

        # ----------------------------------------------------------------
        # v4.4: ローカルMHLW校正セクション
        # ----------------------------------------------------------------
        _render_local_calibration_section(new_result)
        st.markdown("---")

        tab_map, tab_preds, tab_log = st.tabs(["🗺 競合環境マップ", "📊 予測ロジック詳細", "🔍 分析ログ"])
        with tab_map:
            if new_result.lat and new_result.lon:
                has_rx_data = any(p.mhlw_annual_outpatients for p in new_result.nearby_pharmacies)
                sc_label = new_result.config.scenario
                show_virtual = sc_label in ("combined", "gate_only", "all")
                st.markdown(
                    "**凡例**: 🔴⭐ 開局予定地　"
                    + ("🟠 誘致予定クリニック（門前シナリオ）　" if show_virtual else "")
                    + "🔵 病院　🔷 クリニック　🟢 競合薬局"
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
            _render_new_pharmacy_prediction_tabs(new_result)
        with tab_log:
            st.code("\n".join(new_result.search_log))


# ---------------------------------------------------------------------------
# 11. 分析実行関数
# ---------------------------------------------------------------------------

def run_analysis(candidate: PharmacyCandidate, try_mhlw_medical: bool = False) -> None:
    """既存薬局 フル分析（v3.1: MHLW補填を自動実行）"""
    log: List[str] = []
    progress = st.progress(0, text="分析開始…")
    # セッション状態を初期化（前回分をクリア）
    st.session_state["mhlw_supplement"] = []
    st.session_state["mhlw_supplement_log"] = []

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
    # v4.3: pharmacy_type を考慮して初期商圏半径を算出（SM業態は広域検索）
    ph_type_early = getattr(candidate, "pharmacy_type", PHARMACY_TYPE_NORMAL)
    initial_r, _ = calc_commercial_radius(area_density, False, "", pharmacy_type=ph_type_early)
    search_r = max(int(initial_r * 1.5), 600)
    log.append(f"[密度] {area_density:,}人/km²（{density_source}）")

    # C: ジオコーディング（GSI優先）
    progress.progress(30, text="[3/6] 座標を取得中（国土地理院）…")
    gc = GeocoderService()
    lat, lon, geo_msg, geo_src = gc.geocode(pharmacy_address)
    log.append(f"[Geocoding({geo_src})] {geo_msg}")

    # D: Overpass（OSM）
    nearby_medical, nearby_pharmacies = [], []
    if lat and lon:
        progress.progress(40, text=f"[4/7] 近隣施設を検索中（半径{search_r}m）…")
        time.sleep(0.5)
        ov = OverpassSearcher()
        nearby_medical, nearby_pharmacies, ov_msg = ov.search_nearby(lat, lon, search_r)
        log.append(f"[OSM] 半径{search_r}m → {ov_msg}")

        # D.4: 競合薬局の処方箋枚数を自動取得（v3.2: 常時実行、上位10件）
        if nearby_pharmacies:
            progress.progress(47, text=f"[4.3/7] 競合薬局（{len(nearby_pharmacies[:10])}件）の処方箋枚数をMHLWから取得中…")
            rx_scraper = MHLWScraper()
            rx_scraper.initialize_session()
            rx_data = rx_scraper.get_rx_for_nearby_pharmacies(
                [p.name for p in nearby_pharmacies], limit=10
            )
            fetched_rx = 0
            for ph in nearby_pharmacies:
                if ph.name in rx_data and rx_data[ph.name]:
                    ph.mhlw_annual_outpatients = rx_data[ph.name]
                    log.append(f"  [競合薬局MHLW] {ph.name}: {rx_data[ph.name]:,}枚/年")
                    fetched_rx += 1
            log.append(f"[競合薬局MHLW] {fetched_rx}/{len(nearby_pharmacies[:10])}件で処方箋枚数取得")

        # D.5: MHLW自動補填（v3.1: 常に実行）
        progress.progress(52, text="[4.5/7] MHLWから未収録医療機関を自動補填中…")
        pref_code_auto = ""
        for pref_name, pc in PREFECTURE_CODES.items():
            if pref_name in pharmacy_address:
                pref_code_auto = pc
                break
        mhlw_new_facs, mhlw_sup_log = fetch_mhlw_medical_supplement(
            pharmacy_lat=lat,
            pharmacy_lon=lon,
            pharmacy_address=pharmacy_address,
            pref_code=pref_code_auto,
            existing_osm=nearby_medical,
            search_radius_m=search_r,
        )
        log.extend(mhlw_sup_log)
        if mhlw_new_facs:
            st.session_state["mhlw_supplement"] = mhlw_new_facs
            st.session_state["mhlw_supplement_log"] = mhlw_sup_log
            log.append(f"[MHLW補填] {len(mhlw_new_facs)}件を自動補填（OSM未収録）")
        else:
            log.append("[MHLW補填] 新規施設なし（OSMと重複 or 該当なし）")

        # D.6: MHLW外来患者数データ照会（オプション: try_mhlw_medical=True 時）
        if try_mhlw_medical and nearby_medical:
            progress.progress(60, text="[4.6/7] 医療施設のMHLWデータ照会中…")
            for fac in nearby_medical[:5]:
                aop = scraper.get_medical_outpatient_data(fac.name)
                if aop:
                    fac.mhlw_annual_outpatients = aop
                    fac.daily_outpatients = aop // NATIONAL_STATS["working_days"]
                time.sleep(0.5)

    # E: 門前判定・商圏半径確定
    # v4.3: pharmacy_type を渡して SM 業態では広域商圏を使う
    progress.progress(65, text="[5/6] 門前判定・商圏半径確定…")
    is_gate, gate_reason = detect_gate_pharmacy(candidate.name, nearby_medical)
    ph_type = getattr(candidate, "pharmacy_type", PHARMACY_TYPE_NORMAL)
    commercial_r, r_reason = calc_commercial_radius(
        area_density, is_gate, gate_reason, pharmacy_type=ph_type
    )
    log.append(f"[門前] {is_gate} ({gate_reason}) → 半径{commercial_r}m [{ph_type}]")

    # F: 予測（v3.1: OSM + MHLW補填 + 手動追加を全て含めて初回計算）
    progress.progress(75, text="[6/7] 方法①②を計算中…")

    # MHLW補填施設（D.5で取得済み）
    mhlw_facs_for_pred: List[NearbyFacility] = st.session_state.get("mhlw_supplement", [])

    # 手動追加済みの施設
    pre_added_params: List[Dict] = st.session_state.get("manual_facility_params", [])
    manual_facs: List[NearbyFacility] = [
        make_manual_facility(
            lat or 0.0, lon or 0.0,
            p["name"], p["specialty"], p["daily_outpatients"],
            p["distance_m"], p["has_inhouse"],
        )
        for p in pre_added_params
    ] if (lat and lon) else []
    if manual_facs:
        log.append(f"[手動追加] {len(manual_facs)}件を方法①に追加")

    # OSM + MHLW補填 + 手動追加 を統合
    merged_medical = nearby_medical + mhlw_facs_for_pred + manual_facs
    if mhlw_facs_for_pred:
        log.append(f"[統合] OSM {len(nearby_medical)}件 + MHLW補填 {len(mhlw_facs_for_pred)}件"
                   + (f" + 手動 {len(manual_facs)}件" if manual_facs else "")
                   + f" = 合計 {len(merged_medical)}件")

    # 医療機関密集補正: デフォルト外来患者数を持つ施設が多い場合、患者分散を反映
    merged_medical = apply_clinic_congestion_factor(merged_medical, log)

    extra_label = []
    if mhlw_facs_for_pred: extra_label.append(f"MHLW補填{len(mhlw_facs_for_pred)}件含む")
    if manual_facs:        extra_label.append(f"手動追加{len(manual_facs)}件含む")
    m1_label = "方法①: 近隣医療機関アプローチ" + (f"（{', '.join(extra_label)}）" if extra_label else "")
    m1 = Method1Predictor().predict(
        lat or 0.0, lon or 0.0, merged_medical, nearby_pharmacies,
        mode_label=m1_label,
    ) if lat else None
    m2 = Method2Predictor().predict(
        lat or 0.0, lon or 0.0, nearby_pharmacies, area_density, commercial_r,
        density_source=density_source, radius_reason=r_reason,
        nearby_medical=merged_medical,  # v3.2: 門前的競合判定用
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
        nearby_medical=nearby_medical,  # OSM のみ（手動追加は別管理）
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
    """新規開局予測 フル分析（v2.5/v2.6: area_dual / combined / gate_only / all）"""
    log: List[str] = []
    progress = st.progress(0, text="新規開局予測を開始…")

    sc = config.scenario  # "area_dual" | "combined" | "gate_only" | "all"
    need_gate  = sc in ("combined", "gate_only", "all")
    need_area  = sc in ("area_dual", "combined", "all")
    need_m2    = sc in ("area_dual", "combined", "all")

    # A: 密度計算
    # v4.3: pharmacy_type を考慮して初期商圏半径を算出（SM業態は広域検索）
    progress.progress(10, text="[1/5] 住所から人口密度を計算中…")
    area_density, density_source = get_population_density(config.address)
    initial_r, _ = calc_commercial_radius(
        area_density, False, "", pharmacy_type=config.pharmacy_type
    )
    search_r = max(int(initial_r * 1.5), 600)
    log.append(f"[密度] {area_density:,}人/km²（{density_source}）")

    # B: ジオコーディング（GSI優先）
    progress.progress(20, text="[2/5] 座標を取得中（国土地理院）…")
    gc = GeocoderService()
    lat, lon, geo_msg, geo_src = gc.geocode(config.address)
    log.append(f"[Geocoding({geo_src})] {geo_msg}")

    # C: Overpass — 近隣施設検索
    nearby_medical: List[NearbyFacility] = []
    nearby_pharmacies: List[NearbyFacility] = []
    if lat and lon:
        progress.progress(40, text=f"[3/5] 近隣施設を検索中（半径{search_r}m）…")
        time.sleep(0.5)
        ov = OverpassSearcher()
        nearby_medical, nearby_pharmacies, ov_msg = ov.search_nearby(lat, lon, search_r)
        log.append(f"[OSM] 半径{search_r}m → {ov_msg}")

        # 競合薬局の処方箋枚数を自動取得（v3.2: 常時実行、上位10件）
        if nearby_pharmacies:
            progress.progress(55, text=f"[3.5/5] 競合薬局（{len(nearby_pharmacies[:10])}件）の処方箋枚数をMHLWから取得中…")
            rx_scraper2 = MHLWScraper()
            rx_scraper2.initialize_session()
            rx_data = rx_scraper2.get_rx_for_nearby_pharmacies(
                [p.name for p in nearby_pharmacies], limit=10
            )
            fetched_rx2 = 0
            for ph in nearby_pharmacies:
                if ph.name in rx_data and rx_data[ph.name]:
                    ph.mhlw_annual_outpatients = rx_data[ph.name]
                    log.append(f"  [競合薬局MHLW] {ph.name}: {rx_data[ph.name]:,}枚/年")
                    fetched_rx2 += 1
            log.append(f"[競合薬局MHLW] {fetched_rx2}/{len(nearby_pharmacies[:10])}件で処方箋枚数取得")

        # v3.1: MHLW医療機関エリア自動補填（常時実行）
        if lat:
            progress.progress(58, text="[3.7/5] MHLWから未収録医療機関を自動補填中…")
            pref_code = ""
            for pref_name, pc in PREFECTURE_CODES.items():
                if pref_name in config.address:
                    pref_code = pc
                    break
            new_facs, sup_log = fetch_mhlw_medical_supplement(
                pharmacy_lat=lat,
                pharmacy_lon=lon,
                pharmacy_address=config.address,
                pref_code=pref_code,
                existing_osm=nearby_medical,
                search_radius_m=search_r,
            )
            log.extend(sup_log)
            if new_facs:
                nearby_medical = nearby_medical + new_facs
                log.append(f"[MHLW補填] {len(new_facs)}件を近隣医療機関に追加")

        # 医療機関密集補正: デフォルト外来患者数を持つ施設が多い場合、患者分散を反映
        nearby_medical = apply_clinic_congestion_factor(nearby_medical, log)

    # D: 商圏半径確定
    progress.progress(65, text="[4/5] 門前判定・商圏半径を確定中…")
    if need_gate and not need_area:
        # 純粋な門前シナリオA: 医療機関依存型のため商圏半径を小さく固定
        is_gate, gate_reason = True, "門前クリニック誘致シナリオを選択"
        commercial_r, r_reason = 300, "門前クリニック誘致 → 医療機関依存型のため300m固定"
    else:
        # 面シナリオ（B/C/all）: v4.3 pharmacy_type を考慮した商圏半径
        is_gate, gate_reason = detect_gate_pharmacy(config.pharmacy_name, nearby_medical)
        commercial_r, r_reason = calc_commercial_radius(
            area_density, is_gate, gate_reason, pharmacy_type=config.pharmacy_type
        )
    log.append(f"[商圏] 半径{commercial_r}m（{r_reason}）")

    # E: 予測計算
    progress.progress(80, text="[5/5] 予測計算中…")
    method1_gate: Optional[PredictionResult] = None
    method1_area: Optional[PredictionResult] = None
    method2:      Optional[PredictionResult] = None

    if lat:
        # -----------------------------------------------------------
        # 方法①（面のみ）: 既存近隣医療施設だけで Method1 を計算
        #   → シナリオB(area_dual) / C(combined) / all
        # -----------------------------------------------------------
        if need_area:
            method1_area = Method1Predictor().predict(
                lat, lon, nearby_medical, nearby_pharmacies,
                mode_label="シナリオB: 面での集客（既存近隣施設）"
            )
            log.append(f"[方法①(面のみ)] 推計: {method1_area.annual_rx:,}枚/年")

        # -----------------------------------------------------------
        # 方法①（門前込み）: 仮想誘致クリニック + 既存近隣で Method1 を計算
        #   → シナリオA(gate_only) / C(combined) / all
        # -----------------------------------------------------------
        if need_gate:
            virtual_clinic = NearbyFacility(
                name=f"[誘致予定] {config.gate_specialty}クリニック",
                facility_type="clinic",
                lat=(lat + 0.000225),   # ~25m 北にオフセット
                lon=lon,
                distance_m=25,
                specialty=config.gate_specialty,
                daily_outpatients=config.gate_daily_outpatients,
                has_inhouse_pharmacy=config.gate_has_inhouse,
            )
            all_medical_with_gate = [virtual_clinic] + nearby_medical
            method1_gate = Method1Predictor().predict(
                lat, lon, all_medical_with_gate, nearby_pharmacies,
                mode_label="シナリオC/A: 門前クリニック誘致込み"
            )
            log.append(f"[方法①(門前込み)] 推計: {method1_gate.annual_rx:,}枚/年")
            if method1_area:
                gate_add = method1_gate.annual_rx - method1_area.annual_rx
                log.append(f"  → 門前誘致の付加価値: +{gate_add:,}枚/年")

    # -----------------------------------------------------------
    # 方法②（商圏人口動態）: シナリオB / C / all
    # -----------------------------------------------------------
    if need_m2:
        method2 = Method2Predictor().predict(
            lat or 0.0, lon or 0.0, nearby_pharmacies,
            area_density, commercial_r,
            density_source=density_source, radius_reason=r_reason,
            nearby_medical=nearby_medical,  # v3.2: 門前的競合判定用
            pharmacy_type=config.pharmacy_type,  # v4.4: SM業態補正を適用
        )
        log.append(f"[方法②(商圏人口)] 推計: {method2.annual_rx:,}枚/年"
                   + (f" ※SM業態補正適用" if config.pharmacy_type == PHARMACY_TYPE_SUPERMARKET else ""))

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
        method1_gate=method1_gate,
        method1_area=method1_area,
        method2=method2,
        search_log=log,
    )
    st.rerun()


if __name__ == "__main__":
    main()
