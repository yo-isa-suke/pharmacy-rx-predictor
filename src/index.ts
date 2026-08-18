/**
 * 薬局 出店候補地 分析ツール v1.4 ―― Cloudflare Worker（コンテナへの入口）
 *
 * 役割は2つだけ:
 *   1. 共有パスワードによる簡易アクセス制限（誤って全世界に公開しないための保険）
 *   2. リクエストを Streamlit コンテナへそのまま中継する（WebSocketを含む）
 *
 * Streamlit はブラウザとの通信に WebSocket を使うため、必ず Container の fetch() を
 * 経由させる（containerFetch() は WebSocket を通さない）。
 */
import { Container, getContainer } from "@cloudflare/containers";

export interface Env {
	APP: DurableObjectNamespace<StreamlitContainer>;
	/** wrangler secret put APP_PASSWORD で設定する共有パスワード */
	APP_PASSWORD?: string;
}

export class StreamlitContainer extends Container<Env> {
	/** Streamlit の待ち受けポート（Dockerfile の設定と合わせる） */
	defaultPort = 8501;

	/**
	 * 無操作でこの時間が過ぎたらコンテナを停止する（課金を止める）。
	 * 1候補地の分析に1〜2分かかるため、短すぎると分析中に落ちる。
	 * 注意: 停止すると Streamlit のセッション（分析結果）はメモリごと消える。
	 *       結果を残したい場合は停止前にExcelを書き出しておくこと。
	 */
	sleepAfter = "30m";

	/**
	 * 厚労省ナビィ・OpenStreetMap・国土地理院へアクセスするため外向き通信は必須。
	 * （既定値も true だが、意図を明示するために書いている）
	 */
	enableInternet = true;

	/**
	 * 起動完了の判定に使うエンドポイント。
	 * 既定は "ping" だが Streamlit はそのパスを持たないため、
	 * Streamlit が用意しているヘルスチェック用のパスを指定する。
	 */
	pingEndpoint = "localhost/_stcore/health";
}

const COOKIE_NAME = "rx_auth";
const LOGIN_PATH = "/__login";

/** 文字列比較のタイミング差から情報が漏れないようにする */
function safeEqual(a: string, b: string): boolean {
	if (a.length !== b.length) return false;
	let diff = 0;
	for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
	return diff === 0;
}

/** パスワードから Cookie に入れる値（平文を保存しないため）を作る */
async function tokenFor(password: string): Promise<string> {
	const data = new TextEncoder().encode(`pharmacy-rx-predictor:v1.4:${password}`);
	const digest = await crypto.subtle.digest("SHA-256", data);
	return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function readCookie(request: Request, name: string): string | null {
	const header = request.headers.get("Cookie") ?? "";
	for (const part of header.split(";")) {
		const [k, ...v] = part.trim().split("=");
		if (k === name) return v.join("=");
	}
	return null;
}

function page(title: string, bodyHtml: string, status = 200): Response {
	return new Response(
		`<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title}</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, -apple-system, "Hiragino Sans", "Noto Sans JP", sans-serif;
         display: grid; place-items: center; min-height: 100vh; margin: 0; background: #f5f6f7; }
  @media (prefers-color-scheme: dark) { body { background: #16181a; color: #e8eaed; } }
  .card { background: #fff; padding: 2rem 2.25rem; border-radius: 14px; max-width: 30rem;
          box-shadow: 0 2px 16px rgba(0,0,0,.08); line-height: 1.7; }
  @media (prefers-color-scheme: dark) { .card { background: #232629; } }
  h1 { font-size: 1.15rem; margin: 0 0 1rem; }
  input { width: 100%; padding: .6rem .7rem; font-size: 1rem; border-radius: 8px;
          border: 1px solid #c9ced4; box-sizing: border-box; }
  button { margin-top: .85rem; width: 100%; padding: .65rem; font-size: 1rem; border: 0;
           border-radius: 8px; background: #B45309; color: #fff; cursor: pointer; }
  code { background: rgba(127,127,127,.16); padding: .15em .4em; border-radius: 4px;
         font-size: .9em; word-break: break-all; }
  .err { color: #b91c1c; margin: 0 0 .75rem; }
</style></head><body><div class="card">${bodyHtml}</div></body></html>`,
		{ status, headers: { "Content-Type": "text/html; charset=utf-8" } },
	);
}

function loginPage(error?: string): Response {
	return page(
		"薬局 出店候補地 分析ツール",
		`<h1>🏪 薬局 出店候補地 分析ツール v1.4</h1>
     ${error ? `<p class="err">${error}</p>` : ""}
     <form method="POST" action="${LOGIN_PATH}">
       <input type="password" name="password" placeholder="パスワード" autofocus required
              autocomplete="current-password">
       <button type="submit">開く</button>
     </form>`,
		error ? 401 : 200,
	);
}

/**
 * アクセス制限。パスワード未設定なら通さない（fail-closed）。
 * 事故で全世界に公開されるほうが、少し不便なことより困るため。
 */
async function guard(request: Request, env: Env): Promise<Response | null> {
	const password = env.APP_PASSWORD;
	if (!password) {
		return page(
			"セットアップが未完了です",
			`<h1>⚙️ あと1コマンドで公開できます</h1>
       <p>アクセス用のパスワードがまだ設定されていないため、公開を停止しています。</p>
       <p>手元のターミナルで次を実行してください:</p>
       <p><code>npx wrangler secret put APP_PASSWORD</code></p>
       <p>設定後はデプロイし直さなくても、数十秒で反映されます。</p>`,
			503,
		);
	}

	const expected = await tokenFor(password);
	const url = new URL(request.url);

	if (url.pathname === LOGIN_PATH && request.method === "POST") {
		const form = await request.formData();
		const given = String(form.get("password") ?? "");
		if (!safeEqual(given, password)) return loginPage("パスワードが違います。");
		return new Response(null, {
			status: 303,
			headers: {
				Location: "/",
				// Secure/HttpOnly/SameSite=Lax。8時間で失効する。
				"Set-Cookie": `${COOKIE_NAME}=${expected}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=28800`,
			},
		});
	}

	const cookie = readCookie(request, COOKIE_NAME);
	if (cookie && safeEqual(cookie, expected)) return null;   // 認証OK → 通す

	// WebSocket は HTML を返しても意味がないので、素直に 401 を返す
	if (request.headers.get("Upgrade")?.toLowerCase() === "websocket") {
		return new Response("Unauthorized", { status: 401 });
	}
	return loginPage();
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const blocked = await guard(request, env);
		if (blocked) return blocked;

		// インスタンス名を固定して常に同じコンテナへ送る。
		// Streamlit のセッション状態はプロセス内メモリにあるため、リクエストごとに
		// 別インスタンスへ振り分けると分析結果が消えてしまう。
		return getContainer(env.APP, "main").fetch(request);
	},
} satisfies ExportedHandler<Env>;
