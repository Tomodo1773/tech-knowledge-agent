# 品質と観測

この文書は、MVPのtelemetry、content記録、smoke evaluationと、MVP後の品質施策の正本である。

## Telemetry

Foundry Projectに接続したApplication Insights / Log Analyticsを、FunctionsとHosted Agentの共通基盤とする。FunctionsとHosted Agentは同じApplication Insightsへ送信し、`service.name`またはagent名で識別する。FoundryのTraces / MonitorはAgentとevaluation、Application InsightsのTransaction search / LogsはFunctionやQueueを含む横断調査に使う。

Functionsは`host.json`の`telemetryMode: OpenTelemetry`とPython workerのOTelを使う。Hosted AgentはResponses protocol runtimeの自動計装を利用し、`knowledge_search`にはcustom spanを追加する。Hosted AgentのOTel設定はversion単位で不変なので、変更時は新versionを作る。

LINE質問は複数プロセスにまたがるため、一つの論理traceとして追跡する。HTTPはW3C Trace Contextを使い、Queue messageのtelemetry metadataへ`traceparent`、`tracestate`、correlation IDを置き、workerでextractしてconsumer spanを始める。業務payloadとは分離し、ユーザー情報・秘密情報をbaggageへ入れない。GitHub同期は単一Function実行で完結するため、propagationを跨ぐ必要はない。

| フロー | 主なspan |
|---|---|
| GitHub同期 | `github.sync.run`、`github.tree.fetch`、`github.contents.fetch`、`embedding.create`、`cosmos.upsert` |
| LINE質問 | `line.webhook.receive`、`queue.publish`、`agent.invoke`、`knowledge.search`、`cosmos.vector_query`、`line.message.send` |

span名は固定の低カーディナリティ値にする。検索用にcorrelation ID、GitHub commit SHA、LINE event ID、queue状況、再index件数、モデル利用量、検索件数・上位score、Cosmosの時間/RUを属性またはログに残す。ログにはTrace IDとSpan IDを付ける。

## Content記録と保護

個人利用のMVPでは、`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true`によりbuilt-in traceの質問・回答・tool入出力をApplication Insightsへ保存する。これはbuilt-in `gen_ai` telemetryの設定であり、custom spanの属性・引数・戻り値を安全にするものではない。Responses APIの`store: true`により、質問と回答はFoundry projectにも保存される。

LINEのWebhook secret、Authorization header、本文全文をcustom spanへ記録しない。LINE user IDが必要なら[architecture.md](architecture.md#会話履歴)と同じハッシュ化した識別子を使う。閲覧権限は自分のEntra IDと実行に必要なManaged Identityへ限定し、第三者OTLP backendへcontentを送信しない。保持期間とdaily capで保存費用を制御する。

## MVP評価

MVPでは通常traceを確認し、repository内でversion管理する固定dataset約10件によるsmoke evaluationを行う。各caseはquery、期待するsource記事を持ち、実行scriptは回答と引用を出力する。判定は引用に期待記事が含まれるかの確認と目視で足りる。LLM judgeとevaluator versionの記録はMVPでは行わない。

評価はdeployを止める品質gateにはしない。失敗は検索、tool利用、生成のどこにあるかをtraceと根拠記事で確認し、改善材料として扱う。

初期に観察する指標はLINE Webhook 2xx率と`request_timeout`件数、GitHub同期・Function・Agent・LINE送信の失敗率、Agent・vector検索・end-to-endの時間、最終同期時刻、検索結果なし率、引用付き回答率、token / RU / telemetry取り込み量である。数値thresholdは実測後に定める。

## MVP後

LLM judgeによる自動採点、production trace evaluation、recurring / continuous evaluation、evaluation結果のversion管理はMVP後に検討する。これらにはpreview機能を含むため、導入時に運用負荷と価値を再評価する。必要なRBACは[architecture.md](architecture.md#hosted-agentとidentityrbac)を正とする。

## 参考

- [Hosted Agent telemetry](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)
- [Functions OpenTelemetry](https://learn.microsoft.com/azure/azure-functions/opentelemetry-howto)
