from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

import backend.tools.specialized as specialized
from backend.challenge_profiles import category_playbook
from backend.prompts import ChallengeMeta, build_prompt
from backend.tools.specialized import (
    WebSessionStore,
    _infer_forensic_kind,
    _parse_binary_manifest,
    _safe_container_path,
    _validate_web_url,
    do_forensic_triage,
    do_record_forensic_provenance,
)


class FakeSandbox:
    def __init__(self, outputs: list[SimpleNamespace] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.files: dict[str, str] = {}
        self.commands: list[str] = []

    async def exec(self, _command: str, timeout_s: int):
        assert timeout_s > 0
        self.commands.append(_command)
        if self.outputs:
            return self.outputs.pop(0)
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, content: str):
        self.files[path] = content


def test_specialized_paths_are_challenge_scoped() -> None:
    assert _safe_container_path("/challenge/distfiles/chall") == "/challenge/distfiles/chall"
    assert _safe_container_path("/challenge/workspace/a/b") == "/challenge/workspace/a/b"
    with pytest.raises(ValueError):
        _safe_container_path("/challenge/workspace/a/../b")
    with pytest.raises(ValueError):
        _safe_container_path("/tmp/out")
    with pytest.raises(ValueError):
        _safe_container_path("/challenge/distfiles/../../etc/passwd")


def test_web_url_policy_blocks_private_networks() -> None:
    _validate_web_url("https://challenge.example")
    with pytest.raises(ValueError):
        _validate_web_url("http://127.0.0.1:8080")
    with pytest.raises(ValueError):
        _validate_web_url("http://10.0.0.4")


def test_binary_manifest_extracts_protections_and_keeps_bounded_sections() -> None:
    raw = """[FILE]
ELF 64-bit LSB pie executable
[STAT]
123 2026-09-16
[HASH]
deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef  /challenge/distfiles/chall
[ELF_HEADER]
Class: ELF64
Machine: Advanced Micro Devices X86-64
Entry point address:               0x401000
Type:                                 DYN (Position-Independent Executable file)
[PROGRAM_HEADERS]
GNU_STACK 0x0 0x0 0x0 0x0 0x0 RW 0x10
GNU_RELRO 0x0 0x0
[SYMBOLS]
  1: 0000000000401000     0 FUNC    GLOBAL main
[STRINGS]
secret-check
"""
    manifest = _parse_binary_manifest("/challenge/distfiles/chall", raw, 0)
    assert manifest["sha256"] == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert "PIE" in manifest["protections"]
    assert "NX" in manifest["protections"]
    assert manifest["entrypoint"] == "0x401000"
    assert manifest["symbols"]
    assert manifest["strings"] == ["secret-check"]


def test_binary_manifest_tolerates_empty_probe_output() -> None:
    manifest = _parse_binary_manifest("/challenge/distfiles/missing", "", 1)
    assert manifest["file_type"] == ""
    assert manifest["sha256"] == ""


def test_forensic_kind_and_category_prompts() -> None:
    assert _infer_forensic_kind("capture file, pcapng", "", "auto") == "pcap"
    assert _infer_forensic_kind("Windows memory dump", "", "auto") == "memory"
    prompt = build_prompt(
        ChallengeMeta(name="web state", category="web"),
        [],
        model_spec="codex/gpt-5.6-sol/high",
    )
    assert "web_session_open" in prompt
    assert "web_parallel_requests" in prompt
    assert "stateful cookies" in category_playbook("web")


@pytest.mark.asyncio
async def test_forensic_provenance_is_append_only_and_structured() -> None:
    sandbox = FakeSandbox()
    first = await do_record_forensic_provenance(
        sandbox,
        "/challenge/distfiles/disk.img",
        "/challenge/workspace/recovered.bin",
        "icat",
        "icat -o 2048 disk.img 42",
        offset="2048",
        sha256="abc123",
        notes="deleted file candidate",
    )
    second = await do_record_forensic_provenance(
        sandbox,
        "/challenge/distfiles/disk.img",
        "/challenge/workspace/recovered2.bin",
        "foremost",
        "foremost -i disk.img",
    )
    assert json.loads(first)["path"].endswith("provenance.jsonl")
    assert json.loads(second)["record"]["tool"] == "foremost"
    records = [json.loads(line) for line in sandbox.files["/challenge/shared/forensics/provenance.jsonl"].splitlines()]
    assert len(records) == 2
    assert records[0]["offset"] == "2048"


@pytest.mark.asyncio
async def test_forensic_auto_triage_classifies_before_running_one_specialized_probe() -> None:
    sandbox = FakeSandbox(
        [
            SimpleNamespace(
                exit_code=0,
                stdout="[FILE]\npcapng capture file\n[HASH]\n" + "a" * 64 + "  sample.pcap",
                stderr="",
            ),
            SimpleNamespace(exit_code=0, stdout="[PCAP]\nendpoints: 2", stderr=""),
        ]
    )
    result = json.loads(await do_forensic_triage(sandbox, "/challenge/distfiles/sample.pcap"))
    assert result["inferred_kind"] == "pcap"
    assert result["sha256"] == "a" * 64
    assert "pcap" in sandbox.commands[1].casefold()
    assert len(sandbox.commands) == 2


def test_web_session_store_starts_empty_and_is_scoped() -> None:
    store = WebSessionStore()
    assert store.sessions == {}


@pytest.mark.asyncio
async def test_web_session_retains_records_and_closes_client(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.calls: list[tuple[str, str]] = []
            self.closed = False

        async def request(self, method: str, url: str, **_kwargs):
            self.calls.append((method, url))
            return SimpleNamespace(
                url=url,
                status_code=200 if len(self.calls) == 1 else 403,
                headers=httpx.Headers(
                    {"Content-Type": "text/plain", "Set-Cookie": "session=secret"}
                ),
                text="initial" if len(self.calls) == 1 else "denied",
            )

        async def aclose(self):
            self.closed = True

    def factory(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(specialized.httpx, "AsyncClient", factory)
    store = WebSessionStore()
    session, initial = await store.open("https://challenge.example")
    second = await store.request(session.id, "/admin", method="POST", body="x=1")

    assert initial is not None
    assert initial.status_code == 200
    assert second.status_code == 403
    assert len(session.records) == 2
    assert session.records[0].headers["set-cookie"] == "[REDACTED]"
    assert clients[0].calls == [
        ("GET", "https://challenge.example"),
        ("POST", "https://challenge.example/admin"),
    ]
    await store.close_all()
    assert clients[0].closed
