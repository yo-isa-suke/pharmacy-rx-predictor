// ツールバーの拡張機能アイコンをクリックすると、
// カレンダーのタブに「時間メモ」モードの切り替えを通知する。
chrome.action.onClicked.addListener((tab) => {
  if (!tab.id) return;
  chrome.tabs.sendMessage(tab.id, { type: 'tm-toggle' }).catch(() => {
    // カレンダー以外のページ（コンテンツスクリプト未注入）では何もしない
  });
});
