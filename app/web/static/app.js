// ---------------------------------------------------------------------
// Oracle SQL Preflight
// Author:  Federico Guzman  (github.com/kraiosis)
// Website: https://fedeguzman.com    Blog: https://weblantropia.com
//
// Built with AI assistance from Claude (Anthropic). The analyzer's
// runtime behavior stays deterministic and AI-free -- see README.md,
// "Author & Credits", for what the AI assistance covers.
// ---------------------------------------------------------------------
// Oracle SQL Preflight Analyzer — frontend logic
// No frameworks; talks to the local FastAPI JSON API only.

const SEVERITY_ORDER = ["CRITICAL", "ERROR", "WARNING", "INFO"];

const SAMPLES = {
  update_missing_where:
`UPDATE employees
SET salary = salary * 1.10;`,

  delete_missing_where:
`DELETE FROM employees;`,

  set_where_noop:
`UPDATE employees
SET status = 'ACTIVE'
WHERE status = 'ACTIVE';`,

  self_assignment:
`UPDATE employees
SET salary = salary
WHERE department_id = 20;`,

  function_wrapped:
`UPDATE employees
SET status = UPPER(status),
    hire_date = TRUNC(hire_date)
WHERE department_id = 20;`,

  contradictory:
`UPDATE employees
SET status = 'ACTIVE'
WHERE department_id = 10
  AND department_id = 20;`,

  range_contradiction:
`UPDATE employees
SET status = 'ACTIVE'
WHERE salary > 100000
  AND salary < 50000;`,

  constant_false:
`UPDATE employees
SET status = 'ACTIVE'
WHERE 1 = 2;`,

  null_equality:
`UPDATE employees
SET status = 'ACTIVE'
WHERE manager_id = NULL;`,

  select_star:
`SELECT *
FROM employees;`,

  cartesian_join:
`SELECT *
FROM employees e, departments d
WHERE e.status = 'ACTIVE';`,

  insert:
`INSERT INTO employees (id, name, status)
VALUES (1001, 'J. Rivera', 'ACTIVE');`,

  merge:
`MERGE INTO employees e
USING staging_employees s
ON (e.id = s.id)
WHEN MATCHED THEN
  UPDATE SET e.status = s.status
WHEN NOT MATCHED THEN
  INSERT (id, status) VALUES (s.id, s.status);`,

  plsql_procedure:
`CREATE OR REPLACE PROCEDURE purge_logs(p_id IN NUMBER) IS
BEGIN
  UPDATE employees SET salary = salary * 1.1;
  EXECUTE IMMEDIATE 'DELETE FROM logs WHERE id = ' || p_id;
END;
/`,

  clean:
`UPDATE employees
SET status = 'INACTIVE'
WHERE employee_id = 5821;`,
};

// ---------------------------------------------------------------- helpers

function $(sel) { return document.querySelector(sel); }
function $all(sel) { return Array.from(document.querySelectorAll(sel)); }
function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

// ---------------------------------------------------------------- theme

const THEME_KEY = "preflight-theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  $("#icon-theme-dark").classList.toggle("hidden", theme !== "dark");
  $("#icon-theme-light").classList.toggle("hidden", theme !== "light");
  try { localStorage.setItem(THEME_KEY, theme); } catch (e) {}
}

function initTheme() {
  let theme = "dark";
  try {
    theme = localStorage.getItem(THEME_KEY) ||
      (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  } catch (e) {}
  applyTheme(theme);
}

$("#btn-theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  applyTheme(current === "light" ? "dark" : "light");
});

initTheme();

// ---------------------------------------------------------------- top-level views

function setView(name) {
  ["lab", "rules", "schema", "history", "compare", "about"].forEach((v) => {
    $(`#view-${v}`).classList.toggle("hidden", v !== name);
  });
  $all(".topnav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === name);
  });
  if (name === "history") loadHistory();
  if (name === "rules") loadRules();
  if (name === "schema") loadSchema();
  if (name === "lab") { loadExplorerSchema(); loadExplorerRules(); }
  if (name === "compare") renderCompareView();
}

$all(".topnav-item").forEach((btn) => {
  btn.addEventListener("click", () => setView(btn.dataset.view));
});

// ---------------------------------------------------------------- explorer tabs (sidebar)

$all("[data-explorer-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const which = btn.dataset.explorerTab;
    $all("[data-explorer-tab]").forEach((b) => b.classList.toggle("active", b === btn));
    $("#explorer-schema").classList.toggle("hidden", which !== "schema");
    $("#explorer-rules").classList.toggle("hidden", which !== "rules");
  });
});

// ---------------------------------------------------------------- worksheet tabs (multi-buffer editor)

let WORKSHEETS = [{ id: 1, name: "Worksheet 1", sql: $("#sql-input").value }];
let ACTIVE_WORKSHEET = 1;
let NEXT_WORKSHEET_ID = 2;

function renderWorksheetTabs() {
  const list = $("#worksheet-tab-list");
  list.innerHTML = "";
  WORKSHEETS.forEach((ws) => {
    const tab = el("div", "worksheet-tab" + (ws.id === ACTIVE_WORKSHEET ? " active" : ""));
    const label = el("span", "label", ws.name);
    tab.appendChild(label);
    if (WORKSHEETS.length > 1) {
      const close = el("span", "tab-close");
      close.innerHTML = "&times;";
      close.addEventListener("click", (e) => {
        e.stopPropagation();
        closeWorksheet(ws.id);
      });
      tab.appendChild(close);
    }
    tab.addEventListener("click", () => switchWorksheet(ws.id));
    list.appendChild(tab);
  });
}

function switchWorksheet(id) {
  if (id === ACTIVE_WORKSHEET) return;
  const current = WORKSHEETS.find((w) => w.id === ACTIVE_WORKSHEET);
  if (current) current.sql = $("#sql-input").value;
  ACTIVE_WORKSHEET = id;
  const next = WORKSHEETS.find((w) => w.id === id);
  $("#sql-input").value = next ? next.sql : "";
  renderWorksheetTabs();
  syncEditorGutter();
}

function closeWorksheet(id) {
  const idx = WORKSHEETS.findIndex((w) => w.id === id);
  if (idx === -1 || WORKSHEETS.length <= 1) return;
  WORKSHEETS.splice(idx, 1);
  if (ACTIVE_WORKSHEET === id) {
    const fallback = WORKSHEETS[Math.max(0, idx - 1)];
    ACTIVE_WORKSHEET = fallback.id;
    $("#sql-input").value = fallback.sql;
    syncEditorGutter();
  }
  renderWorksheetTabs();
}

$("#worksheet-tab-add").addEventListener("click", () => {
  const current = WORKSHEETS.find((w) => w.id === ACTIVE_WORKSHEET);
  if (current) current.sql = $("#sql-input").value;
  const id = NEXT_WORKSHEET_ID++;
  WORKSHEETS.push({ id, name: `Worksheet ${id}`, sql: "" });
  ACTIVE_WORKSHEET = id;
  $("#sql-input").value = "";
  renderWorksheetTabs();
  syncEditorGutter();
});

renderWorksheetTabs();

// ---------------------------------------------------------------- editor gutter (line numbers)

function syncEditorGutter() {
  const ta = $("#sql-input");
  const gutter = $("#editor-gutter");
  const lineCount = ta.value.split("\n").length;
  let text = "";
  for (let i = 1; i <= lineCount; i++) text += i + "\n";
  gutter.textContent = text.trimEnd() || "1";
  gutter.scrollTop = ta.scrollTop;
}

$("#sql-input").addEventListener("input", syncEditorGutter);
$("#sql-input").addEventListener("scroll", () => {
  $("#editor-gutter").scrollTop = $("#sql-input").scrollTop;
});
syncEditorGutter();

// ---------------------------------------------------------------- sample loader

$("#sample-select").addEventListener("change", (e) => {
  const key = e.target.value;
  if (key && SAMPLES[key]) {
    $("#sql-input").value = SAMPLES[key];
    syncEditorGutter();
  }
  e.target.value = "";
});

$("#btn-clear").addEventListener("click", () => {
  $("#sql-input").value = "";
  syncEditorGutter();
  $("#status-panel").classList.add("hidden");
  $("#result-formatted").classList.add("hidden");
  $("#findings-count-pill").textContent = "0";
  $("#statements-count-pill").textContent = "0";
  $("#statements-body").innerHTML = "";
  $("#result-findings").innerHTML =
    '<div class="text-[12.5px] px-1" style="color:var(--text-faint);">Run Analyze to see findings here.</div>';
  $("#elapsed").textContent = "";
});

// ---------------------------------------------------------------- result tabs

$all("[data-result-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const which = btn.dataset.resultTab;
    $all("[data-result-tab]").forEach((b) => b.classList.toggle("active", b === btn));
    ["findings", "formatted", "statements"].forEach((name) => {
      $(`#result-${name}`).classList.toggle("hidden", name !== which);
    });
  });
});

// ---------------------------------------------------------------- analyze

$("#btn-analyze").addEventListener("click", analyze);

async function analyze() {
  const sql = $("#sql-input").value;
  const btn = $("#btn-analyze");
  btn.disabled = true;
  const originalHtml = btn.innerHTML;
  btn.textContent = "Analyzing…";

  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql }),
    });
    const data = await res.json();
    renderAnalysis(data);
  } catch (err) {
    renderError(err);
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalHtml;
  }
}

function renderError(err) {
  const panel = $("#status-panel");
  panel.classList.remove("hidden");
  $("#status-badge").className = "badge badge-CRITICAL";
  $("#status-badge").textContent = "ERROR";
  $("#status-text").textContent = "Could not reach the local analyzer: " + err;
  $("#status-counts").innerHTML = "";
}

function countBySeverity(statements) {
  const counts = { CRITICAL: 0, ERROR: 0, WARNING: 0, INFO: 0 };
  let parseFailures = 0;
  statements.forEach((s) => {
    if (!s.parse_ok) parseFailures += 1;
    (s.findings || []).forEach((f) => {
      if (counts[f.severity] !== undefined) counts[f.severity] += 1;
    });
  });
  return { counts, parseFailures };
}

function renderAnalysis(data) {
  $("#elapsed").textContent = `${data.elapsed_ms} ms · ${data.statement_count} statement(s)`;

  const panel = $("#status-panel");
  panel.classList.remove("hidden");

  const status = data.status || "OK";
  const badge = $("#status-badge");
  badge.textContent = status;
  badge.className = "badge badge-" + (status === "OK" ? "OK" : status);

  const { counts, parseFailures } = countBySeverity(data.statements || []);
  const statusText = $("#status-text");
  if (parseFailures > 0) {
    statusText.textContent = "SQL could not be fully parsed. See findings below.";
  } else if (status === "CRITICAL") {
    statusText.textContent = "Critical findings detected. Review before executing this SQL.";
  } else if (status === "WARNING") {
    statusText.textContent = "Potential issues found. Review findings before executing.";
  } else if (status === "INFO") {
    statusText.textContent = "No safety concerns detected. Informational notes only.";
  } else {
    statusText.textContent = "No findings. This does not guarantee correctness — offline analysis has limits.";
  }

  const countsWrap = $("#status-counts");
  countsWrap.innerHTML = "";
  const order = [
    ["CRITICAL", "Critical"],
    ["ERROR", "Error"],
    ["WARNING", "Warning"],
    ["INFO", "Info"],
  ];
  order.forEach(([key, label]) => {
    const chip = el("span", "badge badge-outline", `${counts[key]} ${label}`);
    countsWrap.appendChild(chip);
  });
  countsWrap.appendChild(el("span", "badge badge-outline", "DB rows: UNKNOWN (offline)"));

  // findings
  const list = $("#result-findings");
  list.innerHTML = "";
  let findingCount = 0;

  (data.statements || []).forEach((stmt) => {
    (stmt.findings || []).forEach((f) => {
      findingCount += 1;
      list.appendChild(renderFinding(stmt, f));
    });
  });

  $("#findings-count-pill").textContent = String(findingCount);

  if (findingCount === 0) {
    list.innerHTML =
      '<div class="text-[12.5px] px-1" style="color:var(--text-faint);">No findings for this SQL under the current rule set.</div>';
  }

  // formatted sql
  const formattedParts = (data.statements || [])
    .filter((s) => s.formatted_sql)
    .map((s) => s.formatted_sql);
  $("#formatted-output").textContent = formattedParts.length
    ? formattedParts.join(";\n\n") + ";"
    : "(No formatted SQL — PL/SQL blocks and unparsed statements are shown as-is in Statements.)";

  // statements table
  const stmtBody = $("#statements-body");
  stmtBody.innerHTML = "";
  const statements = data.statements || [];
  $("#statements-count-pill").textContent = String(statements.length);
  statements.forEach((s) => {
    const tr = document.createElement("tr");
    const tdIdx = el("td", "rowlabel mono", "#" + (s.index + 1));
    const tdType = el("td", "mono", s.statement_type || "");
    const tdOk = document.createElement("td");
    tdOk.appendChild(el("span", "badge badge-" + (s.parse_ok ? "OK" : "CRITICAL"), s.parse_ok ? "OK" : "PARSE ERROR"));
    const tdFindings = el("td", "", (s.findings || []).map((f) => f.rule_id).join(", ") || "—");
    tr.appendChild(tdIdx);
    tr.appendChild(tdType);
    tr.appendChild(tdOk);
    tr.appendChild(tdFindings);
    stmtBody.appendChild(tr);
  });
}

function renderFinding(stmt, f) {
  const card = el("div", `finding finding-${f.severity} p-4`);

  const top = el("div", "flex items-start justify-between gap-3");
  const left = el("div", "flex items-center gap-2 flex-wrap");
  left.appendChild(el("span", `badge badge-${f.severity}`, f.severity));
  left.appendChild(el("span", "text-[13px] font-medium", f.title));
  top.appendChild(left);
  top.appendChild(el("span", "text-[10.5px] mono shrink-0", f.rule_id));
  card.appendChild(top);

  const meta = el(
    "div",
    "flex items-center gap-2 mt-1.5 text-[10.5px]",
  );
  meta.style.color = "var(--text-faint)";
  meta.appendChild(el("span", "badge-outline badge", `${stmt.statement_type} · stmt ${stmt.index + 1}`));
  meta.appendChild(el("span", "badge-outline badge", f.confidence));
  meta.appendChild(el("span", "badge-outline badge", f.basis === "database" ? "requires database" : "static"));
  card.appendChild(meta);

  if (f.evidence) {
    const ev = el("div", "evidence px-3 py-2 mt-3");
    ev.textContent = f.evidence;
    card.appendChild(ev);
  }

  const why = el("p", "text-[12px] mt-3");
  why.style.color = "var(--text-muted)";
  why.textContent = f.why_it_matters;
  card.appendChild(why);

  const rec = el("p", "text-[12px] mt-1.5");
  rec.style.color = "var(--text-muted)";
  const recLabel = el("span", "", "Recommendation: ");
  recLabel.style.color = "var(--text)";
  recLabel.style.fontWeight = "500";
  rec.appendChild(recLabel);
  rec.appendChild(document.createTextNode(f.recommendation));
  card.appendChild(rec);

  return card;
}

// ---------------------------------------------------------------- format

$("#btn-format").addEventListener("click", async () => {
  const sql = $("#sql-input").value;
  const btn = $("#btn-format");
  btn.disabled = true;
  try {
    const res = await fetch("/api/format", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sql }),
    });
    const data = await res.json();
    if (data.ok) {
      if (data.formatted) { $("#sql-input").value = data.formatted; syncEditorGutter(); }
    } else {
      const panel = $("#status-panel");
      panel.classList.remove("hidden");
      $("#status-badge").className = "badge badge-CRITICAL";
      $("#status-badge").textContent = "CRITICAL";
      $("#status-text").textContent = "Format failed: " + (data.error || "parse error").split("\n")[0];
      $("#status-counts").innerHTML = "";
    }
  } finally {
    btn.disabled = false;
  }
});

// ---------------------------------------------------------------- history

async function loadHistory() {
  const body = $("#history-body");
  const empty = $("#history-empty");
  body.innerHTML = "";
  try {
    const res = await fetch("/api/history?limit=50");
    const data = await res.json();
    const rows = data.history || [];
    if (rows.length === 0) {
      empty.classList.remove("hidden");
      return;
    }
    empty.classList.add("hidden");
    rows.forEach((r) => {
      const tr = document.createElement("tr");

      const tdId = el("td", "mono", "#" + r.id);
      const tdStatus = el("td", "");
      const badge = el("span", "badge badge-" + (r.status === "OK" ? "OK" : r.status), r.status);
      tdStatus.appendChild(badge);

      const tdSql = el("td", "mono");
      tdSql.style.maxWidth = "480px";
      tdSql.style.overflow = "hidden";
      tdSql.style.textOverflow = "ellipsis";
      tdSql.style.whiteSpace = "nowrap";
      tdSql.textContent = r.sql.replace(/\s+/g, " ").trim();

      const tdTime = el("td", "mono", r.created_at);

      tr.appendChild(tdId);
      tr.appendChild(tdStatus);
      tr.appendChild(tdSql);
      tr.appendChild(tdTime);
      body.appendChild(tr);
    });
  } catch (err) {
    empty.classList.remove("hidden");
    empty.textContent = "Could not load history: " + err;
  }
}

$("#btn-refresh-history").addEventListener("click", loadHistory);

// ---------------------------------------------------------------- rules (full catalog view)

let RULES_CACHE = [];
let VALID_SEVERITIES = ["CRITICAL", "ERROR", "WARNING", "INFO"];
let RULES_CATEGORY_FILTER = "";

async function loadRules() {
  const wrap = $("#rules-list");
  wrap.innerHTML = '<div class="text-[12.5px]" style="color:var(--text-faint);">Loading…</div>';
  try {
    const res = await fetch("/api/rules");
    const data = await res.json();
    RULES_CACHE = data.rules || [];
    if (data.valid_severities) VALID_SEVERITIES = data.valid_severities;
    populateCategoryFilter();
    renderRules();
  } catch (err) {
    wrap.innerHTML = `<div class="text-[12.5px]" style="color:var(--crit);">Could not load rule catalog: ${err}</div>`;
  }
}

function populateCategoryFilter() {
  const sel = $("#rules-category-filter");
  const categories = Array.from(new Set(RULES_CACHE.map((r) => r.category))).sort();
  const current = sel.value;
  sel.innerHTML = '<option value="">All categories</option>';
  categories.forEach((c) => {
    const opt = document.createElement("option");
    opt.value = c;
    opt.textContent = c;
    sel.appendChild(opt);
  });
  sel.value = current || "";
}

function renderRules() {
  const wrap = $("#rules-list");
  wrap.innerHTML = "";

  const rules = RULES_CATEGORY_FILTER
    ? RULES_CACHE.filter((r) => r.category === RULES_CATEGORY_FILTER)
    : RULES_CACHE;

  if (rules.length === 0) {
    wrap.innerHTML = '<div class="text-[12.5px]" style="color:var(--text-faint);">No rules in this category.</div>';
    return;
  }

  rules.forEach((r) => {
    wrap.appendChild(renderRuleCard(r));
  });
}

function renderRuleCard(r) {
  const card = el("div", "panel p-4");
  card.dataset.ruleId = r.id;
  if (!r.enabled) card.style.opacity = "0.55";

  const top = el("div", "flex items-center justify-between gap-3 flex-wrap");
  const left = el("div", "flex items-center gap-2 flex-wrap");
  const sevBadge = el("span", "badge badge-" + r.severity, r.severity);
  left.appendChild(sevBadge);
  left.appendChild(el("span", "text-[13px] font-medium", r.title));
  top.appendChild(left);
  top.appendChild(el("span", "text-[10.5px] mono", r.id));
  card.appendChild(top);

  const desc = el("p", "text-[12px] mt-2");
  desc.style.color = "var(--text-muted)";
  desc.textContent = r.description;
  card.appendChild(desc);

  const controls = el("div", "flex items-center justify-between gap-3 mt-3 flex-wrap");

  const meta = el("div", "flex items-center gap-2 flex-wrap");
  meta.appendChild(el("span", "badge badge-outline", r.category));
  meta.appendChild(el("span", "badge badge-outline", r.requires_database ? "requires database" : "offline"));
  controls.appendChild(meta);

  const actions = el("div", "flex items-center gap-2");

  const sevSelect = document.createElement("select");
  sevSelect.className = "btn btn-ghost text-[11.5px] py-1";
  VALID_SEVERITIES.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s;
    opt.textContent = s;
    if (s === r.severity) opt.selected = true;
    sevSelect.appendChild(opt);
  });
  sevSelect.addEventListener("change", () =>
    patchRule(r.id, { severity: sevSelect.value }, card, sevBadge)
  );
  actions.appendChild(sevSelect);

  const toggleBtn = el("button", "btn " + (r.enabled ? "btn-secondary" : "btn-ghost"),
    r.enabled ? "Enabled" : "Disabled");
  toggleBtn.addEventListener("click", () =>
    patchRule(r.id, { enabled: !r.enabled }, card, sevBadge, toggleBtn)
  );
  actions.appendChild(toggleBtn);

  controls.appendChild(actions);
  card.appendChild(controls);

  return card;
}

async function patchRule(ruleId, changes, card, sevBadge, toggleBtn) {
  try {
    const res = await fetch(`/api/rules/${encodeURIComponent(ruleId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      alert("Could not update rule: " + (err.error || res.status));
      return;
    }
    const updated = await res.json();

    const idx = RULES_CACHE.findIndex((x) => x.id === ruleId);
    if (idx !== -1) RULES_CACHE[idx] = updated;

    if (sevBadge) {
      sevBadge.className = "badge badge-" + updated.severity;
      sevBadge.textContent = updated.severity;
    }
    if (toggleBtn) {
      toggleBtn.textContent = updated.enabled ? "Enabled" : "Disabled";
      toggleBtn.className = "btn " + (updated.enabled ? "btn-secondary" : "btn-ghost");
    }
    if (card) card.style.opacity = updated.enabled ? "1" : "0.55";

    // keep the sidebar explorer's rule tree in sync
    loadExplorerRules();
  } catch (err) {
    alert("Could not update rule: " + err);
  }
}

$("#rules-category-filter").addEventListener("change", (e) => {
  RULES_CATEGORY_FILTER = e.target.value;
  renderRules();
});

$("#btn-reload-rules").addEventListener("click", async () => {
  const btn = $("#btn-reload-rules");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Reloading…";
  try {
    await fetch("/api/rules/reload", { method: "POST" });
    await loadRules();
  } catch (err) {
    alert("Could not reload rule catalog: " + err);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

// ---------------------------------------------------------------- explorer: rules (sidebar, compact)

async function loadExplorerRules() {
  const wrap = $("#explorer-rules-tree");
  wrap.innerHTML = '<div class="text-[11.5px] px-1.5 py-2" style="color:var(--text-faint);">Loading…</div>';
  try {
    const res = await fetch("/api/rules");
    const data = await res.json();
    const rules = data.rules || [];
    const byCategory = {};
    rules.forEach((r) => {
      (byCategory[r.category] = byCategory[r.category] || []).push(r);
    });
    wrap.innerHTML = "";
    Object.keys(byCategory).sort().forEach((cat) => {
      wrap.appendChild(renderExplorerRuleCategory(cat, byCategory[cat]));
    });
  } catch (err) {
    wrap.innerHTML = `<div class="text-[11.5px] px-1.5" style="color:var(--crit);">Could not load rules: ${err}</div>`;
  }
}

function renderExplorerRuleCategory(category, rules) {
  const wrap = el("div");
  const head = el("div", "tree-node tree-table");
  const caret = el("span", "tree-caret open");
  caret.innerHTML = "&#9656;";
  head.appendChild(caret);
  head.appendChild(el("span", "tree-icon", "▤"));
  head.appendChild(el("span", "", `${category} (${rules.length})`));
  const children = el("div", "tree-children");

  head.addEventListener("click", () => {
    const isOpen = caret.classList.toggle("open");
    children.classList.toggle("hidden", !isOpen);
  });

  rules.forEach((r) => {
    const row = el("div", "tree-node");
    row.style.justifyContent = "space-between";
    const left = el("div", "flex items-center gap-1.5");
    const dot = document.createElement("span");
    dot.style.width = "6px";
    dot.style.height = "6px";
    dot.style.borderRadius = "999px";
    dot.style.flexShrink = "0";
    dot.style.background = r.enabled ? "var(--" + severityColorVar(r.severity) + ")" : "var(--text-faint)";
    left.appendChild(dot);
    const label = el("span", "", r.id);
    label.title = r.title;
    left.appendChild(label);
    row.appendChild(left);
    if (!r.enabled) row.style.opacity = "0.55";
    row.title = r.title + (r.enabled ? "" : " (disabled)");
    children.appendChild(row);
  });

  wrap.appendChild(head);
  wrap.appendChild(children);
  return wrap;
}

function severityColorVar(sev) {
  if (sev === "CRITICAL" || sev === "ERROR") return "crit";
  if (sev === "WARNING") return "warn";
  if (sev === "INFO") return "info";
  return "ok";
}

$("#explorer-rules-search").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  $all("#explorer-rules-tree .tree-node:not(.tree-table)").forEach((row) => {
    const match = !q || row.textContent.toLowerCase().includes(q) || (row.title || "").toLowerCase().includes(q);
    row.classList.toggle("hidden", !match);
  });
});

// ---------------------------------------------------------------- schema (full page view)

const SAMPLE_DDL =
`CREATE TABLE employees (
    employee_id NUMBER(10) NOT NULL,
    first_name VARCHAR2(50),
    last_name VARCHAR2(50) NOT NULL,
    department_id NUMBER(10),
    salary NUMBER(10,2),
    hire_date DATE,
    CONSTRAINT emp_pk PRIMARY KEY (employee_id)
);

CREATE TABLE departments (
    department_id NUMBER(10) PRIMARY KEY,
    name VARCHAR2(100) NOT NULL
);`;

$("#btn-schema-sample").addEventListener("click", () => {
  $("#schema-ddl-input").value = SAMPLE_DDL;
});

$("#btn-schema-import").addEventListener("click", async () => {
  const ddl = $("#schema-ddl-input").value;
  const btn = $("#btn-schema-import");
  const resultBox = $("#schema-import-result");
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Importing…";
  try {
    const res = await fetch("/api/schema/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ddl }),
    });
    const data = await res.json();
    const parts = [];
    if (data.imported && data.imported.length > 0) {
      parts.push(`Imported: ${data.imported.join(", ")}`);
    }
    if (data.errors && data.errors.length > 0) {
      parts.push(...data.errors.map((e) => "Error: " + e));
    }
    if (parts.length === 0) {
      parts.push("Nothing was imported.");
    }
    resultBox.innerHTML = "";
    parts.forEach((p) => {
      const isError = p.startsWith("Error:");
      const line = el("div", "", p);
      line.style.color = isError ? "var(--crit)" : "var(--ok)";
      resultBox.appendChild(line);
    });
    if (data.imported && data.imported.length > 0) {
      $("#schema-ddl-input").value = "";
      await loadSchema();
      await loadExplorerSchema();
    }
  } catch (err) {
    resultBox.innerHTML = "";
    const line = el("div", "", "Could not import DDL: " + err);
    line.style.color = "var(--crit)";
    resultBox.appendChild(line);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

$("#btn-schema-clear").addEventListener("click", async () => {
  if (!confirm("Remove all imported tables from the offline schema?")) return;
  try {
    await fetch("/api/schema/clear", { method: "POST" });
    await loadSchema();
    await loadExplorerSchema();
  } catch (err) {
    alert("Could not clear schema: " + err);
  }
});

async function loadSchema() {
  const wrap = $("#schema-tables-list");
  wrap.innerHTML = '<div class="text-[12.5px]" style="color:var(--text-faint);">Loading…</div>';
  try {
    const res = await fetch("/api/schema");
    const data = await res.json();
    wrap.innerHTML = "";
    const tables = data.tables || [];
    if (tables.length === 0) {
      wrap.innerHTML =
        '<div class="text-[12.5px]" style="color:var(--text-faint);">No tables imported yet. Paste DDL above to get started.</div>';
      return;
    }
    tables.forEach((t) => wrap.appendChild(renderSchemaTableCard(t)));
  } catch (err) {
    wrap.innerHTML = `<div class="text-[12.5px]" style="color:var(--crit);">Could not load schema: ${err}</div>`;
  }
}

function renderSchemaTableCard(t) {
  const card = el("div", "panel p-4");

  const top = el("div", "flex items-center justify-between gap-3 flex-wrap");
  const left = el("div", "flex items-center gap-2 flex-wrap");
  left.appendChild(el("span", "text-[13px] font-medium mono", t.name));
  if (t.primary_key && t.primary_key.length > 0) {
    left.appendChild(el("span", "badge badge-outline", "PK: " + t.primary_key.join(", ")));
  } else {
    left.appendChild(el("span", "badge badge-outline", "no primary key"));
  }
  top.appendChild(left);

  const removeBtn = el("button", "btn btn-ghost text-[11.5px] py-1", "Remove");
  removeBtn.addEventListener("click", async () => {
    if (!confirm(`Remove '${t.name}' from the imported schema?`)) return;
    await fetch(`/api/schema/${encodeURIComponent(t.name)}`, { method: "DELETE" });
    await loadSchema();
    await loadExplorerSchema();
  });
  top.appendChild(removeBtn);
  card.appendChild(top);

  const table = document.createElement("table");
  table.className = "reftable mt-3";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Column</th><th>Type</th><th>Nullable</th><th>PK</th></tr>";
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  (t.columns || []).forEach((c) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td class="mono rowlabel">${escapeHtml(c.name)}</td>` +
      `<td class="mono">${escapeHtml(c.data_type || "")}</td>` +
      `<td>${c.nullable ? "yes" : "no"}</td>` +
      `<td>${c.is_primary_key ? "✓" : ""}</td>`;
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  card.appendChild(table);

  return card;
}

// ---------------------------------------------------------------- explorer: schema (sidebar tree)

async function loadExplorerSchema() {
  const wrap = $("#explorer-schema-tree");
  wrap.innerHTML = '<div class="text-[11.5px] px-1.5 py-2" style="color:var(--text-faint);">Loading…</div>';
  try {
    const res = await fetch("/api/schema");
    const data = await res.json();
    const tables = data.tables || [];
    wrap.innerHTML = "";
    if (tables.length === 0) {
      const empty = el("div", "text-[11.5px] px-1.5 py-2", "No tables imported. Use the Schema tab to paste DDL.");
      empty.style.color = "var(--text-faint)";
      wrap.appendChild(empty);
      return;
    }
    tables.forEach((t) => wrap.appendChild(renderExplorerTableNode(t)));
  } catch (err) {
    wrap.innerHTML = `<div class="text-[11.5px] px-1.5" style="color:var(--crit);">Could not load schema: ${err}</div>`;
  }
}

function renderExplorerTableNode(t) {
  const wrap = el("div");
  const head = el("div", "tree-node tree-table");
  const caret = el("span", "tree-caret");
  caret.innerHTML = "&#9656;";
  head.appendChild(caret);
  head.appendChild(el("span", "tree-icon", "☷"));
  head.appendChild(el("span", "", t.name));
  const children = el("div", "tree-children hidden");

  head.addEventListener("click", () => {
    const isOpen = caret.classList.toggle("open");
    children.classList.toggle("hidden", !isOpen);
  });

  (t.columns || []).forEach((c) => {
    const row = el("div", "tree-node");
    const bits = [c.name];
    if (c.is_primary_key) bits.push("PK");
    row.textContent = bits.join(" · ");
    row.title = `${c.name} ${c.data_type || ""} ${c.nullable ? "NULL" : "NOT NULL"}`.trim();
    children.appendChild(row);
  });

  wrap.appendChild(head);
  wrap.appendChild(children);
  return wrap;
}

$("#explorer-schema-search").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  $all("#explorer-schema-tree > div").forEach((tableWrap) => {
    if (!q) { tableWrap.classList.remove("hidden"); return; }
    const matches = tableWrap.textContent.toLowerCase().includes(q);
    tableWrap.classList.toggle("hidden", !matches);
  });
});

// ---------------------------------------------------------------- PL/SQL vs T-SQL comparison view

const QUICKREF_ROWS = [
  ["Block terminator", "<code class=\"mono\">/</code> on its own line ends a stored block in SQL*Plus/SQLcl", "<code class=\"mono\">GO</code> on its own line is a batch separator (client-side, not real T-SQL syntax)"],
  ["Statement terminator", "Semicolon <code class=\"mono\">;</code> (often optional in SQL*Plus, required inside PL/SQL)", "Semicolon is optional in most contexts, but required before a CTE and recommended everywhere"],
  ["Variable declaration", "In a <code class=\"mono\">DECLARE</code> section: <code class=\"mono\">v_name VARCHAR2(50);</code>", "Anywhere in a batch: <code class=\"mono\">DECLARE @name VARCHAR(50);</code>"],
  ["Variable prefix", "No sigil — plain identifier (<code class=\"mono\">v_salary</code> by convention)", "<code class=\"mono\">@</code> prefix required (<code class=\"mono\">@Salary</code>)"],
  ["Assignment", "<code class=\"mono\">v_x := 10;</code>", "<code class=\"mono\">SET @x = 10;</code> or <code class=\"mono\">SELECT @x = 10;</code>"],
  ["String concatenation", "<code class=\"mono\">'a' || 'b'</code>", "<code class=\"mono\">'a' + 'b'</code> (or <code class=\"mono\">CONCAT('a','b')</code>)"],
  ["Print / debug output", "<code class=\"mono\">DBMS_OUTPUT.PUT_LINE('x')</code>", "<code class=\"mono\">PRINT 'x'</code>"],
  ["Null-coalescing", "<code class=\"mono\">NVL(a, b)</code>, <code class=\"mono\">COALESCE(a, b, ...)</code>", "<code class=\"mono\">ISNULL(a, b)</code>, <code class=\"mono\">COALESCE(a, b, ...)</code>"],
  ["Current date/time", "<code class=\"mono\">SYSDATE</code>, <code class=\"mono\">SYSTIMESTAMP</code>", "<code class=\"mono\">GETDATE()</code>, <code class=\"mono\">SYSDATETIME()</code>"],
  ["Substring", "<code class=\"mono\">SUBSTR(s, start, len)</code>", "<code class=\"mono\">SUBSTRING(s, start, len)</code>"],
  ["String length", "<code class=\"mono\">LENGTH(s)</code>", "<code class=\"mono\">LEN(s)</code>"],
  ["Row limiting", "<code class=\"mono\">FETCH FIRST n ROWS ONLY</code> (or legacy <code class=\"mono\">ROWNUM</code>)", "<code class=\"mono\">TOP n</code> in the SELECT list, or <code class=\"mono\">OFFSET/FETCH</code>"],
  ["Sequences / identity", "<code class=\"mono\">CREATE SEQUENCE</code>, <code class=\"mono\">seq.NEXTVAL</code>", "<code class=\"mono\">IDENTITY(1,1)</code> column, or <code class=\"mono\">SEQUENCE</code> object + <code class=\"mono\">NEXT VALUE FOR</code>"],
  ["Temporary tables", "Global/private temporary tables (<code class=\"mono\">CREATE GLOBAL TEMPORARY TABLE</code>)", "<code class=\"mono\">#temp</code> (session-local) / <code class=\"mono\">##temp</code> (global)"],
  ["IF statement", "<code class=\"mono\">IF ... THEN ... ELSIF ... ELSE ... END IF;</code>", "<code class=\"mono\">IF ... BEGIN ... END ELSE BEGIN ... END</code>"],
  ["Loop (counted)", "<code class=\"mono\">FOR i IN 1..10 LOOP ... END LOOP;</code>", "<code class=\"mono\">WHILE @i <= 10 BEGIN ... SET @i += 1 END</code> (no native counted loop)"],
  ["Loop (conditional)", "<code class=\"mono\">WHILE cond LOOP ... END LOOP;</code>", "<code class=\"mono\">WHILE cond BEGIN ... END</code>"],
  ["Cursors", "Explicit <code class=\"mono\">CURSOR c IS SELECT ...</code> + <code class=\"mono\">OPEN/FETCH/CLOSE</code>, or a cursor <code class=\"mono\">FOR</code> loop", "<code class=\"mono\">DECLARE CURSOR</code> + <code class=\"mono\">OPEN/FETCH NEXT/CLOSE/DEALLOCATE</code>"],
  ["Exception handling", "<code class=\"mono\">EXCEPTION WHEN ... THEN ...</code> block at the end of a PL/SQL block", "<code class=\"mono\">TRY ... CATCH</code> block"],
  ["Raising an error", "<code class=\"mono\">RAISE_APPLICATION_ERROR(-20001, 'msg')</code>", "<code class=\"mono\">THROW 50001, 'msg', 1;</code> or <code class=\"mono\">RAISERROR</code>"],
  ["Stored procedure", "<code class=\"mono\">CREATE OR REPLACE PROCEDURE p (a IN NUMBER) IS BEGIN ... END;</code>", "<code class=\"mono\">CREATE PROCEDURE p @a INT AS BEGIN ... END;</code>"],
  ["Calling a procedure", "<code class=\"mono\">CALL p(1);</code> or <code class=\"mono\">BEGIN p(1); END;</code>", "<code class=\"mono\">EXEC p @a = 1;</code>"],
  ["Function", "<code class=\"mono\">CREATE FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END;</code>", "<code class=\"mono\">CREATE FUNCTION f() RETURNS INT AS BEGIN RETURN 1 END</code>"],
  ["Dynamic SQL", "<code class=\"mono\">EXECUTE IMMEDIATE 'stmt' USING val;</code>", "<code class=\"mono\">EXEC sp_executesql N'stmt', N'@p INT', @p;</code> or <code class=\"mono\">EXEC('stmt')</code>"],
  ["Transaction control", "<code class=\"mono\">COMMIT;</code> / <code class=\"mono\">ROLLBACK;</code> / <code class=\"mono\">SAVEPOINT</code>", "<code class=\"mono\">BEGIN TRAN</code> / <code class=\"mono\">COMMIT TRAN</code> / <code class=\"mono\">ROLLBACK TRAN</code> / <code class=\"mono\">SAVE TRAN</code>"],
  ["Comments", "<code class=\"mono\">-- line</code> and <code class=\"mono\">/* block */</code>", "<code class=\"mono\">-- line</code> and <code class=\"mono\">/* block */</code> (same)"],
  ["Case-sensitivity", "Identifiers upper-cased unless double-quoted", "Identifiers keep their case; comparisons often case-insensitive by default collation"],
];

function renderCompareQuickref() {
  const body = $("#compare-quickref-body");
  if (body.dataset.rendered) return;
  body.innerHTML = QUICKREF_ROWS.map(([aspect, plsql, tsql]) =>
    `<tr><td class="rowlabel">${aspect}</td><td>${plsql}</td><td>${tsql}</td></tr>`
  ).join("");
  body.dataset.rendered = "1";
}

const COMPARE_TOPICS = [
  {
    title: "1. Anonymous block & variable declaration",
    plsql:
`DECLARE
  v_bonus NUMBER := 0;
  v_name  VARCHAR2(50);
BEGIN
  v_name  := 'Ada';
  v_bonus := 1500 * 1.1;
  DBMS_OUTPUT.PUT_LINE(v_name || ': ' || v_bonus);
END;
/`,
    tsql:
`DECLARE @Bonus DECIMAL(10,2) = 0;
DECLARE @Name  VARCHAR(50);

SET @Name  = 'Ada';
SET @Bonus = 1500 * 1.1;
PRINT @Name + ': ' + CAST(@Bonus AS VARCHAR(20));
GO`,
  },
  {
    title: "2. Conditional logic",
    plsql:
`IF v_salary > 100000 THEN
  v_tier := 'SENIOR';
ELSIF v_salary > 50000 THEN
  v_tier := 'MID';
ELSE
  v_tier := 'JUNIOR';
END IF;`,
    tsql:
`IF @Salary > 100000
  SET @Tier = 'SENIOR';
ELSE IF @Salary > 50000
  SET @Tier = 'MID';
ELSE
  SET @Tier = 'JUNIOR';`,
  },
  {
    title: "3. Loops",
    plsql:
`FOR i IN 1..5 LOOP
  DBMS_OUTPUT.PUT_LINE('Row ' || i);
END LOOP;

WHILE v_count > 0 LOOP
  v_count := v_count - 1;
END LOOP;`,
    tsql:
`DECLARE @i INT = 1;
WHILE @i <= 5
BEGIN
  PRINT 'Row ' + CAST(@i AS VARCHAR(10));
  SET @i += 1;
END;

WHILE @Count > 0
  SET @Count -= 1;`,
  },
  {
    title: "4. Cursors",
    plsql:
`FOR emp_rec IN (
  SELECT employee_id, salary
  FROM employees
  WHERE department_id = 20
) LOOP
  DBMS_OUTPUT.PUT_LINE(emp_rec.employee_id);
END LOOP;`,
    tsql:
`DECLARE emp_cursor CURSOR FOR
  SELECT employee_id, salary
  FROM employees
  WHERE department_id = 20;

OPEN emp_cursor;
FETCH NEXT FROM emp_cursor INTO @EmpId, @Salary;
WHILE @@FETCH_STATUS = 0
BEGIN
  PRINT @EmpId;
  FETCH NEXT FROM emp_cursor INTO @EmpId, @Salary;
END;
CLOSE emp_cursor;
DEALLOCATE emp_cursor;`,
  },
  {
    title: "5. Exception / error handling",
    plsql:
`BEGIN
  UPDATE employees SET salary = salary / 0
  WHERE employee_id = 100;
EXCEPTION
  WHEN ZERO_DIVIDE THEN
    DBMS_OUTPUT.PUT_LINE('Division by zero');
  WHEN OTHERS THEN
    RAISE_APPLICATION_ERROR(-20001, 'Unexpected error');
END;
/`,
    tsql:
`BEGIN TRY
  UPDATE employees SET salary = salary / 0
  WHERE employee_id = 100;
END TRY
BEGIN CATCH
  PRINT 'Error: ' + ERROR_MESSAGE();
  THROW 50001, 'Unexpected error', 1;
END CATCH;`,
  },
  {
    title: "6. Stored procedures",
    plsql:
`CREATE OR REPLACE PROCEDURE give_raise(
  p_emp_id IN NUMBER,
  p_pct    IN NUMBER
) IS
BEGIN
  UPDATE employees
  SET salary = salary * (1 + p_pct / 100)
  WHERE employee_id = p_emp_id;
  COMMIT;
END give_raise;
/

CALL give_raise(100, 10);`,
    tsql:
`CREATE PROCEDURE dbo.GiveRaise
  @EmpId INT,
  @Pct   DECIMAL(5,2)
AS
BEGIN
  UPDATE employees
  SET salary = salary * (1 + @Pct / 100)
  WHERE employee_id = @EmpId;
END;
GO

EXEC dbo.GiveRaise @EmpId = 100, @Pct = 10;`,
  },
  {
    title: "7. Dynamic SQL (the pattern this analyzer flags)",
    plsql:
`EXECUTE IMMEDIATE
  'DELETE FROM logs WHERE id = ' || p_id;
  -- DYNSQL-002: built via || concatenation, SQL-injection risk

EXECUTE IMMEDIATE
  'DELETE FROM logs WHERE id = :id'
  USING p_id;
  -- safer: bind variable instead of concatenation`,
    tsql:
`EXEC('DELETE FROM logs WHERE id = ' + @Id);
-- same concatenation risk

EXEC sp_executesql
  N'DELETE FROM logs WHERE id = @p',
  N'@p INT', @p = @Id;
-- safer: parameterized call`,
  },
];

function renderCompareTopics() {
  const wrap = $("#compare-topics");
  if (wrap.dataset.rendered) return;
  wrap.innerHTML = "";
  COMPARE_TOPICS.forEach((topic) => {
    const box = el("div", "compare-topic");
    box.appendChild(el("div", "compare-topic-title", topic.title));
    const grid = el("div", "compare-grid");

    const left = el("div", "compare-col");
    const leftHead = el("div", "compare-col-head");
    leftHead.style.color = "var(--accent)";
    leftHead.textContent = "PL/SQL (Oracle)";
    left.appendChild(leftHead);
    const leftPre = el("pre", "sql-text text-[12px] px-3 py-2.5 rounded overflow-x-auto");
    leftPre.style.background = "var(--bg-raised)";
    leftPre.style.border = "1px solid var(--border-soft)";
    leftPre.style.color = "var(--text)";
    leftPre.textContent = topic.plsql;
    left.appendChild(leftPre);

    const right = el("div", "compare-col");
    const rightHead = el("div", "compare-col-head");
    rightHead.style.color = "var(--info)";
    rightHead.textContent = "Transact-SQL (SQL Server)";
    right.appendChild(rightHead);
    const rightPre = el("pre", "sql-text text-[12px] px-3 py-2.5 rounded overflow-x-auto");
    rightPre.style.background = "var(--bg-raised)";
    rightPre.style.border = "1px solid var(--border-soft)";
    rightPre.style.color = "var(--text)";
    rightPre.textContent = topic.tsql;
    right.appendChild(rightPre);

    grid.appendChild(left);
    grid.appendChild(right);
    box.appendChild(grid);
    wrap.appendChild(box);
  });
  wrap.dataset.rendered = "1";
}

function renderCompareView() {
  renderCompareQuickref();
  renderCompareTopics();
}

// ---------------------------------------------------------------- init

// Run one analysis on load so the app never looks broken/empty.
analyze();
loadExplorerSchema();
loadExplorerRules();
