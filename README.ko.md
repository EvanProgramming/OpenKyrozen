<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?logo=python&logoColor=white" alt="Python 3.12 또는 3.13">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-555555" alt="macOS, Linux, Windows">
  <img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT 라이선스">
</p>

<p align="center">
  <img src="docs/openkyrozen-banner.svg" alt="OpenKyrozen 터미널 애니메이션 워드마크" width="960">
</p>

<h1 align="center">OpenKyrozen</h1>

<p align="center"><strong>실행하고, 기억하고, 검증된 결과를 바탕으로 개선하는 로컬 우선 터미널 에이전트입니다.</strong></p>

## 설치

OpenKyrozen은 Python **3.12와 3.13**을 지원합니다. Python 3.14는 지원 대상이 아닙니다.

### macOS 또는 Linux

```bash
curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.5/install.sh | sh
kyrozen
```

### Windows PowerShell

```powershell
irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.5/install.ps1 | iex
kyrozen
```

설치 프로그램은 지원되는 Python 환경과 터미널 UI를 준비하고 ~/.kyrozen 아래에 개인 상태를 저장합니다. API key를 읽거나 출력하지 않습니다.

처음 실행할 때 provider를 선택하고 키를 입력합니다. 예:

```bash
export DEEPSEEK_API_KEY=your-key
kyrozen
```

### 소스 체크아웃에서 실행

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
make install
make run
```

가벼운 개발 환경은 make install-core를 사용합니다. Windows 소스 환경은 setup.bat과 run.bat을 사용합니다.

### 검증된 release wheel 직접 설치

```bash
uv tool install --python 3.12 --force --with fastapi --with uvicorn https://github.com/EvanProgramming/OpenKyrozen/releases/download/v2.0.5/openkyrozen-2.0.5-py3-none-any.whl
```

## 빠른 시작

```text
사용자: 이 프로젝트를 읽고 아키텍처를 설명해 줘
사용자: tests/test_server.py의 실패한 테스트를 수정해 줘
사용자: 최신 Python 릴리스 날짜를 검색해 줘
사용자: REST endpoint 추가 계획을 만들어 줘
```

자주 쓰는 명령:

```text
/provider              provider 전환
/mode ask|plan|agent   읽기 전용, 계획, 실행 모드 선택
/project               현재 workspace 확인
/skills                설치된 skill 확인
/learning status       self-learning 상태 확인
/quit                  종료
```

선택 사항인 Web UI는 다음으로 실행합니다:

```bash
kyrozen-web
```

기본 주소는 http://localhost:8000입니다. 특정 프로젝트를 사용하려면 kyrozen --project /path/to/project 또는 kyrozen-web --project /path/to/project를 사용합니다. 일반 kyrozen은 ~/.kyrozen/workspace 전역 workspace를 사용합니다.

## OpenKyrozen 핵심 기능

- 파일, Shell, Git, 웹 검색, 브라우저 세션, 프로젝트 그래프, GitHub를 같은 workspace에서 사용할 수 있습니다.
- Ask와 Plan은 읽기 및 네트워크 작업만 허용합니다. Agent는 승인된 계획이나 명시적으로 요청된 작업을 실행하며 capability와 approval 제한을 따릅니다.
- 18개 주요 모델 provider, 모델 기본값, fallback을 지원합니다.
- SQLite가 session, event, task, claim, learning state의 권위 있는 저장소입니다. ChromaDB는 다시 만들 수 있는 선택적 index입니다.
- self-learning은 결과 증거를 기록하고 검증된 개선만 승격합니다. 권한이나 모델 가중치를 몰래 변경하지 않습니다.
- Jev Decision은 선택 가능한 판단 계층입니다. 요청 라우팅, 확인 질문, 학습 증거, 메모리 관련성, 의심스러운 도구 출력을 검사하며 도구를 실행하거나 승인하지 않고 필요하면 기권합니다.

현재 런타임은 **도구 46개**를 제공하며, 그중 **Git 도구는 14개**입니다. 도구, endpoint, MCP schema의 기준은 [생성된 런타임 인벤토리](docs/tool-inventory.md)입니다.

## 문서

| 내용 | 문서 |
| --- | --- |
| 명령, 모드, workspace | [사용 가이드](docs/usage.md) |
| 런타임 흐름과 안전 경계 | [아키텍처](docs/architecture.md) |
| provider, 환경 변수, 상태 | [설정](docs/configuration.md) |
| Web, REST, MCP | [API 가이드](docs/api.md) |
| 자동 하위 에이전트, 독립 컨텍스트, 상호 검증 | [하위 에이전트 가이드](docs/subagents.md) |
| self-learning, memory, evidence | [Self-learning 가이드](docs/self-evolution.md) |
| Jev Decision 및 교정 증거 | [Jev Decision](docs/decision-assist-validation.md) · [System One](docs/system-one-benchmark.md) |
| 다른 오픈소스 에이전트와 비교 | [비교](docs/comparison.md) |
| 개발, 테스트, 릴리스 | [개발 가이드](docs/development.md) |

DeepSeek 기본 예시는 "model_simple": "deepseek-flash" 및 "model_complex": "deepseek-v4-pro"입니다.

## 보안과 개발

provider credential은 환경 변수 또는 암호화된 ~/.kyrozen_config.json에 저장하세요. Web 서버를 localhost 외부에 공개할 경우 KYROZEN_SERVER_TOKEN을 설정하고 capability와 approval 설정을 확인하세요.

```bash
make check
make docs-check
make test
make lint
```

자세한 내용은 AGENTS.md, [설정 및 보안](docs/configuration.md), [개발 가이드](docs/development.md)를 확인하세요.

## 라이선스

OpenKyrozen은 [MIT License](LICENSE)로 배포됩니다.

<p align="center"><sub><a href="README.md">English</a> · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a> · 한국어</sub></p>
