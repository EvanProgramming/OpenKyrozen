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
  - [Git (15개 도구)](#git-15개-도구)
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

- **내장 26개 도구** — 파일 읽기/쓰기, 셸 명령 실행, 웹 검색, Git 저장소 관리
- **지속적 학습** — 20개의 자기 학습 기능이 백그라운드에서 실행되며 사실 추출, 스킬 발명, 전략 최적화 수행
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
docker run -p 8000:8000 -e DEEPSEEK_API_KEY=sk-... openkyrozen
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
│   도구 실행기   │──► 26개 내장 도구 (파일 I/O, 셸, Git, 웹, 메모리)
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

주 제공자가 실패하면 Kyrozen은 자동으로 폴백 체인(예: DeepSeek → OpenAI → Claude)을 통해 전환합니다. 속도 제한 오류(HTTP 429)는 지터가 포함된 지수 백오프를 트리거합니다.

---

## 🛠 도구 레퍼런스

모든 26개 도구는 JSON 액션 블록에서 일반 문자열 `args` 필드를 허용합니다:

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

### 웹

| 도구 | 설명 | 예시 |
|------|------|------|
| `search_web` | 인터넷 검색 (Google → DDG → Wikipedia) | `"최신 Python 릴리스"` |
| `read_webpage` | URL 텍스트 콘텐츠 가져오기 | `"https://example.com"` |

### Git (15개 도구)

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
| `analyze_remote_repo` | 클론 + 모든 파일 읽기 → 구조화된 요약 |

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

이것이 Kyrozen을 특별하게 만듭니다. **20개의 자기 학습 기능**이 백그라운드에서 지속적으로 실행되며 — 수동 저장이 필요 없습니다. 사용할수록 에이전트는 더 똑똑해집니다.

### 작동 방식

30초마다 (유휴 상태일 때) Kyrozen은 학습 사이클을 실행합니다. 대부분의 기능은 `/self-learning`으로 켜기/끄기가 가능하며, 나머지 9개는 백그라운드에서 자동으로 실행됩니다.

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

장기 메모리는 **ChromaDB**(벡터 데이터베이스, `chroma_memory/`에 저장)를 사용합니다. ChromaDB를 사용할 수 없는 경우 인메모리 저장소로 폴백합니다. 메모리는 의미 기반 검색이 가능하며 — 에이전트는 몇 주 전의 관련 사실을 기억할 수 있습니다.

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
| `POST` | `/api/chat` | 메시지 전송, JSON 응답 받기 |
| `POST` | `/api/chat/stream` | SSE 스트리밍 채팅 |
| `GET` | `/api/memory?q=키워드` | 저장된 메모리 검색 |
| `GET` | `/api/cost` | 토큰 사용량 및 비용 요약 |
| `GET` | `/api/health` | 제공자 상태 + 메모리 수 |
| `GET` | `/api/voice/speak?text=...` | 시스템 TTS로 텍스트 음성 변환 |
| `POST` | `/api/voice/transcribe` | 음성-텍스트 변환 (패스스루) |
| `POST` | `/api/webhooks/register` | Webhook URL 등록 |
| `GET` | `/api/webhooks` | 등록된 Webhook 나열 |
| `POST` | `/api/webhooks/test` | 테스트 Webhook 실행 |
| `POST` | `/mcp` | 모델 컨텍스트 프로토콜 (JSON-RPC 2.0) |

### Docker 배포

```bash
docker build -t openkyrozen .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-your-key \
  -v $(pwd)/chroma_memory:/app/chroma_memory \
  openkyrozen
```

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
| **API 키 암호화** | `~/.kyrozen_config.json` 정적 암호화 (XOR + 머신 파생 SHA-256 키) |
| **프롬프트 인젝션 보호** | 9가지 일반적인 인젝션 패턴 감지 및 필터링 |
| **샌드박스 실행** | 파일 작업을 워크스페이스 경계 내로 제한 |
| **Git 안전성** | 강제 푸시 없음, 하드 리셋 전 경고 |
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
- 도구 인벤토리 검증
- 제공자 임포트 확인
- Docker 빌드 검증

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
├── tools.py             # 26개 내장 도구 (파일, 셸, Git, 웹)
├── providers.py         # 멀티 LLM 추상화 (5개 제공자 + 폴백)
├── memory.py            # ChromaDB 기반 벡터 메모리
├── server.py            # FastAPI 웹 서버 + REST API + 채팅 UI
├── pyproject.toml       # pip 패키지 설정
├── Dockerfile           # Docker 이미지 정의
├── Makefile             # 빌드 자동화 (macOS/Linux)
├── setup.bat / run.bat  # Windows 배치 스크립트
├── plugins/             # 플러그인 디렉토리 (훅 기반)
├── prompts/             # 프롬프트 템플릿 (역할, 지침, 예시)
├── chroma_memory/       # ChromaDB 영구 저장소 (자동 생성)
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
