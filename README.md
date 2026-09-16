# CTF Agent

CTF Agent는 허가된 CTF 문제를 수집하고, 분석하고, 검증하고, 기록하는 Codex 중심의 자율 풀이 시스템입니다. 대회 전체를 관리하는 coordinator와 문제별 lead agent, 필요할 때만 투입되는 delegate, Docker 분석 환경, 실시간 운영 대시보드를 하나의 실행 흐름으로 묶습니다.

Veria Labs와 미국 CTF 팀 [.;,;.](https://ctftime.org/team/222911) 구성원이 개발했습니다. 초기 버전은 BSidesSF 2026 CTF에서 52개 문제를 모두 해결하고 1위를 기록했습니다.

## 한눈에 보기

- CTFd를 5초 간격으로 동기화하거나 로컬 문제를 직접 등록합니다.
- 문제마다 Sol lead가 풀이 전체를 소유하고, 독립적인 소작업만 delegate에 맡깁니다.
- 모든 분석은 문제별 Docker sandbox에서 격리하여 실행합니다.
- exploit, 스크립트, 가설, 실패 기록과 checkpoint를 재시작 후에도 이어갑니다.
- flag 후보를 재현 가능한 증거와 함께 검증한 뒤 제출합니다.
- CPU, 메모리, token, 비용, trace와 agent 상태를 대시보드에서 확인합니다.
- 풀이가 끝나면 한국어 writeup을 만들고 별도 모델로 검수할 수 있습니다.
- 고정 corpus를 이용해 모델 및 runtime 설정을 반복 비교할 수 있습니다.

## 시스템 구조

```text
CTFd / 로컬 문제
       │
       ▼
Coordinator ───── 운영자 메시지 · 대시보드
       │
       ├─ Challenge A ─ Sol lead
       │                  ├─ fast delegate
       │                  ├─ hard delegate
       │                  └─ verifier
       │
       └─ Challenge B ─ Sol lead
                          └─ delegates ...
                               │
                               ▼
                    문제별 Docker sandbox
```

기본 구성은 문제마다 `codex/gpt-5.6-sol/high` lead 하나를 실행합니다. Lead는 빠른 추출이나 분류에는 Luna, 난도가 높은 독립 분석에는 Sol, 재현과 반증에는 Terra delegate를 선택적으로 사용합니다. 기본 한도는 문제당 delegate 최대 4개, 동시 실행 2개입니다.

| 역할 | 기본 모델 | 책임 |
|---|---|---|
| Coordinator | `gpt-5.6-terra` | 문제 선택, 진행 감시, 운영자 지시 전달 |
| Lead | `codex/gpt-5.6-sol/high` | end-to-end 풀이와 최종 증거 통합 |
| Fast delegate | `codex/gpt-5.6-luna/low` | 추출, 분류, 단순 검증 |
| Hard delegate | `codex/gpt-5.6-sol/high` | 암호, VM, heap, protocol 등 고난도 분석 |
| Verifier | `codex/gpt-5.6-terra/medium` | 독립 재현과 반증 |
| Writeup / review | Terra-medium / Luna-medium | 한국어 문서 작성과 독립 검수 |

Delegate의 handoff에는 결론, 근거, 재현 절차가 포함되어야 합니다. 재현 검사를 통과한 결과만 `SUPPORTED_VERIFIED`로 승격됩니다. 시간, step, token, 명령 실행 시간과 flag 제출 횟수에는 각각 상한이 적용됩니다.

## 요구 사항

- Git
- [uv](https://docs.astral.sh/uv/)
- Docker Engine 또는 Linux container mode로 실행 중인 Docker Desktop
- 로그인된 OpenAI Codex CLI
- sandbox image를 빌드할 디스크 공간과 인터넷 연결

Python 3.14 이상은 `uv sync` 과정에서 사용할 수 있는 interpreter를 자동으로 찾거나 설치합니다. 직접 설치해 두어도 됩니다.

설치 전 다음 명령을 확인하세요.

```powershell
git --version
uv --version
docker version
docker info
```

Codex CLI가 `PATH`에 있다면 `codex --version`과 `codex login status`도 확인합니다. Windows에서는 setup script가 VS Code/Cursor extension에 포함된 Codex CLI도 자동 탐색합니다. Codex CLI 로그인이 필요하면 `codex login`을 실행합니다. `OPENAI_API_KEY`는 CLI 로그인 자체를 대신하지 않습니다. 이 프로젝트에서는 `ENABLE_API_FALLBACK=true`로 명시했을 때만 quota 소진 후 direct API fallback에 사용하며, 이 경우 별도 사용량 비용이 발생할 수 있습니다.

## 새 컴퓨터에서 시작

저장소를 받은 컴퓨터마다 setup script를 한 번 실행하면 `.env`, Python 가상환경, 의존성과 Docker sandbox를 한꺼번에 준비합니다. 기존 `.env`와 기존 `ctf-sandbox` image가 있으면 그대로 유지합니다.

### Windows

```powershell
git clone https://github.com/whitedev7773/ctf-agent.git
cd ctf-agent
powershell -ExecutionPolicy Bypass -File .\scripts\setup.ps1
./run-ctf-agent.bat
```

### Linux / macOS

```bash
git clone https://github.com/whitedev7773/ctf-agent.git
cd ctf-agent
bash scripts/setup.sh
bash run-ctf-agent.sh
```

Setup script는 다음 작업을 순서대로 수행합니다.

1. `uv`와 Docker 실행 가능 여부 확인
2. `.env`가 없으면 `.env.example`에서 생성
3. `uv sync`로 Python 3.14 환경과 패키지 설치
4. Docker daemon 연결 확인
5. `ctf-sandbox` image가 없으면 최초 build
6. Codex CLI 로그인 상태 확인

Sandbox를 강제로 다시 만들거나 일부 단계만 건너뛸 수도 있습니다.

```powershell
# Windows: sandbox image 재빌드
.\scripts\setup.ps1 -RebuildSandbox

# Windows: 기존 sandbox image 사용, Codex 확인 생략
.\scripts\setup.ps1 -SkipDockerBuild -SkipCodexCheck
```

```bash
# Linux / macOS: sandbox image 재빌드
bash scripts/setup.sh --rebuild-sandbox

# Linux / macOS: 기존 sandbox image 사용, Codex 확인 생략
bash scripts/setup.sh --skip-docker-build --skip-codex-check
```

실행 후 브라우저에서 <http://127.0.0.1:9400>을 열고 다음 중 하나를 선택합니다.

1. CTFd URL과 API token을 연결해 대회 전체를 운영합니다.
2. standalone mode에서 설명, 접속 정보, flag 형식과 첨부 파일을 직접 등록합니다.

Windows의 `run-ctf-agent.bat`과 Unix 계열의 `run-ctf-agent.sh`는 가상환경이 없으면 `uv sync`를 실행한 뒤 9400 포트에서 대시보드를 시작합니다.

### 수동 설치와 실행

```bash
uv sync
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
cp .env.example .env
uv run ctf-solve --challenges-dir challenges --max-challenges 1 -v
```

Windows PowerShell에서는 `cp` 대신 `Copy-Item .env.example .env`를 사용합니다. 직접 실행 시 대시보드 주소는 <http://127.0.0.1:9400>입니다.

다른 포트를 사용하려면 `--dashboard-port 9500`, 빈 포트를 자동 선택하려면 `--dashboard-port 0`을 지정합니다.

### 기존 PC의 데이터 옮기기

소스 코드 외에 기존 실행 상태도 이어 쓰려면 필요한 디렉터리만 새 PC의 저장소 루트로 복사합니다.

| 대상 | 용도 | 복사 여부 |
|---|---|---|
| `challenges/` | 문제 metadata와 첨부 파일 | 기존 문제를 이어 풀 때 |
| `workspace/` | exploit, checkpoint와 writeup | 진행 중인 풀이를 이어갈 때 |
| `experience/` | 검증된 cross-challenge 경험 | 누적 경험을 유지할 때 |
| `.ctf-agent-settings.json` | 대시보드 runtime 설정 | 같은 설정을 복원할 때 |

`.venv/`, Docker image와 `logs/`는 복사하지 않아도 setup script가 환경을 다시 만들 수 있습니다. `.env`는 새 PC에서 `.env.example`로 생성한 뒤 필요한 값만 다시 입력합니다. Codex CLI 로그인도 컴퓨터별로 한 번 수행합니다.

## 문제 실행 방식

### 대회 전체 운영

CTFd 연결 정보는 대시보드에서 입력하거나 `.env`에 미리 저장할 수 있습니다.

```dotenv
CTFD_URL=https://ctf.example.com
CTFD_TOKEN=ctfd_your_api_token_here
```

그다음 coordinator를 실행합니다.

```powershell
uv run ctf-solve --challenges-dir challenges --max-challenges 4 -v
```

Coordinator는 새 문제와 상태 변화를 polling하고, 풀이 가능한 문제를 concurrency 한도 안에서 시작합니다. 대시보드에서 문제별 진행 상황, agent trace, 자원 사용량, flag 후보와 증거를 확인하고 agent를 제어하거나 힌트를 전달할 수 있습니다.

CLI에서 CTFd 설정을 일시적으로 덮어쓸 수도 있습니다.

```powershell
uv run ctf-solve `
  --ctfd-url https://ctf.example.com `
  --ctfd-token ctfd_your_api_token_here `
  --max-challenges 4
```

### 단일 로컬 문제

문제 디렉터리는 다음 구조를 사용합니다.

```text
challenges/
└─ example/
   ├─ metadata.yml
   └─ distfiles/
      ├─ challenge.bin
      └─ libc.so.6
```

`metadata.yml` 예시:

```yaml
name: Example Challenge
source: Example CTF 2026
category: pwn
value: 500
description: Find the flag in the remote service.
connection_info: nc challenge.example.com 31337
flag_format: TEAM{...}
tags: [x86_64, heap]
hints: []
```

CTFd 제출 없이 실행하려면 다음 명령을 사용합니다.

```powershell
uv run ctf-solve --challenge challenges/example --no-submit -v
```

CTFd에 제출하면서 단일 문제만 실행하려면 연결 정보를 함께 지정합니다.

```powershell
uv run ctf-solve `
  --challenge challenges/example `
  --ctfd-url https://ctf.example.com `
  --ctfd-token ctfd_your_api_token_here `
  -v
```

`distfiles/`와 `metadata.yml`은 container 내부의 `/challenge/` 아래에 read-only로 mount됩니다. Agent가 만든 exploit과 분석 산출물은 host의 `workspace/`에 보존됩니다. CTFd가 연결되지 않았거나 `--no-submit`을 사용한 경우 발견한 flag는 자동 정답 처리되지 않고 운영자 확인이 필요한 candidate로 남습니다.

### 기존 CTFd 문제 내려받기

별도 importer로 CTFd 문제와 첨부 파일을 로컬 형식으로 내려받을 수 있습니다.

```powershell
uv run python pull_challenges.py `
  --url https://ctf.example.com `
  --token ctfd_your_api_token_here `
  --output challenges
```

사용자 이름과 비밀번호를 쓰려면 `--token` 대신 `--username`과 `--password`를 지정합니다. 가능하면 API token 방식을 권장합니다.

## 설정

`.env.example`을 `.env`로 복사한 뒤 필요한 값만 수정하세요. 주요 기본값은 다음과 같습니다.

```dotenv
# 선택: CTFd와 Discord
CTFD_URL=
CTFD_TOKEN=
DISCORD_WEBHOOK_URL=

# API fallback은 명시적으로 활성화한 경우에만 사용
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=
ENABLE_API_FALLBACK=false

# 실행 자원
SANDBOX_IMAGE=ctf-sandbox
MAX_CONCURRENT_CHALLENGES=4
CONTAINER_MEMORY_LIMIT=4g
CONTAINER_CPU_LIMIT=2.0

# 동적 delegation
DYNAMIC_DELEGATION_ENABLED=true
DELEGATE_MODEL_SPEC=codex/gpt-5.6-luna/low
DELEGATE_HARD_MODEL_SPEC=codex/gpt-5.6-sol/high
DELEGATE_VERIFIER_MODEL_SPEC=codex/gpt-5.6-terra/medium
DELEGATE_MAX_AGENTS=4
DELEGATE_MAX_CONCURRENT=2
```

시간, token, step, compaction, writeup과 postprocess를 포함한 전체 설정은 [.env.example](.env.example)에 설명되어 있습니다. 대부분의 runtime 정책은 실행 중 대시보드에서도 변경할 수 있습니다.

대시보드에서 저장한 runtime 정책, CTFd 연결과 Discord webhook은 프로젝트 루트의 `.ctf-agent-settings.json`에 보존되어 다음 실행에 복원됩니다. 이 파일에는 token, password, webhook이 평문으로 저장될 수 있습니다. Git에서 제외되어 있더라도 외부에 공유하지 마세요.

Windows에서 `codex`가 `PATH`에 없다면 실행 파일 경로를 직접 지정할 수 있습니다.

```dotenv
CODEX_CLI_PATH=C:\Users\you\.vscode\extensions\openai.chatgpt-...\bin\windows-x86_64\codex.exe
```

### 모델 지정

기본 primary roster는 Sol-high 하나입니다. `--models`를 반복하면 고정 roster를 직접 구성할 수 있습니다.

```powershell
uv run ctf-solve `
  --challenge challenges/example `
  --models codex/gpt-5.6-sol/high `
  --models codex/gpt-5.6-terra/medium `
  --no-submit
```

모델 스펙 형식은 `provider/model/effort`입니다. 지원 provider와 credential은 사용하는 backend에 따라 달라집니다. 일반적인 Codex 실행에는 로그인된 Codex CLI만 있으면 됩니다. Claude coordinator나 `claude-sdk/*` solver를 선택할 때는 Claude 인증 또는 `ANTHROPIC_API_KEY`가 필요합니다.

### Coordinator backend와 운영자 메시지

```powershell
# 기본 Codex coordinator
uv run ctf-solve --coordinator codex

# 선택적 Claude Agent SDK coordinator
uv run ctf-solve --coordinator claude

# 실행 중인 coordinator에 힌트 전달
uv run ctf-msg --port 9400 "pwn-500의 libc build ID를 먼저 확인"
```

전체 CLI 옵션은 아래에서 확인합니다.

```powershell
uv run ctf-solve --help
uv run ctf-msg --help
uv run ctf-benchmark --help
```

## 대시보드에서 할 수 있는 일

- CTFd 연결, 해제 및 standalone mode 전환
- 로컬 문제와 여러 첨부 파일 등록
- 실행할 문제와 동시 실행 수 관리
- 문제별 agent 상태, elapsed time, token, 비용 및 container 자원 확인
- trace, reasoning state, evidence와 flag candidate 검토
- solver 시작, 중단, 재실행과 운영자 힌트 전달
- Discord webhook 저장, 테스트 및 해제
- 풀이 완료 문제의 writeup 생성, 검수 상태 확인 및 evidence ZIP 다운로드
- runtime 상태와 cross-challenge experience의 독립적인 초기화

`DISCORD_WEBHOOK_URL`을 설정하면 새 문제, 풀이 완료, 검토가 필요한 flag 후보와 writeup 상태를 Discord로 알립니다. Flag 값은 spoiler로 표시되며, webhook 실패는 solver 실행을 중단하지 않습니다.

## Sandbox

Sandbox에는 주요 CTF 분야의 분석 도구가 포함되어 있습니다.

| 분야 | 도구 예시 |
|---|---|
| Binary / Pwn | Ghidra, radare2, GDB, QEMU, pwntools, angr, ROPgadget, ropper, patchelf, LIEF |
| Crypto | SageMath, RsaCtfTool, z3, gmpy2, pycryptodome, cado-nfs, flatter |
| Forensics / Stego | Volatility 3, Sleuth Kit, tshark, YARA, foremost, exiftool, steghide, stegseek, zsteg |
| Web / Network | Playwright/Chromium, curl, nmap, socat, Scapy, requests, WebSocket tooling |
| Android / Web3 | JADX, apktool, androguard, web3.py, eth-abi |
| Media / Misc | ffmpeg, sox, ImageMagick, Pillow, Tesseract, PyTorch, podman/buildah |

전체 목록은 [sandbox/sandbox-tools.txt](sandbox/sandbox-tools.txt)에서 확인할 수 있습니다.

분석 편의를 위해 container는 `SYS_ADMIN`, `SYS_PTRACE`, `seccomp=unconfined` 권한을 사용합니다.

## 결과와 데이터

| 경로 | 내용 | 수명 |
|---|---|---|
| `challenges/` | metadata와 원본 첨부 파일 | 사용자가 관리 |
| `workspace/` | exploit, 분석 파일, checkpoint, reasoning state, writeup | 재실행 후 유지 |
| `experience/` | flag를 제거한 검증된 cross-challenge 경험 | 별도 초기화 전까지 유지 |
| `logs/` | solver JSONL trace | 재실행 후 유지 |
| `.ctf-agent-settings.json` | 대시보드 runtime 및 연결 설정 | 재실행 후 유지 |

정상 종료는 실행 terminal에서 `Ctrl+C`를 사용합니다. 다음 시작 시 `ctf-agent` label이 붙은 orphan container를 자동 정리합니다. 대시보드의 runtime reset과 experience reset은 서로 독립적이며 `challenges/`의 원본 문제 파일은 삭제하지 않습니다.

## Benchmark

고정된 문제 corpus에 여러 runtime variant를 같은 횟수로 실행해 비교할 수 있습니다.

```powershell
uv run ctf-benchmark benchmarks/corpus.yml
uv run ctf-benchmark --summarize benchmarks/results.jsonl
```

요약에는 solve@1/3, flag 도달 시간, fresh token, tool call, 중복 실험률, false candidate, clean reproduction, delegate utility, peer-context token과 가설 반증 지표가 포함됩니다. Manifest 형식과 offline flag 처리 방식은 [benchmarks/README.md](benchmarks/README.md)를 참고하세요.

## 개발과 검증

```powershell
# 전체 테스트
uv run pytest

# 정적 검사
uv run ruff check .

# 특정 테스트만 실행
uv run pytest tests/test_dashboard.py -q
```

`uv` cache 접근 권한 문제가 있는 환경에서는 이미 생성된 가상환경으로 직접 실행할 수 있습니다.

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m backend.cli --help
```

## 문제 해결

### Codex CLI를 찾지 못함

`codex --version`과 `codex login status`를 같은 terminal에서 확인하세요. IDE에 포함된 실행 파일만 사용할 수 있다면 `.env`의 `CODEX_CLI_PATH`를 설정합니다.

### Docker 연결 또는 sandbox 시작 실패

Docker Desktop이 Linux container mode인지 확인한 뒤 다음 명령을 실행합니다.

```powershell
docker info
docker image inspect ctf-sandbox
```

Image가 없다면 `docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .`을 다시 실행하세요. 최초 build는 분석 도구가 많아 오래 걸리고 상당한 디스크 공간을 사용합니다.

### 9400 포트가 이미 사용 중임

`run-ctf-agent.bat`은 기존 listener를 감지하고 종료 여부를 묻습니다. 기존 프로세스를 유지하려면 직접 다른 포트로 실행하세요.

```powershell
uv run ctf-solve --dashboard-port 9500
```

### Flag를 찾았지만 solved로 표시되지 않음

Standalone mode와 `--no-submit`에서는 flag가 `UNVERIFIED CANDIDATE`로 남는 것이 정상입니다. 대시보드 또는 단일 실행 terminal에서 후보를 검토하고 로컬 정답으로 확인하거나, 올바른 CTFd 연결을 설정한 뒤 제출하세요.

### 설정을 바꿨는데 반영되지 않음

기본 `challenges/` 경로를 사용할 때는 대시보드에서 저장한 `.ctf-agent-settings.json` 값이 복원됩니다. CLI 인자는 해당 실행에서 우선하고, 대시보드의 runtime reset은 저장된 정책을 프로젝트 기본값으로 되돌립니다.

더 자세한 운영 절차는 [한국어 Codex 사용자 매뉴얼](docs/CODEX_USER_MANUAL.ko.md), 설계 배경은 [DESIGN.md](DESIGN.md)를 참고하세요.

## Acknowledgements

- [es3n1n/Eruditus](https://github.com/es3n1n/Eruditus) — `pull_challenges.py`의 CTFd interaction 및 HTML helper
- [ljagiello/ctf-skills](https://github.com/ljagiello/ctf-skills) — sandbox에 포함된 category별 CTF skill

## License

[MIT License](LICENSE)
