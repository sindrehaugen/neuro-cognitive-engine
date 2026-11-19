/* NCE Core v3.7.0 — Air-Gapped Unified 5-Tab Control Plane Client
 * Zero external network calls; safe DOM construction via createElement + textContent only.
 */
(function () {
  "use strict";

  const INITIAL_TAB =
    (document.body && document.body.getAttribute("data-initial-tab")) || "overview";

  const state = {
    activeTab: INITIAL_TAB,
    authenticated: false,
    principal: "",
    authMode: "",
    csrfToken: "",
    overview: null,
    backups: null,
    incidents: [],
    probeRuns: [],
    selectedIncidentId: null,
    playbooks: [],
    quarantine: [],
    tenants: null,
    pluginsCatalog: null,
    coreTools: { mcp_tools: [], a2a_skills: [] },
    pluginFilters: {
      type: "all",
      category: "all",
      maturity: "all",
      status: "all",
      q: "",
    },
    pendingPlaybookId: null,
    pendingConfirmationToken: null,
    activeVaultSlug: null,
    toastTimer: null,
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function readCookie(name) {
    const raw = document.cookie || "";
    const parts = raw.split(";");
    for (let i = 0; i < parts.length; i += 1) {
      const seg = parts[i].trim();
      const idx = seg.indexOf("=");
      if (idx > 0 && seg.slice(0, idx).trim() === name) {
        return decodeURIComponent(seg.slice(idx + 1).trim());
      }
    }
    return "";
  }

  function showToast(msg, kind) {
    const toast = byId("toast");
    if (!toast) return;
    toast.className = "toast" + (kind === "err" ? " err" : kind === "ok" ? " ok" : "");
    toast.textContent = msg;
    toast.classList.remove("hidden");
    if (state.toastTimer) clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.add("hidden"), 4200);
  }

  function fmtNum(val, suffix) {
    if (val === null || val === undefined || Number.isNaN(Number(val))) return "-";
    return suffix ? String(val) + suffix : String(val);
  }

  function fmtDate(val) {
    if (!val) return "-";
    return String(val).replace("T", " ").replace("+00:00", "Z");
  }

  function buildMcpUrl() {
    const host = (window.location && window.location.hostname) || "localhost";
    const norm = host === "testserver" ? "localhost" : host;
    return "https:" + "//" + norm + ":8443/mcp";
  }

  async function apiFetch(url, options) {
    const opts = Object.assign({ credentials: "same-origin" }, options || {});
    const method = (opts.method || "GET").toUpperCase();
    const headers = Object.assign({}, opts.headers || {});
    if (method !== "GET" && method !== "HEAD" && method !== "OPTIONS") {
      const csrf = state.csrfToken || readCookie("nce_admin_csrf");
      if (csrf) headers["X-NCE-CSRF-Token"] = csrf;
      if (opts.body && !headers["Content-Type"]) {
        headers["Content-Type"] = "application/json";
      }
    }
    opts.headers = headers;

    const res = await fetch(url, opts);
    let data = null;
    try {
      data = await res.json();
    } catch (_err) {
      data = null;
    }

    if (res.status === 401 && url !== "/admin/session/login") {
      state.authenticated = false;
      updateAuthPill();
      openLoginModal("Session required or expired.");
      throw new Error((data && data.error) || "Authentication required");
    }
    if (!res.ok) {
      throw new Error((data && data.error) || "HTTP " + res.status);
    }
    return data;
  }

  function updateAuthPill() {
    const pill = byId("auth-pill");
    const logoutBtn = byId("btn-logout");
    if (pill) {
      if (state.authenticated) {
        pill.className = "auth-pill ok";
        pill.textContent = (state.principal || "admin") + " [" + (state.authMode || "session") + "]";
      } else {
        pill.className = "auth-pill";
        pill.textContent = "unauthenticated";
      }
    }
    if (logoutBtn) {
      logoutBtn.classList.toggle("hidden", !state.authenticated);
    }
  }

  function openLoginModal(message) {
    const modal = byId("login-modal");
    const errEl = byId("login-error");
    if (errEl) {
      errEl.textContent = message || "";
      errEl.classList.toggle("hidden", !message);
    }
    if (modal) modal.classList.remove("hidden");
  }

  function closeLoginModal() {
    const modal = byId("login-modal");
    if (modal) modal.classList.add("hidden");
  }

  async function checkSessionAndBoot() {
    try {
      const st = await fetch("/admin/session/status", { credentials: "same-origin" });
      const data = await st.json();
      state.authenticated = Boolean(data && data.authenticated);
      state.principal = (data && data.principal) || "";
      state.authMode = (data && data.auth_mode) || "";
      state.csrfToken = (data && data.csrf_token) || readCookie("nce_admin_csrf") || "";
      updateAuthPill();
      if (!state.authenticated) {
        openLoginModal("");
        return;
      }
      closeLoginModal();
      await switchTab(state.activeTab, true);
    } catch (err) {
      openLoginModal("Unable to verify session status: " + err.message);
    }
  }

  async function submitLogin(evt) {
    evt.preventDefault();
    const keyInput = byId("login-secret-input");
    const modeSel = byId("login-mode-select");
    const errEl = byId("login-error");
    const secret = keyInput ? keyInput.value.trim() : "";
    const mode = modeSel ? modeSel.value : "api_key";

    if (!secret) {
      if (errEl) {
        errEl.textContent = "Enter an Admin API key or Bearer JWT.";
        errEl.classList.remove("hidden");
      }
      return;
    }

    const fetchOpts = { method: "POST", credentials: "same-origin", headers: {} };
    if (mode === "bearer") {
      fetchOpts.headers.Authorization = "Bearer " + secret;
    } else {
      fetchOpts.headers["Content-Type"] = "application/json";
      fetchOpts.body = JSON.stringify({ api_key: secret });
    }

    try {
      const res = await fetch("/admin/session/login", fetchOpts);
      const data = await res.json();
      if (!res.ok) {
        throw new Error((data && data.error) || "HTTP " + res.status);
      }
      state.authenticated = true;
      state.principal = data.principal || "admin";
      state.authMode = data.auth_mode || mode;
      state.csrfToken = data.csrf_token || readCookie("nce_admin_csrf") || "";
      if (keyInput) keyInput.value = "";
      updateAuthPill();
      closeLoginModal();
      showToast("Session active (" + state.authMode + ").", "ok");
      await switchTab(state.activeTab, true);
    } catch (err) {
      if (errEl) {
        errEl.textContent = err.message;
        errEl.classList.remove("hidden");
      }
    }
  }

  async function performLogout() {
    try {
      await apiFetch("/admin/session/logout", { method: "POST", body: "{}" });
    } catch (_err) {
      /* session already cleared or expired */
    }
    state.authenticated = false;
    state.csrfToken = "";
    state.principal = "";
    updateAuthPill();
    openLoginModal("Signed out.");
  }

  async function switchTab(tabId, forceReload) {
    state.activeTab = tabId || "overview";
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      const isMatch = btn.getAttribute("data-tab") === state.activeTab;
      btn.classList.toggle("active", isMatch);
      btn.setAttribute("aria-selected", isMatch ? "true" : "false");
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      const isMatch = panel.id === "panel-" + state.activeTab;
      panel.classList.toggle("active", isMatch);
    });

    if (!state.authenticated && !forceReload) return;
    if (state.activeTab === "overview") await loadOverview();
    else if (state.activeTab === "backups") await loadBackups();
    else if (state.activeTab === "ops") await loadOps();
    else if (state.activeTab === "tenants") await loadTenants();
    else if (state.activeTab === "plugins") await loadPluginsAndTools();
  }

  function badgeClassForStatus(statusStr) {
    const s = String(statusStr || "").toLowerCase();
    if (s === "up" || s === "ok" || s === "completed" || s === "passed" || s === "verified" || s === "resolved" || s === "active") {
      return "badge badge-ok";
    }
    if (s === "warn" || s === "warning" || s === "degraded" || s === "disabled" || s === "pending" || s === "requeued") {
      return "badge badge-warn";
    }
    if (s === "down" || s === "unreachable" || s === "failed" || s === "critical" || s === "open" || s === "quarantined") {
      return "badge badge-crit";
    }
    return "badge badge-neutral";
  }

  /* ------------------------------------------------------------------
   * Tab 1: Overview
   * ------------------------------------------------------------------ */
  async function loadOverview() {
    try {
      const data = await apiFetch("/admin/ops/api/overview");
      state.overview = data;
      renderOverview(data);
    } catch (err) {
      showToast("Overview load failed: " + err.message, "err");
    }
  }

  function renderOverview(data) {
    const banner = byId("stub-warning-banner");
    const cog = data.cognitive || {};
    if (banner) {
      banner.classList.toggle("hidden", !cog.stub_warning);
    }

    const storesGrid = byId("overview-stores-grid");
    if (storesGrid) {
      const stores = data.stores || {};
      const order = [
        ["postgres", "PostgreSQL + pgvector"],
        ["mongodb", "MongoDB Document Store"],
        ["redis", "Redis Cache & RQ"],
        ["minio", "MinIO Blob Store"],
      ];
      storesGrid.replaceChildren(
        ...order.map(([key, label]) => {
          const st = stores[key] || { status: "unknown" };
          const card = el("div", "card");
          const head = el("div", "engine-card-head");
          head.appendChild(el("div", "card-title", label));
          head.appendChild(el("span", badgeClassForStatus(st.status), String(st.status || "unknown").toUpperCase()));
          card.appendChild(head);
          card.appendChild(
            el("div", "metric-value", st.latency_ms != null ? fmtNum(st.latency_ms, " ms") : "-")
          );
          const sub = st.buckets != null
            ? "buckets: " + st.buckets
            : st.detail || "4-store telemetry probe";
          card.appendChild(el("div", "metric-sub", sub));
          return card;
        })
      );
    }

    const pgBox = byId("overview-pg-kv");
    if (pgBox) {
      const pg = data.postgres || {};
      const rls = data.rls || {};
      const rlsVerified = rls.verified_forced_tables;
      const rlsExpected = rls.expected_forced_tables || 0;
      const rlsOk = rlsVerified !== null && rlsVerified !== undefined && rlsVerified >= rlsExpected;

      const rlsBadge = byId("overview-rls-badge");
      if (rlsBadge) {
        rlsBadge.className = rlsOk ? "badge badge-ok" : "badge badge-warn";
        rlsBadge.textContent =
          "FORCE RLS " + (rlsVerified != null ? rlsVerified : "?") + "/" + rlsExpected;
      }

      const rows = [
        ["Pool Size / Idle", fmtNum(pg.pool_size) + " / " + fmtNum(pg.pool_idle)],
        ["PG Active / Idle-in-Tx", fmtNum(pg.active_connections) + " / " + fmtNum(pg.idle_in_transaction)],
        ["Waiting Locks", fmtNum(pg.waiting_locks)],
        ["Forced RLS Tables", (rlsVerified != null ? rlsVerified : "?") + " / " + rlsExpected],
      ];
      pgBox.replaceChildren(
        ...rows.map(([k, v]) => {
          const r = el("div", "kv-row");
          r.appendChild(el("span", "kv-key", k));
          r.appendChild(el("span", "kv-val", v));
          return r;
        })
      );
    }

    const redisBox = byId("overview-redis-kv");
    if (redisBox) {
      const rd = data.redis || {};
      const q = data.queues || {};
      const depths = q.depths || {};
      const depthStr = Object.keys(depths).length
        ? Object.entries(depths)
            .map(([k, v]) => k + ":" + v)
            .join(" | ")
        : "none";
      const rows = [
        ["Redis Memory", rd.used_memory_human || "-"],
        ["Salience Buffer Keys", fmtNum(rd.salience_buffer_keys)],
        ["RQ Failed / DLQ Total", fmtNum(q.failed_total)],
        ["RQ Queue Depths", depthStr],
      ];
      redisBox.replaceChildren(
        ...rows.map(([k, v]) => {
          const r = el("div", "kv-row");
          r.appendChild(el("span", "kv-key", k));
          r.appendChild(el("span", "kv-val", v));
          return r;
        })
      );
    }

    const diskBox = byId("overview-disk-card");
    if (diskBox) {
      const d = data.disk || {};
      const pct = Number(d.used_pct || 0);
      const bar = byId("overview-disk-fill");
      if (bar) {
        bar.style.width = Math.min(100, Math.max(0, pct)) + "%";
        bar.className = "progress-fill" + (pct >= 88 ? " crit" : pct >= 75 ? " warn" : "");
      }
      const lbl = byId("overview-disk-summary");
      if (lbl) {
        lbl.textContent =
          fmtNum(d.free_gib, " GiB free") +
          " / " +
          fmtNum(d.total_gib, " GiB total") +
          " (" +
          fmtNum(pct, "% used") +
          ") · path: " +
          (d.path || "-");
      }
    }

    const cogBox = byId("overview-cognitive-kv");
    if (cogBox) {
      const elm = data.ops_elm || {};
      const rows = [
        ["Cognitive Backend", cog.backend || "-"],
        ["Embedding Provider", cog.embedding_provider || "-"],
        ["Embedding Status", cog.status || "-"],
        ["Model", cog.model || "-"],
        ["Ops-ELM Advisory", (elm.status || "disabled") + (elm.configured ? " (" + elm.url + ")" : "")],
      ];
      cogBox.replaceChildren(
        ...rows.map(([k, v]) => {
          const r = el("div", "kv-row");
          r.appendChild(el("span", "kv-key", k));
          r.appendChild(el("span", "kv-val", v));
          return r;
        })
      );
    }
  }

  /* ------------------------------------------------------------------
   * Tab 2: Backups & Drills
   * ------------------------------------------------------------------ */
  async function loadBackups() {
    try {
      const data = await apiFetch("/admin/ops/api/backups");
      state.backups = data;
      renderBackups(data);
    } catch (err) {
      showToast("Backups load failed: " + err.message, "err");
    }
  }

  function renderBackups(data) {
    const pf = data.disk_preflight || data.preflight || {};
    const pfSummary = byId("backups-preflight-summary");
    const pfBadge = byId("backups-preflight-badge");
    const tombBadge = byId("backups-tombstone-badge");
    if (pfSummary) {
      pfSummary.textContent =
        "Available: " +
        fmtNum(pf.free_gib, " GiB") +
        " | Required Preflight (2.5x): " +
        fmtNum(pf.required_gib, " GiB") +
        " | Path: " +
        (pf.path || "-");
    }
    if (pfBadge) {
      pfBadge.className = pf.preflight_ok ? "badge badge-ok" : "badge badge-crit";
      pfBadge.textContent = pf.preflight_ok ? "PREFLIGHT OK" : "LOW DISK HEADROOM";
    }
    if (tombBadge) {
      tombBadge.textContent = "Tombstones: " + fmtNum(data.latest_tombstone_count);
    }

    const tbody = byId("backups-tbody");
    if (!tbody) return;
    const manifests = data.manifests || [];
    if (!manifests.length) {
      const tr = document.createElement("tr");
      const td = el("td", "", "No backup manifests recorded.");
      td.colSpan = 7;
      tr.appendChild(td);
      tbody.replaceChildren(tr);
      return;
    }

    tbody.replaceChildren(
      ...manifests.map((m) => {
        const tr = document.createElement("tr");
        tr.appendChild(el("td", "mono", m.backup_id));
        tr.appendChild(el("td", "mono", fmtDate(m.t_cut)));
        const stTd = document.createElement("td");
        stTd.appendChild(el("span", badgeClassForStatus(m.status), m.status));
        tr.appendChild(stTd);

        const sizeMb = (Number(m.size_bytes || 0) / (1024 * 1024)).toFixed(2);
        tr.appendChild(el("td", "mono", sizeMb + " MiB"));

        const wormCount = m.worm_heads ? Object.keys(m.worm_heads).length : 0;
        const wormTd = document.createElement("td");
        wormTd.appendChild(
          el("span", "badge badge-ok", "WORM:" + wormCount + " / T:" + fmtNum(m.tombstone_count))
        );
        tr.appendChild(wormTd);

        const drillTd = document.createElement("td");
        if (m.last_drill_status) {
          drillTd.appendChild(
            el(
              "span",
              badgeClassForStatus(m.last_drill_status),
              m.last_drill_status + " @ " + fmtDate(m.last_drill_at)
            )
          );
        } else {
          drillTd.appendChild(el("span", "badge badge-neutral", "not drilled"));
        }
        tr.appendChild(drillTd);

        const actTd = document.createElement("td");
        const drillBtn = el("button", "btn btn-sm", "Run Ephemeral Drill");
        drillBtn.type = "button";
        drillBtn.addEventListener("click", () => runBackupDrill(m.backup_id));
        actTd.appendChild(drillBtn);
        tr.appendChild(actTd);
        return tr;
      })
    );
  }

  async function triggerWatermarkBackup() {
    try {
      const res = await apiFetch("/admin/ops/api/backups/trigger", {
        method: "POST",
        body: JSON.stringify({ reason: "console_operator_trigger" }),
      });
      showToast("Watermark backup completed: " + (res.manifest && res.manifest.backup_id ? res.manifest.backup_id : "ok"), "ok");
      await loadBackups();
    } catch (err) {
      showToast("Backup trigger failed: " + err.message, "err");
    }
  }

  async function runBackupDrill(backupId) {
    try {
      const res = await apiFetch(
        "/admin/ops/api/backups/" + encodeURIComponent(backupId) + "/drill",
        { method: "POST", body: JSON.stringify({ approved_by: state.principal || "admin" }) }
      );
      const report = res.drill_report || {};
      showToast("Restore drill " + (report.status || "finished") + " for " + backupId, "ok");
      await loadBackups();
    } catch (err) {
      showToast("Restore drill failed: " + err.message, "err");
    }
  }

  /* ------------------------------------------------------------------
   * Tab 3: Ops Caddy, Playbooks & Quarantine
   * ------------------------------------------------------------------ */
  async function loadOps() {
    try {
      const [incData, qData] = await Promise.all([
        apiFetch("/admin/ops/api/incidents"),
        apiFetch("/admin/ops/api/quarantine"),
      ]);
      state.incidents = incData.incidents || [];
      state.probeRuns = incData.probe_runs || [];
      state.playbooks = incData.playbooks || [];
      state.quarantine = qData.items || [];
      if (state.incidents.length && !state.selectedIncidentId) {
        state.selectedIncidentId = state.incidents[0].incident_id;
      }
      renderOps();
    } catch (err) {
      showToast("Ops load failed: " + err.message, "err");
    }
  }

  function renderOps() {
    const incBody = byId("incidents-tbody");
    if (incBody) {
      if (!state.incidents.length) {
        const tr = document.createElement("tr");
        const td = el("td", "", "No incidents recorded.");
        td.colSpan = 5;
        tr.appendChild(td);
        incBody.replaceChildren(tr);
      } else {
        incBody.replaceChildren(
          ...state.incidents.map((inc) => {
            const tr = el(
              "tr",
              "selectable" + (inc.incident_id === state.selectedIncidentId ? " selected" : "")
            );
            tr.addEventListener("click", () => {
              state.selectedIncidentId = inc.incident_id;
              renderOps();
            });
            const sevTd = document.createElement("td");
            sevTd.appendChild(el("span", badgeClassForStatus(inc.severity), inc.severity));
            tr.appendChild(sevTd);
            tr.appendChild(el("td", "", inc.title));
            const stTd = document.createElement("td");
            stTd.appendChild(el("span", badgeClassForStatus(inc.status), inc.status));
            tr.appendChild(stTd);
            tr.appendChild(el("td", "mono", fmtNum(inc.occurrence_count)));
            tr.appendChild(el("td", "mono", inc.recommended_playbook || "-"));
            return tr;
          })
        );
      }
    }

    const selected = state.incidents.find((i) => i.incident_id === state.selectedIncidentId) || null;
    const factsPre = byId("incident-facts-pre");
    const elmBox = byId("incident-elm-box");
    const elmConfBadge = byId("incident-elm-confidence");
    const elmModeBadge = byId("incident-elm-mode");
    const elmActionBtn = byId("incident-elm-playbook-btn");

    if (factsPre) {
      factsPre.textContent = selected
        ? JSON.stringify(selected.deterministic_facts || {}, null, 2)
        : "Select an incident to inspect deterministic probe facts.";
    }
    if (elmBox) {
      if (!selected) {
        elmBox.textContent = "[Advisory ELM Brief] Select an incident.";
      } else if (!selected.elm_brief_json) {
        elmBox.textContent = "[Advisory ELM Brief] No LLM/ELM brief attached for this incident.";
      } else {
        elmBox.textContent = JSON.stringify(selected.elm_brief_json, null, 2);
      }
    }
    if (elmConfBadge) {
      const conf =
        selected && selected.elm_brief_json && selected.elm_brief_json.confidence !== undefined
          ? selected.elm_brief_json.confidence
          : null;
      elmConfBadge.textContent = conf !== null ? "confidence: " + conf : "confidence: -";
    }
    if (elmModeBadge) {
      elmModeBadge.textContent = "mode: " + ((selected && selected.elm_mode_used) || "none");
    }
    if (elmActionBtn) {
      const pb = selected && selected.recommended_playbook;
      elmActionBtn.classList.toggle("hidden", !pb);
      if (pb) {
        elmActionBtn.textContent = "Preview Recommended: " + pb;
        elmActionBtn.onclick = () => previewPlaybook(pb, selected.incident_id);
      }
    }

    const pbGrid = byId("playbooks-grid");
    if (pbGrid) {
      pbGrid.replaceChildren(
        ...state.playbooks.map((pb) => {
          const card = el("div", "card");
          card.appendChild(el("div", "card-title", pb.title || pb.playbook_id));
          card.appendChild(el("p", "engine-desc", pb.description || ""));
          const foot = el("div", "engine-footer");
          foot.appendChild(el("span", "engine-slug", pb.playbook_id));
          const btn = el("button", "btn btn-sm btn-primary", "Preview Dry-Run");
          btn.type = "button";
          btn.addEventListener("click", () =>
            previewPlaybook(pb.playbook_id, selected ? selected.incident_id : null)
          );
          foot.appendChild(btn);
          card.appendChild(foot);
          return card;
        })
      );
    }

    const qBody = byId("quarantine-tbody");
    if (qBody) {
      if (!state.quarantine.length) {
        const tr = document.createElement("tr");
        const td = el("td", "", "No quarantined jobs.");
        td.colSpan = 6;
        tr.appendChild(td);
        qBody.replaceChildren(tr);
      } else {
        qBody.replaceChildren(
          ...state.quarantine.map((row) => {
            const tr = document.createElement("tr");
            tr.appendChild(el("td", "mono", row.quarantine_id));
            tr.appendChild(el("td", "mono", row.queue_name + " / " + (row.func_name || row.job_fingerprint || "-")));
            tr.appendChild(el("td", "mono", row.exit_reason || row.error_class || "-"));
            tr.appendChild(el("td", "mono", fmtNum(row.failure_count != null ? row.failure_count : row.retry_count)));
            const stTd = document.createElement("td");
            stTd.appendChild(el("span", badgeClassForStatus(row.status), row.status));
            tr.appendChild(stTd);

            const actTd = el("td", "pill-group");
            const reqBtn = el("button", "btn btn-sm", "Requeue");
            reqBtn.type = "button";
            reqBtn.addEventListener("click", () =>
              quarantineAction(row.quarantine_id, "requeue")
            );
            const disBtn = el("button", "btn btn-sm btn-danger", "Dismiss");
            disBtn.type = "button";
            disBtn.addEventListener("click", () =>
              quarantineAction(row.quarantine_id, "dismiss")
            );
            actTd.appendChild(reqBtn);
            actTd.appendChild(disBtn);
            tr.appendChild(actTd);
            return tr;
          })
        );
      }
    }

    const probeBody = byId("probes-tbody");
    if (probeBody) {
      if (!state.probeRuns.length) {
        const tr = document.createElement("tr");
        const td = el("td", "", "No probe runs recorded.");
        td.colSpan = 5;
        tr.appendChild(td);
        probeBody.replaceChildren(tr);
      } else {
        probeBody.replaceChildren(
          ...state.probeRuns.map((pr) => {
            const tr = document.createElement("tr");
            const metrics = pr.metrics_json || {};
            tr.appendChild(el("td", "mono", pr.probe_name));
            const stTd = document.createElement("td");
            stTd.appendChild(el("span", badgeClassForStatus(pr.status), pr.status));
            tr.appendChild(stTd);
            tr.appendChild(el("td", "mono", fmtNum(metrics.latency_ms != null ? metrics.latency_ms : pr.latency_ms, " ms")));
            tr.appendChild(el("td", "mono", fmtDate(pr.created_at || pr.checked_at)));
            tr.appendChild(el("td", "mono", pr.error_message || JSON.stringify(metrics)));
            return tr;
          })
        );
      }
    }
  }

  async function previewPlaybook(playbookId, incidentId) {
    try {
      const payload = incidentId ? { incident_id: incidentId } : {};
      const data = await apiFetch(
        "/admin/ops/api/playbooks/" + encodeURIComponent(playbookId) + "/preview",
        { method: "POST", body: JSON.stringify(payload) }
      );
      const preview = data.preview || {};
      state.pendingPlaybookId = playbookId;
      state.pendingConfirmationToken =
        data.confirmation_token || preview.confirmation_token || "";

      const modal = byId("playbook-modal");
      const title = byId("playbook-modal-title");
      const pre = byId("playbook-modal-preview");
      const tokenEl = byId("playbook-modal-token");
      if (title) title.textContent = "Playbook Dry-Run Preview: " + playbookId;
      if (pre) pre.textContent = JSON.stringify(preview, null, 2);
      if (tokenEl) {
        tokenEl.textContent =
          "confirmation_token: " + (state.pendingConfirmationToken || "(missing)");
      }
      if (modal) modal.classList.remove("hidden");
    } catch (err) {
      showToast("Playbook preview failed: " + err.message, "err");
    }
  }

  async function confirmExecutePlaybook() {
    if (!state.pendingPlaybookId || !state.pendingConfirmationToken) {
      showToast("Missing confirmation token from dry-run preview.", "err");
      return;
    }
    const reasonInput = byId("playbook-modal-reason");
    const reason = reasonInput ? reasonInput.value.trim() : "";
    try {
      await apiFetch(
        "/admin/ops/api/playbooks/" +
          encodeURIComponent(state.pendingPlaybookId) +
          "/execute",
        {
          method: "POST",
          body: JSON.stringify({
            confirmation_token: state.pendingConfirmationToken,
            reason: reason || "admin_console_confirmed",
          }),
        }
      );
      closePlaybookModal();
      showToast("Executed playbook: " + state.pendingPlaybookId, "ok");
      await loadOps();
    } catch (err) {
      showToast("Playbook execution failed: " + err.message, "err");
    }
  }

  function closePlaybookModal() {
    const modal = byId("playbook-modal");
    if (modal) modal.classList.add("hidden");
    state.pendingPlaybookId = null;
    state.pendingConfirmationToken = null;
  }

  async function quarantineAction(qid, action) {
    try {
      await apiFetch(
        "/admin/ops/api/quarantine/" + encodeURIComponent(qid) + "/" + action,
        { method: "POST", body: "{}" }
      );
      showToast("Quarantine job " + qid + " marked " + action + ".", "ok");
      await loadOps();
    } catch (err) {
      showToast("Quarantine action failed: " + err.message, "err");
    }
  }

  /* ------------------------------------------------------------------
   * Tab 4: Tenants & MCP Onboarding
   * ------------------------------------------------------------------ */
  async function loadTenants() {
    try {
      const data = await apiFetch("/admin/ops/api/tenants");
      state.tenants = data;
      renderTenants(data);
    } catch (err) {
      showToast("Tenants load failed: " + err.message, "err");
    }
  }

  function renderTenants(data) {
    const nsSelect = byId("mint-namespace-select");
    const namespaces = data.namespaces || [];
    if (nsSelect) {
      nsSelect.replaceChildren(
        ...namespaces.map((ns) => {
          const nsId = ns.id || ns.namespace_id || "";
          const opt = document.createElement("option");
          opt.value = nsId;
          opt.textContent = ns.slug + " (" + nsId + ")";
          return opt;
        })
      );
    }

    const nsBody = byId("namespaces-tbody");
    if (nsBody) {
      if (!namespaces.length) {
        const tr = document.createElement("tr");
        const td = el("td", "", "No tenant namespaces found (excluding _system).");
        td.colSpan = 4;
        tr.appendChild(td);
        nsBody.replaceChildren(tr);
      } else {
        nsBody.replaceChildren(
          ...namespaces.map((ns) => {
            const nsId = ns.id || ns.namespace_id || "";
            const tr = document.createElement("tr");
            tr.appendChild(el("td", "mono", ns.slug));
            tr.appendChild(el("td", "mono", nsId));
            tr.appendChild(el("td", "", ns.embedding_provider || ns.display_name || ns.name || "-"));
            tr.appendChild(el("td", "mono", fmtDate(ns.created_at)));
            return tr;
          })
        );
      }
    }

    const qBody = byId("quotas-tbody");
    const quotas = data.quotas || [];
    if (qBody) {
      if (!quotas.length) {
        const tr = document.createElement("tr");
        const td = el("td", "", "No active resource quotas.");
        td.colSpan = 4;
        tr.appendChild(td);
        qBody.replaceChildren(tr);
      } else {
        qBody.replaceChildren(
          ...quotas.map((q) => {
            const tr = document.createElement("tr");
            tr.appendChild(el("td", "mono", q.namespace_id));
            tr.appendChild(el("td", "mono", q.resource_type));
            tr.appendChild(el("td", "mono", fmtNum(q.used_amount) + " / " + fmtNum(q.limit_amount)));
            tr.appendChild(el("td", "mono", fmtDate(q.updated_at)));
            return tr;
          })
        );
      }
    }

    const tokBody = byId("tokens-tbody");
    const tokens = data.tokens || [];
    if (tokBody) {
      if (!tokens.length) {
        const tr = document.createElement("tr");
        const td = el("td", "", "No active or recent MCP client tokens.");
        td.colSpan = 7;
        tr.appendChild(td);
        tokBody.replaceChildren(tr);
      } else {
        tokBody.replaceChildren(
          ...tokens.map((t) => {
            const tr = document.createElement("tr");
            tr.appendChild(el("td", "mono", t.token_prefix));
            tr.appendChild(el("td", "mono", t.namespace_id));
            tr.appendChild(el("td", "mono", t.principal_id));
            tr.appendChild(el("td", "mono", (t.scopes || []).join(", ")));
            tr.appendChild(el("td", "mono", t.tool_exposure_mode || "compact"));
            const stTd = document.createElement("td");
            stTd.appendChild(
              el("span", t.active ? "badge badge-ok" : "badge badge-crit", t.active ? "active" : "revoked")
            );
            tr.appendChild(stTd);

            const actTd = document.createElement("td");
            if (t.active) {
              const btn = el("button", "btn btn-sm btn-danger", "Revoke");
              btn.type = "button";
              btn.addEventListener("click", () => revokeClientToken(t.token_id, t.namespace_id));
              actTd.appendChild(btn);
            } else {
              actTd.appendChild(el("span", "mono", fmtDate(t.revoked_at)));
            }
            tr.appendChild(actTd);
            return tr;
          })
        );
      }
    }

    const configPre = byId("mint-config-pre");
    if (configPre && !configPre.dataset.hasMinted) {
      const sample = {
        mcpServers: {
          "nce-http": {
            url: buildMcpUrl(),
            headers: { Authorization: "Bearer <mint-token-to-populate>" },
          },
        },
      };
      configPre.textContent = JSON.stringify(sample, null, 2);
    }
  }

  async function mintClientToken(evt) {
    evt.preventDefault();
    const nsId = byId("mint-namespace-select") ? byId("mint-namespace-select").value.trim() : "";
    const principal = byId("mint-principal-input") ? byId("mint-principal-input").value.trim() : "";
    const ttlHours = byId("mint-ttl-input") ? Number(byId("mint-ttl-input").value || 24) : 24;
    const rateRpm = byId("mint-rpm-input") ? Number(byId("mint-rpm-input").value || 120) : 120;
    const mode = byId("mint-mode-select") ? byId("mint-mode-select").value : "compact";

    const scopes = [];
    document.querySelectorAll("input[name='mint-scope']:checked").forEach((cb) => {
      scopes.push(cb.value);
    });
    if (!scopes.length) {
      showToast("Select at least one token scope.", "err");
      return;
    }

    try {
      const res = await apiFetch("/admin/ops/api/tokens/mint", {
        method: "POST",
        body: JSON.stringify({
          namespace_id: nsId || undefined,
          principal_id: principal || "mcp-client",
          ttl_hours: ttlHours,
          rate_limit_rpm: rateRpm,
          tool_exposure_mode: mode,
          scopes,
        }),
      });
      const outBox = byId("mint-result-box");
      const rawTokEl = byId("mint-raw-token");
      const cfgPre = byId("mint-config-pre");
      if (rawTokEl) rawTokEl.textContent = res.token || "";
      if (cfgPre) {
        cfgPre.dataset.hasMinted = "1";
        cfgPre.textContent = res.mcp_config_json || "";
      }
      if (outBox) outBox.classList.remove("hidden");
      showToast("Scoped MCP token minted (" + mode + ").", "ok");
      await loadTenants();
    } catch (err) {
      showToast("Mint failed: " + err.message, "err");
    }
  }

  async function revokeClientToken(tokenId, namespaceId) {
    try {
      await apiFetch("/admin/ops/api/tokens/" + encodeURIComponent(tokenId) + "/revoke", {
        method: "POST",
        body: JSON.stringify({ namespace_id: namespaceId }),
      });
      showToast("Token revoked: " + tokenId, "ok");
      await loadTenants();
    } catch (err) {
      showToast("Revoke failed: " + err.message, "err");
    }
  }

  async function copyMcpConfig() {
    const pre = byId("mint-config-pre");
    if (!pre || !pre.textContent) return;
    try {
      await navigator.clipboard.writeText(pre.textContent);
      showToast("Copied MCP client JSON configuration.", "ok");
    } catch (_err) {
      showToast("Clipboard unavailable; copy directly from configuration box.", "err");
    }
  }

  /* ------------------------------------------------------------------
   * Tab 5: Plugins, Tools & Connections
   * ------------------------------------------------------------------ */
  async function loadPluginsAndTools() {
    try {
      const params = new URLSearchParams();
      if (state.pluginFilters.type !== "all") params.set("type", state.pluginFilters.type);
      if (state.pluginFilters.category !== "all") params.set("category", state.pluginFilters.category);
      if (state.pluginFilters.maturity !== "all") params.set("maturity", state.pluginFilters.maturity);
      if (state.pluginFilters.status !== "all") params.set("status", state.pluginFilters.status);
      if (state.pluginFilters.q) params.set("q", state.pluginFilters.q);

      const queryStr = params.toString();
      const [plugRes, toolRes] = await Promise.all([
        apiFetch("/admin/plugins/api" + (queryStr ? "?" + queryStr : "")),
        apiFetch("/admin/ops/api/tools").catch(() => ({ mcp_tools: [], a2a_skills: [] })),
      ]);
      state.pluginsCatalog = plugRes;
      state.coreTools = toolRes || { mcp_tools: [], a2a_skills: [] };
      renderPluginsTab();
    } catch (err) {
      showToast("Failed loading Plugins/Tools catalog: " + err.message, "err");
    }
  }

  function renderPluginsTab() {
    const cat = state.pluginsCatalog || { engines: [], facets: {} };
    const catSel = byId("plugin-category-select");
    const facets = cat.facets || {};
    const categories = facets.categories || [];
    if (catSel && categories.length) {
      const current = state.pluginFilters.category;
      const opts = [
        el("option", "", "All Categories"),
        ...categories.map((c) => {
          const o = el("option", "", c);
          o.value = c;
          return o;
        }),
      ];
      opts[0].value = "all";
      catSel.replaceChildren(...opts);
      catSel.value = current;
    }

    const summary = byId("plugins-summary-pill");
    if (summary) {
      const engines = cat.engines || [];
      const activeCount = engines.filter((e) => e.active || e.enabled).length;
      summary.textContent = engines.length + " plugins (" + activeCount + " active)";
    }

    const grid = byId("engines-grid");
    if (grid) {
      const engines = cat.engines || [];
      if (!engines.length) {
        grid.replaceChildren(el("div", "card", "No plugins match the selected filters."));
      } else {
        grid.replaceChildren(
          ...engines.map((eng) => {
            const card = el("div", "engine-card");
            const head = el("div", "engine-card-head");
            const left = document.createElement("div");
            left.appendChild(el("h4", "engine-name", eng.name || eng.slug));
            left.appendChild(el("div", "engine-slug", eng.slug + " · " + (eng.type || "engine")));
            head.appendChild(left);

            const pills = el("div", "pill-group");
            const mat = String(eng.maturity || "stable").toLowerCase();
            if (mat === "beta") {
              pills.appendChild(el("span", "badge badge-beta badge-maturity-beta", "BETA"));
            } else if (mat === "experimental") {
              pills.appendChild(el("span", "badge badge-maturity-experimental", "EXPERIMENTAL"));
            } else {
              pills.appendChild(el("span", "badge badge-maturity-stable", "STABLE"));
            }
            const isRuntimeDisabled = Boolean(eng.runtime_disabled);
            pills.appendChild(
              el(
                "span",
                isRuntimeDisabled ? "badge badge-crit" : "badge badge-ok",
                isRuntimeDisabled ? "DISABLED" : "ENABLED"
              )
            );
            head.appendChild(pills);
            card.appendChild(head);

            card.appendChild(el("p", "engine-desc", eng.description || ""));

            const foot = el("div", "engine-footer");
            const connBtn = el("button", "btn btn-sm", "Connections");
            connBtn.type = "button";
            connBtn.addEventListener("click", () => openConnectionsModal(eng.slug));
            foot.appendChild(connBtn);

            const toggleBtn = el(
              "button",
              isRuntimeDisabled ? "btn btn-sm btn-primary" : "btn btn-sm btn-danger",
              isRuntimeDisabled ? "Enable" : "Disable"
            );
            toggleBtn.type = "button";
            toggleBtn.addEventListener("click", () => toggleEngine(eng.slug, isRuntimeDisabled));
            foot.appendChild(toggleBtn);

            card.appendChild(foot);
            return card;
          })
        );
      }
    }

    const toolsBody = byId("core-tools-tbody");
    if (toolsBody) {
      const combined = []
        .concat((state.coreTools.mcp_tools || []).map((t) => Object.assign({ kind: "mcp" }, t)))
        .concat((state.coreTools.a2a_skills || []).map((t) => Object.assign({ kind: "a2a" }, t)));
      if (!combined.length) {
        const tr = document.createElement("tr");
        const td = el("td", "", "No core MCP/A2A tool records available.");
        td.colSpan = 5;
        tr.appendChild(td);
        toolsBody.replaceChildren(tr);
      } else {
        toolsBody.replaceChildren(
          ...combined.map((tool) => {
            const tr = document.createElement("tr");
            tr.appendChild(el("td", "mono", tool.name));
            tr.appendChild(el("td", "mono", String(tool.kind).toUpperCase()));
            tr.appendChild(el("td", "", tool.impact || "-"));
            const stTd = document.createElement("td");
            stTd.appendChild(
              el("span", tool.enabled ? "badge badge-ok" : "badge badge-crit", tool.enabled ? "enabled" : "disabled")
            );
            tr.appendChild(stTd);
            const actTd = document.createElement("td");
            const btn = el(
              "button",
              tool.enabled ? "btn btn-sm btn-danger" : "btn btn-sm btn-primary",
              tool.enabled ? "Disable" : "Enable"
            );
            btn.type = "button";
            btn.addEventListener("click", () =>
              toggleCoreTool(tool.name, tool.kind, !tool.enabled)
            );
            actTd.appendChild(btn);
            tr.appendChild(actTd);
            return tr;
          })
        );
      }
    }
  }

  async function toggleEngine(slug, enable) {
    try {
      await apiFetch("/admin/plugins/api/toggle", {
        method: "POST",
        body: JSON.stringify({ slug, enabled: Boolean(enable) }),
      });
      showToast("Plugin " + slug + (enable ? " enabled" : " disabled") + " (PG + Redis).", "ok");
      await loadPluginsAndTools();
    } catch (err) {
      showToast("Plugin toggle failed: " + err.message, "err");
    }
  }

  async function toggleCoreTool(toolName, toolType, enable) {
    try {
      await apiFetch("/admin/ops/api/tools/toggle", {
        method: "POST",
        body: JSON.stringify({
          tool_name: toolName,
          tool_type: toolType,
          enabled: Boolean(enable),
        }),
      });
      showToast("Tool " + toolName + (enable ? " enabled" : " disabled") + " (PG + Redis).", "ok");
      await loadPluginsAndTools();
    } catch (err) {
      showToast("Tool toggle failed: " + err.message, "err");
    }
  }

  async function openConnectionsModal(slug) {
    state.activeVaultSlug = slug || "";
    const modal = byId("connections-modal");
    const slugEl = byId("connections-modal-slug");
    const listBox = byId("connections-list");
    if (slugEl) slugEl.textContent = "Plugin / Connector: " + (slug || "all");
    if (listBox) listBox.replaceChildren(el("div", "metric-sub", "Loading masked connections..."));
    if (modal) modal.classList.remove("hidden");

    try {
      const endpoint =
        (modal && modal.getAttribute("data-connections-endpoint")) ||
        "/admin/plugins/api/connections";
      const data = await apiFetch(
        endpoint + (slug ? "?plugin_slug=" + encodeURIComponent(slug) : "")
      );
      const conns = data.connections || [];
      if (listBox) {
        if (!conns.length) {
          listBox.replaceChildren(el("div", "metric-sub", "No configured TenantConnectionVault entries."));
        } else {
          listBox.replaceChildren(
            ...conns.map((c) => {
              const row = el("div", "kv-row");
              row.appendChild(
                el(
                  "span",
                  "kv-key",
                  (c.plugin_slug || "-") + " [" + (c.namespace_id || "default") + "]"
                )
              );
              const right = el("div", "pill-group");
              right.appendChild(el("span", "mono", c.credential_mask || "********"));
              const delBtn = el("button", "btn btn-sm btn-danger", "Delete");
              delBtn.type = "button";
              delBtn.addEventListener("click", () => deleteVaultConnection(c));
              right.appendChild(delBtn);
              row.appendChild(right);
              return row;
            })
          );
        }
      }
    } catch (err) {
      if (listBox) listBox.replaceChildren(el("div", "metric-sub", "Error: " + err.message));
    }
  }

  async function saveVaultConnection(evt) {
    evt.preventDefault();
    const modal = byId("connections-modal");
    const endpoint =
      (modal && modal.getAttribute("data-connections-endpoint")) ||
      "/admin/plugins/api/connections";
    const nsId = byId("conn-ns-input") ? byId("conn-ns-input").value.trim() : "";
    const credsRaw = byId("conn-creds-input") ? byId("conn-creds-input").value.trim() : "{}";
    let parsed = {};
    try {
      parsed = JSON.parse(credsRaw || "{}");
    } catch (_err) {
      showToast("Credentials must be valid JSON object.", "err");
      return;
    }
    try {
      await apiFetch(endpoint, {
        method: "POST",
        body: JSON.stringify({
          plugin_slug: state.activeVaultSlug || "custom",
          namespace_id: nsId || "default",
          credentials: parsed,
        }),
      });
      showToast("Saved connection to TenantConnectionVault.", "ok");
      if (byId("conn-creds-input")) byId("conn-creds-input").value = "";
      await openConnectionsModal(state.activeVaultSlug);
    } catch (err) {
      showToast("Save connection failed: " + err.message, "err");
    }
  }

  async function deleteVaultConnection(conn) {
    const modal = byId("connections-modal");
    const endpoint =
      (modal && modal.getAttribute("data-connections-endpoint")) ||
      "/admin/plugins/api/connections";
    try {
      await apiFetch(endpoint, {
        method: "DELETE",
        body: JSON.stringify({
          plugin_slug: conn.plugin_slug,
          namespace_id: conn.namespace_id,
        }),
      });
      showToast("Removed connection for " + conn.plugin_slug, "ok");
      await openConnectionsModal(state.activeVaultSlug);
    } catch (err) {
      showToast("Delete connection failed: " + err.message, "err");
    }
  }

  function closeConnectionsModal() {
    const modal = byId("connections-modal");
    if (modal) modal.classList.add("hidden");
  }

  /* ------------------------------------------------------------------
   * Event Wiring
   * ------------------------------------------------------------------ */
  function bindEvents() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
    });

    const refreshBtn = byId("btn-refresh-active");
    if (refreshBtn) refreshBtn.addEventListener("click", () => switchTab(state.activeTab, true));

    const logoutBtn = byId("btn-logout");
    if (logoutBtn) logoutBtn.addEventListener("click", performLogout);

    const loginForm = byId("login-form");
    if (loginForm) loginForm.addEventListener("submit", submitLogin);

    const backupTriggerBtn = byId("btn-trigger-backup");
    if (backupTriggerBtn) backupTriggerBtn.addEventListener("click", triggerWatermarkBackup);

    const pbConfirmBtn = byId("playbook-modal-confirm");
    if (pbConfirmBtn) pbConfirmBtn.addEventListener("click", confirmExecutePlaybook);

    const pbCancelBtn = byId("playbook-modal-cancel");
    if (pbCancelBtn) pbCancelBtn.addEventListener("click", closePlaybookModal);

    const mintForm = byId("mint-token-form");
    if (mintForm) mintForm.addEventListener("submit", mintClientToken);

    const copyBtn = byId("btn-copy-mcp-config");
    if (copyBtn) copyBtn.addEventListener("click", copyMcpConfig);

    document.querySelectorAll("#type-segmented .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#type-segmented .seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.pluginFilters.type = btn.getAttribute("data-type") || "all";
        loadPluginsAndTools();
      });
    });

    document.querySelectorAll("#maturity-segmented .seg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll("#maturity-segmented .seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.pluginFilters.maturity = btn.getAttribute("data-maturity") || "all";
        loadPluginsAndTools();
      });
    });

    const catSel = byId("plugin-category-select");
    if (catSel) {
      catSel.addEventListener("change", () => {
        state.pluginFilters.category = catSel.value || "all";
        loadPluginsAndTools();
      });
    }

    const stSel = byId("plugin-status-select");
    if (stSel) {
      stSel.addEventListener("change", () => {
        state.pluginFilters.status = stSel.value || "all";
        loadPluginsAndTools();
      });
    }

    const searchInput = byId("plugin-search-input");
    if (searchInput) {
      searchInput.addEventListener("input", () => {
        state.pluginFilters.q = searchInput.value.trim();
        loadPluginsAndTools();
      });
    }

    const connForm = byId("connections-form");
    if (connForm) connForm.addEventListener("submit", saveVaultConnection);

    const connClose = byId("connections-modal-close");
    if (connClose) connClose.addEventListener("click", closeConnectionsModal);
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    checkSessionAndBoot();
  });
})();
