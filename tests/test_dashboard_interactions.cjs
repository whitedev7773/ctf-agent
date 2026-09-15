const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../backend/dashboard/static/dashboard.js"), "utf8");
const context = vm.createContext({ console });
vm.runInContext(source.slice(0, source.lastIndexOf("initialize();")), context);
const run = code => vm.runInContext(code, context);

(async () => {
  // A rebuilt detail tree retains disclosure state by identity, even after reordering.
  run(`
    window = {scrollX: 0, scrollY: 0, innerHeight: 800, scrollTo() {}, requestAnimationFrame(fn) { fn(); }};
    document = {scrollingElement: {scrollHeight: 800}};
    oldItems = [{dataset: {disclosureKey: "registration"}, open: true}, {dataset: {disclosureKey: "action-menu"}, open: false}];
    oldContent = {querySelectorAll(selector) { return selector.startsWith("details") ? oldItems : []; }};
    saved = captureDetailViewState(oldContent);
    newItems = [{dataset: {disclosureKey: "action-menu"}, open: true}, {dataset: {disclosureKey: "registration"}, open: false}];
    restoreDetailViewState({querySelectorAll(selector) { return selector.startsWith("details") ? newItems : []; }}, saved);
  `);
  assert.equal(run("newItems[0].open"), false);
  assert.equal(run("newItems[1].open"), true);

  // Timing ticks do not rebuild the detail tree; meaningful status changes still do.
  assert.equal(run('detailRenderSignature({name:"demo",elapsed_seconds:1}) === detailRenderSignature({name:"demo",elapsed_seconds:2})'), true);
  assert.equal(run('detailRenderSignature({name:"demo",status:"idle"}) === detailRenderSignature({name:"demo",status:"active"})'), false);

  // Drafts follow field names when the layout changes, without reverting fresh metadata.
  run(`
    saved.fields = [{name: "message", value: "draft"}, {name: "category", value: "old", registration: true}];
    draftFields = [{name: "category", value: "new"}, {name: "message", value: ""}];
    restoreDetailViewState({querySelectorAll(selector) { return selector === "input, textarea" ? draftFields : []; }}, saved);
  `);
  assert.equal(run("draftFields[0].value"), "new");
  assert.equal(run("draftFields[1].value"), "draft");

  // Failed requests preserve drafts; successful ones clear before refresh captures fields.
  run(`
    toast = () => {};
    draft = "unsent";
    observedDraft = null;
    originalRefresh = refresh;
    refresh = async () => { observedDraft = draft; };
    api = async () => { throw new Error("offline"); };
  `);
  assert.equal(await run('runCommand("/test", {}, null, () => { draft = ""; })'), false);
  assert.equal(run("draft"), "unsent");
  run('api = async () => ({message: "ok"});');
  assert.equal(await run('runCommand("/test", {}, null, () => { draft = ""; })'), true);
  assert.equal(run("observedDraft"), "");

  // Modal Escape stays in detail; regular Escape navigates back.
  run(`
    state.selectedChallenge = "demo";
    closed = 0;
    closeChallengeDetail = () => { closed++; };
    document.querySelector = () => ({});
    handleEscape({key: "Escape"});
  `);
  assert.equal(run("closed"), 0);
  run('document.querySelector = () => null; handleEscape({key: "Escape", defaultPrevented: true});');
  assert.equal(run("closed"), 0);
  run('handleEscape({key: "Escape"});');
  assert.equal(run("closed"), 1);

  // Retry the actual refresh path after the first session request fails.
  run(`
    refresh = originalRefresh;
    state.selectedChallenge = null;
    state.csrfToken = "";
    controls = new Map();
    document.getElementById = id => {
      if (!controls.has(id)) controls.set(id, {dataset: {}, textContent: "초기화", setAttribute() {}, removeAttribute() {}});
      return controls.get(id);
    };
    attempts = 0; online = false; views = 0;
    api = async path => {
      if (path === "/api/session") {
        if (++attempts === 1) throw new Error("offline");
        return {csrf_token: "recovered", capabilities: {reset_runtime: true}};
      }
      return {challenges: []};
    };
    syncFrontendClocks = notifyCandidateReviews = renderOverview = renderRows = renderLiveClocks = () => {};
    hasActiveTextSelection = () => false;
    setConnection = value => { online = value; };
    syncViewFromLocation = () => { views++; };
  `);
  await run("refresh()");
  assert.equal(run("online"), false);
  assert.equal(run("state.refreshing"), false);
  await run("refresh()");
  assert.equal(run("online"), true);
  assert.equal(run("state.csrfToken"), "recovered");
  assert.equal(run("views"), 1);
  await run("refresh()");
  assert.equal(run("attempts"), 2);
  assert.equal(run("views"), 1);
  // Closing a modal restores the original live form node and its trigger.
  run(`
    modalDialog = {open: true, close() { this.open = false; }, querySelector() { return null; }};
    modalBody = {replaceChildren() {}};
    restoredNode = null; focused = false; afterClose = false;
    originalForm = {hidden: false, value: "unsaved draft"};
    document.documentElement = {classList: {toggle() {}}};
    document.querySelector = () => modalDialog.open ? modalDialog : null;
    document.getElementById = id => id === "workspace-dialog" ? modalDialog : modalBody;
    state.modal = {
      content: originalForm, wasHidden: true,
      marker: {parentNode: {}, replaceWith(value) { restoredNode = value; }},
      trigger: {isConnected: true, focus() { focused = true; }},
      onClose() { afterClose = true; },
    };
    renderDetailPage();
    closeWorkspaceModal();
  `);
  assert.equal(run("restoredNode === originalForm"), true);
  assert.equal(run("originalForm.value"), "unsaved draft");
  assert.equal(run("originalForm.hidden && focused && afterClose"), true);
  assert.equal(run("state.modal"), null);

  // Confirmation requests serialize and cancellation never approves a queued action.
  run(`
    confirmations = new Map(); shown = 0;
    confirmation = {open: false, showModal() { this.open = true; shown++; }, close() { this.open = false; }};
    document.querySelector = () => confirmation.open ? confirmation : null;
    document.getElementById = id => {
      if (id === "confirmation-dialog") return confirmation;
      if (!confirmations.has(id)) confirmations.set(id, {focus() {}});
      return confirmations.get(id);
    };
  `);
  const firstConfirmation = run('confirmAction("first")');
  const secondConfirmation = run('confirmAction("second")');
  await Promise.resolve();
  assert.equal(run("shown"), 1);
  run('confirmations.get("confirmation-cancel").onclick()');
  assert.equal(await firstConfirmation, false);
  await Promise.resolve();
  assert.equal(run("shown"), 2);
  run('confirmations.get("confirmation-accept").onclick()');
  assert.equal(await secondConfirmation, true);

  // Registration can distinguish a saved challenge from an auto-start failure.
  run(`
    spawnToasts = [];
    api = async () => ({started: false, message: "at capacity"});
    refresh = async () => {};
    toast = (message, type) => { spawnToasts.push({message, type}); };
  `);
  const capacityResult = await run('spawnChallenge("demo")');
  assert.equal(capacityResult.started, false);
  assert.equal(run('spawnToasts[0].type'), "warning");
  run('api = async () => { throw new Error("offline"); };');
  const silentFailure = await run('spawnChallenge("demo", {silent: true})');
  assert.equal(silentFailure.started, false);
  assert.equal(silentFailure.message, "offline");
  assert.equal(run('spawnToasts.length'), 1);
  console.log("Dashboard interaction regressions passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
