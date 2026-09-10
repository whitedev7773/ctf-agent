const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "../backend/dashboard/static/dashboard.js"), "utf8");
// Load clock helpers without starting the page or any network requests.
const helpers = source.slice(0, source.indexOf("function renderLiveClocks()"));
let now = 0;
function browser() {
  const context = vm.createContext({performance: {now: () => now}});
  vm.runInContext(helpers, context);
  return code => vm.runInContext(code, context);
}
const tab = browser();
const snapshot = (uptime, elapsed, active) => `syncFrontendClocks({uptime_seconds:${uptime}, challenges:[{name:"test", elapsed_seconds:${elapsed}, active:${active}}]})`;
tab(snapshot(200, 100, true));
now = 60000;
assert.equal(tab('sampledElapsedSeconds(state.uptimeBaseSeconds)'), 260);
assert.equal(tab('challengeElapsedSeconds({name:"test"})'), 160);
tab(snapshot(260, 150, false));
now = 120000;
assert.equal(tab('challengeElapsedSeconds({name:"test"})'), 150);
const freshTab = browser();
freshTab(snapshot(320, 150, false));
assert.equal(freshTab('challengeElapsedSeconds({name:"test"})'), 150);
// A server or challenge restart must override older, larger browser values.
tab(snapshot(2, 1, true));
assert.equal(tab('sampledElapsedSeconds(state.uptimeBaseSeconds)'), 2);
assert.equal(tab('challengeElapsedSeconds({name:"test"})'), 1);
console.log("Dashboard clock regressions passed");
