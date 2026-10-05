export function connectRoom({ room, url, onState, onEvent, openSocket, sleep }) {
  let lastSeq = 0;
  let attempt = 0;
  let stopped = false;
  const wait = sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const opener = openSocket || ((address) => new WebSocket(address));

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
      await new Promise((resolve) => {
        ws.onopen = () => {
          attempt = 0;
          onState("已連上 " + room);
        };
        ws.onmessage = (ev) => {
          const data = JSON.parse(ev.data);
          const history = data.history || [];
          for (const item of history) {
            if (item.seq > lastSeq) {
              lastSeq = item.seq;
              onEvent(item);
            }
          }
          if (data.seq && data.seq >= lastSeq) {
            lastSeq = data.seq;
            onEvent(data);
          }
        };
        ws.onclose = () => resolve();
        ws.onerror = () => ws.close();
      });
      if (stopped) return;
      attempt += 1;
      onState("斷線，正在重連");
      await wait(Math.min(8000, 400 * 2 ** attempt));
    }
    if (!stopped) onState("服務離線。可按重新連線。");
  }

  loop();
  return { stop() { stopped = true; } };
}
