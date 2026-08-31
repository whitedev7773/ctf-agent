"""Category-aware expert playbooks injected into solver prompts."""

from __future__ import annotations

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


def normalized_category(category: str) -> str:
    value = (category or "").strip().lower()
    return _ALIASES.get(value, value if value in _PLAYBOOKS else "misc")


def category_playbook(category: str) -> str:
    return _PLAYBOOKS[normalized_category(category)]


def solver_lane(model_spec: str) -> str:
    """Give parallel default models complementary jobs instead of identical prompts."""
    lowered = model_spec.lower()
    if "luna" in lowered or "mini" in lowered or "flash" in lowered:
        return (
            "Rapid triage lane: inventory the attack surface, automate cheap tests, and notify the "
            "coordinator early with concrete observations. Escalate promising paths with saved scripts."
        )
    if "terra" in lowered:
        return (
            "Systematic validation lane: build a complete model of the challenge, reproduce each primitive, "
            "and turn sibling hypotheses into reliable end-to-end solvers."
        )
    return (
        "Deep exploitation lane: pursue the hardest or most novel path, challenge assumptions, and produce "
        "a rigorous working exploit/solver rather than a shallow list of possibilities."
    )
