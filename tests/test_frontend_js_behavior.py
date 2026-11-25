"""Execute the real rendered `app.<hash>.js` inside Node.js to verify scroll restoration, multi-item nav, dark-mode toggle, search, and cold-item fallback."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from daily_radar.config import RepoConfig
from daily_radar.renderer import build_static_site


REPO_ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "Node.js binary required for frontend JS execution test")
class FrontendJSBehaviorTest(unittest.TestCase):
    def test_app_js_scroll_restore_prev_next_dark_toggle_search_and_cold_viewer(self) -> None:
        site_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(site_tmp.cleanup)
        site_dir = Path(site_tmp.name) / "site"
        build_static_site(content_dir=REPO_ROOT / "content", repo_cfg=RepoConfig.load(REPO_ROOT), out_dir=site_dir)
        js_files = list((site_dir / "assets").glob("app.*.js"))
        self.assertEqual(len(js_files), 1)
        app_js_path = js_files[0]

        harness = r"""
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const siteDir = process.argv[1];
const appJsCode = fs.readFileSync(process.argv[2], "utf8");

function makeStorage(initial = {}) {
  const store = Object.assign({}, initial);
  return {
    _store: store,
    getItem(k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
    setItem(k, v) { store[k] = String(v); },
    removeItem(k) { delete store[k]; },
  };
}

function makeEl(tag, attrs = {}) {
  const classes = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
  const listeners = {};
  const el = {
    tagName: tag.toUpperCase(),
    id: attrs.id || "",
    value: attrs.value || "",
    checked: !!attrs.checked,
    textContent: attrs.textContent || "",
    innerHTML: "",
    style: {},
    offsetWidth: 100,
    _attrs: Object.assign({}, attrs),
    _listeners: listeners,
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
    },
    getAttribute(k) { return Object.prototype.hasOwnProperty.call(this._attrs, k) ? this._attrs[k] : null; },
    setAttribute(k, v) { this._attrs[k] = String(v); },
    addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
    dispatch(ev, payload = {}) {
      const fns = listeners[ev] || [];
      for (const fn of fns) {
        fn(Object.assign({ currentTarget: el, target: el, preventDefault() {} }, payload));
      }
    },
    click() { this.dispatch("click"); },
    focus() { this._focused = true; },
    scrollIntoView() { this._scrolledIntoView = true; },
    closest(sel) { return null; },
  };
  return el;
}

function mountPage(opts) {
  const rootEl = makeEl("html", opts.rootAttrs);
  const elementsById = {};
  const selectorMap = opts.selectorMap || {};
  for (const [id, el] of Object.entries(opts.byId || {})) {
    elementsById[id] = el;
  }
  const docListeners = {};
  const winListeners = {};
  let scrolledTo = null;
  let historyBackCalls = 0;
  const locationObj = {
    origin: "https://phylipppp-zzy.github.io",
    pathname: opts.pathname,
    href: "https://phylipppp-zzy.github.io" + opts.pathname + (opts.search || ""),
    search: opts.search || "",
  };

  const document = {
    documentElement: rootEl,
    referrer: opts.referrer || "",
    body: { appendChild() {}, removeChild() {} },
    getElementById(id) { return elementsById[id] || null; },
    querySelectorAll(sel) { return selectorMap[sel] || []; },
    querySelector(sel) {
      if (selectorMap[sel] && selectorMap[sel].length) return selectorMap[sel][0];
      const m = sel.match(/^\.summary-card\[data-item-slug="([^"]+)"\]$/);
      if (m) {
        const cards = selectorMap['.summary-card[data-item-slug]'] || [];
        return cards.find(c => c.getAttribute("data-item-slug") === m[1]) || null;
      }
      return null;
    },
    addEventListener(ev, fn) { (docListeners[ev] = docListeners[ev] || []).push(fn); },
    dispatch(ev, payload = {}) {
      for (const fn of (docListeners[ev] || [])) fn(Object.assign({ preventDefault() {} }, payload));
    },
  };

  const windowObj = {
    location: locationObj,
    scrollY: opts.scrollY || 0,
    pageYOffset: opts.scrollY || 0,
    scrollTo(x, y) { scrolledTo = [x, y]; },
    history: { back() { historyBackCalls += 1; } },
    addEventListener(ev, fn) { (winListeners[ev] = winListeners[ev] || []).push(fn); },
    dispatch(ev) { for (const fn of (winListeners[ev] || [])) fn({}); },
    matchMedia(q) {
      if (q.includes("prefers-color-scheme: dark")) return { matches: !!opts.osDark };
      if (q.includes("min-width: 1024px")) return { matches: !!opts.desktop };
      return { matches: false };
    },
  };

  function localFetch(relUrl) {
    const diskPath = path.resolve(siteDir, opts.baseRelDir || ".", relUrl);
    if (!fs.existsSync(diskPath)) {
      return Promise.resolve({ ok: false, status: 404, json: () => Promise.reject(new Error("404")) });
    }
    const data = JSON.parse(fs.readFileSync(diskPath, "utf8"));
    if (opts.mapResponse) opts.mapResponse(data);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(data) });
  }

  const ctx = vm.createContext({
    window: windowObj,
    document,
    localStorage: opts.localStorage,
    sessionStorage: opts.sessionStorage,
    navigator: { clipboard: { writeText: () => Promise.resolve() } },
    fetch: localFetch,
    URL,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    Date,
    Promise,
  });
  vm.runInContext(appJsCode, ctx);
  return { rootEl, document, windowObj, elementsById, getScrolledTo: () => scrolledTo, getHistoryBackCalls: () => historyBackCalls };
}

(async () => {
  const session = makeStorage();
  const local = makeStorage();

  // 1. OS dark mode first-click toggle + middle-click no premature pendingReturn
  const themeBtn = makeEl("button", { id: "theme-toggle-btn" });
  const cardA = makeEl("a", { "data-item-slug": "gui-rl-2611-18715", href: "daily/2026-11-24/item-gui-rl-2611-18715.html" });
  const cardB = makeEl("a", { "data-item-slug": "ossl-ood-2602-18094", href: "daily/2026-11-24/item-ossl-ood-2602-18094.html" });

  const ov1 = mountPage({
    pathname: "/daily-radar/index.html",
    scrollY: 640,
    osDark: true,
    rootAttrs: { "data-root": ".", "data-page-kind": "overview", "data-issue-date": "2026-11-24" },
    byId: { "theme-toggle-btn": themeBtn },
    selectorMap: { ".summary-card[data-item-slug]": [cardA, cardB], ".summary-card": [cardA, cardB] },
    sessionStorage: session,
    localStorage: local,
  });

  // First click in OS dark mode MUST immediately switch to "light" (not stay "dark"!)
  themeBtn.click();
  assert.strictEqual(ov1.rootEl.getAttribute("data-theme"), "light");

  // Click Card A on root /index.html at scrollY=640
  cardA.click();
  const savedAfterClick = JSON.parse(session.getItem("daily_radar_scroll_state_v1"));
  assert.strictEqual(savedAfterClick.scrollY, 640);
  assert.strictEqual(savedAfterClick.pendingReturn, false);

  // 2. Enter Card A detail -> click Next -> enter Card B detail (referrer is item-A.html, NOT overview!) -> click Return!
  const backBtnA = makeEl("a", { href: "index.html", "data-return-overview": "1" });
  mountPage({
    pathname: "/daily-radar/daily/2026-11-24/item-gui-rl-2611-18715.html",
    referrer: "https://phylipppp-zzy.github.io/daily-radar/index.html",
    desktop: true,
    rootAttrs: { "data-root": "../..", "data-page-kind": "detail", "data-issue-date": "2026-11-24", "data-item-slug": "gui-rl-2611-18715" },
    selectorMap: { "[data-return-overview]": [backBtnA] },
    sessionStorage: session,
    localStorage: local,
  });

  const backBtnB = makeEl("a", { href: "index.html", "data-return-overview": "1" });
  const detB = mountPage({
    pathname: "/daily-radar/daily/2026-11-24/item-ossl-ood-2602-18094.html",
    referrer: "https://phylipppp-zzy.github.io/daily-radar/daily/2026-11-24/item-gui-rl-2611-18715.html",
    desktop: true,
    rootAttrs: { "data-root": "../..", "data-page-kind": "detail", "data-issue-date": "2026-11-24", "data-item-slug": "ossl-ood-2602-18094" },
    selectorMap: { "[data-return-overview]": [backBtnB] },
    sessionStorage: session,
    localStorage: local,
  });

  backBtnB.click();
  // MUST NOT call history.back() (which would go to item-A.html)! Must jump to saved root overviewHref!
  assert.strictEqual(detB.getHistoryBackCalls(), 0);
  assert.strictEqual(detB.windowObj.location.href, "https://phylipppp-zzy.github.io/daily-radar/index.html");

  // 3. Re-entering overview restores exact scrollY=640 AND pulses Card B (the card just exited!)
  const cardA2 = makeEl("a", { "data-item-slug": "gui-rl-2611-18715" });
  const cardB2 = makeEl("a", { "data-item-slug": "ossl-ood-2602-18094" });
  const searchBackdrop = makeEl("div", { id: "search-backdrop" });
  const searchInput = makeEl("input", { id: "search-input" });
  const searchResults = makeEl("ul", { id: "search-results" });
  const ov2 = mountPage({
    pathname: "/daily-radar/index.html",
    baseRelDir: ".",
    scrollY: 0,
    rootAttrs: { "data-root": ".", "data-page-kind": "overview", "data-issue-date": "2026-11-24" },
    byId: { "search-backdrop": searchBackdrop, "search-input": searchInput, "search-results": searchResults },
    selectorMap: { ".summary-card[data-item-slug]": [cardA2, cardB2], ".summary-card": [cardA2, cardB2] },
    sessionStorage: session,
    localStorage: local,
  });
  assert.deepStrictEqual(ov2.getScrolledTo(), [0, 640]);
  assert.strictEqual(cardA2.classList.contains("is-read"), true);
  assert.strictEqual(cardB2.classList.contains("is-read"), true);
  assert.strictEqual(cardB2.classList.contains("is-just-returned"), true);
  assert.strictEqual(cardA2.classList.contains("is-just-returned"), false);

  // 4. Direct overview -> detail -> return uses history.back() for instant bfcache restoration
  cardA2.click();
  const backDirect = makeEl("a", { href: "index.html", "data-return-overview": "1" });
  const detDirect = mountPage({
    pathname: "/daily-radar/daily/2026-11-24/item-gui-rl-2611-18715.html",
    referrer: "https://phylipppp-zzy.github.io/daily-radar/index.html",
    rootAttrs: { "data-root": "../..", "data-page-kind": "detail", "data-issue-date": "2026-11-24", "data-item-slug": "gui-rl-2611-18715" },
    selectorMap: { "[data-return-overview]": [backDirect] },
    sessionStorage: session,
    localStorage: local,
  });
  backDirect.click();
  assert.strictEqual(detDirect.getHistoryBackCalls(), 1);

  // 5. Sharded search (`/data/index-2026.json`) & Cold-Archive dynamic viewer (`/daily/item.html?date=...&slug=...`)
  ov2.document.dispatch("keydown", { key: "/", target: { tagName: "BODY" } });
  assert.strictEqual(searchBackdrop.classList.contains("is-open"), true);
  searchInput.value = "oodconceptbench hkbu";
  searchInput.dispatch("input");
  await new Promise(r => setTimeout(r, 30));
  assert.ok(searchResults.innerHTML.includes("OODConceptBench"), "Search failed to find OODConceptBench in index-2026.json");

  const coldOpts = {
    pathname: "/daily-radar/daily/item.html",
    search: "?date=2026-11-22&slug=gui-rl-2610-03818",
    baseRelDir: "daily",
    rootAttrs: { "data-root": "..", "data-page-kind": "cold-item" },
    sessionStorage: session,
    localStorage: local,
  };
  const coldMount = makeEl("main", { id: "cold-item-mount" });
  mountPage({
    ...coldOpts,
    byId: { "cold-item-mount": coldMount },
    mapResponse(issue) {
      const fe = issue.featured_items.find(item => item.slug === "gui-rl-2610-03818").four_elements;
      for (const field of ["verdict_label", "verdict_reason", "critique_points", "evidence_boundary"]) delete fe[field];
    },
  });
  await new Promise(r => setTimeout(r, 30));
  assert.ok(coldMount.innerHTML.includes("Control OSWorld"), "Cold archive viewer failed to render Control OSWorld");
  assert.ok(!coldMount.innerHTML.includes("el-critique"), "Legacy items should omit critique");

  const reviewedMount = makeEl("main", { id: "cold-item-mount" });
  mountPage({
    ...coldOpts,
    byId: { "cold-item-mount": reviewedMount },
    mapResponse(issue) {
      const item = issue.featured_items.find(item => item.slug === "gui-rl-2610-03818");
      Object.assign(item.four_elements, {
        verdict_label: "值得精读",
        verdict_reason: "值得复核 <小样本> 的表现。",
        critique_points: ["界面变化后还能泛化吗？", "多种子方差是多少？", "同一基座下的增益有多大？"],
        evidence_boundary: "已结合 arXiv HTML 正文片段与摘要研判",
      });
    },
  });
  await new Promise(r => setTimeout(r, 30));
  assert.ok(reviewedMount.innerHTML.includes('<section id="el-critique" class="four-el-card critique-card">'));
  assert.ok(reviewedMount.innerHTML.includes("⑤ 审稿人视角批判与独立观点"));
  assert.ok(reviewedMount.innerHTML.includes('<span class="verdict-pill">值得精读</span>'));
  assert.ok(reviewedMount.innerHTML.includes("值得复核 &lt;小样本&gt; 的表现。"));
  assert.ok(reviewedMount.innerHTML.includes(
    '<ol class="critique-list"><li>界面变化后还能泛化吗？</li><li>多种子方差是多少？</li><li>同一基座下的增益有多大？</li></ol>'
  ));
  assert.ok(reviewedMount.innerHTML.includes(
    '<footer class="evidence-boundary">已结合 arXiv HTML 正文片段与摘要研判</footer>'
  ));
  console.log(JSON.stringify({ ok: true }));
})().catch(err => {
  console.error(err);
  process.exit(1);
});
"""
        proc = subprocess.run(
            ["node", "-e", harness, str(site_dir), str(app_js_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        out = json.loads(proc.stdout.strip())
        self.assertTrue(out["ok"])


if __name__ == "__main__":
    unittest.main()
