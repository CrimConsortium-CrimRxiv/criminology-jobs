(function () {
  "use strict";

  // ============================================================
  // Data + state
  // ============================================================
  var DATA = window.JOBS_DATA || { jobs: [], compiled: "" };
  var JOBS = (DATA.jobs || []).slice();

  // ----- UK constituent-country lookup ------------------------------
  // The source data only stores `country: "United Kingdom"`. We split it
  // into the four constituent countries based on the city/region string.
  // Anything not matched falls back to England (statistically dominant
  // and matches the current snapshot).
  var UK_CITY_TO_REGION = (function () {
    var map = {};
    var add = function (region, cities) {
      cities.forEach(function (c) { map[c.toLowerCase()] = region; });
    };
    add("Scotland", [
      "Edinburgh", "Glasgow", "Aberdeen", "Dundee", "St Andrews", "Stirling",
      "Inverness", "Paisley", "Perth", "Ayr", "Falkirk", "Dumfries"
    ]);
    add("Wales", [
      "Cardiff", "Swansea", "Bangor", "Aberystwyth", "Newport", "Wrexham",
      "Pontypridd", "Carmarthen", "Lampeter"
    ]);
    add("Northern Ireland", [
      "Belfast", "Derry", "Londonderry", "Coleraine", "Jordanstown", "Armagh"
    ]);
    return map;
  })();
  function ukRegionFromCity(city) {
    if (!city) return "England";
    var key = String(city).trim().toLowerCase();
    if (UK_CITY_TO_REGION[key]) return UK_CITY_TO_REGION[key];
    // Try last token (handles "Edinburgh, Scotland" or "Greater Glasgow")
    var parts = key.split(/[,/]/).map(function (s) { return s.trim(); });
    for (var i = 0; i < parts.length; i++) {
      if (UK_CITY_TO_REGION[parts[i]]) return UK_CITY_TO_REGION[parts[i]];
    }
    return "England";
  }

  // Annotate every job with a `display_country` we use everywhere the
  // user sees or filters by country. Underlying `country` field is left
  // untouched in case downstream consumers need it.
  JOBS.forEach(function (j) {
    if (j.country === "United Kingdom") {
      j.display_country = ukRegionFromCity(j.city_or_region);
    } else {
      j.display_country = j.country || "";
    }
  });
  // Use the actual current date so deadline labels ("closed", "5 days", etc.) stay correct
  // as the page is viewed over time.
  var TODAY = (function () {
    var d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  })();

  var state = {
    search: "",
    country: null, // single-select
    ranks: new Set(),
    tracks: new Set(), // "Academic" | "Applied"
    sort: "posted_desc",
    consortiumOnly: false,
  };

  function isConsortium(job) { return !!(job && job.consortium_member); }

  // ============================================================
  // Derive Academic vs Applied track from source_site + rank bucket.
  //
  // Default: source decides. ACJS / ASC / HigherEdJobs / jobs.ac.uk =>
  // Academic; TSPA => Applied.
  //
  // Override: any row whose rank bucket is "Practitioner" is reclassified
  // as Applied even if its source is an academic board. This catches the
  // applied-track postings that show up on ACJS (agency analysts, police
  // researchers, NGO program leads, etc.) without requiring per-row
  // hand-classification.
  // ============================================================
  var APPLIED_SOURCES = { "TSPA": true };
  function deriveTrack(job) {
    if (!job) return "Academic";
    if (bucketRank(job.rank_type) === "Practitioner") return "Applied";
    var sources = String(job.source_site || "").split(",").map(function (s) { return s.trim(); });
    for (var i = 0; i < sources.length; i++) {
      if (APPLIED_SOURCES[sources[i]]) return "Applied";
    }
    return "Academic";
  }
  var TRACK_ORDER = ["Academic", "Applied"];

  // Bucket the 27 distinct rank_type values into ~7 useful groups.
  function bucketRank(r) {
    if (!r) return "Other";
    var s = String(r).toLowerCase();
    // Academic buckets first (so "Senior Lecturer" doesn't get caught by industry "Senior")
    if (/tenure|^assistant prof|^assistant\/|^associate prof|open rank|assistant teaching|full prof/.test(s)) return "Tenure-track";
    if (/postdoc|research fellow/.test(s)) return "Postdoc / Research";
    if (/visiting/.test(s)) return "Visiting";
    if (/lecturer|university teacher|^tutor$|instructional/.test(s)) return "Lecturer / Teaching";
    if (/adjunct|part-time|affiliated/.test(s)) return "Adjunct / Part-time";
    if (/clinical/.test(s)) return "Clinical";
    if (/practitioner|academic specialist/.test(s)) return "Practitioner";
    // Industry buckets (TSPA and similar)
    if (/director|head of|\bvp\b|chief|vice president/.test(s)) return "Director / VP";
    if (/manager/.test(s)) return "Manager";
    if (/engineer|architect|data scientist|machine learning|developer/.test(s)) return "Engineer / Technical";
    if (/analyst/.test(s)) return "Analyst";
    if (/\blead\b/.test(s)) return "Lead";
    if (/specialist|counsel|investigator|^agent$/.test(s)) return "Specialist";
    if (/associate/.test(s)) return "Associate";
    if (/^senior$/.test(s)) return "Engineer / Technical";
    return "Other";
  }

  // ============================================================
  // Date helpers
  // ============================================================
  var MONTHS = {
    jan: 0, january: 0, feb: 1, february: 1, mar: 2, march: 2, apr: 3, april: 3,
    may: 4, jun: 5, june: 5, jul: 6, july: 6, aug: 7, august: 7,
    sep: 8, sept: 8, september: 8, oct: 9, october: 9, nov: 10, november: 10,
    dec: 11, december: 11,
  };
  function parseDate(s) {
    if (!s) return null;
    s = String(s).trim();
    // ISO 2026-04-21
    var iso = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    if (iso) return new Date(+iso[1], +iso[2] - 1, +iso[3]);
    // "April 27, 2026"
    var long = s.match(/^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$/);
    if (long && MONTHS[long[1].toLowerCase()] !== undefined) {
      return new Date(+long[3], MONTHS[long[1].toLowerCase()], +long[2]);
    }
    // "21 Apr 2026"
    var euro = s.match(/^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$/);
    if (euro && MONTHS[euro[2].toLowerCase()] !== undefined) {
      return new Date(+euro[3], MONTHS[euro[2].toLowerCase()], +euro[1]);
    }
    return null;
  }
  function fmtDate(d) {
    if (!d) return "";
    var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return months[d.getMonth()] + " " + d.getDate() + ", " + d.getFullYear();
  }
  function prettyDate(s) {
    var d = parseDate(s);
    return d ? fmtDate(d) : (s || "");
  }
  function daysUntil(s) {
    var d = parseDate(s);
    if (!d) return null;
    return Math.floor((d - TODAY) / 86400000);
  }

  // ============================================================
  // Counters for facets
  // ============================================================
  function countBy(jobs, key) {
    var m = {};
    jobs.forEach(function (j) {
      var v = (j[key] || "").trim();
      if (!v) return;
      // Some rows have comma-separated source_site values; split for source counts
      if (key === "source_site" && v.indexOf(",") >= 0) {
        v.split(",").map(function (s) { return s.trim(); }).forEach(function (s) {
          m[s] = (m[s] || 0) + 1;
        });
      } else {
        m[v] = (m[v] || 0) + 1;
      }
    });
    return m;
  }

  // ============================================================
  // Filtering + sorting
  // ============================================================
  function jobMatchesSearch(job) {
    if (!state.search) return true;
    var q = state.search.toLowerCase();
    var hay = [
      job.job_title,
      job.institution,
      job.department_or_school,
      job.area_specialization,
      job.city_or_region,
      job.country,
      job.display_country,
      job.rank_type,
    ].join(" ").toLowerCase();
    return hay.indexOf(q) >= 0;
  }
  function applyFilters() {
    return JOBS.filter(function (j) {
      if (state.consortiumOnly && !isConsortium(j)) return false;
      if (state.country && j.display_country !== state.country) return false;
      if (state.ranks.size > 0 && !state.ranks.has(bucketRank(j.rank_type))) return false;
      if (state.tracks.size > 0 && !state.tracks.has(deriveTrack(j))) return false;
      if (!jobMatchesSearch(j)) return false;
      return true;
    });
  }
  function applySort(jobs) {
    var s = state.sort;
    var sorted = jobs.slice();
    // Always pin consortium-member jobs to the top within the chosen sort,
    // unless the user is already filtering only-consortium (no need then).
    function consortiumPriority(a, b) {
      if (state.consortiumOnly) return 0;
      var ca = isConsortium(a) ? 0 : 1;
      var cb = isConsortium(b) ? 0 : 1;
      return ca - cb;
    }
    if (s === "posted_desc" || s === "posted_asc") {
      sorted.sort(function (a, b) {
        var p = consortiumPriority(a, b); if (p !== 0) return p;
        var da = parseDate(a.posted_date);
        var db = parseDate(b.posted_date);
        if (!da && !db) return 0;
        if (!da) return 1;
        if (!db) return -1;
        return s === "posted_desc" ? db - da : da - db;
      });
    } else if (s === "deadline_asc") {
      sorted.sort(function (a, b) {
        var p = consortiumPriority(a, b); if (p !== 0) return p;
        var da = parseDate(a.deadline_or_review_date);
        var db = parseDate(b.deadline_or_review_date);
        if (!da && !db) return 0;
        if (!da) return 1; // missing deadline last
        if (!db) return -1;
        return da - db;
      });
    } else if (s === "institution") {
      sorted.sort(function (a, b) {
        var p = consortiumPriority(a, b); if (p !== 0) return p;
        return (a.institution || "").localeCompare(b.institution || "");
      });
    } else if (s === "title") {
      sorted.sort(function (a, b) {
        var p = consortiumPriority(a, b); if (p !== 0) return p;
        return (a.job_title || "").localeCompare(b.job_title || "");
      });
    }
    return sorted;
  }

  // ============================================================
  // DOM helpers
  // ============================================================
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === "class") n.className = attrs[k];
        else if (k === "html") n.innerHTML = attrs[k];
        else if (k === "text") n.textContent = attrs[k];
        else if (k.indexOf("on") === 0) n.addEventListener(k.slice(2), attrs[k]);
        else if (attrs[k] !== null && attrs[k] !== undefined) n.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) {
      if (c == null) return;
      n.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    });
    return n;
  }
  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function sourceClass(s) {
    if (!s) return "";
    var t = s.toLowerCase();
    if (t.indexOf("higheredjobs") >= 0) return "higheredjobs";
    if (t.indexOf("asc") >= 0 && t.indexOf("acjs") < 0) return "asc";
    if (t.indexOf("acjs") >= 0 && t.indexOf("asc") < 0) return "acjs";
    if (t.indexOf("jobs.ac.uk") >= 0) return "jobs-ac-uk";
    return "";
  }

  // ============================================================
  // Render: stats
  // ============================================================
  function renderStats(filtered) {
    $("#stat-total").textContent = JOBS.length;
    var countries = {};
    JOBS.forEach(function (j) { if (j.display_country) countries[j.display_country] = true; });
    $("#stat-countries").textContent = Object.keys(countries).length;
    $("#stat-filtered").textContent = filtered.length;
    var compiledEl = $("#stat-compiled");
    if (compiledEl) compiledEl.textContent = DATA.compiled || "—";
  }

  // ============================================================
  // Render: country list
  // ============================================================
  function renderCountries() {
    var counts = countBy(JOBS, "display_country");
    var entries = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
    var list = $("#country-list");
    list.innerHTML = "";
    var allItem = el("div", {
      class: "dept-item" + (state.country == null ? " active" : ""),
      onclick: function () { state.country = null; render(); },
    }, [
      el("span", { text: "All countries" }),
      el("span", { class: "dept-count", text: String(JOBS.length) }),
    ]);
    list.appendChild(allItem);
    entries.forEach(function (c) {
      var item = el("div", {
        class: "dept-item" + (state.country === c ? " active" : ""),
        onclick: function () { state.country = (state.country === c) ? null : c; render(); },
      }, [
        el("span", { text: c }),
        el("span", { class: "dept-count", text: String(counts[c]) }),
      ]);
      list.appendChild(item);
    });
  }

  // ============================================================
  // Render: rank-bucket checkbox group
  // ============================================================
  // Preferred display order for rank buckets.
  var RANK_BUCKET_ORDER = [
    "Tenure-track",
    "Postdoc / Research",
    "Lecturer / Teaching",
    "Visiting",
    "Clinical",
    "Adjunct / Part-time",
    "Practitioner",
    "Director / VP",
    "Manager",
    "Engineer / Technical",
    "Analyst",
    "Lead",
    "Specialist",
    "Associate",
    "Other",
  ];
  // ============================================================
  // Render: track (Academic vs Applied) checkbox group
  // ============================================================
  function renderTrackFilter() {
    var counts = { Academic: 0, Applied: 0 };
    JOBS.forEach(function (j) { counts[deriveTrack(j)]++; });
    var container = $("#track-filter");
    if (!container) return;
    container.innerHTML = "";
    TRACK_ORDER.forEach(function (v) {
      if (!counts[v]) return;
      var checked = state.tracks.has(v);
      var label = el("label", { class: "checkbox-item" }, []);
      var cb = el("input", { type: "checkbox" });
      cb.checked = checked;
      cb.addEventListener("change", function () {
        if (cb.checked) state.tracks.add(v); else state.tracks.delete(v);
        render();
      });
      label.appendChild(cb);
      label.appendChild(el("span", { class: "checkbox-item-label", text: v }));
      label.appendChild(el("span", { class: "checkbox-item-count", text: String(counts[v]) }));
      container.appendChild(label);
    });
  }

  function renderRankFilter() {
    var counts = {};
    JOBS.forEach(function (j) {
      var b = bucketRank(j.rank_type);
      counts[b] = (counts[b] || 0) + 1;
    });
    var entries = RANK_BUCKET_ORDER.filter(function (b) { return counts[b]; });
    var container = $("#rank-filter");
    container.innerHTML = "";
    entries.forEach(function (v) {
      var checked = state.ranks.has(v);
      var label = el("label", { class: "checkbox-item" }, []);
      var cb = el("input", { type: "checkbox" });
      cb.checked = checked;
      cb.addEventListener("change", function () {
        if (cb.checked) state.ranks.add(v); else state.ranks.delete(v);
        render();
      });
      label.appendChild(cb);
      label.appendChild(el("span", { class: "checkbox-item-label", text: v }));
      label.appendChild(el("span", { class: "checkbox-item-count", text: String(counts[v]) }));
      container.appendChild(label);
    });
  }

  // ============================================================
  // Render: active filter chips
  // ============================================================
  function renderActiveFilters() {
    var bar = $("#active-filters");
    bar.innerHTML = "";
    function addChip(label, onClear) {
      var chip = el("span", { class: "active-chip" }, [
        document.createTextNode(label),
        el("button", {
          "aria-label": "Remove " + label,
          onclick: onClear,
          html: "<svg width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2.5' stroke-linecap='round' stroke-linejoin='round'><line x1='18' y1='6' x2='6' y2='18'/><line x1='6' y1='6' x2='18' y2='18'/></svg>",
        }),
      ]);
      bar.appendChild(chip);
    }
    if (state.search) addChip('Search: "' + state.search + '"', function () { state.search = ""; $("#search-input").value = ""; render(); });
    if (state.consortiumOnly) addChip("CrimConsortium only", function () {
      state.consortiumOnly = false;
      var t = $("#consortium-toggle"); if (t) t.checked = false;
      render();
    });
    if (state.country) addChip("Country: " + state.country, function () { state.country = null; render(); });
    state.tracks.forEach(function (v) { addChip("Track: " + v, function () { state.tracks.delete(v); render(); }); });
    state.ranks.forEach(function (v) { addChip("Rank: " + v, function () { state.ranks.delete(v); render(); }); });
  }

  // ============================================================
  // Render: cards
  // ============================================================
  function jobCard(job) {
    var locParts = [];
    if (job.city_or_region) locParts.push(job.city_or_region);
    if (job.display_country) locParts.push(job.display_country);
    var locStr = locParts.join(" · ");

    var tags = [];
    if (job.rank_type) tags.push('<span class="tag tag-rank">' + escapeHtml(bucketRank(job.rank_type)) + "</span>");

    var consortiumBadge = "";
    if (isConsortium(job)) {
      consortiumBadge =
        ' <span class="consortium-badge" title="' + escapeHtml(job.consortium_member) + ' is a CrimConsortium member">' +
        "CONSORTIUM" +
        "</span>";
    }

    var card = el("button", {
      class: "job-card" + (isConsortium(job) ? " is-consortium" : ""),
      type: "button",
      "data-id": job.id,
    });
    var eyebrowParts = [];
    if (consortiumBadge) eyebrowParts.push(consortiumBadge);
    if (job.posted_date) eyebrowParts.push('<span>posted ' + escapeHtml(prettyDate(job.posted_date)) + "</span>");
    var eyebrowHtml = eyebrowParts.length ? ('<div class="job-card-eyebrow">' + eyebrowParts.join(" ") + "</div>") : "";

    card.innerHTML =
      eyebrowHtml +
      '<div class="job-card-title">' + escapeHtml(job.job_title || "—") + "</div>" +
      '<div class="job-card-inst">' + escapeHtml(job.institution || "") + "</div>" +
      (locStr ? '<div class="job-card-meta"><span class="job-card-meta-item">' +
        "<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z'/><circle cx='12' cy='10' r='3'/></svg>" +
        escapeHtml(locStr) + "</span></div>" : "") +
      '<div class="job-card-tags">' + tags.join("") + "</div>";

    card.addEventListener("click", function () { openModal(job); });
    return card;
  }

  function renderResults() {
    var filtered = applyFilters();
    var sorted = applySort(filtered);

    renderStats(filtered);

    $("#results-title").textContent = state.country ? state.country : "All postings";
    $("#results-meta").textContent = sorted.length + " posting" + (sorted.length === 1 ? "" : "s");

    var grid = $("#job-grid");
    var empty = $("#empty-state");
    grid.innerHTML = "";
    if (sorted.length === 0) {
      empty.style.display = "block";
    } else {
      empty.style.display = "none";
      var frag = document.createDocumentFragment();
      sorted.forEach(function (j) { frag.appendChild(jobCard(j)); });
      grid.appendChild(frag);
    }
  }

  // ============================================================
  // Modal
  // ============================================================
  function openModal(job) {
    var sources = (job.source_site || "").split(",").map(function (s) { return s.trim(); });
    var urls = (job.combined_urls || job.job_url || "").split(",").map(function (u) { return u.trim(); }).filter(Boolean);
    var locParts = [];
    if (job.city_or_region) locParts.push(job.city_or_region);
    if (job.display_country) locParts.push(job.display_country);

    $("#modal-eyebrow").innerHTML = sources.map(function (s) {
      return '<span class="source-badge ' + sourceClass(s) + '">' + escapeHtml(s) + "</span>";
    }).join(" ");
    $("#modal-name").textContent = job.job_title || "—";
    $("#modal-title").textContent = [job.institution, job.department_or_school].filter(Boolean).join(" · ");

    function row(label, value, isHtml) {
      var dt = '<dt>' + escapeHtml(label) + '</dt>';
      var dd;
      if (!value) dd = '<dd class="empty">—</dd>';
      else if (isHtml) dd = '<dd>' + value + '</dd>';
      else dd = '<dd>' + escapeHtml(value) + '</dd>';
      return dt + dd;
    }

    var deadlineDisplay = prettyDate(job.deadline_or_review_date) || job.deadline_or_review_date || "";

    var salary = job.salary_range || "";
    if (salary && job.salary_currency) salary = job.salary_currency + " · " + salary;

    var consortiumValue = job.consortium_member
      ? '<a href="https://crimconsortium.com" target="_blank" rel="noopener">' + escapeHtml(job.consortium_member) + ' — CrimConsortium member</a>'
      : null;

    var fields =
      row("Consortium", consortiumValue, true) +
      row("Location", locParts.join(", ")) +
      row("Rank / role", job.rank_type) +
      row("Specialization", job.area_specialization) +
      row("Posted", prettyDate(job.posted_date)) +
      row("Deadline", deadlineDisplay) +
      row("Salary", salary) +
      row("Department", job.department_or_school);

    $("#modal-body").innerHTML = '<dl class="modal-fields">' + fields + "</dl>";

    var actions = $("#modal-actions");
    actions.innerHTML = "";
    urls.forEach(function (u, i) {
      var label = urls.length === 1 ? "View posting" : "View posting " + (i + 1);
      var a = el("a", {
        href: u, target: "_blank", rel: "noopener",
        class: i === 0 ? "btn-primary" : "btn-secondary",
      });
      a.innerHTML = escapeHtml(label) +
        " <svg width='14' height='14' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6'/><polyline points='15 3 21 3 21 9'/><line x1='10' y1='14' x2='21' y2='3'/></svg>";
      actions.appendChild(a);
    });

    $("#modal").classList.add("open");
    document.body.style.overflow = "hidden";
  }
  function closeModal() {
    $("#modal").classList.remove("open");
    document.body.style.overflow = "";
  }

  // ============================================================
  // Theme toggle
  // ============================================================
  var SUN_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="5"/><line x1="12" y1="19" x2="12" y2="22"/><line x1="4.22" y1="4.22" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.78" y2="19.78"/><line x1="2" y1="12" x2="5" y2="12"/><line x1="19" y1="12" x2="22" y2="12"/><line x1="4.22" y1="19.78" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.78" y2="4.22"/></svg>';
  var MOON_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    $("[data-theme-toggle]").innerHTML = t === "dark" ? SUN_ICON : MOON_ICON;
    try { localStorage.setItem("crim-jobs-theme", t); } catch (e) {}
  }
  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem("crim-jobs-theme"); } catch (e) {}
    var t = saved || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    applyTheme(t);
    $("[data-theme-toggle]").addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
      applyTheme(cur === "dark" ? "light" : "dark");
    });
  }

  // ============================================================
  // Coverage
  // ============================================================
  function renderCoverage() {
    var html =
      '<ul>' +
      '<li><strong>ACJS (careers.acjs.org)</strong> — both public pages of the careers board collected on June 29, 2026. After applying the criminology / criminal-justice relevance filter, 1 specific posting retained; the rest excluded as adjunct-pool catch-alls, EMT / paramedic / paralegal, generic auxiliary-services roles, or unrelated counseling postings. Posted dates were given as relative ("X days ago") and converted to absolute dates relative to capture date.</li>' +
      '<li><strong>ASC (asc41.org)</strong> — single static page, no pagination, captured June 29, 2026. All 5 publicly listed postings captured.</li>' +
      '<li><strong>HigherEdJobs (higheredjobs.com)</strong> — Criminal Justice & Criminology faculty category, first 50 active postings captured (June 29, 2026). 24 retained as faculty + research + practitioner roles (assistant/associate professors, lecturers, instructors, visiting faculty, teaching faculty, postdoctoral scholars, professor of the practice); the rest excluded as generic adjunct pools, dual-credit, police-academy training, pure forensic-science, or graduate-assistant roles.</li>' +
      '<li><strong>jobs.ac.uk</strong> — the keyword=criminology search captured June 29, 2026; 3 retained as clearly criminology-relevant (including criminology-adjacent sociology, policing, and law posts housed in criminology departments). The remainder excluded as pure law-school lectureships, generic methodology fellowships, or unrelated technician/editor roles.</li>' +
      '<li><strong>TSPA (tspa.org)</strong> — the public Trust &amp; Safety job board, first indexed May 12, 2026 and last refreshed June 1, 2026 (TSPA refreshes monthly, on the first Monday of each month; carried over verbatim on weekly-only runs). 108 publicly listed postings included — TSPA covers industry trust-and-safety, policy, and content-moderation roles at tech companies (e.g. Google, Discord, Meta, TikTok). No filtering applied: roles span analyst, specialist, manager, lead, engineer, and director levels. Deadlines are not exposed on the source listing, so that column is blank for every TSPA row. <strong>Posted dates</strong> are also not exposed by TSPA; as a substitute, each TSPA row is stamped with the date this dashboard first indexed it, so "Newest posted" sort approximates "most recently seen" rather than the true publication date.</li>' +
      '<li><strong>Detail-page enrichment</strong> — teaching and research expectations are typically only described inside the linked PDFs / detail pages, so those columns are blank for almost every row. A second pass that visits each ad would be required to populate them.</li>' +
      '<li><strong>De-duplication</strong> — records are matched across sources by normalized institution + normalized title; matches are merged into a single row with a comma-separated source_site (e.g. "ASC, HigherEdJobs") and all source URLs concatenated in combined_urls. The current snapshot has 2 cross-site duplicates merged into single rows.</li>' +
      "</ul>";
    $("#coverage-items").innerHTML = html;
  }

  // ============================================================
  // Wire-up
  // ============================================================
  function render() {
    renderCountries();
    renderTrackFilter();
    renderRankFilter();
    renderActiveFilters();
    renderResults();
  }

  function init() {
    initTheme();
    renderCoverage();

    // Search
    var searchInput = $("#search-input");
    var searchClear = $("#search-clear");
    searchInput.addEventListener("input", function () {
      state.search = searchInput.value.trim();
      searchClear.classList.toggle("visible", state.search.length > 0);
      render();
    });
    searchClear.addEventListener("click", function () {
      searchInput.value = "";
      state.search = "";
      searchClear.classList.remove("visible");
      searchInput.focus();
      render();
    });

    // Sort
    $("#sort-select").addEventListener("change", function (e) {
      state.sort = e.target.value;
      renderResults();
    });

    // Clear all
    $("#clear-filters").addEventListener("click", function () {
      state.search = "";
      state.country = null;
      state.ranks.clear();
      state.tracks.clear();
      state.consortiumOnly = false;
      var ct = $("#consortium-toggle"); if (ct) ct.checked = false;
      $("#search-input").value = "";
      $("#search-clear").classList.remove("visible");
      render();
    });

    // Consortium-only toggle
    var consortiumToggle = $("#consortium-toggle");
    if (consortiumToggle) {
      consortiumToggle.addEventListener("change", function (e) {
        state.consortiumOnly = e.target.checked;
        render();
      });
    }

    // Coverage toggle
    var covToggle = $("#coverage-toggle");
    covToggle.addEventListener("click", function () {
      var open = covToggle.classList.toggle("open");
      $("#coverage-items").classList.toggle("open", open);
      $("#coverage-toggle-label").textContent = open ? "Hide coverage details" : "Show coverage details";
    });

    // Modal
    $("#modal-close").addEventListener("click", closeModal);
    $("#modal").addEventListener("click", function (e) {
      if (e.target.id === "modal") closeModal();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closeModal();
    });

    render();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
