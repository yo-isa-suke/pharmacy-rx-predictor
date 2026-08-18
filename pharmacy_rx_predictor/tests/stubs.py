"""Import the Streamlit apps headlessly by stubbing the UI-only modules."""
import sys, types

class _Any:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return _Any()
    def __getattr__(self, n): return _Any()
    def __enter__(self): return _Any()
    def __exit__(self, *a): return False
    def __iter__(self): return iter([_Any(), _Any(), _Any(), _Any(), _Any()])
    def __bool__(self): return False
    def __contains__(self, k): return False
    def __setitem__(self, k, v): pass
    def __getitem__(self, k): return _Any()
    def __int__(self): return 0
    def __float__(self): return 0.0
    def __index__(self): return 0
    def __len__(self): return 0
    def __eq__(self, o): return False
    def __hash__(self): return 0

def install():
    for name in ("streamlit", "folium", "folium.plugins", "streamlit_folium",
                 "pandas", "openpyxl", "openpyxl.styles", "openpyxl.utils",
                 "openpyxl.chart", "openpyxl.formatting", "openpyxl.formatting.rule",
                 "docx"):
        if name in sys.modules:
            continue
        m = types.ModuleType(name)
        m.__getattr__ = lambda n: _Any()
        m.__path__ = []
        sys.modules[name] = m
    class _State(dict):
        def __getattr__(self, n):
            try: return self[n]
            except KeyError: return _Any()
        def __setattr__(self, n, v): self[n] = v
    sys.modules["streamlit"].session_state = _State()
    def _cols(spec, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [_Any() for _ in range(n)]
    sys.modules["streamlit"].columns = _cols
    sys.modules["streamlit"].tabs = lambda names, **k: [_Any() for _ in names]
    sys.modules["streamlit"].cache_resource = lambda *a, **k: (lambda f: f)
    sys.modules["streamlit"].cache_data = lambda *a, **k: (lambda f: f)
