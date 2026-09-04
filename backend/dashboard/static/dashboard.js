"use strict";

const state = {
  csrfToken: "",
  snapshot: null,
  filter: "all",
  query: "",
  selectedChallenge: null,
  refreshTimer: null,
  refreshing: false,
  ctfdInitialized: false,
  drawerReturnChallenge: null,
  localFiles: [],
  candidateNotices: new Set(),
};

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
  byId("stat-total").textContent = `전체 ${stats.total} 문제`;
  byId("stat-swarms").textContent = `${stats.active_swarms}`;
  byId("stat-capacity").textContent = `동시 실행 한도 ${snapshot.max_concurrent_challenges}`;
  byId("stat-agents").textContent = `${stats.active_agents}`;
  byId("stat-agent-total").textContent = `생성된 agent ${stats.total_agents}`;
  byId("stat-cost").textContent = formatMoney(stats.cost_usd);
  byId("stat-tokens").textContent = `${formatTokens(stats.tokens)} raw · ${formatTokens(stats.effective_tokens)} effective`;
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

function renderRows() {
  const tbody = byId("challenge-rows");
  tbody.replaceChildren();
  const challenges = filteredChallenges();
  byId("empty-state").hidden = challenges.length !== 0;
  const hasRegisteredChallenges = Boolean(state.snapshot?.challenges.length);
  byId("empty-title").textContent = hasRegisteredChallenges
    ? "조건에 맞는 문제가 없습니다"
    : "아직 등록된 문제가 없습니다";
  byId("empty-copy").textContent = hasRegisteredChallenges
    ? "검색어나 상태 필터를 바꿔보세요."
    : "아래에서 CTFd를 연결하거나 로컬 문제를 추가하세요.";

  for (const challenge of challenges) {
    const row = node("tr");
    row.dataset.challengeName = challenge.name;
    row.tabIndex = 0;
    row.setAttribute("aria-label", `${challenge.name} 상세 보기`);
    row.addEventListener("click", () => openDrawer(challenge.name));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openDrawer(challenge.name);
      }
    });

    const nameCell = node("td");
    nameCell.append(node("span", "challenge-name", challenge.name));
    const meta = node("span", "challenge-meta");
    meta.append(node("span", "", challenge.category));
    meta.append(node("span", "", `${formatNumber(challenge.value)} pts`));
    if (challenge.solves) meta.append(node("span", "", `${formatNumber(challenge.solves)} solves`));
    nameCell.append(meta);

    const statusCell = node("td");
    statusCell.append(node("span", `status-badge ${challenge.status}`, statusLabel(challenge.status)));

    const agentsCell = node("td");
    const stack = node("div", "agent-stack");
    for (const agent of challenge.agents.slice(0, 4)) {
      const chip = node("span", `agent-chip ${agent.status}`, modelShortName(agent.model_spec));
      chip.title = `${agent.model_spec} · ${agentStatusLabel(agent.status)}`;
      stack.append(chip);
    }
    if (!challenge.agents.length) stack.append(node("span", "progress-copy", "아직 없음"));
    agentsCell.append(stack);

    const steps = challenge.agents.reduce((sum, agent) => sum + Number(agent.steps || 0), 0);
    const progressCell = node("td", "progress-copy");
    progressCell.append(node("strong", "", formatNumber(steps)));
    progressCell.append(document.createTextNode(" steps"));

    const costCell = node("td", "cost-cell", formatMoney(challenge.cost_usd));
    const actionCell = node("td");
    const actionLabel = challenge.active ? "중단" : challenge.solved ? "보기" : "시작";
    actionCell.append(button(actionLabel, "row-action", () => {
      if (challenge.active) stopChallenge(challenge.name);
      else if (challenge.solved) openDrawer(challenge.name);
      else spawnChallenge(challenge.name);
    }));

    row.append(nameCell, statusCell, agentsCell, progressCell, costCell, actionCell);
    tbody.append(row);
  }
}

function metric(label, value) {
  const item = node("div", "detail-metric");
  item.append(node("span", "", label), node("strong", "", value));
  return item;
}

function drawerSection(title, sideText = "") {
  const section = node("section", "drawer-section");
  const heading = node("div", "drawer-section-title");
  heading.append(node("h3", "", title));
  if (sideText) heading.append(node("span", "", sideText));
  section.append(heading);
  return section;
}

function renderDrawer() {
  const challenge = state.snapshot?.challenges.find((item) => item.name === state.selectedChallenge);
  if (!challenge) {
    closeDrawer();
    return;
  }
  const isStandalone = state.snapshot.submission_mode === "standalone";

  byId("drawer-title").textContent = challenge.name;
  byId("drawer-category").textContent = `${challenge.category.toUpperCase()} · ${formatNumber(challenge.value)} PTS`;
  const content = byId("drawer-content");
  content.replaceChildren();

  const summary = node("div", "detail-summary");
  summary.append(
    metric("STATUS", statusLabel(challenge.status)),
    metric("AGENTS", String(challenge.agents.length)),
    metric("COST", formatMoney(challenge.cost_usd)),
  );
  content.append(summary);

  const controls = node("div", "control-row");
  if (challenge.active) {
    controls.append(button("풀이 중단", "danger-button", () => stopChallenge(challenge.name)));
  } else if (!challenge.solved) {
    controls.append(button("SOL 풀이 시작", "primary-button", () => spawnChallenge(challenge.name)));
  }
  controls.append(button("상태 새로고침", "secondary-button", () => refresh({ renderSelected: true })));
  content.append(controls);

  if (challenge.flag) {
    const flagSection = drawerSection(isStandalone ? "로컬 Flag 결과" : "확인된 Flag");
    flagSection.append(node("div", "flag-value", challenge.flag));
    content.append(flagSection);
  }

  if (challenge.candidate) {
    const candidateSection = drawerSection("검증되지 않은 Flag 후보");
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
    content.append(candidateSection);
  }

  const noteMetadata = /^(source agent|stop reason|attempt|tool steps|original handoff|handoff audit|requested deliverable):/i;
  const approachNotes = (Array.isArray(challenge.approach_notes) ? challenge.approach_notes : [])
    .filter((note) => note?.text && !noteMetadata.test(note.text.trim()));
  const notesSection = drawerSection("지금까지의 접근 노트", `${approachNotes.length} NOTES`);
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
  content.append(notesSection);

  const agentsSection = drawerSection("Solver agents", `${challenge.agents.length} AGENTS`);
  if (!challenge.agents.length) {
    agentsSection.append(node("p", "agent-findings", "SOL 풀이를 시작하면 주 solver와 동적 하위 agent 상태가 여기에 표시됩니다."));
  }
  for (const agent of challenge.agents) {
    const card = node("article", "agent-card");
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
      node("span", "", formatMoney(agent.cost_usd)),
    );
    card.append(header, stats);
    if (agent.skill_path) card.append(node("p", "agent-skill", `Skill: ${agent.skill_path}`));
    if (agent.findings) card.append(node("p", "agent-findings", agent.findings));
    if (agent.stop_reason) card.append(node("p", "agent-stop-reason", `종료 사유: ${agent.stop_reason}`));
    if (agent.workspace_path) card.append(node("p", "agent-workspace", `산출물: ${agent.workspace_path}`));
    const traceButton = button("최근 trace 보기 →", "trace-button", () => loadTrace(challenge.name, agent.model_spec, card));
    card.append(traceButton);
    agentsSection.append(card);
  }
  content.append(agentsSection);

  if (challenge.active) {
    const broadcastSection = drawerSection("전체 solver에 힌트 전달");
    const form = node("form", "drawer-form");
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
    content.append(broadcastSection);
  }

  if (!challenge.solved) {
    const submitSection = drawerSection(isStandalone ? "로컬 Flag 후보 기록" : state.snapshot.no_submit ? "Flag 후보 기록" : "Flag 수동 제출");
    const form = node("form", "drawer-form");
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
    content.append(submitSection);
  }
}

function openDrawer(name) {
  if (!state.selectedChallenge) state.drawerReturnChallenge = name;
  state.selectedChallenge = name;
  renderDrawer();
  const drawer = byId("detail-drawer");
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  document.body.style.overflow = "hidden";
  byId("drawer-close").focus();
}

function closeDrawer() {
  const returnChallenge = state.drawerReturnChallenge;
  state.selectedChallenge = null;
  state.drawerReturnChallenge = null;
  const drawer = byId("detail-drawer");
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  document.body.style.overflow = "";
  const returnRow = [...byId("challenge-rows").querySelectorAll("tr")]
    .find((row) => row.dataset.challengeName === returnChallenge);
  if (returnRow) returnRow.focus();
}

async function loadTrace(challenge, model, card) {
  let output = card.querySelector(".trace-output");
  if (!output) {
    output = node("pre", "trace-output", "trace를 불러오는 중…");
    card.append(output);
  } else {
    output.textContent = "trace를 새로 불러오는 중…";
  }
  try {
    const query = new URLSearchParams({ challenge, model, last_n: "60" });
    const result = await api(`/api/trace?${query}`);
    output.textContent = result.trace || "기록된 trace가 없습니다.";
  } catch (error) {
    output.textContent = `Trace 오류: ${error.message}`;
  }
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
    if (state.selectedChallenge && renderSelected) renderDrawer();
  } catch (error) {
    setConnection(false);
  } finally {
    state.refreshing = false;
    byId("refresh-button").disabled = false;
    byId("refresh-button").removeAttribute("aria-busy");
  }
}

async function initialize() {
  byId("refresh-button").addEventListener("click", refresh);
  byId("drawer-close").addEventListener("click", closeDrawer);
  byId("drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.selectedChallenge) {
      closeDrawer();
      return;
    }
    if (event.key !== "Tab" || !state.selectedChallenge) return;
    const panel = byId("detail-drawer").querySelector(".drawer-panel");
    const focusable = [...panel.querySelectorAll("button, input, textarea, [tabindex]:not([tabindex='-1'])")]
      .filter((element) => !element.disabled && element.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
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
      closeDrawer();
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
    const resetSupported = session.capabilities?.reset_runtime === true
      || await endpointAvailable("/api/control/reset-runtime");
    byId("reset-open").disabled = !resetSupported;
    byId("reset-compatibility").hidden = resetSupported;
    if (!resetSupported) {
      byId("reset-open").textContent = "백엔드 재시작 필요";
      byId("reset-open").title = "실행 중인 coordinator가 초기화 API를 지원하지 않습니다.";
    }
    await refresh();
    state.refreshTimer = window.setInterval(refresh, 2500);
  } catch (error) {
    setConnection(false);
    toast(`초기화 실패: ${error.message}`, "error");
  }
}

initialize();
