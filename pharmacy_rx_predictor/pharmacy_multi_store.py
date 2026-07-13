# -*- coding: utf-8 -*-
"""
処方箋予測 — 複数店舗（A/B/C…）比較ツール
====================================================
とあるスーパーの複数の出店候補地（A点・B点・C点…）をまとめて分析し、
「医療機関ベース（ハフ按分）」と「集客ベース（来店客数）」の2トラックで比較する。

- 既存の「260702_Prescription Analysis_v2.py」のモデル/スクレイパーをそのまま再利用
  （既存ファイルは一切変更しない。UIを起動せずに関数だけ読み込む）。
- ブラウザ上で、候補地ごとに①ハフの取り分内訳・②集客の内訳・面/門前一覧（目視修正可）・
  医療機関/薬局リストを表示。
- 「数式入りExcel(.xlsx)」で書き出せる。ユニーク客数・競合・係数を編集すると集客の予測が、
  原資・重みを編集すると医療機関(ハフ)の獲得が、Excel上で自動で再計算される。
"""
import io
import math
import os
import re

import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

st.set_page_config(page_title="複数店舗比較 — 処方箋予測", page_icon="🏪", layout="wide")

CORE_FILE = os.path.join(os.path.dirname(__file__), "260702_Prescription Analysis_v2.py")


# ── 既存ファイルからモデル/スクレイパーだけを読み込む（UIは起動しない） ──────────
@st.cache_resource(show_spinner="モデルを読み込み中…")
def load_core():
    src = open(CORE_FILE, encoding="utf-8").read()
    src = re.sub(r"st\.set_page_config\(.*?\)", "", src, count=1, flags=re.DOTALL)
    for marker in ('\nst.markdown("""\n<style>', "\nst.title("):
        idx = src.find(marker)
        if idx != -1:
            src = src[:idx]
            break
    ns = {"__name__": "rx_core"}
    exec(compile(src, CORE_FILE, "exec"), ns)
    return ns


M = load_core()
MHLWScraper = M["MHLWScraper"]
run_analysis = M["run_analysis"]
PredictionAssumptions = M["PredictionAssumptions"]
HuffParams = M["HuffParams"]
FootfallParams = M["FootfallParams"]
compute_footfall_prediction = M["compute_footfall_prediction"]
classify_menkata = M["classify_menkata"]
footfall_competitor_power = M["footfall_competitor_power"]
FORMAT_PRESETS = M["FORMAT_PRESETS"]
haversine = M["haversine"]
_clinic_annual_rx_pool = M["_clinic_annual_rx_pool"]
_pharmacy_attractiveness = M["_pharmacy_attractiveness"]


@st.cache_resource
def get_scraper():
    return MHLWScraper()


# ════════════════════════════════ サイドバー ════════════════════════════════
with st.sidebar:
    st.header("共通設定")
    radius_m = st.slider("商圏半径 (m)", 500, 5000, 3000, 100,
                         help="スーパー商圏に準拠。全候補地に共通で使います。")
    max_detail = st.slider("詳細取得件数（薬局）", 5, 60, 30, 5,
                           help="ナビィから実績・座標を取る薬局の上限（多いほど正確・遅い）。")
    gate_m = 50

    st.divider()
    st.subheader("🛒 集客ベースの前提（全候補地に共通）")
    store_format = st.selectbox("店舗形態", list(FORMAT_PRESETS.keys()), index=1)
    _pre = FORMAT_PRESETS[store_format]
    ff_r65 = st.number_input("65歳以上の比率", 0.0, 1.0, float(_pre["r65"]), 0.01)
    c1, c2 = st.columns(2)
    ff_v65 = c1.number_input("65+ の月受診回数", 0.0, 6.0, 3.0, 0.1)
    ff_vu65 = c2.number_input("65- の月受診回数", 0.0, 6.0, 1.3, 0.1)
    c3, c4, c5 = st.columns(3)
    ff_issue = c3.number_input("発行率", 0.0, 1.0, 0.8054, 0.0001, format="%.4f")
    ff_ext = c4.number_input("院外率", 0.0, 1.0, 0.8313, 0.0001, format="%.4f")
    ff_use = c5.number_input("利用率", 0.0, 1.0, 0.137, 0.001, format="%.3f")
    c6, c7 = st.columns(2)
    ff_monzen = c6.number_input("門前しきい値(m)", 0, 300, 50, 10,
                                help="最寄りクリニックがこの距離以内の薬局は門前として自動判定→面競合から除外。")
    ff_decay = c7.number_input("面競合の距離減衰λ(m)", 0, 3000, 1000, 100,
                               help="遠い面競合を弱く数える。小さいほど自店シェア↑。")
    ff_main = st.number_input("メイン薬局しきい値(枚/年)", 0, 100000, 15000, 1000,
                              help="実績がこの値以上の薬局は面競合から除外。0で無効。")

    with st.expander("⚙️ 詳細設定（ハフ按分・通常は変更不要）", expanded=False):
        huff_lambda = st.slider("距離減衰 λ (m)", 150, 900, 250, 50)
        huff_boost = st.slider("門前ブースト", 1.0, 15.0, 8.0, 0.5)
        huff_candA = st.number_input("候補店の引力（大型店は上げる）", 0.2, 10.0, 1.0, 0.1)

    st.caption("※ サイドバーや面/門前を変えると、再検索なしで比較表・Excelが即更新されます。")


def make_fp(uni):
    return FootfallParams(
        enabled=(uni > 0), store_format=store_format,
        unique_customers_monthly=float(uni), ratio_65plus=float(ff_r65),
        visits_month_65plus=float(ff_v65), visits_month_under65=float(ff_vu65),
        issue_rate=float(ff_issue), external_rate=float(ff_ext), use_rate=float(ff_use),
        menkata_monzen_dist=float(ff_monzen), menkata_main_rx=float(ff_main),
        competitor_decay_m=float(ff_decay),
    )


def make_hp():
    return HuffParams(lambda_m=float(huff_lambda), monzen_boost=float(huff_boost),
                      candidate_attractiveness=float(huff_candA), monzen_radius=float(gate_m))


# ── ハフの取り分内訳（クリニック1行ずつ・自店の重み/競合の重み合計を明示） ─────────
def huff_breakdown(med, ph, clat, clon, hp, a):
    comps = []
    for p in ph:
        if p.lat is None or p.lon is None:
            continue
        ak = _pharmacy_attractiveness(p, hp.national_avg_rx) if hp.weight_by_power else 1.0
        comps.append((p.lat, p.lon, ak))

    def bw(d, aa):
        v = math.exp(-d / hp.lambda_m)
        if d <= hp.monzen_radius:
            v *= hp.monzen_boost
        return aa * v

    rows = []
    for f in med:
        if f.lat is None or f.lon is None or not getattr(f, "in_area", True):
            continue
        d_self = haversine(clat, clon, f.lat, f.lon)
        if d_self > hp.reach_m:
            continue
        pool = _clinic_annual_rx_pool(f, a, {})
        if pool <= 0:
            continue
        self_w = bw(d_self, hp.candidate_attractiveness)
        den = self_w
        for (plat, plon, ak) in comps:
            dk = haversine(plat, plon, f.lat, f.lon)
            if dk <= hp.reach_m:
                den += bw(dk, ak)
        share = self_w / den if den > 0 else 0.0
        rows.append({"clinic": f.name, "dist": d_self, "pool": pool,
                     "self_w": self_w, "comp_w": den - self_w,
                     "share": share, "captured": pool * share})
    rows.sort(key=lambda r: r["captured"], reverse=True)
    return rows


# ── 生データ＋現在の設定/手修正から、両トラックを算出（再検索なしで即再計算） ──────
def compute_candidate(raw):
    med, ph = raw["med"], raw["ph"]
    clat, clon, uni = raw["clat"], raw["clon"], raw["uni"]
    a = PredictionAssumptions()
    hp = make_hp()
    fp = make_fp(uni)
    hb = huff_breakdown(med, ph, clat, clon, hp, a)
    med_total = sum(r["captured"] for r in hb)
    classified = classify_menkata(ph, med, clat, clon,
                                  monzen_dist=fp.menkata_monzen_dist,
                                  main_rx_threshold=fp.menkata_main_rx, reach_m=hp.reach_m)
    override = st.session_state.get("mk_multi", {}).get(raw["label"], {})
    cpow, cn, cexc = footfall_competitor_power(classified, override, fp.competitor_decay_m,
                                               hp.national_avg_rx)
    foot = compute_footfall_prediction(fp, cpow)
    return {
        "label": raw["label"], "name": raw["name"], "addr": raw["addr"], "uni": uni,
        "med": med, "ph": ph, "clat": clat, "clon": clon, "hp": hp, "fp": fp,
        "huff_rows": hb, "med_total": med_total, "classified": classified, "override": override,
        "comp_power": cpow, "comp_n": cn, "comp_excluded": cexc,
        "foot_total": (foot["total"] if foot else None), "foot": foot,
    }


# ════════════════════════════════ 数式入りExcel ════════════════════════════════
_HDR = Font(bold=True, color="FFFFFF")
_HDR_FILL = PatternFill("solid", fgColor="0F766E")
_HDR_FILL2 = PatternFill("solid", fgColor="B45309")
_INP_FILL = PatternFill("solid", fgColor="FFF7E0")   # 編集できる入力＝薄い黄色
_CALC_FILL = PatternFill("solid", fgColor="EEF2F1")  # 自動計算＝薄いグレー
_BOLD = Font(bold=True)


def _sheet_name(label):
    return re.sub(r"[\\/*?:\[\]]", "_", str(label))[:20]


def _build_footfall_sheet(wb, r):
    fp = r["fp"]
    ws = wb.create_sheet(f"集客_{_sheet_name(r['label'])}")
    ws["A1"] = f"② 集客ベース（来店客数）  {r['label']}  {r['name']}"
    ws["A1"].font = Font(bold=True, size=12)
    ws["A2"] = "【黄色のセルは編集できます。編集すると下の「獲得」が自動で再計算されます】"
    ws["A2"].font = Font(italic=True, size=9, color="B45309")
    inputs = [
        ("月間ユニーク客数", r["uni"]), ("65歳以上の比率", fp.ratio_65plus),
        ("65+ 月受診回数", fp.visits_month_65plus), ("65- 月受診回数", fp.visits_month_under65),
        ("処方箋発行率", fp.issue_rate), ("院外処方率", fp.external_rate),
        ("当該薬局利用率", fp.use_rate), ("面競合の距離減衰λ(m)", fp.competitor_decay_m),
    ]
    for k, (lab, val) in enumerate(inputs):
        rr = 3 + k
        ws.cell(row=rr, column=1, value=lab)
        ws.cell(row=rr, column=2, value=val).fill = _INP_FILL
    computed = [
        ("年間受診延べ(回)", "=(B3*B4*B5+B3*(1-B4)*B6)*12"),
        ("院外処方プール(枚)", "=B12*B7*B8"),
        ("面競合の実効パワー", "=SUM(E20:E500)"),
        ("シェア", "=B9/(1+B14)"),
        ("獲得（年間・枚）", "=B13*B15"),
        ("獲得（月間・枚）", "=B16/12"),
    ]
    for k, (lab, f) in enumerate(computed):
        rr = 12 + k
        ws.cell(row=rr, column=1, value=lab)
        c = ws.cell(row=rr, column=2, value=f)
        c.fill = _CALC_FILL
        if rr in (16, 17):
            c.font = _BOLD
    for j, htxt in enumerate(["競合薬局名", "候補地から(m)", "実績(枚)", "面=1/門前=0", "重み(自動)"], start=1):
        c = ws.cell(row=19, column=j, value=htxt)
        c.font = _HDR
        c.fill = _HDR_FILL
    for k, cl in enumerate(r["classified"]):
        rr = 20 + k
        eff = r["override"].get(cl["key"], cl["auto_menkata"])
        ws.cell(row=rr, column=1, value=cl["name"])
        ws.cell(row=rr, column=2, value=round(cl["d_cand"])).fill = _INP_FILL
        ws.cell(row=rr, column=3, value=int(cl["rx"]) if cl["rx"] else 0).fill = _INP_FILL
        ws.cell(row=rr, column=4, value=(1 if eff else 0)).fill = _INP_FILL
        ws.cell(row=rr, column=5,
                value=f"=IF(D{rr}=1,IF(C{rr}>0,C{rr}/12000,1)*EXP(-B{rr}/$B$10),0)").fill = _CALC_FILL
    for col, w in zip("ABCDE", [30, 14, 12, 14, 14]):
        ws.column_dimensions[col].width = w


def _build_medical_sheet(wb, r):
    hp = r["hp"]
    ws = wb.create_sheet(f"医療機関_{_sheet_name(r['label'])}")
    ws["A1"] = f"① 医療機関ベース（ハフ競合按分）  {r['label']}  {r['name']}"
    ws["A1"].font = Font(bold=True, size=12)
    ws["A2"] = "【黄色=編集可。取り分率・獲得は自動再計算。『競合の重み合計』はアプリ計算値（λ変更は自店側のみ反映）】"
    ws["A2"].font = Font(italic=True, size=9, color="B45309")
    for k, (lab, val) in enumerate([("距離減衰λ(m)", hp.lambda_m), ("門前ブースト", hp.monzen_boost),
                                    ("候補店の引力", hp.candidate_attractiveness)]):
        ws.cell(row=4 + k, column=1, value=lab)
        ws.cell(row=4 + k, column=2, value=val)
    heads = ["クリニック名", "距離(m)", "年間院外処方(原資)", "自店の重み", "競合の重み合計", "取り分率", "獲得(枚/年)"]
    for j, htxt in enumerate(heads, start=1):
        c = ws.cell(row=9, column=j, value=htxt)
        c.font = _HDR
        c.fill = _HDR_FILL2
    rr = 10
    for row in r["huff_rows"]:
        ws.cell(row=rr, column=1, value=row["clinic"])
        ws.cell(row=rr, column=2, value=round(row["dist"]))
        ws.cell(row=rr, column=3, value=round(row["pool"])).fill = _INP_FILL
        ws.cell(row=rr, column=4, value=round(row["self_w"], 4)).fill = _INP_FILL
        ws.cell(row=rr, column=5, value=round(row["comp_w"], 4)).fill = _INP_FILL
        ws.cell(row=rr, column=6, value=f"=IF((D{rr}+E{rr})>0,D{rr}/(D{rr}+E{rr}),0)").fill = _CALC_FILL
        ws.cell(row=rr, column=7, value=f"=C{rr}*F{rr}").fill = _CALC_FILL
        rr += 1
    ws.cell(row=rr, column=6, value="合計（＝①予測）").font = _BOLD
    tot = ws.cell(row=rr, column=7, value=f"=SUM(G10:G{rr-1})")
    tot.font = _BOLD
    ws["A7"] = "ハフ按分による予測（年間・枚）"
    ws["B7"] = f"=G{rr}"
    ws["B7"].font = _BOLD
    ws["C7"] = "（月間）"
    ws["D7"] = f"=B7/12"
    for col, w in zip("ABCDEFG", [28, 10, 18, 12, 14, 10, 14]):
        ws.column_dimensions[col].width = w


def _build_summary_sheet(ws, results):
    """お客様提示用のサマリー（比較表）。"""
    ink = "16211E"
    teal = "0F766E"
    thin = Side(style="thin", color="D7DEDB")
    med_thin = Side(style="thin", color="B9C4C0")
    center = Alignment(horizontal="center", vertical="center")
    right = Alignment(horizontal="right", vertical="center")
    left = Alignment(horizontal="left", vertical="center")

    ncols = 8
    last_col = get_column_letter(ncols)
    # タイトル帯
    ws.merge_cells(f"A1:{last_col}1")
    t = ws["A1"]
    t.value = "処方箋獲得予測  ─  出店候補地の比較"
    t.font = Font(bold=True, size=18, color=teal)
    t.alignment = left
    ws.row_dimensions[1].height = 30
    ws.merge_cells(f"A2:{last_col}2")
    s = ws["A2"]
    s.value = "① 医療機関ベース（ハフ競合按分）  と  ② 集客ベース（来店客数）  の2つの独立した推計で比較"
    s.font = Font(size=10, color="5B6662")
    s.alignment = left
    ws.row_dimensions[2].height = 18

    # 2段ヘッダー（グループ見出し＋小見出し）
    hrow1, hrow2 = 4, 5
    groups = [("", 1), ("", 1), ("", 1), ("① 医療機関ベース", 2), ("② 集客ベース", 2), ("", 1)]
    col = 1
    for title, span in groups:
        if title and span > 1:
            ws.merge_cells(start_row=hrow1, start_column=col, end_row=hrow1, end_column=col + span - 1)
            cc = ws.cell(row=hrow1, column=col, value=title)
            cc.font = Font(bold=True, color="FFFFFF", size=10)
            cc.fill = PatternFill("solid", fgColor=teal)
            cc.alignment = center
            for k in range(span):
                ws.cell(row=hrow1, column=col + k).fill = PatternFill("solid", fgColor=teal)
        col += span
    subheads = ["ラベル", "店舗名 / メモ", "住所", "年間(枚)", "月間(枚)", "年間(枚)", "月間(枚)", "予測レンジ(年)"]
    for j, htxt in enumerate(subheads, start=1):
        cc = ws.cell(row=hrow2, column=j, value=htxt)
        cc.font = Font(bold=True, color="FFFFFF", size=10)
        cc.fill = PatternFill("solid", fgColor="16897E")
        cc.alignment = center
        cc.border = Border(left=thin, right=thin, top=thin, bottom=med_thin)

    # 勝者（集客 or 医療機関が最大）をハイライト
    def keyval(r):
        return r["foot_total"] if r["foot_total"] is not None else (r["med_total"] or 0)
    best_label = max(results, key=keyval)["label"] if results else None

    r0 = hrow2 + 1
    for i, r in enumerate(results):
        row = r0 + i
        ff = f"集客_{_sheet_name(r['label'])}"
        md = f"医療機関_{_sheet_name(r['label'])}"
        vals = {
            1: r["label"], 2: r["name"], 3: r["addr"],
            4: f"='{md}'!B7", 5: f"='{md}'!D7",
        }
        if r["foot_total"] is not None:
            vals[6] = f"='{ff}'!B16"
            vals[7] = f"='{ff}'!B17"
        vals[8] = (f'=IF(AND(ISNUMBER(D{row}),ISNUMBER(F{row})),'
                   f'TEXT(MIN(D{row},F{row}),"#,##0")&"〜"&TEXT(MAX(D{row},F{row}),"#,##0"),"—")')
        is_best = (r["label"] == best_label)
        base_fill = PatternFill("solid", fgColor="E8F3F1") if is_best else (
            PatternFill("solid", fgColor="F6F8F7") if i % 2 else None)
        for j in range(1, ncols + 1):
            cell = ws.cell(row=row, column=j, value=vals.get(j))
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            if base_fill:
                cell.fill = base_fill
            if j == 1:
                cell.font = Font(bold=True, size=11, color=teal)
                cell.alignment = center
            elif j in (4, 5, 6, 7):
                cell.number_format = "#,##0"
                cell.alignment = right
                cell.font = Font(size=11, color=ink)
            elif j == 8:
                cell.alignment = center
            else:
                cell.alignment = left
        ws.row_dimensions[row].height = 22

    note = r0 + len(results) + 1
    ws.merge_cells(start_row=note, start_column=1, end_row=note, end_column=ncols)
    n = ws.cell(row=note, column=1,
                value="※ ①と②は同じ枚数を別データから見積もった2つの推計です（足し算しません）。"
                      "詳細な内訳は各「集客_」「医療機関_」シートを参照。")
    n.font = Font(italic=True, size=9, color="7A8481")
    ws.merge_cells(start_row=note + 1, start_column=1, end_row=note + 1, end_column=ncols)
    ws.cell(row=note + 1, column=1,
            value="※ 緑の行は①②の予測が最大の候補地。").font = Font(italic=True, size=9, color="7A8481")

    for col, w in zip("ABCDEFGH", [8, 24, 34, 13, 12, 13, 12, 18]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A6"
    ws.sheet_view.showGridLines = False


def build_excel(results):
    wb = Workbook()
    ws = wb.active
    ws.title = "比較サマリー"
    _build_summary_sheet(ws, results)
    for r in results:
        _build_footfall_sheet(wb, r)
        _build_medical_sheet(wb, r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ════════════════════════════════ メイン ════════════════════════════════
st.title("🏪 複数店舗（A / B / C）比較 — 処方箋獲得予測")
st.caption(
    "同じスーパーの複数の出店候補地をまとめて分析し、2トラック（医療機関ベース × 集客ベース）で"
    "比較します。ロジックの内訳はブラウザで確認でき、数式入りExcelにも書き出せます。"
)

st.markdown("#### 1. 候補地を入力")
st.caption("候補地ごとに ラベル・店舗名/メモ・住所・月間ユニーク客数（集客ベース用）を入力してください。行は追加できます。")

if "cand_df" not in st.session_state:
    st.session_state["cand_df"] = pd.DataFrame([
        {"ラベル": "A", "店舗名/メモ": "", "住所": "", "月間ユニーク客数": 0},
        {"ラベル": "B", "店舗名/メモ": "", "住所": "", "月間ユニーク客数": 0},
        {"ラベル": "C", "店舗名/メモ": "", "住所": "", "月間ユニーク客数": 0},
    ])

cand_edited = st.data_editor(
    st.session_state["cand_df"], num_rows="dynamic", use_container_width=True, key="cand_editor",
    column_config={
        "ラベル": st.column_config.TextColumn("ラベル", width="small"),
        "店舗名/メモ": st.column_config.TextColumn("店舗名/メモ"),
        "住所": st.column_config.TextColumn("住所", width="large"),
        "月間ユニーク客数": st.column_config.NumberColumn("月間ユニーク客数", min_value=0, step=500),
    },
)

run = st.button("▶ 全候補地を分析する", type="primary")

if run:
    scraper = get_scraper()
    assumptions = PredictionAssumptions()
    targets = [row for _, row in cand_edited.iterrows() if str(row.get("住所", "")).strip()]
    if not targets:
        st.warning("住所を1件以上入力してください。")
        st.stop()
    st.info(f"{len(targets)}件の候補地を順番に分析します（1件あたり数分。混雑時は10分以上かかる場合があります）。")
    raws = []
    overall = st.progress(0.0, text="開始…")
    for i, row in enumerate(targets):
        label = str(row.get("ラベル") or f"#{i+1}").strip()
        addr = str(row["住所"]).strip()
        uni = float(row.get("月間ユニーク客数") or 0)
        overall.progress(i / len(targets), text=f"[{label}] {addr} を分析中… ({i+1}/{len(targets)})")
        log = []
        prog = st.progress(0.0, text=f"[{label}] 収集中…")
        try:
            med, ph, clat, clon = run_analysis(
                addr, int(radius_m), gate_m, int(max_detail), log, prog,
                assumptions=assumptions, polygons=[], exclude_outside_med=True,
            )
            prog.empty()
        except Exception as e:
            prog.empty()
            st.error(f"[{label}] 分析に失敗: {e}")
            continue
        if clat is None:
            st.error(f"[{label}] 住所の座標が取得できませんでした：{addr}")
            continue
        raws.append({"label": label, "name": str(row.get("店舗名/メモ") or ""),
                     "addr": addr, "uni": uni, "clat": clat, "clon": clon, "med": med, "ph": ph})
    overall.progress(1.0, text="完了")
    overall.empty()
    st.session_state["multi_raw"] = raws
    st.session_state["mk_multi"] = {}   # 面/門前の手修正はリセット


# ── 結果の比較表示（毎回、現在の設定＋手修正で再計算） ──────────────────────────
raws = st.session_state.get("multi_raw", [])
if raws:
    computed = [compute_candidate(r) for r in raws]

    st.markdown("#### 2. 比較結果")
    rows = []
    for c in computed:
        med, foot = c["med_total"], c["foot_total"]
        vals = [v for v in (med, foot) if v is not None]
        rng = f"{min(vals):,.0f}〜{max(vals):,.0f}" if len(vals) == 2 else "—"
        rows.append({
            "ラベル": c["label"], "店舗名/メモ": c["name"], "住所": c["addr"][:24],
            "① 医療機関(年)": round(med) if med is not None else None,
            "① 医療機関(月)": round(med / 12) if med is not None else None,
            "② 集客(年)": round(foot) if foot is not None else None,
            "② 集客(月)": round(foot / 12) if foot is not None else None,
            "予測レンジ(年)": rng, "面競合数": c["comp_n"], "寄与医療機関数": len(c["huff_rows"]),
        })
    st.dataframe(
        pd.DataFrame(rows), hide_index=True, use_container_width=True,
        column_config={cc: st.column_config.NumberColumn(cc, format="%d 枚")
                       for cc in ["① 医療機関(年)", "① 医療機関(月)", "② 集客(年)", "② 集客(月)"]},
    )
    ranked = sorted(computed, key=lambda c: (c["foot_total"] or c["med_total"] or 0), reverse=True)
    st.success(f"🏆 最大の候補地： **{ranked[0]['label']}**"
               f"（{ranked[0]['name'] or ranked[0]['addr'][:20]}）")

    # ── 3. 候補地ごとの詳細（ロジックの内訳＋面/門前の目視修正） ──────────────
    st.markdown("#### 3. 候補地ごとの詳細（ロジックの内訳）")
    sel = st.selectbox("詳細を見る候補地", [c["label"] for c in computed],
                       format_func=lambda x: f"{x}　" + next((c["name"] or c["addr"][:16]
                                                              for c in computed if c["label"] == x), ""))
    c = next(x for x in computed if x["label"] == sel)

    m1, m2, m3 = st.columns(3)
    m1.metric("① 医療機関ベース（ハフ）", f"{c['med_total']:,.0f} 枚/年",
              f"月 {c['med_total']/12:,.0f} 枚")
    if c["foot_total"] is not None:
        m2.metric("② 集客ベース", f"{c['foot_total']:,.0f} 枚/年", f"月 {c['foot_total']/12:,.0f} 枚")
    else:
        m2.metric("② 集客ベース", "未入力")
    m3.metric("面競合数 / 除外", f"{c['comp_n']} / {c['comp_excluded']}")

    st.markdown("##### ① 医療機関ベース：ハフの取り分内訳")
    st.caption("各クリニックが出す処方箋（原資）を、自店の重み ÷（自店の重み＋競合の重み合計）の"
               "取り分率で獲得します。合計＝①予測。")
    hb_df = pd.DataFrame([{
        "医療機関": row["clinic"], "距離(m)": round(row["dist"]),
        "年間院外処方(原資)": round(row["pool"]),
        "自店の重み": round(row["self_w"], 3), "競合の重み合計": round(row["comp_w"], 3),
        "取り分率": round(row["share"], 3), "獲得(枚/年)": round(row["captured"]),
    } for row in c["huff_rows"]])
    st.dataframe(hb_df, hide_index=True, use_container_width=True, column_config={
        "取り分率": st.column_config.NumberColumn("取り分率", format="%.3f"),
        "獲得(枚/年)": st.column_config.NumberColumn("獲得(枚/年)", format="%d 枚"),
    })

    if c["foot"]:
        st.markdown("##### ② 集客ベース：内訳")
        fo, fp = c["foot"], c["fp"]
        st.markdown(
            f"- 月間ユニーク客 {fp.unique_customers_monthly:,.0f}人"
            f"（65+ {fo['u65']:,.0f} / 65− {fo['u_under']:,.0f}）\n"
            f"- 年間受診延べ {fo['annual_visits']:,.0f}回 → 院外処方プール {fo['rx_pool']:,.0f}枚\n"
            f"- 利用率 {fp.use_rate:.1%} ÷ (面競合の実効パワー {c['comp_power']:.1f}"
            f"〔面{c['comp_n']}店・距離減衰λ={fp.competitor_decay_m:.0f}m〕 + 1) = シェア {fo['share']:.2%}\n"
            f"- **獲得 = {fo['total']:,.0f} 枚/年**"
        )

    st.markdown("##### 面／門前の判定（目視で修正できます）")
    st.caption("『判定』を面/門前に変えると、この候補地の集客ベース（面競合）と上の比較表・Excelに即反映されます。")
    mk_all = st.session_state.setdefault("mk_multi", {})
    mk = mk_all.setdefault(sel, {})
    if st.button("この候補地の手修正をクリア", key=f"reset_{sel}"):
        mk_all[sel] = {}
        st.rerun()
    auto_map = {r["key"]: r["auto_menkata"] for r in c["classified"]}
    df_mk = pd.DataFrame([{
        "薬局": r["name"], "候補地から(m)": round(r["d_cand"]),
        "最寄りクリニック(m)": round(r["nearest_clinic"]) if r["nearest_clinic"] < 1e8 else None,
        "実績(枚)": r["rx"], "自動判定": "面" if r["auto_menkata"] else "門前",
        "判定": "面" if mk.get(r["key"], r["auto_menkata"]) else "門前", "_key": r["key"],
    } for r in c["classified"]])
    edited_mk = st.data_editor(
        df_mk, hide_index=True, use_container_width=True, key=f"mk_editor_{sel}",
        disabled=["薬局", "候補地から(m)", "最寄りクリニック(m)", "実績(枚)", "自動判定", "_key"],
        column_config={"判定": st.column_config.SelectboxColumn("判定", options=["面", "門前"], width="small")},
    )
    new_ov = {}
    for _, row in edited_mk.iterrows():
        is_men = (row["判定"] == "面")
        if is_men != auto_map.get(row["_key"], True):
            new_ov[row["_key"]] = is_men
    if new_ov != mk:
        mk_all[sel] = new_ov
        st.rerun()

    with st.expander("🏥 医療機関の一覧"):
        st.dataframe(pd.DataFrame([{
            "医療機関": f.name, "距離(m)": int(f.distance_m) if f.distance_m is not None else None,
            "外来(人/日)": f.daily_outpatients, "院内外": f.rx_summary, "種別": f.facility_category,
        } for f in sorted(c["med"], key=lambda x: x.distance_m or 9e9)]),
            hide_index=True, use_container_width=True)
    with st.expander("💊 薬局の一覧"):
        st.dataframe(pd.DataFrame([{
            "薬局": p.name, "距離(m)": int(p.distance_m) if p.distance_m is not None else None,
            "実績(枚/年)": p.annual_rx_count,
        } for p in sorted(c["ph"], key=lambda x: x.distance_m or 9e9)]),
            hide_index=True, use_container_width=True)

    # ── 4. Excel ─────────────────────────────────────────────────────────────
    st.markdown("#### 4. 数式入りExcelで書き出し")
    st.caption("集客シートは客数・競合・係数、医療機関シートは原資・重みを編集すると、"
               "獲得枚数がExcelの数式で自動再計算されます（ブラウザを閉じても手元で編集可能）。")
    st.download_button(
        "📊 比較Excel（数式入り）をダウンロード",
        data=build_excel(computed), file_name="処方箋予測_複数店舗比較.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
