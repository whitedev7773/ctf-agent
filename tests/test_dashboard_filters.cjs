const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../backend/dashboard/static/dashboard.js"), "utf8");
const helpers = source.slice(0, source.indexOf("function currentChallenge"));
const context = vm.createContext({ URL, window: { location: { href: "http://localhost/" } } });
vm.runInContext(helpers, context);
const run = code => vm.runInContext(code, context);

run(`state.snapshot = {challenges: [
  {name:"web/intro", category:"web", value:100, solves:24, status:"solved", solved:true, documented:true, agents:[{}], elapsed_seconds:90, cost_usd:0.2},
  {name:"pwn/heap", category:"pwn", value:500, solves:2, status:"active", solved:false, documented:false, agents:[{}], elapsed_seconds:800, cost_usd:1.4},
  {name:"crypto/rsa", category:"crypto", value:300, solves:5, status:"idle", solved:false, documented:false, agents:[], elapsed_seconds:0, cost_usd:0},
  {name:"web/admin", category:"web", value:250, solves:8, status:"solved", solved:true, documented:false, agents:[{}], elapsed_seconds:400, cost_usd:0.7}
]}`);

assert.deepEqual(Array.from(run("filteredChallenges().map(item => item.name)")), [
  "crypto/rsa", "pwn/heap", "web/admin", "web/intro",
]);

run('state.filters.query = "WEB"');
assert.deepEqual(Array.from(run("filteredChallenges().map(item => item.name)")), ["web/admin", "web/intro"]);

run('state.filters.query = ""; state.filters.statuses = new Set(["active", "idle"])');
assert.deepEqual(Array.from(run("filteredChallenges().map(item => item.name)")), ["crypto/rsa", "pwn/heap"]);

run('state.filters.statuses = new Set(); state.filters.categories = new Set(["web"]); state.filters.minPoints = "200"');
assert.deepEqual(Array.from(run("filteredChallenges().map(item => item.name)")), ["web/admin"]);

run('state.filters.categories = new Set(); state.filters.minPoints = ""; state.filters.documentation = "pending"');
assert.deepEqual(Array.from(run("filteredChallenges().map(item => item.name)")), ["web/admin"]);

run('state.filters.documentation = "all"; state.filters.assignment = "unassigned"');
assert.deepEqual(Array.from(run("filteredChallenges().map(item => item.name)")), ["crypto/rsa"]);

run('state.filters.assignment = "all"; state.filters.maxSolves = "5"; state.filters.maxCost = "1"');
assert.deepEqual(Array.from(run("filteredChallenges().map(item => item.name)")), ["crypto/rsa"]);

run('state.filters.maxSolves = ""; state.filters.maxCost = ""; state.filters.sort = "points-desc"');
assert.deepEqual(Array.from(run("filteredChallenges().map(item => item.name)")), [
  "pwn/heap", "crypto/rsa", "web/admin", "web/intro",
]);

context.window.location.href = "http://localhost/?q=heap&status=active%2Cidle&category=pwn&doc=pending&agents=assigned&max_cost=2&sort=cost-desc";
run("state.filters = filtersFromLocation()");
assert.equal(run("state.filters.query"), "heap");
assert.deepEqual(Array.from(run("[...state.filters.statuses].sort()")), ["active", "idle"]);
assert.deepEqual(Array.from(run("[...state.filters.categories]")), ["pwn"]);
assert.equal(run("state.filters.documentation"), "pending");
assert.equal(run("state.filters.assignment"), "assigned");
assert.equal(run("state.filters.maxCost"), "2");
assert.equal(run("state.filters.sort"), "cost-desc");

context.window.history = {
  state: null,
  replaceState: (_state, _title, url) => { context.window.location.href = String(url); },
};
run("syncFilterUrl()");
const restored = new URL(context.window.location.href);
assert.equal(restored.searchParams.get("q"), "heap");
assert.equal(restored.searchParams.get("status"), "active,idle");
assert.deepEqual(restored.searchParams.getAll("category"), ["pwn"]);
assert.equal(restored.searchParams.get("max_cost"), "2");
assert.equal(restored.searchParams.get("sort"), "cost-desc");

console.log("Dashboard filter regressions passed");
