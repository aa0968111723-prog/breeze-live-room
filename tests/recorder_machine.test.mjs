import assert from "node:assert/strict";
import { createCaptureController } from "../app/static/recorder_machine.js";

function fakeStream() {
  const tracks = [{ stop() { tracks.stopped = (tracks.stopped || 0) + 1; } }];
  return { getTracks: () => tracks };
}

function fakeRecorderFactory() {
  const made = [];
  function createRecorder() {
    const listeners = {};
    const rec = {
      state: "inactive",
      addEventListener(name, fn) { listeners[name] = fn; },
      start() { this.state = "recording"; },
      stop() {
        this.state = "inactive";
        listeners.dataavailable?.({ data: { size: 1200, complete: true } });
        listeners.stop?.();
      },
      requestData() {
        listeners.dataavailable?.({ data: { size: 1200, complete: false } });
      },
    };
    made.push(rec);
    return rec;
  }
  return { createRecorder, made };
}

const factory = fakeRecorderFactory();
const uploads = [];
const ctl = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder: factory.createRecorder,
  newId: () => "sess",
  roomId: () => "class",
  upload: (meta, data) => uploads.push({ meta, data }),
});

await ctl.start();
assert.equal(ctl.state, "recording");
await assert.rejects(() => ctl.start(), /已經在聽/);
assert.equal(factory.made.length, 1);
factory.made[0].requestData();
assert.equal(uploads.length, 0, "中間切片不能當成獨立音檔上傳");
await ctl.stop();
assert.equal(ctl.state, "idle");
assert.equal(uploads.length, 1);
assert.equal(uploads[0].meta.seq, 1);
assert.equal(uploads[0].meta.sessionId, "sess");
assert.ok(ctl.released.tracks >= 1);
assert.ok(ctl.released.timers >= 1);
console.log("recorder machine ok");
