# Cloudflare で v1.4 を公開する

`薬局出店分析ツール_v1.4.py` を **Cloudflare Containers** 上で動かし、ブラウザから
使えるようにするための設定一式です。手元で 3 コマンド実行すれば公開できます。

---

## 先に知っておいてほしいこと

| | |
|---|---|
| **料金** | Cloudflare Containers は **Workers 有料プラン（$5/月〜）が必須**です。無料プランでは動きません。これに加えてコンテナの起動時間ぶんが従量課金されます |
| **必要なもの** | 手元のPCで **Docker Desktop が起動していること**（イメージのビルドに使います）と Node.js |
| **停止と結果** | 30分アクセスが無いとコンテナは自動停止し、**画面に出ていた分析結果はメモリごと消えます**。残したい結果は必ず先に Excel に書き出してください |
| **同時利用** | インスタンスは1つに固定しています。複数人が同時に開いても大丈夫ですが、重い分析を同時に走らせると遅くなります |

> **無料で済ませたい場合**：Streamlit Community Cloud（GitHubリポジトリを繋ぐだけ・無料）
> でも同じアプリが動きます。Cloudflare にこだわらないならそちらのほうが手軽です。

---

## 手順

### 1. リポジトリを手元に用意する

```bash
git clone https://github.com/yo-isa-suke/pharmacy-rx-predictor.git
cd pharmacy-rx-predictor
git checkout claude/pharmacy-listing-tools-fix-mpxytc
npm install
```

### 2. Cloudflare にログインする

```bash
npx wrangler login
```

### 3. デプロイする

Docker Desktop が起動していることを確認してから:

```bash
npx wrangler deploy
```

初回はイメージのビルドとアップロードで **5〜15分**かかります（2回目以降は差分だけなので数分）。
完了すると `https://pharmacy-rx-predictor.<あなたのサブドメイン>.workers.dev` が表示されます。

### 4. パスワードを設定する（必須）

```bash
npx wrangler secret put APP_PASSWORD
```

**この設定をするまで、アプリは公開されません**（設定前にURLを開くと、その旨の案内が出ます）。
厚労省ナビィを叩くツールを誰でも使える状態でネットに置かないための、意図的な作りです。
設定後はデプロイし直さなくても数十秒で反映されます。

以上です。URL を開くとパスワード入力欄が出て、通ると v1.4 の画面が表示されます。

---

## もっとちゃんとアクセス制限したい場合

上のパスワードは「事故防止の最低限」です。社内の複数人で使うなら
**Cloudflare Access（Zero Trust）** を前に置くと、Google アカウント等での
SSO・メンバー単位の許可・アクセスログが使えます。

1. Cloudflare ダッシュボード → **Zero Trust** → **Access** → **Applications** → **Add an application**
2. **Self-hosted** を選び、ドメインに Worker の URL を指定
3. ポリシーで、許可するメールアドレスやドメイン（例：`@あなたの会社.co.jp`）を指定

Access を入れたあとは `APP_PASSWORD` は二重の鍵になります。外したい場合は
`npx wrangler secret delete APP_PASSWORD` ではなく、**Access を必ず先に有効化してから**
外してください（先に外すと一時的に無防備になります）。

---

## 独自ドメインで使う

`wrangler.jsonc` に追記して再デプロイします（そのドメインが Cloudflare で管理されている必要があります）。

```jsonc
"routes": [
  { "pattern": "rx.example.co.jp", "custom_domain": true }
]
```

---

## 構成

```
Dockerfile                  Streamlit v1.4 を動かすイメージ（linux/amd64）
pharmacy_rx_predictor/
  requirements-container.txt  版数を固定した依存（再ビルドで壊れないように）
wrangler.jsonc              Worker とコンテナの設定
src/index.ts                入口。パスワード認証 → コンテナへ中継（WebSocket対応）
```

Streamlit はブラウザとの通信に WebSocket を使うため、`src/index.ts` では
必ず `Container` の `fetch()` を経由させています（`containerFetch()` は WebSocket を
通しません）。また、ナビィ・OpenStreetMap・国土地理院へアクセスする必要があるため
`enableInternet = true` にしています。

---

## うまくいかないとき

| 症状 | 原因と対処 |
|---|---|
| `The Docker CLI is needed...` | Docker Desktop が起動していない。起動してから再実行 |
| `Containers are only available on the Workers Paid plan` | 有料プランへの変更が必要 |
| デプロイは成功するが画面が真っ白／繋がらない | コンテナ起動に30〜60秒かかることがある。少し待って再読み込み。改善しなければ `npx wrangler tail` でログを確認 |
| 分析の途中で切れる | メモリ不足の可能性。`wrangler.jsonc` の `instance_type` を `standard-2` に上げて再デプロイ |
| 医療機関・薬局が0件になる | ナビィ側の仕様変更の可能性。手元で `python3 pharmacy_rx_predictor/navii_health_check.py` を実行すると、どの前提が崩れたか分かります |

### 設定を変えたいとき

| やりたいこと | 変更する場所 |
|---|---|
| 停止までの待ち時間を変える | `src/index.ts` の `sleepAfter`（既定 `"30m"`） |
| メモリ・CPUを増やす | `wrangler.jsonc` の `instance_type`（`standard-1`〜`standard-4`） |
| ライブラリを更新する | `pharmacy_rx_predictor/requirements-container.txt`。更新後は `python3 pharmacy_rx_predictor/tests/test_e2e.py` を通してからデプロイ |
