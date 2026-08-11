# 初期実装前の調査記録（2026-08-11）

これは2026-08-11時点の調査根拠、capacity snapshot、統合判断の記録である。現行設計の正本ではない。変更後の設計は[architecture.md](../architecture.md)、[platform-and-operations.md](../platform-and-operations.md)、[quality.md](../quality.md)、[implementation-plan.md](../implementation-plan.md)を参照する。

対象は当時READMEで定義したMVPであり、実装コード、Azure resource作成、外部サービス設定は含まない。

## 結論

実装開始可能と判断した。実装時に調整する項目と最小vertical sliceを分離し、個人MVPとして過剰な品質gateは採用しない。実resource作成の承認とdeploy直前のcapacity再確認を開始条件とした。

## T1. Hosted Agent、検索tool、deploy

- Python 3.13、Agent Frameworkの`ResponsesHostServer`、`main.py`、in-processの`knowledge_search` toolを採用した。
- `FoundryChatClient` / `ResponsesHostServer`は`store: false`とreasoning `max`を設定し、Responses API経路とcustom toolを使う。
- toolは`CosmosClient(DefaultAzureCredential())`によりHosted Agent identityで接続し、外部検索endpointを作らない。
- source-code deploymentは`azd ai agent init --deploy-mode code --runtime python_3_13 --entry-point main.py --dep-resolution remote_build`を起点とした。remote buildに問題があるときだけbundled packagesを検討する。
- Agent versionはimmutableで、`azd deploy`が新versionへroutingする。split routing / draft preview / 自動rollbackは採用せず、dev疎通失敗時に旧versionへ手動で100%戻す方針とした。
- Agent principal IDはdeploy後に判明するため、Cosmos data-plane reader roleの付与は保護されたCIのpost-deploy操作とした。

根拠: [Hosted Agent code deployment](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code)、[Responses API](https://learn.microsoft.com/azure/foundry/openai/how-to/responses)、[Agent Framework function tools](https://learn.microsoft.com/en-us/agent-framework/agents/tools/function-tools)、[Cosmos DB RBAC](https://learn.microsoft.com/en-us/azure/cosmos-db/how-to-connect-role-based-access-control)。

## T2. region、model、plan、費用

Japan Eastを第一候補、East US 2をcapacity不足時の候補とした。chat / agentは`gpt-5.6-luna` 2026-07-09、GlobalStandard、10K TPM、reasoning `max`、embeddingは`text-embedding-3-large` version `1`、GlobalStandard、10K TPM、3072次元とした。judgeは別deploymentを作らずLunaを共用する。

Cosmos DBはFree Tierをaccount作成時に有効化し、`chunks` containerにはdedicated provisioned throughput 1,000 RU/s、FunctionsはFlex Consumption / always-ready 0とした。GlobalStandardの地域外処理を許容し、Azure Budgetは月額目安1,000円の予測・実績通知のみとした。

当日のsnapshotではJapan East / East US 2のchatとembeddingでplatform / quota各1,000K、usage 0を確認した。これは予約ではないため、deploy前に再確認する。

## T3. LINE非同期応答

LINE Webhookは署名検証後、`webhookEventId`を一意キーにdurable job / outboxを保存して2秒以内に2xxを返し、workerがAgentを実行してPush Messageを送る方針とした。`replyToken`は一回限りかつ有効性が短いため、保存・利用しない。Loading APIは1:1のみbest effortとする。

Pushは同一payloadと`X-Line-Retry-Key`を使い、timeoutまたはHTTP 500だけを24時間以内で再試行する。2xx / 409は完了、4xxは非再試行とする。group / roomは監査記録だけを残し、job / outboxを作らない。

根拠: [Receiving messages](https://developers.line.biz/en/docs/messaging-api/receiving-messages/)、[Loading indicator](https://developers.line.biz/en/docs/messaging-api/use-loading-indicator/)、[Retry API requests](https://developers.line.biz/en/docs/messaging-api/retrying-api-request/)。

## T4. telemetryとevaluation

Foundry ProjectへApplication Insightsを接続し、Responses protocol runtimeのtraceを自動送信できることを確認した。`knowledge_search`はcustom spanで補い、設定変更はnew versionで行う。QueueではW3C trace contextをpayloadのtelemetry metadataに入れてworkerでextractする。

個人利用のため、built-in `gen_ai` message contentの記録を許容した。一方でcredentialをcustom telemetryへ渡さない。trace / recurring / continuous evaluationはpreviewを含むためMVP後へ回し、MVPでは通常traceと固定dataset約10件のsmoke evaluationだけにした。

根拠: [Hosted Agent telemetry](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)、[Trace evaluation requirements](https://learn.microsoft.com/azure/ai-foundry/how-to/develop/cloud-evaluation#trace-data-requirements)、[Functions OpenTelemetry](https://learn.microsoft.com/azure/azure-functions/opentelemetry-howto)。

## 統合判断

調査時点でローカルのZenn repositoryを確認し、Markdown 36件のうち`articles` 28件を同期対象とした。全対象のfront matter構造、画像参照の扱い、GitHub Appの最小権限、Cosmos vector policy、Queue / Tableの冪等性契約を確定した。

当初検討した「smoke成功後にrouting」「自動fallback」「continuous evaluation」は、個人MVPとして複雑さが価値を上回るため採用しなかった。deploy後の一度の疎通確認、手動rollback、固定datasetのsmoke evaluationへ縮小した。
