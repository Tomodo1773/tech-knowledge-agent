# プラットフォームと運用

この文書は、MVPのAzure採用設定、IaC、bootstrap、デプロイと運用の正本である。アプリケーション設計は[architecture.md](architecture.md)を参照する。

## 採用設定

| 項目 | 採用設定 |
|---|---|
| 主要region | Japan East |
| capacity不足時の候補 | East US 2 |
| chat / agent | `gpt-5.6-luna` version `2026-07-09`、GlobalStandard、10K TPM、Responses API、reasoning effortは既定 |
| embedding | `text-embedding-3-small` version `1`、GlobalStandard、10K TPM。vector field設定は[architecture.md](architecture.md#cosmos-db検索ストア)を参照 |
| Functions | Flex Consumption、always-ready 0、Python 3.13。依存はuvで管理する |
| Cosmos DB | Free Tierをaccount作成時に有効化。`chunks` containerはdedicated provisioned throughput 400 RU/s。local authを無効化し、data-plane RBACだけで接続する |

GlobalStandardによる地域外処理を許容する。capacityは予約ではないため、`azd provision`直前に同じregion、SKU、version、TPMを再確認し、利用不可ならEast US 2を検討する。当日の実測snapshotは[調査記録](research/implementation-readiness-2026-08-11.md#t2-regionmodelplan費用)に残す。

Cosmos DBのFree Tierは1 accountあたり1,000 RU/sまでを無料にする。`chunks`だけで1,000 RU/sを占有すると、containerを一つ足した時点で課金が始まる。MVPの想定は1,000 vector未満であり400 RU/sで足りるため、無料枠に余裕を残す。

Functionsはalways-ready 0で始める。LINE Webhookはコールドスタート時に2秒の応答期限を超えることがあるが、Webhook再送と個人利用の使用頻度を踏まえて許容する。実際に困る場合だけalways-ready instanceの追加を検討する。トップレベルで重い依存をimportしない実装規約は[architecture.md](architecture.md#line質問)を正とする。

## コストと日常運用

月額の目安は1,000円とし、Azure Budgetに予測・実績通知を設定する。Budgetはhard stopではないため、通知後に利用状況を確認する。Application Insightsにはdaily cap（初期値0.1 GB/日）を設定し、telemetry取り込みの暴走を防ぐ。Cosmos throughput、model token、Application Insightsの取り込み・保持、Key Vault操作は継続課金になり得る。単価や無料枠は変動するため、実resource作成前に料金を確認する。

MVPでは個人利用・dev一環境を前提とする。自動fallbackやrollback機構は作らず、Queue滞留・poison message、Function/Agentエラー、同期停止、予算超過を必要に応じて確認する。監視項目の詳細は[quality.md](quality.md)を参照する。

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

Agent version、deploy後に判明するAgent principalへのCosmos data-plane role assignment、evaluation datasetはBicepの外にある。これらはローカルのpost-deploy scriptで実行する。

deployはローカルから`azd`で行い、GitHub ActionsからはAzureへログインしない。CIはBicep build、静的検査、指示ファイル同期の検証だけを行う。protected environmentとOIDC federated credentialはMVPでは構成しない。実resource名、principal ID、endpoint、role assignment、deployment outputは公開ログ・artifact・PR commentへ出さない。

## Bootstrap

初回bootstrapで、LINE channel設定、Webhook再送の有効化、Key Vaultへのsecret登録を行う。公開GitHub repositoryのowner、repository、default branch、および応答を許可するLINE `userId`のallowlistはdeploy時の非機密設定として与える。実値を文書やログへ残さず、Bicepにはsecret名と参照だけを置く。

Hosted Agentはsource-code deploymentを使う。`requirements.txt`の生成方法は[ローカル開発](#ローカル開発)を正とする。`azd ai agent init --deploy-mode code --runtime python_3_13 --entry-point main.py --dep-resolution remote_build`を起点にし、artifactは`main.py`、tool module、`requirements.txt`、`.agentignore`、`azure.yaml`で構成する。remote buildに問題が出た場合だけbundled packagesを検討する。

## ローカル開発

Functionsはローカルで実行し、Cosmos、Storage、Foundryは実resourceへ接続する。emulatorは使わない。個人利用の想定使用量ではFree Tierと低い従量課金の範囲に収まり、環境差分を持ち込まない方が単純である。

LINE Webhookのローカル受信には既存のCloudflare Tunnelを使う。Sync Functionを任意のタイミングで動かす場合はFunctionsのadmin API（`POST /admin/functions/{name}`）を使い、そのための手動同期用HTTP endpointをアプリへ追加しない。

依存はuvで管理し、FunctionsとHosted Agentが必要とする`requirements.txt`は`azd`のprepackage hookで`uv export`により生成する。生成した`requirements.txt`はcommitせず、lockfileを正とする。

## デプロイと復旧

`azd deploy`は新しいimmutable Agent versionへendpoint routingを自動設定する。split routingやdraft previewは採用しない。deploy後、Agent principalへの`chunks` container scopeのCosmos data-plane role assignmentをpost-deploy scriptで作成する。

deploy後にdevで一度だけ、Agent起動・Cosmos検索・LINE Pushの疎通を確認する。失敗時はtraceとログから原因を切り分け、修正して再deployする。自動・手動のrollback手順はMVPでは用意せず、必要性はMVP後に判断する。

## 参考

- [Hosted Agent code deployment](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code)
- [Responses API](https://learn.microsoft.com/azure/foundry/openai/how-to/responses)
- [Cosmos DB vector search](https://learn.microsoft.com/azure/cosmos-db/nosql/vector-search)
- [Flex Consumption plan](https://learn.microsoft.com/azure/azure-functions/flex-consumption-plan)
- [LINE webhook error statistics](https://developers.line.biz/ja/docs/messaging-api/check-webhook-error-statistics/)
