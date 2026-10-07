// The host page must show rejected glossary lines and must not say the room was saved.

import assert from "node:assert/strict";
import fs from "node:fs";

import {
  glossaryApplyLoadFailure,
  glossaryApplyLoaded,
  glossaryBoxText,
  glossaryEdit,
  glossaryLegacyBlock,
  glossaryPostBody,
  glossaryPrepareLoad,
  glossaryRefusal,
  glossaryRoomState,
  glossarySaveDecision,
  glossarySaveLine,
  glossarySaveRequest,
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
for (const term of [
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: false, note: "", category: "" },
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: true, note: "備註", category: "" },
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: true, note: "", category: "社團" },
  { zh: "禪學社", en: "Zen Club", aliases: ["柴|學社"], lock: true, note: "", category: "" },
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
assert.equal(/reportGlossary|postGlossary|\/api\/glossary/.test(recover), false);
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
assert.match(post, /glossaryMarkSaved/);
assert.equal(post.includes("這頁剛剛讀到"), false);
assert.match(setup, /loadGlossary/);
assert.match(listen, /loadGlossary/);
assert.match(listen, /已連上/);
assert.match(html, /glossaryPrepareLoad/);
assert.match(html, /glossaryApplyLoadFailure/);
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
const failedHint = glossaryRefusal(failed, saveFailed.reason);
assert.match(failedHint, /沒有寫入/);
assert.equal(failedHint.includes("已儲存"), false);

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
assert.match(glossaryRefusal(dirty, "room-mismatch"), /還沒儲存|不是這個房間/);
