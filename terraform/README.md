---
display_name: Personal AI Agent
description: Azure OpenAI Realtime（gpt-realtime-2.1-mini）で Web チャットと音声チャットができる、常時稼働のパーソナルエージェント
icon: /emojis/1f916.png
tags: [ai, agent, docker, azure-openai]
---

# Personal AI Agent

ワークスペースごとに専用の AI エージェントが常時稼働します。個人の Azure OpenAI API キーを使い、**Web チャット**と**音声チャット**（WebRTC）で会話できます。会話履歴と定期ジョブはワークスペースのボリュームに保存されます。

## ワークスペース作成時に入力するもの

| パラメーター | 説明 |
| --- | --- |
| Azure OpenAI API キー（個人用） | 必須。入力はマスクされ、ブラウザには送られません |
| Azure OpenAI エンドポイント | 例：`https://YOUR-RESOURCE.openai.azure.com`（`/openai/v1/...` 付きの URL でも可） |
| Realtime モデルのデプロイ名 | 既定：`gpt-realtime-2.1-mini` |
| Web 検索に使うモデルのデプロイ名 | 例：`gpt-6.1-terra`。空なら Web 検索なし |
| 音声 | 既定：`marin` |

作成後、ワークスペースの **Personal AI Agent** アプリを開きます。音声チャットではブラウザのマイク許可が必要です。

アプリの「スキル」セクションで、Agent Skills（SKILL.md を含むフォルダの .zip / .skill）のインポート、エクスポート、有効化/無効化、編集ができます。スキルはワークスペースのボリュームに保存されます。

## 管理者向け

- テンプレート変数：`image`（必須で設定。レジストリ上のイメージ）、`azure_openai_endpoint`、`realtime_deployment`、`web_search_deployment`、`app_subdomain`
- 常時稼働にするには自動停止を無効にします：`coder templates edit <template> --default-ttl 0s`（Premium では `--allow-user-autostop=false --autostop-requirement-weekdays none --dormancy-threshold 0s --dormancy-auto-deletion 0s` も）
- データボリュームには `prevent_destroy` が付いています。ワークスペースを削除するときは手動での対応が必要です。
