export function connectRoom({ room, url, onState, onEvent, openSocket, sleep }) {
  const versions = new Map();
  let attempt = 0;
  let stopped = false;
  let socket = null;
  const wait = sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const opener = openSocket || ((address) => new WebSocket(address));

  function take(item) {
    if (!item || !item.id) return;
    const version = item.version || (item.en ? 2 : 1);
    if ((versions.get(item.id) || 0) >= version) return;
    versions.set(item.id, version);
    onEvent(item);
  }

  async function loop() {
    while (!stopped && attempt < 8) {
      onState(attempt === 0 ? "連線中…" : "恢復中…");
      let ws;
      try {
        ws = opener(url());
      } catch {
        onState("服務離線");
        attempt += 1;
        await wait(Math.min(8000, 400 * 2 ** attempt));
        continue;
      }
      socket = ws;
      await new Promise((resolve) => {
        ws.onopen = () => {
          attempt = 0;
          onState("已連上 " + room);
        };
        ws.onmessage = (ev) => {
          const data = JSON.parse(ev.data);
          for (const item of data.history || []) take(item);
          if (data.id) take(data);
        };
        ws.onclose = () => resolve();
        ws.onerror = () => ws.close();
      });
      socket = null;
      if (stopped) return;
      attempt += 1;
      onState("斷線，正在重連");
      await wait(Math.min(8000, 400 * 2 ** attempt));
    }
    if (!stopped) onState("服務離線。可按重新連線。");
  }

  loop();
  return {
    stop() {
      stopped = true;
      if (socket) socket.close();
    },
  };
}
