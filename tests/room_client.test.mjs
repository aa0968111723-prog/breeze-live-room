import assert from "node:assert/strict";
import { connectRoom, liveTail } from "../app/static/room_client.js";

function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

const items = new Map();
function apply(item) {
  const prev = items.get(item.id);
  if (prev && (prev.version || 1) >= (item.version || 1)) return;
  items.set(item.id, item);
}

apply({ id: "room:a:20", session_id: "a", session_ord: 1, seq: 20, version: 1, zh: "舊" });
apply({ id: "room:b:1", session_id: "b", session_ord: 2, seq: 1, version: 1, zh: "新" });
assert.equal(liveTail(items).zh, "新");
apply({ id: "room:a:20", session_id: "a", session_ord: 1, seq: 20, version: 2, zh: "舊", en: "old" });
assert.equal(liveTail(items).zh, "新");
assert.equal(liveTail(items, false, "room:a:20").en, "old");

const events = [];
const sockets = [];
function fakeSocket(address) {
  const ws = {
    address,
    readyState: 1,
    sent: [],
    close() { this.closed = true; this.onclose?.(); },
    send(body) { this.sent.push(body); },
  };
  sockets.push(ws);
  return ws;
}

const conn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket: fakeSocket,
  sleep: () => Promise.resolve(),
  onState: () => {},
  onEvent: (item) => events.push(item),
  onGap: () => events.push({ gap: true }),
});
await tick();
const first = sockets[0];
first.onopen();
first.onmessage({ data: "not-json" });
first.onmessage({ data: JSON.stringify({ type: "ping" }) });
assert.equal(JSON.parse(first.sent[0]).type, "pong");
first.onmessage({
  data: JSON.stringify({
    type: "hello",
    latest_cursor: 3,
    history: [
      { id: "class:a:20", session_id: "a", session_ord: 1, seq: 20, version: 1, cursor: 1, zh: "舊會話" },
      { id: "class:b:1", session_id: "b", session_ord: 2, seq: 1, version: 1, cursor: 2, zh: "新會話" },
    ],
  }),
});
first.onmessage({
  data: JSON.stringify({
    type: "caption",
    id: "class:a:20",
    session_id: "a",
    session_ord: 1,
    seq: 20,
    version: 2,
    cursor: 3,
    zh: "舊會話",
    en: "old session",
  }),
});
assert.equal(events.filter((item) => item.zh === "新會話").length, 1);
assert.equal(events.find((item) => item.id === "class:a:20" && item.version === 2).en, "old session");
const shown = new Map(events.filter((item) => item.id).map((item) => [item.id, item]));
assert.equal(liveTail(shown).zh, "新會話");

first.onclose();
await tick();
assert.match(sockets[1].address, /cursor=3/);
sockets[1].onopen();
sockets[1].onmessage({
  data: JSON.stringify({
    type: "hello",
    latest_cursor: 4,
    gap: true,
    events: [{ id: "class:b:1", session_id: "b", session_ord: 2, seq: 1, version: 2, cursor: 4, zh: "新會話", en: "new" }],
  }),
});
assert.equal(events.some((item) => item.gap), true);
assert.equal(events.filter((item) => item.id === "class:b:1" && item.version === 2).length, 1);
sockets[1].onmessage({
  data: JSON.stringify({ id: "class:b:1", session_id: "b", version: 2, seq: 1, zh: "新會話", en: "new" }),
});
assert.equal(events.filter((item) => item.id === "class:b:1" && item.version === 2).length, 1);

const view = new Map(events.filter((item) => item.id).map((item) => [item.id, item]));
assert.equal(liveTail(view, false, "class:b:1").zh, "新會話");
assert.equal(liveTail(view, false, "class:b:1").id, "class:b:1");
assert.notEqual(liveTail(view, false, "class:b:1").id, "class:a:20");

sockets[1].onmessage({
  data: JSON.stringify({ id: "class:b:1", session_id: "b", version: 2, seq: 99, cursor: 5, zh: "新會話", en: "new" }),
});
assert.equal(events.filter((item) => item.id === "class:b:1" && item.version === 2).length, 1);
assert.equal(conn.cursor, 5);

conn.stop();
assert.equal(sockets[1].closed, true);
assert.equal(sockets[1].onmessage, null);
if (sockets[1].onmessage) sockets[1].onmessage({ data: JSON.stringify({ type: "ping" }) });
assert.equal(sockets[1].sent.length, 0);
await conn.done;
console.log("room client ok");
