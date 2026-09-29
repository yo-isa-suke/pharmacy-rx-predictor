# -*- coding: utf-8 -*-
"""
調剤ツール ポータル（3つのツールを1つのURLで使う入口）
============================================================
画面左のメニューで「出店分析ツール／医療機関ファインダー／薬局ファインダー」を切り替える。
各ツールの本体は tools/ フォルダの最新版。更新は tools/ のファイルを差し替えて push するだけで、
このアプリを使っている全員に自動で反映される（Streamlit側の操作は不要）。

同じタブの中でページを切り替えると、実行中の分析はそこで止まる（Streamlitの仕様）。
出店分析ツールを動かしながらファインダーも使えるよう、左メニューに「別タブで開く」リンクを置いた。
タブが違えば別々の画面として同時に動く。
"""
import streamlit as st

PAGES = [
    # (ファイル, 表示名, アイコン, URLのパス)
    ("tools/薬局出店分析ツール.py", "出店分析ツール", "🏪", "analysis"),
    ("tools/medical_finder.py", "医療機関ファインダー", "🏥", "medical"),
    ("tools/pharmacy_finder.py", "薬局ファインダー", "💊", "pharmacy"),
]

pages = [st.Page(f, title=t, icon=i, url_path=u, default=(n == 0))
         for n, (f, t, i, u) in enumerate(PAGES)]
current = st.navigation(pages)

# 別タブで開くリンク（出店分析の実行中に、ファインダーを並行して使うため）
links = " ".join(
    f'<a href="/{"" if n == 0 else u}" target="_blank" rel="noopener" '
    f'style="display:block;margin:2px 0;font-size:0.85rem;text-decoration:none;">{i} {t} ↗</a>'
    for n, (_, t, i, u) in enumerate(PAGES)
)
st.sidebar.markdown(
    "<div style='font-size:0.8rem;color:gray;margin-top:-4px;'>別タブで開く（同時に動かせます）</div>"
    + links,
    unsafe_allow_html=True,
)
st.sidebar.divider()

current.run()
