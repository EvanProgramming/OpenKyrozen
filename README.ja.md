<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-blue?logo=python" alt="Python">
  <img src="https://img.shields.io/badge/DeepSeek%20%7C%20OpenAI%20%7C%20Claude%20%7C%20Gemini%20%7C%20Ollama-API-green?logo=openai" alt="Multi-Provider">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-brightgreen" alt="License">
  <img src="https://img.shields.io/badge/CI-passing-brightgreen" alt="CI">
</p>

<h1 align="center">✨ OpenKyrozen ✨</h1>
<p align="center"><strong>自己学習型 AI エージェント — DeepSeek · OpenAI · Claude · Gemini · Ollama</strong></p>
<p align="center">ターミナルネイティブな完全自律型 AI エージェント。<em>あらゆる対話から学習</em>し、<br>ファイルシステム操作、Git 管理、バグ修正、そして継続的な自己進化を実現します。</p>

<p align="center">
  🌐 <a href="README.md">English</a> ·
  <a href="README.zh-CN.md">简体中文</a> ·
  <strong>日本語</strong> ·
  <a href="README.ko.md">한국어</a>
</p>

---

## 📑 目次

- [OpenKyrozen とは？](#openkyrozen-とは)
- [🚀 インストール](#-インストール)
  - [前提条件](#前提条件)
  - [方法 A：ソースからインストール](#方法-aソースからインストール推奨)
  - [方法 B：ローカルディレクトリから pip インストール](#方法-bローカルディレクトリから-pip-インストール)
- [📖 使い方ガイド](#-使い方ガイド)
  - [ターミナルモード](#ターミナルモード)
  - [チャット内コマンド](#チャット内コマンド)
  - [Web UI モード](#web-ui-モード)
- [🏗 アーキテクチャ](#-アーキテクチャ)
  - [タスク複雑度ルーティング](#タスク複雑度ルーティング)
  - [モデル自動選択](#モデル自動選択)
  - [プロバイダー管理](#プロバイダー管理)
- [🛠 ツールリファレンス](#-ツールリファレンス)
  - [ファイルとシステム](#ファイルとシステム)
  - [Web](#web)
  - [Git（15 ツール）](#git15-ツール)
  - [メモリ](#メモリ)
- [🧠 専用ワークフロー](#-専用ワークフロー)
  - [バグ修正](#バグ修正6ステッププロトコル)
  - [Git 操作](#git-操作安全第一)
  - [複雑なタスク](#複雑なタスク決して途中で止まらない)
- [🧬 自己学習システム](#-自己学習システム)
- [🌐 Web UI と REST API](#-web-ui-と-rest-api)
- [🔌 プラグインシステム](#-プラグインシステム)
- [🔐 セキュリティ](#-セキュリティ)
- [⚙️ 設定リファレンス](#️-設定リファレンス)
- [🔧 開発](#-開発)
- [📁 プロジェクト構造](#-プロジェクト構造)
- [🙏 巨人の肩の上に立って](#-巨人の肩の上に立って)
- [📄 ライセンス](#-ライセンス)

---

## OpenKyrozen とは？

OpenKyrozen はターミナルで動作する**自己学習型 AI エージェント**です。一般的なチャットボットとは異なり、以下のことが可能です：

- **26 種類の組み込みツール** — ファイルの読み書き、シェルコマンドの実行、Web 検索、Git リポジトリの管理
- **継続的な学習** — 20 の機能を有界 dispatcher で実行し、事実の抽出、スキルの発明、戦略の最適化を記録します
- **あらゆる LLM に対応** — DeepSeek、OpenAI、Claude、Gemini、またはローカルの Ollama モデル
- **クロスプラットフォーム** — macOS、Linux、Windows（端末機能の自動検出付き）
- **Web UI を内蔵** — ブラウザベースのチャットインターフェースと REST API による統合

使うたびに賢くなる AI のパートナーだと考えてください。

---

## 🚀 インストール

### 前提条件

- **Python 3.12 または 3.13**（Python 3.14+ には OpenAI SDK との既知のインポート問題があります）
- 対応プロバイダーの API キー：

| プロバイダー | キーの取得 | コスト |
|-------------|-----------|------|
| **DeepSeek** | [platform.deepseek.com](https://platform.deepseek.com) | ~$0.27/100万入力トークン |
| **OpenAI** | [platform.openai.com](https://platform.openai.com) | ~$2.50/100万入力トークン |
| **Anthropic (Claude)** | [console.anthropic.com](https://console.anthropic.com) | ~$3.00/100万入力トークン |
| **Google (Gemini)** | [aistudio.google.com](https://aistudio.google.com) | ~$0.15/100万入力トークン |
| **Ollama** | [ollama.com](https://ollama.com) | 無料（ローカル実行） |

### 方法 A：ソースからインストール（推奨）

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

### 方法 B：ローカルディレクトリから pip インストール

```bash
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
pip install .

# インストール後はどこからでも実行可能：
kyrozen          # ターミナルエージェント
kyrozen-web      # Web サーバー
```

> **注意：** PyPI からの `pip install openkyrozen` は近日公開予定です。現在はローカルディレクトリからインストールするか、リポジトリをクローンしてください。

初回起動時に API キーの入力を求められます。エージェントは自動的にプロバイダーを検出し、暗号化されたキーを `~/.kyrozen_config.json` に保存します。

---

## 📖 使い方ガイド

### ターミナルモード

起動するとバナーと `You:` プロンプトが表示されます。自然に入力してください——エージェントは英語、中国語、日本語、韓国語を理解します。

```text
You: README を読んで、このプロジェクトの概要を教えて
You: "Hello World" を表示する hello.py を作成して
You: Python の最新リリース日を Web 検索して
You: main.py の200行目付近のバグを修正して
You: すべての変更を適切なメッセージでコミットして
```

Kyrozen は：
1. リクエストを分類（簡単 / 中程度 / 複雑）
2. タスクに最適なモデルを選択
3. 必要に応じて計画を作成
4. ツールを段階的に実行
5. ライブタスクパネルで進捗を表示
6. 完了した作業をサマリー

### チャット内コマンド

| コマンド | 機能 |
|---------|------|
| `/quit` または `/exit` | エージェントを終了 |
| `/provider` | LLM プロバイダーを切り替え（対話型メニュー） |
| `/api_key` | API キーを変更 |
| `/learn` | プロジェクトファイルを即座にメモリにスキャン |
| `/forget` | 最近の学習を表示；`/forget キーワード` で誤った学習を削除 |
| `/update` | Git から最新バージョンをプル |
| `/self-learning` | 個別の自己学習機能をオン/オフ |

### Web UI モード

```bash
python server.py --port 8000
# http://localhost:8000 を開く

# または Docker 経由：
docker build -t openkyrozen .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-... \
  -e KYROZEN_SERVER_TOKEN=change-me \
  -v kyrozen-data:/data \
  openkyrozen
```

Web インターフェースは、リアルタイムストリーミング、コスト追跡、セッション管理を備えたダークテーマのチャット UI を提供します。

---

## 🏗 アーキテクチャ

```
ユーザー入力
    │
    ▼
┌─────────────────┐
│  タスク分類器   │──► 簡単 / 中程度 / 複雑
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  モデル選択器   │──► deepseek-chat / deepseek-reasoner / gpt-4o / claude / gemini / llama
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   LLM プロバイダー │──► 5 つのバックエンド、自動フォールバックチェーン付き
└────────┬────────┘
         │  応答 + ツール呼び出し
         ▼
┌─────────────────┐
│  ツール実行器   │──► 26 種類の組み込みツール（ファイル I/O、シェル、Git、Web、メモリ）
└────────┬────────┘
         │  ツール結果を LLM にフィードバック
         │  （1ターン最大50回のツール呼び出し）
         ▼
┌─────────────────┐
│     応答出力    │──► ユーザーに回答 + タスクサマリーを表示
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   自己学習システム │──► バックグラウンド：事実の抽出、メモリのスコアリング、知識グラフの構築
└─────────────────┘
```

### タスク複雑度ルーティング

Kyrozen はすべてのリクエストを自動分類し、動作を適応させます：

| レベル | トリガー例 | エージェントの動作 |
|-------|-----------|-----------------|
| **簡単** | "こんにちは"、"Python とは"、"ありがとう" | 直接返答、計画オーバーヘッドゼロ |
| **中程度** | "ファイルを一覧表示して README を読んで" | 番号付き計画を作成、ツールを順次実行 |
| **複雑** | "このリポジトリを監査して"、"バグを修正してコミットして"、"Web アプリを構築して" | 完全な計画 → タスクリスト → 進捗追跡 → 途中で止まらない |

### モデル自動選択

エージェントは簡単なタスクと複雑なタスクで異なるモデルを選択します。これらは上書き可能です：

```bash
export KYROZEN_MODEL_SIMPLE=deepseek-chat
export KYROZEN_MODEL_COMPLEX=deepseek-reasoner
```

| プロバイダー | 簡単なタスク（デフォルト） | 複雑なタスク（デフォルト） |
|-------------|------------------------|--------------------------|
| DeepSeek | `deepseek-chat` | `deepseek-reasoner` |
| OpenAI | `gpt-4o` | `gpt-4o` |
| Anthropic | `claude-sonnet-4-20250514` | `claude-sonnet-4-20250514` |
| Google | `gemini-2.5-flash` | `gemini-2.5-pro` |
| Ollama | `llama3.2` | `llama3.2` |

### プロバイダー管理

いつでもプロバイダーを切り替え可能——チャット内で `/provider` を使用するか、環境変数で：

```bash
export KYROZEN_PROVIDER=anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python main.py
```

プライマリプロバイダーが失敗した場合、Kyrozen は自動的にフォールバックチェーン（例：DeepSeek → OpenAI → Claude）で切り替えます。レート制限エラー（HTTP 429）はジッター付き指数バックオフをトリガーします。

---

## 🛠 ツールリファレンス

すべての 26 ツールは、JSON アクションブロック内でプレーン文字列の `args` フィールドを受け付けます：

```json
{"action": "read_file", "args": "README.md"}
```

短縮エイリアスも有効です——`bash`、`cmd`、`sh` → `run_cmd`；`status`、`diff`、`log` → `git_status` など。

### ファイルとシステム

| ツール | 説明 | 例 |
|------|------|------|
| `read_file` | ファイルの内容を読み取り | `"README.md"` |
| `write_file` | ファイルを作成または上書き | `"path|content"` |
| `list_dir` | ディレクトリの内容を一覧表示 | `"."` |
| `list_tree` | 再帰的なディレクトリツリー | `"src/"` |
| `find_files` | グロブベースのファイル検索 | `"*.py|."` |
| `run_cmd` | シェルコマンドを実行 | `"python --version"` |

### Web

| ツール | 説明 | 例 |
|------|------|------|
| `search_web` | インターネット検索（Google → DDG → Wikipedia） | `"最新の Python リリース"` |
| `read_webpage` | URL のテキストコンテンツを取得 | `"https://example.com"` |

### Git（15 ツール）

| ツール | 機能 |
|------|------|
| `git_status` | ワーキングツリーの状態を表示 |
| `git_diff` | アンステージド / ステージド / コミット間の差分 |
| `git_log` | コミット履歴（`--oneline --decorate`） |
| `git_branch` | ブランチの一覧表示 / 作成 / 削除 |
| `git_add` | コミット用にファイルをステージ |
| `git_commit` | メッセージ付きでコミット |
| `git_push` / `git_pull` | リモート同期 |
| `git_checkout` | ブランチの切り替えまたはファイルの復元 |
| `git_stash` | 作業中の変更をスタッシュ / ポップ / 一覧表示 |
| `git_reset` | HEAD をリセット（`--soft` は安全、`--hard` は警告） |
| `git_show` | コミットの詳細を `--stat` で表示 |
| `git_remote` | リモートの一覧表示 / 追加 / 削除 |
| `git_clone` | リポジトリをクローン |
| `analyze_remote_repo` | クローン + 全ファイル読み取り → 構造化サマリー |

### メモリ

| ツール | 説明 |
|------|------|
| `search_memory` | 保存された知識の意味検索 |
| `check_stored_data` | メモリ統計と最近の事実 |

---

## 🧠 専用ワークフロー

### バグ修正（6ステッププロトコル）

エラーやトレースバックを貼り付けると、Kyrozen は自動的に起動します：

1. **再現** — 問題のコードを読み取り、失敗したコマンドを再実行
2. **診断** — トレースバックを解析し、根本原因を特定
3. **仮説** — 変更を行う前に修正案を明示
4. **修正** — 最小限のコード変更を適用
5. **検証** — 失敗したコマンドを再実行；失敗したらステップ2に戻る
6. **説明** — 何が問題だったか、何を変更したか、その理由を説明

修正後、Kyrozen は結果を追跡します。「ありがとう、動いたよ」と言えば成功を記録。「まだ壊れてる」と言えば失敗を記録し、より深い分析をトリガーします。

### Git 操作（安全第一）

- 常に最初に `git_status` を実行
- コミット前に `git_diff` を確認
- 規約に基づいたコミットプレフィックスを使用：`fix:`、`feat:`、`refactor:`、`chore:`
- 明示的なリクエストなしに強制プッシュしない
- ブランチ切り替え前に未コミットの変更を自動スタッシュ
- `git reset --hard` の前に警告

### 複雑なタスク（決して途中で止まらない）

マルチステップの作業（リファクタリング、プロジェクトジェネレーター、コードベース監査）の場合：

- リクエストを検証可能なサブタスクに分解
- 番号付き計画を作成
- 各計画ステップに対応する JSON タスクリストを構築
- `TaskDone` マーカーで進捗を追跡
- 完了時に自動サマリーを生成

---

## 🧬 自己学習システム

これが Kyrozen を特別なものにしています。**20 の自己学習機能**を共通のレジストリで管理し、各機能を独立したフラグ付きの有界単位として実行します。実際に状態が変わったかどうかも SQLite イベントに記録します。

### 仕組み

CLI はアイドル時に 30 秒ごとに最大 4 機能をラウンドロビンで実行します。Web/Gateway は永続化された `learning_cycle` ジョブを使い、チャットターンでは入力依存の好み検出と技術検出も実行します。すべての機能は `/self-learning` で個別に切り替えられ、`GET /api/v2/learning/features` で最新状態を確認できます。入力や証拠がない場合は、変更なしの有界な no-op として記録されます。

| # | 機能 | 学習内容 |
|---|------|---------|
| 1 | **会話学習** | チャットから事実、好み、パターンを抽出 |
| 2 | **プロジェクトファイルスキャン** | すべての `.py` ファイルをコンテキスト用にメモリに読み込み |
| 3 | **古いエントリのエージング** | 存在しなくなったファイルに関する事実を削除 |
| 4 | **ツール自動デバッグ** | ツールの失敗を分析し、根本原因を特定 |
| 5 | **メモリ統合** | 保存された事実の重複排除と要約 |
| 6 | **ツールレビュー** | 使用頻度の低いツールの削除を提案 |
| 7 | **ターゲット調査** | 文書化されていない関数を見つけ、その目的を推測 |
| 8 | **アイドル時リフレクション** | 複雑なタスクの後に何がうまくいったかを振り返り |
| 9 | **戦略の蒸留** | トークン使用量が多い場合に効率化のヒントを抽出 |
| 10 | **新技術自動パッチ** | 未知のライブラリが言及されたときに Web 検索 |
| 11 | **スキル発明** | 過去の成功から再利用可能なスキルテンプレートを作成 |
| 12 | **コンテキスト圧縮** | コンテキストが30K文字を超えたら古いターンを要約 |
| 13 | **修正検証** | バグ修正の成功率を経時的に追跡 |
| 14 | **動的ツール作成** | `DefineTool:` 構文でエージェントが新しいツールを構築 |
| 15 | **ユーザー好みモデル** | コーディングスタイル、好みの言語、詳細度を検出 |
| 16 | **自律検査** | 古いパッケージ、コードスメル、gitignore の欠落をチェック |
| 17 | **メモリ重要度スコアリング** | エントリを0-10で評価；高スコアのエントリが優先 |
| 18 | **知識グラフ** | 保存された事実からエンティティ→関係マップを構築 |
| 19 | **スキル合成** | 複数の学習済みスキルをワークフローに連鎖 |
| 20 | **誤学習ロールバック** | `/forget` コマンドで誤った学習を削除 |

### メモリストレージ

長期メモリには **ChromaDB**（ベクトルデータベース、`chroma_memory/` に保存）を使用します。ChromaDB が利用できない場合はインメモリストレージにフォールバックします。メモリは意味検索が可能で——エージェントは数週間前の関連事実を思い出すことができます。

---

## 🌐 Web UI と REST API

```bash
pip install fastapi uvicorn
python server.py --port 8000
# http://localhost:8000 を開く
```

### REST API エンドポイント

| メソッド | エンドポイント | 説明 |
|--------|-------------|------|
| `GET` | `/` | ダークテーマのチャット Web UI |
| `POST` | `/api/chat` | メッセージを送信し JSON レスポンスを取得 |
| `POST` | `/api/chat/stream` | SSE ストリーミングチャット |
| `GET` | `/api/memory?q=キーワード` | 保存されたメモリを検索 |
| `GET` | `/api/v2/learning/features` | 20 機能のレジストリと最新実行状態 |
| `GET` | `/api/cost` | トークン使用量とコストサマリー |
| `GET` | `/api/health` | プロバイダー状態 + メモリ数 |
| `GET` | `/api/voice/speak?text=...` | システム TTS でテキスト読み上げ |
| `POST` | `/api/voice/transcribe` | 音声テキスト変換（パススルー） |
| `POST` | `/api/webhooks/register` | Webhook URL を登録 |
| `GET` | `/api/webhooks` | 登録済み Webhook を一覧表示 |
| `POST` | `/api/webhooks/test` | テスト Webhook を発火 |
| `POST` | `/mcp` | モデルコンテキストプロトコル（JSON-RPC 2.0） |

### Docker デプロイ

```bash
docker build -t openkyrozen .
docker run -p 8000:8000 \
  -e DEEPSEEK_API_KEY=sk-your-key \
  -e KYROZEN_SERVER_TOKEN=change-me \
  -e KYROZEN_DB_PATH=/data/openkyrozen.sqlite3 \
  -v kyrozen-data:/data \
  openkyrozen
```

イメージは非 root ユーザー `kyrozen` で実行されます。SQLite の事実上の保存先は `/data/openkyrozen.sqlite3`（イメージが `KYROZEN_DB_PATH` に設定）です。コンテナを置き換える場合も同じ名前付きボリュームを `/data` にマウントしてください。ローカルでは `make docker-smoke` で置き換え後の復元テストを実行できます。

---

## 🔌 プラグインシステム

`plugins/` ディレクトリに `register()` 関数を持つ `.py` ファイルを作成します：

```python
# plugins/my_plugin.py
class MyPlugin:
    def on_startup(self, agent=None, **kwargs):
        print("プラグインがロードされました！")

    def on_turn_start(self, user_input, **kwargs):
        print(f"ユーザーの発言：{user_input[:50]}")

    def on_tool_execute(self, action, args, result, **kwargs):
        print(f"ツール {action}({args[:30]}) → {result[:30]}")

def register():
    return MyPlugin()
```

利用可能なフック：`on_startup`、`on_turn_start`、`on_turn_end`、`on_tool_execute`。

動作例は `plugins/turn_logger.py` を参照してください。

---

## 🔐 セキュリティ

| 機能 | 保護内容 |
|------|---------|
| **危険コマンドフィルター** | `rm -rf`、`mkfs`、フォークボム、Windows の破壊的コマンドをブロック |
| **API キー暗号化** | `~/.kyrozen_config.json` を静的暗号化（XOR + マシン派生 SHA-256 キー） |
| **プロンプトインジェクション保護** | 9 種類の一般的なインジェクションパターンを検出してフィルタリング |
| **サンドボックス実行** | ファイル操作をワークスペース境界内に制限 |
| **Git 安全性** | 強制プッシュなし、ハードリセット前に警告 |
| **監査ログ** | すべてのチャット/API イベントをタイムスタンプ付きで `kyrozen_audit.log` に記録 |
| **Python バージョンガード** | Python 3.14+ での起動を拒否 |
| **ツール失敗メモリ** | 過去の失敗を記憶し、繰り返しを回避 |

---

## ⚙️ 設定リファレンス

### 環境変数

| 変数 | 説明 | デフォルト |
|------|------|----------|
| `KYROZEN_PROVIDER` | LLM プロバイダー | `deepseek` |
| `DEEPSEEK_API_KEY` | DeepSeek API キー | — |
| `OPENAI_API_KEY` | OpenAI API キー | — |
| `ANTHROPIC_API_KEY` | Anthropic API キー | — |
| `GEMINI_API_KEY` | Google Gemini API キー | — |
| `KYROZEN_API_KEY` | 汎用 API キー（プロバイダー固有キーを上書き） | — |
| `KYROZEN_MODEL_SIMPLE` | 簡単/中程度タスク用モデル | プロバイダーデフォルト |
| `KYROZEN_MODEL_COMPLEX` | 複雑タスク用モデル | プロバイダーデフォルト |
| `KYROZEN_BASE_URL` | カスタム API ベース URL | プロバイダーデフォルト |

### 設定ファイル（`~/.kyrozen_config.json`）

```json
{
  "provider": "deepseek",
  "api_key": "<暗号化>",
  "model_simple": "deepseek-chat",
  "model_complex": "deepseek-reasoner",
  "encrypted": true
}
```

ファイルは自動管理されます。チャット内で `/provider` または `/api_key` を使用して対話的に更新できます。

---

## 🔧 開発

```bash
# クイック検証
make check

# 構文チェックのみ
make lint

# デバッグモード（フォーマットエラートラップ）
make debug

# 初回 API キー設定
make init

# Python バージョン変更後に venv を再構築
make reinstall

# Web サーバーを起動
make web

# Git ヘルパー
make git-status
make git-log
make commit msg='feat: 説明'
make push
```

### CI/CD

GitHub Actions がプッシュと PR ごとに自動実行：
- Python 3.12 および 3.13 での構文チェック
- ツールインベントリ検証
- プロバイダーインポートチェック
- Docker ビルド検証

### pip パッケージ

```bash
# ローカルディレクトリからインストール（PyPI 公開は近日予定）
pip install .                   # コア + CLI
pip install '.[web]'            # + Web UI
pip install '.[all]'            # + Claude + Gemini + Web
```

---

## 📁 プロジェクト構造

```
OpenKyrozen/
├── main.py              # コアエージェントループ、自己学習、チャットターンロジック
├── tools.py             # 26 種類の組み込みツール（ファイル、シェル、Git、Web）
├── providers.py         # マルチ LLM 抽象化（5 プロバイダー + フォールバック）
├── memory.py            # ChromaDB ベースのベクトルメモリ
├── server.py            # FastAPI Web サーバー + REST API + チャット UI
├── pyproject.toml       # pip パッケージ設定
├── Dockerfile           # Docker イメージ定義
├── Makefile             # ビルド自動化（macOS/Linux）
├── setup.bat / run.bat  # Windows バッチスクリプト
├── plugins/             # プラグインディレクトリ（フックベース）
├── prompts/             # プロンプトテンプレート（役割、指示、例）
├── chroma_memory/       # ChromaDB 永続ストレージ（自動作成）
└── .github/workflows/   # CI/CD パイプライン
```

---

## 🙏 巨人の肩の上に立って

OpenKyrozen は優れたオープンソースプロジェクトの上に構築されています。すべてのメンテナーと貢献者に感謝します。

| プロジェクト | リポジトリ | 用途 |
|-------------|----------|------|
| **Aider** | [paul-gauthier/aider](https://github.com/paul-gauthier/aider) | マルチターンエージェントループ、ツール呼び出しパターン、Git 安全規則に着想 |
| **CodeWhale** | [deepseek-ai/codewhale](https://github.com/deepseek-ai/codewhale) | エージェントランタイムアーキテクチャ、サブエージェント委任、検証規律 |
| **Chroma** | [chroma-core/chroma](https://github.com/chroma-core/chroma) | 長期メモリと意味検索を支えるベクトルデータベース |
| **FastAPI** | [fastapi/fastapi](https://github.com/fastapi/fastapi) | Web サーバー、REST API、リアルタイムストリーミングエンドポイント |
| **Rich** | [Textualize/rich](https://github.com/Textualize/rich) | ターミナル UI — パネル、プログレスバー、シンタックスハイライト、ライブ表示 |
| **OpenAI Python** | [openai/openai-python](https://github.com/openai/openai-python) | DeepSeek、OpenAI、Ollama プロバイダー向け統一 API クライアント |
| **Uvicorn** | [encode/uvicorn](https://github.com/encode/uvicorn) | 本番 Web デプロイ用 ASGI サーバー |
| **googlesearch-python** | [Nv7-GitHub/googlesearch](https://github.com/Nv7-GitHub/googlesearch) | DuckDuckGo が利用不可時の Web 検索フォールバック |

> *「私がより遠くを見渡せたとしたら、それは巨人の肩の上に立っていたからです。」* — アイザック・ニュートン

---

## 📄 ライセンス

MIT ライセンス。詳細は `LICENSE` ファイルを参照してください。

---

<p align="center">
  <sub>学ぶ AI を求める開発者のために ❤️ を込めて</sub>
</p>
