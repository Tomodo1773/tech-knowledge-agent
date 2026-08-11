# プラットフォームと運用

この文書は、MVPのAzure採用設定、IaC、bootstrap、デプロイと運用の正本である。アプリケーション設計は[architecture.md](architecture.md)を参照する。

## 採用設定

| 項目 | 採用設定 |
|---|---|
| 主要region | Japan East |
| capacity不足時の候補 | East US 2 |
| chat / agent | `gpt-5.6-luna` version `2026-07-09`、GlobalStandard、10K TPM、Responses API、reasoning `max` |
| embedding | `text-embedding-3-large` version `1`、GlobalStandard、10K TPM。vector field設定は[architecture.md](architecture.md#cosmos-db検索ストア)を参照 |
| judge | chatと同じLuna deploymentを共用。smokeではreasoning `max`を必須にしない |
| Functions | Flex Consumption、always-ready 0 |
| Cosmos DB | Free Tierをaccount作成時に有効化。`chunks` containerはdedicated provisioned throughput 1,000 RU/s。shared throughput databaseは使わない |

GlobalStandardによる地域外処理を許容する。capacityは予約ではないため、`azd provision`直前に同じregion、SKU、version、TPMを再確認し、利用不可ならEast US 2を検討する。当日の実測snapshotは[調査記録](research/implementation-readiness-2026-08-11.md#t2-regionmodelplan費用)に残す。

## コストと日常運用

月額の目安は1,000円とし、Azure Budgetに予測・実績通知を設定する。Budgetはhard stopではないため、通知後に利用状況を確認する。Cosmos throughput、model token、Application Insightsの取り込み・保持、Key Vault操作は継続課金になり得る。単価や無料枠は変動するため、実resource作成前に料金を確認する。

MVPでは個人利用・dev一環境を前提とする。自動fallbackや自動rollbackは作らず、Queue滞留・poison message、Function/Agentエラー、同期停止、予算超過を日常的に確認する。監視項目の詳細は[quality.md](quality.md)を参照する。

## IaCと配送

Azure Resource Managerで表せる基盤はBicepを正とし、`azd provision`で適用する。`azd deploy`はFunctionsとHosted Agentのartifactを配布する。対応するAzure Verified Module (AVM) がある場合は第一候補とし、versionを固定する。AVMが必要なpropertyを持たない場合だけraw Bicepを使い、理由と再評価条件をcommentへ残す。AVMの`enableTelemetry`は`false`にする。

予定するmodule境界は次のとおり。

| module | 担当 |
|---|---|
| `infra/main.bicep` | subscription / resource group |
| `app/functions.bicep` | FunctionsとStorage |
| `app/data.bicep` | Cosmos DB |
| `app/foundry.bicep` | Foundry、model deployment、Hosted Agent関連基盤 |
| `app/observability.bicep` | Log Analytics、Application Insights、Budget / alert |
| `app/security.bicep` | Key Vaultとidentity |

Agent version、deploy後に判明するAgent principalへのCosmos data-plane role assignment、evaluation dataset / schedule / ruleはBicepの外にある。保護されたCIのpost-deploy stepで実行する。

公開PRではAzureへログインせず、Bicep buildと静的検査だけを行う。実環境のvalidate、what-if、deployは保護されたGitHub Environmentで行い、実resource名、principal ID、endpoint、role assignment、deployment outputを公開ログ・artifact・PR commentへ出さない。

## Bootstrap

初回bootstrapで、GitHub Appの作成・対象repositoryへのinstall・Webhook登録、LINE channel設定、Key Vaultへのsecret登録を行う。実値をBicep parameter、repository、ログへ残さない。Bicepにはsecret名と参照だけを置く。

Hosted Agentはsource-code deploymentを使う。`azd ai agent init --deploy-mode code --runtime python_3_13 --entry-point main.py --dep-resolution remote_build`を起点にし、artifactは`main.py`、tool module、`requirements.txt`、`.agentignore`、`azure.yaml`で構成する。remote buildに問題が出た場合だけbundled packagesを検討する。

## デプロイと手動rollback

`azd deploy`は新しいimmutable Agent versionへendpoint routingを自動設定する。split routingやdraft previewは採用しない。deploy後にdevで一度だけ、Agent起動・Cosmos検索・LINE Pushの疎通を確認する。失敗時は保護環境で次を行う。

1. 現在のroutingと直前versionを確認する。
2. endpoint routingを旧versionへ100%戻す。
3. Agent起動・Cosmos検索・LINE Pushを一度確認し、結果を非公開の運用記録へ残す。

自動化はMVP後に必要性を判断する。

## 参考

- [Hosted Agent code deployment](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code)
- [Reasoning models](https://learn.microsoft.com/azure/foundry/openai/how-to/reasoning)
- [Responses API](https://learn.microsoft.com/azure/foundry/openai/how-to/responses)
- [Cosmos DB vector search](https://learn.microsoft.com/azure/cosmos-db/nosql/vector-search)
