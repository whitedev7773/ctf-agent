"use strict";

const state = {
  csrfToken: "",
  snapshot: null,
  filter: "all",
  query: "",
  selectedChallenge: null,
  refreshTimer: null,
  resourceTimer: null,
  codexUsageTimer: null,
  resourceRefreshing: false,
  codexUsageRefreshing: false,
  refreshing: false,
  ctfdInitialized: false,
  detailReturnChallenge: null,
  localFiles: [],
  candidateNotices: new Set(),
  writeups: new Map(),
  traces: new Map(),
  detailSignature: "",
  deleteChallengeSupported: false,
  deleteChallengeName: "",
  runtimeSettingsInitialized: false,
  runtimeSettingsDirty: false,
  runtimeModels: [],
  codexUsage: null,
};

const runtimeNumericFields = [
  "max-concurrent-challenges",
  "container-cpu-limit",
  "max-attempts-per-challenge",
  "solver-turn-timeout-seconds",
  "solver-turn-idle-timeout-seconds",
  "solver-max-runtime-seconds",
  "solver-max-steps",
  "solver-max-tokens",
  "solver-max-raw-tokens",
  "solver-cached-token-weight",
  "solver-turn-slice-tokens",
  "solver-max-estimated-cost-usd",
  "max-flag-submissions-per-challenge",
  "max-command-timeout-seconds",
  "delegate-max-agents",
  "delegate-max-concurrent",
  "delegate-max-attempts",
  "delegate-max-runtime-seconds",
  "delegate-turn-timeout-seconds",
  "delegate-turn-idle-timeout-seconds",
  "delegate-max-steps",
  "delegate-max-tokens",
  "delegate-max-raw-tokens",
  "delegate-turn-slice-tokens",
];

const runtimeFieldKey = (field) => field.replaceAll("-", "_");

const byId = (id) => document.getElementById(id);

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = text;
  return element;
}

function button(label, className, onClick, disabled = false) {
  const element = node("button", className, label);
  element.type = "button";
  element.disabled = disabled;
  element.addEventListener("click", (event) => {
    event.stopPropagation();
    onClick();
  });
  return element;
}

function formatMoney(value) {
  const number = Number(value || 0);
  if (number > 0 && number < 0.01) return `$${number.toFixed(4)}`;
  return `$${number.toFixed(2)}`;
}

function formatNumber(value) {
  return new Intl.NumberFormat("ko-KR").format(Number(value || 0));
}

function formatTokens(value) {
  const number = Number(value || 0);
  if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(1)}M`;
  if (number >= 1_000) return `${(number / 1_000).toFixed(1)}K`;
  return String(number);
}

function formatFileSize(bytes) {
  const size = Number(bytes || 0);
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

function formatResourceBytes(bytes) {
  const size = Number(bytes || 0);
  if (size >= 1024 ** 3) return `${(size / 1024 ** 3).toFixed(1)} GB`;
  if (size >= 1024 ** 2) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${Math.round(size)} B`;
}

function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds || 0));
  if (value >= 3600) return `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`;
  if (value >= 60) return `${Math.floor(value / 60)}m ${Math.floor(value % 60)}s`;
  return `${Math.floor(value)}s`;
}

function formatResetTime(unixSeconds) {
  if (!unixSeconds) return "초기화 시각 미제공";
  const reset = new Date(Number(unixSeconds) * 1000);
  if (Number.isNaN(reset.getTime())) return "초기화 시각 미제공";
  const remainingSeconds = Math.max(0, Math.floor((reset.getTime() - Date.now()) / 1000));
  const days = Math.floor(remainingSeconds / 86_400);
  const hours = Math.floor((remainingSeconds % 86_400) / 3_600);
  const minutes = Math.floor((remainingSeconds % 3_600) / 60);
  const relative = days
    ? `${days}일 ${hours}시간 후`
    : hours
      ? `${hours}시간 ${minutes}분 후`
      : remainingSeconds > 0
        ? `${Math.max(1, minutes)}분 후`
        : "곧 초기화";
  const absolute = reset.toLocaleString("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${absolute} · ${relative}`;
}

function syncLocalFiles(files) {
  const filesByName = new Map();
  for (const file of files) filesByName.set(file.name, file);
  const unique = [...filesByName.values()];
  state.localFiles = unique;

  const transfer = new DataTransfer();
  for (const file of unique) transfer.items.add(file);
  byId("challenge-files").files = transfer.files;

  const list = byId("file-list");
  list.replaceChildren();
  unique.forEach((file, index) => {
    const item = node("li", "file-item");
    const copy = node("span", "file-item-copy");
    copy.append(
      node("span", "file-item-name", file.name),
      node("span", "file-item-size", formatFileSize(file.size)),
    );
    const remove = button("×", "file-remove", () => {
      syncLocalFiles(state.localFiles.filter((_selected, selectedIndex) => selectedIndex !== index));
    });
    remove.setAttribute("aria-label", `${file.name} 제거`);
    item.append(copy, remove);
    list.append(item);
  });
}

function modelShortName(spec) {
  const lowered = spec.toLowerCase();
  if (lowered.includes("sol")) return "SOL";
  if (lowered.includes("terra")) return "TER";
  if (lowered.includes("luna")) return "LUN";
  const model = spec.split("/")[1] || spec;
  return model.replace(/[^a-z0-9]/gi, "").slice(0, 3).toUpperCase() || "AI";
}

function statusLabel(status) {
  return { active: "실행 중", solved: "해결", candidate: "후보", idle: "대기" }[status] || status;
}

function agentStatusLabel(status) {
  return {
    running: "RUNNING",
    stopping: "BUDGET STOPPING",
    compacting: "COMPACTING",
    waiting: "HANDOFF WAIT",
    won: "WINNER",
    finished: "FINISHED",
    budget_exhausted: "BUDGET STOP",
    error: "ERROR",
    quota_error: "QUOTA STOP",
    cancelled: "CANCELLED",
    candidate_found: "CANDIDATE",
    handoff_complete: "HANDOFF READY",
    gave_up: "STOPPED",
    progress_checkpoint: "CHECKPOINT",
    generating: "WRITING",
    redirecting: "REDIRECTING",
  }[status] || status;
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (options.method && options.method !== "GET") {
    headers.set("X-CTF-Dashboard-Token", state.csrfToken);
  }
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : { message: await response.text() };
  if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
  return payload;
}

async function endpointAvailable(path) {
  try {
    const response = await fetch(path, { method: "OPTIONS", cache: "no-store" });
    return response.status !== 404;
  } catch (_error) {
    return false;
  }
}

function toast(message, type = "success") {
  const item = node("div", `toast ${type}`, message);
  byId("toast-region").append(item);
  window.setTimeout(() => item.remove(), 4200);
}

function notifyCandidateReviews(snapshot) {
  const pending = snapshot.challenges.filter((challenge) => challenge.candidate_review_required);
  document.title = pending.length ? `(${pending.length}) CTF 후보 검토 필요` : "CTF Agent";
  for (const challenge of pending) {
    const key = `${challenge.name}:${challenge.candidate}`;
    if (state.candidateNotices.has(key)) continue;
    state.candidateNotices.add(key);
    const message = `${challenge.name}: Flag 후보 검토가 필요합니다 — ${challenge.candidate}`;
    toast(message);
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification("CTF Agent 후보 검토 필요", { body: message });
    }
  }
}

function setConnection(online) {
  const pill = byId("connection-pill");
  pill.classList.toggle("online", online);
  pill.classList.toggle("offline", !online);
  byId("connection-label").textContent = online ? "LIVE" : "연결 끊김";
}

function renderOverview() {
  const snapshot = state.snapshot;
  if (!snapshot) return;
  const { stats } = snapshot;
  byId("stat-solved").textContent = `${stats.solved}`;
  byId("stat-total").textContent = `전체 ${stats.total} 문제 · 문서화 ${stats.documented || 0}`;
  byId("stat-swarms").textContent = `${stats.active_swarms}`;
  byId("stat-capacity").textContent = `동시 실행 한도 ${snapshot.max_concurrent_challenges}`;
  byId("stat-agents").textContent = `${stats.active_agents}`;
  byId("stat-agent-total").textContent = `생성된 agent ${stats.total_agents}`;
  byId("stat-cost").textContent = formatMoney(stats.cost_usd);
  byId("stat-tokens").textContent = `${formatTokens(stats.tokens)} raw · ${formatTokens(stats.effective_tokens)} effective`;
  if (state.codexUsage) renderCodexUsage(state.codexUsage);
  renderGlobalResources(snapshot.resources || {});
  const experience = snapshot.experience || {};
  byId("experience-records").textContent = formatNumber(experience.record_count || 0);
  byId("experience-size").textContent = formatResourceBytes(experience.total_bytes || 0);
  byId("experience-copy").textContent = experience.updated_at
    ? `${experience.category_count || 0}개 카테고리 · 마지막 승격 ${new Date(experience.updated_at).toLocaleString("ko-KR")}`
    : "검증된 풀이에서 승격된 경험을 다음 문제의 모든 솔버가 읽습니다.";
  const modePill = byId("mode-pill");
  const modes = {
    standalone: ["STANDALONE · 로컬 후보", "dry"],
    dry_run: ["DRY RUN · 제출 안 함", "dry"],
    live: ["LIVE SUBMIT · 자동 제출", "live"],
  };
  const [modeText, modeClass] = modes[snapshot.submission_mode] || modes.dry_run;
  modePill.textContent = modeText;
  modePill.className = `mode-pill ${modeClass}`;
  const restartPill = byId("restart-pill");
  restartPill.hidden = !snapshot.restart_required;
  restartPill.title = snapshot.restart_required
    ? "실행 후 소스 또는 .env가 변경되었습니다. 현재 풀이가 끝난 뒤 coordinator를 재시작하세요."
    : "";

  const ctfd = snapshot.ctfd || {};
  const sourceBadge = byId("ctfd-source-badge");
  sourceBadge.textContent = !ctfd.configured ? "독립 모드" : ctfd.connected ? "연결됨" : "오류";
  sourceBadge.className = `source-badge ${ctfd.connected ? "connected" : ctfd.configured ? "error" : "local"}`;
  byId("ctfd-status-copy").textContent = !ctfd.configured
    ? "CTFd는 선택 사항입니다. 현재 로컬 문제만 사용합니다."
    : ctfd.connected
      ? `${ctfd.url} · 문제 목록을 주기적으로 동기화합니다.`
      : `${ctfd.url} · ${ctfd.error || "연결을 확인할 수 없습니다."}`;
  if (!state.ctfdInitialized) {
    byId("ctfd-url").value = ctfd.url || "";
    state.ctfdInitialized = true;
  }
  renderRuntimeSettings(snapshot.runtime_settings || {});
}

function renderCodexUsage(usage) {
  const badge = byId("codex-usage-badge");
  const copy = byId("codex-usage-copy");
  const container = byId("codex-usage-windows");
  if (!usage?.available) {
    badge.textContent = "사용 불가";
    badge.className = "source-badge error";
    copy.textContent = "로그인된 Codex 계정의 사용 한도를 확인할 수 없습니다.";
    const empty = node("div", "codex-usage-empty");
    empty.append(
      node("strong", "", "사용량을 불러오지 못했습니다."),
      node("span", "", usage?.error || "잠시 후 다시 시도해 주세요."),
    );
    container.replaceChildren(empty);
    return;
  }

  const plan = usage.plan_type ? String(usage.plan_type).toUpperCase() : "CODEX";
  badge.textContent = plan;
  badge.className = "source-badge connected";
  const details = [];
  if (Number.isInteger(usage.available_resets)) {
    details.push(`사용 가능 리셋 ${usage.available_resets}개`);
  }
  if (usage.credits?.unlimited) details.push("크레딧 무제한");
  else if (usage.credits?.balance) details.push(`크레딧 ${usage.credits.balance}`);
  copy.textContent = details.length
    ? `계정 전체 사용량 · ${details.join(" · ")}`
    : "로그인된 계정 전체의 Codex 사용량입니다.";

  const windows = Array.isArray(usage.windows) ? [...usage.windows] : [];
  const order = { five_hour: 0, weekly: 1 };
  windows.sort((left, right) => (order[left.kind] ?? 2) - (order[right.kind] ?? 2));
  if (!windows.length) {
    const empty = node("div", "codex-usage-empty");
    empty.append(
      node("strong", "", "표시할 사용 한도 창이 없습니다."),
      node("span", "", "현재 인증 방식이나 플랜에서는 사용률이 제공되지 않을 수 있습니다."),
    );
    container.replaceChildren(empty);
    return;
  }

  const fragment = document.createDocumentFragment();
  for (const windowUsage of windows) {
    const used = Math.max(0, Math.min(100, Number(windowUsage.used_percent || 0)));
    const severity = used >= 90 ? " is-critical" : used >= 70 ? " is-warning" : "";
    const card = node("article", `codex-limit-card${severity}`);
    const head = node("div", "codex-limit-card-head");
    head.append(
      node("span", "", `${windowUsage.label || "사용량"} 한도`),
      node("strong", "", `${Math.round(used)}%`),
    );
    const bar = node("div", "codex-limit-bar");
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", `${windowUsage.label || "Codex"} 사용량`);
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
    bar.setAttribute("aria-valuenow", String(Math.round(used)));
    const fill = node("span");
    fill.style.width = `${used}%`;
    bar.append(fill);
    const meta = node("div", "codex-limit-meta");
    meta.append(node("span", "", `${Math.round(100 - used)}% 남음`));
    const reset = node("time", "", formatResetTime(windowUsage.resets_at));
    if (windowUsage.resets_at) {
      reset.dateTime = new Date(Number(windowUsage.resets_at) * 1000).toISOString();
    }
    meta.append(reset);
    card.append(head, bar, meta);
    fragment.append(card);
  }
  container.replaceChildren(fragment);
}

async function refreshCodexUsage({ force = false } = {}) {
  if (state.codexUsageRefreshing) return;
  state.codexUsageRefreshing = true;
  const control = byId("codex-usage-refresh");
  control.disabled = true;
  control.setAttribute("aria-busy", "true");
  try {
    state.codexUsage = await api(`/api/codex/usage${force ? "?refresh=1" : ""}`);
    renderCodexUsage(state.codexUsage);
  } catch (error) {
    state.codexUsage = { available: false, error: error.message, windows: [] };
    renderCodexUsage(state.codexUsage);
    if (force) toast(error.message, "error");
  } finally {
    state.codexUsageRefreshing = false;
    control.disabled = false;
    control.removeAttribute("aria-busy");
  }
}

function splitModelSpec(spec, fallbackBase, fallbackEffort) {
  const parts = String(spec || "").split("/");
  return {
    base: parts.length >= 2 ? parts.slice(0, 2).join("/") : fallbackBase,
    effort: parts.length >= 3 ? parts[2] : fallbackEffort,
  };
}

function selectValue(select, value) {
  if (![...select.options].some((option) => option.value === value)) {
    select.add(new Option(value, value));
  }
  select.value = value;
}

function renderRuntimeSettings(settings) {
  if (state.runtimeSettingsInitialized && state.runtimeSettingsDirty) return;
  const models = Array.isArray(settings.models) && settings.models.length
    ? [...settings.models]
    : ["codex/gpt-5.6-sol/high"];
  const primary = splitModelSpec(models[0], "codex/gpt-5.6-sol", "high");
  const delegate = splitModelSpec(
    settings.delegate_model_spec,
    "codex/gpt-5.6-luna",
    "low",
  );
  state.runtimeModels = models;
  selectValue(byId("runtime-primary-model"), primary.base);
  selectValue(byId("runtime-primary-effort"), primary.effort);
  selectValue(byId("runtime-delegate-model"), delegate.base);
  selectValue(byId("runtime-delegate-effort"), delegate.effort);
  byId("runtime-container-memory-limit").value = settings.container_memory_limit ?? "4g";
  byId("runtime-dynamic-delegation-enabled").checked = Boolean(
    settings.dynamic_delegation_enabled,
  );
  for (const field of runtimeNumericFields) {
    const input = byId(`runtime-${field}`);
    const value = settings[runtimeFieldKey(field)];
    if (input && value !== undefined) input.value = value;
  }
  state.runtimeSettingsInitialized = true;
  state.runtimeSettingsDirty = false;
  const badge = byId("runtime-settings-badge");
  badge.textContent = "저장됨";
  badge.className = "source-badge connected";
  const active = Number(state.snapshot?.stats?.active_swarms || 0);
  byId("runtime-settings-copy").textContent = active
    ? `실행 중인 swarm ${active}개는 시작 당시 설정을 유지합니다.`
    : "저장하면 이후 시작하는 swarm과 solver에 적용됩니다.";
}

function runtimeSettingsFromForm() {
  const models = state.runtimeModels.length ? [...state.runtimeModels] : [];
  const primary = `${byId("runtime-primary-model").value}/${byId("runtime-primary-effort").value}`;
  if (models.length) models[0] = primary;
  else models.push(primary);
  const result = {
    models,
    container_memory_limit: byId("runtime-container-memory-limit").value.trim(),
    dynamic_delegation_enabled: byId("runtime-dynamic-delegation-enabled").checked,
    delegate_model_spec: `${byId("runtime-delegate-model").value}/${byId("runtime-delegate-effort").value}`,
  };
  for (const field of runtimeNumericFields) {
    result[runtimeFieldKey(field)] = Number(byId(`runtime-${field}`).value);
  }
  return result;
}

function markRuntimeSettingsDirty() {
  state.runtimeSettingsDirty = true;
  const badge = byId("runtime-settings-badge");
  badge.textContent = "저장 안 됨";
  badge.className = "source-badge dirty";
  byId("runtime-settings-copy").textContent = "변경사항은 저장 전까지 적용되지 않습니다.";
}

async function submitRuntimeSettings(path, body, control) {
  const originalLabel = control.textContent;
  control.disabled = true;
  control.setAttribute("aria-busy", "true");
  control.textContent = "저장 중…";
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(body) });
    toast(result.message || "실행 설정을 저장했습니다.");
    state.runtimeSettingsDirty = false;
    state.runtimeSettingsInitialized = false;
    await refresh();
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  } finally {
    control.disabled = false;
    control.removeAttribute("aria-busy");
    control.textContent = originalLabel;
  }
}

function renderGlobalResources(resources) {
  byId("resource-containers").textContent = formatNumber(resources.container_count || 0);
  byId("resource-cpu").textContent = `${Number(resources.cpu_percent || 0).toFixed(1)}%`;
  const memory = formatResourceBytes(resources.memory_bytes || 0);
  const limit = resources.memory_limit_bytes ? ` / ${formatResourceBytes(resources.memory_limit_bytes)}` : "";
  byId("resource-memory").textContent = `${memory}${limit}`;
  byId("resource-pids").textContent = formatNumber(resources.pids || 0);
  byId("resource-network").textContent = formatResourceBytes(
    Number(resources.network_rx_bytes || 0) + Number(resources.network_tx_bytes || 0),
  );
  byId("resource-updated").textContent = resources.stale_count
    ? `${resources.stale_count}개 샘플 지연`
    : "1초 간격으로 갱신";
}

function filteredChallenges() {
  if (!state.snapshot) return [];
  const query = state.query.trim().toLocaleLowerCase("ko-KR");
  return state.snapshot.challenges.filter((challenge) => {
    const matchesFilter = state.filter === "all" || challenge.status === state.filter;
    const haystack = `${challenge.name} ${challenge.category}`.toLocaleLowerCase("ko-KR");
    return matchesFilter && (!query || haystack.includes(query));
  });
}

function currentChallenge(name) {
  return state.snapshot?.challenges.find((challenge) => challenge.name === name);
}

function createChallengeRow(challengeName) {
  const row = node("tr");
  row.dataset.challengeName = challengeName;
  row.tabIndex = 0;
  row.addEventListener("click", () => openChallengeDetail(row.dataset.challengeName));
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openChallengeDetail(row.dataset.challengeName);
    }
  });

  const nameCell = node("td", "challenge-name-cell");
  const statusCell = node("td", "challenge-status-cell");
  const agentsCell = node("td", "challenge-agents-cell");
  const progressCell = node("td", "progress-copy challenge-progress-cell");
  const costCell = node("td", "cost-cell challenge-cost-cell");
  const actionCell = node("td", "challenge-action-cell");
  const action = node("button", "row-action");
  action.type = "button";
  action.addEventListener("click", (event) => {
    event.stopPropagation();
    const challenge = currentChallenge(row.dataset.challengeName);
    if (!challenge) return;
    if (challenge.active) stopChallenge(challenge.name);
    else if (challenge.solved) openChallengeDetail(challenge.name);
    else spawnChallenge(challenge.name);
  });
  actionCell.append(action);
  row.append(nameCell, statusCell, agentsCell, progressCell, costCell, actionCell);
  return row;
}

function updateChallengeRow(row, challenge) {
  row.dataset.challengeName = challenge.name;
  row.setAttribute("aria-label", `${challenge.name} 상세 보기`);

  const nameCell = row.querySelector(".challenge-name-cell");
  nameCell.replaceChildren(node("span", "challenge-name", challenge.name));
  const meta = node("span", "challenge-meta");
  meta.append(node("span", "", challenge.category));
  meta.append(node("span", "", `${formatNumber(challenge.value)} pts`));
  if (challenge.solves) meta.append(node("span", "", `${formatNumber(challenge.solves)} solves`));
  if (challenge.documented) meta.append(node("span", "documented-badge", "DOCUMENTED"));
  nameCell.append(meta);

  const statusCell = row.querySelector(".challenge-status-cell");
  statusCell.replaceChildren(node("span", `status-badge ${challenge.status}`, statusLabel(challenge.status)));

  const agentsCell = row.querySelector(".challenge-agents-cell");
  const stack = node("div", "agent-stack");
  for (const agent of challenge.agents.slice(0, 4)) {
    const chip = node("span", `agent-chip ${agent.status}`, modelShortName(agent.model_spec));
    chip.title = `${agent.model_spec} · ${agentStatusLabel(agent.status)}`;
    stack.append(chip);
  }
  if (!challenge.agents.length) stack.append(node("span", "progress-copy", "아직 없음"));
  agentsCell.replaceChildren(stack);

  const steps = challenge.agents.reduce((sum, agent) => sum + Number(agent.steps || 0), 0);
  const progressCell = row.querySelector(".challenge-progress-cell");
  const resources = challenge.resources || {};
  progressCell.replaceChildren(node("strong", "", formatNumber(steps)), document.createTextNode(" steps"));
  progressCell.append(
    node(
      "span",
      "row-resource-copy",
      resources.container_count
        ? ` · ${Number(resources.cpu_percent || 0).toFixed(1)}% CPU · ${formatResourceBytes(resources.memory_bytes || 0)}`
        : "",
    ),
  );

  row.querySelector(".challenge-cost-cell").textContent = formatMoney(challenge.cost_usd);
  const action = row.querySelector(".row-action");
  action.textContent = challenge.active ? "중단" : challenge.solved ? "보기" : "시작";
  action.setAttribute(
    "aria-label",
    challenge.active
      ? `${challenge.name} 풀이 중단`
      : challenge.solved
        ? `${challenge.name} 상세 보기`
        : `${challenge.name} 풀이 시작`,
  );
}

function renderRows() {
  const tbody = byId("challenge-rows");
  const challenges = filteredChallenges();
  byId("empty-state").hidden = challenges.length !== 0;
  const hasRegisteredChallenges = Boolean(state.snapshot?.challenges.length);
  byId("empty-title").textContent = hasRegisteredChallenges
    ? "조건에 맞는 문제가 없습니다"
    : "아직 등록된 문제가 없습니다";
  byId("empty-copy").textContent = hasRegisteredChallenges
    ? "검색어나 상태 필터를 바꿔보세요."
    : "아래에서 CTFd를 연결하거나 로컬 문제를 추가하세요.";

  const rowsByName = new Map(
    [...tbody.querySelectorAll("tr")].map((row) => [row.dataset.challengeName, row]),
  );
  const visibleNames = new Set(challenges.map((challenge) => challenge.name));
  for (const row of rowsByName.values()) {
    if (!visibleNames.has(row.dataset.challengeName)) row.remove();
  }

  challenges.forEach((challenge, index) => {
    const row = rowsByName.get(challenge.name) || createChallengeRow(challenge.name);
    updateChallengeRow(row, challenge);
    const current = tbody.children[index];
    if (current !== row) tbody.insertBefore(row, current || null);
  });

  while (tbody.children.length > challenges.length) {
    tbody.lastElementChild.remove();
  }
}

function metric(label, value) {
  const item = node("div", "detail-metric");
  item.append(node("span", "", label), node("strong", "", value));
  return item;
}

function detailSection(title, sideText = "") {
  const section = node("section", "detail-section");
  const heading = node("div", "detail-section-title");
  heading.append(node("h3", "", title));
  if (sideText) heading.append(node("span", "", sideText));
  section.append(heading);
  return section;
}

function sparkline(history, key) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "sparkline");
  svg.setAttribute("viewBox", "0 0 240 34");
  svg.setAttribute("aria-hidden", "true");
  const values = (Array.isArray(history) ? history : [])
    .map((item) => Number(item?.[key] || 0))
    .slice(-60);
  const maximum = Math.max(1, ...values);
  const points = values.map((value, index) => {
    const x = values.length <= 1 ? 0 : index / (values.length - 1) * 240;
    const y = 32 - value / maximum * 30;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", points.join(" "));
  svg.append(line);
  return svg;
}

function buildResourcePanel(challenge) {
  const resources = challenge.resources || {};
  const section = detailSection("실시간 Docker 리소스", `${resources.container_count || 0} CONTAINERS`);
  section.id = "detail-resource-section";
  const grid = node("div", "resource-grid");
  grid.append(
    metric("CPU", `${Number(resources.cpu_percent || 0).toFixed(1)}%`),
    metric("MEMORY", formatResourceBytes(resources.memory_bytes || 0)),
    metric("PROCESSES", formatNumber(resources.pids || 0)),
  );
  section.append(grid);

  for (const agent of challenge.agents) {
    const resource = agent.resource || {};
    if (!Object.keys(resource).length) continue;
    const item = node("article", "resource-agent");
    const head = node("div", "resource-agent-head");
    head.append(
      node("span", "", `${agent.role?.toUpperCase() || "SOLVER"} · ${resource.container_id || "starting"}`),
      node("span", "", resource.stale ? "STALE" : String(resource.status || "UNKNOWN").toUpperCase()),
    );
    const memoryPercent = Math.min(100, Math.max(0, Number(resource.memory_percent || 0)));
    const bar = node("div", "resource-bar");
    const fill = node("span");
    fill.style.width = `${memoryPercent}%`;
    bar.append(fill);
    const details = node("div", "resource-agent-meta");
    details.append(
      node("span", "", `${Number(resource.cpu_percent || 0).toFixed(1)}% CPU`),
      node("span", "", `${formatResourceBytes(resource.memory_bytes || 0)} / ${formatResourceBytes(resource.memory_limit_bytes || 0)}`),
      node("span", "", `${formatNumber(resource.pids || 0)} PIDs`),
      node("span", "", `RX ${formatResourceBytes(resource.network_rx_bytes || 0)}`),
      node("span", "", `TX ${formatResourceBytes(resource.network_tx_bytes || 0)}`),
      node("span", "", formatDuration(resource.uptime_seconds || 0)),
    );
    item.append(head, sparkline(resource.history, "cpu_percent"), bar, details);
    if (resource.error) item.append(node("p", "agent-stop-reason", resource.error));
    section.append(item);
  }
  return section;
}

function buildWriteupSection(challenge) {
  const writeup = challenge.writeup || {};
  const isGenerating = writeup.status === "generating" && writeup.active !== false;
  const writeupStatusLabel = {
    generating: "생성 중",
    complete: "완료",
    needs_attention: "보강 필요",
    pending: "대기",
    not_started: "미생성",
    invalid: "오류",
  }[writeup.status] || String(writeup.status || "미생성");
  const section = detailSection("Writeup & evidence", writeupStatusLabel);
  const status = node("div", `writeup-status${isGenerating ? " is-generating" : ""}`);
  const copy = node("div");
  copy.append(
    node(
      "strong",
      "",
      isGenerating
        ? "라이트업 생성 중"
        : challenge.documented
          ? "DOCUMENTED"
          : challenge.solved
            ? "WRITEUP PENDING"
            : "SOLVE IN PROGRESS",
    ),
    node(
      "span",
      "",
      isGenerating
        ? `${writeup.model_spec || "AI"}가 한국어 최종본과 스크린샷을 구성하고 있습니다.`
        : `${(writeup.reproducers || []).length} reproducers · ${(writeup.screenshots || []).length} screenshots`,
    ),
  );
  const actions = node("div", "writeup-actions");
  if (writeup.writeup_path) {
    actions.append(button("Writeup 보기", "secondary-button", () => loadWriteup(challenge.name)));
  }
  if (challenge.solved) {
    const requestLabel = isGenerating
      ? "라이트업 생성 중…"
      : challenge.documented
      ? "라이트업 재생성"
      : writeup.status === "needs_attention" || writeup.status === "pending"
        ? "라이트업 생성 재개"
        : "라이트업 생성 요청";
    const request = button(
      requestLabel,
      "primary-button",
      () => requestWriteup(challenge.name, request),
      isGenerating,
    );
    if (isGenerating) request.setAttribute("aria-busy", "true");
    actions.append(request);
  }
  status.append(copy, actions);
  section.append(status);
  if (writeup.issues?.length) {
    const issues = node("ul", "writeup-issues");
    writeup.issues.forEach((issue) => issues.append(node("li", "", issue)));
    section.append(issues);
  }
  const loaded = state.writeups.get(challenge.name);
  if (loaded) section.append(node("pre", "writeup-preview", loaded.content || ""));
  if (writeup.screenshots?.length) {
    const gallery = node("div", "screenshot-gallery");
    for (const shot of writeup.screenshots) {
      const figure = node("figure");
      const img = node("img");
      const params = new URLSearchParams({ challenge: challenge.name, path: shot.path });
      img.src = `/api/artifact?${params}`;
      img.alt = shot.caption || "Writeup evidence";
      img.loading = "lazy";
      figure.append(img, node("figcaption", "", shot.caption || shot.path));
      gallery.append(figure);
    }
    section.append(gallery);
  }
  return section;
}

async function loadWriteup(challengeName) {
  try {
    const params = new URLSearchParams({ challenge: challengeName });
    state.writeups.set(challengeName, await api(`/api/writeup?${params}`));
    renderDetailPage({ preserveScroll: true });
  } catch (error) {
    toast(error.message, "error");
  }
}

async function requestWriteup(challengeName, control) {
  const success = await runCommand(
    "/api/control/request-writeup",
    { challenge: challengeName },
    control,
  );
  if (!success) return;
  state.writeups.delete(challengeName);
  renderDetailPage({ preserveScroll: true });
}

function detailRenderSignature(challenge) {
  if (!challenge) return "";
  const { resources: _resources, agents = [], ...stableChallenge } = challenge;
  return JSON.stringify({
    ...stableChallenge,
    agents: agents.map(({ resource: _resource, ...agent }) => agent),
  });
}

function captureDetailViewState(content) {
  const scrolling = document.scrollingElement;
  const maximum = Math.max(0, (scrolling?.scrollHeight || 0) - window.innerHeight);
  return {
    left: window.scrollX,
    top: window.scrollY,
    atBottom: maximum - window.scrollY < 32,
    fields: [...content.querySelectorAll("input, textarea")].map((field) => ({
      name: field.name,
      value: field.value,
    })),
    nestedScroll: [...content.querySelectorAll(".writeup-preview, .trace-output")].map((item) => ({
      className: item.className,
      left: item.scrollLeft,
      top: item.scrollTop,
    })),
  };
}

function restoreDetailViewState(content, saved) {
  const fields = [...content.querySelectorAll("input, textarea")];
  saved.fields.forEach((field, index) => {
    const current = fields[index];
    if (current && current.name === field.name) current.value = field.value;
  });

  const nestedScroll = [...content.querySelectorAll(".writeup-preview, .trace-output")];
  saved.nestedScroll.forEach((item, index) => {
    const current = nestedScroll[index];
    if (!current || current.className !== item.className) return;
    current.scrollLeft = item.left;
    current.scrollTop = item.top;
  });

  const restoreWindow = () => {
    const scrolling = document.scrollingElement;
    const maximum = Math.max(0, (scrolling?.scrollHeight || 0) - window.innerHeight);
    window.scrollTo(saved.left, saved.atBottom ? maximum : Math.min(saved.top, maximum));
  };
  restoreWindow();
  window.requestAnimationFrame(restoreWindow);
}

function renderDetailPage({ preserveScroll = false } = {}) {
  const challenge = state.snapshot?.challenges.find((item) => item.name === state.selectedChallenge);
  if (!challenge) {
    closeChallengeDetail({ replaceHistory: true });
    return;
  }
  const isStandalone = state.snapshot.submission_mode === "standalone";

  document.title = `${challenge.name} · CTF Agent`;
  byId("detail-title").textContent = challenge.name;
  byId("detail-category").textContent = `${challenge.category.toUpperCase()} · ${formatNumber(challenge.value)} PTS`;
  const content = byId("detail-content");
  const savedView = preserveScroll ? captureDetailViewState(content) : null;
  const nextContent = document.createDocumentFragment();

  const overview = node("div", "detail-overview");
  const summary = node("div", "detail-summary");
  summary.append(
    metric("STATUS", statusLabel(challenge.status)),
    metric("AGENTS", String(challenge.agents.length)),
    metric("COST", formatMoney(challenge.cost_usd)),
    metric("WRITEUP", challenge.documented ? "DONE" : challenge.solved ? "PENDING" : "WAITING"),
  );
  const controls = node("div", "control-row detail-actions");
  if (challenge.active) {
    controls.append(button("풀이 중단", "danger-button", () => stopChallenge(challenge.name)));
  } else if (!challenge.solved) {
    controls.append(button("SOL 풀이 시작", "primary-button", () => spawnChallenge(challenge.name)));
  }
  controls.append(button("상태 새로고침", "secondary-button", () => refresh({ renderSelected: true })));
  const deleteButton = button(
    "문제 삭제",
    "destructive-button",
    () => openDeleteChallengeDialog(challenge.name),
    !state.deleteChallengeSupported,
  );
  if (!state.deleteChallengeSupported) {
    deleteButton.title = "새 삭제 API를 사용하려면 coordinator를 재시작해야 합니다.";
  }
  controls.append(deleteButton);
  overview.append(summary, controls);
  nextContent.append(overview);

  const layout = node("div", "detail-layout");
  const mainColumn = node("div", "detail-main-column");
  const sideColumn = node("aside", "detail-side-column");
  layout.append(mainColumn, sideColumn);
  nextContent.append(layout);

  if (challenge.flag) {
    const flagSection = detailSection(isStandalone ? "로컬 Flag 결과" : "확인된 Flag");
    flagSection.append(node("div", "flag-value", challenge.flag));
    mainColumn.append(flagSection);
  }

  if (challenge.candidate) {
    const candidateSection = detailSection("검증되지 않은 Flag 후보");
    candidateSection.append(node("div", "flag-value", challenge.candidate));
    if (challenge.candidate_review_required) {
      const reviewControls = node("div", "control-row");
      const accept = button("정답으로 확인", "primary-button", async () => {
        if (!window.confirm(`${challenge.candidate}를 로컬 정답으로 확정할까요?`)) return;
        await runCommand(
          "/api/control/review-candidate",
          { challenge: challenge.name, flag: challenge.candidate, accepted: true },
          accept,
        );
      });
      const reject = button("오답으로 거부", "danger-button", async () => {
        if (!window.confirm(`${challenge.candidate}를 오답 후보로 제거할까요?`)) return;
        await runCommand(
          "/api/control/review-candidate",
          { challenge: challenge.name, flag: challenge.candidate, accepted: false },
          reject,
        );
      });
      reviewControls.append(accept, reject);
      candidateSection.append(reviewControls);
    }
    mainColumn.append(candidateSection);
  }

  mainColumn.append(buildWriteupSection(challenge));
  sideColumn.append(buildResourcePanel(challenge));

  const noteMetadata = /^(source agent|stop reason|attempt|tool steps|original handoff|handoff audit|requested deliverable):/i;
  const approachNotes = (Array.isArray(challenge.approach_notes) ? challenge.approach_notes : [])
    .filter((note) => note?.text && !noteMetadata.test(note.text.trim()));
  const notesSection = detailSection("지금까지의 접근 노트", `${approachNotes.length} NOTES`);
  if (!approachNotes.length) {
    notesSection.append(node("p", "approach-notes-empty", "아직 기록된 접근이 없습니다. 풀이가 진행되면 핵심 가설과 확인 결과가 여기에 요약됩니다."));
  } else {
    const notesList = node("ol", "approach-notes");
    for (const note of approachNotes) {
      const item = node("li", "approach-note");
      item.append(
        node("p", "approach-note-text", note.text || "기록된 내용 없음"),
        node("span", "approach-note-source", note.source || "solver"),
      );
      notesList.append(item);
    }
    notesSection.append(notesList);
  }
  mainColumn.append(notesSection);

  const agentsSection = detailSection("Solver agents", `${challenge.agents.length} AGENTS`);
  if (!challenge.agents.length) {
    agentsSection.append(node("p", "agent-findings", "SOL 풀이를 시작하면 주 solver와 동적 하위 agent 상태가 여기에 표시됩니다."));
  }
  for (const agent of challenge.agents) {
    const card = node("article", "agent-card");
    card.dataset.challengeName = challenge.name;
    card.dataset.agentModel = agent.model_spec;
    const header = node("div", "agent-card-header");
    header.append(
      node("span", "agent-model", `${agent.role ? agent.role.toUpperCase() + " · " : ""}${agent.model_spec}`),
      node("span", `status-badge ${agent.status === "running" ? "active" : agent.status === "won" ? "solved" : ""}`, agentStatusLabel(agent.status)),
    );
    if (agent.role_title) header.title = agent.role_title;
    const stats = node("div", "agent-stats");
    stats.append(
      node("span", "", `${formatNumber(agent.steps)} steps`),
      node("span", "", `${formatTokens(agent.input_tokens)} in`),
      node("span", "", `${formatTokens(agent.output_tokens)} out`),
      node("span", "", `${formatTokens(agent.effective_tokens)} / ${formatTokens(agent.effective_token_limit)} effective`),
    );
    if (["running", "redirecting"].includes(agent.status) && agent.idle_limit_seconds) {
      stats.append(
        node(
          "span",
          "",
          agent.tool_call_active
            ? "tool active"
            : `idle ${formatDuration(agent.idle_seconds || 0)} / ${formatDuration(agent.idle_limit_seconds)}`,
        ),
      );
    }
    stats.append(node("span", "", formatMoney(agent.cost_usd)));
    card.append(header, stats);
    if (agent.skill_path) card.append(node("p", "agent-skill", `Skill: ${agent.skill_path}`));
    if (agent.findings) card.append(node("p", "agent-findings", agent.findings));
    if (agent.stop_reason) card.append(node("p", "agent-stop-reason", `종료 사유: ${agent.stop_reason}`));
    if (agent.workspace_path) card.append(node("p", "agent-workspace", `산출물: ${agent.workspace_path}`));
    const traceButton = button("최근 trace 보기 →", "trace-button", () => loadTrace(challenge.name, agent.model_spec, card));
    card.append(traceButton);
    const cachedTrace = state.traces.get(challenge.name)?.get(agent.model_spec);
    if (cachedTrace !== undefined) card.append(node("pre", "trace-output", cachedTrace));
    agentsSection.append(card);
  }
  sideColumn.append(agentsSection);

  if (challenge.active) {
    const broadcastSection = detailSection("전체 solver에 힌트 전달");
    const form = node("form", "detail-form");
    const textarea = node("textarea");
    textarea.name = "message";
    textarea.maxLength = 4000;
    textarea.placeholder = "이 문제를 푸는 모든 solver에게 전달할 기술적 힌트";
    textarea.required = true;
    const send = node("button", "primary-button", "Broadcast");
    send.type = "submit";
    form.append(textarea, send);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await runCommand("/api/control/broadcast", { challenge: challenge.name, message: textarea.value }, send);
      textarea.value = "";
    });
    broadcastSection.append(form);
    sideColumn.append(broadcastSection);
  }

  if (!challenge.solved) {
    const submitSection = detailSection(isStandalone ? "로컬 Flag 후보 기록" : state.snapshot.no_submit ? "Flag 후보 기록" : "Flag 수동 제출");
    const form = node("form", "detail-form");
    const input = node("input");
    input.name = "flag";
    input.maxLength = 2000;
    input.placeholder = "TEAM{candidate_flag}";
    input.required = true;
    const send = node("button", "primary-button", isStandalone ? "로컬 후보로 기록" : state.snapshot.no_submit ? "Dry-run 확인" : "CTFd에 제출");
    send.type = "submit";
    form.append(input, send);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!state.snapshot.no_submit && !window.confirm(`${challenge.name}에 이 flag를 제출할까요?`)) return;
      await runCommand("/api/control/submit", { challenge: challenge.name, flag: input.value }, send);
      input.value = "";
    });
    submitSection.append(form);
    sideColumn.append(submitSection);
  }

  content.replaceChildren(nextContent);
  state.detailSignature = detailRenderSignature(challenge);
  if (savedView) restoreDetailViewState(content, savedView);
}

function challengeUrl(name = "") {
  const url = new URL(window.location.href);
  if (name) url.searchParams.set("challenge", name);
  else url.searchParams.delete("challenge");
  return url;
}

function transitionAppView(showDetail, { animate = true, returnChallenge = "" } = {}) {
  const dashboard = byId("dashboard-view");
  const detail = byId("detail-page");
  document.documentElement.dataset.navDirection = showDetail ? "forward" : "back";
  const update = () => {
    dashboard.hidden = showDetail;
    detail.hidden = !showDetail;
    window.scrollTo(0, 0);
    if (showDetail) {
      byId("detail-back").focus({ preventScroll: true });
      return;
    }
    const returnRow = [...byId("challenge-rows").querySelectorAll("tr")]
      .find((row) => row.dataset.challengeName === returnChallenge);
    if (returnRow) returnRow.focus({ preventScroll: true });
  };

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (animate && !reduceMotion && typeof document.startViewTransition === "function") {
    try {
      const transition = document.startViewTransition(update);
      transition.finished.catch(() => {});
      return;
    } catch (_error) {
      // Fall through to the CSS animation when another transition is active.
    }
  }

  update();
  if (animate && !reduceMotion) {
    const target = showDetail ? detail : dashboard;
    target.classList.remove("page-enter");
    void target.offsetWidth;
    target.classList.add("page-enter");
    target.addEventListener("animationend", () => target.classList.remove("page-enter"), { once: true });
  }
}

function openChallengeDetail(name, { updateHistory = true, animate = true } = {}) {
  if (!currentChallenge(name)) return;
  if (!state.selectedChallenge) state.detailReturnChallenge = name;
  state.selectedChallenge = name;
  renderDetailPage();
  if (updateHistory) {
    window.history.pushState({ ctfView: "detail", challenge: name }, "", challengeUrl(name));
  }
  transitionAppView(true, { animate });
}

function closeChallengeDetail({ fromHistory = false, replaceHistory = false, animate = true } = {}) {
  if (!fromHistory && !replaceHistory && window.history.state?.ctfView === "detail") {
    window.history.back();
    return;
  }

  const returnChallenge = state.detailReturnChallenge || state.selectedChallenge || "";
  state.selectedChallenge = null;
  state.detailReturnChallenge = null;
  state.detailSignature = "";
  document.title = "CTF Agent Command Center";
  if (replaceHistory || (!fromHistory && new URL(window.location.href).searchParams.has("challenge"))) {
    window.history.replaceState({ ctfView: "dashboard" }, "", challengeUrl());
  }
  transitionAppView(false, { animate, returnChallenge });
}

function syncViewFromLocation({ animate = true } = {}) {
  const requested = new URL(window.location.href).searchParams.get("challenge");
  if (requested && currentChallenge(requested)) {
    openChallengeDetail(requested, { updateHistory: false, animate });
    return;
  }
  closeChallengeDetail({ fromHistory: true, replaceHistory: Boolean(requested), animate });
}

async function loadTrace(challenge, model, card) {
  let challengeTraces = state.traces.get(challenge);
  if (!challengeTraces) {
    challengeTraces = new Map();
    state.traces.set(challenge, challengeTraces);
  }
  const loadingText = "trace를 불러오는 중…";
  challengeTraces.set(model, loadingText);
  let output = card.querySelector(".trace-output");
  if (!output) {
    output = node("pre", "trace-output", loadingText);
    card.append(output);
  } else {
    output.textContent = loadingText;
  }
  let traceText;
  try {
    const query = new URLSearchParams({ challenge, model, last_n: "60" });
    const result = await api(`/api/trace?${query}`);
    traceText = result.trace || "기록된 trace가 없습니다.";
  } catch (error) {
    traceText = `Trace 오류: ${error.message}`;
  }
  challengeTraces.set(model, traceText);
  const currentCard = [...byId("detail-content").querySelectorAll(".agent-card")].find(
    (item) => item.dataset.challengeName === challenge && item.dataset.agentModel === model,
  );
  const currentOutput = currentCard?.querySelector(".trace-output");
  if (currentOutput) currentOutput.textContent = traceText;
}

async function runCommand(path, body, control = null) {
  const originalLabel = control?.textContent;
  if (control) {
    control.disabled = true;
    control.setAttribute("aria-busy", "true");
    control.textContent = "처리 중…";
  }
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(body) });
    toast(result.message || "요청을 처리했습니다.");
    await refresh({ renderSelected: true });
    return true;
  } catch (error) {
    toast(error.message, "error");
    return false;
  } finally {
    if (control) {
      control.disabled = false;
      control.removeAttribute("aria-busy");
      control.textContent = originalLabel;
    }
  }
}

async function spawnChallenge(name) {
  await runCommand("/api/control/spawn", { challenge: name });
}

async function stopChallenge(name) {
  if (!window.confirm(`${name}의 모든 solver를 중단할까요?`)) return;
  await runCommand("/api/control/stop", { challenge: name });
}

function openDeleteChallengeDialog(name) {
  state.deleteChallengeName = name;
  byId("delete-challenge-name").textContent = name;
  const input = byId("delete-challenge-confirmation");
  input.value = "";
  input.placeholder = name;
  byId("delete-challenge-submit").disabled = true;
  byId("delete-challenge-dialog").showModal();
  input.focus();
}

async function refresh({ renderSelected = false } = {}) {
  if (state.refreshing) return;
  state.refreshing = true;
  byId("refresh-button").disabled = true;
  byId("refresh-button").setAttribute("aria-busy", "true");
  try {
    state.snapshot = await api("/api/status");
    notifyCandidateReviews(state.snapshot);
    setConnection(true);
    renderOverview();
    renderRows();
    if (state.selectedChallenge && renderSelected) {
      const challenge = currentChallenge(state.selectedChallenge);
      const active = document.activeElement;
      const editing = active && byId("detail-content").contains(active)
        && active.matches("input, textarea");
      const changed = detailRenderSignature(challenge) !== state.detailSignature;
      if (changed && !editing) renderDetailPage({ preserveScroll: true });
    }
  } catch (error) {
    setConnection(false);
  } finally {
    state.refreshing = false;
    byId("refresh-button").disabled = false;
    byId("refresh-button").removeAttribute("aria-busy");
  }
}

async function refreshResources() {
  if (state.resourceRefreshing || !state.snapshot) return;
  state.resourceRefreshing = true;
  try {
    const payload = await api("/api/resources");
    state.snapshot.resources = payload.resources || {};
    const byChallenge = new Map((payload.challenges || []).map((item) => [item.name, item]));
    for (const challenge of state.snapshot.challenges) {
      const live = byChallenge.get(challenge.name);
      if (!live) continue;
      challenge.resources = live.resources || {};
      const byAgent = new Map((live.agents || []).map((agent) => [agent.model_spec, agent.resource]));
      for (const agent of challenge.agents) {
        if (byAgent.has(agent.model_spec)) agent.resource = byAgent.get(agent.model_spec);
      }
    }
    renderGlobalResources(state.snapshot.resources);
    for (const row of byId("challenge-rows").querySelectorAll("tr")) {
      const challenge = state.snapshot.challenges.find(
        (item) => item.name === row.dataset.challengeName,
      );
      const copy = row.querySelector(".row-resource-copy");
      if (!challenge || !copy) continue;
      const resources = challenge.resources || {};
      copy.textContent = resources.container_count
        ? ` · ${Number(resources.cpu_percent || 0).toFixed(1)}% CPU · ${formatResourceBytes(resources.memory_bytes || 0)}`
        : "";
    }
    if (state.selectedChallenge) {
      const challenge = state.snapshot.challenges.find((item) => item.name === state.selectedChallenge);
      const current = byId("detail-resource-section");
      if (challenge && current) current.replaceWith(buildResourcePanel(challenge));
    }
  } catch (_error) {
    // The slower status poll owns the global connection indicator.
  } finally {
    state.resourceRefreshing = false;
  }
}

async function initialize() {
  byId("refresh-button").addEventListener("click", refresh);
  byId("codex-usage-refresh").addEventListener("click", () => {
    refreshCodexUsage({ force: true });
  });
  byId("detail-back").addEventListener("click", () => closeChallengeDetail());
  window.addEventListener("popstate", () => syncViewFromLocation());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.selectedChallenge) {
      closeChallengeDetail();
    }
  });

  byId("challenge-search").addEventListener("input", (event) => {
    state.query = event.target.value;
    renderRows();
  });

  byId("status-filter").addEventListener("click", (event) => {
    const target = event.target.closest("button[data-filter]");
    if (!target) return;
    state.filter = target.dataset.filter;
    for (const item of byId("status-filter").querySelectorAll("button")) {
      item.classList.toggle("active", item === target);
      item.setAttribute("aria-pressed", String(item === target));
    }
    renderRows();
  });

  const fileInput = byId("challenge-files");
  const fileDrop = byId("file-drop");
  fileInput.addEventListener("change", () => {
    syncLocalFiles([...state.localFiles, ...fileInput.files]);
  });
  for (const eventName of ["dragenter", "dragover"]) {
    fileDrop.addEventListener(eventName, (event) => {
      event.preventDefault();
      fileDrop.classList.add("dragging");
    });
  }
  for (const eventName of ["dragleave", "drop"]) {
    fileDrop.addEventListener(eventName, (event) => {
      event.preventDefault();
      fileDrop.classList.remove("dragging");
    });
  }
  fileDrop.addEventListener("drop", (event) => {
    if (event.dataTransfer) {
      syncLocalFiles([...state.localFiles, ...event.dataTransfer.files]);
    }
  });

  byId("operator-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = byId("operator-message");
    const submit = event.currentTarget.querySelector("button[type=submit]");
    const success = await runCommand("/api/operator/message", { message: input.value }, submit);
    if (success) input.value = "";
  });

  const runtimeForm = byId("runtime-settings-form");
  runtimeForm.addEventListener("input", markRuntimeSettingsDirty);
  runtimeForm.addEventListener("change", markRuntimeSettingsDirty);
  runtimeForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!runtimeForm.reportValidity()) return;
    const submit = runtimeForm.querySelector("button[type=submit]");
    await submitRuntimeSettings(
      "/api/settings/runtime",
      runtimeSettingsFromForm(),
      submit,
    );
  });
  byId("runtime-settings-reset").addEventListener("click", async (event) => {
    if (!window.confirm("실행 설정을 프로젝트 기본값으로 복원할까요?")) return;
    await submitRuntimeSettings("/api/settings/runtime/reset", {}, event.currentTarget);
  });

  byId("ctfd-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = event.currentTarget.querySelector("button[type=submit]");
    const body = {
      url: byId("ctfd-url").value,
      token: byId("ctfd-token").value,
      username: byId("ctfd-username").value,
      password: byId("ctfd-password").value,
    };
    const success = await runCommand("/api/settings/ctfd", body, submit);
    if (success) {
      byId("ctfd-token").value = "";
      byId("ctfd-password").value = "";
    }
  });

  byId("ctfd-disconnect").addEventListener("click", async (event) => {
    if (!window.confirm("CTFd 연결을 해제하고 독립 모드로 전환할까요?")) return;
    const success = await runCommand("/api/settings/ctfd", { url: "" }, event.currentTarget);
    if (success) {
      byId("ctfd-url").value = "";
      byId("ctfd-token").value = "";
    }
  });

  const resetDialog = byId("reset-dialog");
  const resetInput = byId("reset-confirmation");
  const resetSubmit = byId("reset-submit");
  byId("reset-open").addEventListener("click", () => {
    resetInput.value = "";
    resetSubmit.disabled = true;
    resetDialog.showModal();
    resetInput.focus();
  });
  byId("reset-cancel").addEventListener("click", () => resetDialog.close());
  resetDialog.addEventListener("click", (event) => {
    if (event.target === resetDialog) resetDialog.close();
  });
  resetInput.addEventListener("input", () => {
    resetSubmit.disabled = resetInput.value !== "초기화";
  });
  byId("reset-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (resetInput.value !== "초기화") return;
    const success = await runCommand(
      "/api/control/reset-runtime",
      { confirmation: resetInput.value },
      resetSubmit,
    );
    if (success) {
      closeChallengeDetail({ replaceHistory: true });
      state.candidateNotices.clear();
      state.ctfdInitialized = false;
      byId("ctfd-url").value = "";
      byId("ctfd-token").value = "";
      byId("ctfd-username").value = "";
      byId("ctfd-password").value = "";
      resetDialog.close();
      resetInput.value = "";
      resetSubmit.disabled = true;
    }
  });

  const experienceDialog = byId("experience-reset-dialog");
  const experienceInput = byId("experience-reset-confirmation");
  const experienceSubmit = byId("experience-reset-submit");
  byId("experience-reset-open").addEventListener("click", () => {
    experienceInput.value = "";
    experienceSubmit.disabled = true;
    experienceDialog.showModal();
    experienceInput.focus();
  });
  byId("experience-reset-cancel").addEventListener("click", () => experienceDialog.close());
  experienceDialog.addEventListener("click", (event) => {
    if (event.target === experienceDialog) experienceDialog.close();
  });
  experienceInput.addEventListener("input", () => {
    experienceSubmit.disabled = experienceInput.value !== "경험 초기화";
  });
  byId("experience-reset-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (experienceInput.value !== "경험 초기화") return;
    const success = await runCommand(
      "/api/control/reset-experience",
      { confirmation: experienceInput.value },
      experienceSubmit,
    );
    if (success) {
      experienceDialog.close();
      experienceInput.value = "";
      experienceSubmit.disabled = true;
    }
  });

  const deleteDialog = byId("delete-challenge-dialog");
  const deleteInput = byId("delete-challenge-confirmation");
  const deleteSubmit = byId("delete-challenge-submit");
  byId("delete-challenge-cancel").addEventListener("click", () => deleteDialog.close());
  deleteDialog.addEventListener("click", (event) => {
    if (event.target === deleteDialog) deleteDialog.close();
  });
  deleteInput.addEventListener("input", () => {
    deleteSubmit.disabled = deleteInput.value !== state.deleteChallengeName;
  });
  byId("delete-challenge-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = state.deleteChallengeName;
    if (!name || deleteInput.value !== name) return;
    const success = await runCommand(
      "/api/challenges/delete",
      { challenge: name, confirmation: deleteInput.value },
      deleteSubmit,
    );
    if (success) {
      state.writeups.delete(name);
      state.traces.delete(name);
      deleteDialog.close();
      deleteInput.value = "";
      deleteSubmit.disabled = true;
      state.deleteChallengeName = "";
    }
  });

  byId("local-challenge-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector("button[type=submit]");
    const originalLabel = submit.textContent;
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    submit.textContent = "문제를 등록하는 중…";
    try {
      const result = await api("/api/challenges/local", {
        method: "POST",
        body: new FormData(form),
      });
      toast(result.message);
      form.reset();
      syncLocalFiles([]);
      await refresh();
      await spawnChallenge(result.challenge);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
      submit.textContent = originalLabel;
    }
  });

  try {
    const session = await api("/api/session");
    state.csrfToken = session.csrf_token;
    state.deleteChallengeSupported = session.capabilities?.delete_challenge === true;
    const resetSupported = session.capabilities?.reset_runtime === true
      || await endpointAvailable("/api/control/reset-runtime");
    byId("reset-open").disabled = !resetSupported;
    byId("reset-compatibility").hidden = resetSupported;
    if (!resetSupported) {
      byId("reset-open").textContent = "백엔드 재시작 필요";
      byId("reset-open").title = "실행 중인 coordinator가 초기화 API를 지원하지 않습니다.";
    }
    await refresh();
    void refreshCodexUsage();
    syncViewFromLocation({ animate: false });
    state.refreshTimer = window.setInterval(() => refresh({ renderSelected: true }), 2500);
    state.resourceTimer = window.setInterval(refreshResources, 1000);
    state.codexUsageTimer = window.setInterval(refreshCodexUsage, 30_000);
  } catch (error) {
    setConnection(false);
    toast(`초기화 실패: ${error.message}`, "error");
  }
}

initialize();
