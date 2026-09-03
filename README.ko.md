<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/DeepSeek%20%7C%20OpenAI%20%7C%20Claude%20%7C%20Gemini%20%7C%20Ollama-API-green?logo=openai" alt="Multi-Provider">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="License">
  <img src="https://img.shields.io/badge/CI-passing-brightgreen" alt="CI">
</p>

<h1 align="center">✨ OpenKyrozen ✨</h1>
<p align="center"><strong>자기 학습형 AI 에이전트 — DeepSeek · OpenAI · Claude · Gemini · Ollama</strong></p>
<p align="center">터미널 네이티브 완전 자율 AI 에이전트. <em>모든 상호작용에서 학습</em>하고,<br>파일 시스템 조작, Git 관리, 버그 수정, 그리고 지속적인 자기 진화를 실현합니다.</p>

<p align="center">
  🌐 <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <a href="README.ja.md">日本語</a> ·
  <strong>한국어</strong>
</p>

---

## 📑 목차

- [OpenKyrozen이란?](#openkyrozen이란)
- [🚀 설치](#-설치)
  - [사전 준비사항](#사전-준비사항)
  - [방법 A: 소스에서 설치](#방법-a-소스에서-설치권장)
  - [방법 B: 로컬 디렉토리에서 pip 설치](#방법-b-로컬-디렉토리에서-pip-설치)
- [📖 사용 가이드](#-사용-가이드)
  - [터미널 모드](#터미널-모드)
  - [채팅 내 명령어](#채팅-내-명령어)
  - [Web UI 모드](#web-ui-모드)
- [🏗 아키텍처](#-아키텍처)
  - [작업 복잡도 라우팅](#작업-복잡도-라우팅)
  - [모델 자동 선택](#모델-자동-선택)
  - [제공자 관리](#제공자-관리)
- [🛠 도구 레퍼런스](#-도구-레퍼런스)
  - [파일 및 시스템](#파일-및-시스템)
  - [웹](#웹)
  - [브라우저](#브라우저-5개-도구)
  - [Git (14개 도구)](#git-14개-도구)
  - [메모리](#메모리)
- [🧠 전용 워크플로우](#-전용-워크플로우)
  - [버그 수정](#버그-수정6단계-프로토콜)
  - [Git 작업](#git-작업안전-우선)
  - [복잡한 작업](#복잡한-작업절대-중간에-멈추지-않음)
- [🧬 자기 학습 시스템](#-자기-학습-시스템)
- [🌐 Web UI 및 REST API](#-web-ui-및-rest-api)
- [🔌 플러그인 시스템](#-플러그인-시스템)
- [🔐 보안](#-보안)
- [⚙️ 설정 레퍼런스](#️-설정-레퍼런스)
- [🔧 개발](#-개발)
- [📁 프로젝트 구조](#-프로젝트-구조)
- [🙏 거인의 어깨 위에 서서](#-거인의-어깨-위에-서서)
- [📄 라이선스](#-라이선스)

---

## OpenKyrozen이란?

OpenKyrozen은 터미널에서 실행되는 **자기 학습형 AI 에이전트**입니다. 일반적인 챗봇과 달리 다음과 같은 기능을 제공합니다:

- **31개 런타임 도구** — 파일, 셸, 웹, Git, 브라우저 기본 작업 29개와 SQLite 메모리 작업 2개
- **지속적 학습** — 20개 기능을 제한된 dispatcher로 실행하고 사실 추출, 스킬 발명, 전략 최적화를 기록
- **다양한 LLM 지원** — DeepSeek, OpenAI, Claude, Gemini, 또는 로컬 Ollama 모델
- **크로스 플랫폼** — macOS, Linux, Windows (터미널 기능 자동 감지 포함)
- **내장 Web UI** — 브라우저 기반 채팅 인터페이스 및 REST API 통합

사용할수록 똑똑해지는 AI 동료라고 생각하세요.

---

## 🚀 설치

### 사전 준비사항

- **Python 3.12 또는 3.13** (Python 3.14+는 OpenAI SDK와 알려진 임포트 문제가 있습니다)
- 지원되는 제공자의 API 키:

| 제공자 | 키 발급 | 비용 |
|--------|--------|------|
| **DeepSeek** | [platform.deepseek.com](https://platform.deepseek.com) | ~$0.27/100만 입력 토큰 |
| **OpenAI** | [platform.openai.com](https://platform.openai.com) | ~$2.50/100만 입력 토큰 |
| **Anthropic (Claude)** | [console.anthropic.com](https://console.anthropic.com) | ~$3.00/100만 입력 토큰 |
| **Google (Gemini)** | [aistudio.google.com](https://aistudio.google.com) | ~$0.15/100만 입력 토큰 |
| **Ollama** | [ollama.com](https://ollama.com) | 무료 (로컬 실행) |

### 방법 A: 소스에서 설치 (권장)

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen

# macOS / Linux
make install
make run

# Windows
setup.bat
run.bat
```

### 방법 B: 로컬 디렉토리에서 pip 설치

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
pip install .

# 설치 후 어디서든 실행 가능:
kyrozen          # 터미널 에이전트
kyrozen-web      # 웹 서버
```

> **참고:** PyPI에서 `pip install openkyrozen`은 곧 제공될 예정입니다. 현재는 로컬 디렉토리에서 설치하거나 저장소를 클론하세요.

첫 실행 시 API 키를 입력하라는 메시지가 표시됩니다. 에이전트는 자동으로 제공자를 감지하고 암호화된 키를 `~/.kyrozen_config.json`에 저장합니다.

---

## 📖 사용 가이드

### 터미널 모드

실행하면 배너와 `You:` 프롬프트가 표시됩니다. 자연스럽게 입력하세요 — 에이전트는 영어, 중국어, 일본어, 한국어를 이해합니다.

```text
You: README를 읽고 이 프로젝트가 무엇인지 알려줘
You: "Hello World"를 출력하는 hello.py 파일을 만들어줘
You: Python 최신 릴리스 날짜를 웹에서 검색해줘
You: main.py 200번째 줄 근처의 버그를 수정해줘
You: 모든 변경사항을 좋은 메시지로 커밋해줘
```

Kyrozen의 동작:
1. 요청 분류 (간단 / 중간 / 복잡)
2. 작업에 가장 적합한 모델 선택
3. 필요시 계획 생성
4. 도구를 단계별로 실행
5. 실시간 작업 패널에 진행 상황 표시
6. 완료된 작업 요약

### 채팅 내 명령어

| 명령어 | 기능 |
|--------|------|
| `/quit` 또는 `/exit` | 에이전트 종료 |
| `/provider` | LLM 제공자 전환 (대화형 메뉴) |
| `/api_key` | API 키 변경 |
| `/learn` | 프로젝트 파일을 즉시 메모리에 스캔 |
| `/forget` | 최근 학습 확인; `/forget 키워드`로 잘못된 학습 삭제 |
| `/update` | Git에서 최신 버전 가져오기 |
| `/self-learning` | 개별 자기 학습 기능 켜기/끄기 |

### Web UI 모드

```bash
python server.py --port 8000
# http://localhost:8000 열기

# 또는 Docker 사용:
docker build -t openkyrozen .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-... \
  -e KYROZEN_SERVER_TOKEN=change-me \
  -v kyrozen-data:/data \
  openkyrozen
```

웹 인터페이스는 실시간 스트리밍, 비용 추적, 세션 관리 기능을 갖춘 다크 테마 채팅 UI를 제공합니다.

---

## 🏗 아키텍처

```
사용자 입력
    │
    ▼
┌─────────────────┐
│   작업 분류기   │──► 간단 / 중간 / 복잡
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   모델 선택기   │──► deepseek-chat / deepseek-reasoner / gpt-4o / claude / gemini / llama
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   LLM 제공자    │──► 5개 백엔드, 자동 폴백 체인 포함
└────────┬────────┘
         │  응답 + 도구 호출
         ▼
┌─────────────────┐
│   도구 실행기   │──► 31개 런타임 도구 (파일 I/O, 셸, Git, 웹, 메모리, 브라우저)
└────────┬────────┘
         │  도구 결과를 LLM에 피드백
         │  (턴당 최대 50회 도구 호출)
         ▼
┌─────────────────┐
│     응답 출력   │──► 사용자에게 답변 + 작업 요약 표시
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   자기 학습 시스템 │──► 백그라운드: 사실 추출, 메모리 점수화, 지식 그래프 구축
└─────────────────┘
```

### 작업 복잡도 라우팅

Kyrozen은 모든 요청을 자동 분류하고 동작을 조정합니다:

| 레벨 | 트리거 예시 | 에이전트 동작 |
|------|-----------|-------------|
| **간단** | "안녕", "Python이 뭐야", "고마워" | 직접 응답, 계획 오버헤드 없음 |
| **중간** | "파일 목록 보고 README 읽어줘" | 번호가 매겨진 계획 생성, 도구 순차 실행 |
| **복잡** | "이 저장소 감사해줘", "버그 수정하고 커밋해줘", "웹 앱 구축해줘" | 전체 계획 → 작업 목록 → 진행 추적 → 절대 중간에 멈추지 않음 |

### 모델 자동 선택

에이전트는 간단한 작업과 복잡한 작업에 서로 다른 모델을 선택합니다. 재정의할 수 있습니다:

```bash
export KYROZEN_MODEL_SIMPLE=deepseek-chat
export KYROZEN_MODEL_COMPLEX=deepseek-reasoner
```

| 제공자 | 간단한 작업 (기본값) | 복잡한 작업 (기본값) |
|--------|-------------------|---------------------|
| DeepSeek | `deepseek-chat` | `deepseek-reasoner` |
| OpenAI | `gpt-4o` | `gpt-4o` |
| Anthropic | `claude-sonnet-4-20250514` | `claude-sonnet-4-20250514` |
| Google | `gemini-2.5-flash` | `gemini-2.5-pro` |
| Ollama | `llama3.2` | `llama3.2` |

### 제공자 관리

언제든지 제공자를 전환할 수 있습니다 — 채팅에서 `/provider`를 사용하거나 환경 변수로:

```bash
export KYROZEN_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

주 제공자가 실패하면 Kyrozen은 자동으로 폴백 체인(예: DeepSeek → OpenAI → Claude)을 통해 전환합니다. 속도 제한 오류(HTTP 429)는 지터가 포함된 지수 백오프를 트리거합니다. Ollama는 키가 필요 없는 로컬 제공자입니다. `KYROZEN_PROVIDER=ollama`를 설정하고 필요하면 `KYROZEN_BASE_URL`로 OpenAI-compatible endpoint를 지정하세요. Web headless 시작은 stdin을 읽지 않습니다. 원격 제공자 키가 없으면 명확한 degraded 상태가 되며 대화형 CLI만 키를 요청합니다.

---

## 🛠 도구 레퍼런스

모든 31개 런타임 도구는 JSON 액션 블록에서 일반 문자열 `args` 필드를 허용합니다. 도구 이름, capability 라벨, MCP 입력 schema 및 실제 HTTP 경로는[생성된 런타임 인벤토리](docs/tool-inventory.md)를 기준으로 합니다:

```json
{"action": "read_file", "args": "README.md"}
```

짧은 별칭도 작동합니다 — `bash`, `cmd`, `sh` → `run_cmd`; `status`, `diff`, `log` → `git_status` 등.

### 파일 및 시스템

| 도구 | 설명 | 예시 |
|------|------|------|
| `read_file` | 파일 내용 읽기 | `"README.md"` |
| `write_file` | 파일 생성 또는 덮어쓰기 | `"path|content"` |
| `list_dir` | 디렉토리 내용 나열 | `"."` |
| `list_tree` | 재귀적 디렉토리 트리 | `"src/"` |
| `find_files` | Glob 기반 파일 검색 | `"*.py|."` |
| `run_cmd` | 셸 명령 실행 | `"python --version"` |
| `execute_terminal_command` | `run_cmd`의 별칭 | `"python --version"` |

### 웹

| 도구 | 설명 | 예시 |
|------|------|------|
| `search_web` | 인터넷 검색 (Google → DDG → Wikipedia) | `"최신 Python 릴리스"` |
| `read_webpage` | URL 텍스트 콘텐츠 가져오기 | `"https://example.com"` |
| `analyze_remote_repo` | 원격 저장소를 클론하고 요약 | `"https://github.com/org/repo"` |

### 브라우저 (5개 도구)

브라우저 도구는 격리된 profile을 사용합니다. 사용 전에 선택적 browser
extra를 설치하세요.

| 도구 | 설명 | 예시 |
|------|------|------|
| `browser_open` | URL 열기 | `"https://example.com"` |
| `browser_snapshot` | 현재 페이지 텍스트 읽기 | `"session-id"` |
| `browser_click` | CSS selector 클릭 | `"session-id|button.submit"` |
| `browser_type` | CSS selector 입력 | `"session-id|input[name=q]|query"` |
| `browser_close` | 격리된 브라우저 세션 닫기 | `"session-id"` |

### Git (14개 도구)

| 도구 | 기능 |
|------|------|
| `git_status` | 작업 트리 상태 표시 |
| `git_diff` | 언스테이지드 / 스테이지드 / 커밋 간 차이 |
| `git_log` | 커밋 기록 (`--oneline --decorate`) |
| `git_branch` | 브랜치 나열 / 생성 / 삭제 |
| `git_add` | 커밋할 파일 스테이지 |
| `git_commit` | 메시지와 함께 커밋 |
| `git_push` / `git_pull` | 원격 동기화 |
| `git_checkout` | 브랜치 전환 또는 파일 복원 |
| `git_stash` | 작업 변경사항 스태시 / 팝 / 나열 |
| `git_reset` | HEAD 재설정 (`--soft` 안전, `--hard` 경고) |
| `git_show` | `--stat`으로 커밋 상세 정보 확인 |
| `git_remote` | 원격 저장소 나열 / 추가 / 삭제 |
| `git_clone` | 저장소 클론 |

### 메모리

| 도구 | 설명 |
|------|------|
| `search_memory` | 저장된 지식의 의미 기반 검색 |
| `check_stored_data` | 메모리 통계 및 최근 사실 |

---

## 🧠 전용 워크플로우

### 버그 수정 (6단계 프로토콜)

오류나 트레이스백을 붙여넣으면 Kyrozen이 자동으로 활성화됩니다:

1. **재현** — 문제의 코드를 읽고 실패한 명령을 다시 실행
2. **진단** — 트레이스백을 파싱하여 근본 원인 식별
3. **가설** — 변경하기 전에 수정 방안을 명시
4. **수정** — 최소한의 코드 변경 적용
5. **검증** — 실패한 명령을 다시 실행; 실패하면 2단계로 돌아감
6. **설명** — 무엇이 잘못되었는지, 무엇이 변경되었는지, 그 이유를 설명

수정 후 Kyrozen은 결과를 추적합니다. "고마워, 잘 작동해"라고 말하면 성공을 기록합니다. "아직도 고장이야"라고 말하면 실패를 기록하고 더 깊은 분석을 트리거합니다.

### Git 작업 (안전 우선)

- 항상 먼저 `git_status` 실행
- 커밋 전에 `git_diff` 확인
- 규칙 기반 커밋 접두사 사용: `fix:`, `feat:`, `refactor:`, `chore:`
- 명시적 요청 없이 강제 푸시하지 않음
- 브랜치 전환 전에 커밋되지 않은 변경사항 자동 스태시
- `git reset --hard` 전에 경고

### 복잡한 작업 (절대 중간에 멈추지 않음)

다단계 작업(리팩토링, 프로젝트 생성기, 코드베이스 감사)의 경우:

- 요청을 검증 가능한 하위 작업으로 분해
- 번호가 매겨진 계획 생성
- 각 계획 단계에 매핑된 JSON 작업 목록 구축
- `TaskDone` 마커로 진행 상황 추적
- 완료 시 자동 요약 생성

---

## 🧬 자기 학습 시스템

이것이 Kyrozen을 특별하게 만듭니다. **20개의 자기 학습 기능**을 하나의 레지스트리로 관리하고, 각 기능을 독립 플래그가 있는 제한된 단위로 실행합니다. 실제 상태가 바뀌었는지도 SQLite 이벤트에 기록합니다.

### 작동 방식

CLI는 유휴 상태에서 30초마다 최대 4개 기능을 라운드 로빈으로 실행합니다. Web/Gateway는 영속적인 `learning_cycle` 스케줄러 작업을 사용하고, 채팅 턴에서는 입력에 의존하는 선호도 및 기술 감지도 실행합니다. 모든 기능은 `/self-learning`으로 개별 전환할 수 있으며 `GET /api/v2/learning/features`에서 최신 상태를 확인할 수 있습니다. 입력이나 증거가 없으면 변경 없음인 제한된 no-op으로 기록됩니다.

| # | 기능 | 학습 내용 |
|---|------|---------|
| 1 | **대화 학습** | 채팅에서 사실, 선호도, 패턴 추출 |
| 2 | **프로젝트 파일 스캔** | 모든 `.py` 파일을 컨텍스트용으로 메모리에 읽기 |
| 3 | **오래된 항목 에이징** | 더 이상 존재하지 않는 파일에 대한 사실 삭제 |
| 4 | **도구 자동 디버그** | 도구 실패를 분석하고 근본 원인 식별 |
| 5 | **메모리 통합** | 저장된 사실의 중복 제거 및 요약 |
| 6 | **도구 검토** | 사용률이 낮은 도구의 제거 제안 |
| 7 | **대상 탐색** | 문서화되지 않은 함수를 찾아 목적 추론 |
| 8 | **유휴 반성** | 복잡한 작업 후 무엇이 잘 되었는지 반성 |
| 9 | **전략 증류** | 토큰 사용량이 높을 때 효율성 팁 추출 |
| 10 | **신기술 자동 패치** | 알려지지 않은 라이브러리가 언급되면 웹 검색 |
| 11 | **스킬 발명** | 과거 성공에서 재사용 가능한 스킬 템플릿 생성 |
| 12 | **컨텍스트 압축** | 컨텍스트가 30K자를 초과하면 이전 턴 요약 |
| 13 | **수정 검증** | 시간에 따른 버그 수정 성공률 추적 |
| 14 | **동적 도구 생성** | `DefineTool:` 구문으로 에이전트가 새 도구 구축 |
| 15 | **사용자 선호도 모델** | 코딩 스타일, 선호 언어, 상세도 감지 |
| 16 | **자율 점검** | 오래된 패키지, 코드 스멜, gitignore 누락 확인 |
| 17 | **메모리 중요도 점수화** | 항목을 0-10으로 평가; 높은 점수의 항목 우선 |
| 18 | **지식 그래프** | 저장된 사실에서 엔티티→관계 맵 구축 |
| 19 | **스킬 합성** | 여러 학습된 스킬을 워크플로우로 연결 |
| 20 | **잘못된 학습 롤백** | `/forget` 명령으로 잘못된 학습 삭제 |

### 메모리 저장소

OpenKyrozen v2의 장기 메모리는 **SQLite를 사실의 원본**(`~/.kyrozen/v2/openkyrozen.sqlite3`)으로 사용하고, ChromaDB는 다시 만들 수 있는 파생 의미 인덱스로 사용합니다. workspace와 session은 분리되며 ChromaDB를 사용할 수 없어도 SQLite 키워드 검색으로 영속성이 유지됩니다. Web/MCP 단일 사용자 배포에서는 `KYROZEN_SERVER_TOKEN` 하나가 안정적인 actor 하나를 나타내고, 요청의 `speaker`만으로 private 데이터의 소유자를 바꿀 수 없습니다.

작업은 재시작 후에도 저장되며 상태는 `pending`, `running`, `succeeded`, `failed`, `blocked`, `cancelled`입니다(이전 `done`은 읽기 호환). `TaskDone`만으로는 성공하지 않고 도구 결과, 테스트, 파일 확인 또는 명시적 확인 증거가 필요합니다. 안전한 API 작업은 worker가 재개하며 failed/blocked 작업은 `/api/v2/tasks/{task_id}/resume`으로 명시적으로 재개합니다.

---

## 🌐 Web UI 및 REST API

```bash
pip install fastapi uvicorn
python server.py --port 8000
# http://localhost:8000 열기
```

### REST API 엔드포인트

| 메서드 | 엔드포인트 | 설명 |
|--------|----------|------|
| `GET` | `/` | 다크 테마 채팅 Web UI |
| `POST` | `/api/chat` | 메시지를 보내고 메모리 receipt가 포함된 JSON 응답 받기 |
| `POST` | `/api/chat/stream` | SSE 스트리밍; `[DONE]` 이후에만 완료 webhook 전송 |
| `GET` | `/api/memory?q=키워드` | 저장된 메모리 검색 |
| `GET` | `/api/v2/memory?q=키워드&speaker=...&audience=...&channel=...` | provenance 및 참여자 scope가 포함된 구조화 메모리 |
| `GET/POST` | `/api/v2/tasks` | 영속 작업 조회 및 생성 |
| `POST` | `/api/v2/tasks/{task_id}/resume` | failed/blocked 작업을 명시적으로 재개 |
| `GET` | `/api/v2/learning` | 학습 제안 상태 조회 |
| `GET` | `/api/v2/learning/metrics?profile=...` | 완료, 수정, 오류, 도구, token, 지연 지표 조회 |
| `GET` | `/api/v2/learning/features` | 권위 있는 20개 기능 레지스트리와 최신 실행 상태 |
| `GET` | `/api/v2/learning/{proposal_id}/evidence` | proof card, 적용성, replay 및 결과 receipt 조회 |
| `POST` | `/api/v2/learning/{proposal_id}/replay` | 후보/선행 artifact의 paired replay 결과 기록 |
| `POST` | `/api/v2/learning/{proposal_id}/omission` | artifact 유/무 paired 결과 기록 |
| `POST` | `/api/v2/learning/{proposal_id}/retire` | 비회귀 omission 증거로 artifact retire |
| `POST` | `/api/v2/learning/{proposal_id}/restore` | retired artifact를 canary로 복구 |
| `GET` | `/api/v2/learning/{proposal_id}/capsule` | redacted·harness 독립 경험 capsule 내보내기 |
| `POST` | `/api/v2/learning/capsules` | capsule을 비활성 후보로 가져오기 |
| `GET` | `/api/v2/learning/constitution` | 변경 불가능한 사용자 소유 learning policy 조회 |
| `POST` | `/api/v2/learning/{proposal_id}/rollback` | 활성화된 학습 제안 rollback |
| `GET/POST` | `/api/v2/memory/claims` | 유형·귀속·scope가 있는 memory claim 조회/생성 |
| `GET/DELETE` | `/api/v2/memory/claims/{claim_id}` | claim 설명 또는 단독 의존 항목과 함께 삭제 |
| `GET` | `/api/v2/events` | runtime, session, task, learning 감사 이벤트 조회 |
| `GET/POST` | `/api/v2/schedules` | 영속 interval/one-shot Gateway job |
| `POST` | `/api/v2/schedules/{job_id}/disable` | 예약 job 비활성화 |
| `GET` | `/api/v2/skills` | candidate/active skill 조회 |
| `POST` | `/api/v2/skills/install` | 로컬 `SKILL.md` package 검증 및 설치 |
| `POST` | `/api/v2/skills/{skill_id}/activate` | 검증된 skill 활성화 |
| `POST` | `/api/v2/skills/{skill_id}/rollback` | skill rollback |
| `GET` | `/api/v2/sessions` | 영속 session 조회 |
| `GET` | `/api/v2/sessions/{session_id}` | session context 복구/읽기 |
| `GET` | `/api/v2/agents` | 전문 sub-agent profile 조회 |
| `POST` | `/api/v2/agents/run` | 격리된 memory와 capability로 sub-agent 실행 |
| `GET` | `/api/cost` | token 사용량 및 비용 요약 |
| `GET` | `/api/health` | provider 상태 + memory 수 |
| `GET` | `/api/voice/speak?text=...` | 시스템 TTS 텍스트 음성 변환 |
| `POST` | `/api/voice/transcribe` | 음성-텍스트 변환 (패스스루) |
| `POST` | `/api/webhooks/register` | Webhook URL 등록 |
| `GET` | `/api/webhooks` | 등록된 Webhook 조회 |
| `POST` | `/api/webhooks/test` | 테스트 Webhook 실행 |
| `POST` | `/mcp` | 모델 컨텍스트 프로토콜 (JSON-RPC 2.0) |

모든 JSON Action은 일반 문자열 `args`를 사용합니다. MCP의 `tools/list`와
`server/discover`는 허용된 각 도구의 `inputSchema`를 반환하고 object 인자를 같은 문자열 계약으로 명시적으로 변환합니다. 알 수 없거나 권한이 없는 도구는 JSON-RPC protocol error이며, 실행된 도구의 실패는 `result.isError: true`입니다. 전체 정식 목록은 [docs/tool-inventory.md](docs/tool-inventory.md)를 참조하세요.

### Docker 배포

```bash
docker build -t openkyrozen .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-your-key \
  -e KYROZEN_SERVER_TOKEN=change-me \
  -e KYROZEN_DB_PATH=/data/openkyrozen.sqlite3 \
  -v kyrozen-data:/data \
  openkyrozen
```

이미지는 root가 아닌 `kyrozen` 사용자로 실행됩니다. SQLite 원본 데이터는 `/data/openkyrozen.sqlite3`에 저장되며(이미지에서 `KYROZEN_DB_PATH`를 이 경로로 설정), 컨테이너를 교체할 때도 같은 이름의 볼륨을 `/data`에 마운트해야 합니다. 로컬에서는 `make docker-smoke`로 교체 후 복구 테스트를 실행할 수 있습니다.

---

## 🔌 플러그인 시스템

`plugins/` 디렉토리에 `register()` 함수가 있는 `.py` 파일을 만듭니다:

```python
# plugins/my_plugin.py
class MyPlugin:
    def on_startup(self, agent=None, **kwargs):
        print("플러그인이 로드되었습니다!")

    def on_turn_start(self, user_input, **kwargs):
        print(f"사용자 발언: {user_input[:50]}")

    def on_tool_execute(self, action, args, result, **kwargs):
        print(f"도구 {action}({args[:30]}) → {result[:30]}")

def register():
    return MyPlugin()
```

사용 가능한 훅: `on_startup`, `on_turn_start`, `on_turn_end`, `on_tool_execute`.

작동 예시는 `plugins/turn_logger.py`를 참조하세요.

---

## 🔐 보안

| 기능 | 보호 내용 |
|------|---------|
| **위험 명령어 필터** | `rm -rf`, `mkfs`, 포크 폭탄, Windows 파괴적 명령어 차단 |
| **API 키 암호화** | 무작위 설치 비밀을 사용하는 Fernet 암호화; 설정/비밀 파일 권한 `0600` |
| **프롬프트 인젝션 보호** | 9가지 일반적인 인젝션 패턴 감지 및 필터링 |
| **샌드박스 실행** | 파일 작업을 워크스페이스 경계 내로 제한 |
| **API 인증** | loopback 외 API/MCP 접근에는 `KYROZEN_SERVER_TOKEN` 필요 |
| **Capability 프로필** | Web/MCP 기본값은 `workspace`; 되돌릴 수 없는 `git_reset`과 동적 도구는 `full`에서 명시적으로 허용 |
| **Git 안전** | 강제 푸시 없음; CLI가 고영향 작업을 확인하고 기록 |
| **감사 로그** | 모든 채팅/API 이벤트를 타임스탬프와 함께 `kyrozen_audit.log`에 기록 |
| **Python 버전 가드** | Python 3.14+에서 시작 거부 |
| **도구 실패 메모리** | 과거 실패를 기억하고 반복 방지 |

---

## ⚙️ 설정 레퍼런스

### 환경 변수

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `KYROZEN_PROVIDER` | LLM 제공자 | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek API 키 | — |
| `OPENAI_API_KEY` | OpenAI API 키 | — |
| `ANTHROPIC_API_KEY` | Anthropic API 키 | — |
| `GEMINI_API_KEY` | Google Gemini API 키 | — |
| `KYROZEN_API_KEY` | 범용 API 키 (제공자별 키 재정의) | — |
| `KYROZEN_MODEL_SIMPLE` | 간단/중간 작업용 모델 | 제공자 기본값 |
| `KYROZEN_MODEL_COMPLEX` | 복잡한 작업용 모델 | 제공자 기본값 |
| `KYROZEN_BASE_URL` | 사용자 정의 API 기본 URL | 제공자 기본값 |
| `KYROZEN_DB_PATH` | SQLite 사실 저장소 경로 | `~/.kyrozen/v2/openkyrozen.sqlite3` |
| `KYROZEN_SERVER_TOKEN` | loopback 외 Web/MCP 접근 토큰 | 설정되지 않음 (loopback만) |
| `KYROZEN_SERVER_ACTOR` | 단일 사용자 배포의 안정적인 actor 라벨 | `local` |
| `KYROZEN_EXECUTION_SURFACE` | 실행 표면 (`cli` 또는 `web`) | `cli` |
| `KYROZEN_ALLOW_DYNAMIC_TOOLS` | LLM 생성 Python 도구 허용 (`1`/`true`) | CLI: 활성화; Web/MCP: 비활성화 |
| `KYROZEN_APPROVAL_MODE` | CLI 고영향 Git/동적 도구 확인 (`dangerous`/`never`) | `dangerous` |
| `KYROZEN_WEB_CAPABILITIES` | Web capability (`readonly`, `workspace`, `full`) | `workspace` |
| `KYROZEN_MCP_CAPABILITIES` | MCP capability (`readonly`, `workspace`, `full`) | `workspace` |
| `KYROZEN_AGENT_CONFIG` | 명시적인 `agent.yaml` 경로 | 작업공간, 그 다음 패키지 기본값 |
| `KYROZEN_ROLE` / `KYROZEN_ROLE_PROMPT` | role 이름 또는 role prompt 재정의 | 패키지 prompt |
| `KYROZEN_INSTRUCTIONS` / `KYROZEN_EXAMPLES` | 실행 지침 또는 JSON examples 재정의 | 패키지 prompt |
| `KYROZEN_AGENT_CAPABILITIES` | capability 상한 (surface/승인/인증 우회 불가) | `full` |

### 설정 파일 (`~/.kyrozen_config.json`)

```json
{
  "provider": "deepseek",
  "api_key": "<암호화됨>",
  "model_simple": "deepseek-chat",
  "model_complex": "deepseek-reasoner",
  "encrypted": true
}
```

파일은 자동 관리됩니다. 채팅에서 `/provider` 또는 `/api_key`로 대화형 업데이트가 가능합니다.

---

## 🔧 개발

```bash
# 빠른 검증
make check
make docs-check
make shell-check

# 구문 검사만
make lint

# 디버그 모드 (형식 오류 트랩)
make debug

# 최초 API 키 설정
make init

# Python 버전 변경 후 venv 재구축
make reinstall

# 웹 서버 실행
make web

# Git 헬퍼
make git-status
make git-log
make commit msg='feat: 설명'
make push
```

### CI/CD

GitHub Actions가 모든 푸시와 PR에서 자동 실행:
- Python 3.12 및 3.13에서 구문 검사
- 실제 런타임 레지스트리에서 생성한 도구 목록과 문서 일관성 검사
- 제공자 임포트 확인
- Docker 빌드 및 컨테이너 교체 복구 스모크 테스트

### pip 패키지

```bash
# 로컬 디렉토리에서 설치 (PyPI 게시 곧 예정)
pip install .                   # 코어 + CLI
pip install '.[web]'            # + Web UI
pip install '.[all]'            # + Claude + Gemini + Web
```

---

## 📁 프로젝트 구조

```
OpenKyrozen/
├── main.py              # 코어 에이전트 루프, 자기 학습, 채팅 턴 로직
├── tools.py             # 기본 도구 29개; main.py가 SQLite 메모리 작업 2개 추가
├── providers.py         # 멀티 LLM 추상화 (5개 제공자 + 폴백)
├── memory.py            # SQLite 사실 메모리 + 재생성 가능한 Chroma 인덱스
├── server.py            # FastAPI 웹 서버 + REST API + 채팅 UI
├── pyproject.toml       # pip 패키지 설정
├── Dockerfile           # Docker 이미지 정의
├── Makefile             # 빌드 자동화 (macOS/Linux)
├── setup.bat / run.bat  # Windows 배치 스크립트
├── plugins/             # 플러그인 디렉토리 (훅 기반)
├── prompts/             # 프롬프트 템플릿 (역할, 지침, 예시)
├── docs/tool-inventory.md # 생성된 런타임 도구/경로 인벤토리
├── scripts/              # 재현 가능한 문서/스모크 검사
└── .github/workflows/   # CI/CD 파이프라인
```

---

## 🙏 거인의 어깨 위에 서서

OpenKyrozen은 훌륭한 오픈소스 프로젝트 위에 구축되었습니다. 모든 메인테이너와 기여자에게 감사드립니다.

| 프로젝트 | 저장소 | 용도 |
|---------|--------|------|
| **Aider** | [paul-gauthier/aider](https://github.com/paul-gauthier/aider) | 멀티턴 에이전트 루프, 도구 호출 패턴, Git 안전 규칙에 영감 |
| **CodeWhale** | [deepseek-ai/codewhale](https://github.com/deepseek-ai/codewhale) | 에이전트 런타임 아키텍처, 서브 에이전트 위임, 검증 규율 |
| **Chroma** | [chroma-core/chroma](https://github.com/chroma-core/chroma) | 장기 메모리와 의미 검색을 지원하는 벡터 데이터베이스 |
| **FastAPI** | [fastapi/fastapi](https://github.com/fastapi/fastapi) | 웹 서버, REST API, 실시간 스트리밍 엔드포인트 |
| **Rich** | [Textualize/rich](https://github.com/Textualize/rich) | 터미널 UI — 패널, 프로그레스 바, 구문 강조, 실시간 표시 |
| **OpenAI Python** | [openai/openai-python](https://github.com/openai/openai-python) | DeepSeek, OpenAI, Ollama 제공자를 위한 통합 API 클라이언트 |
| **Uvicorn** | [encode/uvicorn](https://github.com/encode/uvicorn) | 프로덕션 웹 배포용 ASGI 서버 |
| **googlesearch-python** | [Nv7-GitHub/googlesearch](https://github.com/Nv7-GitHub/googlesearch) | DuckDuckGo 사용 불가 시 웹 검색 폴백 |

> *"내가 더 멀리 볼 수 있었던 것은 거인의 어깨 위에 서 있었기 때문입니다."* — 아이작 뉴턴

---

## 📄 라이선스

MIT 라이선스. 자세한 내용은 `LICENSE` 파일을 참조하세요.

---

<p align="center">
  <sub>배우는 AI를 원하는 개발자를 위해 ❤️를 담아</sub>
</p>
