// The host page must show rejected glossary lines and must not say the room was saved.

import assert from "node:assert/strict";
import fs from "node:fs";

import {
  glossaryApplyLoadFailure,
  glossaryApplyLoaded,
  glossaryBeginLoad,
  glossaryBoxText,
  glossaryEdit,
  glossaryLegacyBlock,
  glossaryLoadTargetsSelector,
  glossaryMarkSaved,
  glossaryPostBody,
  glossaryPrepareLoad,
  glossaryRefusal,
  glossaryRoomState,
  glossarySaveDecision,
  glossarySaveLine,
  glossarySaveRequest,
  glossarySaveSettlement,
  glossaryTransportLine,
} from "../app/static/host_glossary.js";

const saved = glossarySaveLine(200, { ok: true, count: 2 });
assert.equal(saved, "已儲存這個房間的術語。");
const keptAll = glossarySaveLine(200, { ok: true, count: 2, deleted: 0 });
assert.equal(keptAll, "已儲存這個房間的術語。");
const removed = glossarySaveLine(200, { ok: true, count: 1, deleted: 2 });
assert.equal(removed, "已儲存這個房間的術語。這次刪了 2 條。");
assert.match(removed, /刪了 2 條/);

const rejected = glossarySaveLine(400, {
  ok: false,
  count: 0,
  rejected: [
    { line: 1, reason: "別名「開始」是常用詞，不能當別名" },
    { line: 2, reason: "標準詞「禅学社」必須是繁體" },
  ],
});
assert.equal(rejected.includes("已儲存"), false);
assert.match(rejected, /第 1 行：別名「開始」是常用詞，不能當別名/);
assert.match(rejected, /第 2 行：標準詞「禅学社」必須是繁體/);

const conflict = glossarySaveLine(409, {
  ok: false,
  rejected: [{ line: 0, reason: "術語表版本不符" }],
});
assert.equal(conflict.includes("已儲存"), false);
assert.match(conflict, /術語表版本不符/);
assert.match(conflict, /重新整理/);
assert.equal(conflict.includes("第 0 行"), false);

const empty = glossarySaveLine(400, { ok: false, rejected: [{ line: 0, reason: "沒有有效的術語，不會清空這個房間的詞表" }] });
assert.equal(empty.includes("已儲存"), false);
assert.match(empty, /不會清空/);

const opaque = glossarySaveLine(500, null);
assert.equal(opaque.includes("已儲存"), false);
assert.match(opaque, /沒有寫入/);

const offline = glossaryTransportLine(new Error("Failed to fetch"));
assert.equal(offline.includes("已儲存"), false);
assert.match(offline, /沒有寫入/);
assert.match(offline, /Failed to fetch/);
const reauth = glossaryTransportLine(new Error("無法重新取得主持權限"));
assert.equal(reauth.includes("已儲存"), false);
assert.match(reauth, /無法重新取得主持權限/);

const posted = glossaryPostBody("class", "s", "般若=prajna", 3);
assert.equal(posted.if_version, 3);
assert.equal(posted.text, "般若=prajna");
assert.equal(Object.hasOwn(glossaryPostBody("class", "s", "般若=prajna", null), "if_version"), false);

const plain = glossaryLegacyBlock([
  { zh: "般若", en: "prajna", aliases: [], lock: true, note: "", category: "" },
]);
assert.equal(plain.locked, false);
assert.equal(plain.note, "");
assert.equal(glossaryBoxText([
  { zh: "般若", en: "prajna", aliases: [] },
  { zh: "開示", en: "Dharma talk", aliases: ["開導"] },
]), "般若=prajna\n開示|開導=Dharma talk");

const many = Array.from({ length: 200 }, (_, i) => ({ zh: "詞" + i, en: "e", aliases: [], lock: true }));
const over = glossaryLegacyBlock(many);
assert.equal(over.locked, true);
assert.match(over.note, /200/);
assert.match(over.note, /主持頁最多 40 條/);
assert.match(over.note, /PUT \/api\/rooms\/\{room_id\}\/glossary/);
assert.equal(over.note.includes("編輯器"), false);
assert.equal(over.note.includes("進階欄位"), false);
const aliasTerm = { zh: "禪學社", en: "Zen Club", aliases: ["柴學社"], lock: true, note: "", category: "" };
const aliasBlock = glossaryLegacyBlock([aliasTerm]);
assert.equal(aliasBlock.locked, false, JSON.stringify(aliasTerm));
assert.equal(aliasBlock.note, "");
assert.equal(glossaryBoxText([aliasTerm]), "禪學社|柴學社=Zen Club");
for (const sample of [
  { zh: "等號", en: "a=b", aliases: ["等号"], text: "等號|等号=a=b" },
  { zh: "管道", en: "a|b", aliases: ["管线"], text: "管道|管线=a|b" },
  { zh: "全形", en: "a＝b", aliases: ["全型"], text: "全形|全型=a＝b" },
]) {
  const term = { zh: sample.zh, en: sample.en, aliases: sample.aliases, lock: true, note: "", category: "" };
  const block = glossaryLegacyBlock([term]);
  assert.equal(block.locked, false, sample.text);
  assert.equal(block.note, "");
  assert.equal(glossaryBoxText([term]), sample.text);
  let round = glossaryRoomState();
  round = glossaryPrepareLoad(round, "class");
  round = glossaryApplyLoaded(round, "class", round.generation, [term], 1);
  assert.equal(round.locked, false, sample.text);
  assert.equal(round.readOnly, false, sample.text);
  assert.equal(round.text, sample.text);
  const again = glossarySaveDecision(round, "class", "s");
  assert.equal(again.post, true, sample.text);
  assert.equal(again.body.text, sample.text);
  assert.equal(again.body.if_version, 1);
}
const englishBreak = glossaryLegacyBlock([{ zh: "般若", en: "pra\njna", aliases: [], lock: true, note: "", category: "" }]);
assert.equal(englishBreak.locked, true);
for (const term of [
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: false, note: "", category: "" },
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: true, note: "備註", category: "" },
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: true, note: "", category: "社團" },
  { zh: "禪學社", en: "Zen Club", aliases: ["柴|學社"], lock: true, note: "", category: "" },
  { zh: "#般若", en: "prajna", aliases: [], lock: true, note: "", category: "" },
]) {
  const block = glossaryLegacyBlock([term]);
  assert.equal(block.locked, true, JSON.stringify(term));
  assert.match(block.note, /備註、分類或未鎖定的詞/);
  assert.match(block.note, /PUT \/api\/rooms\/\{room_id\}\/glossary/);
  assert.equal(block.note.includes("編輯器"), false);
  assert.equal(block.note.includes("進階欄位"), false);
}
const both = glossaryLegacyBlock(many.map((term, i) => (i === 0 ? { ...term, note: "備註" } : term)));
assert.equal(both.locked, true);
assert.match(both.note, /200/);
assert.match(both.note, /40/);
assert.match(both.note, /備註/);
assert.equal(both.note.includes("編輯器"), false);

const blocked = glossarySaveRequest("class", "s", "般若=prajna", 1, true);
assert.equal(blocked.post, false);
const unversioned = glossarySaveRequest("class", "s", "般若=prajna", null, false);
assert.equal(unversioned.post, false);
const ready = glossarySaveRequest("class", "s", "般若=prajna", 4, false);
assert.equal(ready.post, true);
assert.equal(ready.body.if_version, 4);
assert.equal(ready.body.text, "般若=prajna");

const html = fs.readFileSync(new URL("../app/static/host.html", import.meta.url), "utf8");
const js = fs.readFileSync(new URL("../app/static/host_glossary.js", import.meta.url), "utf8");
assert.match(html, /glossarySaveLine/);
assert.equal(html.includes('line("已儲存這個房間的術語。")'), false);
assert.match(html, /reportGlossary/);
assert.match(html, /glossarySaveDecision/);
assert.match(html, /glossaryTransportLine/);
assert.match(html, /id="glossary-note"/);
assert.match(html, /aria-describedby="glossary-note"/);
assert.match(html, /role="status"/);
assert.match(html, /這個房間的術語/);
assert.equal(html.includes("這場術語"), false);
assert.match(html, /aria-disabled/);
assert.equal(html.includes("saveBtn.disabled"), false);
assert.equal(html.includes("請用術語表編輯器修改"), false);
assert.equal(js.includes("請用術語表編輯器修改"), false);
assert.equal(js.includes("進階欄位"), false);
assert.match(html, /glossary-readonly/);
assert.match(html, /（唯讀）/);
const saveRequest = extractBlock(js, "export function glossarySaveRequest(");
assert.match(saveRequest, /Number\.isInteger\(loadedVersion\)/);
assert.match(saveRequest, /glossaryPostBody/);

function extractBlock(source, marker) {
  const start = source.indexOf(marker);
  assert.notEqual(start, -1, marker);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    const ch = source[i];
    if (ch === "{") depth += 1;
    else if (ch === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error("unclosed " + marker);
}

const recover = extractBlock(html, "async function recoverAuth()");
const start = extractBlock(html, "go.onclick = async () =>");
const save = extractBlock(html, 'document.querySelector("#save-terms").onclick = async () =>');
const post = extractBlock(html, "async function postGlossary(");
const setup = extractBlock(html, "async function setup()");
const listen = extractBlock(html, "function listenCaptions(");
const load = extractBlock(html, "async function loadGlossary(");
assert.equal(/reportGlossary|postGlossary|\/api\/glossary/.test(recover), false);
assert.equal(recover.includes("ctl.session"), false);
assert.match(recover, /roomEl\.value/);
assert.match(load, /glossaryBeginLoad/);
assert.equal(load.includes("glossaryPrepareLoad"), false);
assert.ok(load.indexOf("glossaryBeginLoad") < load.indexOf("paintGlossary"));
assert.equal(/reportGlossary|postGlossary|\/api\/glossary/.test(start), false);
assert.match(save, /glossarySaveDecision/);
assert.ok(save.indexOf("glossarySaveDecision") < save.indexOf("reportGlossary"));
assert.equal(save.includes("ctl.session.roomId"), false);
assert.match(save, /reportGlossary/);
assert.match(save, /glossaryRefusal/);
assert.match(save, /glossaryTransportLine/);
assert.match(post, /glossarySaveDecision/);
assert.equal(post.includes("ctl.session.roomId"), false);
assert.equal(post.includes("/api/rooms/"), false);
assert.equal(/Number\.isInteger\(/.test(post), false);
assert.match(post, /409/);
assert.match(post, /glossarySaveSettlement\(\s*glossaryState,\s*requestRoom,\s*requestGeneration,\s*request\.body\.text\s*\)/);
assert.equal(post.includes("glossaryMarkSaved"), false);
assert.ok(post.indexOf("glossarySaveSettlement") < post.indexOf("loadGlossary"));
assert.ok(post.indexOf("glossaryState = settled.state") < post.indexOf("loadGlossary"));
assert.equal(post.includes("這頁剛剛讀到"), false);
assert.match(setup, /loadGlossary/);
assert.match(listen, /loadGlossary/);
assert.match(listen, /已連上/);
assert.match(html, /glossaryBeginLoad/);
assert.match(html, /glossaryApplyLoadFailure/);
assert.match(html, /autocomplete="off"/);
const glossaryBox = html.match(/<textarea id="glossary"[^>]*>/);
assert.ok(glossaryBox);
assert.match(glossaryBox[0], /autocomplete="off"/);
assert.match(start, /術語尚未儲存/);
assert.equal(/reportGlossary|postGlossary|\/api\/glossary/.test(start), false);

const decision = extractBlock(js, "export function glossarySaveDecision(");
assert.match(decision, /room-mismatch/);
assert.match(decision, /locked/);
assert.match(decision, /glossarySaveRequest/);
assert.ok(decision.indexOf("room-mismatch") < decision.indexOf("glossarySaveRequest"));

// Stop in room A, switch the selector to B, edit, save. The post goes to B.
const roomA = [{ zh: "甲", en: "A1", aliases: [], lock: true, note: "", category: "" }];
const roomB = [{ zh: "丙", en: "B1", aliases: [], lock: true, note: "", category: "" }];
let switched = glossaryRoomState();
switched = glossaryPrepareLoad(switched, "room-a");
switched = glossaryApplyLoaded(switched, "room-a", switched.generation, roomA, 1);
assert.equal(switched.room, "room-a");
assert.equal(switched.version, 1);
switched = glossaryPrepareLoad(switched, "room-b");
assert.equal(switched.room, null);
assert.equal(switched.version, null);
assert.equal(switched.text, "");
assert.equal(switched.readOnly, true);
switched = glossaryApplyLoaded(switched, "room-b", switched.generation, roomB, 1);
switched = glossaryEdit(switched, "丙=B1\n丁=B2");
const saveB = glossarySaveDecision(switched, "room-b", "session-from-room-a");
assert.equal(saveB.post, true);
assert.equal(saveB.body.room_id, "room-b");
assert.notEqual(saveB.body.room_id, "room-a");
assert.equal(saveB.body.if_version, 1);
assert.equal(saveB.body.text, "丙=B1\n丁=B2");
assert.equal(saveB.body.session_id, "session-from-room-a");

// Switch to B and the GET fails. Do not post A's glossary, and say so.
let failed = glossaryRoomState();
failed = glossaryPrepareLoad(failed, "room-a");
failed = glossaryApplyLoaded(failed, "room-a", failed.generation, roomA, 1);
const textA = failed.text;
failed = glossaryPrepareLoad(failed, "room-b");
assert.equal(failed.text, "");
assert.notEqual(failed.text, textA);
assert.equal(failed.version, null);
failed = glossaryApplyLoadFailure(failed, "room-b", failed.generation);
assert.equal(failed.room, null);
assert.equal(failed.text, "");
assert.equal(failed.version, null);
const saveFailed = glossarySaveDecision(failed, "room-b", "session-from-room-a");
assert.equal(saveFailed.post, false);
assert.equal(saveFailed.reason, "room-mismatch");
assert.match(failed.note, /讀不到這個房間已經存的術語/);
assert.equal(failed.note.includes("沒有寫入"), false);
assert.equal(failed.note.includes("沒有儲存"), false);
assert.equal(failed.note.includes("伺服器"), false);
const failedHint = glossaryRefusal(failed, saveFailed.reason);
assert.match(failedHint, /^沒有儲存：/);
assert.equal(failedHint.includes("沒有寫入"), false);
assert.equal(failedHint.includes("已儲存"), false);
assert.equal(glossaryRefusal({ note: "" }, "room-mismatch").startsWith("沒有儲存："), true);
assert.equal(glossaryRefusal({ note: "" }, "room-mismatch").includes("沒有寫入"), false);

// A reconnect must not wipe unsaved text or adopt a newer version.
let dirty = glossaryRoomState();
dirty = glossaryPrepareLoad(dirty, "room-a");
dirty = glossaryApplyLoaded(dirty, "room-a", dirty.generation, roomA, 1);
dirty = glossaryEdit(dirty, "甲=changed\n乙=b");
dirty = glossaryPrepareLoad(dirty, "room-a");
assert.equal(dirty.text, "甲=changed\n乙=b");
dirty = glossaryApplyLoaded(dirty, "room-a", dirty.generation, roomA, 9);
assert.equal(dirty.text, "甲=changed\n乙=b");
assert.equal(dirty.version, 1);
assert.match(dirty.note, /還沒儲存/);
assert.equal(dirty.note.includes("伺服器"), false);
assert.match(glossaryRefusal(dirty, "room-mismatch"), /還沒儲存|不是這個房間/);
let missed = glossaryRoomState();
missed = glossaryPrepareLoad(missed, "room-a");
missed = glossaryApplyLoaded(missed, "room-a", missed.generation, roomA, 1);
missed = glossaryEdit(missed, "甲=changed");
missed = glossaryPrepareLoad(missed, "room-a");
missed = glossaryApplyLoadFailure(missed, "room-a", missed.generation);
assert.equal(missed.text, "甲=changed");
assert.equal(missed.version, 1);
assert.match(missed.note, /讀不到這個房間已經存的術語/);
assert.match(missed.note, /還沒儲存的修改還在/);
assert.equal(missed.note.includes("沒有寫入"), false);
assert.equal(missed.note.includes("沒有儲存"), false);
assert.equal(missed.note.includes("伺服器"), false);
let missedClean = glossaryRoomState();
missedClean = glossaryPrepareLoad(missedClean, "room-a");
missedClean = glossaryApplyLoaded(missedClean, "room-a", missedClean.generation, roomA, 1);
const seen = missedClean.text;
missedClean = glossaryPrepareLoad(missedClean, "room-a");
missedClean = glossaryApplyLoadFailure(missedClean, "room-a", missedClean.generation);
assert.equal(missedClean.text, seen);
assert.equal(missedClean.version, 1);
assert.match(missedClean.note, /上次看到的內容/);
assert.equal(missedClean.note.includes("沒有寫入"), false);
assert.equal(missedClean.note.includes("伺服器"), false);

function plainTerm(zh, en) {
  return { zh, en, aliases: [], lock: true, note: "", category: "" };
}

// Text typed before the first load must not be saved over terms the host has not seen.
const thirty = Array.from({ length: 30 }, (_, i) => plainTerm("詞" + i, "e" + i));
let typedEarly = glossaryEdit(glossaryRoomState(), "般若=prajna");
typedEarly = glossaryPrepareLoad(typedEarly, "room-a");
typedEarly = glossaryApplyLoaded(typedEarly, "room-a", typedEarly.generation, thirty, 1);
assert.equal(typedEarly.text, "般若=prajna");
assert.equal(typedEarly.version, null);
assert.equal(typedEarly.prefillUnversioned, true);
assert.match(typedEarly.note, /這個房間已經存了 30 條術語/);
assert.match(typedEarly.note, /開頁前留下的內容/);
assert.match(typedEarly.note, /為了不蓋掉那 30 條/);
assert.match(typedEarly.note, /想留的詞複製起來/);
assert.equal(typedEarly.note.includes("沒有寫入"), false);
assert.equal(typedEarly.note.includes("沒有儲存"), false);
assert.equal(typedEarly.note.includes("伺服器"), false);
assert.equal(typedEarly.note.includes("N 條"), false);
const wipe = glossarySaveDecision(typedEarly, "room-a", "s");
assert.equal(wipe.post, false, "prefilled text must not be posted over unseen server terms");
assert.equal(wipe.reason, "no-version");
assert.equal(wipe.body, undefined);
const wipeHint = glossaryRefusal(typedEarly, wipe.reason);
assert.match(wipeHint, /^沒有儲存：這個房間已經存了 30 條術語/);
assert.equal(wipeHint.split("。")[0].startsWith("沒有儲存："), true);
assert.equal(wipeHint, "沒有儲存：" + typedEarly.note);
assert.equal(wipeHint.includes("沒有寫入"), false);
assert.equal(wipeHint.includes("已儲存"), false);
assert.equal(wipeHint.includes("伺服器"), false);

// An empty server glossary can still take text the host typed before the first load.
let typedEmpty = glossaryEdit(glossaryRoomState(), "般若=prajna");
typedEmpty = glossaryPrepareLoad(typedEmpty, "room-a");
typedEmpty = glossaryApplyLoaded(typedEmpty, "room-a", typedEmpty.generation, [], 0);
assert.equal(typedEmpty.text, "般若=prajna");
assert.equal(typedEmpty.version, 0);
const saveEmpty = glossarySaveDecision(typedEmpty, "room-a", "s");
assert.equal(saveEmpty.post, true);
assert.equal(saveEmpty.body.if_version, 0);
assert.equal(saveEmpty.body.text, "般若=prajna");

// 409 tells the host to reload. A browser that restores the stale textarea must not
// receive the new version, or the next save overwrites the remote edit.
let restored = glossaryEdit(glossaryRoomState(), "般若=prajna");
restored = glossaryPrepareLoad(restored, "class");
const remoteEdit = [plainTerm("甲", "remote-a"), plainTerm("乙", "remote-b")];
restored = glossaryApplyLoaded(restored, "class", restored.generation, remoteEdit, 7);
assert.equal(restored.text, "般若=prajna");
assert.equal(restored.version, null);
assert.notEqual(restored.version, 7);
assert.match(restored.note, /這個房間已經存了 2 條術語/);
assert.equal(restored.note.includes("沒有寫入"), false);
assert.equal(restored.note.includes("沒有儲存"), false);
assert.notEqual(restored.note, typedEarly.note);
const overwrite = glossarySaveDecision(restored, "class", "s");
assert.equal(overwrite.post, false, "restored text must not be posted with the remote version");
assert.equal(overwrite.reason, "no-version");
const overwriteHint = glossaryRefusal(restored, overwrite.reason);
assert.match(overwriteHint, /^沒有儲存：這個房間已經存了 2 條術語/);
assert.equal(overwriteHint, "沒有儲存：" + restored.note);
assert.equal(overwriteHint.includes("沒有寫入"), false);
assert.equal(overwriteHint.includes("沒有採用伺服器版本"), false);

// A→B→A, with B's response arriving last, keeps A. An older A response is dropped too.
let ordered = glossaryRoomState();
ordered = glossaryPrepareLoad(ordered, "room-a");
const genFirstA = ordered.generation;
ordered = glossaryApplyLoaded(ordered, "room-a", genFirstA, roomA, 1);
ordered = glossaryPrepareLoad(ordered, "room-b");
const genB = ordered.generation;
ordered = glossaryPrepareLoad(ordered, "room-a");
const genSecondA = ordered.generation;
assert.notEqual(genB, genSecondA);
const lateB = glossaryApplyLoaded(ordered, "room-b", genB, roomB, 9);
assert.equal(lateB.pendingRoom, "room-a");
assert.equal(lateB.room, null);
assert.equal(lateB.text, "");
assert.equal(lateB.version, null);
ordered = glossaryApplyLoaded(lateB, "room-a", genSecondA, roomA, 2);
assert.equal(ordered.room, "room-a");
assert.equal(ordered.version, 2);
assert.equal(ordered.text, glossaryBoxText(roomA));
const lateOldA = glossaryApplyLoaded(ordered, "room-a", genFirstA, [plainTerm("舊", "old")], 8);
assert.equal(lateOldA.room, "room-a");
assert.equal(lateOldA.version, 2);
assert.equal(lateOldA.text, glossaryBoxText(roomA));

// Save in flight, then the selector moves to B. The late 200 must not clear B or drop B's edit.
let inflight = glossaryRoomState();
inflight = glossaryPrepareLoad(inflight, "room-a");
inflight = glossaryApplyLoaded(inflight, "room-a", inflight.generation, roomA, 1);
inflight = glossaryEdit(inflight, "甲=A1\n乙=A2");
const saveInFlight = glossarySaveDecision(inflight, "room-a", "session-a");
assert.equal(saveInFlight.post, true);
const saveGen = inflight.generation;
const saveRoom = saveInFlight.body.room_id;
inflight = glossaryPrepareLoad(inflight, "room-b");
inflight = glossaryApplyLoaded(inflight, "room-b", inflight.generation, roomB, 1);
inflight = glossaryEdit(inflight, "丙=unsaved");
const unsavedB = inflight.text;
const settledLate = glossarySaveSettlement(inflight, saveRoom, saveGen);
assert.equal(settledLate.settle, false);
assert.equal(settledLate.reloadRoom, undefined);
assert.deepEqual(settledLate.state, inflight);
assert.equal(settledLate.state.room, "room-b");
assert.equal(settledLate.state.text, unsavedB);
assert.equal(settledLate.state.loadedText, glossaryBoxText(roomB));
assert.equal(settledLate.state.version, 1);
assert.equal(settledLate.state.prefillUnversioned, false);
const settledOther = glossarySaveSettlement(inflight, saveRoom, saveGen, "甲=A1\n乙=A2");
assert.equal(settledOther.settle, false);
assert.equal(settledOther.reloadRoom, undefined);
assert.deepEqual(settledOther.state, inflight);
const strayLoad = glossaryBeginLoad(settledLate.state, saveRoom, "room-b");
assert.equal(strayLoad.started, false);
assert.equal(strayLoad.state.text, unsavedB);
assert.equal(strayLoad.state.room, "room-b");
assert.notEqual(strayLoad.state.note, "正在讀取這個房間的術語。");
assert.equal(glossaryLoadTargetsSelector(saveRoom, "room-b"), false);
assert.equal(glossaryLoadTargetsSelector("room-b", "room-b"), true);
assert.equal(glossaryLoadTargetsSelector("  class ", "class"), true);

// Same room and the same generation: settlement marks saved and reloads that room.
let quiet = glossaryRoomState();
quiet = glossaryPrepareLoad(quiet, "room-a");
quiet = glossaryApplyLoaded(quiet, "room-a", quiet.generation, roomA, 1);
quiet = glossaryEdit(quiet, "甲=A1\n乙=A2");
const quietGen = quiet.generation;
const quietSave = glossarySaveDecision(quiet, "room-a", "s");
const appliedSave = glossarySaveSettlement(quiet, quietSave.body.room_id, quietGen);
assert.equal(appliedSave.settle, true);
assert.equal(appliedSave.reloadRoom, "room-a");
assert.equal(appliedSave.state.version, null);
assert.equal(appliedSave.state.loadedText, quiet.text);
assert.equal(appliedSave.state.text, quiet.text);
assert.equal(appliedSave.state.prefillUnversioned, false);
// Same room, newer generation: a reconnect started a load while the POST was in flight.
// Drop the stale version and remember the posted text, then the load takes the new
// version. The next save must not send if_version 1 once the server is already v2.
const reconnected = glossaryPrepareLoad(quiet, "room-a");
assert.equal(reconnected.version, 1);
assert.notEqual(reconnected.generation, quietGen);
const raced = glossarySaveSettlement(reconnected, "room-a", quietGen, quietSave.body.text);
assert.equal(raced.settle, true);
assert.equal(raced.reloadRoom, "room-a");
assert.equal(raced.state.room, "room-a");
assert.equal(raced.state.text, quiet.text);
assert.equal(raced.state.loadedText, quietSave.body.text);
assert.equal(raced.state.version, null);
assert.notEqual(raced.state.version, 1);
const savedTerms = [plainTerm("甲", "A1"), plainTerm("乙", "A2")];
const racedLoaded = glossaryApplyLoaded(raced.state, "room-a", raced.state.generation, savedTerms, 2);
assert.equal(racedLoaded.room, "room-a");
assert.equal(racedLoaded.text, quiet.text);
assert.equal(racedLoaded.loadedText, quiet.text);
assert.equal(racedLoaded.version, 2);
assert.notEqual(racedLoaded.version, 1);
const racedNext = glossarySaveDecision(racedLoaded, "room-a", "s");
assert.equal(racedNext.post, true);
assert.equal(racedNext.body.room_id, "room-a");
assert.equal(racedNext.body.text, quiet.text);
assert.equal(racedNext.body.if_version, 2);
assert.notEqual(racedNext.body.if_version, 1);

// The reconnect load already finished, still dirty and still on v1, before the 200.
// Settlement drops v1, and the reload the page starts then reads v2.
let racedLate = glossaryApplyLoaded(reconnected, "room-a", reconnected.generation, roomA, 1);
assert.equal(racedLate.version, 1);
assert.equal(racedLate.text, quiet.text);
assert.notEqual(racedLate.loadedText, quiet.text);
const racedLateSettled = glossarySaveSettlement(racedLate, "room-a", quietGen, quietSave.body.text);
assert.equal(racedLateSettled.settle, true);
assert.equal(racedLateSettled.reloadRoom, "room-a");
assert.equal(racedLateSettled.state.version, null);
assert.equal(racedLateSettled.state.loadedText, quiet.text);
assert.equal(racedLateSettled.state.text, quiet.text);
assert.notEqual(racedLateSettled.state.version, 1);
let racedReload = glossaryPrepareLoad(racedLateSettled.state, "room-a");
racedReload = glossaryApplyLoaded(racedReload, "room-a", racedReload.generation, savedTerms, 2);
assert.equal(racedReload.version, 2);
assert.equal(racedReload.text, quiet.text);
const racedReloadSave = glossarySaveDecision(racedReload, "room-a", "s");
assert.equal(racedReloadSave.post, true);
assert.equal(racedReloadSave.body.if_version, 2);
assert.notEqual(racedReloadSave.body.if_version, 1);
assert.equal(racedReloadSave.body.text, quiet.text);
assert.equal(racedReloadSave.body.room_id, "room-a");

// Keystrokes after the POST stay in the box and are not posted over the saved terms.
const racedExtra = glossarySaveSettlement(
  glossaryEdit(reconnected, quiet.text + "\n丙=extra"),
  "room-a",
  quietGen,
  quietSave.body.text,
);
assert.equal(racedExtra.state.text, quiet.text + "\n丙=extra");
assert.equal(racedExtra.state.loadedText, quietSave.body.text);
assert.equal(racedExtra.state.version, null);
const racedExtraLoaded = glossaryApplyLoaded(
  racedExtra.state,
  "room-a",
  racedExtra.state.generation,
  savedTerms,
  2,
);
assert.equal(racedExtraLoaded.text, quiet.text + "\n丙=extra");
assert.notEqual(racedExtraLoaded.text, glossaryBoxText(savedTerms));
assert.equal(racedExtraLoaded.version, null);
const racedExtraSave = glossarySaveDecision(racedExtraLoaded, "room-a", "s");
assert.equal(racedExtraSave.post, false, "unsaved keystrokes must not be posted over the saved glossary");
assert.equal(racedExtraSave.reason, "no-version");
assert.equal(racedExtraSave.body, undefined);

// The lock hint names the room on screen and points at a person, not a bare placeholder.
let named = glossaryRoomState();
named = glossaryPrepareLoad(named, "class");
named = glossaryApplyLoaded(named, "class", named.generation, [
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: false, note: "", category: "" },
], 1);
assert.equal(named.locked, true);
assert.match(named.note, /PUT \/api\/rooms\/class\/glossary/);
assert.equal(named.note.includes("{room_id}"), false);
assert.match(named.note, /請找負責詞表的人/);
assert.match(named.note, /修改房間術語表/);

let marked = glossaryEdit(glossaryRoomState(), "甲=changed");
marked = { ...marked, room: "room-a", version: 3, loadedText: "甲=old", prefillUnversioned: true };
marked = glossaryMarkSaved(marked);
assert.equal(marked.text, "甲=changed");
assert.equal(marked.loadedText, "甲=changed");
assert.equal(marked.version, null);
assert.equal(marked.prefillUnversioned, false);
assert.equal(marked.room, "room-a");
