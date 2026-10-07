// The host page must show rejected glossary lines and must not say the room was saved.

import assert from "node:assert/strict";
import fs from "node:fs";

import { glossaryPostBody, glossarySaveLine, glossaryTransportLine } from "../app/static/host_glossary.js";

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

const html = fs.readFileSync(new URL("../app/static/host.html", import.meta.url), "utf8");
assert.match(html, /glossarySaveLine/);
assert.equal(html.includes('line("已儲存這個房間的術語。")'), false);
assert.match(html, /reportGlossary/);
assert.match(html, /glossaryPostBody/);
assert.match(html, /glossaryTransportLine/);

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
assert.equal(/reportGlossary|postGlossary|\/api\/glossary/.test(recover), false);
assert.equal(/reportGlossary|postGlossary|\/api\/glossary/.test(start), false);
assert.match(save, /reportGlossary/);
assert.match(save, /glossaryTransportLine/);
assert.match(post, /if_version|glossaryPostBody/);
assert.match(post, /Number\.isInteger\(version\)/);
