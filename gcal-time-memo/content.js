// Googleカレンダー 時間メモ
// 週表示 / 日表示のタイムグリッド上でドラッグした時間帯を記録し、
// 「9月7日 11:00-13:00」形式のテキストにまとめてコピーできるようにする。
(() => {
  'use strict';
  if (window.__timeMemoInjected) return;
  window.__timeMemoInjected = true;

  const WEEKDAYS_JA = ['日', '月', '火', '水', '木', '金', '土'];
  const SNAP_MIN = 15;          // 15分単位にスナップ
  const MIN_COL_HEIGHT = 400;   // これより低い data-datekey 要素（終日欄・ミニカレンダー）は無視

  let active = false;
  let slots = [];               // {date: 'YYYY-MM-DD', start: 分, end: 分}
  let withWeekday = false;
  let drag = null;              // ドラッグ中の状態
  let renderTimer = null;
  let toastTimer = null;

  // ---------- data-datekey の解析 ----------
  // Googleカレンダーの日付列には data-datekey が付いている。
  // key = ((年 - 1970) << 9) | (月 << 5) | 日
  function dateFromDatekey(key) {
    const k = parseInt(key, 10);
    if (!Number.isFinite(k)) return null;
    const day = k & 31;
    const month = (k >> 5) & 15;
    const year = (k >> 9) + 1970;
    if (month < 1 || month > 12 || day < 1 || day > 31) return null;
    return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
  }

  function getColumns() {
    const cols = [];
    document.querySelectorAll('[data-datekey]').forEach((el) => {
      if (el.clientHeight >= MIN_COL_HEIGHT && el.offsetParent !== null) {
        const date = dateFromDatekey(el.getAttribute('data-datekey'));
        if (date) cols.push({ el, date });
      }
    });
    return cols;
  }

  function columnAtPoint(x, y) {
    for (const col of getColumns()) {
      const r = col.el.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return col;
    }
    return null;
  }

  function minutesFromY(colEl, clientY) {
    const r = colEl.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (clientY - r.top) / r.height));
    const min = Math.round((frac * 1440) / SNAP_MIN) * SNAP_MIN;
    return Math.min(1440, Math.max(0, min));
  }

  // ---------- 表示用フォーマット ----------
  function fmtTime(min) {
    return `${Math.floor(min / 60)}:${String(min % 60).padStart(2, '0')}`;
  }

  function fmtRange(s) {
    return `${fmtTime(s.start)}-${fmtTime(s.end)}`;
  }

  function dateLabel(dateStr) {
    const [y, m, d] = dateStr.split('-').map(Number);
    let label = `${m}月${d}日`;
    if (withWeekday) label += `(${WEEKDAYS_JA[new Date(y, m - 1, d).getDay()]})`;
    return label;
  }

  function buildLines() {
    const byDate = new Map();
    for (const s of [...slots].sort((a, b) =>
      a.date === b.date ? a.start - b.start : a.date < b.date ? -1 : 1)) {
      if (!byDate.has(s.date)) byDate.set(s.date, []);
      byDate.get(s.date).push(fmtRange(s));
    }
    return [...byDate.entries()].map(([date, ranges]) => `${dateLabel(date)} ${ranges.join(', ')}`);
  }

  // ---------- スロット管理 ----------
  function slotKey(s) {
    return `${s.date}_${s.start}_${s.end}`;
  }

  function addSlot(date, start, end) {
    let s = Math.min(start, end);
    let e = Math.max(start, end);
    if (e - s < SNAP_MIN) e = Math.min(1440, s + SNAP_MIN);
    if (e - s < SNAP_MIN) s = e - SNAP_MIN;

    // 同じ日の重なり・隣接する時間帯はひとつにまとめる
    const rest = [];
    for (const other of slots) {
      if (other.date === date && other.start <= e && other.end >= s) {
        s = Math.min(s, other.start);
        e = Math.max(e, other.end);
      } else {
        rest.push(other);
      }
    }
    const added = { date, start: s, end: e };
    slots = [...rest, added];
    persist();
    renderAll();
    return added;
  }

  function removeSlot(key) {
    slots = slots.filter((s) => slotKey(s) !== key);
    persist();
    renderAll();
  }

  function persist() {
    try {
      chrome.storage.local.set({ tmSlots: slots, tmWithWeekday: withWeekday });
    } catch (_) { /* 拡張機能のリロード直後などは無視 */ }
  }

  // ---------- UI: トースト ----------
  function toast(msg) {
    let el = document.querySelector('.tm-toast');
    if (!el) {
      el = document.createElement('div');
      el.className = 'tm-toast tm-ui';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add('tm-toast-show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('tm-toast-show'), 1800);
  }

  // ---------- UI: 起動ボタン ----------
  let launcher = null;
  function ensureLauncher() {
    if (launcher && launcher.isConnected) return;
    launcher = document.createElement('button');
    launcher.className = 'tm-launcher tm-ui';
    launcher.type = 'button';
    launcher.innerHTML = '<span class="tm-clock">🕒</span> 時間メモ';
    launcher.addEventListener('click', start);
    document.body.appendChild(launcher);
    launcher.style.display = active ? 'none' : '';
  }

  // ---------- UI: パネル ----------
  let panel = null;
  function ensurePanel() {
    if (panel && panel.isConnected) return;
    panel = document.createElement('div');
    panel.className = 'tm-panel tm-ui';
    panel.innerHTML = `
      <div class="tm-head">
        <span class="tm-title"><span class="tm-clock">🕒</span> 時間メモ</span>
        <span class="tm-head-right">
          <a class="tm-clear" href="#">クリア</a>
          <span class="tm-count"></span>
        </span>
      </div>
      <div class="tm-list"></div>
      <label class="tm-opt"><input type="checkbox" class="tm-weekday"> 曜日を付ける</label>
      <div class="tm-foot">
        <button type="button" class="tm-btn tm-end">終了</button>
        <button type="button" class="tm-btn tm-copy">コピー</button>
      </div>`;
    document.body.appendChild(panel);

    panel.querySelector('.tm-clear').addEventListener('click', (e) => {
      e.preventDefault();
      slots = [];
      persist();
      renderAll();
      toast('クリアしました');
    });
    panel.querySelector('.tm-end').addEventListener('click', stop);
    panel.querySelector('.tm-copy').addEventListener('click', copyText);
    const cb = panel.querySelector('.tm-weekday');
    cb.checked = withWeekday;
    cb.addEventListener('change', () => {
      withWeekday = cb.checked;
      persist();
      renderPanel();
    });

    // ヘッダーをつかんでパネルを移動できるようにする
    const head = panel.querySelector('.tm-head');
    head.addEventListener('pointerdown', (e) => {
      if (e.target.closest('.tm-clear')) return;
      e.preventDefault();
      const rect = panel.getBoundingClientRect();
      const dx = e.clientX - rect.left;
      const dy = e.clientY - rect.top;
      const move = (ev) => {
        panel.style.left = `${Math.max(0, ev.clientX - dx)}px`;
        panel.style.top = `${Math.max(0, ev.clientY - dy)}px`;
        panel.style.right = 'auto';
      };
      const up = () => {
        window.removeEventListener('pointermove', move, true);
        window.removeEventListener('pointerup', up, true);
      };
      window.addEventListener('pointermove', move, true);
      window.addEventListener('pointerup', up, true);
    });
  }

  function renderPanel() {
    if (!panel || !panel.isConnected) return;
    const lines = buildLines();
    const list = panel.querySelector('.tm-list');
    list.textContent = '';
    if (lines.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'tm-empty';
      empty.textContent = 'カレンダー上でドラッグして\n候補の時間帯を選択してください';
      list.appendChild(empty);
    } else {
      for (const line of lines) {
        const row = document.createElement('div');
        row.className = 'tm-line';
        row.textContent = line;
        list.appendChild(row);
      }
    }
    panel.querySelector('.tm-count').textContent = slots.length ? `${slots.length}件` : '';
  }

  async function copyText() {
    const text = buildLines().join('\n');
    if (!text) {
      toast('コピーする時間帯がありません');
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      ta.remove();
    }
    toast(`コピーしました（${slots.length}件）`);
  }

  // ---------- UI: グリッド上のオーバーレイ ----------
  function ensureColumnPositioned(colEl) {
    if (getComputedStyle(colEl).position === 'static') {
      colEl.style.position = 'relative';
    }
  }

  function makeOverlay(slot) {
    const el = document.createElement('div');
    el.className = 'tm-slot';
    el.dataset.tmKey = slotKey(slot);
    el.style.top = `${(slot.start / 1440) * 100}%`;
    el.style.height = `${((slot.end - slot.start) / 1440) * 100}%`;
    const label = document.createElement('span');
    label.className = 'tm-slot-label';
    label.textContent = fmtRange(slot);
    el.appendChild(label);
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'tm-slot-del tm-ui';
    del.textContent = '×';
    del.title = 'この時間帯を削除';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      removeSlot(el.dataset.tmKey);
    });
    el.appendChild(del);
    return el;
  }

  function renderOverlays() {
    const wanted = new Map();  // key -> slot
    if (active) for (const s of slots) wanted.set(slotKey(s), s);

    // 不要になったオーバーレイを除去
    document.querySelectorAll('.tm-slot').forEach((el) => {
      if (!wanted.has(el.dataset.tmKey)) el.remove();
    });
    if (!active) return;

    const attached = new Set(
      [...document.querySelectorAll('.tm-slot')].map((el) => el.dataset.tmKey)
    );
    const cols = getColumns();
    for (const [key, slot] of wanted) {
      if (attached.has(key)) continue;
      const col = cols.find((c) => c.date === slot.date);
      if (!col) continue;  // 表示中の週にない日付
      ensureColumnPositioned(col.el);
      col.el.appendChild(makeOverlay(slot));
    }
  }

  function renderAll() {
    renderOverlays();
    renderPanel();
  }

  // ---------- ドラッグ処理 ----------
  // capture フェーズで先取りして、Googleカレンダー本体の
  // 「ドラッグで予定を作成」動作をブロックする。
  function onPointerDown(e) {
    if (!active || e.button !== 0) return;
    if (e.target.closest && e.target.closest('.tm-ui')) return;
    const col = columnAtPoint(e.clientX, e.clientY);
    if (!col) return;
    e.preventDefault();
    e.stopImmediatePropagation();

    ensureColumnPositioned(col.el);
    const anchor = minutesFromY(col.el, e.clientY);
    const el = document.createElement('div');
    el.className = 'tm-drag';
    const label = document.createElement('span');
    label.className = 'tm-slot-label';
    el.appendChild(label);
    col.el.appendChild(el);
    drag = { col, anchor, start: anchor, end: anchor, el, label };
    updateDrag(e.clientY);
  }

  function updateDrag(clientY) {
    if (!drag) return;
    const cur = minutesFromY(drag.col.el, clientY);
    drag.start = Math.min(drag.anchor, cur);
    drag.end = Math.max(drag.anchor, cur);
    const start = drag.start;
    let end = drag.end;
    if (end - start < SNAP_MIN) end = Math.min(1440, start + SNAP_MIN);
    drag.el.style.top = `${(start / 1440) * 100}%`;
    drag.el.style.height = `${((end - start) / 1440) * 100}%`;
    drag.label.textContent = `${fmtTime(start)} - ${fmtTime(end)}`;
  }

  function onPointerMove(e) {
    if (!active || !drag) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    updateDrag(e.clientY);
  }

  function onPointerUp(e) {
    if (!active || !drag) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    updateDrag(e.clientY);
    const { col, start, end } = drag;
    drag.el.remove();
    drag = null;
    const added = addSlot(col.date, start, end);
    toast(`追加: ${dateLabel(added.date)} ${fmtRange(added)}`);
  }

  // ドラッグをブロックした後に発生しうる残りのイベントも抑える
  function onSwallow(e) {
    if (!active) return;
    if (e.target.closest && e.target.closest('.tm-ui')) return;
    if (drag || columnAtPoint(e.clientX, e.clientY)) {
      if (e.type !== 'mousemove' || drag) e.stopImmediatePropagation();
      if (e.cancelable && (drag || e.type === 'mousedown')) e.preventDefault();
    }
  }

  const CAPTURE = { capture: true };
  function bindGridEvents() {
    window.addEventListener('pointerdown', onPointerDown, CAPTURE);
    window.addEventListener('pointermove', onPointerMove, CAPTURE);
    window.addEventListener('pointerup', onPointerUp, CAPTURE);
    window.addEventListener('pointercancel', () => {
      if (drag) {
        drag.el.remove();
        drag = null;
      }
    }, CAPTURE);
    for (const t of ['mousedown', 'mousemove', 'mouseup', 'click']) {
      window.addEventListener(t, onSwallow, CAPTURE);
    }
  }

  // ---------- モードの開始 / 終了 ----------
  function start() {
    if (active) return;
    active = true;
    ensureLauncher();
    launcher.style.display = 'none';
    ensurePanel();
    renderAll();
    // 週の切り替えなどでDOMが作り直されてもオーバーレイを貼り直す
    renderTimer = setInterval(renderOverlays, 800);
    if (getColumns().length === 0) {
      toast('週表示または日表示に切り替えてください');
    }
  }

  function stop() {
    if (!active) return;
    active = false;
    clearInterval(renderTimer);
    renderTimer = null;
    if (drag) {
      drag.el.remove();
      drag = null;
    }
    renderOverlays();  // active=false なので全オーバーレイが消える
    if (panel) {
      panel.remove();
      panel = null;
    }
    ensureLauncher();
    launcher.style.display = '';
  }

  // ---------- 初期化 ----------
  try {
    chrome.storage.local.get(['tmSlots', 'tmWithWeekday'], (data) => {
      if (Array.isArray(data.tmSlots)) slots = data.tmSlots;
      withWeekday = !!data.tmWithWeekday;
      renderPanel();
    });
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === 'tm-toggle') {
        active ? stop() : start();
      }
    });
  } catch (_) { /* chrome API が使えない場合は起動ボタンのみで動作 */ }

  bindGridEvents();
  ensureLauncher();
  // SPAなのでbodyが置き換わった場合に備えて起動ボタンを維持する
  setInterval(ensureLauncher, 2000);
})();
