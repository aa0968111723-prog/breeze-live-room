// Host big-caption display. The page keeps the caption map in room_client's view;
// this module decides which line is on screen and refuses an older version.

function rank(item) {
  return [Number(item.session_ord) || 0, Number(item.seq) || 0, Number(item.cursor) || 0];
}

function later(a, b) {
  const left = rank(a);
  const right = rank(b);
  for (let i = 0; i < left.length; i += 1) {
    if (left[i] !== right[i]) return left[i] > right[i];
  }
  return false;
}

export function applyCaption(state, msg) {
  if (!state.items) state.items = new Map();
  if (msg && msg.id != null && msg.id !== "") {
    const version = Number(msg.version) || 1;
    const prev = state.items.get(msg.id);
    if (!prev || (Number(prev.version) || 1) < version) {
      state.items.set(msg.id, { ...msg, version });
    }
  }
  let live = null;
  for (const item of state.items.values()) {
    if (!live || later(item, live)) live = item;
  }
  if (!live) return { zh: "", en: "", label: "", item: null };
  const zh = live.zh || live.error || "";
  const en = typeof live.en === "string" ? live.en : "";
  return { zh, en, label: en ? en : "（尚無英譯）", item: live };
}
