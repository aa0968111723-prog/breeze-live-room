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

const late = [];
let openRelease;
const preparing = createCaptureController({
  periodMs: 60000,
  openMic: () => new Promise((resolve) => { openRelease = resolve; }),
  createRecorder: factory.createRecorder,
  newId: () => "late",
  roomId: () => "class",
  upload: () => {},
});
const starting = preparing.start();
await Promise.resolve();
assert.equal(preparing.state, "preparing");
await preparing.stop();
const lateStream = fakeStream();
late.push(lateStream);
openRelease(lateStream);
await starting;
assert.equal(preparing.state, "idle");
assert.ok(lateStream.getTracks().stopped >= 1, "取消後才到達的麥克風必須關閉");

let uploadRelease;
const draining = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder: factory.createRecorder,
  newId: () => "drain",
  roomId: () => "class",
  upload: () => new Promise((resolve) => { uploadRelease = resolve; }),
});
await draining.start();
const stopping = draining.stop();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(draining.state, "draining");
assert.equal(draining.canEditRoom(), false);
uploadRelease();
await stopping;
assert.equal(draining.state, "idle");
assert.equal(draining.uploads.length, 1);
assert.equal(draining.canEditRoom(), true);

const emptyFactory = fakeRecorderFactory();
const emptyCtl = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder() {
    const rec = emptyFactory.createRecorder();
    const orig = rec.stop.bind(rec);
    rec.stop = () => {
      rec.state = "inactive";
      const listeners = rec;
      orig();
    };
    return rec;
  },
  newId: () => "empty",
  roomId: () => "locked-room",
  upload: () => { throw new Error("空音檔不該上傳"); },
});
const madeBefore = emptyFactory.made.length;
await emptyCtl.start();
const rec = emptyFactory.made[madeBefore];
rec.stop = () => {
  rec.state = "inactive";
  rec.addEventListener = rec.addEventListener;
};
const listeners = {};
rec.addEventListener = (name, fn) => { listeners[name] = fn; };
rec.stop = () => {
  rec.state = "inactive";
  listeners.dataavailable?.({ data: { size: 0, complete: true } });
};
await emptyCtl.stop();
assert.equal(emptyCtl.uploads.length, 0);
assert.equal(emptyCtl.session.roomId, "locked-room");

let roomName = "pinned";
let releasePin;
const pinUploads = [];
const pinCtl = createCaptureController({
  periodMs: 60000,
  openMic: () => new Promise((resolve) => { releasePin = resolve; }),
  createRecorder: factory.createRecorder,
  newId: () => "pin-sess",
  roomId: () => roomName,
  upload: (meta, data) => pinUploads.push({ meta, data }),
});
const pinStart = pinCtl.start();
await Promise.resolve();
roomName = "hijack";
releasePin(fakeStream());
await pinStart;
assert.equal(pinCtl.state, "recording");
assert.equal(pinCtl.session.roomId, "pinned");
assert.equal(pinCtl.session.id, "pin-sess");
await pinCtl.stop();
assert.equal(pinUploads.length, 1);
assert.equal(pinUploads[0].meta.roomId, "pinned");
assert.equal(pinCtl.canEditRoom(), true);

let gateA;
let gateB;
let phase = "a";
const race = createCaptureController({
  periodMs: 60000,
  openMic: () => new Promise((resolve) => {
    if (phase === "a") gateA = resolve;
    else gateB = resolve;
  }),
  createRecorder: factory.createRecorder,
  newId: () => phase,
  roomId: () => "class",
  upload: () => {},
});
const firstStart = race.start();
await Promise.resolve();
await race.stop();
assert.equal(race.state, "idle");
phase = "b";
const secondStart = race.start();
await Promise.resolve();
assert.equal(race.state, "preparing");
const lateMic = fakeStream();
gateA(lateMic);
await firstStart;
assert.equal(race.state, "preparing");
assert.ok(lateMic.getTracks().stopped >= 1);
assert.notEqual(race.session && race.session.id, "a");
const liveMic = fakeStream();
gateB(liveMic);
await secondStart;
assert.equal(race.state, "recording");
assert.equal(race.session.id, "b");
assert.equal(race.session.roomId, "class");
await race.stop();
assert.equal(race.state, "idle");

const boom = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder: factory.createRecorder,
  newId: () => "boom",
  roomId: () => "class",
  upload: async () => { throw new Error("上傳失敗"); },
});
await boom.start();
await boom.stop();
assert.equal(boom.state, "idle");
assert.equal(boom.canEditRoom(), true);
assert.equal(boom.uploads.length, 0);

const flagged = [];
const flaggedRecs = [];
const flagCtl = createCaptureController({
  periodMs: 60000,
  openMic: async () => fakeStream(),
  createRecorder() {
    const listeners = {};
    const rec = {
      state: "inactive",
      addEventListener(name, fn) { listeners[name] = fn; },
      start() { this.state = "recording"; },
      stop() {
        this.state = "inactive";
        listeners.dataavailable?.({ data: { size: 80, complete: false } });
      },
      requestData() {
        listeners.dataavailable?.({ data: { size: 80, complete: true } });
      },
    };
    flaggedRecs.push(rec);
    return rec;
  },
  newId: () => "flag",
  roomId: () => "class",
  upload: (meta, data) => flagged.push({ meta, data }),
});
await flagCtl.start();
flaggedRecs[0].requestData();
assert.equal(flagged.length, 0, "requestData 的 complete 不能觸發上傳");
await flagCtl.stop();
assert.equal(flagged.length, 1);
assert.equal(flagged[0].data.size, 80);
assert.equal(flagged[0].data.complete, false);
console.log("recorder machine ok");
