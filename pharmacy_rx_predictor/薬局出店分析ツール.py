# -*- coding: utf-8 -*-
# 旧URL互換の入口（2026-09-29 のリポジトリ整理で残したもの）。
# 本体は tools/薬局出店分析ツール.py（最新版）。既存のStreamlitアプリのURLを変えずに動かすためだけに置いている。
# チームが共通URL（tools_portal.py のアプリ）に移り終えたら、このファイルは削除してよい。
import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/薬局出店分析ツール.py"), run_name="__main__")
