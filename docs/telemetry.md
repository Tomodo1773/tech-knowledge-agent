# Telemetry

この文書は、FunctionsとHosted Agentのtelemetry設計の正本である。content記録の可否とMVP評価は[quality.md](quality.md)、Azureの採用設定と運用は[platform-and-operations.md](platform-and-operations.md)を参照する。

## 観測で答える問い

設計はこの問いに答えられるかで評価する。答えられない設計は採らず、答えるのに複数の画面を突き合わせる必要がある設計は減点する。

| # | 問い | 頻度 |
|---|---|---|
| Q1 | Slackの1質問はどの段階で失敗したか（受信 / queue / Agent / 検索 / 送信） | 障害時 |
| Q2 | なぜ失敗したか（認証 / timeout / throttling / データ不整合） | 障害時 |
| Q3 | 回答は何を根拠にしたか（検索が何件、どの記事、distance） | 評価時・日常 |
| Q4 | 遅いのはどこか（Agent / embedding / Cosmos / Slack） | 日常 |
| Q5 | GitHub同期は成功したか、何件処理したか、最後はいつか | 日次 |
| Q6 | 取り込み量はcapに収まっているか | 週次 |
| Q7 | smoke evaluationの結果をtraceと突き合わせられるか | 評価時 |

前提は個人利用であり、質問は1日数件から数十件、同期は1日1回である。Log Analyticsのdaily capは0.1 GB、retentionは30日で、このcapの小ささは以降の判断すべてに効く設計制約である。

## signalの役割分担

spanを主体とし、logはspanが構造上運べないものだけを運ぶ。custom metricは持たない。

| signal | 役割 | 根拠 |
|---|---|---|
| span | Q1 / Q3 / Q4 / Q5 / Q7に答える | 1質問が複数processを跨ぐ。親子関係と時間を持つのはspanだけで、属性allowlistという安全装置もspan側にある |
| log | Q2のうち、appが例外を置き換えて情報を捨てる箇所だけ | spanのstatusは失敗したことしか言えず、原因の識別子は例外の型にしか残らない |
| custom metric | 持たない | Q6はApplication Insightsの使用量とdaily capで足りる。metricを足すとcapを自分で消費する |

telemetryを追加するときの判断基準は一つで、その事実がspanに載るかを先に問う。載るならlogに書かない。spanとlogへ同じ事実を二重に持たせない。

## 基盤

Foundry Projectに接続したApplication Insights / Log Analyticsを、FunctionsとHosted Agentの共通基盤とする。両者は同じApplication Insightsへ送信し、`service.name`またはagent名で識別する。FoundryのTraces / MonitorはAgentとevaluation、Application InsightsのTransaction search / LogsはFunctionやQueueを含む横断調査に使う。Agent固有の運用指標は、Application Insightsの「Agents (Preview)」がAgent Framework由来のspanから組み立てる。

Functionsは`host.json`の`telemetryMode: OpenTelemetry`とPython workerのOTelを使う。Hosted AgentはResponses protocol runtimeの自動計装を利用し、`knowledge_search`にはcustom spanを追加する。Hosted AgentのOTel設定はversion単位で不変なので、変更時は新versionを作る。

## trace

### 伝播

Slack質問は複数プロセスにまたがるため、一つの論理traceとして追跡する。HTTPはW3C Trace Contextを使い、Queue messageのtelemetry metadataへ`traceparent`と任意の`tracestate`を置き、workerでextractしてconsumer spanを始める。Slackの`eventId`は業務上の重複排除キーとend-to-endのcorrelation IDを兼ね、別fieldを追加しない。ユーザー情報・秘密情報をbaggageへ入れない。GitHub同期は単一Function実行で完結するため、propagationを跨ぐ必要はない。

workerからHosted Agentを呼ぶHTTPだけは`extra_headers`で`traceparent`を明示的に載せる。OpenAI clientはhttpxを使い、Azure Monitor distroはhttpxを自動計装しないため、放置するとheaderが出ずFoundryがAgentを別traceで開始する。Foundryはこのheaderをcontainerへ転送するので、明示するだけでAgent側のspanが`agent.request`にぶら下がる。

| フロー | 主なspan |
|---|---|
| GitHub同期 | `github.sync.run`、`github.tree.fetch`、`github.contents.fetch`、`embedding.create`、`cosmos.upsert` |
| Slack質問 | `slack.event.receive`、`queue.publish`、`agent.request`、`knowledge.search`、`cosmos.vector_query`、`slack.message.send` |

### SDKの自動計装は止めない

1操作は`queue.publish`（自作）、`QueueClient.send_message`（SDK）、`Azure queue: <account>/slack-questions`（HTTP層）のように3層で出る。同じ1操作なので減らしたくなるが、Function Appのapp設定`OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`で`azure_sdk`を止める選択は採らない。

理由は、消える側が規約準拠のspanだからである。Application Insightsのdependency型は、`az.namespace`を持つAzure SDK spanなら`Microsoft.Storage`や`Microsoft.DocumentDB`、`db.system`や`messaging.system`を持つなら`Cosmos DB`や`Queue Message`になる。属性を持たない自作spanは、INTERNALなら`InProc`、CLIENTなら`N/A`にしかならない。SDK計装を止めると、型の付くspanを消して型の付かないspanだけが残り、Application Mapから外部依存のnodeが消える。

量も測った。三層のままでdependencyは24時間で1,522行である。内訳は`HTTP` 1,003、`InProc` 263、`N/A` 207、`Azure table` 33、`Queue Message` 8、`Azure queue` 8で、型が付いているのはすべてSDK span由来、自作spanはすべて`InProc`か`N/A`だった。0.1 GB/日のcapは実測で約7万行に相当する（capに触れていた24時間が、trace 67,639 + dependency 1,504 + request 192だった）。三層のままのdependency 1,522行はその約2%にすぎない。capを食っていたのはlog channelの自己増殖のほうである。減らす動機がないので、三層はこのまま残す。

Hosted Agent側も同じ三層構造を出しているが、そこで計装しているのはFoundry runtimeであり、`src/agent`は`opentelemetry-api`しか依存に持たない。Function App側の設定はAgentには効かない。

### 命名

自作spanは独自命名を維持し、`gen_ai.*`属性を付けない。OTelのsemantic conventionsへ寄せる具体的な見返りは「Agents (Preview)」が点灯することだが、この画面は2026-08-13の実測時点でAgent Framework由来のspanだけで完全に成立しており、エージェント実行数、生成AIエラー、tool呼び出し、model呼び出し、tokenのいずれもFunction側のspanを必要としていない。

見返りがない一方で、壊す側のリスクは残る。この画面はmain agentとsubagentを区別し、distro側にもmain agent帰属の処理がある。Function側のspanがgen_aiのinvoke_agent操作を名乗ったときに実行回数が二重計上されるかは試していないが、いま正しく出ている画面を、得るもののない変更で危険に晒す理由がない。

Agent Framework由来の`chat {model}`と`execute_tool {name}`は既に規約準拠であり、自作していないので触らない。Function側の`agent.request`は、platform側のserver span `invoke_agent`と名前の語順が逆で紛らわしかったため改名した経緯を持つ。両者は別物で、`agent.request`はFunctionから見た送信要求の所要時間を測り、`knowledge.conversation_continued`と`knowledge.response_id`を持ち、traceparent注入の親になる。

### 属性

span名は固定の低カーディナリティ値にする。検索用にSlack `eventId`をcorrelation IDとして、GitHub commit SHA、queue状況、再index件数、モデル利用量、検索件数・最小distance、Cosmosの時間 / RUをspan属性に残す。属性キーはallowlistで固定し、質問文、回答、token、Slackヘッダは載せない。これらはspanが持つ値であり、logへ重ねない。

## log channel

### 収集範囲

Python workerは`configure_azure_monitor(logger_name=...)`へapp設定`PYTHON_APPLICATIONINSIGHTS_LOGGER_NAME`の値をそのまま渡し、distroはその一つのlogger subtreeだけを収集する。既定値は空文字、つまりroot loggerで、process内の全libraryが対象になる。収集範囲はappの名前空間`knowledge_agent`だけとし、他は収集しない。

| 収集しないもの | 理由 |
|---|---|
| `azure.core`のHTTP log、Cosmos / Table / QueueのSDK log | 同じ1操作を自作spanとSDK spanの両方が既に記録している。logにも持たせると三重の上にもう一層積むことになる |
| Functions workerのinvocation log | host processがrequestとして独立に記録する。`PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY`はこの二重化を避ける設定である |
| `azure.monitor.opentelemetry.exporter`、`azure.core`のHTTP logging policy | levelを問わず収集できない。送信成功はINFO、送信失敗の再試行はWARNINGで書かれるため、収集すると送信がlogを生み、そのlogが次のbatchで送られてまたlogを生む |

3行目が、rootを収集してWARNINGで絞るという中間案を採らない理由である。INFOのループは平常時に回り、WARNINGのループはexporterが失敗しているとき、つまりtelemetryを最も失いたくない場面で回る。ループを止めるのはlevelではなく収集範囲である。distroのREADMEも`logger_name`について "Setting this value is imperative so logs created from the SDK itself are not tracked." と書いている。この設定を欠いた24時間で62,519行のノイズが出て、実trace 1,593件を押し下げ、dependencyの約22%がdaily capで欠落した。

### 何を書くか

現時点で条件を満たすのは一つだけである。

| 事象 | level | なぜspanで足りないか |
|---|---|---|
| Hosted Agent呼び出しの失敗（例外クラス名のみ） | ERROR | `HostedAgentClient.ask`は`from None`で原因を捨ててhost logへresponse本文が出ないようにしている。spanにはerror statusが残るが、認証失敗かtimeoutか429かはここで消える |

Slack eventの受理・拒否は同じ値が`knowledge.audit_reason`としてspan属性にあるため、logには書かない。結果としてFunction Appがtelemetryへ出すlogはこの1種類だけになる。構造はspanが持っているので、これは設計どおりの姿である。

### どこから出すか

収集されるsubtreeは`knowledge_agent` packageそのものなので、package内のmoduleが`logging.getLogger(__name__)`で得たloggerから出す。追加の仕組みは要らない。log levelはどのloggerについても変更しない。

entry pointの`function_app.py`はpackageの外にあり`__name__`がsubtreeに入らないため、ここからのlogはlevelを問わずtelemetryに届かない。相関が切れるのではなく届かないので、entry pointがloggerを作らないことと、rootのlogging関数を誰も呼ばないことをtestで検査する。

log messageにはspan属性のようなallowlistが効かない。質問文、回答、Slack secret、SDKのresponse本文をmessageへ書かず、識別子、結果値、例外クラス名だけを書く。Trace IDとSpan IDは手で付けない。distroが付けるhandlerがrecordを`context=get_current()`で組み立てるため、span内で出したrecordは既に相関している。

### host processのlog

`PYTHON_APPLICATIONINSIGHTS_LOGGER_NAME`はworker processにしか効かない。host processのlogは`host.json`の`logging`で絞る。ここをOpenTelemetry providerに限って`Warning`にする。

```json
"logging": { "OpenTelemetry": { "logLevel": { "default": "Warning" } } }
```

hostのInformation logは、instanceが起動するたびに出す設定ダンプ（`ScriptJobHostOptions`、`QueuesOptions`、`Starting JobHost`など）が大半で、1 instanceあたり約27行になる。Flex Consumptionはscale to zeroするので起動が頻繁で、実測ではrequest 5件に対しhostのlogが134行出た。invocationの成否と所要時間は`requests`にあるため、これをlogとしても持つ意味がない。providerを限定しているので、console側のlog levelは変わらない。

### SDKのWARNING / ERRORを捨てる

名前空間の外のlogはWARNINGもERRORも届かない。これを許容する。SDKの失敗を知る必要が出たら、収集範囲を広げるのではなく、appがcatchして自分の名前空間へ書く。message、level、内容を自分で決められる点でも望ましい。

調査でSDKの生logがどうしても要る場合だけ、`PYTHON_APPLICATIONINSIGHTS_LOGGER_NAME`を一時的に必要なsubtree（`azure.cosmos`、`openai`など）へ向けて再現し、終わったら戻す。空文字と`azure`は上表3行目を含むので指定しない。

## 量とコスト

sampling率は下げず100%のままにする。ノイズを断った後の取り込み量は従来の1割以下になり、個人利用の規模では数件のtraceを完全な形で追えることのほうが価値が高い。量の上限はdaily cap 0.1 GBとretention 30日で持つ。

`telemetryMode: OpenTelemetry`は`host.json`の`logging.applicationInsights`セクションを無効にし、既定モードのadaptive samplingもここに属するため、OTelモードではsamplingを自分で決める必要がある。再びcapに触れた場合は、Function Appのapp設定へ`OTEL_TRACES_SAMPLER=microsoft.rate_limited`と`OTEL_TRACES_SAMPLER_ARG`（1秒あたりのtrace数）を置いて上限を作る。

Python workerの`azure-monitor-opentelemetry`依存は外さない。[Functions OpenTelemetry](https://learn.microsoft.com/azure/azure-functions/opentelemetry-howto)のPython手順はこのpackageの追加を指示し、`PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY=true`があれば`configure_azure_monitor()`の記述を省けるとしている。同文書がworkerでdistroを避けよと書いているのは、.NETのAspNetCoreInstrumentationのようなrequest計装がhostのrequest telemetryと二重になる場合であり、この構成の問題とは別である。

resource診断設定はKey Vaultの`AuditEvent`だけをLog Analyticsへ流す。Storage、Cosmos、Function App、Foundry accountには診断設定を作らない。AVMの既定は`allLogs`であり、Storageならblob / queue / tableの全トランザクション、Cosmosなら全`DataPlaneRequests`が1行ずつ入る。これは上表のspanと重複するうえ、daily capを先に使い切って本来見たいtelemetryを止める。platform metricsは診断設定なしでAzure Monitorから参照できる。Key Vaultの`AuditEvent`だけは同等のmetricがなく、Function AppのKey Vault参照が解決できないときの切り分けに要るため残す。

## deploy後の検証

構成を変更したら次を確認する。すべて満たすまで直ったと扱わない。

1. all-zero trace IDのlog行が消えている。残ってよいのはhost processの起動ログだけで、これは再起動のたびに数十行出る。
2. `dependencies`の`sum(itemCount)`が`count()`と一致している（capによる間引きが止まっている）。
3. 「Agents (Preview)」が引き続き成立している。

## 参考

- [Hosted Agent telemetry](https://learn.microsoft.com/azure/foundry/agents/how-to/configure-hosted-agent-telemetry)
- [Functions OpenTelemetry](https://learn.microsoft.com/azure/azure-functions/opentelemetry-howto)
- [Azure Monitor OpenTelemetry Distro for Python](https://learn.microsoft.com/python/api/overview/azure/monitor-opentelemetry-readme)
