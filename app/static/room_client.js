export function compareCaptions(a, b) {
  const ord = (Number(a.session_ord) || 0) - (Number(b.session_ord) || 0);
  if (ord) return ord;
  const seq = (Number(a.seq) || 0) - (Number(b.seq) || 0);
  if (seq) return seq;
  return (Number(a.cursor) || 0) - (Number(b.cursor) || 0);
}

export function orderedCaptions(items) {
  return [...items.values()].sort(compareCaptions);
}

export function liveTail(items, follow = true, pinned = null) {
  if (!follow && pinned && items.has(pinned)) return items.get(pinned);
  const list = orderedCaptions(items);
  return list.length ? list[list.length - 1] : null;
}

function noteCursor(current, value) {
  if (value == null || value === "") return current;
  const n = Number(value);
  if (!Number.isFinite(n)) return current;
  return Math.max(current, n);
}

export function connectRoom({ room, url, onState, onEvent, onGap, openSocket, sleep }) {
  const versions = new Map();
  let cursor = 0;
  let attempt = 0;
  let stopped = false;
  let socket = null;
  let timer = null;
  let cancelWait = null;
  let resolveDone = null;
  const done = new Promise((resolve) => { resolveDone = resolve; });
  const opener = openSocket || ((address) => new WebSocket(address));

  function remember(item) {
    if (!item || item.id == null || item.id === "") return false;
    cursor = noteCursor(cursor, item.cursor);
    const version = Number(item.version) || 1;
    const key = (item.session_id || "") + ":" + item.id;
    if ((versions.get(key) || 0) >= version) return false;
    versions.set(key, version);
    if (versions.size > 500) versions.delete(versions.keys().next().value);
    return true;
  }

  function address() {
    const base = url();
    const join = base.includes("?") ? "&" : "?";
    return base + join + "cursor=" + encodeURIComponent(String(cursor));
  }

  function waitMs(ms) {
    if (sleep) return sleep(ms);
    return new Promise((resolve) => {
      const finish = () => {
        timer = null;
        cancelWait = null;
        resolve();
      };
      cancelWait = finish;
      timer = setTimeout(finish, ms);
    });
  }

  function detach(ws) {
    if (!ws) return;
    ws.onopen = null;
    ws.onmessage = null;
    ws.onerror = null;
    ws.onclose = null;
  }

  async function loop() {
    while (!stopped && attempt < 8) {
      onState(attempt === 0 ? "連線中…" : "恢復中…");
      let ws;
      try {
        ws = opener(address());
      } catch {
        onState("服務離線");
        attempt += 1;
        await waitMs(Math.min(8000, 400 * 2 ** attempt));
        continue;
      }
      socket = ws;
      await new Promise((resolve) => {
        const finish = () => {
          if (cancelWait === finish) cancelWait = null;
          resolve();
        };
        cancelWait = finish;
        ws.onopen = () => { attempt = 0; onState("已連上 " + room); };
        ws.onmessage = (ev) => {
          let data;
          try {
            data = JSON.parse(ev.data);
          } catch {
            return;
          }
          if (!data || typeof data !== "object") return;
          if (data.type === "ping") {
            try { ws.send(JSON.stringify({ type: "pong" })); } catch { /* closed */ }
            return;
          }
          cursor = noteCursor(cursor, data.latest_cursor);
          if (data.gap && onGap) onGap(data);
          for (const item of data.history || []) {
            if (remember(item)) onEvent(item);
          }
          for (const item of data.events || []) {
            if (remember(item)) onEvent(item);
          }
          if (data.id && remember(data)) onEvent(data);
        };
        ws.onclose = () => finish();
        ws.onerror = () => { try { ws.close(); } catch { finish(); } };
      });
      detach(ws);
      if (socket === ws) socket = null;
      if (stopped) break;
      attempt += 1;
      onState("斷線，正在重連");
      await waitMs(Math.min(8000, 400 * 2 ** attempt));
    }
    if (!stopped) onState("服務離線。可按重新連線。");
    resolveDone();
  }

  loop();
  return {
    done,
    get cursor() { return cursor; },
    nudge() {
      if (stopped) return;
      attempt = 0;
      if (cancelWait) cancelWait();
    },
    stop() {
      stopped = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      const cancel = cancelWait;
      cancelWait = null;
      if (socket) {
        const ws = socket;
        socket = null;
        detach(ws);
        try { ws.close(); } catch { /* already closed */ }
      }
      if (cancel) cancel();
    },
  };
}
