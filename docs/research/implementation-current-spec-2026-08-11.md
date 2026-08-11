# 実装開始時の現行仕様確認（2026-08-11）

この文書は[実装計画](../implementation-plan.md) Step 0の調査記録である。実装中の設計値は[architecture.md](../architecture.md)と[platform-and-operations.md](../platform-and-operations.md)を正とする。確認は公式文書、公式sample、ローカルtoolのread-only操作だけで行い、依存導入、scaffold生成、Azure resource作成、Slack設定変更は行っていない。

## 採用するtoolingとscaffold

| 項目 | 採用値 |
|---|---|
| Azure Developer CLI | `azd 1.28.1`。公式quickstartの下限`1.27.1`を満たす |
| Foundry extension | install入口は`microsoft.foundry`。`azure.yaml`の`requiredVersions.extensions.azure.ai.agents`は、Python projectのsource deploy修正とdeploy後identity outputを備える公式`azure-dev` beta.9を下限とし、`>=1.0.0-beta.9`とする |
| Agent runtime | Python 3.13、source-code deployment、remote build |
| Agent protocol | Responses `2.0.0` |
| Agent Framework | `agent-framework-core==1.13.0`、`agent-framework-foundry==1.10.4`、`agent-framework-foundry-hosting==1.0.0b260730`をStep 1のdependency syncで解決し、`src/agent/uv.lock`で固定した。`max_function_calls`はFoundry client 1.10.4からCore 1.13.0のrequest単位設定へ渡ることを実classで確認した。manifestは公式sampleと同じ直接依存と最低constraintを保持する |
| Functions runtime | Flex Consumption、Python 3.13、Python v2 programming model |

Hosted Agentのmanaged hosting service自体はGAだが、PythonのAgent Framework hosting integrationはprerelease、Foundry extensionもbetaである。個人MVPの学習価値を優先して採用し、lockfile更新時とdeploy前に互換性を再確認する。

現行のAgent Framework sampleとextension schemaを境界として、Hosted Agent serviceは`language: python`、`codeConfiguration.runtime: python_3_13`、文字列の`codeConfiguration.entryPoint: main.py`、`kind: hosted`、Responses protocol `2.0.0`を持つ。model deploymentは別の`azure.ai.project` serviceに置き、Agent serviceが`uses`で参照する。Agent設定はlistの`environmentVariables`を使う。`FOUNDRY_PROJECT_ENDPOINT`はplatform注入なので重複定義しない。chat model名に加え、Agent search adapterが使う`COSMOS_ENDPOINT`と専用`EMBEDDING_MODEL_DEPLOYMENT_NAME`をBicep outputからazd環境経由で渡す。database `knowledge`とcontainer `chunks`は共有契約の固定値なので環境変数を増やさない。

この`codeConfiguration`付きserviceはdirect source ZIP deploymentであり、顧客管理ACRを必要としない。公式`azure-dev`では、Python projectに`pyproject.toml`があると誤ってcontainer経路と判定してACR入力を求める問題がbeta.7で修正された。さらにbeta.9のdeploy finalizeはAgent instance identity principal IDを`AGENT_<SERVICE>_INSTANCE_IDENTITY_PRINCIPAL_ID`へ出力するため、postdeploy RBACの再現性を含めてbeta.9未満を許可しない。

このrepositoryでは公式sampleを直接展開しない。Step 1で既存設計に合わせて`src/agent/`を作り、rootの`azure.yaml`へ同じservice形状を手作業で統合する。Agentの除外fileは現行sampleに合わせて`.azdignore`とする。IaCはsynthesized infrastructureではなく既存方針どおり`infra.provider: bicep`、`infra.path: ./infra`とする。旧`agent.manifest.yaml`と`agent.yaml`は作らない。

`ResponsesHostServer`、`FoundryChatClient`、`default_options={"store": false}`は現行sampleと一致する。chat側は内部aio project clientを使い、同期`knowledge_search`のquery embedding側は別の同期project clientを使ってlifecycleを分離する。Responses側が会話履歴を管理するため、Agent内部では履歴を二重保存しない。

## RBAC差分

- Hosted Agent identityは同じFoundry projectのendpoint経由で行うmodel inferenceにimplicit accessを持つ。明示的なFoundry roleは追加せず、外部resourceである`chunks` containerへのCosmos DB Built-in Data Readerだけをdeploy後に付与する。
- Function App MIはembeddingとAgent呼び出しのため、Foundry project scopeのFoundry Userを使う。
- Foundry Project MIはaccount scopeのFoundry Userと、evaluation用にLog Analytics workspace scopeのLog Analytics Data Readerを使う。
- Function App UAIとFoundry Project MIは、local authを無効化したApplication Insightsへ送信するため、Application Insights scopeのMonitoring Metrics Publisherを使う。Foundry connectionは公式sampleの`2025-09-01` APIと`ProjectManagedIdentity`を使い、ApiKey credentialを持たない。
- deploy実行者はFoundry project scopeのFoundry Project Managerを必要とする。外部resourceのrole assignment作成にはOwnerまたはRole Based Access Control Administratorが必要である。

Foundry roleは2026年に名称変更されており、旧`Azure AI User`等の表記を新しい`Foundry User`等へ統一する。role IDは名称変更前後で同じである。

## FunctionsとSlack

Flex ConsumptionはPython 3.13をサポートする。Function Appは推奨されるPython v2 decorator modelを使い、triggerは`function_app.py`へ登録する。Queue Triggerの直列化方針、Slack署名、3秒以内の2xx、再送時のevent重複抑止、DMの`message.im`選別には設計変更を要する差分はなかった。

Slack APIはpackage versionとして固定しない。HTTP contractをunit testで固定し、SDKを採用する場合だけ`uv.lock`でversionを固定する。

## AVM採用version

2026-08-11の公式AVM catalogとMicrosoft Container Registryの安定tagを確認し、次をStep 3のBicep参照に固定する。すべて`enableTelemetry: false`とする。

| module | version |
|---|---|
| `avm/res/web/serverfarm` | `0.7.0` |
| `avm/res/web/site` | `0.24.0` |
| `avm/res/storage/storage-account` | `0.33.0` |
| `avm/res/document-db/database-account` | `0.21.0` |
| `avm/res/cognitive-services/account` | `0.18.0` |
| `avm/res/operational-insights/workspace` | `0.16.1` |
| `avm/res/insights/component` | `0.8.0` |
| `avm/res/key-vault/vault` | `0.14.0` |
| `avm/res/consumption/budget/rg-scope` | `0.1.0` |

各versionは公式`Azure/bicep-registry-modules`のGit tagをread-onlyで再照合した。上表の9件はすべて公開tagが存在する。Budgetの`rg-scope`は`0.1.0`だけがmatching releaseであり、誤って記録した不存在versionから修正した。`0.1.0`のschemaは採用するactual / forecast budgetの`amount`、`name`、`contactEmails`、`operator`、`resetPeriod`、`thresholds`、`thresholdType`、`enableTelemetry`を扱える。

FunctionsのAVMはFlex Consumptionの`FC1` server farmと`functionAppConfig`を扱える。実装時に必要propertyが固定versionで表現できないことが判明したmoduleだけraw Bicepへ切り替え、理由をmodule commentへ残す。

## ローカルpreflight

- 利用可能: `azd 1.28.1`、Socket Firewall Free `1.15.0`、`uv 0.11.33`、uv管理のPython `3.13.2`
- 利用可能: Azure CLI `2.89.0`内蔵Bicep CLI `0.42.1`
- 未導入: Foundry extension、Azure Functions Core Tools、standalone Bicep CLI
- PATH既定のPythonは`3.11.15`なので、各`pyproject.toml`で`requires-python`を3.13に固定する。依存は`sfw uv sync`で同期し、その後の実行は`uv run --no-sync`を入口にする

Python依存はSocket Firewall経由で同期した。最初のsandbox内実行は無出力で完了せず、権限昇格後は解決した。その後、共有uv cache内の`attrs` metadata欠損で一度installに失敗し、lockfileを保持したまま`--no-cache`で同期を完了した。workspace / environment / user profile直下に参照可能なSFW設定やcooldown設定はなく、tool versionもmanifestの`uv==0.11.33`と一致したため、無出力の原因をversion / cooldown不整合またはcache破損の一方へ断定しない。

Foundry extensionとFunctions Core ToolsはSocket Firewall対応の導入経路を確認できていないため、この作業単位では導入しない。Azure CLI内蔵Bicepは利用できるが、BCP190単独ではmoduleの未cacheと不存在を区別できない。全固定tagの実在を公式Git refsで別途確認したうえで、現行cacheに揃っていないmoduleを`az bicep build`するとMCRからrestoreすることを確認した。これは`sfw`非対応の依存取得になるため無断実行せず、Step 3ではlocal policy検査までを行いBicep buildを未完gateとして残す。`azure.yaml`のextension検証、Function host discovery、`azd provision`も未実行である。

## 根拠

- [公式Agent Framework Responses sample](https://github.com/microsoft-foundry/foundry-samples/tree/main/samples/python/hosted-agents/agent-framework/responses/01-basic)
- [Azure Developer CLI extension source](https://github.com/Azure/azure-dev/tree/main/cli/azd/extensions)
- [Hosted Agent projectの初期化](https://learn.microsoft.com/azure/foundry/agents/how-to/init-agent-project)
- [azure.yaml reference](https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference)
- [Agent Framework Hosted Agent hosting](https://learn.microsoft.com/agent-framework/hosting/foundry-hosted-agent)
- [Hosted Agent permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions)
- [Foundry UAI infrastructure sample](https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/20-user-assigned-identity)
- [Azure Functions Python reference](https://learn.microsoft.com/azure/azure-functions/functions-reference-python)
- [Azure Functions app settings](https://learn.microsoft.com/azure/azure-functions/functions-app-settings#applicationinsights_authentication_string)
- [Flex Consumption plan](https://learn.microsoft.com/azure/azure-functions/flex-consumption-plan)
- [AVM Bicep resource module catalog](https://azure.github.io/Azure-Verified-Modules/indexes/bicep/bicep-resource-modules/)
