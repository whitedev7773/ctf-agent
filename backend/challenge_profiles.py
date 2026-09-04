"""Category-aware expert playbooks injected into solver prompts."""

from __future__ import annotations

from dataclasses import dataclass

from backend.model_specs import model_id_from_spec

_ALIASES = {
    "binary": "pwn",
    "exploitation": "pwn",
    "reverse": "reversing",
    "rev": "reversing",
    "re": "reversing",
    "crypto": "cryptography",
    "forensic": "forensics",
    "steg": "forensics",
    "steganography": "forensics",
    "kernel": "pwn",
    "mobile": "android",
    "apk": "android",
    "smart contract": "blockchain",
    "web3": "blockchain",
}


_PLAYBOOKS: dict[str, str] = {
    "pwn": """### Pwn specialist playbook
- Fingerprint architecture, mitigations, loader and libc first (`file`, `checksec`, `readelf`, `ldd`).
- Reproduce locally with the supplied loader/libc; use `patchelf`, pwntools tubes and corefiles.
- Audit allocator and protocol state, then choose the shortest reliable primitive: ret2libc/ROP, format string, heap, race, seccomp bypass, or logic flaw.
- Write a deterministic exploit under `/challenge/workspace/`, add retries for ASLR/network jitter, and test it repeatedly before submission.
- For foreign architectures use `qemu-*-static` plus `gdb-multiarch`; for kernel material inspect configs, symbols and intended device surface before fuzzing.""",
    "reversing": """### Reversing specialist playbook
- Identify format, ISA, packing, managed runtime and anti-debugging before decompilation.
- Combine static (`r2`, `objdump`, `pyghidra`, `jadx`/`apktool`) and dynamic (`gdb`, `strace`, `ltrace`, QEMU) evidence.
- Once static analysis identifies a concrete layout, boundary or state transition, build the smallest harness that measures it before another broad extraction/decompilation pass.
- Lift verification logic into a small Python/Z3/angr solver; avoid manually transcribing large constants when scripts can extract them.
- Search for custom VM bytecode, opaque predicates, self-modification and crypto misuse. Preserve every extractor and patch in `/challenge/workspace/`.""",
    "cryptography": """### Cryptography specialist playbook
- Parse parameters exactly and build a reproducible Sage/Python solver before attempting guesses.
- Test structural weaknesses: nonce/key reuse, related messages, partial leakage, invalid curves, subgroup attacks, padding/oracle behavior and biased randomness.
- For lattices derive bounds and dimension explicitly; validate recovered secrets against the original equations before decoding.
- Use Sage, fpylll/flatter, RsaCtfTool, CADO-NFS and Z3 selectively. Save scripts and intermediate factors/bases in `/challenge/workspace/`.""",
    "web": """### Web specialist playbook
- Map routes, methods, cookies, JavaScript bundles, API schemas and trust boundaries before fuzzing blindly.
- Maintain a stateful Python requests/httpx client in `/challenge/workspace/`; record raw requests that prove each primitive.
- Check auth/session confusion, parser differentials, request smuggling, cache behavior, deserialization, template/query injection, SSRF and race conditions.
- For browser-only behavior inspect bundles and CSP, then use Playwright/Chromium when available; use OOB callbacks only within contest scope.""",
    "forensics": """### Forensics specialist playbook
- Hash and identify every input, preserve originals, and work only on copies in `/challenge/workspace/`.
- Build a timeline across filesystem, packet, memory and metadata evidence instead of relying on one carving tool.
- Use Sleuth Kit, Volatility, tshark, binwalk, YARA, exiftool and media transforms; verify offsets and recovered encodings with scripts.
- Treat nested archives, polyglots, alternate streams, deleted records and timestamp manipulation as first-class hypotheses.""",
    "misc": """### Misc specialist playbook
- Classify the underlying domain quickly: protocol, constraint solving, image/audio, game, automation, esolang or data science.
- Convert repetitive interaction into a deterministic script and capture samples before optimizing.
- Look for representation mismatches, floating-point edges, Unicode/parser differences, race conditions and unintended solver shortcuts.
- Preserve generators, decoders and interaction scripts in `/challenge/workspace/`.""",
    "android": """### Android specialist playbook
- Decode the manifest, resources and smali with apktool, then inspect DEX/native libraries with androguard, r2 and pyghidra.
- Trace exported components, deep links, WebViews, intent extras, local storage, JNI boundaries and certificate/pinning logic.
- Reproduce cryptographic and protocol code outside the app where possible; save patches, hooks and extraction scripts in `/challenge/workspace/`.
- Distinguish static-secret recovery from behavior that truly requires an emulator/device before spending time on dynamic instrumentation.""",
    "blockchain": """### Blockchain specialist playbook
- Reconstruct contract state, roles and exact transaction sequence; do not reason from isolated functions only.
- Check reentrancy, delegatecall/storage collisions, signature replay/malleability, oracle assumptions, precision, CREATE2 and callback ordering.
- Build a local Python web3 reproduction and compute calldata/storage slots explicitly. Preserve transaction scripts and state assumptions.
- Validate that the exploit changes the challenge's win condition, not merely that one suspicious call succeeds.""",
}

_EXTERNAL_SKILLS_BY_CATEGORY = {
    "pwn": "ctf-pwn",
    "reversing": "ctf-reverse",
    "cryptography": "ctf-crypto",
    "web": "ctf-web",
    "forensics": "ctf-forensics",
    "misc": "ctf-misc",
    "android": "ctf-reverse",
    "blockchain": "ctf-misc",
}


def normalized_category(category: str) -> str:
    value = (category or "").strip().lower()
    return _ALIASES.get(value, value if value in _PLAYBOOKS else "misc")


def category_playbook(category: str) -> str:
    return _PLAYBOOKS[normalized_category(category)]


def external_skill_path(category: str) -> str:
    """Route a challenge to one pinned ctf-skills entry point in the sandbox."""
    raw = (category or "").strip().lower()
    if "osint" in raw:
        skill = "ctf-osint"
    elif "malware" in raw:
        skill = "ctf-malware"
    elif raw in {"ai", "ml", "ai/ml", "machine learning", "artificial intelligence"}:
        skill = "ctf-ai-ml"
    else:
        skill = _EXTERNAL_SKILLS_BY_CATEGORY[normalized_category(category)]
    return f"/challenge/skills/{skill}/SKILL.md"


@dataclass(frozen=True)
class SolverRole:
    """One explicit responsibility in a challenge swarm."""

    key: str
    title: str
    objective: str
    instructions: tuple[str, ...]
    handoff_path: str
    handoff_wait_factor: float

    def prompt(self) -> str:
        lines = [f"### {self.title}", self.objective]
        lines.extend(f"- {instruction}" for instruction in self.instructions)
        lines.append(f"- Required handoff: `{self.handoff_path}`")
        return "\n".join(lines)


SCOUT_ROLE = SolverRole(
    key="scout",
    title="Rapid triage lane (SCOUT)",
    objective="Own intake and triage. Produce a small evidence-backed map before deep solving begins.",
    instructions=(
        "Inspect `/challenge/shared/` first and never repeat facts already recorded there.",
        "Fingerprint inputs, protections, format and the likely validation/attack surface using bounded cheap tools.",
        "Separate confirmed facts from ranked hypotheses and identify one falsifiable solve route.",
        "By tool step 8, write the handoff and call `notify_coordinator` with decisive facts and artifact paths.",
        "Avoid long symbolic jobs, broad web searches and exhaustive debugger traces; hand those to the analyst.",
    ),
    handoff_path="/challenge/shared/scout/TRIAGE.md",
    handoff_wait_factor=0.0,
)

ANALYST_ROLE = SolverRole(
    key="analyst",
    title="Systematic solve lane (ANALYST)",
    objective="Own the primary solve path and convert triage evidence into a reproducible end-to-end solver.",
    instructions=(
        "Start by reading `/challenge/shared/scout/TRIAGE.md`; perform only missing minimum triage.",
        "Model the verifier, protocol or exploit primitive and test the cheapest high-value hypothesis first.",
        "Prefer bounded targeted debugger/tool runs over broad scans, timing guesses or repeated inventory.",
        "Save the final `solve.py`, `solve.sage` or `exploit.py` under `/challenge/shared/analyst/`.",
        "Notify the coordinator after every decisive primitive, disproved route or candidate-producing run.",
    ),
    handoff_path="/challenge/shared/analyst/SOLUTION.md",
    handoff_wait_factor=1.0,
)

VERIFIER_ROLE = SolverRole(
    key="verifier",
    title="Independent verification lane (VERIFIER)",
    objective="Own independent evidence review, hard-gap closure and final candidate verification.",
    instructions=(
        "Read scout and analyst handoffs before invoking routine inventory tools.",
        "Challenge assumptions and focus on the narrowest unresolved blocker or an orthogonal high-value method.",
        "Reproduce the final solver against the untouched target in a clean process/session.",
        "Reject reflected input, patched success branches, static placeholders and unverified guesses.",
        "Use `flag_found` only for direct eligible output; otherwise return `incomplete` with the exact gap.",
    ),
    handoff_path="/challenge/shared/verifier/VERIFY.md",
    handoff_wait_factor=2.0,
)

LEAD_ROLE = SolverRole(
    key="lead",
    title="Primary solve owner (LEAD)",
    objective=(
        "Own the end-to-end solve, preserve the global hypothesis tree, and use bounded "
        "delegation only when an independent subproblem can be specified precisely."
    ),
    instructions=(
        "Read existing shared artifacts, then establish the main solve route yourself. Maintain `/challenge/shared/lead/STATE.md` with Confirmed facts, Conflicts, Current blocker, and Next experiment.",
        "Keep the critical path; never delegate the whole challenge or wait idly for workers.",
        "Use `delegate_task` only for narrow parallel work with a falsifiable question and explicit deliverable.",
        "Check delegate status at natural decision points. Recompute critical offsets/assumptions and do not integrate a handoff whose evidence audit reports conflicts or missing reproduction.",
        "After a static primitive is credible, prioritize a minimal executable harness or debugger measurement; bulk extraction is not progress unless it answers the current blocker.",
        "Reproduce the final exploit or solver directly before reporting a candidate.",
    ),
    handoff_path="/challenge/shared/lead/SOLUTION.md",
    handoff_wait_factor=0.0,
)

DELEGATE_ROLE = SolverRole(
    key="delegate",
    title="Bounded delegated worker (DELEGATE)",
    objective="Answer one narrow question for the lead with the cheapest decisive experiment.",
    instructions=(
        "Work only on the delegated task; do not restart broad challenge triage.",
        "Read existing shared evidence first and avoid duplicating the lead or another worker.",
        "Prefer a small script or bounded tool run that proves or disproves the assigned hypothesis.",
        "Cross-check arithmetic and layout claims against source or observed addresses; explicitly name any conflict with an existing handoff.",
        "Publish concise positive or negative evidence using the required Conclusion, Evidence, Reproduction, and Assumptions and conflicts sections.",
        "Stop after the requested deliverable; the lead owns integration and final verification.",
    ),
    handoff_path="/challenge/shared/delegates/",
    handoff_wait_factor=0.0,
)

SPECIALIST_ROLE = SolverRole(
    key="specialist",
    title="Orthogonal specialist lane (SPECIALIST)",
    objective="Pursue one complementary hypothesis that the named scout, analyst and verifier do not cover.",
    instructions=(
        "Read every existing handoff before selecting a lane.",
        "State the one hypothesis you own and avoid duplicating active work.",
        "Use bounded experiments and publish positive or negative evidence promptly.",
        "Leave reproducible scripts and concise notes for the other roles.",
    ),
    handoff_path="/challenge/shared/specialist/HANDOFF.md",
    handoff_wait_factor=1.0,
)


def solver_role(model_spec: str) -> SolverRole:
    """Map known model families to stable complementary responsibilities."""
    if any(part.startswith("delegate-") for part in model_spec.split("/")[3:]):
        return DELEGATE_ROLE
    model_id = model_id_from_spec(model_spec).lower()
    if any(marker in model_id for marker in ("luna", "mini", "flash", "spark")):
        return SCOUT_ROLE
    if "terra" in model_id:
        return ANALYST_ROLE
    if "sol" in model_id:
        return LEAD_ROLE
    if "opus" in model_id:
        return VERIFIER_ROLE
    return SPECIALIST_ROLE


def solver_lane(model_spec: str) -> str:
    """Return the complete role contract injected into a solver prompt."""
    return solver_role(model_spec).prompt()


def format_solver_roster(model_specs: list[str]) -> str:
    """Describe configured assignments for coordinator prompts and diagnostics."""
    return "\n".join(
        f"- `{spec}` → {solver_role(spec).title}: {solver_role(spec).objective}"
        for spec in model_specs
    )
