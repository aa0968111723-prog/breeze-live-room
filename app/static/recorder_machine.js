export function createCaptureController(deps) {
  let state = "idle";
  let session = null;
  let seq = 0;
  let timer = null;
  let recorder = null;
  let stream = null;
  let stopRequested = false;
  const released = { tracks: 0, timers: 0, recorders: 0 };
  const uploads = [];

  function releaseStream() {
    if (!stream) return;
    for (const track of stream.getTracks()) track.stop();
    released.tracks += stream.getTracks().length;
    stream = null;
  }

  function clearTimer() {
    if (!timer) return;
    clearInterval(timer);
    timer = null;
    released.timers += 1;
  }

  function stopRecorder() {
    if (!recorder || recorder.state === "inactive") return Promise.resolve();
    return new Promise((resolve) => {
      const done = () => resolve();
      recorder.addEventListener("stop", done, { once: true });
      recorder.stop();
      released.recorders += 1;
    });
  }

  async function beginSegment() {
    recorder = deps.createRecorder(stream);
    const current = { sessionId: session.id, roomId: session.roomId, seq: seq + 1 };
    recorder.addEventListener("dataavailable", (ev) => {
      if (!ev.data || ev.data.complete === false) return;
      uploads.push(current);
      deps.upload(current, ev.data);
    });
    recorder.start();
    seq = current.seq;
  }

  return {
    get state() { return state; },
    get released() { return released; },
    get uploads() { return uploads; },
    get session() { return session; },
    async start() {
      if (state === "preparing" || state === "recording" || state === "draining") {
        throw new Error("已經在聽，請先停止");
      }
      state = "preparing";
      stopRequested = false;
      try {
        stream = await deps.openMic();
        session = { id: deps.newId(), roomId: deps.roomId() };
        seq = 0;
        state = "recording";
        await beginSegment();
        timer = setInterval(() => {
          if (stopRequested || state !== "recording") return;
          stopRecorder().then(async () => {
            if (stopRequested || state !== "recording") return;
            await beginSegment();
          });
        }, deps.periodMs || 6000);
      } catch (err) {
        clearTimer();
        releaseStream();
        state = "error";
        throw err;
      }
    },
    async stop() {
      if (state !== "recording" && state !== "preparing") return;
      state = "draining";
      stopRequested = true;
      clearTimer();
      await stopRecorder();
      releaseStream();
      recorder = null;
      state = "idle";
    },
  };
}
