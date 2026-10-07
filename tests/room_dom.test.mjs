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
assert.match(html, /font-size:\s*calc\(var\(--size\)\s*\*\s*var\(--project-scale\)\)/);
assert.match(html, /font-size:\s*calc\(var\(--size\)\s*\*\s*var\(--project-scale\)\s*\*\s*\.62\)/);

const buttonBody = html.slice(html.indexOf("#c9843f") - 40, html.indexOf("#c9843f") + 40);
assert.match(buttonBody, /#c9843f/);
assert.match(buttonBody, /#16130f/);
assert.ok(contrast("#16130f", "#c9843f") >= 4.5);
assert.ok(contrast("#16130f", "#c9843f") >= 6);

const header = html.split("<header>")[1].split("</header>")[0];
assert.match(header, /id="leave-project"[^>]*>離開投影</);
assert.match(html, /html\.project #leave-project, body\.project #leave-project \{ display: inline-block; \}/);
assert.match(body, /#leave-project"\)\.onclick = \(\) => \{ remember\(\{ \.\.\.prefs, mode: "both" \}\); \}/);
assert.match(body, /document\.documentElement\.classList\.toggle/);
assert.match(body, /safeStorage\(\(\) => window\.sessionStorage\)/);
assert.match(body, /safeStorage\(\(\) => window\.localStorage\)/g);
const reads = body.match(/window\.(localStorage|sessionStorage)/g) || [];
const wrapped = body.match(/safeStorage\(\(\) => window\.(localStorage|sessionStorage)\)/g) || [];
assert.equal(reads.length, wrapped.length);
assert.ok(wrapped.length >= 3);
assert.match(body, /visibilitychange/);
assert.match(body, /conn\.nudge\(\)/);

console.log("room dom ok");
