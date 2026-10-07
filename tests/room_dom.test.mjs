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
assert.match(html, /--project-vw:\s*7vw/);
assert.match(html, /--project-vh:\s*10vh/);
assert.match(html, /html\.project\.size-28, body\.project\.size-28 \{ --project-vw: 5\.5vw; --project-vh: 9vh; \}/);
assert.match(html, /html\.project\.size-64, body\.project\.size-64 \{ --project-vh: 13vh; \}/);
const projectFont = "font-size: max(var(--size), min(calc(var(--size) * var(--project-scale)), var(--project-vw), var(--project-vh))); overflow-wrap: anywhere; min-width: 0;";
assert.ok(html.includes("html.project .en, body.project .en { " + projectFont));
assert.ok(html.includes("html.project .zh, body.project .zh { " + projectFont));
assert.doesNotMatch(html, /project-scale\)\s*\*\s*\.62/);
assert.match(html, /html\.project body, body\.project \{[^}]*max-height:\s*100dvh[^}]*overflow:\s*hidden/);
assert.match(html, /body\.project \.stage \{[^}]*align-content:\s*flex-end[^}]*min-height:\s*0[^}]*overflow:\s*hidden/);

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
const style = html.slice(html.indexOf("<style>") + 7, html.indexOf("</style>"));
assert.match(style, /^\s*\[hidden\]\s*\{\s*display:\s*none\s*!important\s*;?\s*\}/);
assert.match(html, /#drawer label \{[^}]*min-height:\s*44px/);
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

function projectCaps(token) {
  if (token === "28") return { vw: 0.055, vh: 0.09 };
  if (token === "64") return { vw: 0.07, vh: 0.13 };
  return { vw: 0.07, vh: 0.1 };
}

function projectFontPx(sizePx, width, height, token) {
  const caps = projectCaps(token);
  return Math.max(sizePx, Math.min(sizePx * 1.8, width * caps.vw, height * caps.vh));
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
function authorHiddenWins(sheet) {
  return /^\s*\[hidden\]\s*\{\s*display:\s*none\s*!important/.test(sheet);
}

// Header: 8+8 padding and a 44px control row. At <=390 the status wraps onto its own line.
function headerHeight(width) {
  const padY = 8 + 8;
  const row = 44;
  const wraps = width <= 390 && /@media \(max-width:\s*390px\)[\s\S]*#state \{ flex: 1 0 100%;/.test(html);
  if (!wraps) return padY + row;
  return padY + row + 8 + 24;
}

// Open drawer: padding 8+12, 44px rows, 8px gaps. Phones wrap to three rows
// (168px); from 768px the controls sit on one row (64px). Projection collapses it.
function openDrawerHeight(width) {
  const pad = 8 + 12;
  const row = 44;
  const gap = 8;
  const rows = width < 768 ? 3 : 1;
  return pad + rows * row + (rows - 1) * gap;
}

function drawerHeight(width) {
  if (authorHiddenWins(style)) return 0;
  return openDrawerHeight(width);
}

const phoneFonts = ["28", "34", "46", "64"].map((token) => projectFontPx(declaredSize(token, 390), 390, 844, token));
assert.ok(phoneFonts[0] < phoneFonts[1], `phone 小 ${phoneFonts[0]} vs 中 ${phoneFonts[1]}`);
const deskFonts = ["28", "34", "46", "64"].map((token) => projectFontPx(declaredSize(token, 1280), 1280, 720, token));
assert.ok(deskFonts[2] < deskFonts[3], `720p 大 ${deskFonts[2]} vs 特大 ${deskFonts[3]}`);
for (let i = 1; i < deskFonts.length; i += 1) {
  assert.ok(deskFonts[i - 1] <= deskFonts[i], deskFonts.join(","));
}

for (const viewport of viewports) {
  for (const size of ["28", "34", "46", "64"]) {
    const sizePx = declaredSize(size, viewport.width);
    const font = projectFontPx(sizePx, viewport.width, viewport.height, size);
    const content = captionContentWidth(viewport.width);
    assert.ok(font <= content, `glyph ${font}px wider than ${content}px at ${viewport.width} size-${size}`);
    const natural = [...CHINESE].length * font;
    const canWrap = html.includes("overflow-wrap: anywhere") && html.includes("min-width: 0");
    const usedWidth = canWrap ? font : natural;
    assert.ok(usedWidth <= content, `zh uses ${usedWidth}px > ${content}px at ${viewport.width} size-${size}`);
    const stacked = viewport.width - 16 < (18 * REM + 40) * 2;
    const enH = blockHeight(font, ENGLISH.length, content, 0.5, 1.35, 20);
    const zhH = blockHeight(font, [...CHINESE].length, content, 1, 1.45, 20);
    const block = stacked ? enH + zhH : Math.max(enH, zhH);
    const header = headerHeight(viewport.width);
    const drawer = drawerHeight(viewport.width);
    const bottom = header + drawer + block;
    const clips = /max-height:\s*100dvh/.test(html) && /body\.project \.stage \{[^}]*overflow:\s*hidden/.test(html) && /align-content:\s*flex-end/.test(html);
    if (bottom > viewport.height) {
      assert.ok(clips, `bottom ${bottom} = header ${header} + drawer ${drawer} + caption ${block} exceeds ${viewport.height} at ${viewport.width} size-${size}`);
      const room = viewport.height - header - drawer;
      const newest = font * 1.35 + 20;
      assert.ok(room >= newest, `room ${room}px < newest line ${newest}px at ${viewport.width} size-${size} (header ${header} drawer ${drawer})`);
    }
    if (viewport.width === 320 && viewport.height === 568 && size === "46") {
      assert.ok(bottom <= viewport.height, `320 size-46 must fit: header ${header} + drawer ${drawer} + caption ${block} = ${bottom}`);
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

assert.match(body, /const text = expiryNotice\(data && data\.ids, items\);\s*if \(!text\) return;/);
assert.match(body, /retryCountdown\(connDetail\.nextRetryAt/);
assert.match(body, /setInterval\(\(\) => \{ paintChrome\(\); paintSubtitle\(\); \}, 1000\)/);

// Author display vs the user-agent [hidden] rule. #drawer { display:flex } and
// #drawer label { display:inline-flex } used to win, so hidden stayed on screen.
function identAt(text, index) {
  const match = /^[\w-]+/.exec(text.slice(index));
  return { name: match ? match[0] : "", next: index + (match ? match[0].length : 0) };
}

function matchCompound(el, compound) {
  if (compound === "*") return true;
  let index = 0;
  let saw = false;
  while (index < compound.length) {
    const ch = compound[index];
    if (ch === "#") {
      const id = identAt(compound, index + 1);
      if (el.id !== id.name) return false;
      index = id.next;
      saw = true;
    } else if (ch === ".") {
      const cls = identAt(compound, index + 1);
      if (!el.classes.has(cls.name)) return false;
      index = cls.next;
      saw = true;
    } else if (ch === "[") {
      const end = compound.indexOf("]", index);
      if (end < 0) return false;
      const inner = compound.slice(index + 1, end);
      if (inner === "hidden") {
        if (!el.hidden) return false;
      } else {
        const attr = inner.match(/^([\w-]+)(?:\s*[~|^$*]?=\s*"?([^"\]]+)"?)?$/);
        if (!attr) return false;
        const actual = el.attrs && el.attrs[attr[1]];
        if (attr[2] == null) {
          if (actual == null) return false;
        } else if (actual !== attr[2]) return false;
      }
      index = end + 1;
      saw = true;
    } else if (ch === ":") {
      return false;
    } else if (/[a-z*]/i.test(ch)) {
      const tag = identAt(compound, index);
      if (tag.name !== "*" && el.tag !== tag.name) return false;
      index = tag.next;
      saw = true;
    } else {
      index += 1;
    }
  }
  return saw;
}

function matchesSelector(el, selector) {
  const parts = selector.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return false;
  let node = el;
  if (!matchCompound(node, parts[parts.length - 1])) return false;
  for (let part = parts.length - 2; part >= 0; part -= 1) {
    node = node.parent;
    let found = false;
    while (node) {
      if (matchCompound(node, parts[part])) {
        found = true;
        break;
      }
      node = node.parent;
    }
    if (!found) return false;
  }
  return true;
}

function specificity(selector) {
  const ids = (selector.match(/#[\w-]+/g) || []).length;
  const classes = (selector.match(/\.[\w-]+/g) || []).length;
  const attrs = (selector.match(/\[[^\]]+\]/g) || []).length;
  const stripped = selector
    .replace(/#[\w-]+/g, " ")
    .replace(/\.[\w-]+/g, " ")
    .replace(/\[[^\]]+\]/g, " ")
    .replace(/:[\w-]+(?:\([^)]*\))?/g, " ");
  const elements = stripped.split(/[\s>+~]+/).filter((part) => part && part !== "*").length;
  return [ids, classes + attrs, elements];
}

function authorDisplayRules(sheet) {
  const rules = [];
  const pattern = /([^{}@]+)\{([^{}]*)\}/g;
  let found;
  while ((found = pattern.exec(sheet))) {
    const bodyText = found[2];
    const declared = bodyText.match(/display\s*:\s*([^;]+)/);
    if (!declared) continue;
    const important = /display\s*:[^;]*!important/.test(bodyText);
    const value = declared[1].replace(/!important/g, "").trim();
    for (const raw of found[1].split(",")) {
      const selector = raw.trim();
      if (!selector) continue;
      rules.push({ selector, value, important, specificity: specificity(selector), order: rules.length });
    }
  }
  return rules;
}

function computedDisplay(el, sheet) {
  const initial = { div: "block", p: "block", label: "inline", button: "inline-block", span: "inline" }[el.tag] || "inline";
  let winner = { value: initial, rank: [-1, 0, 0, 0, -1] };
  const rules = [
    { selector: "[hidden]", value: "none", important: false, specificity: [0, 1, 0], order: -1, origin: 0 },
    ...authorDisplayRules(sheet).map((rule) => ({ ...rule, origin: 1 })),
  ];
  for (const rule of rules) {
    if (!matchesSelector(el, rule.selector)) continue;
    const importance = rule.important ? 2 : rule.origin === 0 ? 0 : 1;
    const rank = [importance, ...rule.specificity, rule.order];
    let better = false;
    for (let i = 0; i < rank.length; i += 1) {
      if (rank[i] === winner.rank[i]) continue;
      better = rank[i] > winner.rank[i];
      break;
    }
    if (better) winner = { value: rule.value, rank };
  }
  return winner.value;
}

function element(fields) {
  return {
    tag: fields.tag,
    id: fields.id || "",
    classes: fields.classes || new Set(),
    hidden: !!fields.hidden,
    attrs: fields.attrs || {},
    parent: fields.parent || null,
  };
}

const page = element({ tag: "body" });
const drawer = element({ tag: "div", id: "drawer", hidden: true, parent: page });
assert.equal(computedDisplay(drawer, style), "none", "collapsed drawer must not paint");
drawer.hidden = false;
assert.equal(computedDisplay(drawer, style), "flex", "opening the drawer shows the controls");
drawer.hidden = true;
assert.equal(computedDisplay(drawer, style), "none", "closing the drawer hides it again");

const wakeLabel = element({ tag: "label", id: "wake-label", hidden: true, parent: drawer });
const wakeHelp = element({ tag: "p", id: "wake-help", hidden: false, parent: drawer });
assert.equal(computedDisplay(wakeLabel, style), "none", "unsupported wake lock hides the dead toggle");
assert.notEqual(computedDisplay(wakeHelp, style), "none", "unsupported wake lock keeps the note");
wakeLabel.hidden = false;
wakeHelp.hidden = true;
assert.notEqual(computedDisplay(wakeLabel, style), "none");
assert.equal(computedDisplay(wakeHelp, style), "none");

console.log("room dom ok");
