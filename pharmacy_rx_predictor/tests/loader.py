# -*- coding: utf-8 -*-
"""Streamlit アプリのロジック部分だけを（UI描画を実行せずに）読み込むローダ。"""
import io, types, sys
import stubs; stubs.install()

def load(path, module_name, cut_marker=None):
    src = io.open(path, encoding="utf-8").read()
    if cut_marker:
        i = src.index(cut_marker)
        src = src[:i]
    mod = types.ModuleType(module_name)
    mod.__file__ = path
    sys.modules[module_name] = mod
    exec(compile(src, path, "exec"), mod.__dict__)
    return mod
