# 品質と観測

この文書は、MVPのtelemetry、content記録、smoke evaluationと、MVP後の品質施策の正本である。

## Telemetry

Foundry Projectに接続したApplication Insights / Log Analyticsを、FunctionsとHosted Agentの共通基盤とする。FunctionsとHosted Agentは同じApplication Insightsへ送信し、`service.name`またはagent名で識別する。FoundryのTraces / MonitorはAgentとevaluation、Application InsightsのTransaction search / LogsはFunctionやQueueを含む横断調査に使う。

Functionsは`host.json`の`telemetryMode: OpenTelemetry`とPython workerのOTelを使う。Hosted AgentはResponses protocol runtimeの自動計装を利用し、`knowledge_search`にはcustom spanを追加する。Hosted AgentのOTel設定はversion単位で不変なので、変更時は新versionを作る。

LINEとGitHubの各フローを一つの論理traceとして追跡する。HTTPはW3C Trace Contextを使い、Queue messageのtelemetry metadataへ`traceparent`、`tracestate`、correlation IDを置き、workerでextractしてconsumer spanを始める。業務payloadとは分離し、ユーザー情報・秘密情報をbaggageへ入れない。

| フロー | 主なspan |
|---|---|
| GitHub同期 | `github.webhook.receive`、`queue.publish`、`index.run`、`github.contents.fetch`、`embedding.create`、`cosmos.upsert` |
| LINE質問 | `line.webhook.receive`、`queue.publish`、`agent.invoke`、`knowledge.search`、`cosmos.vector_query`、`line.message.send` |

span名は固定の低カーディナリティ値にする。検索用にcorrelation ID、GitHub delivery ID / commit SHA、LINE event ID、queue状況、index version、モデル利用量、検索件数・上位score、Cosmosの時間/RUを属性またはログに残す。ログにはTrace IDとSpan IDを付ける。

## Content記録と保護

個人利用のMVPでは、`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`によりbuilt-in traceの質問・回答・tool入出力をApplication Insightsへ保存する。これはbuilt-in `gen_ai` telemetryの設定であり、custom spanの属性・引数・戻り値を安全にするものではない。

LINEの`replyToken`、GitHub token、Webhook secret、Authorization header、本文全文をcustom spanへ記録しない。LINE user IDが必要ならハッシュ化した識別子を使う。閲覧権限は自分のEntra IDと実行に必要なManaged Identityへ限定し、第三者OTLP backendへcontentを送信しない。保持期間とevaluation件数で保存費用を制御する。

## MVP評価

MVPでは通常traceを確認し、repository内でversion管理する固定dataset約10件によるsmoke evaluationを行う。検索品質と最終回答品質を分け、各caseにはquery、期待するsource、必要なら期待条件を持たせる。評価結果にはdataset version、agent version、embedding / chunking version、evaluator version、モデルversionを記録する。

評価はdeployのroutingを止める品質gateにはしない。失敗は検索、tool利用、生成のどこにあるかをtraceと根拠記事で確認し、改善材料として扱う。重要caseだけ複数回実行して非決定性を確認する。

初期に観察する指標はWebhook 2xx率、Function/Agent/LINE送信の失敗率、Queue待機、Agent・vector検索・end-to-endの時間、最終同期時刻、検索結果なし率、引用付き回答率、token/RU/telemetry取り込み量である。数値thresholdは実測後に定める。

## MVP後

production trace evaluation、recurring / continuous evaluation、scheduled trace evaluation fallbackはMVP後に検討する。これらにはpreview機能を含むため、導入時に運用負荷と価値を再評価する。必要なRBACは[architecture.md](architecture.md#hosted-agentとidentityrbac)を正とする。

## 参考

- [Hosted Agent telemetry](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)
- [Trace evaluation requirements](https://learn.microsoft.com/azure/ai-foundry/how-to/develop/cloud-evaluation#trace-data-requirements)
- [Functions OpenTelemetry](https://learn.microsoft.com/azure/azure-functions/opentelemetry-howto)
