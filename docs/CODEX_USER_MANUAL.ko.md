# CTF Agent Codex 사용자 매뉴얼

> 기준일: 2026-09-02
> 대상: 이 저장소를 Codex 기반 CTF 자동 풀이 에이전트로 운영하는 사용자

이 문서는 OpenAI Codex 자체의 전체 기능 설명서가 아니라, 이 프로젝트를 실제 CTF에서 설치하고 점검하고 운영하는 방법을 설명한다. 반드시 참가가 허가된 CTF 및 본인이 테스트 권한을 가진 대상에만 사용한다.

## 1. 동작 개요

전체 대회 모드에서 프로그램은 다음 순서로 동작한다.

1. Codex coordinator가 `codex app-server`와 JSON-RPC로 연결된다.
2. CTFd가 연결된 경우 poller가 5초마다 문제 및 풀이 상태를 확인한다. 연결하지 않으면 로컬 독립 모드로 동작한다.
3. CTFd의 새 문제 또는 대시보드에서 등록한 로컬 문제의 설명과 첨부 파일을 `challenges/`에서 관리한다.
4. 문제마다 SOL-xhigh 주 solver 하나를 시작한다. SOL은 초기 분석 뒤 독립적으로 분리할 가치가 있는 좁은 작업만 Luna-low 하위 agent에 실시간 위임한다.
5. 각 solver는 별도의 Docker 컨테이너에서 분석 도구를 사용한다.
6. 한 solver가 flag를 찾으면 같은 문제의 나머지 solver를 중단한다. CTFd 연결 시에는 사이트에 제출해 확인하고, 독립 모드에서는 후보로 멈춘 뒤 운영자 확인을 기다린다.
7. coordinator는 solver trace와 중간 결과를 읽고 필요한 힌트를 다시 전달한다.

기본 구성은 문제 하나당 다음 주 solver 하나를 실행한다.

| 역할 | 모델 스펙 | 용도 |
|---|---|---|
| 주 풀이 | `codex/gpt-5.6-sol/xhigh` | 전체 풀이 경로, 작업 분할, 결과 통합과 최종 검증 |
| 동적 하위 agent | `codex/gpt-5.6-luna/low` | SOL이 지정한 하나의 좁고 검증 가능한 보조 작업 |

Coordinator의 기본 모델은 `gpt-5.6-terra`, reasoning effort는 `medium`이다. OpenAI는 Sol을 flagship, Terra를 성능·비용 균형형, Luna를 효율적인 대량 작업용 모델로 안내한다.

## 2. 사전 요구 사항

다음 프로그램이 필요하다.

- Python 3.14 이상
- `uv`
- Docker Engine 또는 Linux container 모드의 Docker Desktop
- OpenAI Codex CLI
- CTFd를 사용할 경우 해당 URL과 API token(독립 모드에서는 불필요)
- sandbox image를 빌드할 디스크 공간과 인터넷 연결

PowerShell에서 기본 상태를 확인한다.

```powershell
python --version
uv --version
docker version
docker info
codex --version
codex login status
```

이 프로젝트가 실행하는 `codex app-server`는 `uv run ctf-solve`와 같은 호스트 환경의 `PATH`에서 발견되어야 한다. IDE 안에서는 Codex가 보여도 해당 터미널에서 `codex --version`이 실패한다면 PATH부터 수정한다.

## 3. Codex 설치와 인증

Codex CLI 설치 방법은 운영체제별로 바뀔 수 있으므로 [공식 Codex CLI 설치 안내](https://developers.openai.com/codex/cli)를 우선한다.

### 3.1 ChatGPT 계정 로그인—권장

```powershell
codex login
codex login status
```

`codex login`은 브라우저 인증을 시작한다. ChatGPT 구독을 통한 로컬 Codex 사용은 이 방식이 가장 간단하다.

브라우저 callback을 사용할 수 없는 원격·headless 환경에서는 device code 로그인을 사용할 수 있다.

```powershell
codex login --device-auth
```

### 3.2 API key로 Codex CLI 로그인

API 사용량 기반 과금으로 Codex CLI를 사용할 때는 환경 변수의 값을 stdin으로 전달한다. 키 문자열을 명령 기록에 직접 입력하지 않는다.

```powershell
$env:OPENAI_API_KEY | codex login --with-api-key
codex login status
```

ChatGPT 로그인과 API key 로그인은 과금 및 workspace 정책이 다르다. API key 로그인은 OpenAI Platform의 일반 API 사용량으로 과금된다. 자세한 차이는 [공식 인증 문서](https://developers.openai.com/codex/auth)를 확인한다.

로그아웃은 다음과 같다.

```powershell
codex logout
```

### 3.3 이 프로젝트의 `.env`와 Codex 로그인은 별개

`.env`의 `OPENAI_API_KEY`는 Codex 구독 quota가 소진되었을 때 direct API fallback에 사용된다. `.env`에 키를 적는 것만으로 Codex CLI의 로그인 상태가 만들어지는 것은 아니다. 대회 전에 반드시 `codex login status`를 별도로 확인한다.

## 4. 프로젝트 설치

저장소 루트에서 실행한다.

```powershell
uv sync
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
Copy-Item .env.example .env
```

Linux 또는 WSL에서는 다음처럼 복사할 수 있다.

```bash
cp .env.example .env
```

Sandbox image는 radare2, GDB, pwntools, angr, SageMath, RsaCtfTool, Volatility, stego 및 미디어 도구를 포함하므로 최초 빌드에 시간이 걸린다. 대회 시작 직전에 처음 빌드하지 말고 사전에 완료한다.

## 5. 환경 설정

최소 `.env` 예시는 다음과 같다.

```env
# 선택 사항: 비워두고 대시보드에서 연결할 수 있다
CTFD_URL=
CTFD_TOKEN=

# Codex quota 소진 시 direct OpenAI API fallback을 사용할 때만 입력
OPENAI_API_KEY=
ENABLE_API_FALLBACK=false

# 선택 사항
CONTAINER_MEMORY_LIMIT=4g
MAX_CONCURRENT_CHALLENGES=1
```

주요 설정은 다음과 같다.

| 환경 변수 | 기본값 | 설명 |
|---|---:|---|
| `CTFD_URL` | 빈 값 | 선택적인 CTFd base URL; 빈 값이면 독립 모드 |
| `CTFD_TOKEN` | 빈 값 | CTFd API token |
| `CTFD_USER` | `admin` | token이 없을 때 로그인 사용자 |
| `CTFD_PASS` | `admin` | token이 없을 때 로그인 비밀번호 |
| `OPENAI_API_KEY` | 빈 값 | Codex quota fallback용 API key |
| `SANDBOX_IMAGE` | `ctf-sandbox` | 사용할 Docker image |
| `CONTAINER_MEMORY_LIMIT` | `4g` | solver 컨테이너 하나의 memory limit |
| `CONTAINER_CPU_LIMIT` | `2.0` | solver 컨테이너 하나의 CPU limit |
| `MAX_CONCURRENT_CHALLENGES` | `1` | 동시에 실행할 문제 swarm 수 |
| `WORKSPACE_ROOT` | `workspace` | 영구 exploit·solver·checkpoint 저장 경로 |
| `LOGS_ROOT` | `logs` | Solver JSONL trace 저장 경로 |
| `SOLVER_TURN_TIMEOUT_SECONDS` | `1800` | model turn 하나의 최대 실행 시간 |
| `SOLVER_MAX_RUNTIME_SECONDS` | `10800` | solver 하나의 전체 최대 실행 시간 |
| `SOLVER_HANDOFF_WAIT_SECONDS` | `180` | 여러 primary 모델을 직접 지정했을 때 단계별 handoff 대기 시간 |
| `MAX_ATTEMPTS_PER_CHALLENGE` | `8` | SOL LEAD 최대 turn/slice 수; 역할별 배율 적용 |
| `SOLVER_MAX_STEPS` | `300` | solver별 최대 tool step 수 |
| `SOLVER_MAX_TOKENS` | `1500000` | 캐시 가중 유효 token 기준 상한; `0`이면 비활성화 |
| `SOLVER_MAX_RAW_TOKENS` | `12000000` | 캐시 여부와 무관한 raw token 안전 상한 |
| `SOLVER_CACHED_TOKEN_WEIGHT` | `0.10` | 유효 token 계산에서 cached input에 적용할 비율 |
| `SOLVER_TURN_SLICE_TOKENS` | `1500000` | 긴 Codex turn을 checkpoint·compact할 raw token 간격 |
| `SOLVER_MAX_ESTIMATED_COST_USD` | `0` | solver별 추정 비용 상한; `0`이면 비활성화 |
| `MAX_FLAG_SUBMISSIONS_PER_CHALLENGE` | `8` | 문제별 live flag 제출 최대 횟수 |
| `MAX_COMMAND_TIMEOUT_SECONDS` | `600` | container 명령 하나의 최대 실행 시간 |
| `ENABLE_API_FALLBACK` | `false` | quota 소진 후 사용량 과금 API로 전환할지 여부 |
| `DYNAMIC_DELEGATION_ENABLED` | `true` | SOL 주 solver의 실시간 하위 agent 생성 허용 |
| `DELEGATE_MODEL_SPEC` | `codex/gpt-5.6-luna/low` | 하위 agent 모델과 effort |
| `DELEGATE_MAX_AGENTS` | `4` | 문제 하나에서 생성할 수 있는 하위 agent 누적 상한 |
| `DELEGATE_MAX_CONCURRENT` | `2` | 문제 하나에서 동시에 실행할 하위 agent 상한 |
| `DELEGATE_MAX_ATTEMPTS` | `4` | 하위 agent 최대 turn/slice 수 |
| `DELEGATE_MAX_RUNTIME_SECONDS` | `1800` | 하위 agent 전체 실행 시간 상한 |
| `DELEGATE_TURN_TIMEOUT_SECONDS` | `600` | 하위 agent turn 하나의 상한 |
| `DELEGATE_MAX_STEPS` | `96` | 하위 agent tool step 상한 |
| `DELEGATE_MAX_TOKENS` | `250000` | 하위 agent 캐시 가중 유효 token 상한 |
| `DELEGATE_MAX_RAW_TOKENS` | `1200000` | 하위 agent raw token 안전 상한 |
| `DELEGATE_TURN_SLICE_TOKENS` | `300000` | 하위 agent compact/resume 간격 |
| `DELEGATE_POSTPROCESS_ON_BUDGET_STOP` | `true` | 중단된 delegate의 handoff가 없거나 불완전할 때 후처리 worker 생성 |
| `DELEGATE_POSTPROCESS_MAX_AGENTS` | `1` | 문제당 후처리 worker 누적 상한 |
| `DELEGATE_POSTPROCESS_MAX_TOKENS` | `80000` | 후처리 worker 유효 token 상한 |
| `DELEGATE_POSTPROCESS_MAX_RAW_TOKENS` | `400000` | 후처리 worker raw token 안전 상한 |

Codex solver는 App Server의 실시간 usage 알림에서 token·추정 비용 상한을 감지하면 현재
turn에 `turn/interrupt`를 보내며, tool step 상한에 도달해도 같은 방식으로 중단한다. 유효
token은 `uncached input + output + cached input × SOLVER_CACHED_TOKEN_WEIGHT`로 계산한다.
따라서 기본 `1.5M`은 긴 캐시 문맥을 매 호출마다 raw로 중복 계산해 조기 중단하지 않으면서도
새 분석과 출력에는 엄격한 상한으로 작동한다. `12M` raw 상한은 캐시가 많아도 무한 실행되지
않게 하는 별도 안전장치다. Direct API fallback에는 남은 raw token·tool-call 한도를 전달한다.

기본 역할별 실제 상한은 다음과 같다.

| 역할 | 유효 token | raw token | turn slice | 최대 slice/turn |
|---|---:|---:|---:|---:|
| SCOUT | 450k | 1.8M | 600k | 3 |
| ANALYST | 1.275M | 10.2M | 1.5M | 7 |
| LEAD (SOL) | 1.5M | 12M | 1.5M | 8 |
| DELEGATE (Luna) | 250k | 1.2M | 300k | 4 |
| SPECIALIST | 975k | 7.8M | 1.2M | 6 |

Slice 상한에 닿으면 전체 풀이를 실패로 끝내지 않는다. 현재 turn을 중단하고 App Server
문맥을 compact한 뒤 같은 경로를 재개한다. 다만 private workspace 또는 shared handoff에
새 산출물이 생긴 경우에만 다음 slice를 자동 승인한다. 측정 가능한 진전이 없으면 token
승급을 거부한다. 이때 exploit·solver·harness·재현 문서·상태 ledger처럼 다시 사용할 수 있는
증거만 진전으로 인정한다. firmware volume, raw dump, object, 전체 추출 로그처럼 도구가 기계적으로
대량 생성한 파일은 resume manifest와 진전 판정에서 제외한다. Bash와 파일 출력은 기본 12,000자로 제한하고 잘릴 때 앞·뒤와 flag 형태
문자열을 보존한다.

CTFd를 사용할 때는 token 방식을 권장한다. URL과 token은 실행 후 대시보드에서도 입력할 수 있으며 이 값은 현재 프로세스에만 적용된다. `.env`와 Codex 인증 캐시는 비밀 정보이며 저장소에 commit하거나 대회 채팅에 공유하지 않는다.

## 6. 대회 전 점검

다음 명령이 모두 성공해야 한다.

```powershell
codex login status
docker image inspect ctf-sandbox
uv run ctf-solve --help
```

가능하면 실제 대회와 동일한 네트워크에서 연습 문제 하나를 dry-run한다.

```powershell
uv run ctf-solve `
  --challenge challenges\practice `
  --models codex/gpt-5.6-luna/medium `
  --no-submit `
  -v
```

`--no-submit`은 flag 후보를 CTFd에 제출하지 않는다. CTFd URL 자체가 비어 있어도 단일 문제 모드는 자동으로 독립 모드가 되어 CTFd 제출 없이 결과를 출력한다. 분석과 도구 호출은 그대로 수행되므로 기능 및 비용 테스트용으로 적합하다.

## 7. 문제 파일 준비

### 7.1 CTFd에서 미리 받기

```powershell
uv run python pull_challenges.py `
  --url https://ctf.example.com `
  --token ctfd_your_api_token_here `
  --output challenges
```

Username/password 방식도 지원한다.

```powershell
uv run python pull_challenges.py `
  --url https://ctf.example.com `
  --username myteam `
  --password team_password `
  --output challenges
```

전체 대회 모드는 필요한 문제를 자동으로 받을 수 있으므로 사전 pull은 필수가 아니다. 다만 파일 다운로드와 CTFd 인증을 미리 검증하는 데 유용하다.

### 7.2 수동 문제 디렉터리

단일 문제 모드에는 다음 구조가 필요하다.

```text
challenges/
└── example/
    ├── metadata.yml
    └── distfiles/
        └── challenge.bin
```

`metadata.yml` 예시:

```yaml
name: Example Challenge
category: pwn
value: 500
description: Find the flag in the remote service.
connection_info: nc challenge.example.com 31337
flag_format: TEAM{...}
tags:
  - x86_64
  - heap
hints: []
```

`distfiles/`는 컨테이너의 `/challenge/distfiles`에 read-only로 mount된다. Solver가 만든 exploit과 중간 파일은 `/challenge/workspace`에 기록되고 호스트의 `workspace/<challenge>/<model>/`에 영구 보존된다. 각 경로의 `.ctf-agent-state.json`에는 마지막 상태, 시도, step, token, 추정 비용과 종료 사유가 저장된다. 같은 문제와 model을 다시 실행하면 기존 산출물을 이어서 사용할 수 있다.

## 8. 실행 방법

### 8.1 단일 문제

기본적으로 SOL-xhigh 하나를 실행하고, SOL이 필요하다고 판단한 경우에만 Luna-low 하위 agent를 생성한다.

```powershell
uv run ctf-solve `
  --challenge challenges\example `
  --ctfd-url https://ctf.example.com `
  --ctfd-token ctfd_your_api_token_here `
  -v
```

Luna 하나만 사용해 빠르게 점검하려면 다음과 같이 실행한다.

```powershell
uv run ctf-solve `
  --challenge challenges\example `
  --models codex/gpt-5.6-luna/medium `
  --no-submit `
  -v
```

CTFd 없이 대화형 터미널에서 단일 문제를 실행하면 후보 발견 시 `Confirm "..." as the local answer?` 확인 프롬프트가 표시된다. 승인 전에는 후보일 뿐이며, 비대화형 실행에서는 후보를 출력만 하고 종료한다.

### 8.2 전체 대회—권장 운영 명령

`--challenge`를 생략하면 Codex coordinator가 계속 실행된다. CTFd 옵션은 선택 사항이다. 생략한 뒤 대시보드에서 연결하거나 독립 모드로 로컬 문제를 추가할 수 있다.

```powershell
uv run ctf-solve `
  --ctfd-url https://ctf.example.com `
  --ctfd-token ctfd_your_api_token_here `
  --challenges-dir challenges `
  --coordinator codex `
  --coordinator-model gpt-5.6-terra `
  --max-challenges 1 `
  --dashboard-port 9400 `
  -v
```

16GB PC에서는 `--max-challenges 1`로 시작한다. 가벼운 Web/Misc 문제에서 CPU, RAM, Docker 상태와 Codex quota를 확인한 뒤에만 `2` 이상으로 늘린다.

### 8.3 모델을 직접 선택하기

`--models`는 쉼표 목록이 아니라 반복 옵션이다.

```powershell
uv run ctf-solve `
  --models codex/gpt-5.6-sol/xhigh `
  --models codex/gpt-5.6-terra/high `
  --max-challenges 3 `
  --dashboard-port 9400 `
  -v
```

모델 스펙 형식은 다음과 같다.

```text
codex/<model-id>/<reasoning-effort>
```

권장 조합:

| 상황 | 조합 |
|---|---|
| 기능 점검·쉬운 문제 | `codex/gpt-5.6-luna/medium` |
| 일반 대회 운영 | Terra `high` + Luna `medium` |
| 어려운 결승 문제 | Sol `xhigh` + Terra `high` |
| 최고 품질 단일 시도 | `codex/gpt-5.6-sol/max` |

높은 reasoning effort가 항상 더 좋은 것은 아니다. 높은 단계는 지연 시간과 사용량이 늘 수 있으므로 실제 문제에서 측정해 선택한다. 대회 중 기본 조합을 바꿀 때는 실행 중인 프로세스를 종료하고 새 옵션으로 다시 시작한다.

## 9. 자원 계획

활성 solver 수의 안전 상한은 대략 다음과 같다.

```text
최대 solver 컨테이너 수 = --max-challenges × (--models 개수 + DELEGATE_MAX_CONCURRENT)
```

기본 `--max-challenges 1`에서는 SOL 하나만 먼저 실행되며, 동적 위임이 실제로 필요할 때만 최대 2개의 Luna가 추가되어 최대 3개 컨테이너가 된다. 각 컨테이너는 기본적으로 CPU 2개와 memory limit 4GB를 갖는다. Memory limit는 예약량은 아니지만 동시에 무거운 angr, SageMath, Ghidra 작업이 실행되면 호스트가 빠르게 부족해질 수 있다.

일반적인 출발점:

- 16GB RAM: 기본 SOL 주 solver, `--max-challenges 1`, `DELEGATE_MAX_CONCURRENT=1`
- 32GB RAM: 기본 SOL 주 solver, `--max-challenges 1`, 하위 agent 최대 2개
- 64GB 이상: `--max-challenges 2~3`부터 실제 peak memory를 측정

이는 보장값이 아니라 보수적인 운영 시작점이다. 문제 유형과 Docker Desktop의 VM memory 설정에 따라 달라진다.

## 10. 실행 중 운영

### 10.1 웹 대시보드

전체 대회 모드를 실행한 뒤 브라우저에서 다음 주소를 연다.

```text
http://127.0.0.1:9400
```

대시보드에서는 다음 작업을 할 수 있다.

- 실행 중 CTFd URL/token 또는 아이디/비밀번호를 입력해 연결하거나 연결 해제
- CTFd 없이 문제명, 설명, 접속 정보, 첨부 파일을 등록하고 즉시 단일 문제 풀이 시작
- 전체 문제, 해결 수, 활성 swarm과 agent, token 및 추정 비용 확인
- 문제명·카테고리 검색과 상태 필터링
- 문제 상세에서 지금까지의 핵심 접근, 확인된 사실, 현재 blocker와 다음 실험 노트 확인
- 문제별 solver 단계, findings와 최근 trace 확인
- Swarm 시작 및 중단
- 같은 문제의 모든 solver에 힌트 broadcast
- Coordinator에 운영자 메시지 전송
- Flag 후보 dry-run 또는 CTFd 수동 제출
- 기존 풀이 기록과 런타임 환경 초기화

CTFd를 연결하지 않은 독립 모드에서는 flag를 외부에 제출하지 않는다. Solver가 찾은 flag와 수동 입력한 값은 검증되지 않은 후보로 기록되고 운영자 확인 뒤에만 로컬 해결로 바뀐다. 후보와 확인된 로컬 결과는 `WORKSPACE_ROOT/.ctf-agent-runtime.json`에 원자적으로 저장되어 coordinator를 정상 재시작해도 유지된다. 대시보드에서 입력한 CTFd 인증 정보는 `.env`에 저장되지 않고 현재 coordinator 프로세스가 종료되면 사라진다.

대시보드는 coordinator와 같은 프로세스에서 실행되며 `127.0.0.1`에만 bind된다. 상태는 약 2.5초마다 갱신된다. 쓰기 요청은 브라우저가 같은 origin에서 받은 세션별 token을 요구한다.

`지금까지의 접근 노트`는 별도 LLM 호출 없이 실행 중 findings와 `STATE.md`, `SOLUTION.md`, `TRIAGE.md`, delegate/recovery handoff에서 생성된다. 최대 6개 문장만 표시하고 코드 블록·명령·표는 제외하므로 token 비용이 없으며 workspace가 보존되는 한 재시작 뒤에도 유지된다.

`풀이 기록 및 환경 초기화`는 실행 중인 모든 solver를 먼저 중단한 뒤 `challenges/`, `WORKSPACE_ROOT`, `LOGS_ROOT`, 문제 결과·후보·비용 상태와 현재 CTFd 연결을 비운다. 버튼을 누른 것만으로는 실행되지 않으며 확인 창에 `초기화`를 정확히 입력해야 한다. 삭제된 런타임 기록은 복구할 수 없지만 소스 코드, `.env`, 가상환경과 Docker 이미지는 보존된다. 안전하지 않거나 프로젝트 소스와 겹치는 경로가 설정되어 있으면 서버가 초기화를 거부한다.

다른 port를 사용하려면 다음과 같이 실행한다.

```powershell
uv run ctf-solve --dashboard-port 9500 ...
```

`--dashboard-port 0`을 사용하면 운영체제가 빈 port를 자동 선택하며 console의 `Dashboard listening` 로그에서 실제 주소를 확인한다.

### 10.2 Console과 trace 확인

`-v`를 켜면 coordinator 이벤트, 컨테이너 시작, solver 상태, 비용 추정치를 더 자세히 볼 수 있다.

Solver별 JSONL trace는 `logs/` 아래에 생성된다.

```powershell
Get-ChildItem logs
Get-Content logs\trace-문제명-모델명-타임스탬프.jsonl -Wait
```

Linux/WSL:

```bash
ls -lt logs/
tail -f logs/trace-*.jsonl
```

Trace에는 tool call, 일부 tool result, token usage, bump, 오류, 종료 상태가 기록된다. Flag와 exploit 단서가 포함될 수 있으므로 대회 후 공유 전에 검토한다.

### 10.3 Coordinator에 운영자 메시지 보내기

Coordinator를 `--dashboard-port 9400`으로 실행했다면 다른 터미널에서 다음처럼 메시지를 보낸다.

```powershell
uv run ctf-msg --port 9400 "web-100은 JWT alg confusion을 우선 확인해"
```

예시:

```powershell
uv run ctf-msg --port 9400 "rev-hard의 실제 flag 형식은 TEAM{...}"
uv run ctf-msg --port 9400 "crypto-300은 nonce reuse 가능성이 높음"
uv run ctf-msg --port 9400 "pwn-500 solver trace를 읽고 막힌 agent를 구체적으로 bump해"
```

Message endpoint는 대시보드와 같은 `127.0.0.1:9400`을 사용한다. 운영 편의를 위해 고정 port를 권장한다.

### 10.4 Flag 제출 안전장치

- `--no-submit` 또는 CTFd가 없는 독립 모드에서는 flag를 **검증되지 않은 후보**로만 기록한다. 후보만으로 문제를 해결 상태로 바꾸지 않는다.
- solver turn은 정답 근거가 없을 때 `incomplete`로 종료할 수 있으며, swarm은 설정된 attempt/runtime 예산 안에서 다른 접근으로 계속한다.
- 독립 모드에서 후보가 발견되면 해당 swarm은 추가 비용이 발생하지 않도록 중단되고 대시보드가 후보 검토 알림을 표시한다. 운영자가 `정답으로 확인` 또는 `오답으로 거부`를 선택할 때까지 문제는 `후보` 상태로 남는다. 오답으로 거부한 뒤에는 swarm을 다시 시작해 풀이를 계속한다.
- 같은 flag는 swarm 전체에서 중복 제출하지 않는다.
- 입력한 flag 형식(`cce2026{...}` 등)에 맞지 않는 후보는 CTFd 전송 전에 거부한다.
- 문제별 live 제출은 기본 8회로 제한한다.
- 오답 제출 후 model별 cooldown이 `0, 30, 120, 300, 600초` 순으로 증가한다.
- 한 solver의 flag가 정답으로 확인되면 해당 문제의 나머지 solver가 취소된다.

대시보드의 `후보` 상태와 `검증되지 않은 Flag 후보`는 정답 판정이 아니다. 실제 CTFd의 `CORRECT`/`ALREADY SOLVED` 응답을 받은 결과만 `해결`로 집계한다.

대회 규칙에 자동 제출 금지, 제출 횟수 제한 또는 penalty가 있다면 반드시 `--no-submit`으로 운영하고 사람이 검토한다.

## 11. Quota와 비용

Codex solver는 기본적으로 현재 Codex CLI 로그인 세션을 사용한다. ChatGPT 로그인은 해당 구독/workspace의 Codex 사용 정책을 따르고, API key 로그인은 OpenAI Platform 사용량 기반 과금을 따른다.

Codex quota 관련 오류가 감지되어도 기본적으로 solver를 중단하며 유료 API로 자동 전환하지 않는다. `.env`에서 `ENABLE_API_FALLBACK=true`를 명시한 경우에만 다음 direct API fallback을 시도한다.

| Codex 모델 | Fallback |
|---|---|
| `codex/gpt-5.6-sol/*` | `openai/gpt-5.6-sol` |
| `codex/gpt-5.6-terra/*` | `openai/gpt-5.6-terra` |
| `codex/gpt-5.6-luna/*` | `openai/gpt-5.6-luna` |

Fallback을 실제로 사용하려면 `ENABLE_API_FALLBACK=true`와 유효한 `OPENAI_API_KEY`가 모두 필요하며, 이때는 사용량 기반 API 요금이 실제로 발생한다. 비용 표시는 App Server가 보낸 token usage와 내장 가격표를 기반으로 한 추정치이며 최종 청구서와 다를 수 있다.

비용과 quota를 줄이는 가장 효과적인 방법은 다음과 같다.

1. `--max-challenges`를 줄인다.
2. 기본 3개 대신 Terra 또는 Luna 하나만 사용한다.
3. `xhigh`/`max`를 어려운 문제에만 사용한다.
4. 첫 시험 실행은 `--no-submit`과 Luna로 수행한다.

서로 다른 QEMU system boot 자체는 exploit 개발 과정의 정상적인 검증으로 허용한다. 대신
유효한 출력 없이 timeout만 발생한 boot가 두 번 연속이면 경고하고 다음 boot를 차단한다.
부팅·주소 leak·exploit 실행처럼 새 신호가 나온 실행은 실패 횟수를 초기화한다. Coordinator가
명시적으로 agent를 bump해도 이 상태가 초기화된다.

## 12. 종료와 재시작

정상 종료는 실행 터미널에서 `Ctrl+C`를 사용한다. Coordinator는 poller와 swarm을 중단하고 Docker 컨테이너를 정리한다. `workspace/`의 분석 산출물, solver별 `.ctf-agent-state.json`, 전역 `.ctf-agent-runtime.json`은 삭제하지 않으므로 재실행하거나 사람이 직접 이어서 분석할 수 있다. 대시보드의 `풀이 기록 및 환경 초기화`를 실행한 경우에만 이 전역 결과 상태도 함께 제거된다.

강제 종료로 컨테이너가 남았는지 확인하려면 다음을 사용한다.

```powershell
docker ps -a --filter label=ctf-agent
```

다음 `ctf-solve` 실행 시 `ctf-agent` label이 있는 orphan 컨테이너를 자동 정리한다. 같은 호스트에서 이 label을 사용하는 다른 작업이 있다면 함께 정리될 수 있으므로 label을 공유하지 않는다.

## 13. 보안 주의 사항

이 프로젝트의 Docker sandbox는 편의상 디버깅 및 포렌식 권한을 강하게 허용한다. 현재 컨테이너에는 `SYS_ADMIN`, `SYS_PTRACE`, `seccomp=unconfined`, loop device가 포함되므로 강한 보안 경계로 간주하면 안 된다.

실전 권장 사항:

- 개인 개발 PC보다 대회 전용 VM 또는 폐기 가능한 호스트에서 실행한다.
- 호스트에 cloud credential, SSH key, 개인 문서를 두지 않는다.
- Docker socket을 solver 컨테이너에 mount하지 않는다.
- 참가가 허가된 CTF 서비스 외에는 공격하지 않는다.
- `.env`, `~/.codex/auth.json`, trace를 외부에 공유하지 않는다.
- 대회 종료 후 VM과 credential을 정리하거나 교체한다.

CTFd client는 self-signed 인증서를 허용하기 위해 현재 TLS 인증서 검증을 비활성화한다. 신뢰할 수 없는 네트워크에서는 중간자 공격 위험이 있으므로 대회 공식 URL과 네트워크를 확인한다. 첨부 파일이 외부 origin에 있을 때 구현상 CTFd API token을 전달하지 않지만, 내려받은 파일 자체는 신뢰할 수 없는 입력으로 취급해야 한다.

## 14. 문제 해결

### `codex` 명령을 찾을 수 없음

```powershell
Get-Command codex
codex --version
```

Codex가 설치된 shell과 `uv run ctf-solve`를 실행하는 shell이 같은 PATH를 사용하는지 확인한다.
Windows에서는 agent가 PATH를 먼저 확인한 뒤 VS Code, VS Code Insiders, Cursor의 OpenAI 확장 경로도 자동 탐색한다. 자동 탐색이 되지 않으면 `.env`에 전체 실행 파일 경로를 지정한다.

```dotenv
CODEX_CLI_PATH=C:\Users\사용자명\.vscode\extensions\openai.chatgpt-버전\bin\windows-x86_64\codex.exe
```

### Codex 인증 오류

```powershell
codex login status
codex login
```

원격 환경이면 `codex login --device-auth`를 사용한다. 사내 TLS proxy가 있다면 공식 문서의 `CODEX_CA_CERTIFICATE` 설정을 확인한다. Codex App Server는 HTTPS뿐 아니라 secure WebSocket outbound 연결도 필요할 수 있다.

### `No metadata.yml found`

`--challenge`에는 문제 디렉터리를 지정해야 하며 그 바로 아래에 `metadata.yml`이 있어야 한다.

```text
--challenge challenges/example
                    └── metadata.yml
```

### Docker 연결 오류

```powershell
docker info
docker image inspect ctf-sandbox
docker ps -a --filter label=ctf-agent
```

Windows에서는 Docker Desktop이 실행 중이고 Linux container 모드인지 확인한다.

### Model unavailable 또는 reasoning effort 오류

Codex CLI를 업데이트한 뒤 현재 계정에서 제공되는 모델을 확인한다. 우선 기본 스펙으로 되돌린다.

```text
codex/gpt-5.6-sol/xhigh
codex/gpt-5.6-terra/high
codex/gpt-5.6-luna/medium
```

Model 가용성은 계정, workspace 정책, CLI 버전에 따라 달라질 수 있다.

### Solver가 `0 steps, $0`으로 종료

대개 모델 연결, 인증, App Server 시작 또는 Docker 시작 전에 실패한 경우다.

1. `-v`로 다시 실행한다.
2. `codex login status`를 확인한다.
3. `docker info`를 확인한다.
4. outbound HTTPS/WebSocket이 차단되지 않았는지 확인한다.
5. Luna 하나와 `--max-challenges 1`로 범위를 줄여 재현한다.

### CTFd 인증 또는 제출 오류

- CTFd URL에 불필요한 `/challenges` 경로를 넣지 말고 base URL만 사용한다.
- API token의 유효 기간과 권한을 확인한다.
- `pull_challenges.py`로 token 검증을 먼저 수행한다.
- 대회가 CTFd 호환 API를 사용하는지 확인한다.

### PC가 느려지거나 Docker memory가 부족함

- `--max-challenges 1`로 낮춘다.
- Luna 또는 Terra 하나만 선택한다.
- `.env`의 `CONTAINER_MEMORY_LIMIT`을 낮춘다.
- Docker Desktop VM에 할당된 CPU/RAM을 확인한다.

### 운영자 메시지 전송 실패

Coordinator 실행 로그의 실제 port와 `ctf-msg --port` 값이 같은지 확인한다. Endpoint는 loopback 전용이므로 다른 PC에서 직접 접속할 수 없다.

## 15. CLI 옵션 요약

| 옵션 | 설명 |
|---|---|
| `--ctfd-url URL` | `.env`의 CTFd URL 덮어쓰기 |
| `--ctfd-token TOKEN` | `.env`의 CTFd token 덮어쓰기 |
| `--image NAME` | Docker sandbox image 이름 |
| `--models SPEC` | Solver 모델 지정, 여러 번 사용 가능 |
| `--challenge DIR` | 단일 문제 모드; 생략하면 전체 coordinator 모드 |
| `--challenges-dir DIR` | 전체 대회 문제 저장 디렉터리 |
| `--no-submit` | 실제 flag 제출 금지 |
| `--coordinator codex` | Codex coordinator 사용; 현재 기본값 |
| `--coordinator-model MODEL` | Coordinator model ID |
| `--max-challenges N` | 동시에 실행할 문제 swarm 수 |
| `--dashboard-port PORT` | 로컬 대시보드와 운영자 메시지 port, 기본 `9400`; `0`은 자동 선택 |
| `--msg-port PORT` | `--dashboard-port`의 이전 버전 호환 alias |
| `-v`, `--verbose` | 상세 로그 |

항상 현재 코드의 옵션을 최종 기준으로 확인한다.

```powershell
uv run ctf-solve --help
uv run ctf-msg --help
```

## 16. 실전 체크리스트

### 대회 전날

- [ ] `uv sync` 완료
- [ ] `ctf-sandbox` image 빌드 완료
- [ ] Codex CLI 업데이트 및 로그인 완료
- [ ] 연습 문제 dry-run 성공
- [ ] Docker Desktop 자원 설정 확인
- [ ] CTFd token 발급 방법 확인
- [ ] 전용 VM snapshot 또는 복구 지점 준비

### 시작 10분 전

- [ ] `codex login status` 성공
- [ ] `docker info` 성공
- [ ] CTFd 사용 시 `.env` 또는 대시보드의 URL/token 확인
- [ ] `--max-challenges`와 모델 수 확인
- [ ] `--dashboard-port 9400` 사용 여부 확인
- [ ] 로그 저장 공간 확인

### 대회 중

- [ ] Console의 quota, Docker, CTFd 오류 감시
- [ ] `logs/`의 정체된 solver trace 확인
- [ ] 발견한 flag 형식과 공통 취약점을 coordinator에 전달
- [ ] 제출 penalty가 있으면 `--no-submit` 유지
- [ ] 자원 부족 시 Luna 단독 또는 동시 문제 수 축소

### 대회 종료 후

- [ ] `Ctrl+C`로 정상 종료
- [ ] 남은 `ctf-agent` 컨테이너 확인
- [ ] `logs/`와 필요한 결과 기록 보관
- [ ] CTFd token 및 임시 API key 폐기 또는 회전
- [ ] 전용 VM 정리

## 17. 공식 참고 문서

- [Codex CLI](https://developers.openai.com/codex/cli)
- [Codex 인증](https://developers.openai.com/codex/auth)
- [Codex CLI 명령 reference](https://developers.openai.com/codex/cli/reference)
- [Codex configuration reference](https://developers.openai.com/codex/config-reference)
- [Codex App Server](https://developers.openai.com/codex/app-server)
- [GPT-5.6 모델 가이드](https://developers.openai.com/api/docs/guides/latest-model)
- [OpenAI 모델 카탈로그](https://developers.openai.com/api/docs/models)

## 18. 모델 역할과 외부 CTF 스킬

기본 실행은 SOL-xhigh가 문제 전체의 소유권을 갖고 필요할 때만 작업을 분할한다.

| 모델 | 역할 | 주요 산출물 |
|---|---|---|
| `codex/gpt-5.6-sol/xhigh` | LEAD: 주 풀이, 작업 분할, 증거 통합, 최종 검증 | `/challenge/shared/lead/SOLUTION.md` |
| `codex/gpt-5.6-luna/low/.../delegate-NN` | DELEGATE: 지정된 단일 가설의 저비용 검증 | `/challenge/shared/delegates/delegate-NN.md` |
| `codex/gpt-5.6-luna/low/.../postprocess` | POSTPROCESS: 중단된 불완전 handoff 정리·충돌 검증 | `/challenge/shared/recovery/` |

SOL은 초기 triage를 직접 수행한 뒤 `delegate_task`로 좁고 독립적인 질문과 필요한 산출물을 지정한다. 하위 agent 생성은 즉시 반환되므로 SOL은 기다리지 않고 주 경로를 계속 푼다. SOL은 `/challenge/shared/lead/STATE.md`에 확인된 사실, 충돌, 현재 blocker, 다음 실험을 유지한다. 정적 primitive가 구체화되면 추가적인 전체 추출보다 최소 harness·debugger 측정을 우선한다.

각 delegate handoff에는 `Conclusion`, `Evidence`, `Reproduction`, `Assumptions and conflicts` 절이 필요하다. `check_delegates`는 이를 검사해 `passed` 또는 `UNSAFE`를 표시한다. SOL은 `UNSAFE` 결과를 그대로 통합하지 않고 상충하는 offset·allocator·수식 등을 가장 작은 판별 실험으로 확인한다. 표현만 바꾼 유사 작업의 중복 위임, 문제 전체 위임, 동시·누적 한도 초과 요청은 거부된다. `--models`를 여러 번 지정하면 해당 모델들은 처음부터 실행되는 고정 primary roster가 되므로 기본 적응형 구성이 더 효율적이다.

Delegate가 token·step·시간·시도 한도로 중단되면 runtime은 중단 사유, 마지막 findings, 원래 요청과 handoff 감사를 `/challenge/shared/recovery/*-budget-stop.md`에 먼저 저장하고 SOL 전용 메시지로 전달한다. 원래 handoff가 감사를 통과하면 추가 모델을 만들지 않는다. handoff가 없거나 `UNSAFE`일 때만 80K 유효 token·1회 시도의 후처리 Luna를 최대 한 개 생성한다. 후처리 worker는 새로운 풀이를 시작하지 않고 기존 증거의 모순을 정리하며, 다시 중단돼도 다른 후처리 worker를 재귀적으로 만들지 않는다.

Coordinator 실행 후 backend 소스나 `.env`가 변경되면 대시보드 상단에 **재시작 필요**가 표시된다. 현재 활성 풀이가 끝난 뒤 재시작해야 변경된 예산과 추론 정책이 적용된다.

Sandbox에는 [ljagiello/ctf-skills](https://github.com/ljagiello/ctf-skills)의 검토·고정된
revision이 `/challenge/skills/`에 설치된다. Solver는 문제 카테고리에 해당하는 `SKILL.md`를
먼저 읽고 필요한 reference만 점진적으로 사용한다. 외부 저장소의 installer나 script는 자동
실행하지 않으며, 이 프로젝트의 sandbox·범위·예산·flag 증거 규칙이 항상 우선한다.

이 기능을 적용하려면 sandbox image를 다시 빌드해야 한다.

```powershell
docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .
```
