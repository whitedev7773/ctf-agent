# CTF Agent

승인된 CTF 문제를 자동으로 분석하고 풀이하는 Codex 중심의 멀티 에이전트 시스템입니다. 대회 전체를 관리하는 coordinator, 문제별 Sol lead, 필요할 때만 생성되는 전문 delegate, 격리된 Docker 분석 환경, 로컬 운영 대시보드로 구성됩니다.

> 이 프로젝트는 참가 권한이 있는 CTF 및 본인이 소유하거나 명시적으로 허가받은 시스템에서만 사용하세요.

Veria Labs와 미국 CTF 팀 [.;,;.](https://ctftime.org/team/222911) 구성원이 개발했으며, 초기 버전은 **BSidesSF 2026 CTF에서 52/52 문제를 해결해 1위**를 기록했습니다.

## 현재 동작 구조

기본 실행은 여러 모델을 무조건 동시에 경주시하지 않습니다. 문제마다 `codex/gpt-5.6-sol/high` 리드 하나가 전체 풀이를 소유하고, 독립적으로 분리할 가치가 있는 좁은 작업만 delegate에 맡깁니다.

```text
CTFd 또는 로컬 문제
        │
        ▼
Coordinator (Codex 기본: GPT-5.6 Terra)
        │  문제 선택, 진행 감시, 힌트 전달
        ▼
Challenge Swarm
  └─ Sol-high lead
       ├─ Luna-low delegate      빠른 추출·분류·단순 검증
       ├─ Sol-high delegate      어려운 암호·VM·heap·protocol 분석
       └─ Terra-medium verifier  독립 재현·검증
                │
                ▼
       문제별 Docker sandbox
```

Delegate는 기본적으로 최대 4개, 동시에 2개까지만 실행됩니다. 중복 작업은 거부되며 handoff에는 결론, 증거, 재현 절차가 필요합니다. 재현기가 실제로 통과해야 결과가 `SUPPORTED_VERIFIED`로 승격됩니다. 모든 solver에는 시간, step, token, 명령 및 flag 제출 상한이 적용됩니다.

기본 모델 역할은 다음과 같습니다.

| 역할 | 모델 스펙 | 용도 |
|---|---|---|
| Coordinator | `gpt-5.6-terra` | 대회 운영과 swarm 조정 |
| Lead | `codex/gpt-5.6-sol/high` | end-to-end 풀이, 가설 및 증거 통합 |
| Fast delegate | `codex/gpt-5.6-luna/low` | 제한된 추출·triage 작업 |
| Hard delegate | `codex/gpt-5.6-sol/high` | 난도가 높은 독립 분석 |
| Verifier | `codex/gpt-5.6-terra/medium` | 재현과 반증 중심의 독립 검증 |
| Writeup / review | Terra-medium / Luna-medium | 한국어 라이트업 작성과 검수 |

`--models`를 여러 번 지정하면 고정 primary roster를 별도로 구성할 수 있지만, 기본값은 Sol-high 하나입니다.

## 주요 기능

- CTFd 5초 polling 또는 dashboard 기반 로컬 문제 등록
- CTFd 없이 실행할 수 있는 standalone mode
- 증거, 가설, blocker, 모순, 실패 실험을 보존하는 구조화된 reasoning state
- 재시작 후에도 exploit, script, checkpoint를 이어 쓰는 persistent workspace
- blocker·mechanism·primitive를 기준으로 검색하는 flag-redacted solve experience
- semantic loop detection과 evidence-gated budget 확장
- GDB, `nc`, REPL, QEMU monitor를 유지하는 persistent PTY session
- 문제 category별 playbook과 `/challenge/skills`의 CTF skill
- solver별 CPU, memory, PID, network, block I/O 및 token/cost telemetry
- 운영자 힌트, solver 제어, trace 및 evidence 조회가 가능한 로컬 dashboard
- Terra 작성 → Luna 독립 검수 → 반려 시 Terra 수정·Luna 재검수의 한국어 writeup과 Markdown/evidence ZIP
- 고정 corpus 및 variant 비교를 위한 재현 가능한 benchmark harness

## 요구 사항

- Python 3.14 이상
- [uv](https://docs.astral.sh/uv/)
- Docker Engine 또는 Linux container mode의 Docker Desktop
- 로그인된 OpenAI Codex CLI
- sandbox image를 빌드할 디스크 공간과 인터넷 연결

선택 사항:

- `OPENAI_API_KEY`: `ENABLE_API_FALLBACK=true`일 때만 Codex quota fallback에 사용되며 사용량 기반 비용이 발생할 수 있습니다.
- Claude 인증 또는 `ANTHROPIC_API_KEY`: Claude coordinator 또는 `claude-sdk/*` solver를 사용할 때 필요합니다.
- CTFd URL/token: dashboard에서도 나중에 입력할 수 있습니다.

## 빠른 시작

### Windows

```powershell
uv sync
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
Copy-Item .env.example .env
./run-ctf-agent.bat
```

`run-ctf-agent.bat`은 가상환경이 없으면 `uv sync`를 실행한 뒤 coordinator와 dashboard를 시작합니다. 기본 dashboard 주소는 <http://127.0.0.1:9400>이고 launcher의 기본 동시 문제 수는 3개입니다. 자원이 제한된 PC에서는 먼저 `$env:CTF_AGENT_MAX_CHALLENGES = "1"`로 낮추세요.

### PowerShell 또는 Linux/macOS에서 직접 실행

```powershell
uv sync
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
uv run ctf-solve --challenges-dir challenges --max-challenges 1 -v
```

대시보드에서 다음 두 방식 중 하나를 선택할 수 있습니다.

1. CTFd URL과 API token을 입력해 전체 대회 mode로 연결합니다.
2. standalone mode에서 설명, 접속 정보, flag 형식, 첨부 파일을 포함한 로컬 문제를 등록합니다.

CTFd 정보는 `.env`의 `CTFD_URL`/`CTFD_TOKEN` 또는 CLI의 `--ctfd-url`/`--ctfd-token`으로 미리 지정해도 됩니다. 다른 port는 `--dashboard-port 9500`, 자동 선택은 `--dashboard-port 0`을 사용합니다.

## 단일 문제 실행

문제 디렉터리는 다음 형식을 사용합니다.

```text
challenges/
└─ example/
   ├─ metadata.yml
   └─ distfiles/
      └─ challenge.bin
```

`metadata.yml` 예시:

```yaml
name: Example Challenge
category: pwn
value: 500
description: Find the flag in the remote service.
connection_info: nc challenge.example.com 31337
flag_format: TEAM{...}
tags: [x86_64, heap]
hints: []
```

standalone dry-run:

```powershell
uv run ctf-solve --challenge challenges/example --no-submit -v
```

CTFd에 제출하며 실행:

```powershell
uv run ctf-solve `
  --challenge challenges/example `
  --ctfd-url https://ctf.example.com `
  --ctfd-token ctfd_your_api_token_here `
  -v
```

`distfiles/`는 container의 `/challenge/distfiles`에 read-only로 mount됩니다. 생성된 exploit과 분석 파일은 host의 `workspace/<challenge>/<model>/`에 보존됩니다. standalone 또는 `--no-submit`에서 찾은 flag는 검증되지 않은 candidate로 남으며, 자동으로 정답 처리되지 않습니다.

## 설정

`.env.example`을 `.env`로 복사한 뒤 필요한 값만 변경합니다. desktop-safe 기본값은 문제 1개, 문제당 Sol lead 1개, delegate 동시 실행 2개, container당 memory 4 GiB와 CPU 2개입니다.

```dotenv
CTFD_URL=
CTFD_TOKEN=

OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
ENABLE_API_FALLBACK=false

MAX_CONCURRENT_CHALLENGES=1
CONTAINER_MEMORY_LIMIT=4g
CONTAINER_CPU_LIMIT=2.0

DYNAMIC_DELEGATION_ENABLED=true
DELEGATE_MODEL_SPEC=codex/gpt-5.6-luna/low
DELEGATE_HARD_MODEL_SPEC=codex/gpt-5.6-sol/high
DELEGATE_VERIFIER_MODEL_SPEC=codex/gpt-5.6-terra/medium
DELEGATE_MAX_AGENTS=4
DELEGATE_MAX_CONCURRENT=2
```

시간, token, step, writeup, postprocess 등 전체 설정과 기본값은 [.env.example](.env.example)을 참고하세요. 기본 `challenges/` 경로를 사용할 때 Dashboard에서 바꾼 비밀이 아닌 runtime 설정은 프로젝트 루트의 `.ctf-agent-settings.json`에 저장되며 다음 실행에도 적용됩니다.

Windows에서 `codex`가 `PATH`에 없다면 `.env`에 실행 파일을 지정할 수 있습니다.

```dotenv
CODEX_CLI_PATH=C:\Users\you\.vscode\extensions\openai.chatgpt-...\bin\windows-x86_64\codex.exe
```

## Coordinator와 운영자 메시지

```powershell
# Codex coordinator (기본)
uv run ctf-solve --coordinator codex

# Claude Agent SDK coordinator (선택)
uv run ctf-solve --coordinator claude

# 실행 중인 coordinator에 힌트 전송
uv run ctf-msg --port 9400 "pwn-500의 libc build ID를 먼저 확인"
```

주요 CLI 옵션은 `uv run ctf-solve --help`와 `uv run ctf-msg --help`에서 확인할 수 있습니다.

## Sandbox 도구

| 분야 | 주요 도구 |
|---|---|
| Binary / Pwn | radare2, GDB/gdb-multiarch, QEMU, pwntools, angr, ROPgadget, ropper, one_gadget, patchelf, LIEF, pyghidra |
| Crypto | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs |
| Forensics / Stego | volatility3, Sleuthkit, tshark, YARA, foremost, exiftool, steghide, stegseek, zsteg, tesseract |
| Web / Network | Playwright/Chromium, curl, nmap, socat, Scapy, requests, WebSocket tooling |
| Mobile / Web3 | apktool, androguard, web3.py, eth-abi |
| Misc | ffmpeg, sox, ImageMagick, Pillow, PyTorch, podman/buildah |

Sandbox는 분석 편의를 위해 `SYS_ADMIN`, `SYS_PTRACE`, `seccomp=unconfined` 등 강한 권한을 사용합니다. 개인 credential이나 민감 문서가 없는 전용 VM에서 실행하는 것을 권장합니다.

## Benchmark

고정 corpus와 runtime variant를 같은 반복 횟수로 비교할 수 있습니다. manifest 형식은 [benchmarks/README.md](benchmarks/README.md)를 참고하세요.

```powershell
uv run ctf-benchmark benchmarks/corpus.yml
uv run ctf-benchmark --summarize benchmarks/results.jsonl
```

요약에는 solve@1/3, flag 도달 시간, fresh token과 tool call 효율, 중복 실험률, false candidate, clean reproduction, delegate utility, peer-context token 및 가설 반증 지표가 포함됩니다.

## 데이터와 복구

| 경로 | 내용 |
|---|---|
| `challenges/` | 문제 metadata와 배포 파일 |
| `workspace/` | solver 산출물, checkpoint, reasoning state, writeup |
| `experience/` | 검증 후 flag가 제거된 cross-challenge 경험 |
| `logs/` | solver JSONL trace |

정상 종료는 실행 terminal에서 `Ctrl+C`를 사용합니다. 다음 실행 때 `ctf-agent` label이 붙은 orphan container는 자동 정리됩니다. Dashboard의 runtime reset과 experience reset은 서로 독립적이며, 문제 파일과 source code는 삭제하지 않습니다.

더 자세한 설치, 대회 운영, flag 검증, quota, writeup 및 troubleshooting 절차는 [한국어 Codex 사용자 매뉴얼](docs/CODEX_USER_MANUAL.ko.md)을 참고하세요.

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — `pull_challenges.py`의 CTFd interaction 및 HTML helper
- [ljagiello/ctf-skills](https://github.com/ljagiello/ctf-skills) — sandbox에 포함된 category별 CTF skill

## License

[MIT License](LICENSE)
