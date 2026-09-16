"use strict";

function defaultFilters() {
  return {
    query: "",
    statuses: new Set(),
    categories: new Set(),
    documentation: "all",
    assignment: "all",
    minPoints: "",
    maxPoints: "",
    maxSolves: "",
    maxCost: "",
    sort: "name-asc",
  };
}

const state = {
  csrfToken: "",
  viewInitialized: false,
  dashboardScroll: 0,
  detailDrafts: new Map(),
  modal: null,
  operations: new Map(),
  confirmationQueue: Promise.resolve(),
  snapshot: null,
  filters: defaultFilters(),
  selectedChallenge: null,
  refreshTimer: null,
  resourceTimer: null,
  codexUsageTimer: null,
  resourceRefreshing: false,
  codexUsageRefreshing: false,
  refreshing: false,
  ctfdInitialized: false,
  discordInitialized: false,
  detailReturnChallenge: null,
  localFiles: [],
  candidateNotices: new Set(),
  writeups: new Map(),
  traces: new Map(),
  detailSignature: "",
  deleteChallengeSupported: false,
  updateChallengeSupported: false,
  deleteChallengeName: "",
  runtimeSettingsInitialized: false,
  runtimeSettingsDirty: false,
  runtimeModels: [],
  codexUsage: null,
  snapshotReceivedAt: null,
  uptimeBaseSeconds: 0,
  challengeClocks: new Map(),
  clockTimer: null,
};

const runtimeNumericFields = [
  "max-concurrent-challenges",
  "container-cpu-limit",
  "max-attempts-per-challenge",
  "coordinator-max-swarm-runs-per-challenge",
  "coordinator-swarm-retry-delay-seconds",
  "solver-turn-timeout-seconds",
  "solver-turn-idle-timeout-seconds",
  "solver-guidance-interrupt-min-interval-seconds",
  "solver-max-runtime-seconds",
  "solver-max-steps",
  "solver-max-tokens",
  "solver-max-raw-tokens",
  "solver-cached-token-weight",
  "solver-turn-slice-tokens",
  "solver-compaction-timeout-seconds",
  "solver-compaction-max-waits",
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

// HeroUI's icons are intentionally kept inline so the dashboard has no icon
// font or third-party runtime dependency. The 24px viewBox and currentColor
// stroke match HeroUI's default icon treatment.
const ICON_PATHS = {
  arrowLeft: ["M19 12H5", "m11 18-6-6 6-6"],
  arrowRight: ["M5 12h14", "m13 6 6 6-6 6"],
  check: ["m5 12 4 4L19 6"],
  chevronDown: ["m6 9 6 6 6-6"],
  chevronRight: ["m9 18 6-6-6-6"],
  copy: ["M9 9h10v10H9z", "M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"],
  download: ["M12 3v12", "m7 10 5 5 5-5", "M5 21h14"],
  filter: ["M4 5h16", "M7 12h10", "M10 19h4"],
  link: ["M10 13a5 5 0 0 0 7.1.1l1.4-1.4a5 5 0 0 0-7.1-7.1L10.6 5.4", "M14 11a5 5 0 0 0-7.1-.1l-1.4 1.4a5 5 0 0 0 7.1 7.1l.8-.8"],
  plus: ["M12 5v14", "M5 12h14"],
  play: ["m9 5 10 7-10 7V5Z"],
  refresh: ["M20 11a8 8 0 0 0-14.7-4L4 9", "M4 5v4h4", "M4 13a8 8 0 0 0 14.7 4L20 15", "M20 19v-4h-4"],
  search: ["m21 21-4.35-4.35", "M11 18a7 7 0 1 1 0-14 7 7 0 0 1 0 14Z"],
  send: ["m3 11 18-8-8 18-2-8-8-2Z", "m11 13 10-10"],
  settings: ["M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z", "M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-1.8 1.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5v.1h-2.6v-.1a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1-1.8-1.8.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H6.4v-2.6h.1a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1 1.8-1.8.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.5v-.1H15v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1 1.8 1.8-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1h.1V14h-.1a1.7 1.7 0 0 0-1.5 1Z"],
  stop: ["M6 6h12v12H6z"],
  terminal: ["M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5v-13Z", "m8 9 3 3-3 3", "M13 15h3"],
  trash: ["M4 7h16", "M10 11v6", "M14 11v6", "m6-4V20a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V7", "m3-3 1-1h4l1 1"],
  upload: ["M12 16V4", "m7 9 5-5 5 5", "M5 20h14"],
  x: ["m6 6 12 12", "M18 6 6 18"],
};

function icon(name, size = 16, className = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", `ui-icon${className ? ` ${className}` : ""}`);
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const pathData of ICON_PATHS[name] || []) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData);
    svg.append(path);
  }
  return svg;
}

function appendIconLabel(element, label, iconName, iconPosition = "start") {
  element.replaceChildren();
  element.dataset.iconName = iconName;
  element.dataset.iconLabel = label;
  const labelNode = node("span", "button-label", label);
  if (iconPosition === "end") element.append(labelNode, icon(iconName));
  else element.append(icon(iconName), labelNode);
  return element;
}

function setButtonLabel(element, label) {
  if (element?.dataset.iconName) {
    appendIconLabel(element, label, element.dataset.iconName);
    return;
  }
  element.textContent = label;
}

function inferButtonIcon(label, className = "") {
  const text = String(label || "");
  if (className.includes("destructive") || /삭제|제거/.test(text)) return "trash";
  if (/취소|닫기/.test(text)) return "x";
  if (/복사/.test(text)) return "copy";
  if (/다운로드|ZIP/.test(text)) return "download";
  if (/새로고침|다시/.test(text)) return "refresh";
  if (/확인|인정|저장|연결|테스트/.test(text)) return "check";
  if (/검토|검색/.test(text)) return "search";
  if (/전송/.test(text)) return "send";
  if (/시작|등록|추가|생성/.test(text)) return "plus";
  if (/보기|열기|결과/.test(text)) return "chevronRight";
  return "";
}

function initializeIconography() {
  const labels = [
    ["#operations-open", "settings"],
    ["#add-challenge", "plus"],
    [".dashboard-hero-actions .button-link", "arrowRight", "end"],
    ["#operator-form button", "send"],
    ["#filter-reset", "refresh"],
    ["#empty-add-challenge", "plus"],
    ["#runtime-settings-reset", "refresh"],
    ["#ctfd-form button[type=submit]", "link"],
    ["#ctfd-disconnect", "x"],
    ["#discord-form button[type=submit]", "check"],
    ["#discord-disconnect", "x"],
    ["#local-challenge-form > button[type=submit]", "plus"],
    ["#experience-export", "download"],
    ["#experience-import", "upload"],
    ["#experience-reset-open", "trash"],
    ["#reset-open", "trash"],
    ["#detail-back", "arrowLeft"],
    ["#workspace-dialog-close", "x"],
    ["#confirmation-cancel", "x"],
    ["#confirmation-accept", "check"],
    ["#reset-cancel", "x"],
    ["#reset-submit", "trash"],
    ["#experience-reset-cancel", "x"],
    ["#experience-reset-submit", "trash"],
    ["#delete-challenge-cancel", "x"],
    ["#delete-challenge-submit", "trash"],
  ];
  for (const [selector, iconName, position] of labels) {
    const element = document.querySelector(selector);
    if (!element || element.dataset.iconName) continue;
    appendIconLabel(element, element.textContent.trim(), iconName, position);
  }
  const filterToggle = byId("advanced-filter-toggle");
  if (filterToggle && !filterToggle.dataset.iconName) {
    const count = byId("filter-count");
    filterToggle.replaceChildren(icon("filter"), node("span", "button-label", "고급 필터"));
    if (count) filterToggle.append(count);
    filterToggle.dataset.iconName = "filter";
    filterToggle.dataset.iconLabel = "고급 필터";
  }
  for (const element of document.querySelectorAll(".icon-button")) {
    if (element.dataset.iconName) continue;
    const iconName = {"refresh-button": "refresh", "codex-usage-refresh": "refresh"}[element.id] || "refresh";
    element.replaceChildren(icon(iconName, 18));
    element.dataset.iconName = iconName;
  }
  const searchIcon = document.querySelector(".search-box > span[aria-hidden=true]");
  if (searchIcon) searchIcon.replaceChildren(icon("search", 17));
  const terminal = document.querySelector(".terminal-glyph");
  if (terminal) terminal.replaceChildren(icon("terminal", 19));
  const emptyIcon = document.querySelector(".empty-state > span");
  if (emptyIcon) emptyIcon.replaceChildren(icon("search", 28));
  const fileDrop = document.querySelector(".file-drop");
  if (fileDrop && !fileDrop.querySelector(".ui-icon")) fileDrop.prepend(icon("upload", 20));
}

function syncModalScrollLock() {
  document.documentElement.classList.toggle("modal-open", Boolean(document.querySelector("dialog[open]")));
}

function closeWorkspaceModal({ restoreFocus = true } = {}) {
  const dialog = byId("workspace-dialog");
  const modal = state.modal;
  state.modal = null;
  if (modal?.marker?.parentNode) {
    modal.marker.replaceWith(modal.content);
    modal.content.hidden = modal.wasHidden;
  }
  if (dialog.open) dialog.close();
  byId("workspace-dialog-body").replaceChildren();
  dialog.querySelector(":scope > .modal-notices")?.remove();
  syncModalScrollLock();
  modal?.onClose?.();
  if (restoreFocus && modal?.trigger?.isConnected) modal.trigger.focus({ preventScroll: true });
}

function openWorkspaceModal(title, content, { operation = "", onClose = null } = {}) {
  const trigger = state.modal?.trigger || document.activeElement;
  if (state.modal) closeWorkspaceModal({ restoreFocus: false });
  const marker = content.parentNode ? document.createComment("modal-return") : null;
  if (marker) content.before(marker);
  state.modal = { content, marker, trigger, wasHidden: content.hidden, onClose };
  content.hidden = false;
  const dialog = byId("workspace-dialog");
  byId("workspace-dialog-title").textContent = title;
  byId("workspace-dialog-body").replaceChildren(content);
  const tabs = byId("workspace-dialog-tabs");
  tabs.hidden = !operation;
  for (const tab of tabs.querySelectorAll("button")) {
    tab.setAttribute("aria-pressed", String(tab.dataset.operation === operation));
  }
  dialog.showModal();
  syncModalScrollLock();
  byId("workspace-dialog-body").scrollTop = 0;
  const focus = content.querySelector("input:not([type=hidden]), textarea, select");
  (focus || byId("workspace-dialog-close")).focus({ preventScroll: true });
}

function confirmAction(message) {
  const request = () => new Promise((resolve) => {
    const dialog = byId("confirmation-dialog");
    byId("confirmation-message").textContent = message;
    const finish = (accepted) => {
      byId("confirmation-accept").onclick = null;
      byId("confirmation-cancel").onclick = null;
      dialog.oncancel = null;
      dialog.close();
      syncModalScrollLock();
      resolve(accepted);
    };
    byId("confirmation-accept").onclick = () => finish(true);
    byId("confirmation-cancel").onclick = () => finish(false);
    dialog.oncancel = (event) => { event.preventDefault(); finish(false); };
    dialog.showModal();
    syncModalScrollLock();
    byId("confirmation-cancel").focus();
  });
  const result = state.confirmationQueue.then(request);
  state.confirmationQueue = result.catch(() => false);
  return result;
}

function modalSection(section, title, subtitle = "", onOpen = null) {
  const card = node("section", "modal-launcher");
  const copy = node("div");
  copy.append(node("h3", "", title));
  if (subtitle) copy.append(node("p", "", subtitle));
  const stash = node("div");
  stash.hidden = true;
  stash.append(section);
  const open = onOpen || (() => openWorkspaceModal(title, section));
  const launch = button("열기", "secondary-button", open, false, "chevronRight");
  launch.setAttribute("aria-label", `${title} 열기`);
  launch.setAttribute("aria-haspopup", "dialog");
  card.append(copy, launch, stash);
  card.openModal = open;
  return card;
}

function openOperation(key) {
  const operation = state.operations.get(key);
  if (operation) openWorkspaceModal(operation.title, operation.content, { operation: key });
}

function initializeModals() {
  byId("workspace-dialog-close").addEventListener("click", () => closeWorkspaceModal());
  byId("workspace-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeWorkspaceModal();
  });
  for (const dialog of document.querySelectorAll("dialog")) {
    dialog.addEventListener("close", syncModalScrollLock);
    dialog.addEventListener("toggle", syncModalScrollLock);
  }
  const operations = [
    ["runtime", "실행 설정", document.querySelector(".runtime-settings-section")],
    ["ctfd", "CTFd 연결", byId("ctfd-form").closest("article")],
    ["discord", "Discord 알림", byId("discord-form").closest("article")],
    ["local", "문제 추가", byId("local-challenge-form").closest("article")],
    ["experience", "공유 경험", document.querySelector(".experience-panel")],
    ["maintenance", "환경 관리", document.querySelector(".maintenance-section")],
  ];
  const launcherBar = node("section", "operations-launchers");
  launcherBar.setAttribute("aria-label", "문제 및 운영 도구");
  const stash = node("div");
  stash.hidden = true;
  for (const [key, title, content] of operations) {
    state.operations.set(key, {title, content});
    stash.append(content);
    const operationIcon = {runtime: "settings", ctfd: "link", discord: "send", local: "plus", experience: "copy", maintenance: "trash"}[key];
    launcherBar.append(button(title, "secondary-button", () => openOperation(key), false, operationIcon));
    const tab = button(title, "secondary-button", () => openOperation(key), false, operationIcon);
    tab.dataset.operation = key;
    byId("workspace-dialog-tabs").append(tab);
  }
  launcherBar.append(stash);
  document.querySelector(".operations-disclosure").replaceWith(launcherBar);
  const system = document.querySelector(".system-disclosure");
  const monitor = system.querySelector(".dashboard-disclosure-content");
  state.operations.set("monitor", {title: "시스템 모니터", content: monitor});
  const operator = document.querySelector(".operator-panel");
  state.operations.set("operator", {title: "Coordinator에게 지시", content: operator});
  for (const key of ["monitor", "operator"]) {
    const tab = button(state.operations.get(key).title, "secondary-button", () => openOperation(key), false, key === "monitor" ? "terminal" : "send");
    tab.dataset.operation = key;
    byId("workspace-dialog-tabs").append(tab);
  }
  byId("operations-open").addEventListener("click", () => openOperation("runtime"));
}

function button(label, className, onClick, disabled = false, iconName = "") {
  const element = node("button", className, label);
  element.type = "button";
  element.disabled = disabled;
  const resolvedIcon = iconName || inferButtonIcon(label, className);
  if (resolvedIcon) appendIconLabel(element, label, resolvedIcon);
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

function hasActiveTextSelection() {
  const selection = window.getSelection?.();
  return Boolean(selection && !selection.isCollapsed && selection.toString().trim());
}

function sampledElapsedSeconds(baseSeconds) {
  const delta = state.snapshotReceivedAt !== null
    ? Math.max(0, (performance.now() - state.snapshotReceivedAt) / 1000)
    : 0;
  return Math.max(0, Number(baseSeconds || 0) + delta);
}

function observedChallengeDuration(challenge) {
  return Math.max(0, Number(challenge.elapsed_seconds || 0));
}

function challengeElapsedSeconds(challenge) {
  const clock = state.challengeClocks.get(challenge.name);
  if (!clock) return observedChallengeDuration(challenge);
  const delta = clock.active ? Math.max(0, (performance.now() - clock.sampledAt) / 1000) : 0;
  return clock.baseSeconds + delta;
}

function syncFrontendClocks(snapshot) {
  const now = performance.now();
  state.snapshotReceivedAt = now;
  state.uptimeBaseSeconds = Math.max(0, Number(snapshot.uptime_seconds || 0));

  const currentNames = new Set();
  for (const challenge of snapshot.challenges || []) {
    currentNames.add(challenge.name);
    state.challengeClocks.set(challenge.name, {
      active: Boolean(challenge.active),
      baseSeconds: observedChallengeDuration(challenge),
      sampledAt: now,
    });
  }
  for (const name of state.challengeClocks.keys()) {
    if (!currentNames.has(name)) state.challengeClocks.delete(name);
  }
}

function renderLiveClocks() {
  if (!state.snapshot || hasActiveTextSelection()) return;
  const uptime = byId("uptime-value");
  if (uptime) uptime.textContent = `UPTIME ${formatDuration(sampledElapsedSeconds(state.uptimeBaseSeconds))}`;
  for (const element of document.querySelectorAll("[data-challenge-elapsed]")) {
    const challenge = currentChallenge(element.dataset.challengeElapsed);
    if (challenge) element.textContent = `풀이 ${formatDuration(challengeElapsedSeconds(challenge))}`;
  }
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

async function syncLocalFiles(files) {
  const filesByName = new Map();
  for (const file of files) {
    const previous = filesByName.get(file.name);
    if (previous && previous !== file && !await confirmAction(`'${file.name}' 파일이 이미 선택되어 있습니다. 새 파일로 교체할까요?`)) continue;
    filesByName.set(file.name, file);
  }
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
    const remove = button("", "file-remove", () => {
      syncLocalFiles(state.localFiles.filter((_selected, selectedIndex) => selectedIndex !== index));
    }, false, "x");
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
  const dialog = [...document.querySelectorAll("dialog[open]")].at(-1);
  if (dialog) {
    let notices = dialog.querySelector(":scope > .modal-notices");
    if (!notices) {
      notices = node("div", "modal-notices");
      notices.setAttribute("aria-live", "polite");
      dialog.append(notices);
    }
    notices.append(item);
  } else byId("toast-region").append(item);
  if (type === "error") {
    item.setAttribute("role", "alert");
    item.append(button("닫기", "secondary-button", () => item.remove(), false, "x"));
  } else window.setTimeout(() => item.remove(), 4200);
}

function copyControl(text, label = "복사") {
  return button(label, "secondary-button", async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast("복사했습니다.");
    } catch (_error) {
      toast("복사하지 못했습니다. 내용을 선택해 직접 복사하세요.", "error");
    }
  }, false, "copy");
}

function showFormError(form, message) {
  if (!form) return;
  form.querySelector(".form-error")?.remove();
  const notice = node("p", "form-error wide-field", message);
  notice.setAttribute("role", "alert");
  form.append(notice);
}

function documentPreview(text, className, key) {
  const wrapper = node("div", "document-viewer");
  const controls = node("div", "document-controls");
  controls.append(copyControl(text, "문서 복사"));
  const reading = node("div", "document-reading");
  let code = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("```")) {
      if (code) code = null;
      else { code = node("pre"); reading.append(code); }
    } else if (code) code.textContent += `${line}\n`;
    else {
      const heading = line.match(/^#{1,6}\s+(.+)$/);
      reading.append(node(heading ? "h4" : "p", "", heading ? heading[1] : line));
    }
  }
  const raw = node("details", "document-source");
  raw.dataset.disclosureKey = `${key}-source`;
  raw.append(node("summary", "", "원문 보기"), node("pre", className, text));
  wrapper.append(controls, reading, raw);
  const title = className === "solution-review-report" ? "검토 보고서" : "라이트업";
  const launch = button(`${title} 열기`, "secondary-button", () => openWorkspaceModal(title, wrapper));
  launch.setAttribute("aria-haspopup", "dialog");
  return launch;
}

function jumpToSection(id) {
  const target = byId(id);
  if (!target) return;
  if (target.openModal) { target.openModal(); return; }
  for (let ancestor = target; ancestor; ancestor = ancestor.parentElement) {
    if (ancestor.tagName === "DETAILS") ancestor.open = true;
  }
  target.scrollIntoView({ block: "start" });
  const field = target.querySelector("input, textarea, button");
  if (field) field.focus({ preventScroll: true });
  else { target.tabIndex = -1; target.focus({ preventScroll: true }); }
}

function openLocalChallengeForm() {
  openOperation("local");
}

function notifyCandidateReviews(snapshot) {
  const pending = snapshot.challenges.filter((challenge) => challenge.candidate_review_required);
  document.title = state.selectedChallenge
    ? `${state.selectedChallenge} · CTF Agent`
    : pending.length ? `(${pending.length}) CTF 후보 검토 필요` : "CTF Agent";
  for (const challenge of pending) {
    const key = `${challenge.name}:${challenge.candidate}`;
    if (state.candidateNotices.has(key)) continue;
    state.candidateNotices.add(key);
    const message = challenge.candidate_conflicts_with_solved
      ? `${challenge.name}: 확정 Flag와 다른 후보가 발견되었습니다 — ${challenge.candidate}`
      : `${challenge.name}: Flag 후보 검토가 필요합니다 — ${challenge.candidate}`;
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
  const discord = snapshot.discord || {};
  const discordBadge = byId("discord-source-badge");
  discordBadge.textContent = discord.configured ? "설정됨" : "미설정";
  discordBadge.className = `source-badge ${discord.configured ? "connected" : "local"}`;
  byId("discord-status-copy").textContent = discord.configured
    ? "서버 알림이 활성화되어 있습니다. URL은 보안을 위해 표시하지 않습니다."
    : "Discord 채널 웹훅을 연결하면 주요 진행 상황을 받을 수 있습니다.";
  if (!state.discordInitialized) {
    byId("discord-webhook-url").value = "";
    byId("discord-webhook-url").placeholder = discord.configured
      ? "새 URL을 입력하면 기존 설정을 교체합니다"
      : "https://discord.com/api/webhooks/...";
    state.discordInitialized = true;
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
  const writeup = splitModelSpec(
    settings.writeup_model_spec,
    "codex/gpt-5.6-terra",
    "medium",
  );
  const writeupReview = splitModelSpec(
    settings.writeup_review_model_spec,
    "codex/gpt-5.6-luna",
    "medium",
  );
  state.runtimeModels = models;
  selectValue(byId("runtime-primary-model"), primary.base);
  selectValue(byId("runtime-primary-effort"), primary.effort);
  selectValue(byId("runtime-delegate-model"), delegate.base);
  selectValue(byId("runtime-delegate-effort"), delegate.effort);
  selectValue(byId("runtime-writeup-model"), writeup.base);
  selectValue(byId("runtime-writeup-effort"), writeup.effort);
  selectValue(byId("runtime-writeup-review-model"), writeupReview.base);
  selectValue(byId("runtime-writeup-review-effort"), writeupReview.effort);
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
    writeup_model_spec: `${byId("runtime-writeup-model").value}/${byId("runtime-writeup-effort").value}`,
    writeup_review_model_spec: `${byId("runtime-writeup-review-model").value}/${byId("runtime-writeup-review-effort").value}`,
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
  const form = byId("runtime-settings-form");
  form.querySelector(".form-error")?.remove();
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
    showFormError(form, error.message);
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

function optionalNumber(value) {
  if (value === "" || value === null || value === undefined) return null;
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function challengeCategories(challenge) {
  const aliases = {
    binary: "pwn", exploitation: "pwn", pwnable: "pwn",
    reverse: "reversing", rev: "reversing", re: "reversing", rerversing: "reversing",
    crypto: "cryptography", forensic: "forensics", steg: "forensics", steganography: "forensics",
    kernel: "pwn", mobile: "android", apk: "android", "smart contract": "blockchain", web3: "blockchain",
  };
  const source = Array.isArray(challenge.categories)
    ? challenge.categories
    : String(challenge.category || "").split(/\s*\/\s*/);
  const seen = new Set();
  return source
    .map((category) => String(category || "").trim().toLocaleLowerCase("ko-KR"))
    .map((category) => aliases[category] || category)
    .filter((category) => {
      if (!category || seen.has(category)) return false;
      seen.add(category);
      return true;
    });
}

function challengeMatchesFilters(challenge, filters) {
  const query = String(filters.query || "").trim().toLocaleLowerCase("ko-KR");
  const categories = challengeCategories(challenge);
  const haystack = `${challenge.name || ""} ${categories.join(" ")}`.toLocaleLowerCase("ko-KR");
  if (query && !haystack.includes(query)) return false;
  if (filters.statuses.size && !filters.statuses.has(challenge.status)) return false;
  if (filters.categories.size && !categories.some((category) => filters.categories.has(category))) return false;
  if (filters.documentation === "documented" && !challenge.documented) return false;
  if (filters.documentation === "pending" && (!challenge.solved || challenge.documented)) return false;
  const hasAgents = Array.isArray(challenge.agents) && challenge.agents.length > 0;
  if (filters.assignment === "assigned" && !hasAgents) return false;
  if (filters.assignment === "unassigned" && hasAgents) return false;

  const points = Number(challenge.value || 0);
  const solves = Number(challenge.solves || 0);
  const cost = Number(challenge.cost_usd || 0);
  const minPoints = optionalNumber(filters.minPoints);
  const maxPoints = optionalNumber(filters.maxPoints);
  const maxSolves = optionalNumber(filters.maxSolves);
  const maxCost = optionalNumber(filters.maxCost);
  return !(minPoints !== null && points < minPoints)
    && !(maxPoints !== null && points > maxPoints)
    && !(maxSolves !== null && solves > maxSolves)
    && !(maxCost !== null && cost > maxCost);
}

function compareChallenges(left, right, sort) {
  const nameOrder = String(left.name || "").localeCompare(String(right.name || ""), "ko-KR", {
    numeric: true,
    sensitivity: "base",
  });
  const numeric = (key, direction) => {
    const difference = Number(left[key] || 0) - Number(right[key] || 0);
    return (direction === "desc" ? -difference : difference) || nameOrder;
  };
  if (sort === "points-desc") return numeric("value", "desc");
  if (sort === "points-asc") return numeric("value", "asc");
  if (sort === "solves-asc") return numeric("solves", "asc");
  if (sort === "solves-desc") return numeric("solves", "desc");
  if (sort === "elapsed-desc") return numeric("elapsed_seconds", "desc");
  if (sort === "cost-desc") return numeric("cost_usd", "desc");
  return nameOrder;
}

function filteredChallenges() {
  if (!state.snapshot) return [];
  return state.snapshot.challenges
    .filter((challenge) => challengeMatchesFilters(challenge, state.filters))
    .sort((left, right) => compareChallenges(left, right, state.filters.sort));
}

function filterConditionCount(filters, includeQuery = true) {
  return Number(includeQuery && Boolean(String(filters.query || "").trim()))
    + Number(filters.statuses.size > 0)
    + Number(filters.categories.size > 0)
    + Number(filters.documentation !== "all")
    + Number(filters.assignment !== "all")
    + Number(optionalNumber(filters.minPoints) !== null)
    + Number(optionalNumber(filters.maxPoints) !== null)
    + Number(optionalNumber(filters.maxSolves) !== null)
    + Number(optionalNumber(filters.maxCost) !== null);
}

function filtersFromLocation() {
  const params = new URL(window.location.href).searchParams;
  const allowedStatuses = new Set(["active", "candidate", "solved", "idle"]);
  const allowedDocumentation = new Set(["all", "documented", "pending"]);
  const allowedAssignment = new Set(["all", "assigned", "unassigned"]);
  const allowedSorts = new Set([
    "name-asc", "points-desc", "points-asc", "solves-asc", "solves-desc", "elapsed-desc", "cost-desc",
  ]);
  const statuses = new Set(
    (params.get("status") || "").split(",").filter((value) => allowedStatuses.has(value)),
  );
  const documentation = params.get("doc") || "all";
  const assignment = params.get("agents") || "all";
  const sort = params.get("sort") || "name-asc";
  const numericParam = (name) => {
    const value = params.get(name) || "";
    return optionalNumber(value) === null ? "" : value;
  };
  return {
    query: params.get("q") || "",
    statuses,
    categories: new Set(
      params.getAll("category")
        .flatMap((category) => category.split(/\s*\/\s*/))
        .map((category) => category.trim())
        .filter(Boolean),
    ),
    documentation: allowedDocumentation.has(documentation) ? documentation : "all",
    assignment: allowedAssignment.has(assignment) ? assignment : "all",
    minPoints: numericParam("min_points"),
    maxPoints: numericParam("max_points"),
    maxSolves: numericParam("max_solves"),
    maxCost: numericParam("max_cost"),
    sort: allowedSorts.has(sort) ? sort : "name-asc",
  };
}

function syncFilterUrl() {
  const url = new URL(window.location.href);
  const filters = state.filters;
  for (const key of [
    "q", "status", "category", "doc", "agents", "min_points", "max_points", "max_solves", "max_cost", "sort",
  ]) {
    url.searchParams.delete(key);
  }
  if (filters.query.trim()) url.searchParams.set("q", filters.query.trim());
  if (filters.statuses.size) url.searchParams.set("status", [...filters.statuses].sort().join(","));
  for (const category of [...filters.categories].sort()) url.searchParams.append("category", category);
  if (filters.documentation !== "all") url.searchParams.set("doc", filters.documentation);
  if (filters.assignment !== "all") url.searchParams.set("agents", filters.assignment);
  if (optionalNumber(filters.minPoints) !== null) url.searchParams.set("min_points", filters.minPoints);
  if (optionalNumber(filters.maxPoints) !== null) url.searchParams.set("max_points", filters.maxPoints);
  if (optionalNumber(filters.maxSolves) !== null) url.searchParams.set("max_solves", filters.maxSolves);
  if (optionalNumber(filters.maxCost) !== null) url.searchParams.set("max_cost", filters.maxCost);
  if (filters.sort !== "name-asc") url.searchParams.set("sort", filters.sort);
  window.history.replaceState(window.history.state, "", url);
}

function renderCategoryFilters() {
  if (!state.snapshot) return;
  const container = byId("category-filter");
  const counts = new Map();
  for (const challenge of state.snapshot.challenges) {
    for (const category of challengeCategories(challenge)) {
      counts.set(category, (counts.get(category) || 0) + 1);
    }
  }
  const categories = [...new Set([...counts.keys(), ...state.filters.categories])]
    .sort((left, right) => left.localeCompare(right, "ko-KR", { sensitivity: "base" }));
  const current = [...container.querySelectorAll("input")].map((input) => input.value);
  if (current.join("\u0000") !== categories.join("\u0000")) {
    container.replaceChildren();
    for (const category of categories) {
      const label = node("label", "filter-option");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = category;
      label.append(input, node("span", "", `${category} ${formatNumber(counts.get(category) || 0)}`));
      container.append(label);
    }
    if (!categories.length) container.append(node("span", "filter-placeholder", "사용 가능한 카테고리가 없습니다."));
  } else {
    for (const input of container.querySelectorAll("input")) {
      input.nextElementSibling.textContent = `${input.value} ${formatNumber(counts.get(input.value) || 0)}`;
    }
  }
}

function renderFilterControls() {
  renderCategoryFilters();
  byId("challenge-search").value = state.filters.query;
  byId("challenge-sort").value = state.filters.sort;
  byId("documentation-filter").value = state.filters.documentation;
  byId("assignment-filter").value = state.filters.assignment;
  for (const [id, key] of [["min-points-filter", "minPoints"], ["max-points-filter", "maxPoints"], ["max-solves-filter", "maxSolves"], ["max-cost-filter", "maxCost"]]) {
    const input = byId(id);
    if (document.activeElement !== input) input.value = state.filters[key];
  }
  for (const input of byId("status-filter").querySelectorAll("input")) {
    input.checked = state.filters.statuses.has(input.value);
  }
  for (const input of byId("category-filter").querySelectorAll("input")) {
    input.checked = state.filters.categories.has(input.value);
  }
  const statusCounts = new Map();
  for (const challenge of state.snapshot?.challenges || []) {
    statusCounts.set(challenge.status, (statusCounts.get(challenge.status) || 0) + 1);
  }
  for (const input of byId("status-filter").querySelectorAll("input")) {
    input.nextElementSibling.textContent = `${statusLabel(input.value)} ${formatNumber(statusCounts.get(input.value) || 0)}`;
  }
  const advancedCount = filterConditionCount(state.filters, false);
  const count = byId("filter-count");
  count.textContent = String(advancedCount);
  count.hidden = advancedCount === 0;
}

function filterChip(label, remove) {
  const chip = node("button", "active-filter-chip");
  chip.type = "button";
  chip.setAttribute("aria-label", `${label} 필터 제거`);
  chip.append(node("span", "", label), node("span", "filter-chip-remove", "×"));
  chip.addEventListener("click", () => {
    remove();
    applyFilterChange();
  });
  return chip;
}

function renderFilterSummary(visibleCount) {
  const total = state.snapshot?.challenges.length || 0;
  byId("filter-result-count").textContent = `전체 ${formatNumber(total)}개 중 ${formatNumber(visibleCount)}개 표시`;
  const modalCount = byId("filter-modal-result");
  if (modalCount) modalCount.textContent = byId("filter-result-count").textContent;
  const chips = byId("active-filter-chips");
  const signature = JSON.stringify([state.filters.query, [...state.filters.statuses], [...state.filters.categories], state.filters.documentation, state.filters.assignment, state.filters.minPoints, state.filters.maxPoints, state.filters.maxSolves, state.filters.maxCost]);
  byId("filter-reset").hidden = filterConditionCount(state.filters) === 0 && state.filters.sort === "name-asc";
  if (chips.dataset.signature === signature) return;
  chips.dataset.signature = signature;
  chips.replaceChildren();
  if (state.filters.query.trim()) {
    chips.append(filterChip(`검색: ${state.filters.query.trim()}`, () => { state.filters.query = ""; }));
  }
  for (const status of state.filters.statuses) {
    chips.append(filterChip(`상태: ${statusLabel(status)}`, () => state.filters.statuses.delete(status)));
  }
  for (const category of state.filters.categories) {
    chips.append(filterChip(`카테고리: ${category}`, () => state.filters.categories.delete(category)));
  }
  const documentationLabels = { documented: "라이트업 완료", pending: "해결 후 미작성" };
  if (documentationLabels[state.filters.documentation]) {
    chips.append(filterChip(documentationLabels[state.filters.documentation], () => { state.filters.documentation = "all"; }));
  }
  const assignmentLabels = { assigned: "Solver 배정됨", unassigned: "Solver 미배정" };
  if (assignmentLabels[state.filters.assignment]) {
    chips.append(filterChip(assignmentLabels[state.filters.assignment], () => { state.filters.assignment = "all"; }));
  }
  const ranges = [
    ["minPoints", "최소 점수"], ["maxPoints", "최대 점수"],
    ["maxSolves", "최대 풀이 수"], ["maxCost", "최대 비용 $"],
  ];
  for (const [key, label] of ranges) {
    if (optionalNumber(state.filters[key]) !== null) {
      chips.append(filterChip(`${label}: ${state.filters[key]}`, () => { state.filters[key] = ""; }));
    }
  }
  byId("filter-reset").hidden = filterConditionCount(state.filters) === 0 && state.filters.sort === "name-asc";
}

function applyFilterChange() {
  syncFilterUrl();
  renderFilterControls();
  renderRows();
}

function resetFilters() {
  state.filters = defaultFilters();
  applyFilterChange();
}

function currentChallenge(name) {
  return state.snapshot?.challenges.find((challenge) => challenge.name === name);
}

function createChallengeRow(challengeName) {
  const row = node("tr");
  row.dataset.challengeName = challengeName;
  row.addEventListener("click", () => {
    if (hasActiveTextSelection()) return;
    openChallengeDetail(row.dataset.challengeName);
  });

  const nameCell = node("td", "challenge-name-cell");
  const detailLink = node("button", "challenge-name");
  detailLink.type = "button";
  detailLink.addEventListener("click", (event) => {
    event.stopPropagation();
    openChallengeDetail(row.dataset.challengeName);
  });
  const meta = node("span", "challenge-meta");
  nameCell.append(detailLink, meta);
  const statusCell = node("td", "challenge-status-cell");
  const progressCell = node("td", "progress-copy challenge-progress-cell");
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
  row.append(nameCell, statusCell, progressCell, actionCell);
  return row;
}

function updateChallengeRow(row, challenge) {
  row.dataset.challengeName = challenge.name;

  const nameCell = row.querySelector(".challenge-name-cell");
  const detailLink = nameCell.querySelector(".challenge-name");
  detailLink.textContent = challenge.name;
  detailLink.setAttribute("aria-label", `${challenge.name} 상세 보기`);
  const meta = nameCell.querySelector(".challenge-meta");
  meta.replaceChildren();
  meta.append(node("span", "", challengeCategories(challenge).join(" / ") || "Unknown"));
  meta.append(node("span", "", `${formatNumber(challenge.value)} pts`));
  if (challenge.solves) meta.append(node("span", "", `${formatNumber(challenge.solves)} solves`));
  if (challenge.documented) meta.append(node("span", "documented-badge", "DOCUMENTED"));
  const statusCell = row.querySelector(".challenge-status-cell");
  statusCell.replaceChildren(node("span", `status-badge ${challenge.status}`, statusLabel(challenge.status)));

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
  const elapsed = node(
    "span",
    "challenge-elapsed row-duration-copy",
    `풀이 ${formatDuration(challengeElapsedSeconds(challenge))}`,
  );
  elapsed.dataset.challengeElapsed = challenge.name;
  progressCell.append(elapsed);

  const action = row.querySelector(".row-action");
  const actionLabel = challenge.active ? "중단" : challenge.solved ? "보기" : "시작";
  const actionIcon = challenge.active ? "stop" : challenge.solved ? "chevronRight" : "play";
  appendIconLabel(action, actionLabel, actionIcon);
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
  renderFilterControls();
  renderFilterSummary(challenges.length);
  byId("empty-state").hidden = challenges.length !== 0;
  const hasRegisteredChallenges = Boolean(state.snapshot?.challenges.length);
  byId("empty-title").textContent = hasRegisteredChallenges
    ? "조건에 맞는 문제가 없습니다"
    : "아직 등록된 문제가 없습니다";
  byId("empty-copy").textContent = hasRegisteredChallenges
    ? "검색어나 적용 중인 필터를 바꾸거나 전체 초기화해보세요."
    : "아래에서 CTFd를 연결하거나 로컬 문제를 추가하세요.";
  byId("empty-add-challenge").hidden = hasRegisteredChallenges;

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

function collapseDetailSection(section, title, sideText = "", open = false) {
  const disclosure = node("details", "detail-disclosure");
  disclosure.dataset.disclosureKey = title;
  disclosure.open = open;
  const summary = node("summary", "detail-disclosure-summary");
  const copy = node("span");
  copy.append(node("strong", "", title));
  if (sideText) copy.append(node("small", "", sideText));
  summary.append(copy, node("span", "disclosure-action", open ? "접기" : "열기"));
  disclosure.addEventListener("toggle", () => {
    summary.querySelector(".disclosure-action").textContent = disclosure.open ? "접기" : "열기";
  });
  disclosure.append(summary, section);
  return disclosure;
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

function buildResourcePanel(challenge, contentOnly = false) {
  const resources = challenge.resources || {};
  const section = detailSection("실시간 Docker 리소스", `${resources.container_count || 0} CONTAINERS`);
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
  if (contentOnly) return section;
  const disclosure = modalSection(
    section,
    "Docker 리소스",
    `${resources.container_count || 0} CONTAINERS`,
    () => {
      openWorkspaceModal("Docker 리소스", section);
      state.modal.resourceName = challenge.name;
    },
  );
  disclosure.id = "detail-resource-section";
  return disclosure;
}

function buildRegistrationSection(challenge) {
  const registration = challenge.registration || {};
  const sourceLabels = { local: "로컬 등록", ctfd: "CTFd 가져오기", metadata: "메타데이터 파일" };
  const source = sourceLabels[registration.source] || "메타데이터 파일";
  const attachments = Array.isArray(registration.attachments) ? registration.attachments : [];
  const section = detailSection("등록 정보", source.toUpperCase());

  const fixed = node("dl", "registration-fixed-grid");
  const fixedItem = (label, value) => {
    const item = node("div", "registration-fixed-item");
    item.append(node("dt", "", label), node("dd", "", value));
    return item;
  };
  fixed.append(
    fixedItem("등록 방식", source),
    fixedItem("문제명", challenge.name),
    fixedItem("첨부 파일", attachments.length ? `${formatNumber(attachments.length)}개` : "없음"),
    fixedItem(
      "메타데이터 갱신",
      registration.metadata_updated_at
        ? new Date(registration.metadata_updated_at).toLocaleString("ko-KR")
        : "확인 불가",
    ),
  );
  section.append(fixed);

  if (attachments.length) {
    const files = node("ul", "registration-files");
    for (const filename of attachments) files.append(node("li", "", filename));
    section.append(files);
  }

  const help = node(
    "p",
    "registration-help",
    "등록 방식, 문제명과 첨부 파일은 고정 정보입니다. 아래 메타데이터 변경은 목록 검색과 필터에 즉시 반영됩니다.",
  );
  section.append(help);

  const form = node("form", "registration-form");
  const draftStatus = node("p", "draft-status wide-field", "");
  draftStatus.setAttribute("aria-live", "polite");
  form.addEventListener("input", () => {
    form.dataset.dirty = "true";
    draftStatus.textContent = "저장되지 않은 변경 · 이 탭에서 임시 보관됩니다.";
  });
  const field = (labelText, name, value, options = {}) => {
    const label = node("label", options.wide ? "wide-field" : "");
    label.append(node("span", "", labelText));
    const control = document.createElement(options.multiline ? "textarea" : "input");
    control.name = name;
    control.value = value ?? "";
    if (!options.multiline) control.type = options.type || "text";
    if (options.min !== undefined) control.min = String(options.min);
    if (options.step !== undefined) control.step = String(options.step);
    if (options.maxLength) control.maxLength = options.maxLength;
    label.append(control);
    return label;
  };
  form.append(
    field(
      "카테고리",
      "category",
      registration.category ?? challengeCategories(challenge).join(" / "),
      { maxLength: 100 },
    ),
    field("점수", "value", challenge.value, { type: "number", min: 0, step: 1 }),
    field("풀이 수", "solves", challenge.solves, { type: "number", min: 0, step: 1 }),
    field("접속 정보", "connection_info", registration.connection_info, { maxLength: 2000 }),
    field("Flag 형식", "flag_format", registration.flag_format, { maxLength: 500, wide: true }),
    field("설명", "description", registration.description, { multiline: true, maxLength: 20000, wide: true }),
  );
  const actions = node("div", "registration-actions wide-field");
  const save = node("button", "secondary-button", "등록 정보 저장");
  save.type = "submit";
  save.disabled = !state.updateChallengeSupported;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const values = new FormData(form);
    await runCommand(
      "/api/challenges/update",
      {
        challenge: challenge.name,
        category: values.get("category"),
        value: values.get("value"),
        solves: values.get("solves"),
        connection_info: values.get("connection_info"),
        flag_format: values.get("flag_format"),
        description: values.get("description"),
      },
      save,
      () => {
        delete form.dataset.dirty;
        draftStatus.textContent = "저장했습니다.";
      },
    );
  });
  if (!state.updateChallengeSupported) {
    save.title = "등록 정보 수정 API를 사용하려면 coordinator를 재시작해야 합니다.";
  }
  actions.append(save);
  form.append(draftStatus, actions);
  section.append(form);
  return modalSection(section, "등록 정보 수정", source);
}

function buildChallengeOverview(challenge) {
  const section = detailSection("문제 개요");
  section.id = "detail-overview-section";
  const registration = challenge.registration || {};
  section.append(node("p", "challenge-description", registration.description || "등록된 설명이 없습니다."));
  for (const [label, value] of [["접속 정보", registration.connection_info], ["Flag 형식", registration.flag_format]]) {
    if (!value) continue;
    const item = node("div", "overview-value");
    item.append(node("strong", "", label), node("pre", "", value), copyControl(value));
    section.append(item);
  }
  if (registration.attachments?.length) {
    const files = node("ul", "overview-files");
    for (const filename of registration.attachments) {
      const item = node("li");
      const link = node("a", "", filename);
      link.href = `/api/attachment?${new URLSearchParams({challenge: challenge.name, path: filename})}`;
      link.download = filename.split("/").pop();
      item.append(link);
      files.append(item);
    }
    section.append(node("h4", "", "첨부 파일"), files);
  }
  return section;
}

function buildSolutionReviewSection(challenge) {
  const review = challenge.solution_review || {};
  if (review.status === "not_requested") return null;
  const verdict = String(review.report || "").match(/\b(ON_TRACK|NEEDS_PIVOT|INSUFFICIENT_EVIDENCE)\b/)?.[1] || "";
  const labels = {
    running: "검토 중",
    complete: "검토 완료",
    error: "검토 실패",
  };
  const section = detailSection("현재 풀이 검토", labels[review.status] || "검토 결과");
  const card = node("article", `solution-review-card is-${review.status || "idle"}`);
  const header = node("div", "solution-review-header");
  const copy = node("div");
  copy.append(
    node(
      "strong",
      "",
      review.status === "running"
        ? "독립 검토 에이전트가 확인 중입니다"
        : verdict
          ? `검토 완료 · ${verdict}`
          : labels[review.status] || "검토 결과",
    ),
    node(
      "span",
      "",
      review.status === "running"
        ? `${review.activity || "풀이 근거를 대조하는 중"} · ${review.model_spec || "reviewer"} · ${review.steps || 0} steps`
        : review.status === "complete"
          ? `${review.model_spec || "reviewer"} · 검토가 끝나 에이전트는 종료되었습니다.`
          : review.activity || "검토를 완료하지 못했습니다.",
    ),
  );
  const rerun = button(
    review.status === "running" ? "검토 중…" : "다시 검토",
    "secondary-button",
    () => requestSolutionReview(challenge.name, rerun),
    review.status === "running",
  );
  if (review.status === "running") rerun.setAttribute("aria-busy", "true");
  header.append(copy, rerun);
  card.append(header);
  if (review.report) {
    card.append(documentPreview(review.report, "solution-review-report", "review-document"));
  }
  section.append(card);
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
        ? `${writeup.phase_label || "라이트업 생성"} 중`
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
        ? `${writeup.activity || "작업 내용을 준비하는 중"} · ${writeup.phase === "reviewing" ? writeup.review_model_spec : writeup.model_spec || "AI"} · 도구 실행 ${writeup.steps || 0}회 · ${writeup.idle_seconds == null ? "에이전트 시작 중" : `마지막 활동 ${Math.floor(writeup.idle_seconds)}초 전 (무응답 ${writeup.idle_timeout_seconds}초 후 중단)`}`
        : `${(writeup.reproducers || []).length} reproducers · ${(writeup.screenshots || []).length} screenshots`,
    ),
  );
  const actions = node("div", "writeup-actions");
  if (writeup.writeup_path) {
    actions.append(button("Writeup 보기", "secondary-button", () => loadWriteup(challenge.name)));
    actions.append(button("Markdown + 사진 ZIP", "secondary-button", () => downloadWriteupArchive(challenge.name)));
  }
  if (challenge.solved) {
    const requestLabel = isGenerating
      ? "라이트업 생성 중…"
      : challenge.documented
      ? "라이트업 재생성"
      : writeup.status === "needs_attention" && writeup.review_path
        ? "부족 항목 수정 재개"
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
  if (writeup.screenshots?.length) {
    const gallery = node("div", "screenshot-gallery");
    for (const shot of writeup.screenshots) {
      const figure = node("figure");
      const img = node("img");
      const params = new URLSearchParams({ challenge: challenge.name, path: shot.path });
      img.src = `/api/artifact?${params}`;
      img.alt = shot.caption || "Writeup evidence";
      img.loading = "lazy";
      const fullImage = node("a");
      fullImage.href = img.src;
      fullImage.target = "_blank";
      fullImage.rel = "noopener";
      fullImage.setAttribute("aria-label", `${img.alt} 원본 이미지 열기`);
      fullImage.addEventListener("click", (event) => {
        event.preventDefault();
        const viewer = node("figure", "image-viewer");
        const original = node("img");
        original.src = img.src;
        original.alt = img.alt;
        viewer.append(original, node("figcaption", "", shot.caption || shot.path));
        openWorkspaceModal("증거 이미지", viewer);
      });
      fullImage.append(img);
      figure.append(fullImage, node("figcaption", "", shot.caption || shot.path));
      gallery.append(figure);
    }
    section.append(gallery);
  }
  return challenge.solved
    ? section
    : collapseDetailSection(section, "Writeup & evidence", writeupStatusLabel);
}

async function loadWriteup(challengeName) {
  const container = node("div", "document-viewer", "라이트업을 불러오는 중…");
  openWorkspaceModal("라이트업", container);
  try {
    const params = new URLSearchParams({ challenge: challengeName });
    state.writeups.set(challengeName, await api(`/api/writeup?${params}`));
    if (state.modal?.content === container) {
      const launch = documentPreview(state.writeups.get(challengeName).content || "", "writeup-preview", "writeup-document");
      launch.click();
    }
  } catch (error) {
    if (state.modal?.content === container) container.textContent = error.message;
    toast(error.message, "error");
  }
}

function downloadWriteupArchive(challengeName) {
  const params = new URLSearchParams({ challenge: challengeName });
  window.location.assign(`/api/writeup/archive?${params}`);
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

async function requestSolutionReview(challengeName, control) {
  await runCommand(
    "/api/control/review-solution",
    { challenge: challengeName },
    control,
  );
}

function detailRenderSignature(challenge) {
  if (!challenge) return "";
  const { resources: _resources, elapsed_seconds: _elapsed, agents = [], ...stableChallenge } = challenge;
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
    disclosures: [...content.querySelectorAll("details[data-disclosure-key]")].map((item) => ({
      key: item.dataset.disclosureKey,
      open: item.open,
    })),
    fields: [...content.querySelectorAll("input, textarea")].map((field) => ({
      name: field.name,
      value: field.value,
      registration: Boolean(field.closest?.(".registration-form")),
    })),
    dirty: Boolean(content.querySelector?.(".registration-form[data-dirty]")),
    errors: [...content.querySelectorAll("form")].map((form) => ({
      field: form.querySelector("input, textarea")?.name,
      message: form.querySelector(".form-error")?.textContent,
    })).filter((item) => item.message),
    nestedScroll: [...content.querySelectorAll(".writeup-preview, .trace-output, .solution-review-report")].map((item) => ({
      className: item.className,
      left: item.scrollLeft,
      top: item.scrollTop,
    })),
  };
}

function restoreDetailViewState(content, saved) {
  const disclosures = new Map(saved.disclosures.map((item) => [item.key, item.open]));
  for (const item of content.querySelectorAll("details[data-disclosure-key]")) {
    if (disclosures.has(item.dataset.disclosureKey)) item.open = disclosures.get(item.dataset.disclosureKey);
  }
  const fields = [...content.querySelectorAll("input, textarea")];
  saved.fields.forEach((field) => {
    if (field.registration && !saved.dirty) return;
    const current = fields.find((item) => item.name === field.name);
    if (current) current.value = field.value;
  });
  const registrationForm = content.querySelector?.(".registration-form");
  if (saved.dirty && registrationForm) {
    registrationForm.dataset.dirty = "true";
    registrationForm.querySelector(".draft-status").textContent = "저장되지 않은 변경 · 이 탭에서 임시 보관됩니다.";
  }
  for (const error of saved.errors || []) {
    const field = fields.find((item) => item.name === error.field);
    const form = field?.closest("form");
    if (form) {
      const notice = node("p", "form-error wide-field", error.message);
      notice.setAttribute("role", "alert");
      form.append(notice);
    }
  }

  const nestedScroll = [...content.querySelectorAll(".writeup-preview, .trace-output, .solution-review-report")];
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
  if (state.modal) return;
  const challenge = state.snapshot?.challenges.find((item) => item.name === state.selectedChallenge);
  if (!challenge) {
    closeChallengeDetail({ replaceHistory: true });
    return;
  }
  const isStandalone = state.snapshot.submission_mode === "standalone";

  document.title = `${challenge.name} · CTF Agent`;
  byId("detail-title").textContent = challenge.name;
  const categories = challengeCategories(challenge).join(" / ") || "Unknown";
  byId("detail-category").textContent = `${categories.toUpperCase()} · ${formatNumber(challenge.value)} PTS`;
  const content = byId("detail-content");
  const savedView = preserveScroll ? captureDetailViewState(content) : state.detailDrafts.get(challenge.name);
  const nextContent = document.createDocumentFragment();

  const overview = node("div", "detail-overview");
  const summary = node("div", "detail-summary");
  summary.append(
    metric("STATUS", statusLabel(challenge.status)),
    metric("AGENTS", String(challenge.agents.length)),
    metric("SOLVE TIME", formatDuration(challengeElapsedSeconds(challenge))),
    metric("COST", formatMoney(challenge.cost_usd)),
  );
  summary.children[2].querySelector("strong").dataset.challengeElapsed = challenge.name;
  const controls = node("div", "control-row detail-actions");
  if (challenge.active) {
    controls.append(button("풀이 중단", "danger-button", () => stopChallenge(challenge.name), false, "stop"));
  } else if (!challenge.solved) {
    controls.append(button("SOL 풀이 시작", "primary-button", () => spawnChallenge(challenge.name), false, "play"));
  }
  const reviewStatus = challenge.solution_review || {};
  const reviewButton = button(
    reviewStatus.active ? "풀이 검토 중…" : "현재 풀이 검토",
    challenge.active ? "primary-button" : "secondary-button",
    () => requestSolutionReview(challenge.name, reviewButton),
    reviewStatus.active,
    "search",
  );
  if (reviewStatus.active) reviewButton.setAttribute("aria-busy", "true");
  controls.append(reviewButton);
  const more = node("details", "detail-action-menu");
  more.dataset.disclosureKey = "action-menu";
  const moreSummary = node("summary", "secondary-button");
  appendIconLabel(moreSummary, "더보기", "chevronDown", "end");
  const morePanel = node("div", "detail-action-menu-panel");
  morePanel.append(button("상태 새로고침", "secondary-button", () => refresh({ renderSelected: true }), false, "refresh"));
  const deleteButton = button(
    "문제 삭제",
    "destructive-button",
    () => openDeleteChallengeDialog(challenge.name),
    !state.deleteChallengeSupported,
    "trash",
  );
  if (!state.deleteChallengeSupported) {
    deleteButton.title = "새 삭제 API를 사용하려면 coordinator를 재시작해야 합니다.";
  }
  morePanel.append(deleteButton);
  more.append(moreSummary, morePanel);
  controls.append(more);
  overview.append(summary, controls);
  nextContent.append(overview);

  const navigation = node("nav", "detail-section-nav");
  navigation.setAttribute("aria-label", "문제 세부 섹션 이동");
  for (const [id, label] of [["detail-overview-section", "개요"], ["detail-notes-section", "진행"], ["detail-results-section", "결과"], ["detail-input-section", "입력"]]) {
    if (id === "detail-input-section" && challenge.solved && !challenge.active) continue;
    navigation.append(button(label, "secondary-button", () => jumpToSection(id)));
  }
  nextContent.append(navigation);

  const layout = node("div", "detail-layout");
  const mainColumn = node("div", "detail-main-column");
  const sideColumn = node("aside", "detail-side-column");
  layout.append(mainColumn, sideColumn);
  nextContent.append(layout);
  mainColumn.append(buildChallengeOverview(challenge));
  if (challenge.flag) {
    const flagSection = detailSection(isStandalone ? "로컬 Flag 결과" : "확인된 Flag");
    flagSection.append(node("div", "flag-value", challenge.flag));
    flagSection.append(copyControl(challenge.flag, "Flag 복사"));
    mainColumn.append(flagSection);
  }

  const pendingCandidates = [...new Set(
    [challenge.candidate, ...(challenge.candidates || [])].filter(Boolean),
  )];
  const formatMismatchCandidates = new Set(challenge.format_mismatch_candidates || []);
  if (pendingCandidates.length) {
    const conflictsWithSolved = Boolean(challenge.candidate_conflicts_with_solved);
    const candidateSection = detailSection(
      conflictsWithSolved ? "확정 Flag와 다른 후보" : "검증되지 않은 Flag 후보",
      `${pendingCandidates.length} ${conflictsWithSolved ? "CONFLICT" : "PENDING"}`,
    );
    if (conflictsWithSolved) {
      candidateSection.append(node(
        "p",
        "candidate-conflict-warning",
        "이 문제는 이미 해결 처리되었습니다. 확정 Flag와 값이 다른 후보만 별도로 보존했으므로 추가 정답인지 오답인지 검토해 주세요.",
      ));
    }
    for (const candidateFlag of pendingCandidates) {
      const candidateEntry = node("div", "candidate-entry");
      candidateEntry.append(node("div", "flag-value", candidateFlag));
      if (formatMismatchCandidates.has(candidateFlag)) {
        candidateEntry.append(node(
          "div",
          "candidate-format-warning",
          `ALT FORMAT · expected ${challenge.candidate_format_hint || "configured hint"} · exact observed form preserved`,
        ));
      }
      candidateEntry.append(copyControl(candidateFlag, "후보 복사"));
      if (challenge.candidate_review_required) {
      const reviewControls = node("div", "control-row");
      const accept = button(conflictsWithSolved ? "추가 정답으로 인정" : "정답으로 확인", "primary-button", async () => {
        const prompt = conflictsWithSolved
          ? `${candidateFlag}를 이 문제의 추가 정답으로 인정할까요?`
          : `${candidateFlag}를 로컬 정답으로 확정할까요?`;
        if (!await confirmAction(prompt)) return;
        await runCommand(
          "/api/control/review-candidate",
          { challenge: challenge.name, flag: candidateFlag, accepted: true },
          accept,
        );
      });
      const reject = button("오답으로 거부", "danger-button", async () => {
        if (!await confirmAction(`${candidateFlag}를 오답 후보로 제거할까요?`)) return;
        await runCommand(
          "/api/control/review-candidate",
          { challenge: challenge.name, flag: candidateFlag, accepted: false },
          reject,
        );
      }, false, "x");
      reviewControls.append(accept, reject);
        candidateEntry.append(reviewControls);
      }
      candidateSection.append(candidateEntry);
    }
    mainColumn.append(candidateSection);
  }

  const rejectedCandidates = [...new Set(challenge.rejected_candidates || [])];
  if (rejectedCandidates.length) {
    const rejectedSection = detailSection(
      "거부된 Flag 후보",
      `${rejectedCandidates.length} REJECTED`,
    );
    for (const rejectedFlag of rejectedCandidates) {
      const rejectedEntry = node("div", "candidate-entry is-rejected");
      rejectedEntry.append(node("div", "flag-value", rejectedFlag));
      rejectedEntry.append(copyControl(rejectedFlag, "거부된 후보 복사"));
      rejectedSection.append(rejectedEntry);
    }
    mainColumn.append(rejectedSection);
  }

  const solutionReviewSection = buildSolutionReviewSection(challenge);
  if (solutionReviewSection) mainColumn.append(solutionReviewSection);
  sideColumn.append(buildResourcePanel(challenge));

  const noteMetadata = /^(source agent|stop reason|attempt|tool steps|original handoff|handoff audit|requested deliverable):/i;
  const approachNotes = (Array.isArray(challenge.approach_notes) ? challenge.approach_notes : [])
    .filter((note) => note?.text && !noteMetadata.test(note.text.trim()));
  const notesSection = detailSection("지금까지의 접근 노트", `${approachNotes.length} NOTES`);
  notesSection.id = "detail-notes-section";
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
  const resultsSection = buildWriteupSection(challenge);
  resultsSection.id = "detail-results-section";
  mainColumn.append(resultsSection);
  mainColumn.append(buildRegistrationSection(challenge));

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
      node("span", "", `elapsed ${formatDuration(agent.duration_seconds || 0)}`),
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
    const traceButton = button("최근 trace 보기", "trace-button", () => loadTrace(challenge.name, agent.model_spec), false, "arrowRight");
    card.append(traceButton);
    agentsSection.append(card);
  }
  sideColumn.append(
    collapseDetailSection(
      agentsSection,
      "Solver agents",
      `${challenge.agents.length} AGENTS`,
      challenge.active && challenge.agents.length > 0,
    ),
  );

  const inputPanel = node("div", "challenge-input-panel");
  if (challenge.active) {
    const broadcastSection = detailSection("전체 solver에 힌트 전달");
    const form = node("form", "detail-form");
    const textarea = node("textarea");
    textarea.name = "message";
    textarea.id = "detail-hint-input";
    textarea.maxLength = 4000;
    textarea.placeholder = "이 문제를 푸는 모든 solver에게 전달할 기술적 힌트";
    textarea.required = true;
    const send = node("button", "secondary-button", "Broadcast");
    send.type = "submit";
    const label = node("label", "", "Solver에게 전달할 힌트");
    label.htmlFor = textarea.id;
    form.append(label, textarea, send);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      await runCommand("/api/control/broadcast", { challenge: challenge.name, message: textarea.value }, send, () => {
        textarea.value = "";
      });
    });
    broadcastSection.append(form);
    inputPanel.append(broadcastSection);
  }

  if (!challenge.solved) {
    const submitSection = detailSection(isStandalone ? "로컬 Flag 후보 기록" : state.snapshot.no_submit ? "Flag 후보 기록" : "Flag 수동 제출");
    const form = node("form", "detail-form");
    const input = node("input");
    input.name = "flag";
    input.id = "detail-flag-input";
    input.maxLength = 2000;
    input.placeholder = "TEAM{candidate_flag}";
    input.required = true;
    const send = node("button", "primary-button", isStandalone ? "로컬 후보로 기록" : state.snapshot.no_submit ? "Dry-run 확인" : "CTFd에 제출");
    send.type = "submit";
    const label = node("label", "", "Flag 값");
    label.htmlFor = input.id;
    form.append(label, input, send);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!state.snapshot.no_submit && !await confirmAction(`${challenge.name}에 이 flag를 제출할까요?`)) return;
      await runCommand("/api/control/submit", { challenge: challenge.name, flag: input.value }, send, () => {
        input.value = "";
      });
    });
    submitSection.append(form);
    inputPanel.append(submitSection);
  }

  if (inputPanel.childElementCount) {
    const inputSection = modalSection(inputPanel, "문제 입력", "힌트 전달 · Flag 입력");
    inputSection.id = "detail-input-section";
    sideColumn.append(inputSection);
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
    window.scrollTo(0, showDetail ? 0 : state.dashboardScroll);
    if (showDetail) {
      byId("detail-back").focus({ preventScroll: true });
      return;
    }
    const returnRow = [...byId("challenge-rows").querySelectorAll("tr")]
      .find((row) => row.dataset.challengeName === returnChallenge);
    if (returnRow) returnRow.querySelector(".challenge-name")?.focus({ preventScroll: true });
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
  if (state.modal) closeWorkspaceModal({ restoreFocus: false });
  if (!state.selectedChallenge) state.dashboardScroll = window.scrollY;
  else state.detailDrafts.set(state.selectedChallenge, captureDetailViewState(byId("detail-content")));
  if (!state.selectedChallenge) state.detailReturnChallenge = name;
  state.selectedChallenge = name;
  renderDetailPage();
  if (updateHistory) {
    window.history.pushState({ ctfView: "detail", challenge: name }, "", challengeUrl(name));
  }
  transitionAppView(true, { animate });
}

function closeChallengeDetail({ fromHistory = false, replaceHistory = false, animate = true } = {}) {
  if (state.modal) closeWorkspaceModal({ restoreFocus: false });
  if (!fromHistory && !replaceHistory && window.history.state?.ctfView === "detail") {
    window.history.back();
    return;
  }

  const returnChallenge = state.detailReturnChallenge || state.selectedChallenge || "";
  if (state.selectedChallenge) state.detailDrafts.set(state.selectedChallenge, captureDetailViewState(byId("detail-content")));
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

async function loadTrace(challenge, model) {
  let challengeTraces = state.traces.get(challenge);
  if (!challengeTraces) {
    challengeTraces = new Map();
    state.traces.set(challenge, challengeTraces);
  }
  const loadingText = "trace를 불러오는 중…";
  const container = node("div");
  const output = node("pre", "trace-output", challengeTraces.get(model) || loadingText);
  output.tabIndex = 0;
  output.setAttribute("role", "region");
  output.setAttribute("aria-label", "최근 solver trace");
  container.append(output);
  openWorkspaceModal(`${challenge} · ${model} trace`, container);
  let traceText;
  try {
    const query = new URLSearchParams({ challenge, model, last_n: "60" });
    const result = await api(`/api/trace?${query}`);
    traceText = result.trace || "기록된 trace가 없습니다.";
  } catch (error) {
    traceText = `Trace 오류: ${error.message}`;
  }
  challengeTraces.set(model, traceText);
  if (state.modal?.content === container) {
    output.textContent = traceText;
    container.prepend(copyControl(traceText, "trace 복사"));
  }
}

async function runCommand(path, body, control = null, onSuccess = null) {
  const originalLabel = control?.textContent;
  const form = control?.closest("form");
  form?.querySelector(".form-error")?.remove();
  if (control) {
    control.disabled = true;
    control.setAttribute("aria-busy", "true");
    setButtonLabel(control, "처리 중…");
  }
  try {
    const result = await api(path, { method: "POST", body: JSON.stringify(body) });
    if (onSuccess) onSuccess();
    toast(result.message || "요청을 처리했습니다.");
    await refresh({ renderSelected: true });
    return true;
  } catch (error) {
    showFormError(form, error.message);
    toast(error.message, "error");
    return false;
  } finally {
    if (control) {
      control.disabled = false;
      control.removeAttribute("aria-busy");
      setButtonLabel(control, originalLabel);
    }
  }
}

async function spawnChallenge(name, { silent = false } = {}) {
  try {
    const result = await api("/api/control/spawn", {
      method: "POST",
      body: JSON.stringify({ challenge: name }),
    });
    const started = Boolean(result.started);
    const message = result.message
      || (started ? "풀이를 시작했습니다." : "풀이를 시작하지 못했습니다.");
    if (!silent) toast(message, started ? "success" : "warning");
    await refresh({ renderSelected: true });
    return { started, message };
  } catch (error) {
    if (!silent) toast(error.message, "error");
    return { started: false, message: error.message };
  }
}

async function stopChallenge(name) {
  if (!await confirmAction(`${name}의 모든 solver를 중단할까요?`)) return;
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
    if (!state.csrfToken) await refreshSession();
    const snapshot = await api("/api/status");
    syncFrontendClocks(snapshot);
    state.snapshot = snapshot;
    notifyCandidateReviews(state.snapshot);
    setConnection(true);
    const selecting = hasActiveTextSelection();
    if (!selecting) {
      renderOverview();
      renderRows();
    }
    if (state.selectedChallenge && renderSelected && !state.modal) {
      const challenge = currentChallenge(state.selectedChallenge);
      const active = document.activeElement;
      const editing = active && byId("detail-content").contains(active)
        && active.matches("input, textarea");
      const changed = detailRenderSignature(challenge) !== state.detailSignature;
      if (changed && !editing && !selecting) renderDetailPage({ preserveScroll: true });
    }
    renderLiveClocks();
    if (!state.viewInitialized) {
      syncViewFromLocation({ animate: false });
      state.viewInitialized = true;
    }
  } catch (error) {
    console.error("Dashboard refresh failed", error);
    state.csrfToken = "";
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
    if (hasActiveTextSelection()) return;
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
      if (challenge && state.modal?.resourceName === challenge.name) {
        const latest = buildResourcePanel(challenge, true);
        state.modal.content.replaceChildren(...latest.childNodes);
      } else if (challenge && current && !state.modal) current.replaceWith(buildResourcePanel(challenge));
    }
  } catch (_error) {
    // The slower status poll owns the global connection indicator.
  } finally {
    state.resourceRefreshing = false;
  }
}

async function refreshSession() {
  const session = await api("/api/session");
  state.deleteChallengeSupported = session.capabilities?.delete_challenge === true;
  state.updateChallengeSupported = session.capabilities?.update_challenge === true;
  const resetSupported = session.capabilities?.reset_runtime === true
    || await endpointAvailable("/api/control/reset-runtime");
  const reset = byId("reset-open");
  reset.disabled = !resetSupported;
  byId("reset-compatibility").hidden = resetSupported;
  if (!reset.dataset.defaultLabel) reset.dataset.defaultLabel = reset.textContent;
  setButtonLabel(reset, resetSupported ? reset.dataset.defaultLabel : "백엔드 재시작 필요");
  reset.title = resetSupported ? "" : "실행 중인 coordinator가 초기화 API를 지원하지 않습니다.";
  state.csrfToken = session.csrf_token;
}

function handleEscape(event) {
  if (event.key !== "Escape" || event.defaultPrevented || document.querySelector("dialog[open]")) return;
  if (state.selectedChallenge) closeChallengeDetail();
}

async function initialize() {
  state.filters = filtersFromLocation();
  initializeModals();
  initializeIconography();
  byId("add-challenge").addEventListener("click", openLocalChallengeForm);
  byId("empty-add-challenge").addEventListener("click", openLocalChallengeForm);
  for (const disclosure of document.querySelectorAll(".dashboard-disclosure")) {
    const action = disclosure.querySelector(":scope > summary .disclosure-action");
    const syncLabel = () => {
      if (action) action.textContent = disclosure.open ? "접기" : "열기";
    };
    disclosure.addEventListener("toggle", syncLabel);
    syncLabel();
  }
  byId("refresh-button").addEventListener("click", () => refresh({ renderSelected: true }));
  byId("codex-usage-refresh").addEventListener("click", () => {
    refreshCodexUsage({ force: true });
  });
  byId("detail-back").addEventListener("click", () => closeChallengeDetail());
  window.addEventListener("popstate", () => {
    state.filters = filtersFromLocation();
    renderFilterControls();
    renderRows();
    syncViewFromLocation();
  });
  document.addEventListener("keydown", handleEscape);

  byId("challenge-search").addEventListener("input", (event) => {
    state.filters.query = event.target.value;
    syncFilterUrl();
    renderRows();
  });

  byId("challenge-sort").addEventListener("change", (event) => {
    state.filters.sort = event.target.value;
    applyFilterChange();
  });

  const advancedToggle = byId("advanced-filter-toggle");
  const advancedPanel = byId("advanced-filter-panel");
  advancedToggle.addEventListener("click", () => {
    advancedToggle.setAttribute("aria-expanded", "true");
    openWorkspaceModal("문제 필터", advancedPanel, {
      onClose: () => {
        readAdvancedFilters();
        advancedToggle.setAttribute("aria-expanded", "false");
      },
    });
  });

  const readAdvancedFilters = () => {
    state.filters.statuses = new Set(
      [...byId("status-filter").querySelectorAll("input:checked")].map((input) => input.value),
    );
    state.filters.categories = new Set(
      [...byId("category-filter").querySelectorAll("input:checked")].map((input) => input.value),
    );
    state.filters.documentation = byId("documentation-filter").value;
    state.filters.assignment = byId("assignment-filter").value;
    const numericValue = (id) => {
      const value = byId(id).value;
      return optionalNumber(value) === null ? "" : value;
    };
    state.filters.minPoints = numericValue("min-points-filter");
    state.filters.maxPoints = numericValue("max-points-filter");
    state.filters.maxSolves = numericValue("max-solves-filter");
    state.filters.maxCost = numericValue("max-cost-filter");
    if (state.filters.minPoints !== "" && state.filters.maxPoints !== ""
      && Number(state.filters.minPoints) > Number(state.filters.maxPoints)) {
      [state.filters.minPoints, state.filters.maxPoints] = [state.filters.maxPoints, state.filters.minPoints];
    }
    applyFilterChange();
  };
  advancedPanel.addEventListener("change", readAdvancedFilters);
  const filterActions = node("div", "filter-modal-actions");
  const filterResult = node("p");
  filterResult.id = "filter-modal-result";
  filterResult.setAttribute("aria-live", "polite");
  filterActions.append(filterResult, button("필터 초기화", "secondary-button", resetFilters, false, "refresh"), button("결과 보기", "primary-button", () => closeWorkspaceModal(), false, "arrowRight"));
  advancedPanel.append(filterActions);
  byId("filter-reset").addEventListener("click", resetFilters);

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
    const control = event.currentTarget;
    if (!await confirmAction("실행 설정을 프로젝트 기본값으로 복원할까요?")) return;
    await submitRuntimeSettings("/api/settings/runtime/reset", {}, control);
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
    const control = event.currentTarget;
    if (!await confirmAction("CTFd 연결을 해제하고 독립 모드로 전환할까요?")) return;
    const success = await runCommand("/api/settings/ctfd", { url: "" }, control);
    if (success) {
      byId("ctfd-url").value = "";
      byId("ctfd-token").value = "";
    }
  });

  byId("discord-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = byId("discord-webhook-url");
    if (!input.value.trim()) {
      showToast("Discord 웹훅 URL을 입력하세요.", "error");
      input.focus();
      return;
    }
    const submit = event.currentTarget.querySelector("button[type=submit]");
    const success = await runCommand(
      "/api/settings/discord",
      { webhook_url: input.value },
      submit,
    );
    if (success) {
      input.value = "";
      state.discordInitialized = false;
    }
  });

  byId("discord-disconnect").addEventListener("click", async (event) => {
    const control = event.currentTarget;
    if (!await confirmAction("Discord 웹훅 알림을 해제할까요?")) return;
    const success = await runCommand(
      "/api/settings/discord",
      { webhook_url: "" },
      control,
    );
    if (success) {
      byId("discord-webhook-url").value = "";
      state.discordInitialized = false;
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
      state.detailDrafts.clear();
      state.ctfdInitialized = false;
      state.discordInitialized = false;
      byId("ctfd-url").value = "";
      byId("ctfd-token").value = "";
      byId("ctfd-username").value = "";
      byId("ctfd-password").value = "";
      byId("discord-webhook-url").value = "";
      resetDialog.close();
      resetInput.value = "";
      resetSubmit.disabled = true;
    }
  });

  const experienceDialog = byId("experience-reset-dialog");
  const experienceInput = byId("experience-reset-confirmation");
  const experienceSubmit = byId("experience-reset-submit");
  const experienceExport = byId("experience-export");
  const experienceImport = byId("experience-import");
  const experienceImportFile = byId("experience-import-file");
  experienceExport.addEventListener("click", async () => {
    const originalLabel = experienceExport.textContent;
    experienceExport.disabled = true;
    experienceExport.setAttribute("aria-busy", "true");
    setButtonLabel(experienceExport, "내보내는 중…");
    try {
      const response = await fetch("/api/experience/export", { cache: "no-store" });
      if (!response.ok) throw new Error(await response.text() || `HTTP ${response.status}`);
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") || "";
      const matched = disposition.match(/filename="?([^";]+)"?/i);
      const link = document.createElement("a");
      const objectUrl = URL.createObjectURL(blob);
      link.href = objectUrl;
      link.download = matched?.[1] || "ctf-agent-experience.zip";
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
      toast(`공유 경험 ${response.headers.get("x-experience-records") || "0"}건을 내보냈습니다.`);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      experienceExport.disabled = false;
      experienceExport.removeAttribute("aria-busy");
      setButtonLabel(experienceExport, originalLabel);
    }
  });
  experienceImport.addEventListener("click", () => experienceImportFile.click());
  experienceImportFile.addEventListener("change", async () => {
    const archive = experienceImportFile.files?.[0];
    if (!archive) return;
    const originalLabel = experienceImport.textContent;
    experienceImport.disabled = true;
    experienceImport.setAttribute("aria-busy", "true");
    setButtonLabel(experienceImport, "불러오는 중…");
    try {
      const form = new FormData();
      form.append("archive", archive);
      const result = await api("/api/experience/import", { method: "POST", body: form });
      toast(result.message);
      await refresh({ renderSelected: true });
    } catch (error) {
      toast(error.message, "error");
    } finally {
      experienceImportFile.value = "";
      experienceImport.disabled = false;
      experienceImport.removeAttribute("aria-busy");
      setButtonLabel(experienceImport, originalLabel);
    }
  });
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
      state.detailDrafts.delete(name);
      deleteDialog.close();
      deleteInput.value = "";
      deleteSubmit.disabled = true;
      state.deleteChallengeName = "";
    }
  });

  byId("local-challenge-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    form.querySelector(".form-error")?.remove();
    const submit = form.querySelector("button[type=submit]");
    const originalLabel = submit.textContent;
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    setButtonLabel(submit, "문제를 등록하는 중…");
    try {
      const result = await api("/api/challenges/local", {
        method: "POST",
        body: new FormData(form),
      });
      toast(result.message);
      form.reset();
      await syncLocalFiles([]);
      await refresh();
      const spawn = await spawnChallenge(result.challenge, { silent: true });
      if (!spawn.started) {
        toast(
          `문제 '${result.challenge}' 등록은 완료됐지만 자동 풀이 시작에 실패했습니다: ${spawn.message} 목록의 시작 버튼으로 다시 시도할 수 있습니다.`,
          "warning",
        );
      }
    } catch (error) {
      showFormError(form, error.message);
      toast(error.message, "error");
    } finally {
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
      setButtonLabel(submit, originalLabel);
    }
  });

  state.refreshTimer = window.setInterval(() => refresh({ renderSelected: true }), 2500);
  state.resourceTimer = window.setInterval(refreshResources, 1000);
  state.clockTimer = window.setInterval(renderLiveClocks, 1000);
  state.codexUsageTimer = window.setInterval(refreshCodexUsage, 30_000);
  await refresh();
  void refreshCodexUsage();
}

initialize();
