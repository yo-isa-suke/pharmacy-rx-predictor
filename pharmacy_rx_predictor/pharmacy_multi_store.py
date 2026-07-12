# -*- coding: utf-8 -*-
"""
処方箋予測 — 複数店舗（A/B/C…）比較ツール
====================================================
とあるスーパーの複数の出店候補地（A点・B点・C点…）をまとめて分析し、
「医療機関ベース（ハフ按分）」と「集客ベース（来店客数）」の2トラックで比較する。

- 既存の「260702_Prescription Analysis_v2.py」のモデル/スクレイパーをそのまま再利用
  （既存ファイルは一切変更しない。UIを起動せずに関数だけ読み込む）。
- 結果は「数式入りExcel(.xlsx)」で書き出せる。ユニーク客数・競合・係数を Excel上で
  編集すると、集客ベースの予測枚数が自動で再計算される（ブラウザを閉じても手元で編集可能）。
"""
import io
import os
import re

import pandas as pd
import streamlit as st
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

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
GeocoderService = M["GeocoderService"]
run_analysis = M["run_analysis"]
PredictionAssumptions = M["PredictionAssumptions"]
HuffParams = M["HuffParams"]
FootfallParams = M["FootfallParams"]
compute_capture_prediction = M["compute_capture_prediction"]
compute_huff_prediction = M["compute_huff_prediction"]
compute_footfall_prediction = M["compute_footfall_prediction"]
classify_menkata = M["classify_menkata"]
footfall_competitor_power = M["footfall_competitor_power"]
inflow_band_label = M["inflow_band_label"]
FORMAT_PRESETS = M["FORMAT_PRESETS"]


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
                                help="最寄りクリニックがこの距離以内の薬局は門前として面競合から除外。")
    ff_decay = c7.number_input("面競合の距離減衰λ(m)", 0, 3000, 1000, 100,
                               help="遠い面競合を弱く数える。小さいほど自店シェア↑。")
    ff_main = st.number_input("メイン薬局しきい値(枚/年)", 0, 100000, 15000, 1000,
                              help="実績がこの値以上の薬局は面競合から除外。0で無効。")

    with st.expander("⚙️ 詳細設定（ハフ按分・通常は変更不要）", expanded=False):
        huff_lambda = st.slider("距離減衰 λ (m)", 150, 900, 250, 50)
        huff_boost = st.slider("門前ブースト", 1.0, 15.0, 8.0, 0.5)
        huff_candA = st.number_input("候補店の引力（大型店は上げる）", 0.2, 10.0, 1.0, 0.1)


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


# ── 数式入りExcel（編集で自動再計算） ───────────────────────────────────────────
_HDR = Font(bold=True, color="FFFFFF")
_HDR_FILL = PatternFill("solid", fgColor="0F766E")
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
    ]  # B3..B10
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
    ]  # B12..B17
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
        ws.cell(row=rr, column=1, value=cl["name"])
        ws.cell(row=rr, column=2, value=round(cl["d_cand"])).fill = _INP_FILL
        ws.cell(row=rr, column=3, value=int(cl["rx"]) if cl["rx"] else 0).fill = _INP_FILL
        ws.cell(row=rr, column=4, value=(1 if cl["auto_menkata"] else 0)).fill = _INP_FILL
        ws.cell(row=rr, column=5,
                value=f"=IF(D{rr}=1,IF(C{rr}>0,C{rr}/12000,1)*EXP(-B{rr}/$B$10),0)").fill = _CALC_FILL
    for col, w in zip("ABCDE", [30, 14, 12, 14, 14]):
        ws.column_dimensions[col].width = w


def _build_medical_sheet(wb, r):
    ws = wb.create_sheet(f"医療機関_{_sheet_name(r['label'])}")
    ws["A1"] = f"① 医療機関ベース  {r['label']}  {r['name']}"
    ws["A1"].font = Font(bold=True, size=12)
    ws["A3"] = "ハフ按分による予測（アプリ算出・年間）"
    ws["B3"] = round(r["med_total"]) if r["med_total"] is not None else None
    ws["B3"].font = _BOLD
    ws["A4"] = "（月間）"
    ws["B4"] = "=B3/12"
    ws["A6"] = "※下表は競合按分なしの『積み上げ』内訳（編集用の参考）。①の予測値は上のB3（ハフ按分）です。"
    ws["A6"].font = Font(italic=True, size=9, color="B45309")
    for j, htxt in enumerate(["医療機関", "距離(m)", "外来(人/日)", "年間診療日数", "係数", "流入率", "獲得(枚/年)"], start=1):
        c = ws.cell(row=8, column=j, value=htxt)
        c.font = _HDR
        c.fill = _HDR_FILL
    rr = 9
    for f in sorted(r["med_facs"], key=lambda x: (x.captured_rx or -1), reverse=True):
        if not f.captured_rx:
            continue
        op = (round(f.annual_op_visits / f.annual_op_days_used)
              if (f.annual_op_visits and f.annual_op_days_used) else 0)
        ws.cell(row=rr, column=1, value=f.name)
        ws.cell(row=rr, column=2, value=int(f.distance_m) if f.distance_m is not None else None)
        ws.cell(row=rr, column=3, value=op).fill = _INP_FILL
        ws.cell(row=rr, column=4, value=round(f.annual_op_days_used or 0)).fill = _INP_FILL
        ws.cell(row=rr, column=5, value=round(f.external_rx_factor, 3)).fill = _INP_FILL
        ws.cell(row=rr, column=6, value=round(f.inflow_rate, 3)).fill = _INP_FILL
        ws.cell(row=rr, column=7, value=f"=C{rr}*D{rr}*E{rr}*F{rr}").fill = _CALC_FILL
        rr += 1
    ws.cell(row=rr, column=6, value="合計(積み上げ)").font = _BOLD
    ws.cell(row=rr, column=7, value=f"=SUM(G9:G{rr-1})").font = _BOLD
    for col, w in zip("ABCDEFG", [28, 10, 12, 14, 8, 10, 14]):
        ws.column_dimensions[col].width = w


def build_excel(results):
    wb = Workbook()
    ws = wb.active
    ws.title = "比較サマリー"
    ws["A1"] = "処方箋獲得予測 — 複数店舗比較"
    ws["A1"].font = Font(bold=True, size=14)
    heads = ["ラベル", "店舗名/メモ", "住所", "① 医療機関(年)", "(月)", "② 集客(年)", "(月)", "予測レンジ(年)"]
    for j, htxt in enumerate(heads, start=1):
        c = ws.cell(row=3, column=j, value=htxt)
        c.font = _HDR
        c.fill = _HDR_FILL
    for i, r in enumerate(results):
        row = 4 + i
        ff = f"集客_{_sheet_name(r['label'])}"
        ws.cell(row=row, column=1, value=r["label"])
        ws.cell(row=row, column=2, value=r["name"])
        ws.cell(row=row, column=3, value=r["addr"])
        ws.cell(row=row, column=4,
                value=round(r["med_total"]) if r["med_total"] is not None else None)
        ws.cell(row=row, column=5, value=f"=IF(ISNUMBER(D{row}),D{row}/12,\"\")")
        if r["foot_total"] is not None:
            ws.cell(row=row, column=6, value=f"='{ff}'!B16")
            ws.cell(row=row, column=7, value=f"='{ff}'!B17")
        ws.cell(row=row, column=8,
                value=(f'=IF(AND(ISNUMBER(D{row}),ISNUMBER(F{row})),'
                       f'TEXT(MIN(D{row},F{row}),"#,##0")&"〜"&TEXT(MAX(D{row},F{row}),"#,##0"),"—")'))
    for col, w in zip("ABCDEFGH", [6, 22, 34, 15, 10, 15, 10, 18]):
        ws.column_dimensions[col].width = w
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
    "比較します。数字を編集できる **数式入りExcel** で書き出せます。"
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
    hp = make_hp()
    targets = [r for _, r in cand_edited.iterrows() if str(r.get("住所", "")).strip()]
    if not targets:
        st.warning("住所を1件以上入力してください。")
        st.stop()
    st.info(f"{len(targets)}件の候補地を順番に分析します（1件あたり数分。混雑時は10分以上かかる場合があります）。")
    results = []
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
        # 医療機関ベース（ハフ）＋積み上げ内訳
        summ = compute_capture_prediction(med, assumptions)
        huff = compute_huff_prediction(med, ph, clat, clon, assumptions, hp)
        # 集客ベース
        fp = make_fp(uni)
        classified = classify_menkata(ph, med, clat, clon,
                                      monzen_dist=fp.menkata_monzen_dist,
                                      main_rx_threshold=fp.menkata_main_rx, reach_m=hp.reach_m)
        cpow, cn, cexc = footfall_competitor_power(classified, {}, fp.competitor_decay_m, hp.national_avg_rx)
        foot = compute_footfall_prediction(fp, cpow)
        results.append({
            "label": label, "name": str(row.get("店舗名/メモ") or ""), "addr": addr,
            "lat": clat, "lon": clon, "uni": uni,
            "med_total": huff["total"], "foot_total": (foot["total"] if foot else None),
            "comp_n": cn, "comp_excluded": cexc, "comp_power": cpow,
            "n_contributing": summ["n_contributing"],
            "med_facs": med, "classified": classified, "fp": fp,
        })
    overall.progress(1.0, text="完了")
    overall.empty()
    st.session_state["multi_results"] = results


# ── 結果の比較表示 ─────────────────────────────────────────────────────────────
results = st.session_state.get("multi_results", [])
if results:
    st.markdown("#### 2. 比較結果")
    rows = []
    for r in results:
        med = r["med_total"]
        foot = r["foot_total"]
        vals = [v for v in (med, foot) if v is not None]
        rng = f"{min(vals):,.0f}〜{max(vals):,.0f}" if len(vals) == 2 else "—"
        rows.append({
            "ラベル": r["label"], "店舗名/メモ": r["name"], "住所": r["addr"][:24],
            "① 医療機関(年)": round(med) if med is not None else None,
            "① 医療機関(月)": round(med / 12) if med is not None else None,
            "② 集客(年)": round(foot) if foot is not None else None,
            "② 集客(月)": round(foot / 12) if foot is not None else None,
            "予測レンジ(年)": rng,
            "面競合数": r["comp_n"], "寄与医療機関数": r["n_contributing"],
        })
    df = pd.DataFrame(rows)
    st.dataframe(
        df, hide_index=True, use_container_width=True,
        column_config={c: st.column_config.NumberColumn(c, format="%d 枚")
                       for c in ["① 医療機関(年)", "① 医療機関(月)", "② 集客(年)", "② 集客(月)"]},
    )
    # 簡単なランキング
    ranked = sorted(results, key=lambda r: (r["foot_total"] or r["med_total"] or 0), reverse=True)
    best = ranked[0]
    st.success(f"🏆 集客ベース（または医療機関ベース）が最大の候補地： **{best['label']}**"
               f"（{best['name'] or best['addr'][:20]}）")

    st.markdown("#### 3. 数式入りExcelで書き出し")
    st.caption("ユニーク客数・競合・係数を Excel上で編集すると、集客ベースの予測が自動で再計算されます"
               "（ブラウザを閉じても手元で編集・保存できます）。")
    xlsx = build_excel(results)
    st.download_button(
        "📊 比較Excel（数式入り）をダウンロード",
        data=xlsx, file_name="処方箋予測_複数店舗比較.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
    )
