# 実装計画

この文書はMVP実装の開始条件、実装順、最小vertical slice、完了条件の正本である。設計値は[architecture.md](architecture.md)、Azure設定は[platform-and-operations.md](platform-and-operations.md)を参照する。

## 開始条件

- [プラットフォームと運用の費用方針](platform-and-operations.md#コストと日常運用)に従った実resource作成の許可を得ること
- deploy直前にFoundryのregion、SKU、version、TPMのcapacity / quotaを再確認すること
- LINE、Key VaultのbootstrapとGitHub同期元の非機密設定に必要な実値を安全に用意できること

## リポジトリ構成

Function AppとHosted Agentは依存が異なるため、別のPython projectとして分ける。

| path | 内容 |
|---|---|
| `infra/` | Bicep |
| `src/functions/` | Function App |
| `src/agent/` | Hosted Agent |
| `tests/` | unit test |
| `scripts/` | post-deployのrole assignment、smoke evaluation、repository policy検証 |

lintとtestはruffとpytestを使う。testの対象はfront matter検証、chunk分割、blob SHA差分判定のようにAzureへ接続しない処理だけとし、Azure resourceのmockは作らない。

## 実装順

1. Bicep / `azd`の骨組み、Key Vault、Storage、Cosmos、Foundry、Application Insightsを作る。
2. Timer TriggerのSync Functionで、SHA確認、Trees APIによるblob SHA突き合わせ、front matter検証、chunk化、embedding、Cosmos upsertと削除反映を実装する。
3. Hosted Agentと`knowledge_search`をdeployし、Agent identityへのCosmos reader roleをpost-deploy scriptで付与する。
4. LINE Webhook、Storage Queueへの投入、Agent WorkerからのLINE Pushと会話履歴の`previous_response_id`参照を実装する。
5. OpenTelemetryの相関と固定datasetのsmoke evaluationを追加する。
6. ローカルから`azd`でdeployし、一度だけ手動の疎通確認を行う。

## 最小vertical slice

最初のsliceは、次を一件ずつ通すことに限定する。

1. Hosted Agentが起動し、Agent Managed IdentityでCosmos vector queryが成功する。
2. LINE質問を受信し、Queue経由でAgent回答をPush Messageとして返す。
3. LINE → Queue → Agent → Cosmosを一つのtraceとして相関できる。

このsliceはroutingの品質gateではない。失敗時はremote build、custom span、LINE送信を切り分けて調査する。

## 実装時に決める項目

- chunk size / overlapと、batch制限を超える記事を記事単位で再実行する方法
- Application Insightsの保持期間とsampling
- 実記事から作るsmoke dataset、baseline後の改善優先度
- 固定するAVM versionと、必要propertyが未対応の場合のraw Bicep
- Function App MIとHosted Agent identityに付与するFoundryのdata-plane role名

## MVP完了条件

- `articles/**/*.md`を初期同期でき、default branchの変更と削除が次回Timerで反映される。
- 変更のない記事が再embeddingされないことを、二回目のTimer実行で確認できる。
- LINEの1:1質問に、根拠記事へのリンク付きでPush Messageを返せる。
- 直前の質問を踏まえた追い質問に答えられ、24時間後は新しい会話として扱われる。
- `webhookEventId`の重複チェックにより、Webhook再送で回答が二重に届かない。
- allowlist外の利用者とgroup / roomには回答せず、監査記録だけが残る。
- AgentがManaged IdentityでCosmosを検索し、credentialをコードやログへ出さない。
- 一件のLINE質問と一件のGitHub同期をtraceで追跡できる。
- 固定約10件のsmoke evaluationを実行し、結果とtraceを確認できる。
- `azd provision` / `azd deploy` と、失敗を修正して再deployする手順を再現できる。

production trace評価、continuous evaluation、rollback機構、コールドスタート対策のalways-ready instanceはMVP完了条件に含めない。
