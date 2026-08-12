# 品質と観測

この文書は、MVPのtelemetry、content記録、smoke evaluationと、MVP後の品質施策の正本である。

## Telemetry

Foundry Projectに接続したApplication Insights / Log Analyticsを、FunctionsとHosted Agentの共通基盤とする。FunctionsとHosted Agentは同じApplication Insightsへ送信し、`service.name`またはagent名で識別する。FoundryのTraces / MonitorはAgentとevaluation、Application InsightsのTransaction search / LogsはFunctionやQueueを含む横断調査に使う。

Functionsは`host.json`の`telemetryMode: OpenTelemetry`とPython workerのOTelを使う。Hosted AgentはResponses protocol runtimeの自動計装を利用し、`knowledge_search`にはcustom spanを追加する。Hosted AgentのOTel設定はversion単位で不変なので、変更時は新versionを作る。

Slack質問は複数プロセスにまたがるため、一つの論理traceとして追跡する。HTTPはW3C Trace Contextを使い、Queue messageのtelemetry metadataへ`traceparent`と任意の`tracestate`を置き、workerでextractしてconsumer spanを始める。Slackの`eventId`は業務上の重複排除キーとend-to-endのcorrelation IDを兼ね、別fieldを追加しない。ユーザー情報・秘密情報をbaggageへ入れない。GitHub同期は単一Function実行で完結するため、propagationを跨ぐ必要はない。

| フロー | 主なspan |
|---|---|
| GitHub同期 | `github.sync.run`、`github.tree.fetch`、`github.contents.fetch`、`embedding.create`、`cosmos.upsert` |
| Slack質問 | `slack.event.receive`、`queue.publish`、`agent.invoke`、`knowledge.search`、`cosmos.vector_query`、`slack.message.send` |

resource診断設定はKey Vaultの`AuditEvent`だけをLog Analyticsへ流す。Storage、Cosmos、Function App、Foundry accountには診断設定を作らない。AVMの既定は`allLogs`であり、Storageならblob / queue / tableの全トランザクション、Cosmosなら全`DataPlaneRequests`が1行ずつ入る。これは上表のspanと重複するうえ、0.1 GB/日のdaily capを先に使い切って本来見たいtelemetryを止める。platform metricsは診断設定なしでAzure Monitorから参照できる。Key Vaultの`AuditEvent`だけは同等のmetricがなく、Function AppのKey Vault参照が解決できないときの切り分けに要るため残す。

span名は固定の低カーディナリティ値にする。検索用にSlack `eventId`をcorrelation IDとして、GitHub commit SHA、queue状況、再index件数、モデル利用量、検索件数・最小distance、Cosmosの時間/RUを属性またはログに残す。ログにはTrace IDとSpan IDを付ける。

## Content記録と保護

個人利用のMVPでは、Agent versionの環境変数`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`を固定で`true`にし、built-in traceの質問・回答・tool入出力をApplication Insightsへ保存する。公開可能な技術記事と個人の技術質問だけを入力する前提であり、credential、個人情報、業務上の非公開情報はSlack質問にも記事にも含めない。これはbuilt-in `gen_ai` telemetryの設定であり、custom spanの属性・引数・戻り値を安全にするものではない。Hosted Agentの外側のResponses protocolが質問・回答と会話履歴をFoundry側で管理する一方、Agent内部のmodel callは`store: false`とし、model layerへ重複保存しない。

Slack Signing Secret、Bot token、Authorization header、event本文全文をcustom spanへ記録しない。Slack workspace、user、channel、threadの識別が必要なら[architecture.md](architecture.md#会話履歴)と同じ組み合わせをハッシュ化して使う。閲覧権限は自分のEntra IDと実行に必要なManaged Identityへ限定し、第三者OTLP backendへcontentを送信しない。保持期間とdaily capで保存費用を制御する。

## MVP評価

MVPでは通常traceを確認し、repository内でversion管理する固定dataset約10件によるsmoke evaluationを行う。各caseはquery、期待するsource記事を持ち、実行scriptは回答と引用を出力する。判定は引用に期待記事が含まれるかの確認と目視で足りる。LLM judgeとevaluator versionの記録はMVPでは行わない。

評価はdeployを止める品質gateにはしない。失敗は検索、tool利用、生成のどこにあるかをtraceと根拠記事で確認し、改善材料として扱う。

初期に観察する指標はSlack eventの3秒以内2xx率、`http_timeout`による再送件数、GitHub同期・Function・Agent・Slack送信の失敗率、Agent・vector検索・end-to-endの時間、最終同期時刻、検索結果なし率、引用付き回答率、token / RU / telemetry取り込み量である。数値thresholdは実測後に定める。

## MVP後

LLM judgeによる自動採点、production trace evaluation、recurring / continuous evaluation、evaluation結果のversion管理はMVP後に検討する。これらにはpreview機能を含むため、導入時に運用負荷と価値を再評価する。必要なRBACは[architecture.md](architecture.md#hosted-agentとidentityrbac)を正とする。

## 参考

- [Hosted Agent telemetry](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)
- [Functions OpenTelemetry](https://learn.microsoft.com/azure/azure-functions/opentelemetry-howto)
