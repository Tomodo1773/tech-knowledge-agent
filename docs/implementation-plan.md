# 実装計画

この文書はMVP実装の開始条件、実装順、最小vertical slice、完了条件の正本である。設計値は[architecture.md](architecture.md)、Azure設定は[platform-and-operations.md](platform-and-operations.md)を参照する。

## 開始条件

- [プラットフォームと運用の費用方針](platform-and-operations.md#コストと日常運用)に従った実resource作成の許可を得ること
- deploy直前にFoundryのregion、SKU、version、TPMのcapacity / quotaを再確認すること
- LINE、Key VaultのbootstrapとGitHub同期元の非機密設定に必要な実値を安全に用意できること

## 実装順

1. Bicep / `azd`の骨組み、Key Vault、Storage、Cosmos、Foundry、Application Insightsを作る。
2. TimerによるGitHub SHA確認、Tableの同期state、Storage Queueへの直接投入を実装する。
3. Indexerで初回同期と変更時の全件reconcile、front matter検証、chunk化、embedding、Cosmos upsertと削除反映を実装する。
4. Hosted Agentと`knowledge_search`をdeployし、Agent identityへのCosmos reader roleをpost-deployで付与する。
5. LINE Webhook、Storage Queueへの直接投入、Agent WorkerからのLINE Pushを実装する。
6. OpenTelemetryの相関と固定datasetのsmoke evaluationを追加する。
7. protected environmentでdeployし、一度だけ手動の疎通確認を行う。

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

## MVP完了条件

- `articles/**/*.md`全件を初期同期でき、default branchの変更と削除が次回Timerで冪等に反映される。
- LINEの1:1質問に、根拠記事へのリンク付きでPush Messageを返せる。
- LINE job state、Webhook再送、Queue再実行、同一retry keyにより質問の重複処理とPushの重複送信を抑止できる。
- AgentがManaged IdentityでCosmosを検索し、credentialをコードやログへ出さない。
- 一件のLINE質問と一件のGitHub同期をtraceで追跡できる。
- 固定約10件のsmoke evaluationを実行し、結果とtraceを確認できる。
- `azd provision` / `azd deploy` と、失敗を修正して再deployする手順を再現できる。

production trace評価、continuous evaluation、rollback機構はMVP完了条件に含めない。
