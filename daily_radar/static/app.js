/* Daily Research Radar — Lightweight Shared Client Script
 * Handles:
 * 1. Exact scroll-position save & restore across Overview <-> Detail navigation (sessionStorage + bfcache)
 * 2. Read-card state marker (localStorage) + 1.2s breathing highlight on return
 * 3. One-click clipboard copy (BibTeX, zoteroctl command, Feishu feedback tokens)
 * 4. Sharded yearly JSON instant full-text search (/data/index-YYYY.json)
 * 5. Full keyboard navigation (/, Esc, J/K, Enter, <-/->, C) + light/dark theme toggle
 * 6. Dynamic cold-archive item fallback (/daily/item.html?date=...&slug=...)
 */
(function () {
  "use strict";

  var SCROLL_STATE_KEY = "daily_radar_scroll_state_v1";
  var READ_SET_KEY = "daily_radar_read_slugs_v1";
  var THEME_KEY = "daily_radar_theme_v1";

  var rootEl = document.documentElement;
  var siteRoot = rootEl.getAttribute("data-root") || ".";
  var pageKind = rootEl.getAttribute("data-page-kind") || "";
  var issueDate = rootEl.getAttribute("data-issue-date") || "";

  // 1. Theme persistence
  function applySavedTheme() {
    try {
      var saved = localStorage.getItem(THEME_KEY);
      if (saved === "light" || saved === "dark") {
        rootEl.setAttribute("data-theme", saved);
      }
    } catch (_e) {}
  }
  applySavedTheme();

  var themeBtn = document.getElementById("theme-toggle-btn");
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var explicit = rootEl.getAttribute("data-theme");
      var effective =
        explicit === "light" || explicit === "dark"
          ? explicit
          : window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light";
      var next = effective === "dark" ? "light" : "dark";
      rootEl.setAttribute("data-theme", next);
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (_e) {}
    });
  }

  // Toast helper
  var toastTimer = null;
  function showToast(msg) {
    var el = document.getElementById("radar-toast");
    if (!el) return;
    el.textContent = msg;
    el.classList.add("is-visible");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      el.classList.remove("is-visible");
    }, 2100);
  }

  // Read slugs tracking
  function getReadMap() {
    try {
      return JSON.parse(localStorage.getItem(READ_SET_KEY) || "{}");
    } catch (_e) {
      return {};
    }
  }

  function markSlugRead(slug) {
    if (!slug) return;
    try {
      var map = getReadMap();
      map[slug] = 1;
      localStorage.setItem(READ_SET_KEY, JSON.stringify(map));
    } catch (_e) {}
  }

  function decorateReadCards() {
    var map = getReadMap();
    var cards = document.querySelectorAll(".summary-card[data-item-slug]");
    for (var i = 0; i < cards.length; i++) {
      var slug = cards[i].getAttribute("data-item-slug");
      if (slug && map[slug]) {
        cards[i].classList.add("is-read");
      }
    }
  }

  function stripHash(url) {
    return String(url || "").split("#")[0];
  }

  // 2. Overview <-> Detail scroll position save & exact restoration
  function wireOverviewScrollSave() {
    if (pageKind !== "overview") return;
    decorateReadCards();

    var cards = document.querySelectorAll(".summary-card[data-item-slug]");
    for (var i = 0; i < cards.length; i++) {
      cards[i].addEventListener("click", function (ev) {
        var card = ev.currentTarget;
        var slug = card.getAttribute("data-item-slug") || "";
        markSlugRead(slug);
        card.classList.add("is-read");
        try {
          // Set pendingReturn=false on click; detail page promotes pendingReturn=true when actually entered,
          // so middle-click/new-tab opens do not leave stale pending scroll jumps on the overview tab.
          sessionStorage.setItem(
            SCROLL_STATE_KEY,
            JSON.stringify({
              issueDate: issueDate,
              overviewPath: window.location.pathname,
              overviewHref: stripHash(window.location.href),
              scrollY: window.scrollY || window.pageYOffset || 0,
              slug: slug,
              pendingReturn: false,
              ts: Date.now()
            })
          );
        } catch (_e) {}
      });
    }

    function restoreOverviewPosition() {
      decorateReadCards();
      try {
        var raw = sessionStorage.getItem(SCROLL_STATE_KEY);
        if (!raw) return;
        var state = JSON.parse(raw);
        if (!state || !state.pendingReturn || state.issueDate !== issueDate) return;
        if (Date.now() - (state.ts || 0) > 3600 * 1000) return;

        state.pendingReturn = false;
        sessionStorage.setItem(SCROLL_STATE_KEY, JSON.stringify(state));

        window.scrollTo(0, Number(state.scrollY) || 0);
        if (state.slug) {
          var target = document.querySelector('.summary-card[data-item-slug="' + state.slug + '"]');
          if (target) {
            target.classList.remove("is-just-returned");
            void target.offsetWidth;
            target.classList.add("is-just-returned");
          }
        }
      } catch (_e) {}
    }

    restoreOverviewPosition();
    window.addEventListener("pageshow", function () {
      restoreOverviewPosition();
    });
  }
  wireOverviewScrollSave();

  function armDetailReturnState(activeDate, activeSlug) {
    if (!activeDate || !activeSlug) return;
    try {
      var raw = sessionStorage.getItem(SCROLL_STATE_KEY);
      if (!raw) return;
      var state = JSON.parse(raw);
      if (state && state.issueDate === activeDate) {
        // Update slug to the article currently being read so prev/next navigation pulses the card just exited,
        // and arm pendingReturn=true so swipe-back / browser Back (pageshow) restores scrollY automatically!
        state.slug = activeSlug;
        state.pendingReturn = true;
        state.ts = Date.now();
        sessionStorage.setItem(SCROLL_STATE_KEY, JSON.stringify(state));
      }
    } catch (_e) {}
  }

  function isDirectOverviewReferrer(refUrl, stateOverviewHref, activeDate) {
    if (!refUrl || refUrl.indexOf(window.location.origin) !== 0) return false;
    var cleanRef = stripHash(refUrl);
    if (stateOverviewHref && cleanRef === stripHash(stateOverviewHref)) return true;
    try {
      var refPath = new URL(cleanRef).pathname;
      if (activeDate && refPath.slice(-("/daily/" + activeDate + "/index.html").length) === "/daily/" + activeDate + "/index.html") {
        return true;
      }
      if (activeDate && refPath.slice(-("/daily/" + activeDate + "/").length) === "/daily/" + activeDate + "/") {
        return true;
      }
    } catch (_e) {}
    return false;
  }

  function triggerReturnToOverview(fallbackHref, overrideDate) {
    var targetDate = overrideDate || issueDate;
    try {
      var raw = sessionStorage.getItem(SCROLL_STATE_KEY);
      if (raw) {
        var state = JSON.parse(raw);
        if (state && state.issueDate === targetDate) {
          state.pendingReturn = true;
          sessionStorage.setItem(SCROLL_STATE_KEY, JSON.stringify(state));
          // Only invoke history.back() if document.referrer is the overview page itself!
          // If user navigated Card A -> Next -> Card B, referrer is item-A.html: jump directly to saved overviewHref!
          if (isDirectOverviewReferrer(document.referrer, state.overviewHref, targetDate)) {
            window.history.back();
            return;
          }
          if (state.overviewHref && state.overviewHref.indexOf(window.location.origin) === 0) {
            window.location.href = state.overviewHref;
            return;
          }
        }
      }
    } catch (_e) {}
    window.location.href = fallbackHref || "index.html";
  }

  if (pageKind === "detail") {
    var curSlug = rootEl.getAttribute("data-item-slug") || "";
    markSlugRead(curSlug);
    armDetailReturnState(issueDate, curSlug);

    // Auto-open English abstract on wide desktop screens while keeping mobile folded
    if (window.matchMedia && window.matchMedia("(min-width: 1024px)").matches) {
      var absEl = document.getElementById("en-abstract-details");
      if (absEl) absEl.setAttribute("open", "open");
    }

    var backLinks = document.querySelectorAll("[data-return-overview]");
    for (var b = 0; b < backLinks.length; b++) {
      backLinks[b].addEventListener("click", function (ev) {
        ev.preventDefault();
        var href = ev.currentTarget.getAttribute("href") || "index.html";
        triggerReturnToOverview(href, issueDate);
      });
    }
  }

  // Date selector jump
  var dateSelect = document.getElementById("issue-date-jump");
  if (dateSelect) {
    dateSelect.addEventListener("change", function () {
      if (dateSelect.value) {
        window.location.href = dateSelect.value;
      }
    });
  }

  // 3. One-click copy buttons
  function copyText(text, label) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () {
          showToast("已复制" + label);
        },
        function () {
          fallbackCopy(text, label);
        }
      );
    } else {
      fallbackCopy(text, label);
    }
  }

  function fallbackCopy(text, label) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      showToast("已复制" + label);
    } catch (_e) {
      showToast("复制失败，请展开代码块手动复制");
    }
    document.body.removeChild(ta);
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-copy-target], [data-copy-text]");
    if (!btn) return;
    var label = btn.getAttribute("data-copy-label") || "内容";
    var inlineText = btn.getAttribute("data-copy-text");
    if (inlineText !== null) {
      copyText(inlineText, label);
      return;
    }
    var targetId = btn.getAttribute("data-copy-target");
    var srcEl = targetId ? document.getElementById(targetId) : null;
    if (srcEl) {
      copyText(srcEl.textContent || "", label);
    }
  });

  // Conference secondary filter pills
  var filterBtns = document.querySelectorAll("[data-conf-filter]");
  if (filterBtns.length > 0) {
    for (var f = 0; f < filterBtns.length; f++) {
      filterBtns[f].addEventListener("click", function (ev) {
        var mode = ev.currentTarget.getAttribute("data-conf-filter");
        var cards = document.querySelectorAll(".summary-card[data-conf-tags]");
        for (var c = 0; c < cards.length; c++) {
          var tags = (cards[c].getAttribute("data-conf-tags") || "").toLowerCase();
          var show = mode === "all" || tags.indexOf(mode) !== -1;
          cards[c].style.display = show ? "" : "none";
        }
      });
    }
  }

  // 4. Sharded yearly JSON instant search
  var searchBackdrop = document.getElementById("search-backdrop");
  var searchInput = document.getElementById("search-input");
  var searchResultsEl = document.getElementById("search-results");
  var searchAllYearsChk = document.getElementById("search-all-years");
  var loadedShards = {};
  var availableYears = null;

  function fetchJson(url) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function ensureSearchShards(loadAllYears) {
    var curYear = (issueDate || new Date().toISOString().slice(0, 4)).slice(0, 4);
    var pYears = availableYears
      ? Promise.resolve(availableYears)
      : fetchJson(siteRoot + "/data/years.json")
          .then(function (d) {
            availableYears = d.years || [curYear];
            return availableYears;
          })
          .catch(function () {
            availableYears = [curYear];
            return availableYears;
          });

    return pYears.then(function (years) {
      var targets = loadAllYears ? years : [years[0] || curYear];
      var tasks = targets.map(function (yr) {
        if (loadedShards[yr]) return Promise.resolve(loadedShards[yr]);
        return fetchJson(siteRoot + "/data/index-" + yr + ".json")
          .then(function (rows) {
            loadedShards[yr] = rows;
            return rows;
          })
          .catch(function () {
            loadedShards[yr] = [];
            return [];
          });
      });
      return Promise.all(tasks).then(function (lists) {
        var merged = [];
        for (var i = 0; i < lists.length; i++) merged = merged.concat(lists[i]);
        return merged;
      });
    });
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function runSearchQuery() {
    if (!searchInput || !searchResultsEl) return;
    var q = searchInput.value.trim().toLowerCase();
    if (!q) {
      searchResultsEl.innerHTML = "<li>输入中英文关键词、模型名、基准名（如 OSWorld / NegLabel / verl）或学者姓名即时检索…</li>";
      return;
    }
    var terms = q.split(/\s+/).filter(Boolean);
    var allYr = !!(searchAllYearsChk && searchAllYearsChk.checked);
    ensureSearchShards(allYr).then(function (rows) {
      var hits = [];
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        var hay = (
          (r.title_zh || "") + " " +
          (r.title_en || "") + " " +
          (r.pain || "") + " " +
          (r.authors || "") + " " +
          (r.institutions || "") + " " +
          (r.tags || "") + " " +
          (r.metrics || "")
        ).toLowerCase();
        var ok = true;
        for (var t = 0; t < terms.length; t++) {
          if (hay.indexOf(terms[t]) === -1) {
            ok = false;
            break;
          }
        }
        if (ok) hits.push(r);
        if (hits.length >= 30) break;
      }
      if (!hits.length) {
        searchResultsEl.innerHTML = "<li>未命中匹配条目（可勾选“跨年搜索全部历史”扩大范围）。</li>";
        return;
      }
      searchResultsEl.innerHTML = hits
        .map(function (h) {
          return (
            '<li><a href="' +
            escapeHtml(siteRoot + "/" + h.url) +
            '"><strong>[' +
            escapeHtml(h.track_label || "") +
            "] " +
            escapeHtml(h.title_zh || h.title_en) +
            '</strong></a><div style="font-size:13px;color:var(--text-muted)">' +
            escapeHtml(h.date) +
            " · " +
            escapeHtml(h.pain || "") +
            "</div></li>"
          );
        })
        .join("");
    });
  }

  function openSearch() {
    if (!searchBackdrop) return;
    searchBackdrop.classList.add("is-open");
    ensureSearchShards(false);
    if (searchInput) {
      searchInput.focus();
      runSearchQuery();
    }
  }

  function closeSearch() {
    if (!searchBackdrop) return;
    searchBackdrop.classList.remove("is-open");
  }

  var openSearchBtns = document.querySelectorAll("[data-open-search]");
  for (var s = 0; s < openSearchBtns.length; s++) {
    openSearchBtns[s].addEventListener("click", function (ev) {
      ev.preventDefault();
      openSearch();
    });
  }
  if (searchBackdrop) {
    searchBackdrop.addEventListener("click", function (ev) {
      if (ev.target === searchBackdrop) closeSearch();
    });
  }
  var closeSearchBtn = document.getElementById("search-close-btn");
  if (closeSearchBtn) closeSearchBtn.addEventListener("click", closeSearch);
  if (searchInput) searchInput.addEventListener("input", runSearchQuery);
  if (searchAllYearsChk) searchAllYearsChk.addEventListener("change", runSearchQuery);

  // 5. Keyboard navigation (/, Esc, J/K, Enter, <-/->, C)
  var activeCardIdx = -1;
  document.addEventListener("keydown", function (ev) {
    var tag = (ev.target && ev.target.tagName) ? ev.target.tagName.toLowerCase() : "";
    var inInput = tag === "input" || tag === "textarea" || tag === "select";

    if (ev.key === "Escape") {
      if (searchBackdrop && searchBackdrop.classList.contains("is-open")) {
        closeSearch();
        return;
      }
      if (pageKind === "detail" && !inInput) {
        var backEl = document.querySelector("[data-return-overview]");
        triggerReturnToOverview(backEl ? backEl.getAttribute("href") : "index.html");
      }
      return;
    }

    if (inInput || ev.metaKey || ev.ctrlKey || ev.altKey) return;

    if (ev.key === "/") {
      ev.preventDefault();
      openSearch();
      return;
    }

    if (pageKind === "overview" && (ev.key === "j" || ev.key === "k" || ev.key === "Enter")) {
      var cards = document.querySelectorAll(".summary-card");
      if (!cards.length) return;
      if (ev.key === "Enter" && activeCardIdx >= 0 && activeCardIdx < cards.length) {
        cards[activeCardIdx].click();
        return;
      }
      if (ev.key === "j" || ev.key === "k") {
        ev.preventDefault();
        if (activeCardIdx >= 0 && activeCardIdx < cards.length) {
          cards[activeCardIdx].classList.remove("is-keyboard-active");
        }
        activeCardIdx = ev.key === "j"
          ? Math.min(cards.length - 1, activeCardIdx + 1)
          : Math.max(0, activeCardIdx <= 0 ? 0 : activeCardIdx - 1);
        cards[activeCardIdx].classList.add("is-keyboard-active");
        cards[activeCardIdx].scrollIntoView({ block: "nearest" });
      }
      return;
    }

    if (pageKind === "detail") {
      if (ev.key === "c") {
        var zotBtn = document.getElementById("copy-zoteroctl-btn");
        if (zotBtn) zotBtn.click();
      } else if (ev.key === "ArrowLeft" || ev.key === "k") {
        var prevLink = document.getElementById("detail-prev-link");
        if (prevLink && prevLink.getAttribute("href")) window.location.href = prevLink.getAttribute("href");
      } else if (ev.key === "ArrowRight" || ev.key === "j") {
        var nextLink = document.getElementById("detail-next-link");
        if (nextLink && nextLink.getAttribute("href")) window.location.href = nextLink.getAttribute("href");
      }
    }
  });

  // 6. Dynamic cold-archive viewer fallback (/daily/item.html?date=YYYY-MM-DD&slug=...)
  if (pageKind === "cold-item") {
    var params = new URLSearchParams(window.location.search);
    var d = params.get("date") || "";
    var slug = params.get("slug") || "";
    var mount = document.getElementById("cold-item-mount");
    if (mount && /^\d{4}-\d{2}-\d{2}$/.test(d) && slug) {
      markSlugRead(slug);
      armDetailReturnState(d, slug);
      fetchJson(siteRoot + "/daily/" + d + "/issue.json")
        .then(function (issue) {
          var items = issue.featured_items || [];
          var found = null;
          for (var i = 0; i < items.length; i++) {
            if (items[i].slug === slug) {
              found = items[i];
              break;
            }
          }
          if (!found) {
            mount.innerHTML = '<div class="empty-state">未找到对应历史条目。</div>';
            return;
          }
          var fe = found.four_elements || {};
          var backHref = d + "/index.html";
          mount.innerHTML =
            '<div class="detail-header"><h1 class="detail-h1">' + escapeHtml(found.title_zh) + "</h1>" +
            '<p class="detail-en-sub">' + escapeHtml(found.title_en) + "</p>" +
            '<div class="meta-list"><div><strong>发布日期与渠道：</strong>' + escapeHtml(found.published_date || d) + " · " + escapeHtml(found.venue_or_source || "") + "</div></div>" +
            '<p style="margin-top:12px;"><a class="btn" id="cold-return-btn" href="' + escapeHtml(backHref) + '">← 返回 ' + escapeHtml(d) + " 总览</a> " +
            '<a class="btn btn-primary" target="_blank" rel="noopener" href="' + escapeHtml(found.url_primary) + '">一手原文直达 ↗</a></p></div>' +
            '<section class="four-el-card"><h2>① 真实痛点与翻车场景</h2><p>' + escapeHtml(fe.pain_point || "") + "</p></section>" +
            '<section class="four-el-card"><h2>② 具体方法机制</h2><p>' + escapeHtml(fe.mechanism || "") + "</p></section>" +
            '<section class="four-el-card"><h2>③ 实验基准数字与对比口径</h2><p>' + escapeHtml(fe.experiments_summary || "") + "</p></section>" +
            '<section class="four-el-card"><h2>④ 公开技术参考与复现成本评估</h2><p>' + escapeHtml(fe.repro_and_reference || "") + "</p></section>" +
            '<details class="abstract-box"><summary>展开/收起原文英文摘要对照</summary><div class="abstract-content">' + escapeHtml(found.abstract_en || "") + "</div></details>";
          var coldBack = document.getElementById("cold-return-btn");
          if (coldBack) {
            coldBack.addEventListener("click", function (ev) {
              ev.preventDefault();
              triggerReturnToOverview(backHref, d);
            });
          }
        })
        .catch(function () {
          mount.innerHTML = '<div class="empty-state">加载历史归档 JSON 失败。</div>';
        });
    }
  }
})();
