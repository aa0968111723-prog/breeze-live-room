// The host page must show rejected glossary lines and must not say the room was saved.

import assert from "node:assert/strict";
import fs from "node:fs";

import {
  glossaryBoxText,
  glossaryLegacyBlock,
  glossaryPostBody,
  glossarySaveLine,
  glossarySaveRequest,
  glossaryTransportLine,
} from "../app/static/host_glossary.js";

const saved = glossarySaveLine(200, { ok: true, count: 2 });
assert.equal(saved, "已儲存這個房間的術語。");

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
assert.match(over.note, /編輯器/);
for (const term of [
  { zh: "禪學社", en: "Zen Club", aliases: ["柴學社"], lock: true, note: "", category: "" },
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: false, note: "", category: "" },
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: true, note: "備註", category: "" },
  { zh: "禪學社", en: "Zen Club", aliases: [], lock: true, note: "", category: "社團" },
]) {
  const block = glossaryLegacyBlock([term]);
  assert.equal(block.locked, true, JSON.stringify(term));
  assert.match(block.note, /進階/);
  assert.match(block.note, /編輯器/);
}
const both = glossaryLegacyBlock(many.map((term, i) => (i === 0 ? { ...term, aliases: ["別名"] } : term)));
assert.equal(both.locked, true);
assert.match(both.note, /200/);
assert.match(both.note, /進階/);

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
assert.match(html, /glossarySaveRequest/);
assert.match(html, /glossaryTransportLine/);
assert.match(html, /id="glossary-note"/);
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
assert.match(save, /glossaryLocked/);
assert.ok(save.indexOf("glossaryLocked") < save.indexOf("reportGlossary"));
assert.match(save, /reportGlossary/);
assert.match(save, /glossaryTransportLine/);
assert.match(post, /glossarySaveRequest/);
assert.equal(post.includes("/api/rooms/"), false);
assert.equal(/Number\.isInteger\(/.test(post), false);
assert.match(post, /409/);
assert.equal(post.includes("這頁剛剛讀到"), false);
assert.match(setup, /loadGlossary/);
assert.match(listen, /loadGlossary/);
assert.match(listen, /已連上/);
