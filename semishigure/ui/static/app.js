/* Semishigure UI — Vue 3 application (docs/ui-ux-proposal.md). */
(function () {
  "use strict";
  const { createApp, ref, computed, onMounted, watch, nextTick } = Vue;

  const ICONS = {
    check: '<path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
    "x-circle": '<circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
    warning: '<path d="M8 1.8 15 14H1z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M8 6v4M8 11.6v.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
    play: '<path d="M4 2.5v11l9-5.5z" fill="currentColor"/>',
    stop: '<rect x="3" y="3" width="10" height="10" rx="1.5" fill="currentColor"/>',
    sun: '<circle cx="8" cy="8" r="3" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2M3 3l1.4 1.4M11.6 11.6 13 13M3 13l1.4-1.4M11.6 4.4 13 3" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>',
    moon: '<path d="M13.5 10.2A6 6 0 0 1 5.8 2.5a6 6 0 1 0 7.7 7.7z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>',
    auto: '<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor"/>',
  };
  const Ico = { props: { name: String }, template: '<svg class="ic" viewBox="0 0 16 16" aria-hidden="true" v-html="path"></svg>', computed: { path() { return ICONS[this.name] || ""; } } };

  const pad = (v) => String(v).padStart(2, "0");
  const mmss = (v) => { if (v == null || isNaN(v)) return "–"; const s = Math.max(0, Math.round(v)); return Math.floor(s / 60) + ":" + pad(s % 60); };
  const hhmmss = (t) => { if (!t) return "–"; const d = new Date(t * 1000); return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); };
  const ts = (t) => { if (!t) return "–"; const d = new Date(t * 1000); return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()); };
  const fmt = (v, digits) => (v === null || v === undefined || v === "" || (typeof v === "number" && isNaN(v))) ? "–" : (typeof v === "number" && digits !== undefined ? v.toFixed(digits) : v);
  const n = (v) => (v === null || v === undefined) ? "–" : (typeof v === "number" ? v.toLocaleString("ja-JP") : v);
  const ago = (t) => { if (!t) return "–"; const s = Math.max(0, Math.round(Date.now() / 1000 - t)); return s < 60 ? s + " 秒前" : mmss(s) + " 前"; };
  const LEVEL_RE = /\[(EMERG|ALERT|CRIT|ERR|WARNING|NOTICE|INFO|DEBUG)\]|(ERROR|WARNING|NOTICE|VERBOSE|DEBUG)\[\d+\]/;
  const levelOf = (line) => { const m = LEVEL_RE.exec(line || ""); const l = m ? (m[1] || m[2]) : ""; return l === "ERROR" ? "ERR" : l; };
  const TABS = [{ id: "run", label: "実行" }, { id: "scenarios", label: "シナリオ" }, { id: "profiles", label: "PBX" }, { id: "results", label: "結果" }];
  const NUMERIC_FIELDS = ["sip_port", "sip_tls_port", "ssh_port", "esl_port", "rtp_port_start", "rtp_port_end", "max_concurrency"];
  const PROFILE_GROUPS = [
    { title: "基本", fields: ["name", "type", "host", "domain", "environment", "max_concurrency", "notes"] },
    { title: "SIP / RTP", fields: ["sip_port", "sip_transport", "sip_tls_port", "tls_verify", "tls_ca", "rtp_port_start", "rtp_port_end"] },
    { title: "実行方法", fields: ["executor"] },
    { title: "SSH（executor が ssh のとき）", fields: ["ssh_host", "ssh_port", "ssh_user", "ssh_key", "ssh_key_passphrase_ref", "ssh_known_hosts", "ssh_strict_host_key"], when: (v) => v.executor === "ssh" },
    { title: "ESL / AMI と監視", fields: ["esl_host", "esl_port", "esl_password_ref", "fs_cli", "log_path", "process_name", "conf_dir"] },
    { title: "その他", fields: ["extra"] },
  ];
  const PROFILE_HELP = {
    name: "一意の名前。シナリオの pbx_profile やコマンドの --pbx-profile で参照します",
    host: "SIP の宛先（internal プロファイルの bind アドレス）",
    domain: "SIP ドメイン（REGISTER / INVITE の To ドメイン）",
    environment: "prod にすると開始前の確認と上限 20 が掛かります",
    max_concurrency: "このプロファイル固有の上限 N（空なら設定なし）",
    ssh_key_passphrase_ref: "鍵にパスフレーズがあるときだけ。secret:NAME の形で書きます（値は書かない）",
    esl_host: "PBX ホストから見たアドレス。SSH のときはポートフォワードします",
    esl_password_ref: "secret:NAME の形。値は環境変数 SEMISHIGURE_SECRET_NAME か semishigure secret set で渡します",
    fs_cli: "FreeSWITCH は fs_cli、Asterisk は asterisk（Docker なら docker exec ... を含める）",
    log_path: "PBX のログ。レベル別の件数と log_patterns プラグインが読みます",
    extra: "JSON。Asterisk の ami_user、asterisk_conf など",
  };

  createApp({
    components: { ico: Ico },
    setup() {
      // ---- state ----
      const connected = ref(false), lastUpdate = ref(""), lastAt = ref(0), nowTick = ref(Date.now());
      const scenarios = ref([]), runs = ref([]), run = ref(null), alert = ref(null), toast = ref(null), busy = ref(""), pending = ref("");
      const form = ref({ scenario: "", pbx_profile: "", monitor: true, mode: "fixed", target: 1, schedule: "", preset: "", max_concurrency: 50, ramp_rate: 0.5, call_duration: 180, confirm_prod: false, ignore_register_failure: false, name: "" });
      const fieldErrors = ref({});
      const profiles = ref([]), profilesPath = ref(""), profileFields = ref([]), profileDefaults = ref({}), profileTest = ref(null);
      const selectedRuns = ref([]), runsQuery = ref(""), runsSort = ref({ key: "id", dir: "desc" });
      const tab = ref("run"), scenarioDir = ref("");
      const precheckState = ref(null);
      const detail = ref(null), detailRows = ref([]), compare = ref(null);
      const editor = ref({ file: "", text: "", parsed: null, newName: "", msg: "", bad: false });
      const pform = ref({ name: "", values: {}, msg: "", bad: false });
      const rate = ref({ ramp_rate: 0.5, call_duration: 180 });
      const burstTarget = ref(10), scheduleText = ref("");
      const callsFilter = ref("all"), callsSort = ref({ key: "", dir: "desc" }), logWarnOnly = ref(false);
      const theme = ref("system");
      const reducedMotion = ref(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
      const updatePaused = ref(false), updateInterval = ref(reducedMotion.value ? 2 : 0.5);
      const dialog = ref(null);
      const chartEl = ref(null), mchartEl = ref(null), dchartEl = ref(null), cchartEl = ref(null), dlgEl = ref(null), dlgCancel = ref(null), yamlEl = ref(null), prodBox = ref(null), detailEl = ref(null);
      let ws = null, rateSynced = false, chart = null, mchart = null, dchart = null, cchart = null, lastApplied = 0, pendingSnap = null, dialogResolve = null, targetTimer = null, toastTimer = null;

      setInterval(() => { nowTick.value = Date.now(); }, 1000);

      // ---- theme ----
      try { theme.value = localStorage.getItem("semishigure.theme") || "system"; } catch (e) { /* private mode */ }
      const applyTheme = () => { if (theme.value === "light" || theme.value === "dark") document.documentElement.dataset.theme = theme.value; else delete document.documentElement.dataset.theme; try { localStorage.setItem("semishigure.theme", theme.value); } catch (e) { /* ignore */ } nextTick(rebuildCharts); };
      const themeLabel = computed(() => ({ system: "システム設定", light: "ライト", dark: "ダーク" }[theme.value]));
      function cycleTheme() { theme.value = { system: "light", light: "dark", dark: "system" }[theme.value]; applyTheme(); }
      if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => nextTick(rebuildCharts));

      // ---- navigation (hash) ----
      function setTab(t) { tab.value = t; const h = t === "results" && detail.value ? "#results/" + detail.value.id : "#" + t; if (location.hash !== h) history.replaceState(null, "", h); }
      function readHash() { const m = /^#(run|scenarios|profiles|results)(?:\/(\d+))?/.exec(location.hash || ""); if (!m) return; tab.value = m[1]; if (m[2]) openRun(Number(m[2]), true); }
      window.addEventListener("hashchange", readHash);

      // ---- helpers ----
      const ctl = computed(() => (run.value ? run.value.controller : {}));
      const runActive = computed(() => !!(run.value && !run.value.finished));
      const runFinished = computed(() => !!(run.value && run.value.finished));
      const precheckRunning = computed(() => !!(precheckState.value && precheckState.value.running));
      const precheckResult = computed(() => (precheckState.value && precheckState.value.result) || null);
      const precheckNg = computed(() => (precheckResult.value ? precheckResult.value.items.filter((i) => !i.ok).length : 0));
      const precheckProgress = computed(() => { const p = precheckState.value; if (!p || !p.running) return 0; return (p.elapsed_s || 0) / ((p.hold_seconds || 6) + 4) * 100; });
      const selectedScenario = computed(() => scenarios.value.find((s) => s.file === form.value.scenario));
      const presets = computed(() => (selectedScenario.value ? selectedScenario.value.presets || [] : []));
      const runPresets = computed(() => { const s = run.value && scenarios.value.find((x) => x.name === run.value.scenario); return s ? s.presets || [] : []; });
      const activeProfile = computed(() => { const name = form.value.pbx_profile || (selectedScenario.value && selectedScenario.value.pbx_profile); return profiles.value.find((x) => x.name === name) || null; });
      const activeEnv = computed(() => (activeProfile.value ? activeProfile.value.environment : (selectedScenario.value && selectedScenario.value.pbx ? selectedScenario.value.pbx.environment : "")));
      const effectiveCap = computed(() => { let cap = Math.min(Number(form.value.max_concurrency) || 50, 50); if (activeEnv.value === "prod") cap = Math.min(cap, 20); if (activeProfile.value && activeProfile.value.max_concurrency) cap = Math.min(cap, activeProfile.value.max_concurrency); return cap; });
      const resolved = computed(() => {
        const s = selectedScenario.value, p = activeProfile.value; const parsed = s && s.parsed ? s.parsed : null;
        return {
          host: p ? p.host : (s && s.pbx ? s.pbx.host : "-"), domain: p && p.domain ? p.domain : (s && s.pbx ? s.pbx.domain : ""), env: activeEnv.value,
          profileName: p ? p.name : "", executor: p ? p.executor : "",
          caller: parsed && parsed.caller ? parsed.caller.auth_user + " → " + parsed.caller.destination : (s ? s.name : "-"),
          answerers: parsed && parsed.answerer ? (parsed.answerer.extensions || []).map((e) => e.user + "(" + e.max_calls + ")").join(", ") : "",
          plugins: s && s.plugins ? s.plugins.join(", ") : "",
          secrets: parsed ? secretsOf(parsed).concat(p && p.esl_password_ref && p.esl_password_ref.startsWith("secret:") ? [p.esl_password_ref.slice(7)] : []) : [],
        };
      });
      function secretsOf(obj, out) { out = out || []; if (!obj || typeof obj !== "object") return out; for (const v of Object.values(obj)) { if (typeof v === "string" && v.startsWith("secret:")) { if (!out.includes(v.slice(7))) out.push(v.slice(7)); } else if (v && typeof v === "object") secretsOf(v, out); } return out; }
      const modeText = computed(() => form.value.mode === "schedule" ? "段階 " + (form.value.schedule || "(未入力)") : form.value.mode === "preset" ? "プリセット " + (form.value.preset || "") : "固定 N=" + form.value.target);
      const stale = computed(() => !connected.value && !!run.value && lastAt.value > 0);
      const staleFor = computed(() => { nowTick.value; return mmss((Date.now() - lastAt.value) / 1000); });
      const backoffText = computed(() => { const r = ctl.value.backoff_reason || ""; const m = /(\d+) consecutive (\d+)/.exec(r); const t = /target lowered to (\d+)/.exec(r); return m ? `PBX が ${m[2]} を ${m[1]} 回連続で返したため、目標を ${t ? t[1] : ctl.value.target} に下げました（自動減少）。応答内線の空きと PBX 側の制限を確認してください。` : "自動減少: " + r; });
      const extList = computed(() => (run.value ? Object.entries(run.value.registrations || {}).map(([user, r]) => ({ user, ...r })) : []));
      const eventsDesc = computed(() => (run.value ? [...run.value.events].reverse() : []));
      const kindOf = (e) => e.kind || (/backoff|lowered|consecutive/.test(e.message) ? "backoff" : /schedule|step/.test(e.message) ? "schedule" : /monitor/.test(e.message) ? "monitor" : /plugin/.test(e.message) ? "plugins" : "controller");
      const busyRejects = computed(() => (run.value ? run.value.stats.answerer_busy_rejects || 0 : 0));
      const failText = computed(() => { const f = run.value ? run.value.stats.fail_by_status || {} : {}; const s = Object.entries(f).map(([k, v]) => k + " × " + v).join(" · "); return s || "失敗なし"; });
      const answeredText = computed(() => { const a = run.value ? run.value.stats.answered_by_ext || {} : {}; return Object.entries(a).map(([k, v]) => k + ":" + v).join(" ") || "応答なし"; });
      const nextStep = computed(() => { const c = ctl.value; if (!c.schedule_step || !c.schedule) return null; return c.schedule[c.schedule_step.index + 1] || null; });
      const mon = computed(() => (run.value && run.value.monitor) ? run.value.monitor : { last: {}, log_level_counts: {}, top_threads: [], threads_by_name: {}, log_tail: [], series: [], esl_events: {}, hangup_causes: {} });
      const monAge = computed(() => { nowTick.value; return mon.value.last && mon.value.last.wall ? ago(mon.value.last.wall) : "–"; });
      const monStale = computed(() => { nowTick.value; const m = mon.value; if (!m.last || !m.last.wall || !runActive.value) return false; return Date.now() / 1000 - m.last.wall > 3 * (m.interval || 2) + 2; });
      const processLabel = computed(() => (run.value && run.value.pbx_profile ? (run.value.pbx_profile.process_name || run.value.pbx_profile.type) : "PBX"));
      const customMetrics = computed(() => Object.fromEntries(Object.entries((mon.value.last && mon.value.last.custom) || {}).filter(([k]) => !k.includes("."))));
      const logView = computed(() => { const lines = [...(mon.value.log_tail || [])].reverse(); return logWarnOnly.value ? lines.filter((l) => ["WARNING", "ERR", "CRIT", "ALERT", "EMERG"].includes(levelOf(l))) : lines; });
      const seriesTruncated = computed(() => !!(run.value && run.value.series.length >= 900 && run.value.series[0].t > 1));
      const chartSummary = computed(() => { if (!run.value) return ""; const s = run.value.series; if (!s.length) return "同時数の推移。まだサンプルがありません。"; const em = Math.max(...s.map((p) => p.established)), lm = Math.max(...s.map((p) => p.rtp_late_max_ms || 0)); return `同時数の推移。${mmss(s[0].t)} から ${mmss(s[s.length - 1].t)}。目標 ${ctl.value.target}、確立数の最大 ${em}、RTP 送出遅れの最大 ${lm.toFixed(2)} ms。`; });
      const detailSummary = computed(() => { const d = detail.value; if (!d || !d.samples) return ""; const s = d.samples.filter((p) => p.kind !== "monitor"); if (!s.length) return "サンプルがありません"; return `ラン ${d.id} の同時数の推移。確立数の最大 ${Math.max(...s.map((p) => p.established))}、RTP 送出遅れの最大 ${Math.max(...s.map((p) => p.rtp_late_max_ms || 0)).toFixed(2)} ms。`; });
      const srSummaryRaw = computed(() => (runActive.value ? `目標 ${ctl.value.target}、確立 ${ctl.value.established}、接続中 ${ctl.value.pending}、失敗 ${run.value.stats.calls_failed}` : ""));
      const srSummary = ref("");
      setInterval(() => { srSummary.value = srSummaryRaw.value; }, 10000);
      const flatten = (obj, prefix = "") => { const out = {}; for (const [k, v] of Object.entries(obj || {})) { if (k === "class" || k === "output" || k === "errors") continue; if (v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).length && Object.values(v).every((x) => x && typeof x === "object")) Object.assign(out, flatten(v, prefix + k + ".")); else out[prefix + k] = v; } return out; };
      const stateLabel = (c) => ({ ESTABLISHED: "確立", RINGING: "呼出中", INVITING: "発信中", AUTH: "認証中", IDLE: "待機", TERMINATING: "切断中", DONE: c.status && c.status >= 300 ? "失敗" : "終了", FAILED: "失敗" }[c.state] || c.state);
      const sortState = (s, key) => (s.key === key ? (s.dir === "asc" ? "ascending" : "descending") : "none");
      const sortBy = (list, key, dir, getter) => { const g = getter || ((x) => x[key]); return [...list].sort((a, b) => { const va = g(a), vb = g(b); if (va == null && vb == null) return 0; if (va == null) return 1; if (vb == null) return -1; return (va < vb ? -1 : va > vb ? 1 : 0) * (dir === "asc" ? 1 : -1); }); };
      const callsView = computed(() => { if (!run.value) return []; let l = run.value.calls; if (callsFilter.value === "active") l = l.filter((c) => c.state !== "DONE"); if (callsFilter.value === "failed") l = l.filter((c) => (c.status && c.status >= 300) || c.state === "FAILED"); if (callsSort.value.key) l = sortBy(l, callsSort.value.key, callsSort.value.dir); return l; });
      function sortCalls(key) { callsSort.value = callsSort.value.key === key && callsSort.value.dir === "desc" ? { key, dir: "asc" } : { key, dir: "desc" }; }
      const runsView = computed(() => { const q = runsQuery.value.trim().toLowerCase(); let l = runs.value; if (q) l = l.filter((r) => (r.name || "").toLowerCase().includes(q) || (r.scenario_name || "").toLowerCase().includes(q)); const k = runsSort.value.key; const get = k === "p95" ? (r) => (r.summary ? r.summary.invite_to_200_ms.p95 : null) : k === "late" ? (r) => (r.summary ? r.summary.rtp.late_max_ms : null) : (r) => r[k]; return sortBy(l, k, runsSort.value.dir, get); });
      function sortRuns(key) { runsSort.value = runsSort.value.key === key && runsSort.value.dir === "desc" ? { key, dir: "asc" } : { key, dir: "desc" }; }
      const recentRuns = computed(() => runs.value.slice(0, 5));
      const profileGroups = computed(() => { const known = new Set(PROFILE_GROUPS.flatMap((g) => g.fields)); const rest = profileFields.value.filter((f) => !known.has(f)); const groups = PROFILE_GROUPS.map((g) => ({ ...g, fields: g.fields.filter((f) => profileFields.value.includes(f)) })); if (rest.length) groups.push({ title: "追加項目", fields: rest }); return groups.filter((g) => g.fields.length); });

      // ---- feedback ----
      function showToast(text, kind) { toast.value = { text, kind: kind || "ok" }; clearTimeout(toastTimer); toastTimer = setTimeout(() => { toast.value = null; }, 4000); }
      function showAlert(text, kind) { alert.value = { text, kind: kind || "bad" }; }
      function confirmDialog(opts) { dialog.value = Object.assign({ confirmLabel: "実行する", danger: false, lines: [], prod: false }, opts); return new Promise((resolve) => { dialogResolve = resolve; nextTick(() => { if (dlgEl.value && !dlgEl.value.open) dlgEl.value.showModal(); if (dlgCancel.value) dlgCancel.value.focus(); }); }); }
      function dialogAnswer(ok) { if (dlgEl.value && dlgEl.value.open) dlgEl.value.close(); const r = dialogResolve; dialogResolve = null; dialog.value = null; if (r) r(ok); }

      // ---- API ----
      async function api(path, body, opts) {
        opts = opts || {};
        const r = await fetch(path, { method: opts.method || (body === undefined ? "GET" : "POST"), headers: { "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) { const err = new Error(j.detail || r.statusText); err.status = r.status; if (!opts.quiet) showAlert(japanese(err)); throw err; }
        return j;
      }
      function japanese(err) {
        const m = String(err.message || err);
        if (err.status === 409 && /run is already/.test(m)) return "すでにランが進行中です。";
        if (err.status === 409 && /precheck/.test(m)) return "事前チェックが進行中です。終わるまで待ってください。";
        if (err.status === 409 && /exists/.test(m)) return "同じ名前のシナリオがあります: " + m;
        if (err.status === 428) return "本番環境（prod）への負荷には確認が必要です。フォームのチェックを入れてください。";
        if (/secret/i.test(m)) return "秘密情報を解決できません: " + m + "。環境変数 SEMISHIGURE_SECRET_NAME か semishigure secret set で値を渡してください。";
        if (err.status === 502) return "PBX に接続できません: " + m;
        return m;
      }
      async function post(kind, path, body) { pending.value = kind; try { return await api(path, body || {}); } catch (e) { return null; } finally { pending.value = ""; } }

      // ---- loaders ----
      async function loadScenarios() {
        scenarios.value = await api("/api/scenarios").catch(() => []);
        for (const s of scenarios.value) { if (!s.error && !s.parsed) { try { s.parsed = (await api("/api/scenarios/" + s.file, undefined, { quiet: true })).parsed; } catch (e) { /* ignore */ } } }
        if (!form.value.scenario && scenarios.value.length) form.value.scenario = scenarios.value[0].file;
        if (!editor.value.file && scenarios.value.length) { editor.value.file = scenarios.value[0].file; await loadScenarioText(); }
        const st = await api("/api/state", undefined, { quiet: true }).catch(() => ({})); scenarioDir.value = st.scenario_dir || ""; if (st.precheck) precheckState.value = st.precheck;
      }
      async function loadProfiles() { const r = await api("/api/pbx/profiles", undefined, { quiet: true }).catch(() => ({ profiles: [], path: "", fields: [], defaults: {} })); profiles.value = r.profiles; profilesPath.value = r.path; profileFields.value = r.fields || []; profileDefaults.value = r.defaults || {}; if (!Object.keys(pform.value.values).length) loadProfileForm(); }
      async function loadRuns() { runs.value = await api("/api/runs", undefined, { quiet: true }).catch(() => []); }

      // ---- scenarios ----
      async function loadScenarioText() { if (!editor.value.file) return; try { const r = await api("/api/scenarios/" + editor.value.file); editor.value.text = r.yaml; editor.value.parsed = r.parsed; editor.value.msg = ""; editor.value.bad = false; } catch (e) { /* alert shown */ } }
      async function saveScenario() {
        editor.value.msg = ""; busy.value = "scenario";
        try { const j = await api("/api/scenarios/" + editor.value.file, { yaml: editor.value.text }, { method: "PUT", quiet: true }); editor.value.msg = "保存しました: " + j.file; editor.value.bad = false; showToast("シナリオを保存しました"); await loadScenarios(); await loadScenarioText(); }
        catch (e) { editor.value.msg = String(e.message || e).replace(/^invalid scenario: /, "シナリオとして解釈できません。\n"); editor.value.bad = true; nextTick(() => yamlEl.value && yamlEl.value.focus()); }
        finally { busy.value = ""; }
      }
      async function createScenario() {
        const name = editor.value.newName.trim(); if (!name) return; busy.value = "scenario";
        try { const j = await api("/api/scenarios", { name, template: editor.value.file || null }, { quiet: true }); await loadScenarios(); editor.value.file = j.file; editor.value.newName = ""; await loadScenarioText(); showToast(j.file + " を作成しました"); }
        catch (e) { editor.value.msg = japanese(e); editor.value.bad = true; }
        finally { busy.value = ""; }
      }
      async function deleteScenario() {
        if (!editor.value.file) return;
        if (!(await confirmDialog({ title: "シナリオを削除しますか？", lines: [editor.value.file + " をディスクから削除します。元に戻せません。"], confirmLabel: "削除する", danger: true }))) return;
        try { await api("/api/scenarios/" + editor.value.file, undefined, { method: "DELETE" }); showToast("削除しました"); editor.value.file = ""; editor.value.text = ""; editor.value.parsed = null; await loadScenarios(); } catch (e) { /* alert shown */ }
      }

      // ---- profiles ----
      function loadProfileForm() {
        const p = profiles.value.find((x) => x.name === pform.value.name);
        const vals = {};
        for (const f of profileFields.value) { let v = p ? p[f] : profileDefaults.value[f]; if (v === undefined) v = ""; if (f === "extra") v = JSON.stringify(v || {}); if (v === null) v = ""; vals[f] = v; }
        pform.value.values = vals; pform.value.msg = ""; pform.value.bad = false; fieldErrors.value.pname = "";
      }
      async function saveProfile() {
        const vals = { ...pform.value.values }; fieldErrors.value.pname = "";
        try { vals.extra = vals.extra ? JSON.parse(vals.extra) : {}; } catch (e) { pform.value.msg = "extra は JSON で書いてください: " + e.message; pform.value.bad = true; return; }
        for (const k of NUMERIC_FIELDS) { if (vals[k] !== "" && vals[k] !== null && vals[k] !== undefined) vals[k] = Number(vals[k]); else if (k === "max_concurrency") vals[k] = null; else delete vals[k]; }
        const name = String(vals.name || "").trim(); if (!name) { fieldErrors.value.pname = "name は必須です"; pform.value.msg = "name を入力してください"; pform.value.bad = true; nextTick(() => { const el = document.getElementById("pf-name"); if (el) el.focus(); }); return; }
        busy.value = "profile";
        try { await api("/api/pbx/profiles/" + encodeURIComponent(name), { profile: vals }, { method: "PUT", quiet: true }); pform.value.msg = "保存しました: " + name; pform.value.bad = false; showToast("プロファイルを保存しました"); await loadProfiles(); pform.value.name = name; }
        catch (e) { pform.value.msg = japanese(e); pform.value.bad = true; }
        finally { busy.value = ""; }
      }
      async function deleteProfile() {
        if (!pform.value.name) return;
        if (!(await confirmDialog({ title: "プロファイルを削除しますか？", lines: [pform.value.name + " を " + profilesPath.value + " から削除します。"], confirmLabel: "削除する", danger: true }))) return;
        try { await api("/api/pbx/profiles/" + encodeURIComponent(pform.value.name), undefined, { method: "DELETE" }); showToast("削除しました"); pform.value.name = ""; await loadProfiles(); loadProfileForm(); } catch (e) { /* alert shown */ }
      }
      async function testProfile(name) { if (!name) return; busy.value = "test"; profileTest.value = null; try { profileTest.value = { name, ...(await api("/api/pbx/profiles/" + encodeURIComponent(name) + "/test", {})) }; } catch (e) { /* alert shown */ } finally { busy.value = ""; } }

      // ---- run control ----
      function startBody() { const f = form.value; const body = { scenario: f.scenario, pbx_profile: f.pbx_profile || null, monitor: f.monitor, ramp_rate: f.ramp_rate, call_duration: f.call_duration, max_concurrency: f.max_concurrency, confirm_prod: f.confirm_prod, ignore_register_failure: f.ignore_register_failure, name: f.name || "" }; if (f.mode === "schedule") { body.schedule = f.schedule; body.target = 0; } else if (f.mode === "preset") { body.preset = f.preset || presets.value[0]; body.target = 0; } else body.target = f.target; return body; }
      async function startRun() {
        fieldErrors.value = {}; if (!form.value.scenario) { fieldErrors.value.scenario = "シナリオを選んでください"; return; }
        if (form.value.mode === "schedule" && !/^\s*\d+(\s*:\s*\d+(\.\d+)?)?(\s*,\s*\d+(\s*:\s*\d+(\.\d+)?)?)*\s*$/.test(form.value.schedule || "")) { fieldErrors.value.schedule = "N:秒 をカンマ区切りで書いてください（例 5:180,10:180）"; return; }
        if (activeEnv.value === "prod" && !form.value.confirm_prod) { fieldErrors.value.confirm_prod = "本番環境に負荷を掛けることを確認するチェックを入れてください"; nextTick(() => prodBox.value && prodBox.value.focus()); return; }
        if (activeEnv.value === "prod") { const ok = await confirmDialog({ title: "本番環境に負荷を掛けます", prod: true, lines: [`${resolved.value.host}（${resolved.value.domain}）に ${modeText.value}、上限 ${effectiveCap.value} で発信します。`, "監視している PBX の通話に影響する可能性があります。"], confirmLabel: "開始する", danger: true }); if (!ok) return; }
        busy.value = "start";
        try { await api("/api/run/start", startBody(), { quiet: true }); rateSynced = false; showToast("ランを開始しました"); }
        catch (e) { if (e.status === 428) { fieldErrors.value.confirm_prod = japanese(e); nextTick(() => prodBox.value && prodBox.value.focus()); } else if (/invalid schedule/.test(e.message)) fieldErrors.value.schedule = e.message; else showAlert(japanese(e)); }
        finally { busy.value = ""; }
      }
      async function restartSame() { run.value = null; await nextTick(); startRun(); }
      async function runPrecheck() {
        fieldErrors.value = {}; if (!form.value.scenario) { fieldErrors.value.scenario = "シナリオを選んでください"; return; }
        busy.value = "precheck"; precheckState.value = { running: true, started_at: Date.now() / 1000, hold_seconds: 6, elapsed_s: 0, result: null };
        try { const res = await api("/api/precheck", { scenario: form.value.scenario, pbx_profile: form.value.pbx_profile || null, monitor: form.value.monitor }, { quiet: true }); precheckState.value = { running: false, result: res }; showToast(res.ok ? "事前チェック: 合格" : "事前チェック: NG " + res.items.filter((i) => !i.ok).length + " 件", res.ok ? "ok" : "bad"); }
        catch (e) { precheckState.value = { running: false, result: { ok: false, items: [{ name: "起動", ok: false, detail: japanese(e) }], elapsed_s: 0 } }; }
        finally { busy.value = ""; }
      }
      async function stopRun() {
        if (!run.value) return;
        const ok = await confirmDialog({ title: "ランを停止しますか？", prod: run.value.pbx.environment === "prod", lines: [`対象: ${run.value.pbx.host}（${run.value.pbx.environment}）`, `確立中の ${ctl.value.established} 本と接続中の ${ctl.value.pending} 本をすべて BYE し、結果を保存して終了します。`], confirmLabel: "停止する", danger: true });
        if (!ok) return; busy.value = "stop";
        try { await api("/api/run/stop", {}); await loadRuns(); showToast("ランを停止しました"); } catch (e) { /* alert shown */ } finally { busy.value = ""; }
      }
      async function hangupAll() {
        if (!run.value) return;
        const ok = await confirmDialog({ title: "全通話を切りますか？", prod: run.value.pbx.environment === "prod", lines: [`対象: ${run.value.pbx.host}（${run.value.pbx.environment}）`, `確立中の ${ctl.value.established} 本を BYE します。目標 N はそのままなので、コントローラはすぐに掛け直します。`], confirmLabel: "全通話を切る", danger: true });
        if (ok) await post("hangup", "/api/run/hangup_all");
      }
      async function burst() {
        if (!run.value) return;
        const ok = await confirmDialog({ title: "バーストしますか？", prod: run.value.pbx.environment === "prod", lines: [`発信レートの制限を外し、目標 ${burstTarget.value} まで一気に発信します（現在 確立 ${ctl.value.established}）。`], confirmLabel: "バーストする", danger: run.value.pbx.environment === "prod" });
        if (ok) await post("burst", "/api/run/burst", { target: burstTarget.value });
      }
      const setTarget = (v) => post("target", "/api/run/target", { target: Number(v) });
      const setTargetDebounced = (v) => { clearTimeout(targetTimer); targetTimer = setTimeout(() => setTarget(v), 200); };
      const adjust = (d) => post("adjust", "/api/run/adjust", { delta: d });
      const setRate = () => post("rate", "/api/run/rate", rate.value);
      const schedule = async () => { if (await post("schedule", "/api/run/schedule", { text: scheduleText.value })) showToast("スケジュールを適用しました"); };
      const preset = (p) => post("schedule", "/api/run/schedule", { preset: p });
      const clearSchedule = () => post("schedule", "/api/run/schedule/clear");
      const pause = () => post("pause", "/api/run/pause");
      const resume = () => post("pause", "/api/run/resume");

      // ---- results ----
      async function openRun(id, fromHash) {
        try { const d = await api("/api/runs/" + id); detail.value = d; detailRows.value = (await api("/api/runs/" + id + "/rows", undefined, { quiet: true }).catch(() => ({ rows: [] }))).rows; }
        catch (e) { return; }
        tab.value = "results"; if (!fromHash) history.replaceState(null, "", "#results/" + id);
        await nextTick(); drawDetail(); if (!fromHash && detailEl.value) detailEl.value.scrollIntoView({ behavior: reducedMotion.value ? "auto" : "smooth", block: "start" });
      }
      function closeDetail() { detail.value = null; if (dchart) { dchart.destroy(); dchart = null; } history.replaceState(null, "", "#results"); }
      const runIndex = () => runsView.value.findIndex((r) => detail.value && r.id === detail.value.id);
      const hasRun = (d) => { const i = runIndex(); return i >= 0 && !!runsView.value[i + d]; };
      const stepRun = (d) => { const i = runIndex(); const r = runsView.value[i + d]; if (r) openRun(r.id); };
      async function compareRuns() {
        const ids = [...selectedRuns.value].sort((a, b) => a - b); if (ids.length < 2) return;
        const rs = [], rowSets = [];
        for (const id of ids) { try { rs.push(await api("/api/runs/" + id)); rowSets.push((await api("/api/runs/" + id + "/rows", undefined, { quiet: true })).rows); } catch (e) { return; } }
        const rows = rowSets[0].map((row, i) => (row.label === null ? row : { label: row.label, section: row.section, values: rowSets.map((set) => { const m = set.find((x) => x.label === row.label); return m ? m.value : null; }) }));
        compare.value = { runs: rs.map((r) => ({ id: r.id, name: r.name })), rows, data: rs };
        await nextTick();
        if (cchart) cchart.destroy();
        cchart = SemiCharts.compareChart(cchartEl.value, rs.map((r) => "#" + r.id + " " + r.name));
        cchart.setData(SemiCharts.compareData(rs));
      }
      function drawDetail() {
        if (!dchartEl.value || !detail.value) return;
        const samples = detail.value.samples || []; const load = samples.filter((p) => p.kind !== "monitor"), monS = samples.filter((p) => p.kind === "monitor");
        if (!dchart) dchart = SemiCharts.runChart(dchartEl.value, monS.length > 0); else dchart.rebuild();
        dchart.setData(SemiCharts.runData(load, monS), "このランには samples がありません");
      }

      // ---- live feed ----
      function applySnapshot(s) {
        const wasActive = runActive.value;
        run.value = s.run; precheckState.value = s.precheck || (precheckState.value && precheckState.value.running ? precheckState.value : precheckState.value);
        if (run.value && !rateSynced) { rate.value = { ramp_rate: run.value.controller.ramp_rate, call_duration: run.value.controller.call_duration }; rateSynced = true; burstTarget.value = Math.min(run.value.controller.max_concurrency, Math.max(burstTarget.value, run.value.controller.target)); }
        if (wasActive && run.value && run.value.finished) { loadRuns(); showToast("ランが終了しました: " + run.value.name); }
        nextTick(drawLive);
      }
      function drawLive() {
        if (!run.value) return;
        if (chartEl.value) { if (!chart) chart = SemiCharts.runChart(chartEl.value, !!(run.value.monitor && run.value.monitor.series)); chart.setData(SemiCharts.runData(run.value.series, run.value.monitor && run.value.monitor.series)); }
        if (mchartEl.value && run.value.monitor && !run.value.monitor.error) { if (!mchart) mchart = SemiCharts.monitorChart(mchartEl.value, processLabel.value); mchart.setData(SemiCharts.monitorData(run.value.monitor.series)); }
      }
      function rebuildCharts() { if (chart) { chart.destroy(); chart = null; } if (mchart) { mchart.destroy(); mchart = null; } drawLive(); if (detail.value) { if (dchart) { dchart.destroy(); dchart = null; } drawDetail(); } }
      watch(run, (v, old) => { if (!v && old) { if (chart) { chart.destroy(); chart = null; } if (mchart) { mchart.destroy(); mchart = null; } } });
      function connect() {
        ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
        ws.onopen = () => { connected.value = true; };
        ws.onclose = () => { connected.value = false; setTimeout(connect, 1500); };
        ws.onmessage = (ev) => {
          const s = JSON.parse(ev.data); lastAt.value = Date.now(); lastUpdate.value = hhmmss(lastAt.value / 1000);
          if (updatePaused.value && run.value && s.run && !s.run.finished) { pendingSnap = s; return; }
          const interval = (reducedMotion.value ? Math.max(2, updateInterval.value) : updateInterval.value) * 1000;
          if (Date.now() - lastApplied < interval - 50 && run.value && s.run && !s.run.finished && s.run.name === run.value.name) { pendingSnap = s; return; }
          lastApplied = Date.now(); pendingSnap = null; applySnapshot(s);
        };
      }
      setInterval(() => { if (pendingSnap && !updatePaused.value && Date.now() - lastApplied >= updateInterval.value * 1000) { lastApplied = Date.now(); const s = pendingSnap; pendingSnap = null; applySnapshot(s); } }, 250);
      watch(updatePaused, (p) => { if (!p && pendingSnap) { const s = pendingSnap; pendingSnap = null; applySnapshot(s); } });
      watch(selectedScenario, (s) => { if (s && !s.error) { form.value.target = s.target; form.value.ramp_rate = s.ramp_rate; form.value.call_duration = s.call_duration; if (!s.presets || !s.presets.includes(form.value.preset)) form.value.preset = s.presets && s.presets.length ? s.presets[0] : ""; if (form.value.mode === "preset" && !(s.presets || []).length) form.value.mode = "fixed"; } });
      watch(activeEnv, (e) => { if (e === "prod") form.value.max_concurrency = Math.min(form.value.max_concurrency, 20); });
      watch(tab, (t) => { if (t === "results") nextTick(() => { if (dchart) dchart.resize(); if (cchart) cchart.resize(); }); if (t === "run") nextTick(() => { if (chart) chart.resize(); if (mchart) mchart.resize(); }); });

      onMounted(async () => { applyTheme(); await loadScenarios(); await loadProfiles(); await loadRuns(); readHash(); connect(); });

      return {
        tabs: TABS, tab, setTab, connected, lastUpdate, run, precheckRunning, precheckState, precheckProgress, precheckResult, precheckNg, mmss, hhmmss, theme, themeLabel, cycleTheme,
        alert, stale, staleFor, ctl, backoffText, srSummary, toast, runActive, runFinished, form, fieldErrors, scenarios, scenarioDir, selectedScenario, profiles, presets, effectiveCap, activeEnv,
        startRun, runPrecheck, busy, restartSame, prodBox, setTargetDebounced, setTarget, adjust, pending, scheduleText, schedule, runPresets, preset, clearSchedule, rate, setRate, pause, resume, burstTarget, burst,
        extList, eventsDesc, kindOf, hangupAll, stopRun, resolved, testProfile, profileTest, modeText, recentRuns, openRun, ts, fmt, n, ago, nextStep, busyRejects, failText, answeredText, mon, monStale, monAge,
        seriesTruncated, chartEl, chartSummary, updateInterval, updatePaused, reducedMotion, callsFilter, callsView, sortState, callsSort, sortCalls, stateLabel, processLabel, mchartEl, customMetrics, logWarnOnly, logView, levelOf, flatten,
        editor, loadScenarioText, createScenario, deleteScenario, yamlEl, saveScenario, pform, loadProfileForm, deleteProfile, profileGroups, numericFields: NUMERIC_FIELDS, profileHelp: PROFILE_HELP, saveProfile, profilesPath,
        runs, runsQuery, selectedRuns, compareRuns, runsView, runsSort, sortRuns, detail, compare, cchartEl, detailEl, stepRun, hasRun, closeDetail, dchartEl, detailSummary, detailRows, dlgEl, dialog, dialogAnswer, dlgCancel,
      };
    },
  }).mount("#app");
})();
