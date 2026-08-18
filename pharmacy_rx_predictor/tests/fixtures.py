# -*- coding: utf-8 -*-
"""ナビィ一覧ページの模擬HTML。旧HTML(h3.name)と新HTML(h2.name + mapLink)の両方。"""

def _item_old(name, pref, kikan, kbn=None, addr="山梨県中央市若宮50-1"):
    kb = f"&kikanKbn={kbn}" if kbn is not None else ""
    return f'''
    <div class="item">
      <h3 class="name"><a href="/znk-web/juminkanja/S2430/initialize?prefCd={pref}&kikanCd={kikan}{kb}">{name}</a></h3>
      <p>〒409-3806 {addr}</p>
    </div>'''

def _item_new(name, pref, kikan, kbn=None, addr="山梨県中央市若宮50-1", lat=None, lon=None):
    kb = f"&kikanKbn={kbn}" if kbn is not None else ""
    maplink = ""
    if lat is not None:
        maplink = (f'<a class="mapLink" data-url="https://www.google.com/maps?q={lat},{lon}">'
                   f'Googleマップ</a>')
    return f'''
    <div class="item">
      <h2 class="name"><a href="/znk-web/juminkanja/S2430/initialize?prefCd={pref}&kikanCd={kikan}{kb}">{name}</a></h2>
      <p>〒409-3806 {addr}</p>
      {maplink}
    </div>'''

def page(items_html, total, shown=20):
    return f'''<html><body>
      <div class="searchResult">
        <p>1ページに{shown}件表示しています</p>
        <p>検索結果 {total} 件</p>
        <div class="resultItems">{items_html}</div>
      </div>
    </body></html>'''

PH_NAMES = [
    ("さくら薬局中央店", "19", "1910001"),
    ("さくら薬局東町店", "19", "1910002"),   # 同一チェーン別店舗（旧dedupで消えていた）
    ("日本調剤 甲府薬局", "19", "1910003"),
    ("アイン薬局 若宮店", "19", "1910004"),
]
MED_NAMES = [
    ("田中内科クリニック", "19", "1920001", 2),
    ("中田内科クリニック", "19", "1920002", 2),   # 文字集合が同一（旧dedupで消えていた）
    ("市立中央病院",       "19", "1920003", 1),
    ("やまなし歯科医院",   "19", "1920004", 2),
]

def pharmacy_page_old(total=4):
    return page("".join(_item_old(n, p, k) for n, p, k in PH_NAMES), total)

def pharmacy_page_new(total=4, with_coords=True):
    body = "".join(
        _item_new(n, p, k, lat=(35.60 + i * 0.001) if with_coords else None,
                  lon=(138.50 + i * 0.001) if with_coords else None)
        for i, (n, p, k) in enumerate(PH_NAMES))
    return page(body, total)

def med_page_new(total=4):
    body = "".join(
        _item_new(n, p, k, kbn=kb, lat=35.60 + i * 0.001, lon=138.50 + i * 0.001)
        for i, (n, p, k, kb) in enumerate(MED_NAMES))
    return page(body, total)

def med_page_old(total=4):
    return page("".join(_item_old(n, p, k, kbn=kb) for n, p, k, kb in MED_NAMES), total)

# 総件数が「20件表示」より後ろに出るページ（旧パーサは total=20 と誤読していた）
def pharmacy_page_paged(total=57):
    return pharmacy_page_new(total=total)
