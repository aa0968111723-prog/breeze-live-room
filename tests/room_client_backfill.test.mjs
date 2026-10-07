// A deferred hello must not clear captions already on screen. The retry wait
// is retry_after plus jitter, never shorter than retry_after.

import assert from "node:assert/strict";
import { connectRoom, createCaptionView, deferredRetryMs } from "../app/static/room_client.js";

function tick() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

function fakeSocket(address) {
  return {
    address,
    readyState: 1,
    sent: [],
    close() { this.closed = true; this.onclose?.(); },
    send(body) { this.sent.push(body); },
  };
}

assert.equal(deferredRetryMs(2000, 0), 2000);
assert.equal(deferredRetryMs(2000, 1), 3000);
assert.equal(deferredRetryMs(2000, 0.5), 2500);
assert.ok(deferredRetryMs(0, 0) >= 500);
assert.ok(deferredRetryMs(0, 1) <= 1000);
assert.ok(deferredRetryMs(2000, 0) <= deferredRetryMs(2000, 1));

const shown = [];
const resets = [];
const view = createCaptionView();
view.apply({ id: "class:s:9", session_id: "s", seq: 9, version: 1, zh: "還在" });

function paint(why) {
  const text = [...view.items.values()].map((item) => item.zh).join(",");
  shown.push(why + ":" + text);
  assert.notEqual(text, "", "screen painted blank");
}
paint("seed");

const sockets = [];
const waits = [];
const conn = connectRoom({
  room: "class",
  url: () => "ws://127.0.0.1:8780/ws/listen?room_id=class",
  openSocket(address) {
    const ws = fakeSocket(address);
    sockets.push(ws);
    return ws;
  },
  sleep(ms) {
    waits.push(ms);
    return Promise.resolve();
  },
  random: () => 1,
  onState: () => {},
  onEvent(item) {
    view.apply(item);
    paint("event");
  },
  onBackfill(rows) {
    view.replace(rows);
    paint("backfill");
  },
  onReset() {
    resets.push("reset");
    view.reset();
  },
});

await tick();
sockets[0].onopen();
sockets[0].onmessage({
  data: JSON.stringify({
    type: "hello",
    epoch: 1,
    latest_cursor: 4,
    history: [{ id: "class:s:9", session_id: "s", seq: 9, version: 1, cursor: 4, zh: "還在" }],
  }),
});
sockets[0].onclose();
await tick();
sockets[1].onopen();
sockets[1].onmessage({
  data: JSON.stringify({
    type: "hello",
    epoch: 2,
    latest_cursor: 4,
    backfill_deferred: true,
    retry_after_ms: 2000,
    history: [],
    events: [],
  }),
});
await tick();
assert.deepEqual(resets, []);
assert.equal(view.items.get("class:s:9").zh, "還在");
assert.equal(shown.some((row) => row.endsWith(":")), false);
assert.equal(waits.at(-1), deferredRetryMs(2000, 1));
const replay = sockets.at(-1);
assert.match(replay.address, /replay=1/);
assert.match(replay.address, /cursor=0/);
replay.onopen();
replay.onmessage({
  data: JSON.stringify({
    type: "hello",
    epoch: 2,
    latest_cursor: 4,
    backfill: [
      { id: "class:s:1", session_id: "s", seq: 1, version: 1, cursor: 1, zh: "甲" },
      { id: "class:s:2", session_id: "s", seq: 2, version: 1, cursor: 2, zh: "乙" },
    ],
  }),
});
assert.ok(resets.length >= 1);
assert.equal([...view.items.values()].map((item) => item.zh).join(","), "甲,乙");
assert.equal(shown.some((row) => row.endsWith(":")), false);
assert.match(shown.at(-1), /甲,乙/);
conn.stop();
await conn.done;
console.log("room client backfill ok");
