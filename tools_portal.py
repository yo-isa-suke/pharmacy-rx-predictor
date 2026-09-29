# -*- coding: utf-8 -*-
"""
調剤ツール ポータル（3つのツールを1つのURLで使う入口）
============================================================
画面左のメニューで「医療機関ファインダー／薬局ファインダー／出店分析ツール」を切り替える。
各ツールの本体は tools/ フォルダの最新版。更新は tools/ のファイルを差し替えて push するだけで、
このアプリを使っている全員に自動で反映される（Streamlit側の操作は不要）。
"""
import streamlit as st

pages = [
    st.Page("tools/medical_finder.py", title="医療機関ファインダー", icon="🏥",
            url_path="medical", default=True),
    st.Page("tools/pharmacy_finder.py", title="薬局ファインダー", icon="💊", url_path="pharmacy"),
    st.Page("tools/薬局出店分析ツール.py", title="出店分析ツール", icon="🏪", url_path="analysis"),
]
st.navigation(pages).run()
