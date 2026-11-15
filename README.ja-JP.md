# Personal AI Agent — Coder PoC

[English](README.md) | 日本語

**ユーザーごとに専用のワークスペースで常時稼働するパーソナル AI エージェント**の概念実証（PoC）です。[Coder](https://coder.com/) がユーザーごとに Docker コンテナを用意し、その中で FastAPI アプリが動きます。各ユーザーは**自分の Azure OpenAI API キー**を Coder のワークスペースパラメーターに入力し、Realtime モデル（既定 `gpt-realtime-2.1-mini`）を使って **Web チャット**と**音声チャット**の両方で会話できます。会話履歴（テキスト・音声）と定期ジョブは永続ボリューム上の SQLite に保存され、テキストと音声で同じ文脈を共有します。

> [!WARNING]
> これは PoC であり、そのまま本番導入できるエンタープライズシステム**ではありません**。検証以外で使う前に「[本番運用に向けた課題とセキュリティ](#本番運用に向けた課題とセキュリティ)」を確認してください。

## 機能

| 機能 | 状態 |
| --- | --- |
| Web チャット（Realtime モデルにサーバー側 WebSocket で接続、テキスト出力） | ✅ 実装済み |
| 音声チャット（ブラウザ ⇔ Azure を WebRTC で接続。SDP はバックエンドが中継） | ✅ 実装済み |
| ユーザーごとの API キー（Coder ワークスペースパラメーター、入力マスク） | ✅ 実装済み |
| テキスト・音声・ジョブ結果の共通履歴（直近の履歴を指示文に含めて文脈を共有） | ✅ 実装済み |
| 毎日指定した時・分に指示を実行する定期ジョブ（APScheduler の cron） | ✅ 実装済み |
| 常時稼働（tini、自動再起動、ヘルスチェック、Coder エージェントの組み込み） | ✅ 実装済み（自動停止の無効化はテンプレート設定で行います） |
| SQLite 永続化（`/data/agent.db`） | ✅ 実装済み |
| 自然言語からのスケジュール作成 | ⏳ 予定 |
| Web 検索（Realtime の関数ツール → Responses API の `web_search`、出典付き）。テキスト・音声・定期ジョブで利用可 | ✅ 実装済み |
| Agent Skills のインポート・エクスポート・管理（一覧、有効化/無効化、SKILL.md の編集、新規作成、削除）とエージェントからの利用 | ✅ 実装済み |
| MCP ツール（メール送信など） | ⏳ 予定 |

`WEB_SEARCH_DEPLOYMENT` を設定すると、モデルは最新情報が必要な質問で `web_search` ツールを呼びます。バックエンドが同じ API キーで、Responses API の Web 検索ツールに対応したチャットモデル（例：`gpt-6.1-terra`）に検索させ、要点と出典をモデルに返します。音声チャットでは、ブラウザが受け取ったツール呼び出しを `/api/tools/call` で実行します。

## チャットと表示

- **チャット**：左ペイン（狭い画面では下部）に一覧を表示します。「＋ 新規チャット」で新しいチャットを始め、クリックで過去のチャットを開き、🗑 で削除します。名前は最初のメッセージから自動で付きます。モデルの文脈に入るのは、そのチャットの履歴だけです。音声で話した内容は、通話を始めたチャットに保存されます。定期ジョブの結果は「⏰ 定期ジョブの結果」に入ります。
- **既存データの移行**：チャット機能より前の履歴は、初回起動時に「これまでの会話」という 1 つのチャットにまとめます。
- **リッチ表示**：エージェントの返答は Markdown（表、見出し、リンクなど）で表示します（[marked](https://marked.js.org/) と [DOMPurify](https://github.com/cure53/DOMPurify) を cdnjs から読み込み。読み込めない場合はプレーンテキスト）。
- **インフォグラフィック**：テキストチャットで図解を頼むと、モデルは ```` ```html ```` ブロックで自己完結した HTML／SVG を返し、画面がそれを図として描画します。描画は `sandbox` 付き iframe（スクリプト、フォーム、ポップアップ、遷移は不可）で行い、CSP ですべての外部読み込みを禁止しています。音声では図は出さず、口頭で要点を伝えます。

## Agent Skills

[Agent Skills](https://agentskills.io) 形式（`SKILL.md` を含むフォルダ）のスキルを、Web UI の「スキル」セクションで管理できます。

- **保存先**：`/data/skills/<name>/`（永続ボリューム）。有効/無効の状態は `/data/skills/.state.json` に保存します。
- **インポート**：スキルフォルダの `.zip` / `.skill`（1 つの zip に複数スキルも可）、または `SKILL.md` 単体。同名のスキルは「同名は上書き」にチェックしたときだけ置き換えます。
- **エクスポート**：スキルごと、またはすべてを zip（`<name>/SKILL.md ...` の形）でダウンロード。他の Agent Skills 対応ツールでもそのまま使えます。
- **既定スキル**：Anthropic の [skill-creator](https://github.com/anthropics/skills/tree/main/skills/skill-creator)（Apache-2.0、改変なし）を同梱し、初回起動時に `/data/skills` へ配置します。一度配置したら、無効化・編集・削除しても再配置しません。スクリプトやサブエージェント、評価ループはこの環境では実行できないため、エージェントはそれらの手順を省き、保存ツールで保存します。取得元のコミットは [app/default_skills/README.md](app/default_skills/README.md) に記録しています。
- **再読み込み**：「⟳ 再読み込み」ボタンで、保存先の最新状態を一覧に反映します（ターミナルでの直接編集や、別タブ・音声での保存を反映したいとき）。
- **チャットでの作成・修正**：「〇〇のスキルを作って」と頼むと、エージェントが SKILL.md を組み立てて `save_skill` で保存します。参照資料は `write_skill_file` で `references/` などに書き込みます。既存スキルを直すときは `load_skill` で読んでから更新します。エージェントはスキルを削除できません（削除は画面から行います）。
- **エージェントでの利用**：有効なスキルの `name` と `description` だけを指示文に入れます。依頼が説明に当てはまると、モデルが `load_skill`（SKILL.md と同梱ファイル一覧）と `read_skill_file`（参照ファイル）を呼びます。テキスト・音声・定期ジョブのどれでも使えます。
- **制限**：スキル内のスクリプトは**実行しません**（読み取りのみ）。インポート時は、上位フォルダへのパス、絶対パス、シンボリックリンク、大きすぎる zip（20MB 超、展開後 50MB 超、300 ファイル超、1 ファイル 5MB 超）を拒否します。`name` は英小文字・数字・ハイフン（64 文字以内）、`description` は必須（1024 文字以内）です。

## アーキテクチャ

```
ブラウザ ──HTTPS──► Coder（Entra ID 認証、アプリプロキシ、share=owner）
   │                    │
   │                    ▼
   │          ワークスペースコンテナ（ユーザーごとに 1 つ、restart=unless-stopped）
   │          ┌──────────────────────────────────────────────────────────┐
   │          │ tini ─┬─ Coder エージェント（バックグラウンド、自動再起動） │
   │          │       └─ FastAPI :8000                                    │
   │          │            ├─ Web チャット ──WebSocket(api-key)──► Azure OpenAI Realtime
   │          │            ├─ 音声: SDP 中継 ─client_secrets→calls──► 〃
   │          │            ├─ APScheduler（定期ジョブ）──WebSocket──► 〃
   │          │            └─ SQLite（/data ボリューム）              │
   │          └──────────────────────────────────────────────────────────┘
   └────────── 音声メディア（WebRTC, UDP/TCP）──────────────────────────► Azure OpenAI Realtime
```

- **API キーはブラウザに渡りません。** 音声チャットでは、ブラウザが作った SDP offer をバックエンドが受け取ります。バックエンドが個人キーで短命のクライアントシークレット（`/openai/v1/realtime/client_secrets`）を発行し、それを使って Azure の `/openai/v1/realtime/calls` と SDP を交換します。ブラウザに返すのは SDP answer だけです。
- 音声の書き起こし（ユーザー発話とエージェント応答）はデータチャネルのイベントから取り出し、`/api/transcripts` で履歴に保存します。

## リポジトリ構成

```
.
├── app/main.py           # FastAPI：API、Realtime 接続（WebSocket / WebRTC 中継）、ツール、スケジューラー、SQLite
├── app/skills.py         # Agent Skills の保存、インポート／エクスポート、検証、既定スキルの配置、モデル用ツール
├── app/default_skills/   # 既定スキル（Anthropic の skill-creator、Apache-2.0）
├── app/static/index.html # Web UI（チャット、音声、スキル、定期ジョブ。広い画面では左ペインにスキルと定期ジョブ）
├── entrypoint.sh         # Coder エージェント（任意）をバックグラウンドで起動し、uvicorn を前面で実行
├── Dockerfile            # python:3.12-slim + tini + curl、非 root（UID 10001）、HEALTHCHECK
├── requirements.txt      # fastapi, uvicorn, httpx, websockets, apscheduler, pydantic, PyYAML
└── terraform/main.tf     # Coder テンプレート（Docker プロバイダー）
```

## API

| メソッド | パス | 説明 |
| --- | --- | --- |
| `GET` | `/` | Web UI |
| `GET` | `/health` | ヘルスチェック（`{"ok": true}`） |
| `GET` | `/api/status` | 設定状態（キーの有無、デプロイ名、エンドポイントのホスト名。キーそのものは返しません） |
| `POST` | `/api/chat` | `{"message": "...", "conversation_id": "..."}` を送ると `{"answer": "...", "conversation_id": "..."}` が返る（`conversation_id` を省くと新しいチャットを作成） |
| `GET` / `POST` | `/api/conversations` | チャット一覧（更新順） / 新規作成 |
| `PATCH` / `DELETE` | `/api/conversations/{id}` | `{"title": "..."}` で名前を変更 / チャットとそのメッセージを削除 |
| `POST` | `/api/realtime/sdp?conversation_id=...` | 本文に SDP offer（`application/sdp`）を送ると SDP answer が返る |
| `POST` | `/api/tools/call` | 音声セッションのツール呼び出しを実行：`{"name": "web_search", "arguments": "{\"query\": \"...\"}"}` |
| `POST` | `/api/transcripts` | 音声の書き起こしを保存：`{"role": "user"\|"assistant", "content": "...", "conversation_id": "..."}` |
| `GET` | `/api/skills` | スキル一覧（名前、説明、有効/無効、ファイル数、エラー） |
| `POST` | `/api/skills` | `{"content": "<SKILL.md>"}` で新規作成 |
| `POST` | `/api/skills/import?filename=x.zip&overwrite=false` | 本文にファイル（.zip / .skill / SKILL.md）を送ってインポート |
| `GET` | `/api/skills/export` | すべてのスキルを zip でダウンロード |
| `GET` / `PUT` / `PATCH` / `DELETE` | `/api/skills/{name}` | 取得 / SKILL.md の更新 / `{"enabled": bool}` / 削除 |
| `GET` | `/api/skills/{name}/export` | スキルを zip でダウンロード |
| `GET` | `/api/skills/{name}/files/{path}` | 同梱ファイルの内容 |
| `GET` | `/api/skills/template?name=...` | 新規作成用の SKILL.md ひな形 |
| `GET` | `/api/history?conversation_id=...` | そのチャットの直近 200 件のメッセージ（`channel` は `text` / `voice` / `job`） |
| `POST` | `/api/jobs` | 毎日のジョブを登録：`{"hour": 7, "minute": 0, "prompt": "..."}` |
| `GET` | `/api/jobs` | 登録済みジョブの一覧 |
| `DELETE` | `/api/jobs/{id}` | ジョブを削除 |

## 設定（環境変数）

Coder で使う場合、主な値はワークスペースパラメーターから自動で設定されます（下記「Coder テンプレート」を参照）。

| 変数 | 必須 | 既定値 | 説明 |
| --- | --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | はい | — | 例：`https://YOUR-RESOURCE.openai.azure.com`（`/openai/v1` 付きでも可） |
| `AZURE_OPENAI_API_KEY` | はい | — | ユーザー個人の API キー |
| `AZURE_OPENAI_REALTIME_DEPLOYMENT` | いいえ | `gpt-realtime-2.1-mini` | Realtime モデルのデプロイ名（旧 `AZURE_OPENAI_DEPLOYMENT` も読みます） |
| `REALTIME_VOICE` | いいえ | `marin` | 音声の種類 |
| `REALTIME_TRANSCRIBE_MODEL` | いいえ | `whisper-1` | ユーザー発話の書き起こしモデル。空にすると書き起こしを無効化 |
| `REALTIME_TRANSCRIBE_LANGUAGE` | いいえ | `ja` | 書き起こしの言語 |
| `AZURE_OPENAI_REALTIME_WEBRTC_URL` | いいえ | `{endpoint}/openai/v1/realtime/calls?webrtc_filter=on` | WebRTC の SDP 交換先（リージョン別 URL を使う場合に上書き） |
| `WEB_SEARCH_DEPLOYMENT` | いいえ | 空（検索なし） | Web 検索に使うチャットモデルのデプロイ名（Responses API の `web_search` ツール対応。例：`gpt-6.1-terra`） |
| `AGENT_INSTRUCTIONS` | いいえ | 日本語アシスタント用の既定文 | システム指示 |
| `CONTEXT_MESSAGES` | いいえ | `20` | 指示文に含める直近履歴の件数 |
| `APP_TIMEZONE` | いいえ | `Asia/Tokyo` | ジョブのスケジュールとタイムスタンプのタイムゾーン |
| `DATA_DIR` | いいえ | `/data` | SQLite の保存先 |
| `CODER_AGENT_INIT_SCRIPT` | いいえ | — | Coder が設定。指定するとエントリーポイントが Coder エージェントを起動 |

## Azure 側の準備

1. Azure OpenAI（または Foundry）リソースに Realtime モデルをデプロイします。デプロイ名は既定で `gpt-realtime-2.1-mini` を想定しています。別の名前にした場合は、パラメーター「Realtime モデルのデプロイ名」で指定してください。モデルを提供しているリージョンかどうかを事前に確認してください。
2. ユーザーごとにキーを分ける場合は、ユーザー（または部署）ごとにリソースを用意し、そのキーとエンドポイントを各ユーザーに渡します。
3. 書き起こしに使う `whisper-1` などのモデルが使えるか確認します。使えない場合は `REALTIME_TRANSCRIBE_MODEL` を変えるか、空にしてください。空にすると、ユーザー発話は履歴に残りません（エージェントの応答は残ります）。

## ビルド

```sh
docker build -t personal-ai-agent:poc .
```

## ローカルでの動作確認（Coder なし）

```sh
docker volume create personal-agent-test
cat > .env <<'EOF'
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com
AZURE_OPENAI_API_KEY=YOUR-KEY
AZURE_OPENAI_REALTIME_DEPLOYMENT=gpt-realtime-2.1-mini
EOF
docker run --rm -p 127.0.0.1:8000:8000 -v personal-agent-test:/data --env-file .env personal-ai-agent:poc
```

http://localhost:8000 を開きます。`localhost` はブラウザが安全なコンテキストとして扱うため、マイクも使えます。`.env` は `.gitignore` 済みです。

## Coder テンプレート

1. イメージをビルドし、ワークスペースを動かす Docker デーモンから取得できるレジストリにプッシュします。
2. `terraform/` を新しいテンプレートとして登録します。

   ```sh
   coder templates push personal-agent -d terraform \
     --variable image=REGISTRY/personal-ai-agent:poc \
     --variable azure_openai_endpoint=https://YOUR-RESOURCE.openai.azure.com
   ```

   | テンプレート変数 | 既定値 | 説明 |
   | --- | --- | --- |
   | `image` | `personal-ai-agent:poc` | イメージ |
   | `azure_openai_endpoint` | 空 | エンドポイントの既定値（ユーザーが上書き可） |
   | `realtime_deployment` | `gpt-realtime-2.1-mini` | デプロイ名の既定値（ユーザーが上書き可） |
   | `web_search_deployment` | 空 | Web 検索モデルの既定値（ユーザーが上書き可。空なら検索なし） |
   | `app_subdomain` | `false` | `true` でサブドメイン方式のアプリにする（ワイルドカードのアクセス URL が必要） |

3. **常時稼働の設定。** テンプレートの自動停止を無効にします。

   ```sh
   coder templates edit personal-agent --default-ttl 0s
   # Premium の場合は、ユーザーによる自動停止の上書き・週次の強制停止・休止・休止後の自動削除も無効にします
   coder templates edit personal-agent --allow-user-autostop=false --autostop-requirement-weekdays none --dormancy-threshold 0s --dormancy-auto-deletion 0s
   ```

   フラグ名は Coder のバージョンで変わることがあるため、`coder templates edit --help` で確認してください。コンテナは `restart=unless-stopped` で、tini が PID 1 です。アプリが落ちると Docker が再起動し、Docker デーモンやホストが再起動したときもコンテナが自動で起動します。Coder エージェントは終了しても 5 秒後に再起動します。
4. **ユーザーの操作。** ワークスペースを作成すると、次のパラメーターの入力を求められます。後から「設定」で変更することもでき、変更するとワークスペースが再ビルドされます。
   - **Azure OpenAI API キー（個人用）**：必須。入力はマスクされます。
   - **Azure OpenAI エンドポイント**：既定値はテンプレート変数の値です。
   - **Realtime モデルのデプロイ名**、**Web 検索に使うモデルのデプロイ名**、**音声**
5. ワークスペースの「Personal AI Agent」アプリを開きます。アプリは `share = "owner"` なので、本人だけがアクセスできます。音声チャットには HTTPS（Coder のアクセス URL）が必要です。
6. ネットワーク要件：
   - コンテナから Azure へ HTTPS と WSS で接続できること。
   - ブラウザから Azure の WebRTC メディア（UDP、または TCP/443 フォールバック）に到達できること。
   - コンテナから Coder のアクセス URL に到達できること（エージェントのダウンロードと接続）。アクセス URL が `localhost` の場合は、`host.docker.internal` に置き換えて接続します。

## 本番運用に向けた課題とセキュリティ

- **API キーの保管場所。** 個人キーは Coder のワークスペースビルドパラメーターとして Coder の DB と Terraform state に保存され、コンテナの環境変数（`docker inspect` で見える）として渡されます。ワークスペースのオーナーと Coder 管理者が参照できます。本番では、Key Vault からの取得、Coder の外部認証、ユーザーごとのトークンブローカーなどを検討してください。
- **音声の認証情報。** ブラウザには短命のクライアントシークレットも渡しません。バックエンドがシークレットを発行し、SDP 交換もバックエンドが行います。
- **Realtime API の互換性。** この実装は Azure OpenAI Realtime の GA インターフェイス（`/openai/v1/realtime`、`client_secrets`、`calls`、`api-key` ヘッダー）を前提にしています。モックサーバーではテストしましたが、実際の Azure と `gpt-realtime-2.1-mini` ではまだ試していません。導入前に、Web チャット、音声、書き起こしを実環境で確認してください。
- **ネットワーク公開。** ユーザーがコンテナのポート 8000 に直接到達できないことを確認してください。アプリ自体には認証がなく、Coder の認証付きプロキシ（`share = "owner"`）に依存しています。
- **コストとクォータ。** 常時稼働とはいえ、Azure の課金が発生するのは会話中と定期ジョブの実行時だけです。ただし音声チャットは継続的に課金されるので、リソースごとにクォータやアラートを設定してください。
- **スケジューラー。** APScheduler はプロセス内で動くため、ワークスペースが停止している間はジョブが実行されません（上記の常時稼働設定が前提です）。SQLite とスケジューラーのプロセスは、ワークスペースごとに 1 つにしてください。
- **ボリューム。** データボリュームには `prevent_destroy` が付いています。ワークスペースを削除するときは、手動での対応が必要です。
- Web 閲覧やメール送信は実装されていません。ツールを有効にする前に、MCP ツールのレジストリ、承認フロー、外部通信の制限、ログ、プロンプトインジェクション対策が必要です。
