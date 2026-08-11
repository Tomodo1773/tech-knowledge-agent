# 技術ナレッジ検索エージェント

Azure AI関連スタックを学びながら、自分の技術ブログをSlackから検索・質問できる個人用エージェントを作る。MVPはGitHubで管理するZenn記事を検索対象にし、Azure AI Foundry Hosted Agentで回答する。

## MVPで提供すること

- 公開GitHub repositoryのdefault branchにある`articles/**/*.md`を同期し、ベクトル検索できるようにする
- Timer Functionが日次でcommit SHAとblob SHAを確認し、変更のあった記事だけを再indexする
- Slack AppとのDMで質問を受け、検索根拠へのリンク付きでスレッド返信する。同じスレッドの直前の会話を踏まえた追い質問に答えられる
- 処理状況と失敗を追跡できる最低限のトレースと約10件のsmoke evaluationを持つ

画像OCR、複数利用者・高可用性、Slack Agent機能・Block Kit・streaming、rollback機構、continuous evaluationはMVPの対象外とする。

## 最小構成

```mermaid
flowchart LR
  GH[GitHub / Zenn articles] -->|daily SHA + tree check| SYNC[Sync Function]
  SYNC --> E[Foundry embedding]
  SYNC --> C[Cosmos DB: chunks]
  SYNC --> T[Table Storage: state]
  SLACK[Slack DM] -->|Events API| SWH[Slack Events Function]
  SWH --> T
  SWH -->|enqueue| Q[Storage Queue]
  Q --> W[Agent Worker Function]
  W --> A[Foundry Hosted Agent]
  A -->|knowledge_search| C
  W -->|chat.postMessage| SLACK
```

## 採用と重要な決定

- AzureはJapan Eastを第一候補にし、FoundryはLuna、埋め込みは`text-embedding-3-small`を使う。
- 検索対象は`articles/**/*.md`の全記事。`published`の値では除外しない。
- 差分判定はGit Trees APIのblob SHAで行い、content hashを自前で計算しない。
- 会話履歴はHosted AgentのResponses protocolが管理し、Workerは`previous_response_id`で会話をつなぐ。Agent内部のmodel callは`store: false`とする。
- Slackは単一workspace・単一利用者・DMだけを対象とし、トップレベルメッセージを会話の起点、スレッド内メッセージを追い質問として扱う。
- 公開GitHub repositoryは認証なしのGitHub APIで読み、Azure内の接続はManaged Identityを使う。
- 個人MVPなので、deployはローカルの`azd`から行い、deploy後の確認は一度の疎通確認に限定する。
- コスト方針とAzure Budgetは[プラットフォームと運用](docs/platform-and-operations.md#コストと日常運用)を正とする。

詳細な設計値と運用手順は、READMEへ重複させず下記を正とする。

## 文書マップ

| 文書 | 役割 |
|---|---|
| [architecture.md](docs/architecture.md) | データ同期・質問応答・データ契約・identity/RBACの設計上の正本 |
| [platform-and-operations.md](docs/platform-and-operations.md) | Azure採用設定、IaC、bootstrap、deploy・運用の正本 |
| [quality.md](docs/quality.md) | telemetry、content記録、MVP評価とMVP後の品質施策の正本 |
| [implementation-plan.md](docs/implementation-plan.md) | 着手条件、実装順、vertical slice、完了条件の正本 |
| [repository-policy.md](docs/repository-policy.md) | 個人開発に見合う設計範囲、公開repository、IaC、供給網に関する恒久ポリシー |
| [research/implementation-readiness-2026-08-11.md](docs/research/implementation-readiness-2026-08-11.md) | 調査時点の根拠・capacity snapshot・統合判断の記録。現行設計の正本ではない |

## 現在地

初期構想と実装方針は確定済みで、Azure resource、実装コード、外部サービス設定はまだ作成していない。実装は[実装計画](docs/implementation-plan.md)の開始条件を満たしてから行う。
