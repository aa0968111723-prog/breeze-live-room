// DOM wiring for the audience page: no white flash, projection keeps the
// user's text size, and dark/projection buttons clear the contrast floor.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { contrast } from "../app/static/room_view.js";

const html = readFileSync(new URL("../app/static/room.html", import.meta.url), "utf8");
const head = html.slice(0, html.indexOf("</head>"));
const body = html.slice(html.indexOf("</head>"));

assert.equal(head.includes('<script type="module">'), false);
const inline = [...head.matchAll(/<script(?![^>]*\btype\s*=)[^>]*>([\s\S]*?)<\/script>/g)];
assert.equal(inline.length, 1, "head needs one synchronous prefs script");
const source = inline[0][1];
assert.match(source, /breeze\.audience\.prefs/);
assert.match(source, /Content-Security-Policy/);
assert.match(source, /hash/);
assert.equal(source.includes("async"), false);
assert.equal(source.includes("defer"), false);

function boot(storage, mediaDark = false) {
  const classes = [];
  const context = {
    localStorage: storage,
    document: {
      documentElement: {
        classList: { add(name) { classes.push(name); } },
      },
    },
    window: {
      matchMedia() { return { matches: !!mediaDark }; },
    },
  };
  vm.runInNewContext(source, context);
  return classes;
}

const stored = (prefs) => ({
  getItem(key) {
    return key === "breeze.audience.prefs" ? JSON.stringify(prefs) : null;
  },
});

assert.deepEqual(
  boot(stored({ mode: "project", size: "64", theme: "light" })),
  ["size-64", "project"],
);
assert.deepEqual(
  boot(stored({ mode: "both", size: "28", theme: "dark" })),
  ["size-28", "dark"],
);
assert.ok(boot(stored({ theme: "system", size: "46" }), true).includes("dark"));
assert.equal(boot(stored({ theme: "system", size: "46" }), true).includes("size-46"), true);
assert.deepEqual(boot(stored({ theme: "light" }), true), []);
assert.deepEqual(boot({ getItem() { throw new Error("SecurityError"); } }), []);
assert.deepEqual(boot({ getItem() { return "{"; } }), []);

assert.doesNotMatch(html, /body\.project\s*\{[^}]*--size\s*:/);
assert.doesNotMatch(html, /html\.project body\s*\{[^}]*--size\s*:/);
assert.match(html, /--project-scale:\s*1\.8/);
const projectFont = "font-size: max(var(--size), min(calc(var(--size)*1.8), 7vw, 10vh)); overflow-wrap: anywhere; min-width: 0;";
assert.ok(html.includes("html.project .en, body.project .en { " + projectFont));
assert.ok(html.includes("html.project .zh, body.project .zh { " + projectFont));
assert.doesNotMatch(html, /project-scale\)\s*\*\s*\.62/);

const buttonBody = html.slice(html.indexOf("#c9843f") - 40, html.indexOf("#c9843f") + 40);
assert.match(buttonBody, /#c9843f/);
assert.match(buttonBody, /#16130f/);
assert.ok(contrast("#16130f", "#c9843f") >= 4.5);
assert.ok(contrast("#16130f", "#c9843f") >= 6);

const header = html.split("<header>")[1].split("</header>")[0];
assert.match(header, /id="leave-project"[^>]*>離開投影</);
assert.match(html, /html\.project #leave-project, body\.project #leave-project \{ display: inline-block; \}/);
assert.match(body, /#leave-project"\)\.onclick = \(\) => \{[\s\S]*remember\(\{ \.\.\.prefs, mode: "both" \}\);[\s\S]*restoreProjectionFocus\(\)/);
assert.match(body, /leaveProjection\(prefs, ev\.key\)[\s\S]*restoreProjectionFocus\(\)/);
assert.match(body, /focusAfterProjection\(back\)/);
assert.match(html, /#drawer label \{ min-height: 44px; display: inline-flex; align-items: center; gap: 8px; \}/);
assert.match(html, /#drawer input\[type="checkbox"\] \{ width: 24px; height: 24px; margin: 0; \}/);
assert.match(html, /id="offline"[^>]*>手機沒網路。恢復網路後會自動接回。</);
const subRule = html.match(/\.sub \{[^}]+\}/);
assert.ok(subRule);
assert.equal(subRule[0].includes("ellipsis"), false);
assert.equal(subRule[0].includes("nowrap"), false);
assert.match(html, /#state \{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;/);
assert.match(html, /@media \(max-width: 390px\) \{[\s\S]*html\.project #state, body\.project #state \{ flex: 1 0 100%; min-width: 100%; overflow: visible; text-overflow: unset; white-space: normal; overflow-wrap: anywhere; \}/);
assert.match(body, /document\.documentElement\.classList\.toggle/);
assert.match(body, /safeStorage\(\(\) => window\.sessionStorage\)/);
assert.match(body, /safeStorage\(\(\) => window\.localStorage\)/g);
const reads = body.match(/window\.(localStorage|sessionStorage)/g) || [];
const wrapped = body.match(/safeStorage\(\(\) => window\.(localStorage|sessionStorage)\)/g) || [];
assert.equal(reads.length, wrapped.length);
assert.ok(wrapped.length >= 3);
assert.match(body, /visibilitychange/);
assert.match(body, /conn\.nudge\(\)/);
assert.match(body, /stageNoteFor\(kind, mode, hostLive\)/);

// Stylesheet arithmetic, not a layout engine. 1rem is the browser default 16px.
// Headless Chrome on a developer machine can repeat this; CI does not launch a browser.
const REM = 16;
const ENGLISH = "A".repeat(72);
const CHINESE = "字".repeat(36);

function declaredSize(token, width) {
  const block = html.match(new RegExp(String.raw`size-${token}[^{]*\{[^}]*--size:\s*clamp\(([^)]+)\)`));
  assert.ok(block, token);
  const px = (part) => {
    const text = part.trim();
    if (text.endsWith("rem")) return parseFloat(text) * REM;
    if (text.endsWith("vw")) return (parseFloat(text) / 100) * width;
    throw new Error(text);
  };
  const [min, preferred, max] = block[1].split(",").map(px);
  return Math.min(max, Math.max(min, preferred));
}

function projectFontPx(sizePx, width, height) {
  return Math.max(sizePx, Math.min(sizePx * 1.8, width * 0.07, height * 0.1));
}

function captionContentWidth(viewport) {
  const stagePad = 16;
  const textPad = 40;
  const itemBorder = 18 * REM + textPad;
  const inner = viewport - stagePad;
  if (inner >= itemBorder * 2) return inner / 2 - textPad;
  return inner - textPad;
}

function blockHeight(fontPx, chars, contentWidth, charRatio, lineHeight, padY) {
  const perLine = Math.max(1, Math.floor(contentWidth / (fontPx * charRatio)));
  const lines = Math.ceil(chars / perLine);
  return padY + lines * fontPx * lineHeight;
}

const viewports = [
  { width: 320, height: 568 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1280, height: 720 },
];
for (const viewport of viewports) {
  for (const size of ["46", "64"]) {
    const sizePx = declaredSize(size, viewport.width);
    const font = projectFontPx(sizePx, viewport.width, viewport.height);
    const content = captionContentWidth(viewport.width);
    assert.ok(font <= content, `glyph ${font}px wider than ${content}px at ${viewport.width} size-${size}`);
    const wrapped = Math.min(content, Math.max(font, Math.min([...CHINESE].length * font, content)));
    assert.ok(wrapped <= content);
    if (!html.includes("overflow-wrap: anywhere") || !html.includes("min-width: 0")) {
      assert.ok([...CHINESE].length * font <= content);
    }
    if (viewport.width === 1280 && viewport.height === 720) {
      const enH = blockHeight(font, ENGLISH.length, content, 0.5, 1.35, 20);
      const zhH = blockHeight(font, [...CHINESE].length, content, 1, 1.45, 20);
      assert.ok(enH <= viewport.height, `en ${enH} size-${size}`);
      assert.ok(zhH <= viewport.height, `zh ${zhH} size-${size}`);
    }
  }
}
const uncapped = declaredSize("64", 1280) * 1.8;
const uncappedHeight = blockHeight(uncapped, ENGLISH.length, captionContentWidth(1280), 0.5, 1.35, 20);
assert.ok(uncappedHeight > 720, "the fixture must still overflow the old uncapped projection size");

function projectionStateWidth(viewport) {
  const content = viewport - 24;
  const ownRow = viewport <= 390 && /@media \(max-width:\s*390px\)[\s\S]*#state \{ flex: 1 0 100%; min-width: 100%;/.test(html);
  if (ownRow) return content;
  return content - (24 + 4 * 16) - (24 + 3 * 16) - 16;
}
assert.ok(projectionStateWidth(320) >= 240, String(projectionStateWidth(320)));
assert.ok(projectionStateWidth(390) >= 240, String(projectionStateWidth(390)));

console.log("room dom ok");
