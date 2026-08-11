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

実装開始時のtoolingのversion方針、scaffold、AVMの採用versionは[現行仕様確認](research/implementation-current-spec-2026-08-11.md)を正とする。`azure.yaml`ではFoundry extensionの`azure.ai.agents`に最低version constraint `>=1.0.0-beta.9`を設定する。Agent Frameworkを含むPython依存の正確なversionはdependency sync後の各`uv.lock`で固定する。

GlobalStandardによる地域外処理を許容する。capacityは予約ではないため、`azd provision`直前に同じregion、SKU、version、TPMを再確認し、利用不可ならEast US 2を検討する。当日の実測snapshotは[調査記録](research/implementation-readiness-2026-08-11.md#t2-regionmodelplan費用)に残す。

Cosmos DBのFree Tierは1 accountあたり1,000 RU/sまでを無料にする。`chunks`だけで1,000 RU/sを占有すると、containerを一つ足した時点で課金が始まる。MVPの想定は1,000 vector未満であり400 RU/sで足りるため、無料枠に余裕を残す。

Functionsはalways-ready 0で始める。Slack Events APIはコールドスタート時に3秒の応答期限を超えることがあるが、最大3回のevent再送と個人利用の使用頻度を踏まえて許容する。Slackは60分間の配信試行の95%超が失敗するとevent subscriptionを一時的に無効化するため、応答失敗が続く場合はこの兆候として扱う。実際に困る場合だけalways-ready instanceの追加を検討する。トップレベルで重い依存をimportしない実装規約は[architecture.md](architecture.md#slack質問)を正とする。

## コストと日常運用

月額の目安は1,000円とし、Azure Budgetに予測・実績通知を設定する。Budgetはhard stopではないため、通知後に利用状況を確認する。Application Insightsにはdaily cap（初期値0.1 GB/日）を設定し、telemetry取り込みの暴走を防ぐ。Cosmos throughput、model token、Application Insightsの取り込み・保持、Key Vault操作は継続課金になり得る。単価や無料枠は変動するため、実resource作成前に料金を確認する。

MVPでは個人利用・dev一環境を前提とする。自動fallbackやrollback機構は作らず、Queue滞留・poison message、Function/Agentエラー、同期停止、予算超過を必要に応じて確認する。監視項目の詳細は[quality.md](quality.md)を参照する。

GitHub同期は毎日18:00 UTC（JST 03:00）に実行する。匿名GitHub GETは接続5秒・応答30秒、最大3 attemptとし、429、5xx、timeoutだけをbounded backoffで再試行する。`Retry-After`も2秒を上限にする。Azure SDK clientはDefaultAzureCredentialによるFunction App Managed Identityを共有する。Table / Cosmos / project clientは接続5秒・応答30秒・SDK retry 2回、embeddingのOpenAI clientは全体30秒・retry 2回を初期値とする。実測でGitHub rate limitや大きな記事取得に支障がある場合だけ変更する。

## IaCと配送

Azure Resource Managerで表せる基盤はBicepを正とし、`azd provision`で適用する。`azd deploy`はFunctionsとHosted Agentのartifactを配布する。対応するAzure Verified Module (AVM) がある場合は第一候補とし、versionを固定する。AVMが必要なpropertyを持たない場合だけraw Bicepを使い、理由と再評価条件をcommentへ残す。AVMの`enableTelemetry`は`false`にする。

予定するmodule境界は次のとおり。

| module | 担当 |
|---|---|
| `infra/main.bicep` | subscription / resource group |
| `infra/app/functions.bicep` | FunctionsとStorage |
| `infra/app/data.bicep` | Cosmos DB |
| `infra/app/foundry.bicep` | Foundry、model deployment、Hosted Agent関連基盤 |
| `infra/app/observability.bicep` | Log Analytics、Application Insights、Budget / alert |
| `infra/app/security.bicep` | Key Vaultとidentity |

Agent version、deploy後に判明するAgent principalへのCosmos data-plane role assignment、Responses endpoint、evaluation datasetはBicepの外にある。これらはローカルのpost-deploy scriptで扱い、Bicepで仮のendpointを作らない。

deployはローカルから`azd`で行い、GitHub ActionsからはAzureへログインしない。CIは静的検査と指示ファイル同期を行い、Socket Firewall方針に適合するAVM restore経路の確定後にBicep buildも追加する。protected environmentとOIDC federated credentialはMVPでは構成しない。実resource名、principal ID、endpoint、role assignment、deployment outputは公開ログ・artifact・PR commentへ出さない。

Application Insightsはlocal authを無効化する。Function Appにはconnection stringと`APPLICATIONINSIGHTS_AUTHENTICATION_STRING=Authorization=AAD;ClientId=<Function UAI client ID>`を設定し、UAIへApplication Insights scopeのMonitoring Metrics Publisherを付与する。Foundry projectのApp Insights connectionは現行公式sampleと同じ`2025-09-01` APIの`ProjectManagedIdentity`を使い、credential keyを保存しない。Project MIにも同scopeのMonitoring Metrics Publisherを付与し、connection stringは接続metadataとしてだけ渡す。

Hosted Agentは評価と障害解析のため`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`を固定し、質問・回答・tool入出力をApplication Insightsへ記録する。これは公開可能な技術記事と個人の技術質問だけを扱うMVP前提であり、credential、個人情報、業務上の非公開情報を入力しない。閲覧権限、保持期間、daily capの保護は[品質](quality.md#content記録と保護)を正とする。

## Bootstrap

初回bootstrapで、Slack Appをmanifest templateから自分のworkspaceへ作成・installし、App HomeのMessages tab、Bot Token Scopeの`im:history`、`chat:write`、`reactions:write`、`message.im` event subscriptionを設定する。`reactions:write`は受付を示す`eyes` reactionに使う。App Homeは`messages_tab_enabled: true`だけでなく`messages_tab_read_only_enabled: false`も必要で、これを省くとMessages tabに入力欄が出ずDMを送れない。Function endpointの作成後にEvents API Request URLを登録し、Slack Signing SecretとBot tokenをKey Vaultへ保存する。Request URLの登録時にSlackが`url_verification`を送るため、Events Functionは先にdeployしておく。公開GitHub repositoryのowner、repository、default branch、および応答を許可するSlack `team_id`と`user`のallowlistはdeploy時の設定として与える。実値を文書やログへ残さず、Bicepにはsecret名と参照だけを置く。複数workspace向けOAuth、Slack Marketplace公開は構成しない。Slack Agent機能を採用しない理由は次節に記す。

## Slack Agent機能を採用しない理由

回答待ちの状態表示には、本来`assistant.threads.setStatus`が最も自然である。`chat:write`で呼べて追加scopeの負担はなく、`loading_messages`による回転表示も持つ。学習方針としても、Agent向けの専用機能があるならそれを使うのが筋である。

採用しない理由はコストの一点に絞られる。このmethodを使うにはApp設定でAgents機能を有効化し、manifestで`agent_view`を指定する必要があり、**Agent viewの利用には有料のSlack planが必要**である。個人利用のworkspaceを有料化する判断はMVPの範囲を超えるため、[コスト方針](#コストと日常運用)に従って見送り、`eyes` reactionで代替する。Block Kitを使わないのは`markdown_text`で足りるからであり、理由が異なる。

有料planのworkspaceを使える場合、または学習目的でSlack Developer Programの無料sandboxを使う場合は、採用を再評価する。その際は次の差分を確認する。

- `agent_view`への切り替えは元に戻せない。新規appは`agent_view`のみ選択できる。
- 会話がMessages tab上のtimelineへ移り、`message.im`は常に`thread_ts`を伴う。[会話履歴](architecture.md#会話履歴)の「トップレベルDMは新しい会話」という境界の作り方を見直す必要がある。
- event subscriptionに`app_home_opened`と`app_context_changed`が加わる。
- statusには2分のtimeoutがあり、それまでにメッセージを送らないと消える。

Hosted AgentはResponses protocolのsource-code deploymentを使う。現在の`azd`ではrepository rootの`azure.yaml`がFoundry project、model deployment、Hosted Agent serviceとdeploy設定の正本であり、Bicepを使うこのprojectでは`infra.provider: bicep`と`infra.path: ./infra`で接続する。旧来の`agent.manifest.yaml` / `agent.yaml`を正本にしない。

owner境界は、`azure.yaml`がmodel deploymentとAgent serviceを定義し、BicepがresourceとRBACを作る形に固定する。Foundry extensionが`azure.yaml`から生成する`AI_PROJECT_DEPLOYMENTS`をBicep parameterへ渡すため、model名・version・SKU・capacityをBicepへ二重記載しない。Bicepが返すprojectのsecure outputをextensionがdeploy時に利用する。Cosmos endpointとquery embedding deployment名はBicepの`COSMOS_ENDPOINT` / `EMBEDDING_MODEL_DEPLOYMENT_NAME` outputからazd環境へ渡し、Agent serviceの同名`environmentVariables`へ注入する。chat用`AZURE_AI_MODEL_DEPLOYMENT_NAME`をembeddingへ流用しない。database `knowledge`とcontainer `chunks`は共有契約の固定値なので環境変数にしない。`codeConfiguration`を持つdirect source ZIP deploymentでは顧客ACRを作らない。

現行のAgent Framework sampleとextension schemaで確認したservice形状を、Step 1で既存fileを上書きせずrootの`azure.yaml`へ統合する。Agent serviceは`language: python`、`codeConfiguration.runtime: python_3_13`、`codeConfiguration.entryPoint: main.py`、Responses protocol `2.0.0`とする。Agent設定はlistの`environmentVariables`を使い、`FOUNDRY_PROJECT_ENDPOINT`はplatform注入に任せて重複定義しない。model deployment名と`COSMOS_ENDPOINT`だけを明示的に渡す。Agent sourceは`src/agent/`に置き、`requirements.txt`の生成方法は[ローカル開発](#ローカル開発)を正とする。

## ローカル開発

Functionsはローカルで実行し、Cosmos、Storage、Foundryは実resourceへ接続する。emulatorは使わない。個人利用の想定使用量ではFree Tierと低い従量課金の範囲に収まり、環境差分を持ち込まない方が単純である。

Slack eventのローカル受信には既存のCloudflare Tunnelを使い、開発中だけSlack AppのEvents API Request URLをTunnelへ向ける。Sync Functionを任意のタイミングで動かす場合はFunctionsのadmin API（`POST /admin/functions/{name}`）を使い、そのための手動同期用HTTP endpointをアプリへ追加しない。

依存はuvで管理し、FunctionsとHosted Agentが必要とする`requirements.txt`は`azd`のprepackage hookで`uv export`により生成する。生成した`requirements.txt`はcommitせず、lockfileを正とする。

## デプロイと復旧

`azd deploy`は新しいimmutable Agent versionへendpoint routingを自動設定する。split routingやdraft previewは採用しない。deploy後、Agent principalへの`chunks` container scopeのCosmos data-plane role assignmentをpost-deploy scriptで作成する。

rootの`postdeploy` hookは全serviceのdeploy後に一度だけ動き、Foundry extensionが生成した`AGENT_KNOWLEDGE_AGENT_RESPONSES_ENDPOINT`とBicep output由来の`SERVICE_FUNCTIONS_RESOURCE_NAME`、`AZURE_RESOURCE_GROUP`、azd標準の`AZURE_SUBSCRIPTION_ID`を受け取る。scriptはHTTPSのFoundry Responses endpointとsubscription UUIDを検証し、list / setの両方へsubscriptionを明示してAzure CLIのcurrent contextに依存しない。Function Appの`KNOWLEDGE_AGENT_ENDPOINT`と異なる場合だけ更新し、同値なら何も変更しない。値は標準出力・エラーへ出さず、値の欠落・不正、CLI失敗はhook全体を失敗させる。初回はFunctionsとAgentを含む`azd deploy`を使い、Agent未deployの状態でFunctionsだけをdeployして配線済みとは扱わない。

同じhookはbeta.9以降のextensionが出力する`AGENT_KNOWLEDGE_AGENT_INSTANCE_IDENTITY_PRINCIPAL_ID`をUUIDとして検証し、subscription、resource group、`COSMOS_ACCOUNT_NAME`、固定scope `/dbs/knowledge/colls/chunks`を明示してCosmos Built-in Data Readerを冪等作成する。Azure CLIとrole scriptのstdout / stderrは内部で破棄し、成功・失敗ともprincipal、scope、resource名を出さず固定文言だけを返す。role assignmentを先に完了し、その後にResponses endpointをFunction設定へ反映する。どちらかの値が欠落・不正、またはCLIが失敗した場合はendpointを公開せずhook全体を失敗させる。

deploy後にdevで一度だけ、Agent起動・Cosmos検索・Slack DMのスレッド返信を確認する。失敗時はtraceとログから原因を切り分け、修正して再deployする。自動・手動のrollback手順はMVPでは用意せず、必要性はMVP後に判断する。

## 参考

- [azure.yaml reference for hosted agents](https://learn.microsoft.com/azure/foundry/agents/concepts/azure-yaml-reference)
- [Agent FrameworkのFoundry Hosted Agent hosting](https://learn.microsoft.com/agent-framework/hosting/foundry-hosted-agent)
- [Hosted Agent deployment](https://learn.microsoft.com/azure/foundry/agents/how-to/deploy-hosted-agent)
- [Hosted Agent endpoints](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#agent-identity-and-endpoint)
- [azd hooks](https://learn.microsoft.com/azure/developer/azure-developer-cli/azd-extensibility)
- [Responses API](https://learn.microsoft.com/azure/foundry/openai/how-to/responses)
- [Cosmos DB vector search](https://learn.microsoft.com/azure/cosmos-db/nosql/vector-search)
- [Flex Consumption plan](https://learn.microsoft.com/azure/azure-functions/flex-consumption-plan)
- [Slack Events API](https://docs.slack.dev/apis/events-api/)
- [Verifying requests from Slack](https://docs.slack.dev/authentication/verifying-requests-from-slack)
- [Slack App manifests](https://docs.slack.dev/app-manifests/)
- [message.im event](https://docs.slack.dev/reference/events/message.im)
- [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/)
- [reactions.add](https://docs.slack.dev/reference/methods/reactions.add/)
- [assistant.threads.setStatus](https://docs.slack.dev/reference/methods/assistant.threads.setStatus/)
- [Developing an AI app](https://docs.slack.dev/ai/developing-ai-apps/)
