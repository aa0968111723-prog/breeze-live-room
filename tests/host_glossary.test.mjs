// The host page must show rejected glossary lines and must not say the room was saved.

import assert from "node:assert/strict";
import fs from "node:fs";

import { glossarySaveLine } from "../app/static/host_glossary.js";

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
assert.equal(conflict.includes("第 0 行"), false);

const empty = glossarySaveLine(400, { ok: false, rejected: [{ line: 0, reason: "沒有有效的術語，不會清空這個房間的詞表" }] });
assert.equal(empty.includes("已儲存"), false);
assert.match(empty, /不會清空/);

const opaque = glossarySaveLine(500, null);
assert.equal(opaque.includes("已儲存"), false);
assert.match(opaque, /沒有寫入/);

const html = fs.readFileSync(new URL("../app/static/host.html", import.meta.url), "utf8");
assert.match(html, /glossarySaveLine/);
assert.equal(html.includes('line("已儲存這個房間的術語。")'), false);
assert.match(html, /reportGlossary/);
