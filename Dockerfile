# ─────────────────────────────────────────────────────────────────────────────
# 薬局 出店候補地 分析ツール v1.4 を Cloudflare Containers 上で動かすイメージ
#
# Cloudflare Containers は linux/amd64 のイメージしか受け付けないため、
# Apple Silicon (M1/M2/M3) のMacからビルドする場合も --platform を明示している。
# ─────────────────────────────────────────────────────────────────────────────
FROM --platform=linux/amd64 python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Tokyo

WORKDIR /app

# curl はコンテナのヘルスチェック確認用（デバッグ時にあると助かる）
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tzdata \
 && rm -rf /var/lib/apt/lists/*

# 依存だけ先に入れる（アプリ本体を変えてもここのレイヤーは再利用される＝ビルドが速い）
# 版数を固定した requirements-container.txt を使う。元の requirements.txt は「>=」指定のため、
# 再ビルドのたびに新版が入り、手元では動くのに公開版だけ壊れる、という事故が起きうる。
COPY requirements-container.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# アプリ本体。ファイル名が日本語のままだとCMDや各種ツールで扱いづらいので app.py に置く。
COPY ["pharmacy_rx_predictor/薬局出店分析ツール_v1.4.py", "./app.py"]

# Streamlit の設定。Worker 経由（リバースプロキシ配下）で動かすための最小限。
RUN mkdir -p /root/.streamlit && printf '%s\n' \
    '[server]' \
    'headless = true' \
    'address = "0.0.0.0"' \
    'port = 8501' \
    'enableCORS = false' \
    '# WebSocketの圧縮はプロキシ経由だと相性が悪いことがあるため無効化する' \
    'enableWebsocketCompression = false' \
    '# アップロード上限（Excel書き出しが主なので小さめで十分）' \
    'maxUploadSize = 50' \
    '' \
    '[browser]' \
    'gatherUsageStats = false' \
    '' \
    '[theme]' \
    'base = "light"' \
    > /root/.streamlit/config.toml

EXPOSE 8501

# Streamlit は /_stcore/health を返す。Worker 側の pingEndpoint がこれを見る。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
  CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "app.py"]
