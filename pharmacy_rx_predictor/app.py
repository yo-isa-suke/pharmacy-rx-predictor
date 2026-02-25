"""
薬局 年間処方箋枚数 予測ツール v2.0
====================================
厚生労働省「薬局機能情報提供制度」ポータルから実データを取得し、
見つからない場合は統計モデルで推計するアプリ。

【v2.0 修正点】
- 正しいAPIエンドポイントに修正（Spring Boot ベース）
- 薬局名のオートコンプリート（候補一覧からの選択）
- セッション管理の修正
- 処方箋データ解析の強化
"""

import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
import streamlit as st
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# 定数・参照データ
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
    "fiscal_year": 2022,
    "total_prescriptions": 885_000_000,
    "total_pharmacies": 61_860,
    "average_per_year": 14_305,
    "median_estimate": 8_000,
    "daily_average": 44,
    "working_days": 305,
    "source": "厚生労働省「調剤医療費（電算処理分）の動向」（2022年度）・日本薬剤師会調査",
    "source_url": "https://www.mhlw.go.jp/topics/medias/med/",
}

# 大手チェーン薬局データ（各社IR・薬局機能情報より推計）
CHAIN_DATA: Dict[str, Dict] = {
    "ウエルシア":        {"annual_est": 45_000, "min": 20_000, "max": 80_000,  "ir": "ウエルシアHD 統合報告書"},
    "ツルハ":            {"annual_est": 30_000, "min": 15_000, "max": 60_000,  "ir": "ツルハHD 有価証券報告書"},
    "マツモトキヨシ":    {"annual_est": 18_000, "min": 8_000,  "max": 35_000,  "ir": "マツキヨコスモス IR資料"},
    "マツキヨ":          {"annual_est": 18_000, "min": 8_000,  "max": 35_000,  "ir": "マツキヨコスモス IR資料"},
    "コスモス薬品":      {"annual_est": 22_000, "min": 10_000, "max": 40_000,  "ir": "コスモス薬品 有価証券報告書"},
    "スギ薬局":          {"annual_est": 40_000, "min": 20_000, "max": 70_000,  "ir": "スギHD IR資料"},
    "カワチ薬品":        {"annual_est": 35_000, "min": 15_000, "max": 60_000,  "ir": "カワチ薬品 有価証券報告書"},
    "クリエイト":        {"annual_est": 25_000, "min": 12_000, "max": 45_000,  "ir": "クリエイトSDHD IR"},
    "サンドラッグ":      {"annual_est": 16_000, "min": 8_000,  "max": 30_000,  "ir": "サンドラッグ 有価証券報告書"},
    "日本調剤":          {"annual_est": 55_000, "min": 25_000, "max": 120_000, "ir": "日本調剤 統合報告書"},
    "クオール":          {"annual_est": 35_000, "min": 15_000, "max": 70_000,  "ir": "クオールHD IR資料"},
    "アイン":            {"annual_est": 50_000, "min": 25_000, "max": 100_000, "ir": "アインHD 有価証券報告書"},
    "アインファーマシーズ":{"annual_est": 50_000,"min": 25_000, "max": 100_000,"ir": "アインHD 有価証券報告書"},
    "ファーマライズ":    {"annual_est": 30_000, "min": 15_000, "max": 55_000,  "ir": "ファーマライズHD IR"},
    "総合メディカル":    {"annual_est": 40_000, "min": 20_000, "max": 80_000,  "ir": "総合メディカル IR資料"},
    "くすりの福太郎":    {"annual_est": 20_000, "min": 8_000,  "max": 40_000,  "ir": "IR・薬局機能情報より推計"},
    "セイムス":          {"annual_est": 15_000, "min": 6_000,  "max": 28_000,  "ir": "富士薬品グループ IR"},
    "ファーマックス":    {"annual_est": 28_000, "min": 12_000, "max": 55_000,  "ir": "薬局機能情報集計より推計"},
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
class SearchResult:
    pharmacy_name: str
    annual_prescriptions: Optional[int] = None
    prescriptions_range: Optional[Tuple[int, int]] = None
    daily_estimate: Optional[int] = None
    data_source: str = "unknown"
    source_label: str = ""
    source_url: str = ""
    confidence: str = "low"
    pharmacy_type: str = ""
    methodology: List[str] = field(default_factory=list)
    references: List[Dict] = field(default_factory=list)
    mhlw_found: bool = False
    mhlw_has_rx_data: bool = False
    web_search_found: bool = False
    search_log: List[str] = field(default_factory=list)
    mhlw_fields: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. 厚生労働省 薬局機能情報提供制度 スクレイパー（修正版）
# ---------------------------------------------------------------------------

class MHLWScraper:
    """
    厚生労働省「医療情報ネット（ナビイ）」ポータルから薬局データを取得する。
    ポータル: https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/

    正しいAPIエンドポイント（調査済み）:
    - セッション初期化: GET /znk-web/juminkanja/S2300/initialize
    - 検索バリデーション: GET /znk-web/juminkanja/S2300/yakkyokuSearch?yakkyokuKeyword=XXX&searchJudgeKbn=2
    - 検索結果一覧: GET /znk-web/juminkanja/S2400/initialize/{keyword}/?sjk=2&page=0&size=20&sortNo=1
    - 薬局詳細: GET /znk-web/juminkanja/S2430/initialize?prefCd=XX&kikanCd=XXXXX&kikanKbn=5
    """

    DOMAIN = "https://www.iryou.teikyouseido.mhlw.go.jp"
    BASE   = DOMAIN + "/znk-web"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/121.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2300/initialize",
        })
        self._initialized = False
        self.csrf_token = ""

    def initialize_session(self) -> Tuple[bool, str]:
        """セッションクッキーとCSRFトークンを取得"""
        try:
            r = self.session.get(
                f"{self.BASE}/juminkanja/S2300/initialize",
                timeout=15,
                allow_redirects=True,
            )
            if r.status_code != 200:
                return False, f"初期化失敗 HTTP {r.status_code}"

            soup = BeautifulSoup(r.text, "html.parser")
            csrf_meta = soup.find("meta", {"name": "_csrf"})
            if csrf_meta:
                self.csrf_token = csrf_meta.get("content", "")

            self._initialized = True
            return True, "OK"

        except requests.Timeout:
            return False, "接続タイムアウト（15秒）"
        except requests.ConnectionError as e:
            return False, f"接続エラー: {e}"
        except Exception as e:
            return False, f"エラー: {type(e).__name__}: {e}"

    def search_candidates(
        self, keyword: str, pref_code: str = "", max_pages: int = 3
    ) -> Tuple[List[PharmacyCandidate], int, str]:
        """
        薬局名キーワードで検索し、候補リストを返す。

        Returns:
            (candidates_list, total_count, status_message)
        """
        if not self._initialized:
            ok, msg = self.initialize_session()
            if not ok:
                return [], 0, f"セッション初期化失敗: {msg}"

        log_msgs = []

        try:
            # Step 1: 検索バリデーション
            params = {
                "yakkyokuKeyword": keyword,
                "yakkyokuKeyword2": "",
                "searchJudgeKbn": "2",
            }
            r = self.session.get(
                f"{self.BASE}/juminkanja/S2300/yakkyokuSearch",
                params=params,
                headers={"ajaxFlag": "true"},
                timeout=15,
            )

            if r.status_code != 200:
                return [], 0, f"検索バリデーション失敗 HTTP {r.status_code}"

            try:
                j = r.json()
                if j.get("code") != "0":
                    return [], 0, f"検索エラー: {j.get('messages', '不明')}"
            except Exception:
                return [], 0, "レスポンスのJSON解析失敗"

            log_msgs.append(f"検索バリデーション OK (keyword='{keyword}')")

            # Step 2: 結果一覧を取得（複数ページ対応）
            all_candidates: List[PharmacyCandidate] = []
            total_count = 0
            encoded_keyword = urllib.parse.quote(keyword)

            for page in range(max_pages):
                params_list = {
                    "sjk": "2",
                    "page": str(page),
                    "size": "20",
                    "sortNo": "1",
                }
                if pref_code:
                    params_list["prefCd"] = pref_code

                r2 = self.session.get(
                    f"{self.BASE}/juminkanja/S2400/initialize/{encoded_keyword}/",
                    params=params_list,
                    timeout=15,
                )

                if r2.status_code != 200:
                    log_msgs.append(f"  page {page}: HTTP {r2.status_code} → 中断")
                    break

                candidates, total = self._parse_candidate_list(r2.text)
                if page == 0:
                    total_count = total
                    log_msgs.append(f"  合計 {total}件ヒット")

                all_candidates.extend(candidates)
                log_msgs.append(f"  page {page}: {len(candidates)}件取得（累計 {len(all_candidates)}件）")

                if len(candidates) == 0 or len(all_candidates) >= total_count:
                    break

                time.sleep(0.3)

            status = f"{len(all_candidates)}件取得 (全{total_count}件中)"
            return all_candidates, total_count, status

        except requests.Timeout:
            return [], 0, "タイムアウト"
        except Exception as e:
            return [], 0, f"エラー: {type(e).__name__}: {e}"

    def _parse_candidate_list(
        self, html: str
    ) -> Tuple[List[PharmacyCandidate], int]:
        """検索結果一覧ページをパース"""
        soup = BeautifulSoup(html, "html.parser")
        candidates = []

        # 合計件数を取得
        total = 0
        page_text = soup.get_text()
        cnt_match = re.search(r"(\d{1,6})\s*件", page_text)
        if cnt_match:
            total = int(cnt_match.group(1))

        # 各薬局アイテムをパース
        items = soup.find_all("div", class_="item")

        # div.item が見つからない場合は他のパターンを試みる
        if not items:
            # リスト要素としての薬局名リンクを直接探す
            items = soup.find_all(
                lambda tag: tag.name in ["li", "div", "tr"]
                and tag.find("a", href=re.compile(r"S2430"))
            )

        for item in items:
            # ナビゲーション等の非薬局itemを除外: h3.name を持つものだけ
            h3_name = item.find("h3", class_="name")
            if not h3_name:
                continue
            link = h3_name.find("a", href=True)
            if not link:
                continue

            name = link.get_text(strip=True)
            href = link.get("href", "")
            if not href:
                continue

            # 絶対URLに変換
            if href.startswith("/"):
                href = self.DOMAIN + href
            elif not href.startswith("http"):
                href = self.DOMAIN + "/znk-web/" + href

            # URLパラメータから prefCd / kikanCd を取得
            parsed = urllib.parse.urlparse(href)
            qp = dict(urllib.parse.parse_qsl(parsed.query))
            pref_cd = qp.get("prefCd", "")
            kikan_cd = qp.get("kikanCd", "")

            # 住所を取得
            # MHLWポータルでは dt 内に <img alt="住所"> がある構造
            address = ""
            for dl in item.find_all("dl"):
                dt = dl.find("dt")
                if not dt:
                    continue
                img = dt.find("img")
                dt_text = dt.get_text(strip=True)
                is_address = (img and "住所" in img.get("alt", "")) or any(
                    kw in dt_text for kw in ["住所", "所在地"]
                )
                if is_address:
                    dd = dl.find("dd")
                    if dd:
                        # Googleマップリンクを除去してテキスト取得
                        for a in dd.find_all("a"):
                            a.decompose()
                        raw_addr = dd.get_text(strip=True)
                        # 〒XXX-XXXX を除いてクリーンな住所を抽出
                        cleaned = re.sub(r"〒\s*\d{3}[-－]\d{4}\s*", "", raw_addr)
                        cleaned = re.sub(r"\s+", " ", cleaned).strip()
                        address = cleaned[:60]
                        break

            # フォールバック: 〒XXXXX形式から住所を抽出
            if not address:
                full_item_text = item.get_text(separator=" ", strip=True)
                addr_match = re.search(r"〒\s*\d{3}-?\d{4}\s+(\S+.{5,50}?)(?:\s+\(|\s+TEL|\s+電話|$)", full_item_text)
                if addr_match:
                    address = addr_match.group(1).strip()[:60]

            if name:
                candidates.append(
                    PharmacyCandidate(
                        name=name,
                        address=address,
                        href=href,
                        pref_cd=pref_cd,
                        kikan_cd=kikan_cd,
                    )
                )

        return candidates, max(total, len(candidates))

    def get_pharmacy_detail(
        self, candidate: PharmacyCandidate
    ) -> Tuple[Optional[Dict], str]:
        """薬局詳細ページを取得して処方箋受付回数等を抽出"""
        if not self._initialized:
            ok, msg = self.initialize_session()
            if not ok:
                return None, f"セッション初期化失敗: {msg}"

        # prefCd と kikanCd が分かっていれば直接APIを叩く
        if candidate.pref_cd and candidate.kikan_cd:
            url = (
                f"{self.BASE}/juminkanja/S2430/initialize"
                f"?prefCd={candidate.pref_cd}"
                f"&kikanCd={candidate.kikan_cd}"
                f"&kikanKbn=5"
            )
        else:
            url = candidate.href
            if not url.startswith("http"):
                url = self.DOMAIN + url

        try:
            r = self.session.get(url, timeout=15)
            if r.status_code != 200:
                return None, f"詳細ページ取得失敗 HTTP {r.status_code}"

            data = self._parse_detail_page(r.text)
            data["source_url"] = url
            return data, "OK"

        except requests.Timeout:
            return None, "タイムアウト"
        except Exception as e:
            return None, f"エラー: {type(e).__name__}: {e}"

    def _parse_detail_page(self, html: str) -> Dict:
        """詳細ページから処方箋受付回数等を抽出"""
        soup = BeautifulSoup(html, "html.parser")
        data: Dict = {}
        full_text = soup.get_text(separator=" ")

        # ── 施設名
        for tag in ["h1", "h2", "h3"]:
            el = soup.find(tag)
            if el:
                data["facility_name"] = el.get_text(strip=True)
                break

        # ── 構造化データ（th/td テーブル）を全て取得
        fields: Dict[str, str] = {}
        for row in soup.find_all("tr"):
            cells = row.find_all(["th", "td"])
            if len(cells) >= 2:
                key = cells[0].get_text(strip=True)
                val = cells[1].get_text(strip=True)
                if key:
                    fields[key] = val

        # dl/dt/dd 形式も対応
        for dl in soup.find_all("dl"):
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            for dt, dd in zip(dts, dds):
                key = dt.get_text(strip=True)
                val = dd.get_text(strip=True)
                if key:
                    fields[key] = val

        data["all_fields"] = fields

        # ── 処方箋受付回数を探す
        # 薬局機能情報では「総取扱処方箋数」= 報告期日の前年1年間の取扱処方箋枚数
        rx_annual = None
        rx_period = None
        rx_raw = None

        # 1) 「総取扱処方箋数」フィールドを最優先で探す
        for field_key, field_val in fields.items():
            if "総取扱処方箋数" in field_key:
                nums = re.findall(r"[\d,]+", field_val)
                if nums:
                    try:
                        n = int(nums[0].replace(",", ""))
                        if n > 0:
                            rx_annual = n
                            rx_period = "年間実績（報告期日の前年1年間の取扱処方箋枚数）"
                            rx_raw = n
                            break
                    except (ValueError, OverflowError):
                        pass

        # 2) 他の処方箋・受付関連フィールドを探す（フォールバック）
        if rx_annual is None:
            for field_key, field_val in fields.items():
                if not any(kw in field_key for kw in ["処方", "受付回数"]):
                    continue
                # 回数・件数・枚数など数値を含むもの
                nums = re.findall(r"[\d,]+", field_val)
                if not nums:
                    continue
                try:
                    n = int(nums[0].replace(",", ""))
                    if n == 0:
                        continue
                    if "週" in field_key or "週" in field_val:
                        rx_annual = int(n * 52.14)
                        rx_period = f"週平均 {n}回 → 年換算（× 52.14週）"
                        rx_raw = n
                    elif "月" in field_key or "月" in field_val:
                        rx_annual = int(n * 12)
                        rx_period = f"月平均 {n}回 → 年換算（× 12ヶ月）"
                        rx_raw = n
                    elif "年" in field_key or "年間" in field_val:
                        rx_annual = n
                        rx_period = "年間実績"
                        rx_raw = n
                    elif "日" in field_key:
                        rx_annual = int(n * NATIONAL_STATS["working_days"])
                        rx_period = f"1日平均 {n}枚 → 年換算（× {NATIONAL_STATS['working_days']}日）"
                        rx_raw = n
                    if rx_annual:
                        break
                except (ValueError, OverflowError):
                    continue

        # 3) フルテキストから正規表現で探す（最終フォールバック）
        if rx_annual is None:
            text_patterns = [
                (r"総取扱処方箋数[^\d]*(\d{1,3}(?:,\d{3})*|\d{4,})\s*件", "annual"),
                (r"週\s*平均[^\d]{0,15}(\d{1,4}(?:,\d{3})*)\s*(?:回|枚)", "weekly"),
                (r"月\s*平均[^\d]{0,15}(\d{1,5}(?:,\d{3})*)\s*(?:回|枚)", "monthly"),
                (r"年間[^\d]{0,15}(\d{1,6}(?:,\d{3})*)\s*(?:回|件|枚)", "annual"),
                (r"1日\s*(?:平均)?[^\d]{0,15}(\d{2,3}(?:,\d{3})*)\s*(?:回|枚)", "daily"),
            ]
            for pat, period in text_patterns:
                m = re.search(pat, full_text, re.DOTALL)
                if m:
                    try:
                        n = int(m.group(1).replace(",", ""))
                        if n == 0:
                            continue
                        if period == "weekly":
                            rx_annual = int(n * 52.14)
                            rx_period = f"週平均 {n}回 → 年換算"
                        elif period == "monthly":
                            rx_annual = int(n * 12)
                            rx_period = f"月平均 {n}回 → 年換算"
                        elif period == "annual":
                            rx_annual = n
                            rx_period = "年間実績"
                        elif period == "daily":
                            rx_annual = int(n * NATIONAL_STATS["working_days"])
                            rx_period = f"1日平均 {n}枚 → 年換算"
                        rx_raw = n
                        break
                    except (ValueError, OverflowError):
                        continue

        data["prescriptions_annual"] = rx_annual
        data["prescription_period_label"] = rx_period
        data["prescription_raw_value"] = rx_raw

        return data


# ---------------------------------------------------------------------------
# 2. ウェブ検索（DuckDuckGo）
# ---------------------------------------------------------------------------

class WebSearcher:
    DDG_URL = "https://html.duckduckgo.com/html/"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36"
            ),
        })

    def search(self, pharmacy_name: str) -> Tuple[Optional[Dict], str]:
        queries = [
            f'"{pharmacy_name}" 処方箋受付回数 薬局機能情報',
            f'{pharmacy_name} 処方箋 年間 枚数 受付',
        ]
        for query in queries:
            result = self._run(query)
            if result:
                return result, "ウェブ検索でデータを発見"
            time.sleep(0.5)
        return None, "ウェブ検索でも該当データなし"

    def _run(self, query: str) -> Optional[Dict]:
        try:
            r = self.session.post(
                self.DDG_URL,
                data={"q": query, "kl": "jp-jp"},
                timeout=12,
            )
            if r.status_code != 200:
                return None
            soup = BeautifulSoup(r.text, "html.parser")
            for result in soup.find_all("div", class_="result")[:5]:
                snippet_tag = result.find("a", class_="result__snippet") or result.find(
                    "div", class_="result__snippet"
                )
                link_tag = result.find("a", class_=re.compile(r"result.*url|result.*title", re.I))
                snippet = snippet_tag.get_text(strip=True) if snippet_tag else ""
                href = link_tag.get("href", "") if link_tag else ""
                extracted = self._extract(snippet)
                if extracted:
                    extracted["web_url"] = href
                    extracted["snippet"] = snippet
                    return extracted
        except Exception:
            pass
        return None

    def _extract(self, text: str) -> Optional[Dict]:
        for pat, period, mult in [
            (r"(\d{1,3}(?:,\d{3})*)\s*枚.*?年", "annual", 1.0),
            (r"年.*?(\d{1,3}(?:,\d{3})*)\s*枚", "annual", 1.0),
            (r"1日.*?(\d{2,3})\s*枚", "daily", 305.0),
            (r"週.*?(\d{2,4})\s*枚", "weekly", 52.14),
        ]:
            m = re.search(pat, text)
            if m:
                try:
                    val = float(m.group(1).replace(",", ""))
                    annual = int(val * mult)
                    if 500 <= annual <= 5_000_000:
                        return {"prescriptions_per_year": annual, "period": period}
                except (ValueError, OverflowError):
                    pass
        return None


# ---------------------------------------------------------------------------
# 3. 統計モデルによる推計
# ---------------------------------------------------------------------------

class PharmacyEstimator:
    HOSPITAL_LARGE = ["大学病院", "医療センター", "国立病院", "市立病院",
                      "県立病院", "済生会", "日赤", "赤十字", "JCHO", "がんセンター", "中央病院"]
    HOSPITAL_GATE  = ["門前", "病院前", "ホスピタル"]
    HOSPITAL_NAME  = ["病院", "Hospital"]
    CLINIC_KW      = ["クリニック", "診療所", "医院", "内科", "外科", "皮膚科",
                      "整形外科", "眼科", "耳鼻科", "小児科", "産婦人科"]

    def estimate(self, pharmacy_name: str, prefecture: str = "") -> SearchResult:
        refs: List[Dict] = []

        # チェーン判定
        matched_chain: Optional[str] = None
        chain_info: Optional[Dict] = None
        for chain, info in CHAIN_DATA.items():
            if chain in pharmacy_name:
                matched_chain = chain
                chain_info = info
                break

        # 立地判定
        is_large_hosp = any(kw in pharmacy_name for kw in self.HOSPITAL_LARGE)
        is_hosp_gate  = is_large_hosp or any(kw in pharmacy_name for kw in self.HOSPITAL_GATE + self.HOSPITAL_NAME)
        is_clinic      = any(kw in pharmacy_name for kw in self.CLINIC_KW)

        # 推計値決定
        if matched_chain and is_large_hosp:
            pharmacy_type = f"大病院門前チェーン薬局（{matched_chain}）"
            annual_est = int(chain_info["annual_est"] * 2.5)
            min_val, max_val = int(chain_info["annual_est"] * 1.2), int(chain_info["annual_est"] * 5.0)
            confidence = "medium"
            basis = f"「{matched_chain}」IR代表値 {chain_info['annual_est']:,}枚/年 × 大病院門前係数 2.5"
            refs.append({"name": chain_info["ir"], "desc": "IR公開データ × 病院門前係数より推計", "url": ""})

        elif matched_chain and is_hosp_gate:
            pharmacy_type = f"病院門前チェーン薬局（{matched_chain}）"
            annual_est = int(chain_info["annual_est"] * 1.8)
            min_val, max_val = int(chain_info["annual_est"] * 0.9), int(chain_info["annual_est"] * 3.5)
            confidence = "medium"
            basis = f"「{matched_chain}」IR代表値 {chain_info['annual_est']:,}枚/年 × 病院門前係数 1.8"
            refs.append({"name": chain_info["ir"], "desc": "IR公開データ × 病院門前係数より推計", "url": ""})

        elif matched_chain and is_clinic:
            pharmacy_type = f"クリニック周辺チェーン薬局（{matched_chain}）"
            annual_est = int(chain_info["annual_est"] * 1.1)
            min_val, max_val = int(chain_info["annual_est"] * 0.5), int(chain_info["annual_est"] * 2.0)
            confidence = "medium"
            basis = f"「{matched_chain}」IR代表値 {chain_info['annual_est']:,}枚/年 × クリニック係数 1.1"
            refs.append({"name": chain_info["ir"], "desc": "IR公開データより推計", "url": ""})

        elif matched_chain:
            pharmacy_type = f"チェーン薬局（{matched_chain}）"
            annual_est = chain_info["annual_est"]
            min_val, max_val = chain_info["min"], chain_info["max"]
            confidence = "medium"
            basis = f"「{matched_chain}」公開IR・薬局機能情報集計データを参照"
            refs.append({"name": chain_info["ir"], "desc": "IR有価証券報告書・薬局機能情報より", "url": ""})

        elif is_large_hosp:
            pharmacy_type = "大病院門前薬局（独立系）"
            annual_est, min_val, max_val = 80_000, 30_000, 200_000
            confidence = "low"
            basis = "大学病院・医療センター等の大病院門前として分類"

        elif is_hosp_gate:
            pharmacy_type = "病院門前薬局（独立系）"
            annual_est, min_val, max_val = 35_000, 12_000, 120_000
            confidence = "low"
            basis = "病院門前薬局として分類（名称に病院関連キーワードを含む）"

        elif is_clinic:
            pharmacy_type = "クリニック周辺薬局"
            annual_est, min_val, max_val = 12_000, 4_000, 28_000
            confidence = "low"
            basis = "クリニック・診療所周辺薬局として分類"

        else:
            pharmacy_type = "地域密着型薬局"
            annual_est, min_val, max_val = NATIONAL_STATS["median_estimate"], 2_000, 18_000
            confidence = "low"
            basis = f"全国薬局中央値推計（右歪み分布のため平均{NATIONAL_STATS['average_per_year']:,}枚より中央値を採用）"

        refs += [
            {
                "name": "厚生労働省「調剤医療費（電算処理分）の動向」2022年度",
                "desc": (
                    f"全国薬局数 {NATIONAL_STATS['total_pharmacies']:,}施設 / "
                    f"年間処方箋 {NATIONAL_STATS['total_prescriptions']//100_000_000:.1f}億枚 / "
                    f"1施設平均 {NATIONAL_STATS['average_per_year']:,}枚/年"
                ),
                "url": NATIONAL_STATS["source_url"],
            },
            {
                "name": "日本薬剤師会「薬局・薬剤師に関する基本データ」",
                "desc": f"1日平均処方箋受付枚数: {NATIONAL_STATS['daily_average']}枚（2020年調査）",
                "url": "https://www.nichiyaku.or.jp/",
            },
            {
                "name": "厚生労働省 薬局機能情報提供制度（医療情報ネット ナビイ）",
                "desc": "個別薬局の機能情報（処方箋受付回数含む）。本アプリで直接検索可能。",
                "url": "https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2300/initialize",
            },
        ]

        daily_est = annual_est // NATIONAL_STATS["working_days"]
        base_calc = NATIONAL_STATS["daily_average"] * NATIONAL_STATS["working_days"]

        methodology = [
            "### 推計手順",
            "",
            "**STEP 1 — 厚生労働省データ検索**",
            "　MHLWポータル（薬局機能情報提供制度）を検索しましたが、",
            "　処方箋受付回数のデータが取得できなかったため統計モデルで推計します。",
            "",
            "**STEP 2 — チェーン薬局・立地タイプ判定**",
            f"　→ タイプ: **{pharmacy_type}**",
            f"　→ 根拠: {basis}",
            "",
            "**STEP 3 — 推計値算出**",
            f"　→ 代表値: **{annual_est:,}枚/年**",
            f"　→ レンジ: {min_val:,}〜{max_val:,}枚/年",
            f"　→ 1日換算: 約{daily_est}枚/日",
            "",
            "**STEP 4 — 全国統計との整合性確認**",
            f"　全国平均: 日次平均{NATIONAL_STATS['daily_average']}枚 × {NATIONAL_STATS['working_days']}日 = {base_calc:,}枚/年",
            "",
            "**⚠ 注意**: 本推計は参考値です。正確な数値は厚労省ポータルで薬局名を検索するか、",
            "各薬局に直接お問い合わせください。",
        ]

        return SearchResult(
            pharmacy_name=pharmacy_name,
            annual_prescriptions=annual_est,
            prescriptions_range=(min_val, max_val),
            daily_estimate=daily_est,
            data_source="statistical_estimation",
            source_label="統計モデル推計",
            confidence=confidence,
            pharmacy_type=pharmacy_type,
            methodology=methodology,
            references=refs,
            mhlw_found=False,
            mhlw_has_rx_data=False,
        )


# ---------------------------------------------------------------------------
# Streamlit UI ヘルパー
# ---------------------------------------------------------------------------

def confidence_label(c: str) -> str:
    return {"high": "🟢 高（実データ）", "medium": "🟡 中（IR・公開情報）", "low": "🔴 低（統計推計）"}.get(c, "不明")


def render_result(result: SearchResult) -> None:
    # ── ソース種別バナー
    if result.mhlw_found and result.mhlw_has_rx_data:
        st.success("✅ **厚生労働省 薬局機能情報提供制度** から実績データを取得しました")
    elif result.mhlw_found and not result.mhlw_has_rx_data:
        st.info(
            "ℹ️ **厚生労働省ポータル** で薬局は見つかりましたが、処方箋受付回数の記載がありませんでした。"
            "統計モデルで補完しています。"
        )
    elif result.web_search_found:
        st.info("🌐 **ウェブ検索** でデータを発見しました（参考値）")
    else:
        st.warning(
            "📊 厚労省ポータル・ウェブ検索でデータが取得できなかったため、"
            "**統計モデル**による推計値を表示しています"
        )

    st.markdown("---")

    # ── KPIカード
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("年間処方箋受付枚数", f"{result.annual_prescriptions:,} 枚" if result.annual_prescriptions else "---")
    if result.prescriptions_range:
        c2.metric("推計レンジ（下限〜上限）", f"{result.prescriptions_range[0]:,}〜{result.prescriptions_range[1]:,}")
    c3.metric("1日あたり推計", f"約 {result.daily_estimate} 枚/日" if result.daily_estimate else "---")
    c4.metric("信頼度", confidence_label(result.confidence))

    if result.pharmacy_type:
        st.caption(f"薬局タイプ: **{result.pharmacy_type}**")

    st.markdown("---")

    # ── MHLW から取得できたフィールドを表示
    if result.mhlw_found and result.mhlw_fields:
        with st.expander("📋 厚生労働省ポータルから取得したデータ（全フィールド）"):
            display_fields = {k: v for k, v in result.mhlw_fields.items() if v and not k.startswith("field_")}
            if display_fields:
                for k, v in list(display_fields.items())[:50]:
                    st.text(f"  {k}: {v}")
            else:
                for k, v in list(result.mhlw_fields.items())[:50]:
                    st.text(f"  {k}: {v}")

    # ── タブ
    tab1, tab2, tab3 = st.tabs(["📋 推計ロジック", "📚 参照ソース", "🔍 検索ログ"])

    with tab1:
        for line in result.methodology:
            st.markdown(line)

    with tab2:
        for i, ref in enumerate(result.references, 1):
            with st.expander(f"{i}. {ref['name']}"):
                st.write(ref.get("desc", ""))
                if ref.get("url"):
                    st.markdown(f"🔗 [{ref['url']}]({ref['url']})")
        st.markdown("---")
        st.markdown(
            "#### 直接確認する\n"
            "- [厚生労働省 医療情報ネット（ナビイ）薬局検索]"
            "(https://www.iryou.teikyouseido.mhlw.go.jp/znk-web/juminkanja/S2300/initialize)"
            " — 薬局名で検索して処方箋受付回数を直接確認できます\n"
            "- [厚生労働省 調剤医療費の動向](https://www.mhlw.go.jp/topics/medias/med/)"
            " — 全国集計統計"
        )

    with tab3:
        if result.search_log:
            st.code("\n".join(result.search_log))
        else:
            st.write("ログなし")


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="薬局 処方箋枚数予測ツール",
        page_icon="💊",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    st.title("💊 薬局 年間処方箋枚数 予測ツール")
    st.markdown(
        "薬局名を入力して**候補を検索**→候補から選択すると、"
        "厚生労働省データ（医療情報ネット ナビイ）から処方箋受付回数を取得します。"
        "データがない場合は統計モデルで推計します。"
    )

    # ── session_state 初期化
    for key, default in [
        ("candidates", []),
        ("total_count", 0),
        ("selected_idx", 0),
        ("final_result", None),
        ("search_done", False),
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ================================================================
    # STEP 1: キーワード入力 + 候補検索
    # ================================================================
    st.markdown("### STEP 1 — 薬局名を入力して候補を検索")

    col_kw, col_pref = st.columns([3, 1])
    with col_kw:
        keyword = st.text_input(
            "薬局名（一部でも可）",
            placeholder="例: ウエルシア 渋谷 / 日本調剤 新宿 / まつもと薬局",
            help="完全一致でなくてもOKです。部分文字列で検索します。",
            key="keyword_input",
        )
    with col_pref:
        prefecture = st.selectbox("都道府県（任意）", ["（指定なし）"] + PREFECTURES, key="pref_select")

    col_btn1, col_opt = st.columns([2, 1])
    with col_btn1:
        search_btn = st.button("🔍 候補を検索（MHLWポータル）", use_container_width=True, type="primary")
    with col_opt:
        skip_mhlw = st.checkbox("MHLWポータルをスキップ（統計モデルのみ）", value=False)

    if search_btn and keyword.strip():
        pref_code = PREFECTURE_CODES.get(prefecture, "")
        st.session_state["final_result"] = None
        st.session_state["search_done"] = False

        if skip_mhlw:
            # 統計モデルのみ
            estimator = PharmacyEstimator()
            result = estimator.estimate(keyword.strip(), prefecture)
            result.search_log = ["[MHLW] スキップ（ユーザー設定）"]
            st.session_state["final_result"] = result
            st.session_state["search_done"] = True
            st.session_state["candidates"] = []
        else:
            with st.spinner("厚生労働省ポータルを検索中…（初回は10〜20秒かかります）"):
                scraper = MHLWScraper()
                candidates, total, status_msg = scraper.search_candidates(
                    keyword.strip(), pref_code, max_pages=3
                )

            st.session_state["candidates"] = candidates
            st.session_state["total_count"] = total
            st.session_state["selected_idx"] = 0

            if not candidates:
                st.warning(f"MHLWポータルで候補が見つかりませんでした（{status_msg}）。統計モデルで推計します。")
                estimator = PharmacyEstimator()
                result = estimator.estimate(keyword.strip(), prefecture)
                result.search_log = [f"[MHLW] {status_msg}", "[ESTIMATE] 統計モデルによる推計を実行"]
                st.session_state["final_result"] = result
                st.session_state["search_done"] = True
            else:
                st.success(f"✅ {status_msg}（全{total}件中 最大60件表示）")

    # ================================================================
    # STEP 2: 候補選択 + 詳細取得
    # ================================================================
    candidates: List[PharmacyCandidate] = st.session_state.get("candidates", [])

    if candidates and st.session_state.get("final_result") is None:
        st.markdown("---")
        st.markdown("### STEP 2 — 薬局を選択して詳細データを取得")

        # プルダウン選択肢を作成（名前 + 住所）
        options = [
            f"{c.name}　{('（' + c.address[:35] + '）') if c.address else ''}"
            for c in candidates
        ]

        selected_label = st.selectbox(
            f"候補一覧（{len(candidates)}件）",
            options,
            index=st.session_state["selected_idx"],
            key="candidate_select",
        )
        sel_idx = options.index(selected_label)
        st.session_state["selected_idx"] = sel_idx
        sel_candidate = candidates[sel_idx]

        # 選択された薬局の詳細プレビュー
        col_info1, col_info2 = st.columns(2)
        col_info1.caption(f"📍 住所: {sel_candidate.address or '不明'}")
        col_info2.caption(
            f"🔗 MHLWページ: [詳細を見る]({sel_candidate.href})" if sel_candidate.href else ""
        )

        col_fetch, col_stat = st.columns([2, 1])
        with col_fetch:
            fetch_btn = st.button(
                "📄 この薬局の処方箋データを取得", use_container_width=True, type="primary"
            )
        with col_stat:
            use_stat = st.button("📊 統計モデルで推計", use_container_width=True)

        if fetch_btn:
            with st.spinner(f"「{sel_candidate.name}」の詳細データを取得中…"):
                scraper = MHLWScraper()
                ok, _ = scraper.initialize_session()
                detail, detail_msg = scraper.get_pharmacy_detail(sel_candidate)

            log = [
                f"[MHLW] 候補選択: {sel_candidate.name}",
                f"[MHLW] 詳細取得: {detail_msg}",
            ]

            if detail and detail.get("prescriptions_annual"):
                rx = detail["prescriptions_annual"]
                daily = rx // NATIONAL_STATS["working_days"]
                period_label = detail.get("prescription_period_label", "不明")
                result = SearchResult(
                    pharmacy_name=sel_candidate.name,
                    annual_prescriptions=rx,
                    daily_estimate=daily,
                    data_source="mhlw_portal",
                    source_label="厚生労働省 薬局機能情報提供制度",
                    source_url=detail.get("source_url", sel_candidate.href),
                    confidence="high",
                    pharmacy_type="MHLWポータル実績データ",
                    methodology=[
                        "### 取得方法",
                        "",
                        "**厚生労働省 医療情報ネット（ナビイ）薬局機能情報提供制度** より直接取得。",
                        "",
                        f"**取得値**: {period_label}",
                        f"**年間換算**: {rx:,}枚/年",
                        f"**1日換算**: 約{daily}枚/日（年間稼働日数{NATIONAL_STATS['working_days']}日で除算）",
                        "",
                        f"**MHLWポータルURL**: {detail.get('source_url', '')}",
                    ],
                    references=[
                        {
                            "name": "厚生労働省 薬局機能情報提供制度",
                            "desc": "薬局が毎年都道府県に報告する機能情報（処方箋受付回数を含む）",
                            "url": detail.get("source_url", sel_candidate.href),
                        }
                    ],
                    mhlw_found=True,
                    mhlw_has_rx_data=True,
                    search_log=log,
                    mhlw_fields=detail.get("all_fields", {}),
                )
            elif detail:
                # MHLW で薬局は見つかったが処方箋数データなし → 統計モデルで補完
                log.append("[MHLW] 処方箋受付回数の記載なし → 統計モデルで補完")
                estimator = PharmacyEstimator()
                result = estimator.estimate(sel_candidate.name)
                result.mhlw_found = True
                result.mhlw_has_rx_data = False
                result.mhlw_fields = detail.get("all_fields", {})
                result.search_log = log
                result.source_url = detail.get("source_url", sel_candidate.href)
                result.methodology = [
                    "### 取得方法",
                    "",
                    "**厚生労働省ポータル** で薬局情報を取得しましたが、",
                    "処方箋受付回数の項目が記載されていませんでした。",
                    "（薬局によっては報告していない場合があります）",
                    "",
                ] + result.methodology
            else:
                # 詳細ページ取得失敗 → 統計モデル
                log.append(f"[MHLW] 詳細取得失敗: {detail_msg} → 統計モデルで推計")
                estimator = PharmacyEstimator()
                result = estimator.estimate(sel_candidate.name)
                result.search_log = log

            st.session_state["final_result"] = result
            st.session_state["search_done"] = True

        if use_stat:
            estimator = PharmacyEstimator()
            result = estimator.estimate(sel_candidate.name)
            result.search_log = [f"[STAT] 「{sel_candidate.name}」を統計モデルで推計"]
            st.session_state["final_result"] = result
            st.session_state["search_done"] = True

    # ================================================================
    # STEP 3: 結果表示
    # ================================================================
    final_result: Optional[SearchResult] = st.session_state.get("final_result")

    if final_result:
        st.markdown("---")
        st.markdown(f"## 結果: `{final_result.pharmacy_name}`")
        render_result(final_result)

    # ── 初期画面（未検索時）
    if not st.session_state.get("search_done") and not candidates and final_result is None:
        st.markdown("---")
        st.markdown("### 全国統計（参考）")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("全国薬局数（2022年）", f"{NATIONAL_STATS['total_pharmacies']:,} 施設")
        c2.metric("年間処方箋総数", f"{NATIONAL_STATS['total_prescriptions'] // 100_000_000:.1f} 億枚")
        c3.metric("1施設あたり平均", f"{NATIONAL_STATS['average_per_year']:,} 枚/年")
        c4.metric("1日平均受付枚数", f"{NATIONAL_STATS['daily_average']} 枚/日")
        st.caption(f"出典: {NATIONAL_STATS['source']}")

        st.markdown("---")
        st.markdown("### 使い方")
        col1, col2, col3 = st.columns(3)
        col1.markdown(
            "**① 薬局名を入力**\n\n"
            "完全一致でなくてOKです。\n"
            "「ウエルシア 渋谷」のように\nチェーン名＋地名で絞り込めます。"
        )
        col2.markdown(
            "**② 候補から選択**\n\n"
            "MHLWポータルから最大60件の\n"
            "候補が表示されます。\n"
            "プルダウンで正しい薬局を選択。"
        )
        col3.markdown(
            "**③ データ取得・推計**\n\n"
            "処方箋受付回数の実績データを取得。\n"
            "記載がない場合はIR・統計データ\nから自動推計します。"
        )


if __name__ == "__main__":
    main()
