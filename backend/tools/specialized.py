"""Category-focused helpers for high-signal CTF analysis.

The helpers in this module deliberately return compact, machine-readable
summaries.  They do not replace the sandbox or the evidence ledger; they make
the first useful observation reusable by every solver lane.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from backend.tools.core import MAX_OUTPUT, _is_internal_url, _truncate

_ALLOWED_ROOTS = ("/challenge/distfiles", "/challenge/workspace", "/challenge/shared")
_MAX_WEB_BODY = 20_000
_MAX_PARALLEL_REQUESTS = 32


def _safe_container_path(path: str) -> str:
    """Accept only paths inside the challenge-controlled container roots."""
    value = str(path or "").strip()
    if not value.startswith("/"):
        raise ValueError("path must be an absolute /challenge path")
    candidate = PurePosixPath(value)
    if ".." in candidate.parts:
        raise ValueError("path traversal is not allowed")
    normalized = str(candidate)
    if not any(normalized == root or normalized.startswith(root + "/") for root in _ALLOWED_ROOTS):
        raise ValueError("path must be under /challenge/distfiles, /challenge/workspace, or /challenge/shared")
    return normalized


def _redact_header(name: str, value: str) -> str:
    if name.casefold() in {"set-cookie", "authorization", "proxy-authorization"}:
        return "[REDACTED]"
    return value[:500]


def _header_summary(headers: httpx.Headers) -> dict[str, str]:
    return {name: _redact_header(name, value) for name, value in headers.items()}


@dataclass
class WebResponseRecord:
    id: str
    method: str
    url: str
    status_code: int
    elapsed_ms: float
    headers: dict[str, str]
    body: str
    body_sha256: str
    created_at: float = field(default_factory=time.time)


@dataclass
class WebSession:
    id: str
    base_url: str
    client: httpx.AsyncClient
    records: list[WebResponseRecord] = field(default_factory=list)


class WebSessionStore:
    """Small per-solver HTTP state store with bounded, replayable records."""

    def __init__(self) -> None:
        self.sessions: dict[str, WebSession] = {}
        self._lock = asyncio.Lock()

    async def open(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> tuple[WebSession, WebResponseRecord | None]:
        _validate_web_url(base_url)
        session_id = f"web-{uuid.uuid4().hex[:10]}"
        client = httpx.AsyncClient(
            verify=False,
            timeout=30.0,
            follow_redirects=follow_redirects,
            headers={"User-Agent": "ctf-agent/2.0", **(headers or {})},
        )
        session = WebSession(session_id, base_url, client)
        async with self._lock:
            self.sessions[session_id] = session
        try:
            record = await self.request(session_id, base_url, method="GET")
        except Exception:
            await self.close(session_id)
            raise
        return session, record

    async def request(
        self,
        session_id: str,
        url: str,
        *,
        method: str = "GET",
        body: str = "",
        headers: dict[str, str] | None = None,
    ) -> WebResponseRecord:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown web session: {session_id}")
        target = urljoin(session.base_url, url)
        _validate_web_url(target)
        started = time.perf_counter()
        response = await session.client.request(
            method.upper(),
            target,
            content=body or None,
            headers=headers,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        text = response.text
        bounded = text if len(text) <= _MAX_WEB_BODY else text[:_MAX_WEB_BODY] + "\n...[truncated]"
        record = WebResponseRecord(
            id=f"resp-{uuid.uuid4().hex[:10]}",
            method=method.upper(),
            url=str(response.url),
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            headers=_header_summary(response.headers),
            body=bounded,
            body_sha256=hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        )
        session.records.append(record)
        session.records[:] = session.records[-100:]
        return record

    async def parallel(
        self,
        session_id: str,
        url: str,
        *,
        count: int,
        method: str = "GET",
        body: str = "",
        headers: dict[str, str] | None = None,
    ) -> list[WebResponseRecord]:
        count = max(1, min(int(count), _MAX_PARALLEL_REQUESTS))
        semaphore = asyncio.Semaphore(min(count, 8))

        async def one() -> WebResponseRecord:
            async with semaphore:
                return await self.request(
                    session_id,
                    url,
                    method=method,
                    body=body,
                    headers=headers,
                )

        return list(await asyncio.gather(*(one() for _ in range(count))))

    def get_record(self, session_id: str, response_id: str) -> WebResponseRecord:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown web session: {session_id}")
        record = next((item for item in session.records if item.id == response_id), None)
        if record is None:
            raise KeyError(f"unknown response: {response_id}")
        return record

    async def close(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            await session.client.aclose()

    async def close_all(self) -> None:
        for session_id in list(self.sessions):
            await self.close(session_id)


def _validate_web_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must use http or https")
    if _is_internal_url(url):
        raise ValueError("access to internal/private networks is blocked")


def _record_json(record: WebResponseRecord, *, include_body: bool = False) -> dict[str, Any]:
    payload = {
        "id": record.id,
        "method": record.method,
        "url": record.url,
        "status_code": record.status_code,
        "elapsed_ms": record.elapsed_ms,
        "headers": record.headers,
        "body_sha256": record.body_sha256,
        "body_length": len(record.body),
        "created_at": record.created_at,
    }
    if include_body:
        payload["body"] = record.body
    return payload


async def do_web_session_open(
    store: WebSessionStore,
    base_url: str,
    headers: dict[str, str] | None = None,
) -> str:
    session, initial = await store.open(base_url, headers=headers)
    return json.dumps(
        {
            "session_id": session.id,
            "base_url": session.base_url,
            "initial_response": _record_json(initial, include_body=True) if initial else None,
            "cookie_state": "maintained internally; values are not returned",
        },
        ensure_ascii=False,
    )


async def do_web_session_request(
    store: WebSessionStore,
    session_id: str,
    url: str,
    method: str = "GET",
    body: str = "",
    headers: dict[str, str] | None = None,
) -> str:
    record = await store.request(session_id, url, method=method, body=body, headers=headers)
    return json.dumps(_record_json(record, include_body=True), ensure_ascii=False)


async def do_web_parallel_requests(
    store: WebSessionStore,
    session_id: str,
    url: str,
    count: int = 2,
    method: str = "GET",
    body: str = "",
    headers: dict[str, str] | None = None,
) -> str:
    records = await store.parallel(
        session_id,
        url,
        count=count,
        method=method,
        body=body,
        headers=headers,
    )
    return json.dumps(
        {
            "count": len(records),
            "responses": [_record_json(record) for record in records],
            "note": "Use only for the authorized challenge endpoint and keep the count bounded.",
        },
        ensure_ascii=False,
    )


async def do_web_session_diff(
    store: WebSessionStore,
    session_id: str,
    response_a: str,
    response_b: str,
) -> str:
    first = store.get_record(session_id, response_a)
    second = store.get_record(session_id, response_b)
    body_a, body_b = first.body, second.body
    mismatch = next(
        (
            index
            for index, (left, right) in enumerate(zip(body_a, body_b, strict=False))
            if left != right
        ),
        min(len(body_a), len(body_b)),
    )
    return json.dumps(
        {
            "response_a": _record_json(first),
            "response_b": _record_json(second),
            "same_status": first.status_code == second.status_code,
            "same_body": first.body_sha256 == second.body_sha256,
            "first_body_mismatch_offset": mismatch,
            "body_a_context": body_a[max(0, mismatch - 120) : mismatch + 240],
            "body_b_context": body_b[max(0, mismatch - 120) : mismatch + 240],
        },
        ensure_ascii=False,
    )


async def do_web_session_export(
    store: WebSessionStore,
    sandbox: Any,
    session_id: str,
) -> str:
    session = store.sessions.get(session_id)
    if session is None:
        raise KeyError(f"unknown web session: {session_id}")
    path = f"/challenge/shared/web/sessions/{session_id}.json"
    payload = {
        "session_id": session.id,
        "base_url": session.base_url,
        "cookie_state": "redacted",
        "responses": [_record_json(record) for record in session.records],
    }
    await sandbox.write_file(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return f"Exported {len(session.records)} redacted response records to {path}"


async def do_binary_triage(
    sandbox: Any,
    path: str,
    shared_path: str = "/challenge/shared/reversing/binary-analysis.json",
) -> str:
    target = _safe_container_path(path)
    command = "\n".join(
        [
            "set +e",
            f"target={shlex.quote(target)}",
            'printf "[FILE]\\n"; file -b "$target"',
            'printf "[STAT]\\n"; stat -c "%s %y" "$target"',
            'printf "[HASH]\\n"; sha256sum "$target"',
            'printf "[ELF_HEADER]\\n"; readelf -h "$target" 2>/dev/null | head -40',
            'printf "[PROGRAM_HEADERS]\\n"; readelf -W -l "$target" 2>/dev/null | grep -E "Type|GNU_STACK|GNU_RELRO|INTERP" | head -40',
            'printf "[SYMBOLS]\\n"; readelf -Ws "$target" 2>/dev/null | head -80',
            'printf "[STRINGS]\\n"; strings -n 8 "$target" 2>/dev/null | head -80',
        ]
    )
    result = await sandbox.exec(command, timeout_s=90)
    raw = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    manifest = _parse_binary_manifest(target, raw, result.exit_code)
    shared = _safe_container_path(shared_path)
    await sandbox.write_file(shared, json.dumps(manifest, ensure_ascii=False, indent=2))
    return json.dumps(manifest, ensure_ascii=False)


def _section(raw: str, name: str) -> str:
    match = re.search(rf"(?ms)^\[{re.escape(name)}\]\n(.*?)(?=^\[[A-Z_]+\]\n|\Z)", raw)
    return match.group(1).strip() if match else ""


def _parse_binary_manifest(path: str, raw: str, exit_code: int) -> dict[str, Any]:
    file_type = _first_line(_section(raw, "FILE"))
    stat_line = _section(raw, "STAT").splitlines()[0] if _section(raw, "STAT") else ""
    hash_line = _section(raw, "HASH").splitlines()[0] if _section(raw, "HASH") else ""
    header = _section(raw, "ELF_HEADER")
    programs = _section(raw, "PROGRAM_HEADERS")
    protections: list[str] = []
    if re.search(r"(?m)^Type:\s+DYN\b", header):
        protections.append("PIE")
    elif "Type:" in header:
        protections.append("non-PIE")
    if "GNU_STACK" in programs and "RWE" not in programs:
        protections.append("NX")
    if "GNU_RELRO" in programs:
        protections.append("RELRO")
    entry = re.search(r"Entry point address:\s+(0x[0-9a-fA-F]+)", header)
    return {
        "version": 1,
        "path": path,
        "exit_code": exit_code,
        "file_type": file_type,
        "stat": stat_line,
        "sha256": _extract_sha256(hash_line),
        "architecture": _first_match(header, r"Class:\s+(.+)")
        or _first_match(header, r"Machine:\s+(.+)"),
        "entrypoint": entry.group(1) if entry else "",
        "protections": protections,
        "symbols": _section(raw, "SYMBOLS").splitlines()[:80],
        "strings": _section(raw, "STRINGS").splitlines()[:80],
        "raw_summary": _truncate(raw, MAX_OUTPUT),
        "next_actions": [
            "Compare the manifest with a controlled native execution before symbolic exploration.",
            "Inspect only candidate functions or strings related to input and validation paths.",
        ],
    }


def _first_match(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(1).strip()[:200] if match else ""


def _first_line(text: str) -> str:
    return text.splitlines()[0][:500] if text else ""


def _extract_sha256(line: str) -> str:
    match = re.search(r"\b[a-fA-F0-9]{64}\b", line)
    return match.group(0).lower() if match else ""


async def do_forensic_triage(
    sandbox: Any,
    path: str,
    mode: str = "auto",
    shared_path: str = "/challenge/shared/forensics/triage.json",
) -> str:
    target = _safe_container_path(path)
    mode = (mode or "auto").casefold()
    if mode not in {"auto", "disk", "pcap", "memory", "archive"}:
        raise ValueError("mode must be auto, disk, pcap, memory, or archive")
    probe = await sandbox.exec(_forensic_probe_command(target), timeout_s=30)
    probe_raw = "\n".join(part for part in (probe.stdout, probe.stderr) if part).strip()
    file_type = _first_line(_section(probe_raw, "FILE"))
    kind = _infer_forensic_kind(file_type, probe_raw, mode)
    detail = _forensic_detail_command(target, kind)
    detail_raw = ""
    detail_exit_code = 0
    if detail:
        detail_result = await sandbox.exec(detail, timeout_s=90)
        detail_raw = "\n".join(
            part for part in (detail_result.stdout, detail_result.stderr) if part
        ).strip()
        detail_exit_code = detail_result.exit_code
    raw = "\n".join(part for part in (probe_raw, detail_raw) if part).strip()
    hash_line = _section(raw, "HASH").splitlines()[0] if _section(raw, "HASH") else ""
    recommendations = _forensic_recommendations(kind, raw)
    manifest = {
        "version": 1,
        "path": target,
        "mode": mode,
        "inferred_kind": kind,
        "exit_code": detail_exit_code if detail else probe.exit_code,
        "file_type": file_type,
        "sha256": _extract_sha256(hash_line),
        "sections": {
            name.casefold(): _section(raw, name)[:4000]
            for name in ("STAT", "MAGIC", "PARTITIONS", "PCAP", "MEMORY", "ARCHIVE")
            if _section(raw, name)
        },
        "recommendations": recommendations,
        "raw_summary": _truncate(raw, MAX_OUTPUT),
    }
    shared = _safe_container_path(shared_path)
    await sandbox.write_file(shared, json.dumps(manifest, ensure_ascii=False, indent=2))
    return json.dumps(manifest, ensure_ascii=False)


def _forensic_probe_command(target: str) -> str:
    return "\n".join(
        [
            "set +e",
            f"target={shlex.quote(target)}",
            'printf "[FILE]\\n"; file -b "$target"',
            'printf "[STAT]\\n"; stat -c "%s %y" "$target"',
            'printf "[HASH]\\n"; sha256sum "$target"',
            'printf "[MAGIC]\\n"; xxd -l 32 -p "$target"',
        ]
    )


def _forensic_detail_command(target: str, kind: str) -> str:
    commands = {
        "disk": 'printf "[PARTITIONS]\\n"; mmls "$target" 2>/dev/null | head -80',
        "pcap": 'printf "[PCAP]\\n"; tshark -r "$target" -q -z io,phs 2>/dev/null | head -80',
        "memory": 'printf "[MEMORY]\\n"; vol -f "$target" windows.info 2>/dev/null | head -80',
        "archive": 'printf "[ARCHIVE]\\n"; 7z l -slt "$target" 2>/dev/null | head -100',
    }
    command = commands.get(kind)
    if not command:
        return ""
    return "\n".join(["set +e", f"target={shlex.quote(target)}", command])


def _infer_forensic_kind(file_type: str, raw: str, mode: str) -> str:
    if mode != "auto":
        return mode
    lowered = f"{file_type}\n{raw}".casefold()
    if "pcap" in lowered or "pcapng" in lowered or "tcpdump capture" in lowered:
        return "pcap"
    if "memory" in lowered or "crash dump" in lowered or "hiberfil" in lowered:
        return "memory"
    if (
        "filesystem" in lowered
        or "disk image" in lowered
        or "partition" in lowered
        or "boot sector" in lowered
        or "ntfs" in lowered
        or "ext4" in lowered
    ):
        return "disk"
    if "archive" in lowered or "zip" in lowered or "7-zip" in lowered:
        return "archive"
    return "file"


def _forensic_recommendations(kind: str, raw: str) -> list[str]:
    if kind == "pcap":
        return ["Build a protocol/endpoint summary with tshark before following individual streams."]
    if kind == "memory":
        return ["Identify the profile and preserve process/network findings with command provenance."]
    if kind == "disk":
        return ["Record partition offsets, then extract selected inodes or filesystem artifacts without altering the image."]
    if kind == "archive":
        return ["List members and hashes before extraction; extract only selected members into workspace."]
    return ["Identify the container/encoding first, then inspect copies and record each extraction provenance."]


async def do_record_forensic_provenance(
    sandbox: Any,
    source_path: str,
    artifact_path: str,
    tool: str,
    command: str,
    offset: str = "",
    sha256: str = "",
    notes: str = "",
) -> str:
    source = _safe_container_path(source_path)
    artifact = _safe_container_path(artifact_path)
    record = {
        "source_path": source,
        "artifact_path": artifact,
        "tool": tool[:120],
        "command": command[:2000],
        "offset": offset[:200],
        "sha256": sha256[:128],
        "notes": notes[:2000],
        "recorded_at": time.time(),
    }
    path = "/challenge/shared/forensics/provenance.jsonl"
    try:
        current = await sandbox.read_file(path)
        existing = current.decode("utf-8", errors="replace") if isinstance(current, bytes) else str(current)
    except Exception:
        existing = ""
    await sandbox.write_file(path, existing + json.dumps(record, ensure_ascii=False) + "\n")
    return json.dumps({"path": path, "record": record}, ensure_ascii=False)
