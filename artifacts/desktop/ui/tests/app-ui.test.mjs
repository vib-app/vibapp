import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

function loadUi(invoke = null, navigatorLanguage = "en-US", runtimeControls = []) {
  const storage = new Map();
  const browser = { language: navigatorLanguage };
  const elements = new Map([
    ["#view", { innerHTML: "" }],
    ["#main", { focus() {} }],
    ["#toast", { textContent: "", className: "toast" }],
  ]);
  const document = {
    documentElement: { lang: "" },
    title: "",
    querySelector(selector) { return elements.get(selector) || null; },
    querySelectorAll(selector) {
      if (selector === "[data-runtime-field]" || selector === "[data-runtime-action], [data-runtime-field]") return runtimeControls;
      if (selector === "#view input, #view textarea, #view select, #view [contenteditable]") return runtimeControls;
      return [];
    },
    addEventListener() {},
  };
  const window = {
    __VIBAPP_UI_TEST__: true,
    document,
    location: { search: "" },
    setTimeout() {},
    setInterval() {},
    confirm() { return true; },
    crypto: {
      randomUUID() { return "00000000-0000-4000-8000-000000000001"; },
      subtle: webcrypto.subtle,
    },
  };
  if (invoke) window.__TAURI__ = { core: { invoke } };
  const context = vm.createContext({
    window,
    document,
    localStorage: { getItem(key) { return storage.get(key) ?? null; }, setItem(key, value) { storage.set(key, value); } },
    navigator: browser,
    URLSearchParams,
    Intl,
    Date,
    Math,
    Number,
    String,
    Boolean,
    Array,
    Map,
    Set,
    JSON,
    Object,
    RegExp,
    Error,
    Event: class {},
    TextEncoder,
    Uint8Array,
    FormData: class {},
    fetch: async () => { throw new Error("network forbidden in UI tests"); },
  });
  const source = fs.readFileSync(new URL("../app.js", import.meta.url), "utf8");
  vm.runInContext(source, context, { filename: "app.js" });
  return { ...window.VibAppUiTest, testDocument: document, testWindow: window, testStorage: storage, testNavigator: browser };
}

test("locale follows the primary browser language, defaults to English, and honors explicit choice", () => {
  for (const language of ["en", "en-GB", "fr-FR", "ja-JP", "", undefined]) {
    const ui = loadUi(null, language);
    assert.equal(ui.resolveLocale(), "en-US");
  }
  for (const language of ["zh", "zh-CN", "zh-HK", "zh-TW", "zh-Hant-HK"]) {
    const ui = loadUi(null, language);
    assert.equal(ui.resolveLocale(), "zh-CN");
    ui.setLocale("en-US");
    assert.equal(ui.resolveLocale(), "en-US");
    assert.equal(ui.testStorage.get("vibapp.ui_locale"), "en-US");
    ui.setLocale("auto");
    assert.equal(ui.resolveLocale(), "zh-CN");
    ui.testNavigator.languages = ["fr-FR", "zh-CN"];
    assert.equal(ui.resolveLocale(), "en-US");
  }
});

test("web-only download link follows locale and stays hidden in the native launcher", () => {
  const ui = loadUi();
  const link = { dataset: { localeKey: "download_client" }, textContent: "", hidden: true };
  ui.testDocument.querySelectorAll = selector =>
    ["[data-locale-key]", "[data-client-download]"].includes(selector) ? [link] : [];
  ui.setLocale("en-US");
  assert.equal(link.hidden, true);
  ui.testWindow.VibAppWebBridge = { invoke: async () => ({}) };
  ui.setLocale("en-US");
  assert.equal(link.hidden, false);
  assert.equal(link.textContent, "Download");
  ui.setLocale("zh-CN");
  assert.equal(link.textContent, "下载客户端");
  const html = fs.readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /https:\/\/github\.com\/vib-app\/vibapp\/releases/);
});

test("web navigation says Store while desktop keeps Library in both locales", () => {
  const ui = loadUi();
  const nav = { dataset: { localeKey: "nav_apps" }, textContent: "" };
  ui.testDocument.querySelectorAll = selector => selector === "[data-locale-key]" ? [nav] : [];
  ui.setLocale("en-US");
  assert.equal(nav.textContent, "Library");
  ui.setLocale("zh-CN");
  assert.equal(nav.textContent, "应用库");
  ui.testWindow.VibAppWebBridge = { invoke: async () => ({}) };
  ui.setLocale("en-US");
  assert.equal(nav.textContent, "Store");
  ui.setLocale("zh-CN");
  assert.equal(nav.textContent, "应用商店");
});

test("Store open prefers verified browser runtime; otherwise hands public apps to the client", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const app = { ...installedApp(), app_id: "ai.vibapp.clock", kind: "ui", publication_state: "published",
    installation_state: "client-required", web_runtime_available: false, launch_eligible: false, install_eligible: false };
  ui.testWindow.VibAppWebBridge = { invoke: async () => ({}) };
  ui.model.data = { apps: [app], jobs: [], needs: [], meta: {} };
  ui.model.selectedAppId = app.app_id;
  assert.equal(ui.webAppOpenMode(app), "desktop");
  assert.match(ui.renderApps(), /href="vibapp:\/\/ai.vibapp.clock"/);
  assert.match(ui.renderApps(), /target="_blank" rel="noopener" data-open-desktop-app=/);
  assert.match(ui.renderApps(), /Didn't open\? Install or update VibApp/);
  assert.doesNotMatch(ui.renderApps(), /Download package|data-launch=/);
  app.web_runtime_available = true;
  app.launch_eligible = true;
  assert.equal(ui.webAppOpenMode(app), "browser");
  assert.match(ui.renderApps(), /data-launch=/);
  assert.doesNotMatch(ui.renderApps(), /data-open-desktop-app=/);
  app.launch_eligible = false;
  assert.equal(ui.webAppOpenMode(app), "desktop", "A WASM flag alone does not authorize browser execution");
  for (const id of ["app?install=1", "app/path", "user@app", "app:80", "app..id", "<script>"]) {
    assert.equal(ui.desktopAppUrl(id), null);
  }
});

test("native incoming app is confined to its own install pane and escapes untrusted metadata", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  ui.model.storeOpenRequest = { request_id: "test", app_id: "ai.vibapp.clock", status: "ready",
    record: { app: { display_name: "<script>alert(1)</script>" }, runtime: { capabilities: [] } } };
  assert.equal(ui.renderStoreOpenRequest(), "", "The main window must not show inline consent");
  ui.model.installWindow = true;
  assert.match(ui.renderStoreOpenRequest(), /Install and open/);
  assert.match(ui.renderStoreOpenRequest(), />Cancel<\/button>/);
  assert.doesNotMatch(ui.renderStoreOpenRequest(), /search-home|header-nav|Find it/);
  assert.doesNotMatch(ui.renderStoreOpenRequest(), /<script>/);
  ui.model.storeOpenRequest.status = "downloading";
  assert.doesNotMatch(ui.renderStoreOpenRequest(), /data-confirm-store-open/);
  ui.model.runtimeWindowAppId = "ai.vibapp.clock";
  assert.equal(ui.renderStoreOpenRequest(), "");
});

test("install pane describes real permissions in both languages and locks consent during install", () => {
  const ui = loadUi();
  ui.model.installWindow = true;
  ui.model.storeOpenRequest = { request_id: "test", app_id: "ai.vibapp.clock", status: "ready",
    record: { app: { display_name: "Clock" }, runtime: { capabilities: [
      { interface: "vibapp:experimental-v0/clock@0.0.1" },
      { interface: "vibapp:experimental-v0/kv@0.0.1" },
    ] } } };
  ui.setTestLocale("en-US");
  assert.match(ui.renderStoreOpenRequest(), /Read the time/);
  assert.match(ui.renderStoreOpenRequest(), /Save this app&#39;s data/);
  ui.setTestLocale("zh-CN");
  assert.match(ui.renderStoreOpenRequest(), /读取时间/);
  assert.match(ui.renderStoreOpenRequest(), /保存此应用的数据/);
  ui.model.storeOpenRequest.status = "installing";
  assert.doesNotMatch(ui.renderStoreOpenRequest(), /data-confirm-store-open/);
  assert.match(ui.renderStoreOpenRequest(), /data-dismiss-store-open="test" disabled/);
});

test("language rerender preserves local drafts, unchecked consent, and does not store credentials", () => {
  const ui = loadUi();
  const original = [
    { name: "description", type: "textarea", form: { id: "hero-composer" }, value: "我的购物单", checked: false },
    { name: "embedding_consent", type: "checkbox", form: { id: "hero-composer" }, value: "granted", checked: false },
    { name: "generation_api_key", type: "password", form: { className: "model-settings-form" }, value: "not-a-real-secret", checked: false },
  ];
  let controls = original;
  ui.testDocument.querySelectorAll = selector => selector === "#view input, #view textarea, #view select" ? controls : [];
  const draft = ui.captureViewDrafts();
  controls = original.map(node => ({ ...node, value: node.type === "checkbox" ? "granted" : "", checked: true }));
  ui.restoreViewDrafts(draft);
  assert.equal(controls[0].value, "我的购物单");
  assert.equal(controls[1].checked, false);
  assert.equal(controls[2].value, "not-a-real-secret");
  assert.equal(ui.testStorage.size, 0);
});

test("hosted UI is bilingual, concise, and cannot present unavailable services as connected", () => {
  const ui = loadUi();
  ui.testDocument.documentElement.dataset = { vibappHostedShell: "true" };
  for (const locale of ["en-US", "zh-CN"]) {
    ui.setLocale(locale);
    const home = ui.renderSearchHome();
    assert.match(home, /class="send-button"[^>]*disabled/);
    assert.doesNotMatch(home, /local-status|WEB · WASM|LAUNCHER \+ APPSTORE/);
    assert.match(home, locale === "en-US" ? /Find it\. Vibe it\./ : /发现所需/);
    const settings = ui.renderSettings();
    assert.equal((settings.match(/<details class="settings-disclosure">/g) || []).length, 4);
    assert.equal((settings.match(/<fieldset class="settings-fields" disabled>/g) || []).length, 3);
    assert.doesNotMatch(settings, /192\.168\.|value="codex"|value="gpt-/);
  }
});

function canonicalFixtureJson(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return `[${value.map(canonicalFixtureJson).join(",")}]`;
  if (typeof value === "object") {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalFixtureJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function retryJob() {
  return {
    job_id: "job-retry", task_id: "development-retry", title: "Retry test", status: "codeagent-running",
    current_stage: "model-retry", updated_at_utc: "2026-09-08T01:00:00Z",
    verification: { status: "running" },
    codeagent_diagnostics: {
      state: "model-retry", model_requests: 1, logical_model_requests: 1, model_retries: 0, compiler_checks: 0,
      retry: { attempt: 2, max_attempts: 3, http_status: 502, delay_seconds: 1.25, reason: "provider-upstream-unavailable" },
    },
  };
}

function modelBudget(authoring = 30, repair = 0, phase = "authoring") {
  return { schema_version: "vibapp.model-request-budget-v1", phase,
    total_limit: 48, authoring_limit: 40, repair_limit: 8,
    total_used: authoring + repair, authoring_used: authoring, repair_used: repair,
    total_remaining: 48 - authoring - repair, authoring_remaining: 40 - authoring, repair_remaining: 8 - repair };
}

function failureObservation() {
  return { schema_version: "vibapp.docker-failure-diagnostic-v1", failure_origin: "provider-process",
    provider_error_category: "stream-decode", child_exit_code: 1, child_signal: null,
    stdout_bytes: 0, stderr_bytes: 400, bridge_stdout_bytes: 1000, bridge_stderr_bytes: 0,
    output_limit_exceeded: false, frame_limit_exceeded: false,
    container_exit_code: null, container_oom_killed: null, container_running: null };
}

test("phase budgets render past legacy24 and reserve repair without promising completion", () => {
  const ui = loadUi();
  const job = retryJob();
  Object.assign(job.codeagent_diagnostics, { model_requests: 30, logical_model_requests: 30, model_request_budget: modelBudget() });
  for (const locale of ["en-US", "zh-CN"]) {
    ui.setTestLocale(locale);
    let html = ui.renderCodeagentDiagnostics(job);
    assert.match(html, /30\/48/);
    assert.match(html, /HTTP 502/);
    assert.match(html, locale === "en-US" ? /10 attempts remaining; 8 reserved for compiler repairs/ : /初次编写剩余 10 次；另预留 8 次/);
    assert.match(html, locale === "en-US" ? /not a completion percentage/ : /不是完成百分比/);
    Object.assign(job.codeagent_diagnostics, { state: "compiler-feedback", model_requests: 42, logical_model_requests: 42, model_request_budget: modelBudget(40, 2, "repair") });
    html = ui.renderCodeagentDiagnostics(job);
    assert.match(html, locale === "en-US" ? /6 attempts remaining, shared across repair rounds/ : /编译修复剩余 6 次（多轮修复共用）/);
    Object.assign(job.codeagent_diagnostics, { state: "model-retry", model_requests: 30, logical_model_requests: 30, model_request_budget: modelBudget() });
  }
  for (const patch of [{ total_used: true }, { authoring_remaining: 11 }, { phase: "SECRET" }, { total_used: 31 }, { extra: "SECRET" }, { repair_used: 1, repair_remaining: 7, total_used: 31, total_remaining: 17 }]) {
    job.codeagent_diagnostics.model_request_budget = { ...modelBudget(), ...patch };
    assert.equal(ui.renderCodeagentDiagnostics(job), "");
  }
});

test("closed failure observations distinguish reported cause, confirmed OOM and unknown", () => {
  const ui = loadUi();
  const job = retryJob();
  job.status = "failed";
  Object.assign(job.codeagent_diagnostics, { state: "failed", failure_code: "provider-failed", upstream_status: 200, failure_diagnostic: failureObservation() });
  for (const locale of ["en-US", "zh-CN"]) {
    ui.setTestLocale(locale);
    let html = ui.renderCodeagentDiagnostics(job);
    assert.match(html, locale === "en-US" ? /Agent-reported:.*could not be decoded/ : /智能体报告：无法解析/);
    assert.match(html, locale === "en-US" ? /Agent exit code: 1/ : /智能体退出码 1/);
    assert.doesNotMatch(html, /HTTP 200|2\/3/);
    job.codeagent_diagnostics.failure_diagnostic.container_oom_killed = true;
    html = ui.renderCodeagentDiagnostics(job);
    assert.match(html, locale === "en-US" ? /Runtime confirmed:.*memory capacity/ : /运行环境确认：容器因内存不足/);
    job.codeagent_diagnostics.failure_diagnostic = { ...failureObservation(), provider_error_category: "unknown" };
    assert.match(ui.renderCodeagentDiagnostics(job), locale === "en-US" ? /root cause is still undetermined/ : /仍不能确定根因/);
    delete job.codeagent_diagnostics.failure_diagnostic;
    assert.match(ui.renderCodeagentDiagnostics(job), locale === "en-US" ? /no usable exit diagnostics/ : /没有可用的退出诊断/);
    job.codeagent_diagnostics.failure_diagnostic = failureObservation();
  }
  for (const patch of [{ raw_stderr: "SECRET" }, { failure_origin: "SECRET" }, { child_exit_code: true }, { child_signal: "SECRET" }, { container_oom_killed: 1 }, { stdout_bytes: 8388609 }]) {
    job.codeagent_diagnostics.failure_diagnostic = { ...failureObservation(), ...patch };
    const html = ui.renderCodeagentDiagnostics(job);
    assert.doesNotMatch(html, /SECRET|could not be decoded|无法解析|智能体退出码|Agent exit code/);
  }
  job.codeagent_diagnostics.failure_code = "provider-request-budget-exhausted";
  assert.doesNotMatch(ui.renderCodeagentDiagnostics(job), /HTTP 200/);
});

test("bounded retries appear in Builds and details in English and Chinese", () => {
  const ui = loadUi();
  const job = retryJob();
  ui.model.data = { jobs: [job], apps: [] };
  for (const locale of ["en-US", "zh-CN"]) {
    ui.setTestLocale(locale);
    for (const html of [ui.renderBuilds(), ui.renderJobConversation(job, null)]) {
      assert.match(html, /HTTP 502/);
      assert.match(html, /2\/3/);
      assert.match(html, /role="status"/);
      assert.match(html, locale === "en-US" ? /same authoring session/ : /保留当前编写会话/);
      assert.match(html, locale === "en-US" ? /Retries performed: 0/ : /已执行重试 0 次/);
      assert.match(html, locale === "en-US" ? /Compiler checks: 0/ : /编译检查 0 次/);
    }
  }
});

test("exhausted 502 shows actionable terminal cause without exposing raw diagnostics", () => {
  const ui = loadUi();
  const job = retryJob();
  job.status = "failed";
  job.verification.status = "failed";
  Object.assign(job.codeagent_diagnostics, {
    state: "failed", model_requests: 3, logical_model_requests: 1, model_retries: 2,
    failure_code: "provider-upstream-unavailable", upstream_status: 502,
    upstream_error: "SECRET <script>alert(1)</script> /private/path",
    upstream_request: { phase: "SECRET" },
  });
  for (const locale of ["en-US", "zh-CN"]) {
    ui.setTestLocale(locale);
    const html = ui.renderCodeagentDiagnostics(job);
    assert.match(html, /provider-upstream-unavailable/);
    assert.match(html, /HTTP 502/);
    assert.match(html, locale === "en-US" ? /automatic recovery has stopped/ : /自动恢复已停止/);
    assert.doesNotMatch(html, /SECRET|<script>|\/private\/path|2\/3/);
  }
  job.codeagent_diagnostics.failure_code = "provider-upstream-rejected";
  delete job.codeagent_diagnostics.logical_model_requests;
  delete job.codeagent_diagnostics.model_retries;
  for (const locale of ["en-US", "zh-CN"]) {
    ui.setTestLocale(locale);
    const legacy = ui.renderCodeagentDiagnostics(job);
    assert.match(legacy, /HTTP 502/);
    assert.match(legacy, locale === "en-US" ? /Review the HTTP status, service availability/ : /请检查 HTTP 状态、服务可用性/);
    assert.doesNotMatch(legacy, /not an automatically retryable transient failure|不能按临时故障|automatic recovery has stopped|自动恢复已停止|Retries performed|已执行重试/);
  }
  ui.setTestLocale("en-US");
  job.codeagent_diagnostics.failure_code = "provider-authentication-failed";
  assert.match(ui.renderCodeagentDiagnostics(job), /Check the account or credentials in Settings/);
});

test("terminal delivery states suppress stale retry and forged failures", () => {
  const ui = loadUi();
  for (const status of ["failed", "succeeded", "private-appstore-ready", "delivery-worker-failed"]) {
    const job = retryJob();
    job.status = status;
    assert.equal(ui.renderCodeagentDiagnostics(job), "");
  }
  const success = retryJob();
  success.status = "succeeded";
  success.codeagent_diagnostics.state = "failed";
  success.codeagent_diagnostics.failure_code = "provider-upstream-unavailable";
  assert.equal(ui.renderCodeagentDiagnostics(success), "");
});

test("legacy missing counters are not invented and forged retry fields are omitted", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  assert.equal(ui.renderCodeagentDiagnostics({}), "");
  const job = retryJob();
  job.codeagent_diagnostics = { state: "authoring", model_requests: 8, compiler_checks: 1 };
  const legacy = ui.renderCodeagentDiagnostics(job);
  assert.match(legacy, /Model network attempts: 8/);
  assert.doesNotMatch(legacy, /Retries performed|Logical requests/);
  for (const patch of [
    { state: [] }, { model_requests: true }, { compiler_checks: -1 },
    { retry: { ...retryJob().codeagent_diagnostics.retry, reason: "SECRET <script>" } },
    { retry: { ...retryJob().codeagent_diagnostics.retry, http_status: 401 } },
    { retry: { ...retryJob().codeagent_diagnostics.retry, delay_seconds: 31 } },
    { retry: { ...retryJob().codeagent_diagnostics.retry, path: "/private/path" } },
  ]) {
    const malformed = retryJob();
    Object.assign(malformed.codeagent_diagnostics, patch);
    assert.equal(ui.renderCodeagentDiagnostics(malformed), "");
  }
});

test("optional counts are consistent in every state, including legacy terminal rejected requests", () => {
  const ui = loadUi();
  for (const state of ["authoring", "compiler-feedback", "succeeded", "failed", "cancelled"]) {
    for (const patch of [{ logical_model_requests: 64 }, { model_retries: 4 }, { logical_model_requests: true }, { model_requests: 2 }]) {
      const job = retryJob();
      Object.assign(job.codeagent_diagnostics, { state }, patch);
      assert.equal(ui.renderCodeagentDiagnostics(job), "");
    }
  }
  const legacy = retryJob();
  legacy.status = "failed";
  Object.assign(legacy.codeagent_diagnostics, { state: "failed", model_requests: 1, logical_model_requests: 2, model_retries: 0 });
  assert.notEqual(ui.renderCodeagentDiagnostics(legacy), "");
});

function draftResult() {
  return {
    need: { need_id: "need-abc" },
    need_spec: {
      state: "draft",
      inferred_app_kind: "ui",
      missing_fields: ["acceptance-criteria"],
      draft: { need_id: "need-abc", goal: "Create a durable task list." },
      analysis: {
        status: "analyzed",
        app_kind: "ui",
        capabilities: ["kv", "settings"],
        goal_summary: "Create a durable task list.",
        questions: ["What result proves success?"],
        assumptions: ["Data remains local."],
      },
    },
    consent: { lan_llm_analysis: "granted", registry_embedding: "granted" },
    registry: {
      route: "refinement",
      recommendations: [],
      refinement: { reason_code: "no-acceptable-semantic-match", message: "No acceptable match." },
    },
    refinement_questions: ["What result proves success?"],
    cloud_development: { enabled: false, blockers: ["needspec-incomplete"] },
  };
}

function unsupportedCapabilityResult() {
  return {
    need: { need_id: "need-unsupported", status: "needs-refinement", codeagent_task_created: false },
    need_spec: {
      state: "draft",
      inferred_app_kind: "ui",
      inferred_world: "ui-only-reference",
      missing_fields: [],
      validation_errors: ["The ui-only-reference world does not support the requested HTTP capability."],
      draft: { need_id: "need-unsupported", goal: "Show live weather in a UI app." },
    },
    consent: { lan_llm_analysis: "granted", registry_embedding: "granted" },
    registry: {
      route: "refinement",
      recommendations: [],
      refinement: {
        reason_code: "unsupported-capability-combination",
        message: "Backend-localized message is intentionally replaced by the GUI.",
      },
    },
    refinement_questions: ["The ui-only-reference world does not support the requested HTTP capability."],
    cloud_development: {
      enabled: false,
      blockers: ["unsupported-capability-combination", "need-incomplete"],
      codeagent_task_created: false,
      external_request_made: false,
    },
  };
}

function productAssessmentJob() {
  const attempt = {
    schema_version: "vibapp.product-assessment-attempt.experimental-v1",
    document_type: "product-assessment-attempt",
    task_kind: "product-assessment",
    task_id: `assessment-${"a".repeat(32)}`,
    attempt_id: `assessment-attempt-0001-${"a".repeat(16)}`,
    need_id: "need-unsupported",
    reason_code: "unsupported-capability-combination",
    status: "unsupported",
    stage: "product-capability-assessment",
    codeagent_task_created: false,
    external_request_made: false,
  };
  return {
    job_id: attempt.task_id,
    task_id: attempt.task_id,
    attempt_id: attempt.attempt_id,
    task_kind: "product-assessment",
    need_id: attempt.need_id,
    title: "Live weather",
    status: "unsupported",
    progress_percent: 100,
    current_stage: "product-capability-assessment",
    updated_at_utc: "2026-08-28T08:00:00Z",
    history: [attempt],
    assessment: attempt,
    stages: [{ kind: "product-assessment", status: "succeeded", label: "Product capability assessment complete" }],
    verification: { status: "rejected", independent: false, summary: "Unsupported; CodeAgent was not invoked." },
    codeagent_task_created: false,
    external_request_made: false,
  };
}

function completeResult() {
  return {
    need: { need_id: "need-abc" },
    need_spec: {
      state: "complete",
      canonical_digest_sha256: "a".repeat(64),
      app_kind: "ui",
      capabilities: ["kv", "settings"],
      ecosystem_target: { layout_policy: "host-responsive" },
    },
    consent: {
      lan_llm_analysis: "granted",
      registry_embedding: "granted",
      cloud_remote_processing: "not-granted",
      public_sharing: "not-granted",
    },
    registry: { route: "refinement", refinement: { reason_code: "no-match", message: "No acceptable existing app matched." } },
    cloud_development: {
      enabled: false,
      blockers: ["remote-processing-consent-required"],
      data_leaving_device: { summary: "No remote authority is bound.", classes: [], provider: null },
      consent_binding: { decision: "not-requested" },
      task_preparation: { schema_preview_available: false },
    },
  };
}

function submissionReadyResult({
  attemptId = "attempt-0001-1111111111111111",
  consentId = "consent-cloud-aaaaaaaaaaaaaaaaaaaaaaaa",
  expiresAtUtc = "2099-01-01T00:00:00Z",
  immutableTaskDigest = "d".repeat(64),
  registryEvidenceSha256 = null,
} = {}) {
  const result = completeResult();
  const needSpecDigest = "a".repeat(64);
  const registryRequestId = `registry.${"a".repeat(24)}`;
  result.registry = {
    schema_version: "vibapp.registry-route.experimental.2026-08-24.1",
    status: "experimental-product-hold",
    route: "refinement",
    request_id: registryRequestId,
    need_id: "need-abc",
    recommendations: [],
    refinement: { reason_code: "no-hard-filter-match", message: "No candidate passed every hard filter." },
    retrieval: { mode: "hard-filter-then-keyword-plus-embedding" },
    rejected: [],
    codeagent_handoff: { created: false, permitted: false, reason: "Registry does not hand off to CodeAgent." },
  };
  const exactRegistryEvidenceSha256 = registryEvidenceSha256 || createHash("sha256")
    .update(canonicalFixtureJson(result.registry))
    .digest("hex");
  const jobId = "job-cloud-111111111111111111111111";
  const provider = "openai-codex";
  const model = null;
  const providerIdentitySha256 = "f".repeat(64);
  const consent = {
    consent_id: consentId,
    consent_type: "remote-processing",
    subject: "local-user",
    job_id: jobId,
    attempt_id: attemptId,
    provider,
    model,
    provider_execution_identity_sha256: providerIdentitySha256,
    payload_digest_sha256: immutableTaskDigest,
    uploaded_data_classes: [
      "need",
      "package-intent",
      "target-contract",
      "execution-limits",
      "generation-policy",
      "authoritative-contract",
      "provider-execution-identity",
    ],
    decision: "granted",
    single_use: true,
    issued_at_utc: "2026-01-01T00:00:00Z",
    expires_at_utc: expiresAtUtc,
    policy_version: "vibapp.cloud-codeagent-policy.experimental-v7",
    contract_digest_sha256: "b".repeat(64),
    instructions_digest_sha256: "c".repeat(64),
  };
  result.cloud_development.required_conditions = {
    provider_execution_available: true,
    authoritative_registry_no_match: true,
    registry_development_evidence_bound: true,
  };
  result.cloud_development.consent_binding = { ...consent };
  result.cloud_development.task_preparation = {
    schema_preview_available: true,
    submission_available: true,
    registry_evidence_available: true,
    registry_evidence_binding: {
      schema_version: "vibapp.registry-development-evidence.experimental-v1",
      document_type: "registry-development-evidence",
      immutable_task_digest_sha256: immutableTaskDigest,
      need_spec_digest_sha256: needSpecDigest,
      need_id: "need-abc",
      registry_request_id: registryRequestId,
      registry_evidence_sha256: exactRegistryEvidenceSha256,
    },
    schema_preview: {
      schema_version: "vibapp.cloud-codeagent-task.experimental-v3",
      job_id: jobId,
      immutable_task_digest_sha256: immutableTaskDigest,
      need_spec_digest_sha256: needSpecDigest,
      need_spec: { need_id: "need-abc" },
      execution_attempt: { attempt_id: attemptId },
      remote_processing_consent: true,
      provider,
      model,
      provider_execution_identity: { identity_sha256: providerIdentitySha256 },
      consent: { ...consent },
    },
  };
  return result;
}

function installedApp() {
  return {
    app_id: "ai.vibapp.fixture",
    display_name: "Fixture",
    version: "1.0.0",
    kind: "hybrid",
    summary: "Fixture app",
    verification_state: "verified",
    verification_summary: "Verified locally.",
    installation_state: "installed",
    launch_eligible: true,
    package_digest_sha256: "b".repeat(64),
    component_sha256: "c".repeat(64),
    permissions: ["kv"],
    service_entrypoints: [
      { id: "service.alpha", label: "Alpha" },
      { id: "service.beta", label: "Beta" },
    ],
    active_service_entrypoints: ["service.beta"],
    service_statuses: {
      "service.alpha": { state: "stopped" },
      "service.beta": { state: "running", health: "healthy" },
    },
  };
}

function runtimeResult() {
  return {
    descriptor: { id: "ai.vibapp.fixture", display_name: "Fixture", version: "1.0.0" },
    launcher_context: { installed: true, mode: "installed" },
    runtime_binding: {
      entrypoint: "launcher.main",
      package_digest_sha256: "b".repeat(64),
      component_sha256: "c".repeat(64),
      generation: "7",
      session: "session-1",
      surface: "surface-1",
      route: "home",
    },
    trusted_identity: {
      app_id: "ai.vibapp.fixture",
      display_name: "Fixture",
      version: "1.0.0",
      publisher: "Fixture Publisher",
      publication_badge: "private",
      permission_count: 1,
      active_package_digest_sha256: "b".repeat(64),
      generation: "7",
    },
    surface: {
      session: "session-1",
      surface: "surface-1",
      route: "home",
      view: {
        title: "Fixture",
        root: "root",
        nodes: [
          { id: "root", parent: null, kind: { listContainer: { label: "Main" } } },
          { id: "name", parent: "root", kind: { field: { field: "name", label: "Name", kind: "text", value: { text: "Ada" }, required: true, sensitive: false, choices: [] } } },
          { id: "secret", parent: "root", kind: { field: { field: "token", label: "Token", kind: "text", value: { empty: null }, required: true, sensitive: true, choices: [] } } },
          { id: "save", parent: "root", kind: { button: { label: "Save", action: "save", style: "primary", disabled: false } } },
          { id: "confirm", parent: "root", kind: { confirmation: { title: "Delete?", message: "This cannot be undone.", confirmAction: "delete", cancelAction: "cancel", destructive: true } } },
        ],
      },
    },
  };
}

test("explicit locale selection applies before rerender even when the system locale is Chinese", () => {
  const ui = loadUi(null, "zh-CN");
  ui.setLocale("en-US");
  assert.equal(ui.model.localePreference, "en-US");
  assert.equal(ui.model.locale, "en-US");
});

test("host-generated application identity is stable, inert, and shows publication origin", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const app = installedApp();
  app.publication_state = "private";
  app.publisher = "VibApp Local Builder";
  const identity = ui.appIdentityModel(app);
  assert.equal(identity.appId, app.app_id);
  assert.equal(identity.monogram, "F");
  assert.equal(identity.motifIndex, ui.appIdentityModel(app).motifIndex);
  assert.equal(ui.hashAppIdentity(app.app_id), ui.hashAppIdentity(app.app_id));
  assert.notEqual(ui.hashAppIdentity(app.app_id), ui.hashAppIdentity("ai.vibapp.other"));

  const hostile = ui.appIdentityIcon({ app_id: 'ai.vibapp.bad\" onclick=\"alert(1)', display_name: "<script>" }, "large bad<script>");
  assert.match(hostile, /class="app-icon app-icon-generated app-palette-[0-7] large"/);
  assert.doesNotMatch(hostile, /\sstyle=/);
  assert.match(hostile, /data-app-icon-id="ai\.vibapp\.bad&quot; onclick=&quot;alert\(1\)"/);
  assert.doesNotMatch(hostile, /<script>/);
  assert.doesNotMatch(hostile, /bad<script>/);

  ui.model.data = { apps: [app], jobs: [], needs: [] };
  ui.model.selectedAppId = app.app_id;
  const privateLibrary = ui.renderApps();
  assert.match(privateLibrary, /data-app-icon-id="ai\.vibapp\.fixture"/);
  assert.match(privateLibrary, /VibApp Local Builder/);
  assert.match(privateLibrary, /class="badge private">Private</);

  app.publication_state = "published";
  app.publication_badge = "public-appstore";
  const publicLibrary = ui.renderApps();
  assert.match(publicLibrary, /class="badge public-appstore">Public AppStore</);
});

test("English primary rendered flows contain no hard-coded Chinese chrome", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  ui.model.activeNeedId = "need-abc";
  ui.model.data = {
    apps: [installedApp()],
    jobs: [{ task_id: "task-1", job_id: "job-1", need_id: "need-abc", title: "Fixture build", status: "running", progress_percent: 20, updated_at_utc: "2026-08-27T00:00:00Z", stages: [{ label: "Builder", status: "running" }], verification: { status: "pending", summary: "Waiting." } }],
    needs: [],
  };
  ui.model.selectedAppId = "ai.vibapp.fixture";
  ui.model.runningApp = runtimeResult();
  const rendered = [
    ui.renderMessage({ role: "assistant", result: draftResult() }),
    ui.renderNeedSpecForm(draftResult()),
    ui.renderMessage({ role: "assistant", result: completeResult() }),
    ui.renderApps(),
    ui.renderBuilds(),
    ui.renderRuntime(),
  ].join("\n");
  assert.doesNotMatch(rendered, /\p{Script=Han}/u);
  assert.deepEqual(Object.keys(ui.i18n["en-US"]).sort(), Object.keys(ui.i18n["zh-CN"]).sort());
  assert.match(rendered, /data-runtime-action="save"/);
  assert.match(rendered, /capability is unavailable/);
});

test("unsupported capability combinations are rendered as platform blockers, not missing requirements", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  ui.model.activeNeedId = "need-unsupported";
  const html = ui.renderMessage({ role: "assistant", result: unsupportedCapabilityResult() });
  assert.match(html, /unsupported-capability-combination/);
  assert.match(html, /current VibApp platform does not support the selected app kind and capability combination/);
  assert.match(html, /Compatibility details/);
  assert.match(html, /Safely stopped at capability compatibility checking/);
  assert.match(html, /adding request prose alone will not remove this limit/i);
  assert.doesNotMatch(html, /Please refine the request|Request refinement is still in progress|Only these details are needed/);
  assert.doesNotMatch(html, /Backend-localized message/);

  const form = ui.renderNeedSpecForm(unsupportedCapabilityResult());
  assert.match(form, /COMPATIBILITY BLOCKED/);
  assert.match(form, /Platform compatibility blocker/);
  assert.match(form, /Compatibility details/);
  assert.doesNotMatch(form, /finish the local draft|Only these details are needed/);

  ui.setTestLocale("zh-CN");
  const chinese = ui.renderMessage({ role: "assistant", result: unsupportedCapabilityResult() });
  assert.match(chinese, /当前 VibApp 平台不支持所选应用形态与能力组合/);
  assert.doesNotMatch(chinese, /需要先把需求补充清楚|仍在需求细化阶段|只需要补这些/);
});

test("terminal product assessments are traceable without masquerading as CodeAgent attempts", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const job = productAssessmentJob();
  ui.model.data = { apps: [], jobs: [job], needs: [] };

  const builds = ui.renderBuilds();
  assert.match(builds, /data-task-kind="product-assessment"/);
  assert.match(builds, /Product capability assessment complete/);
  assert.match(builds, /Rejected/);

  const detail = ui.renderMessage({ role: "assistant", job, need: null });
  assert.match(detail, /not a CodeAgent development task/);
  assert.match(detail, /unsupported-capability-combination/);
  assert.match(detail, /task_created=false/);
  assert.match(detail, /external_request_made=false/);
  assert.match(detail, /Assessment record/);
  assert.doesNotMatch(detail, /Edit and retry/);
});

test("a prepared task cannot expose submit controls while provider containment is unavailable", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const result = completeResult();
  result.consent.cloud_remote_processing = "granted-single-use";
  result.cloud_development.required_conditions = {
    provider_execution_available: false,
  };
  result.cloud_development.provider_execution = {
    execution_available: false,
    blocker: {
      code: "local-live-containment-unavailable",
      message: "whole-descendant containment is unavailable",
    },
  };
  result.cloud_development.task_preparation = {
    schema_preview_available: true,
    schema_preview: { immutable_task_digest_sha256: "d".repeat(64) },
  };
  const rendered = ui.renderMessage({ role: "assistant", result });
  assert.match(rendered, /live CodeAgent execution is safely paused/);
  assert.match(rendered, /local-live-containment-unavailable/);
  assert.doesNotMatch(rendered, /data-submit-development=/);
  assert.doesNotMatch(rendered, /data-codeagent-submit-confirmation/);
});

test("development submit controls bind the exact task attempt and one-time consent", async () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const result = submissionReadyResult();
  assert.equal(await ui.verifyDevelopmentResultIntegrity(result), true);
  const digest = "d".repeat(64);
  const registryDigest = result.cloud_development.task_preparation.registry_evidence_binding.registry_evidence_sha256;
  const rendered = ui.renderMessage({ role: "assistant", result });
  assert.match(rendered, new RegExp(`data-submit-development="${digest}"`));
  assert.match(rendered, /data-submit-attempt="attempt-0001-1111111111111111"/);
  assert.match(rendered, /data-submit-consent="consent-cloud-aaaaaaaaaaaaaaaaaaaaaaaa"/);
  assert.match(rendered, /data-submit-expires="2099-01-01T00:00:00Z"/);
  assert.match(rendered, new RegExp(`data-submit-registry-evidence="${registryDigest}"`));

  const laterResult = submissionReadyResult({
    attemptId: "attempt-0002-2222222222222222",
    consentId: "consent-cloud-bbbbbbbbbbbbbbbbbbbbbbbb",
  });
  assert.equal(await ui.verifyDevelopmentResultIntegrity(laterResult), true);
  const firstMessage = { role: "assistant", result };
  const laterMessage = { role: "assistant", result: laterResult };
  const exact = ui.findPreparedDevelopmentTask([firstMessage, laterMessage], {
    immutableTaskDigest: digest,
    attemptId: "attempt-0001-1111111111111111",
    consentId: "consent-cloud-aaaaaaaaaaaaaaaaaaaaaaaa",
    expiresAtUtc: "2099-01-01T00:00:00Z",
    registryRequestId: `registry.${"a".repeat(24)}`,
    registryEvidenceSha256: registryDigest,
  });
  assert.equal(exact.message, firstMessage);
  assert.equal(exact.task.execution_attempt.attempt_id, "attempt-0001-1111111111111111");
  assert.equal(ui.findPreparedDevelopmentTask([laterMessage], {
    immutableTaskDigest: digest,
    attemptId: "attempt-0001-1111111111111111",
    consentId: "consent-cloud-aaaaaaaaaaaaaaaaaaaaaaaa",
    expiresAtUtc: "2099-01-01T00:00:00Z",
    registryRequestId: `registry.${"a".repeat(24)}`,
    registryEvidenceSha256: registryDigest,
  }), null);
});

test("recommendations and incomplete v3 identities never expose development submit controls", async () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const result = submissionReadyResult();
  assert.equal(await ui.verifyDevelopmentResultIntegrity(result), true);
  result.cloud_development.task_preparation.schema_preview = { immutable_task_digest_sha256: "d".repeat(64) };
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result }), /data-submit-development=/);

  const validTask = submissionReadyResult().cloud_development.task_preparation.schema_preview;
  result.cloud_development.task_preparation.schema_preview = validTask;
  result.cloud_development.required_conditions.registry_development_evidence_bound = false;
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result }), /data-submit-development=/);
  result.cloud_development.required_conditions.registry_development_evidence_bound = true;
  result.registry = { route: "recommendation", recommendations: [] };
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result }), /data-submit-development=/);
});

test("expired one-time consent cannot render a development submit control", async () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  assert.equal(ui.consentBindingCurrentlyValid({ expires_at_utc: "2026-01-01T00:00:00Z" }, Date.parse("2026-01-01T00:00:01Z")), false);
  assert.equal(ui.consentBindingCurrentlyValid({ expires_at_utc: "2026-01-01T00:00:02Z" }, Date.parse("2026-01-01T00:00:01Z")), true);
  const result = submissionReadyResult({ expiresAtUtc: "2020-01-01T00:00:00Z" });
  assert.equal(await ui.verifyDevelopmentResultIntegrity(result), true);
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result }), /data-submit-development=/);
});

test("development submit fails closed on consent or trusted Registry binding drift", async () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const missingTaskExpiry = submissionReadyResult();
  assert.equal(await ui.verifyDevelopmentResultIntegrity(missingTaskExpiry), true);
  delete missingTaskExpiry.cloud_development.task_preparation.schema_preview.consent.expires_at_utc;
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result: missingTaskExpiry }), /data-submit-development=/);

  const consentDrift = submissionReadyResult();
  assert.equal(await ui.verifyDevelopmentResultIntegrity(consentDrift), true);
  consentDrift.cloud_development.consent_binding.expires_at_utc = "2098-01-01T00:00:00Z";
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result: consentDrift }), /data-submit-development=/);

  const evidenceDrift = submissionReadyResult();
  assert.equal(await ui.verifyDevelopmentResultIntegrity(evidenceDrift), true);
  evidenceDrift.cloud_development.task_preparation.registry_evidence_binding.immutable_task_digest_sha256 = "f".repeat(64);
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result: evidenceDrift }), /data-submit-development=/);

  const hiddenConsentDrift = submissionReadyResult();
  assert.equal(await ui.verifyDevelopmentResultIntegrity(hiddenConsentDrift), true);
  hiddenConsentDrift.cloud_development.consent_binding.provider = "opencode";
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result: hiddenConsentDrift }), /data-submit-development=/);

  const registryContentDrift = submissionReadyResult();
  assert.equal(await ui.verifyDevelopmentResultIntegrity(registryContentDrift), true);
  registryContentDrift.registry.refinement.message = "A different no-match explanation.";
  assert.doesNotMatch(ui.renderMessage({ role: "assistant", result: registryContentDrift }), /data-submit-development=/);

  const message = { role: "assistant", result: submissionReadyResult({ expiresAtUtc: "2026-01-01T00:00:02Z" }) };
  assert.equal(await ui.verifyDevelopmentResultIntegrity(message.result), true);
  assert.equal(ui.findPreparedDevelopmentTask([message], {
    immutableTaskDigest: "d".repeat(64),
    attemptId: "attempt-0001-1111111111111111",
    consentId: "consent-cloud-aaaaaaaaaaaaaaaaaaaaaaaa",
    expiresAtUtc: "2026-01-01T00:00:02Z",
    registryRequestId: `registry.${"a".repeat(24)}`,
    registryEvidenceSha256: message.result.cloud_development.task_preparation.registry_evidence_binding.registry_evidence_sha256,
  }, Date.parse("2026-01-01T00:00:03Z")), null);
});

test("shared settings UI exposes bounded host network controls without raw app networking", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  ui.model.networkSettings = {
    p2pEnabled: true,
    seedVerifiedApps: true,
    allowUserFileSeeding: false,
    uploadLimitKibPerSecond: 2048,
    downloadLimitKibPerSecond: 8192,
    cacheLimitMib: 4096,
    maxActiveTransfers: 12,
    rtcEnabled: true,
    maxActiveChannels: 6,
    turnEnabled: false,
    turnUrls: [],
    turnUsername: "",
    hasTurnCredential: true,
  };
  ui.model.networkStatus = {
    state: "running",
    foregroundOnly: false,
    activeTransfers: 2,
    torrents: 3,
    lastError: null,
  };
  const rendered = ui.renderSettings();
  assert.match(rendered, /network-settings-form/);
  assert.match(rendered, /name="upload_limit"[^>]*value="2048"/);
  assert.match(rendered, /name="download_limit"[^>]*value="8192"/);
  assert.match(rendered, /P2P file transfer and RTC are based on the RoomHash network/);
  assert.match(rendered, /Network node status · Running/);
  assert.match(rendered, /Active transfers 2 · joined resources 3/);
  assert.match(rendered, /data-network-node-action="stop"/);
  assert.match(rendered, /apps do not receive raw WebTorrent, RTC, or arbitrary network access/);
  assert.doesNotMatch(rendered, /private-turn-secret/);
  assert.deepEqual(Object.keys(ui.i18n["en-US"]).sort(), Object.keys(ui.i18n["zh-CN"]).sort());
});

test("installed runtime exposes explicit temporary collaboration controls", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  ui.model.runningApp = runtimeResult();
  ui.model.networkSettings = { p2pEnabled: true, rtcEnabled: true };
  ui.model.collaborationStatus = { state: "ready", sessions: [] };
  const local = ui.renderRuntime();
  assert.match(local, /data-collaboration-confirm/);
  assert.match(local, /data-collaboration-create/);
  assert.match(local, /data-collaboration-join/);
  assert.match(local, /all-zero UUID never joins the network/);
  ui.model.collaborationStatus = {
    state: "ready",
    sessions: [{
      sessionId: "10000000-0000-4000-8000-000000000001",
      appId: "ai.vibapp.fixture",
      channelId: "20000000-0000-4000-8000-000000000001",
      expiresAt: 1_900_000_000_000,
      peerCount: 2,
    }],
  };
  const shared = ui.renderRuntime();
  assert.match(shared, /20000000-0000-4000-8000-000000000001/);
  assert.match(shared, /Connected to 2 other nodes/);
  assert.match(shared, /data-collaboration-leave=/);
  assert.doesNotMatch(shared, /magnet:|tracker URL|TURN credential/);
});

test("user and app content retain their original language", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  assert.match(ui.renderMessage({ role: "user", text: "用户自己的中文内容" }), /用户自己的中文内容/);
  const app = installedApp();
  app.display_name = "用户应用";
  ui.model.data = { apps: [app], jobs: [], needs: [] };
  ui.model.selectedAppId = app.app_id;
  assert.match(ui.renderApps(), /用户应用/);
});

test("browser package cache is shown as verified cache and never as an installation", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const app = installedApp();
  app.installation_state = "cached";
  app.web_package_cached = true;
  app.web_runtime_available = false;
  app.launch_eligible = false;
  app.service_entrypoints = [];
  app.kind = "ui";
  ui.model.data = { apps: [app], jobs: [], needs: [] };
  ui.model.selectedAppId = app.app_id;
  const cached = ui.renderApps();
  assert.match(cached, /Verified package cached in this browser/);
  assert.match(cached, /has no verified Web Runtime; use VibApp Client to run it/);
  assert.doesNotMatch(cached, /Installed and enabled|data-install-app|magnet:|locator_path/);

  app.installation_state = "candidate";
  app.web_package_cached = false;
  app.web_cache_eligible = true;
  app.install_eligible = true;
  const candidate = ui.renderApps();
  assert.match(candidate, /data-install-app=/);
  assert.match(candidate, /Download and verify/);
});

test("desktop-only browser rows explain the unavailable runtime without offering native controls", () => {
  const ui = loadUi();
  const app = { ...installedApp(), kind: "ui", installation_state: "client-required",
    launch_mode: "client-required", launch_eligible: false, install_eligible: false,
    update_eligible: false, available_update: null, update: null, active_service_entrypoints: [],
    service_statuses: {}, web_package_cached: false, web_cache_eligible: false,
    web_runtime_available: false, web_unavailable_reason: "browser-runtime-unavailable" };
  ui.model.data = { apps: [app], jobs: [], needs: [], meta: { runtime_mode: "browser-wasm" } };
  ui.model.selectedAppId = app.app_id;
  for (const locale of ["en-US", "zh-CN"]) {
    ui.setTestLocale(locale);
    const html = ui.renderApps();
    assert.match(html, locale === "en-US" ? /Desktop client required/ : /需桌面客户端/);
    assert.match(html, locale === "en-US" ? /no verified browser runtime/ : /尚无可验证的网页运行包/);
    assert.match(html, /role="status"/);
    assert.doesNotMatch(html, /data-install-app=|data-launch=|data-app-action=|Installed and enabled|已安装并启用/);
  }
  app.kind = "hybrid";
  ui.setTestLocale("en-US");
  assert.match(ui.renderApps(), /Manage background services in the desktop client/);
  assert.doesNotMatch(ui.renderApps(), /data-app-action=|entrypoints hosted by a separate daemon/);
});

test("local browser preview keeps its preview action and never becomes a private install", () => {
  const ui = loadUi();
  const app = { ...installedApp(), kind: "ui", installation_state: "not-installed",
    launch_mode: "web-worker-foreground", launch_eligible: true, install_eligible: false,
    verification_state: "locally-derived-awaiting-independent-verifier", web_unavailable_reason: null,
    service_entrypoints: [] };
  ui.model.data = { apps: [app], jobs: [], needs: [] };
  ui.model.selectedAppId = app.app_id;
  ui.setTestLocale("en-US");
  const html = ui.renderApps();
  assert.match(html, /Private preview|Open preview/);
  assert.match(html, /data-launch=/);
  assert.doesNotMatch(html, /data-install-app=|data-app-action=|Desktop client required/);
});

test("task detail reconstructs typed durable turns and preserves all attempts", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const entry = (entryId, sequence, role, content, taskId = null, attemptId = null, snapshot = null) => ({
    schema_version: "vibapp.conversation-entry.experimental-v1",
    entry_id: entryId,
    sequence,
    need_id: "need-abc",
    task_id: taskId,
    attempt_id: attemptId,
    role,
    kind: role === "user" ? "requirement-edited" : "assistant-refinement",
    content,
    snapshot,
  });
  ui.model.data = {
    apps: [],
    needs: [
      { need_id: "need-abc", conversation_transcript: [entry("one", 1, "user", "Initial request"), entry("two", 2, "assistant", "Initial answer", null, null, draftResult())] },
      { need_id: "need-abc", description: "Edited request", conversation_transcript: [entry("three", 3, "user", "Edited request", "task-1", "attempt-1"), entry("four", 4, "assistant", "Retry answer", "task-1", "attempt-1", draftResult())] },
    ],
  };
  const job = {
    need_id: "need-abc",
    task_id: "task-1",
    attempt_id: "attempt-2",
    status: "failed",
    history: [
      { attempt_id: "attempt-1", status: "failed", stage: "builder", error: { code: "build-failed", message: "First failure" } },
      { attempt_id: "attempt-2", status: "failed", stage: "verifier", error: { code: "verification-failed", message: "Second failure" } },
    ],
  };
  const transcript = ui.conversationForJob(job);
  assert.equal(transcript.length, 5);
  assert.deepEqual(transcript.slice(0, 4).map(item => item.text || item.result?.need?.need_id), ["Initial request", "need-abc", "Edited request", "need-abc"]);
  assert.equal(transcript[2].taskId, "task-1");
  assert.equal(transcript[2].attemptId, "attempt-1");
  const html = ui.renderMessage(transcript.at(-1));
  assert.match(html, /attempt-1/);
  assert.match(html, /attempt-2/);
});

test("task detail tolerates null errors and malformed legacy history without losing valid attempts", () => {
  const ui = loadUi();
  const attempts = [null, false, [], "old", { attempt_id: "successful", status: "succeeded", error: null },
    { attempt_id: "missing-error", status: "failed" },
    { attempt_id: "string-error", status: "failed", error: "<legacy failure>" },
    { attempt_id: "object-error", status: "failed", error: { code: "build-failed", message: "<compile failure>" } }];
  for (const history of [attempts, { attempts }]) {
    const html = ui.renderJobConversation({ status: "failed", history }, null);
    for (const id of ["successful", "missing-error", "string-error", "object-error"]) assert.match(html, new RegExp(id));
    assert.match(html, /&lt;legacy failure&gt;/);
    assert.match(html, /build-failed.*&lt;compile failure&gt;/);
    assert.doesNotMatch(html, /<legacy failure>|<compile failure>/);
  }
  assert.doesNotThrow(() => ui.renderJobConversation({ history: { attempts: "invalid" } }, null));
  assert.match(ui.renderJobConversation({ history: null, attempts: [attempts[4]] }, null), /successful/);
});

test("diagnostics render bounded fifth through eighth network retries and reject ninth", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  for (const retries of [5, 8, 9]) {
    const job = retryJob();
    Object.assign(job.codeagent_diagnostics, { model_requests: 20, logical_model_requests: 20 - retries,
      model_retries: retries, model_request_budget: modelBudget(20) });
    const html = ui.renderCodeagentDiagnostics(job);
    if (retries <= 8) assert.match(html, new RegExp(`Retries performed: ${retries}`));
    else assert.equal(html, "");
  }
});

test("read-only legacy history stays explicit and offers no automatic retry", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  ui.model.data = { apps: [], needs: [], jobs: [{ task_id: "old-task", title: "Old app", status: "legacy-history",
    legacy_read_only: true, history: [{ attempt_id: "old-attempt", status: "private-appstore-ready", error: null }],
    verification: { status: "historical", summary: "Recorded completion, not newly verified" } }] };
  assert.match(ui.renderBuilds(), /Historical record/);
  assert.doesNotMatch(ui.renderJobConversation(ui.model.data.jobs[0], null), /data-edit-job=|data-job-run=/);
});

test("a job can run only the exact package produced by that job", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const app = installedApp();
  ui.model.data = { apps: [app], jobs: [], needs: [] };
  const job = {
    need_id: "need-abc",
    task_id: "task-1",
    attempt_id: "attempt-0001-1111111111111111",
    status: "succeeded",
    outputs: { app_id: app.app_id, package_digest_sha256: app.package_digest_sha256 },
    history: [],
  };
  const exact = ui.renderMessage({ role: "assistant", job, need: null });
  assert.match(exact, new RegExp(`data-job-package-digest="${app.package_digest_sha256}"`));
  assert.match(exact, /Run this build/);

  job.outputs.package_digest_sha256 = "f".repeat(64);
  const stale = ui.renderMessage({ role: "assistant", job, need: null });
  assert.doesNotMatch(stale, /data-job-run=/);
  assert.doesNotMatch(stale, /Run this build/);
});

test("every service entrypoint has independent lifecycle controls", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  ui.model.data = { apps: [installedApp()], jobs: [], needs: [] };
  ui.model.selectedAppId = "ai.vibapp.fixture";
  const html = ui.renderApps();
  assert.equal((html.match(/data-service-entrypoint=/g) || []).length, 2);
  assert.match(html, /data-entrypoint="service\.alpha"[^>]*>Start service/);
  assert.match(html, /data-entrypoint="service\.beta"[^>]*>Stop service/);
  assert.match(html, /data-entrypoint="service\.beta"[^>]*>Check health/);
});

test("semantic action wire values are strict and sensitive plaintext fails closed", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const plain = value => JSON.parse(JSON.stringify(value));
  assert.deepEqual(plain(ui.runtimeWireValue({ kind: "text", sensitive: false, required: true, label: "Name" }, { value: "Ada" })), { tag: "text", value: "Ada" });
  assert.deepEqual(plain(ui.runtimeWireValue({ kind: "Text", sensitive: false, required: true, label: "Name" }, { value: "Ada" })), { tag: "text", value: "Ada" });
  assert.deepEqual(plain(ui.runtimeWireValue({ kind: "TimeZone", sensitive: false, required: true, label: "Zone" }, { value: "Asia/Shanghai" })), { tag: "time-zone", value: "Asia/Shanghai" });
  assert.deepEqual(plain(ui.runtimeWireValue({ kind: "Text", sensitive: false, required: true, label: "Name" }, { value: "" })), { tag: "empty" });
  assert.deepEqual(plain(ui.runtimeWireValue({ kind: "integer", sensitive: false, required: true, label: "Count" }, { value: "42" })), { tag: "integer", value: 42 });
  assert.deepEqual(plain(ui.runtimeWireValue({ kind: "date", sensitive: false, required: true, label: "Date" }, { value: "2026-08-27" })), { tag: "date", value: { year: 2026, month: 8, day: 27 } });
  assert.deepEqual(plain(ui.runtimeWireValue({ kind: "text", sensitive: true, required: true, label: "Token", value: { "secret-handle": "opaque-1" } }, { value: "plaintext-must-be-ignored" })), { tag: "secret-handle", value: "opaque-1" });
  assert.throws(() => ui.runtimeWireValue({ kind: "text", sensitive: true, required: true, label: "Token", value: { text: "plaintext" } }, { value: "plaintext" }), /Secret Store/);
});

test("runtime accepts only exact binding and surface identity", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const previous = runtimeResult();
  const good = { runtime_binding: { ...previous.runtime_binding }, surface: { ...previous.surface, view: { ...previous.surface.view, title: "Updated" } } };
  assert.equal(ui.acceptRuntimeActionResult(previous, good).surface.view.title, "Updated");
  const stale = { runtime_binding: { ...previous.runtime_binding, generation: 8 }, surface: good.surface };
  assert.throws(() => ui.acceptRuntimeActionResult(previous, stale), /stale binding/);
  const forged = { runtime_binding: { ...previous.runtime_binding }, surface: { ...good.surface, route: "forged" } };
  assert.throws(() => ui.acceptRuntimeActionResult(previous, forged), /forged or stale surface/);
});

test("Text fields render multiline editors and preserve leading and embedded newlines", () => {
  const ui = loadUi();
  const value = "\n第一行\n第二行\n";
  const html = ui.renderRuntimeTree([{ id: "body", parent: null, kind: { Field: {
    field: "body", label: "正文", kind: "Text", value: { Text: value }, sensitive: false, required: false,
  } } }], "body");
  assert.match(html, /<textarea[^>]*data-runtime-kind="text"/);
  assert.ok(html.includes(">\n" + value + "</textarea>"));
  assert.deepEqual(JSON.parse(JSON.stringify(ui.runtimeWireValue({ kind: "Text", sensitive: false }, { value }))), { tag: "text", value });
});

test("automatic surface refresh is restricted to display-only app windows", () => {
  const ui = loadUi();
  const surface = runtimeResult().surface;
  surface.view.nodes = [
    { id: "root", parent: null, kind: { listContainer: { label: "Clock" } } },
    { id: "clock", parent: "root", kind: { text: { text: "12:34:56", style: "title" } } },
    { id: "progress", parent: "root", kind: { progress: { label: "Day", current: 12, total: 24 } } },
  ];
  assert.equal(ui.runtimeSurfaceIsDisplayOnly(surface), true);
  surface.view.nodes.push({ id: "field", parent: "root", kind: { field: { field: "name", label: "Name", kind: "text" } } });
  assert.equal(ui.runtimeSurfaceIsDisplayOnly(surface), false);
  surface.view.nodes.pop();
  surface.view.nodes.push({ id: "button", parent: "root", kind: { button: { label: "Start", action: "start" } } });
  assert.equal(ui.runtimeSurfaceIsDisplayOnly(surface), false);
});

test("a one-node title surface receives the standalone primary-display layout", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const surface = runtimeResult().surface;
  surface.view.nodes = [
    { id: "clock", parent: null, kind: { text: { text: "12:34:56", style: "heading" } } },
  ];
  surface.view.root = "clock";
  assert.equal(ui.runtimeSurfaceIsSingleDisplay(surface), true);
  // The native daemon serializes the actual enum in PascalCase.
  surface.view.nodes[0].kind = { Text: { text: "12:34:56", style: "Heading" } };
  assert.equal(ui.runtimeSurfaceIsSingleDisplay(surface), true);
  ui.model.data = { apps: [installedApp()], jobs: [], needs: [] };
  ui.model.runningApp = { ...runtimeResult(), surface };
  ui.model.runtimeWindowAppId = "ai.vibapp.fixture";
  const standalone = ui.renderRuntime();
  assert.match(standalone, /guest-surface single-display/);
  assert.match(standalone, /data-host-trusted-identity[^>]*data-binding-exact="true"/);
  assert.match(standalone, /aria-label="VibApp Client verified application window: Fixture, ai\.vibapp\.fixture, version 1\.0\.0"/);
  assert.match(standalone, /Fixture Publisher/);
  assert.match(standalone, /1 permissions host-managed/);
  assert.match(standalone, /<details class="collaboration-panel compact-overlay">/);
  assert.doesNotMatch(standalone, /<header>|Client hosted|data-app-icon-id=/);

  surface.view.nodes.push({ id: "caption", parent: null, kind: { text: { text: "Extra", style: "body" } } });
  assert.equal(ui.runtimeSurfaceIsSingleDisplay(surface), false);
});

test("semantic action rows keep keypad order and mixed content spans the row", () => {
  const ui = loadUi();
  const button = (id, parent) => ({ id, parent, kind: { button: { action: id, label: id } } });
  const nodes = [
    { id: "root", parent: null, kind: { listContainer: { label: "Actions" } } },
    { id: "row", parent: "root", kind: { listContainer: { label: null } } },
    ...["7", "8", "9", "/"].map(id => button(id, "row")),
    button("clear", "root"), button("equals", "root"),
  ];
  const html = ui.renderRuntimeTree(nodes, "root", true);
  assert.match(html, /^<section class="runtime-group runtime-actions runtime-actions-2"/);
  assert.match(html, /<section class="runtime-group runtime-actions runtime-actions-4"/);
  const actions = [...html.matchAll(/data-runtime-action="([^"]+)"/g)].map(match => match[1]);
  assert.deepEqual(actions, ["7", "8", "9", "/", "clear", "equals"]);
  nodes.splice(2, 4, button("one", "row"));
  const single = ui.renderRuntimeTree(nodes, "root", true);
  assert.doesNotMatch(single, /runtime-actions-4/);
  assert.match(single, /<section class="runtime-group" >.*data-runtime-action="one"/);
});

test("content fitting measures intrinsic content and not the viewport minimum", () => {
  const ui = loadUi();
  const guest = { classList: { contains: () => false }, children: [{}],
    firstElementChild: { getBoundingClientRect: () => ({ top: 50 }) },
    lastElementChild: { getBoundingClientRect: () => ({ bottom: 560 }) } };
  const original = ui.testDocument.querySelector;
  ui.testDocument.querySelector = selector => selector === ".guest-surface" ? guest
    : selector === ".runtime-trust-strip" ? { getBoundingClientRect: () => ({ height: 34 }) } : original(selector);
  ui.testWindow.getComputedStyle = () => ({ paddingTop: "24px", paddingBottom: "68px" });
  assert.equal(ui.runtimeInitialContentHeight(), 636);
  guest.lastElementChild.getBoundingClientRect = () => ({ bottom: 100000 });
  assert.equal(ui.runtimeInitialContentHeight(), 1200);
  guest.classList.contains = () => true;
  assert.equal(ui.runtimeInitialContentHeight(), null);
  assert.equal(loadUi().runtimeInitialContentHeight(), null);
});

test("slim trusted strip fails closed when active package binding does not match", () => {
  const ui = loadUi();
  ui.setTestLocale("en-US");
  const running = runtimeResult();
  running.trusted_identity.active_package_digest_sha256 = "d".repeat(64);
  const identity = ui.runtimeTrustedIdentity(running);
  assert.equal(identity.exact, false);
  assert.equal(identity.publisher, null);
  const strip = ui.renderRuntimeTrustStrip(running);
  assert.match(strip, /data-binding-exact="false"/);
  assert.match(strip, /Identity binding pending/);
  assert.match(strip, /Application identity pending/);
  assert.match(strip, /Permission status unavailable/);
  assert.doesNotMatch(strip, />Fixture</);
  assert.doesNotMatch(strip, /Fixture Publisher/);
});

test("display-only child window refresh sends the exact trusted binding without overlapping", async () => {
  let observed = null;
  const ui = loadUi(async (command, payload) => {
    observed = { command, payload };
    const surface = JSON.parse(JSON.stringify(ui.model.runningApp.surface));
    surface.view.nodes[1].kind.text.text = "12:34:57";
    return { runtime_binding: { ...ui.model.runningApp.runtime_binding }, surface };
  });
  const running = runtimeResult();
  running.surface.view.nodes = [
    { id: "root", parent: null, kind: { listContainer: { label: "Clock" } } },
    { id: "clock", parent: "root", kind: { text: { text: "12:34:56", style: "title" } } },
  ];
  ui.model.data = { apps: [installedApp()], jobs: [], needs: [] };
  ui.model.route = "runtime";
  ui.model.runningApp = running;
  ui.model.runtimeWindowAppId = running.descriptor.id;
  ui.model.runtimeRefreshActive = true;
  await ui.refreshRuntimeSurface();
  assert.equal(observed.command, "refresh_app_surface");
  assert.deepEqual(Object.keys(observed.payload).sort(), ["appId", "componentSha256", "entrypoint", "eventId", "generation", "packageDigestSha256", "route", "session", "surface"].sort());
  assert.match(observed.payload.eventId, /^desktop-refresh-/);
  assert.equal(ui.model.runningApp.surface.view.nodes[1].kind.text.text, "12:34:57");
  assert.equal(ui.model.runtimeRefreshInFlight, false);
});

test("an unchanged tick keeps refreshing, including an interactive stopwatch", async () => {
  let calls = 0;
  const ui = loadUi(async () => {
    calls++;
    const surface = JSON.parse(JSON.stringify(ui.model.runningApp.surface));
    if (calls === 2) surface.view.nodes[1].kind.text.text = "12:35";
    return { runtime_binding: { ...ui.model.runningApp.runtime_binding }, surface };
  });
  const running = runtimeResult();
  running.surface.view.nodes = [
    { id: "root", parent: null, kind: { listContainer: { label: "Clock" } } },
    { id: "clock", parent: "root", kind: { text: { text: "12:34", style: "title" } } },
    { id: "pause", parent: "root", kind: { button: { label: "Pause", action: "pause" } } },
  ];
  Object.assign(ui.model, { data: { apps: [installedApp()], jobs: [], needs: [] }, route: "runtime",
    runningApp: running, runtimeWindowAppId: running.descriptor.id, runtimeRefreshActive: true });
  await ui.refreshRuntimeSurface();
  assert.equal(ui.model.runtimeRefreshActive, true);
  await ui.refreshRuntimeSurface();
  assert.equal(calls, 2);
  assert.equal(ui.model.runningApp.surface.view.nodes[1].kind.text.text, "12:35");
  ui.model.runtimeDispatching = true;
  await ui.refreshRuntimeSurface();
  assert.equal(calls, 2, "refresh cannot overlap a mutating action");
});

test("background render preserves unsaved settings without copying secrets into model", () => {
  const control = { tagName: "INPUT", type: "password", value: "unsaved-test-secret", defaultValue: "" };
  const ui = loadUi(null, "en-US", [control]);
  ui.model.data = { apps: [], jobs: [], needs: [] };
  ui.testDocument.querySelector("#view").innerHTML = "untouched-editor";
  assert.equal(ui.hasUnsavedFormEdits(), true);
  ui.render({ background: true });
  assert.equal(ui.testDocument.querySelector("#view").innerHTML, "untouched-editor");
  assert.equal(control.value, "unsaved-test-secret");
  assert.equal(JSON.stringify(ui.model).includes("unsaved-test-secret"), false);
  control.value = "";
  assert.equal(ui.hasUnsavedFormEdits(), false);
});

test("semantic click dispatches the exact bound action DTO including idempotency event", async () => {
  let observed = null;
  const ui = loadUi(async (command, payload) => {
    observed = { command, payload };
    return { runtime_binding: { ...ui.model.runningApp.runtime_binding }, surface: { ...ui.model.runningApp.surface } };
  });
  ui.setTestLocale("en-US");
  const running = runtimeResult();
  running.surface.view.nodes = [
    { id: "root", parent: null, kind: { listContainer: { label: "Main" } } },
    { id: "save", parent: "root", kind: { button: { label: "Save", action: "save", style: "primary", disabled: false } } },
  ];
  ui.model.runningApp = running;
  ui.model.data = { apps: [], jobs: [], needs: [] };
  await ui.dispatchRuntimeAction("save");
  assert.equal(observed.command, "dispatch_app_action");
  assert.deepEqual(Object.keys(observed.payload).sort(), ["action", "appId", "componentSha256", "entrypoint", "eventId", "fields", "generation", "packageDigestSha256", "route", "session", "surface"].sort());
  assert.equal(observed.payload.action, "save");
  assert.match(observed.payload.eventId, /^desktop-action-/);
  assert.equal(observed.payload.generation, "7");
  assert.equal(observed.payload.session, "session-1");
  assert.equal(observed.payload.fields.length, 0);
});

test("semantic click normalizes daemon PascalCase field kinds into the exact action DTO", async () => {
  let observed = null;
  const control = { dataset: { runtimeField: "new-name" }, value: "牛奶", disabled: false };
  const ui = loadUi(async (command, payload) => {
    observed = { command, payload };
    return { runtime_binding: { ...ui.model.runningApp.runtime_binding }, surface: { ...ui.model.runningApp.surface } };
  }, "zh-CN", [control]);
  const running = runtimeResult();
  running.surface.view.nodes = [
    { id: "root", parent: null, kind: { ListContainer: { label: "购物清单" } } },
    { id: "name", parent: "root", kind: { Field: { field: "new-name", label: "商品名称", kind: "Text", value: { Empty: null }, required: true, sensitive: false, choices: [] } } },
    { id: "add", parent: "root", kind: { Button: { label: "新增", action: "add-item", style: "Primary", disabled: false } } },
  ];
  ui.model.runningApp = running;
  ui.model.data = { apps: [], jobs: [], needs: [] };
  await ui.dispatchRuntimeAction("add-item");
  assert.equal(observed.command, "dispatch_app_action");
  assert.deepEqual(JSON.parse(JSON.stringify(observed.payload.fields)), [
    { field: "new-name", value: { tag: "text", value: "牛奶" } },
  ]);
});

test("an unrelated action is not blocked by an empty required field without an action binding", async () => {
  let observed = null;
  const control = { dataset: { runtimeField: "new-name" }, value: "", disabled: false };
  const ui = loadUi(async (command, payload) => {
    observed = { command, payload };
    return { runtime_binding: { ...ui.model.runningApp.runtime_binding }, surface: { ...ui.model.runningApp.surface } };
  }, "zh-CN", [control]);
  const running = runtimeResult();
  running.surface.view.nodes = [
    { id: "root", parent: null, kind: { ListContainer: { label: "购物清单" } } },
    { id: "name", parent: "root", kind: { Field: { field: "new-name", label: "商品名称", kind: "Text", value: { Empty: null }, required: true, sensitive: false, choices: [] } } },
    { id: "toggle", parent: "root", kind: { Button: { label: "完成", action: "toggle:1", style: "Secondary", disabled: false } } },
  ];
  ui.model.runningApp = running;
  ui.model.data = { apps: [], jobs: [], needs: [] };
  await ui.dispatchRuntimeAction("toggle:1");
  assert.equal(observed.command, "dispatch_app_action");
  assert.deepEqual(JSON.parse(JSON.stringify(observed.payload.fields)), [
    { field: "new-name", value: { tag: "empty" } },
  ]);
});

test("queued development task renders a clean task-created card and collapses technical details", () => {
  const ui = loadUi();
  ui.setTestLocale("zh-CN");
  const result = completeResult();
  result.cloud_development.local_queue_receipt = { status: "queued", queue_id: "q-1" };
  result.cloud_development.local_codeagent_adapter = { started: true, provider_id: "codex" };
  const renderedZh = ui.renderMessage({ role: "assistant", result });
  assert.match(renderedZh, /class="task-created-card"/);
  assert.match(renderedZh, /应用创建任务已提交/);
  assert.match(renderedZh, /OpenAI Codex 已接单，正在本地沙箱中生成源码…/);
  assert.match(renderedZh, /data-route="builds"/);
  assert.match(renderedZh, /class="technical-details"/);

  ui.setTestLocale("en-US");
  const renderedEn = ui.renderMessage({ role: "assistant", result });
  assert.match(renderedEn, /class="task-created-card"/);
  assert.match(renderedEn, /Application creation task submitted/);
  assert.match(renderedEn, /OpenAI Codex accepted the task and is generating source code/);
  assert.match(renderedEn, /Go to Builds to view progress/);
  assert.doesNotMatch(renderedEn, /\p{Script=Han}/u);
});

test("draft requirement renders streamlined assistant-lead and informational auto-permissions", () => {
  const ui = loadUi();
  ui.setTestLocale("zh-CN");
  const draft = draftResult();
  const renderedZh = ui.renderMessage({ role: "assistant", result: draft });
  assert.match(renderedZh, /class="assistant-lead"/);
  assert.match(renderedZh, /class="technical-details"/);
  assert.doesNotMatch(renderedZh, /兼容性详情/);

  const formZh = ui.renderNeedSpecForm(draft);
  assert.match(formZh, /class="auto-permissions-card"/);
  assert.match(formZh, /AI 自动识别所需权限/);
  assert.match(formZh, /个人本地运行 · 安全沙箱隔离/);
  assert.match(formZh, /data-permission-chips/);
  assert.doesNotMatch(formZh, /<input type="checkbox" name="permissions"/);

  ui.setTestLocale("en-US");
  const renderedEn = ui.renderMessage({ role: "assistant", result: draft });
  assert.doesNotMatch(renderedEn, /\p{Script=Han}/u);
  const formEn = ui.renderNeedSpecForm(draft);
  assert.doesNotMatch(formEn, /\p{Script=Han}/u);
});

test("completed need user bubble uses friendly non-technical phrasing", () => {
  const ui = loadUi();
  ui.setTestLocale("zh-CN");
  const userMsgZh = ui.renderMessage({ role: "user", text: "确认需求并开始构建：番茄时钟" });
  assert.match(userMsgZh, /确认需求并开始构建：番茄时钟/);
  assert.doesNotMatch(userMsgZh, /宿主兼容由/);
  assert.doesNotMatch(userMsgZh, /三项授权分别记录/);

  ui.setTestLocale("en-US");
  const userMsgEn = ui.renderMessage({ role: "user", text: "Confirm requirements and build app: Pomodoro Timer" });
  assert.match(userMsgEn, /Confirm requirements and build app: Pomodoro Timer/);
  assert.doesNotMatch(userMsgEn, /\p{Script=Han}/u);
});
