# -*- coding: utf-8 -*-
"""ナビィを模したフェイクHTTPセッション。実通信なしで run_analysis を通す。"""
import json, re, urllib.parse

CLAT, CLON = 35.6000, 138.5000

def _off(i):
    """i番目の施設の座標（中心から 40m 刻みで東へ）。"""
    return CLAT, CLON + i * 0.00045      # 約40m/件

class Resp:
    def __init__(self, text="", code=200, payload=None):
        self.text = text; self.status_code = code; self._payload = payload
    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

def _item(name, pref, kikan, kbn, lat, lon, addr="山梨県中央市若宮50-1"):
    return f'''<div class="item">
      <h2 class="name"><a href="/znk-web/juminkanja/S2430/initialize?prefCd={pref}&kikanCd={kikan}&kikanKbn={kbn}">{name}</a></h2>
      <p>〒409-3806 {addr}</p>
      <a class="mapLink" data-url="https://www.google.com/maps?q={lat},{lon}">Googleマップ</a>
    </div>'''

def _page(items, total):
    return (f'<html><body><div class="searchResult"><p>1ページに20件表示しています</p>'
            f'<p>検索結果 {total} 件</p><div class="resultItems">{"".join(items)}</div>'
            f'</div></body></html>')

DETAIL = '''<html><body>
  <a href="https://www.google.com/maps?q={lat},{lon}">地図</a>
  <table><tr><th>所在地</th><td>〒409-3806 山梨県中央市若宮50-1</td></tr>
         <tr><th>院内・院外処方</th><td>院外処方</td></tr></table>
  <div class="item"><h2>患者数</h2>
    <table>
      <tr><th></th><th>入院患者数</th><th>外来患者数</th></tr>
      <tr><th class="ptn4ItemName">前年度1日平均</th><td>12</td><td>{op}</td></tr>
    </table>
  </div>
  <table><tr><th>処方箋受付回数（年間）</th><td>{rx}回</td></tr></table>
  <p>{name}</p>
</body></html>'''


class FakeNavii:
    """N_MED 件の医療機関 / N_PH 件の薬局を返すフェイクナビィ。"""

    def __init__(self, n_med=57, n_ph=34):
        self.n_med, self.n_ph = n_med, n_ph
        self.hits = {"ph_pages": set(), "med_pages": set(), "details": set()}
        self.cookies = {}
        self.headers = {}
        # 意図的に紛らわしい名前を入れる（旧dedupなら消えていたペア）
        self.med_names = ["田中内科クリニック", "中田内科クリニック"] + \
                         [f"やまなしクリニック{i}" for i in range(n_med - 2)]
        self.ph_names = ["さくら薬局中央店", "さくら薬局東町店"] + \
                        [f"甲府調剤薬局{i}" for i in range(n_ph - 2)]

    # requests.Session 互換
    def mount(self, *a, **k): pass

    def get(self, url, params=None, timeout=None, **kw):
        params = params or {}
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        q.update({k: str(v) for k, v in params.items()})

        if "S2300/yakkyokuSearch" in url:
            return Resp(payload={"code": "0", "result": {"id": "PHSEARCH"}})
        if "S2320/search" in url:
            return Resp(payload={"code": "0", "result": {
                "redirectUrl": "https://x/znk-web/juminkanja/S2400/initialize?id=MEDSEARCH"}})
        if "S2400/initialize" in url:
            page = int(q.get("page", 0))
            if q.get("id") == "PHSEARCH":
                self.hits["ph_pages"].add(page)
                names, total, kbn, pref = self.ph_names, self.n_ph, 5, "19"
                cd = lambda i: f"5{i:06d}"
            else:
                self.hits["med_pages"].add(page)
                names, total, kbn, pref = self.med_names, self.n_med, 2, "19"
                cd = lambda i: f"2{i:06d}"
            lo, hi = page * 20, min((page + 1) * 20, total)
            items = []
            for i in range(lo, hi):
                lat, lon = _off(i)
                items.append(_item(names[i], pref, cd(i), kbn, lat, lon))
            return Resp(_page(items, total))
        if "S2430/initialize" in url:
            cd = q.get("kikanCd", "")
            kbn = q.get("kikanKbn", "")
            self.hits["details"].add((cd, kbn))
            m = re.match(r"([25])(\d{6})$", cd)
            if not m:
                return Resp("E-0109 データは存在しません")
            kind, idx = m.group(1), int(m.group(2))
            # 薬局は kikanKbn=5、医療機関は 2 のときだけ実データを返す
            if (kind == "5") != (kbn == "5"):
                return Resp("E-0109 データは存在しません")
            lat, lon = _off(idx)
            names = self.ph_names if kind == "5" else self.med_names
            return Resp(DETAIL.format(lat=lat, lon=lon, op=60 + idx,
                                      rx=20000 + idx * 100, name=names[idx]))
        return Resp("<html><body>ok</body></html>")

    def post(self, *a, **k):
        return Resp("<html></html>")
