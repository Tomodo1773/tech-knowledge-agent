# アーキテクチャ

この文書は、MVPのアプリケーション、データ、identity/RBAC設計の正本である。Azureの採用設定と運用は[platform-and-operations.md](platform-and-operations.md)、観測は[telemetry.md](telemetry.md)、評価は[quality.md](quality.md)を参照する。

## 境界とフロー

Sync Function（Timer Trigger）、Slack Events Function（HTTP Trigger）、Agent Worker Function（Queue Trigger）の3関数は、単一のFunction Appにまとめる。Bicep module、deploy、Managed Identityも1つに統一し、複数Function Appへの分割は行わない。この結果、Function App MIはSyncのCosmos書き込み・embedding呼び出しからSlack eventのQueue書き込みまでを合わせ持つ。個人利用のMVPでは、least privilegeのための分割よりも構成の単純さを優先する。

### GitHub同期

Sync FunctionはTimer Triggerで毎日18:00 UTC（JST 03:00）に起動し、`run_on_startup=false`とする。past-due起動も省略せず通常と同じ全件reconcileを行い、同期を一つの実行で完結させる。同期用のQueue、job table、leaseは持たない。

1. 非公開GitHub repositoryのdefault branchのcommit SHAを、Key Vault由来のtokenで認証したGitHub APIで確認する。最終同期済みSHAと同じ場合もtreeと保存済みarticle manifestを照合し、path、blob SHA、chunking version、削除候補まで完全一致したときだけ何もせず終了する。`sourceRevision`はcommit固定URLの参照先であり差分keyには使わないため、無関係なcommitで全記事を再embeddingしない。
2. Git Trees APIを`recursive=1`で一度呼び、`articles/**/*.md`のpathとblob SHAの一覧を得る。
3. Cosmosから各記事の全chunk manifestを取得し、`id`、`sourcePath`、`sourceRevision`、`sourceBlobSha`、`chunkingVersion`が記事内で一貫し、`chunkIndex`が0からの重複なし連番であることと件数を検査してからtree側と突き合わせる。不整合は前回の複数batch書込みが途中で止まった状態として`needs_reindex`にし、先頭chunkだけで正常と判定しない。
4. 追加された記事、blob SHAが変わった記事、`chunkingVersion`が現行と異なる記事だけを再indexする。tree側に存在しない記事のchunkは削除する。
5. 最終同期SHAと実行結果を記録する。

blob SHAはGitが内容から決める識別子なので、自前のcontent hashは計算しない。この突き合わせはforce push、初回実行、前回実行の途中失敗のいずれでも同じ手順で収束するため、checkpointや部分再開を持たない。失敗した実行は次回Timerがやり直す。手動同期はMVPの必須機能にせず、Timerの再実行で復旧する。

### Slack質問

1. Slack Events Functionはraw request body、`X-Slack-Request-Timestamp`、`X-Slack-Signature`を使って署名と5分以内のtimestampを検証する。Events APIの`url_verification`はQueueへ入れず、その場でchallengeを返す。HTTP triggerは`authLevel: anonymous`とし、Request URLへ関数キーを載せない。保護は署名検証とallowlistで行う。
2. `event_callback`では、許可したworkspaceと利用者からの`message.im`であり、`subtype`と`bot_id`を持たない通常のテキストメッセージであることを確認する。外側の`event_id`を`event` tableへInsert Entityで書き込み、409なら再送とみなして何も投入せず2xxを返す。Insert成功時だけStorage Queueへ投入して2xxを返す。read-then-writeは使わず、Insertの成否そのものを排他とする。Insert成功後にQueue投入が失敗したeventは再送でも復旧しないが、利用者が質問し直せば足りるため補償処理は持たない。
3. Agent Worker Functionは処理開始時に`reactions.add`で`messageTs`が指す利用者メッセージへ`eyes`を付け、受け付けたことを示す。回答後も外さず、受信の記録として残す。この呼び出しはbest effortとし、失敗しても回答処理を止めない。追い質問では毎回その質問自身へ付くため、`already_reacted`にはならない。
4. Agent Worker FunctionがHosted Agentを呼び、Agentの`knowledge_search` toolがCosmosを検索する。
5. Agent Worker FunctionがSlack Web APIの`chat.postMessage`で、元メッセージを親とするスレッドへ回答する。`rootTs`は`event.thread_ts`があればその値、なければ`event.ts`とする。回答は`text`ではなく`markdown_text`で送り、Block Kitは採用しない。citationは重複を除いたMarkdown linkを末尾の共通`## Sources` blockへ置く。回答が4,000文字を超える場合は本文を先に切り詰め、source URLをすべて保持する。`unfurl_links`と`unfurl_media`は`false`にする。

`markdown_text`を使うのは、Agentが生成するのが標準Markdownだからである。`text`フィールドはSlack独自の`mrkdwn`として解釈され、`**太字**`はそのまま表示され、`[題名](URL)`はリンクにならない。根拠記事へのリンクを壊さないため、標準Markdownをそのまま受け付ける`markdown_text`を正とし、Agent出力をmrkdwnへ変換する処理は書かない。`markdown_text`は`text`・`blocks`と併用できないため、`text`は指定しない。上限は12,000文字だが、読みやすさのため4,000文字で整形する。

Slack Events APIは3秒以内に2xxを返せない場合、ほぼ即時、1分後、5分後に最大3回再送する。さらに、60分間の配信試行の95%超が失敗するとevent subscriptionが一時的に無効化される。Flex ConsumptionのPython Functionはコールドスタートで3秒を超えることがあるが、最初の要求でFunction Appが起動すれば後続の再送を処理できる可能性が高く、個人利用の頻度では無効化の閾値には届かない。`event_id`のInsertにより再送を重複投入させない。これは確実な配信保証ではないため、always-ready instanceはMVPでは設定せず、実際に困った場合だけ追加する。

重複、allowlist外、DM以外を含め、Slackへは常に2xxを返す。Slackは2xx応答を再送しないため、`X-Slack-No-Retry`ヘッダは使わない。

`eyes`のreactionは、回答までの十数秒から数十秒のあいだ画面が無反応になることへの最小限の手当てである。本来これに相当するのはSlackの`assistant.threads.setStatus`だが、採用可否は[プラットフォームと運用](platform-and-operations.md#slack-agent機能を採用しない理由)を正とする。reactionは失敗の切り分けにも使え、`eyes`が付いたのに回答が来なければ受信は成功しWorkerで落ちたと判断できる。

コールドスタートを短くするため、Function Appのトップレベルでは重い依存をimportしない。Cosmos、Foundry、Agent関連のSDKは各ハンドラの内部でimportし、Slack event受信の経路が他機能の依存を読み込まないようにする。

対象は単一workspaceで許可した利用者とのDMだけとする。`team_id`と`user`をallowlistで限定し、allowlist外は`unauthorized_source`、DM以外は`unsupported_conversation_type`の監査記録だけを残して2xxを返し、Queueへ投入しない。これはmodel tokenとFoundryへ保存されるcontentを想定外の相手に消費させないための制限でもある。allowlist値はdeploy時の設定として与え、実値を文書、repository、ログへ記録しない。Slack Appは自分のworkspaceへ手動installし、複数workspace向けOAuth install flowは持たない。

## Hosted Agentの呼び出し

WorkerはResponses endpointをURLとして受け取らず、`AIProjectClient.get_openai_client(agent_name=...)`にendpointを組み立てさせる。入力は`FOUNDRY_PROJECT_ENDPOINT`と、azure.yamlのservice名と一致する固定契約`knowledge-agent`の二つだけである。同じ値をSDKも手組みも同じ形（`{project}/agents/{name}/endpoint/protocols/openai` + `api-version=v1`、scope `https://ai.azure.com/.default`）へ落とすので、手組みは重複でしかない。

SDKに寄せる利点は三つある。deploy後にしか決まらない値が消えるので、endpointをFunction Appのapp設定へ書き戻すpostdeploy段が要らなくなる。endpoint文字列を検査していた設定validationも、project endpointの検査一本に減る。そして`AIProjectInstrumentor`のtraceparent注入hookは`get_openai_client()`が返したclientにしか付かないため、これを使うことがAgent側spanをこのtraceへ入れる条件になる（[telemetry.md](telemetry.md#伝播)）。

代償は`AIProjectClient(allow_preview=True)`が要ること。`agent_name`はpreview扱いで、`Foundry-Features`ヘッダが付く。agent名がazure.yamlと食い違えば存在しないagentを呼ぶだけで静かに壊れるので、一致は`check-infra-policy.ps1`が検査する。

## 会話履歴

Agent Worker FunctionがHosted AgentのResponses endpointに対するクライアントとなる。外側のResponses protocolが応答と会話履歴を管理し、Workerは呼び出しの戻りにあるresponse idを記録して、次の質問で`previous_response_id`として同じendpointへ渡す。Agent container内部の`FoundryChatClient`によるmodel callは`store: false`とし、model layerへ会話履歴を重複保存しない。自前で会話履歴を組み立てて毎回送る方式は採らない。

Table StorageにSlack threadごとの最新`responseId`と更新時刻だけを持つ。partition keyは`conversation`、row keyは`${teamId}:${channelId}:${rootTs}`をSHA-256でハッシュ化した値とする。

会話の境界はSlackのthread構造を一次的な根拠とする。トップレベルのDMは新しい会話を始め、同じ`rootTs`を持つスレッド内のメッセージだけが会話を継続する。利用者がどこへ返信したかが意思表示なので、内容や経過時間から話題の切れ目を推定しない。

経過時間による打ち切りは話題の境界判定ではなく、安全網として残す。最終更新から7日以上経過している場合は参照を捨て、同じSlack thread内でも新しい会話として開始する。狙いは、長寿命のスレッドでcontextとtokenが際限なく積み上がることの抑止と、Foundry側の保持期間を過ぎて無効化された`responseId`を渡さないことである。実際の保持期間はdeploy時に確認し、7日がそれを下回っていることを確かめる。

Hosted AgentのResponses protocolが管理する質問と回答はFoundry側に保存される。個人利用のMVPではこれを許容し、[quality.md](quality.md#content記録と保護)のcontent記録方針と同じ扱いとする。

## データソース契約

対象はGitHubのdefault branchにある`articles/**/*.md`の全件で、`published: true/false`を問わずmetadataとして保持する。`draft/**`、`x-articles/**`、rootの補助Markdown、非Markdownは対象外とする。`books/**/*.md`は内容追加時に別途有効化する。

`title`、`emoji`、`type`、`topics[]`、`published`を必須front matterとし、`published_at`は任意、`slug`はfilenameとする。必須項目の欠落またはparse失敗は補完しない。新規記事なら対象外としてerrorへ残し、他の追加・更新・削除は継続する。既存記事が不正化した場合だけ同期全体を失敗させ、書込み前に停止して旧chunkと同じtreeで見つけた削除候補を保持する。同期結果は部分継続したerrorと全体を停止したerrorを区別して記録する。各chunkにはcommit SHA固定のGitHub blob URLを`sourceUrl`に保存する。公開Zenn URLは将来のoptional metadataである。調査時の件数とfront matter確認結果は[調査記録](research/implementation-readiness-2026-08-11.md#統合判断)に残す。

MarkdownはCRLF / CRをLFへ正規化し、空本文を拒否する。chunkはUnicode文字数で最大1,600文字、直前chunkとのoverlapを最大200文字とし、`0 <= overlap < size`を常に満たす。code fence外のATX headingでsectionを分け、headingはmetadataへ保存する。fence内の`#`はheadingとして扱わず、段落とcode fenceは最大長以内なら分割しない。単一blockが最大長を超える場合だけ文字境界で強制分割する。chunk indexは記事ごとに0から始まる連番とする。

画像参照は本文に残すが、MVPではOCR・画像本文indexを行わない。`images/**`の変更はblob SHA比較の対象外なので、再indexを誘発しない。

同期対象は非公開repositoryとする。記事本文は公開しても問題ない内容だが、commit履歴には執筆過程が残るため、source repository自体は公開しない。読み取りにはKey Vaultへ置いたGitHub personal access tokenを使い、Function App MIがKey Vault参照で解決する。tokenはrepositoryのContents読み取りだけに絞ったfine-grained tokenを想定し、期限切れ時はKey Vaultのsecretを差し替える。owner、repository、default branchはdeploy時の非機密設定として与え、実値をこの文書へ記録しない。

非公開repositoryは`raw.githubusercontent.com`からtokenで読めないため、記事本文はGit Blobs APIの`application/vnd.github.raw`で取得する。取得対象はtreeが返したblob SHAで content-addressed に決まるので、path encodingとrevisionの二重解決を持たない。citationの`sourceUrl`は従来どおりcommit固定のGitHub blob URLで、閲覧できるのはrepositoryへaccessできる本人だけである。

## 状態とメッセージ契約

Storage Queueと同じStorage AccountのTable Storageに`state` tableを一つ置き、次の3種類だけを保持する。outbox、relay、job status machineは作らない。

job storeを持たないため、Agent Workerが必要とする情報はQueue messageが運ぶ。messageはSlackの`eventId`、`teamId`、`userId`、`channelId`、`rootTs`、`messageTs`、質問文、telemetry metadataを持ち、Signing SecretとBot tokenは置かない。`rootTs`は返信先スレッドの識別、`messageTs`は`eyes`を付ける利用者メッセージ自身の識別に使う。トップレベルDMでは両者が一致し、追い質問では異なる。Slack IDとtimestampは送信先と会話識別に必要なので生の値を運ぶ。Queueは同じStorage Account内にあり、Managed Identityでのみ読み書きされ、保存時に暗号化される。

| partition | key | 保持する値 |
|---|---|---|
| `sync` | `github` | 任意の`lastSuccessfulSha`、`lastRunAt`、`lastRunResult`。結果語彙は`success`、新規不正記事だけを除外した`partial`、既存記事不正・transport/index失敗の`failed`に限定する。`partial`はheadを進め、`failed`は直前のSHAを保持する。初回成功前の失敗ではSHAを持たない |
| `event` | `{eventId}` | `receivedAt`。Slack event再送の重複投入抑止だけに使う |
| `conversation` | `{threadKeyHash}` | `responseId`、`updatedAt` |

Queue messageのwire keyはcamelCaseとし、次の形だけを許可する。`eventId`はSlack eventの重複排除キーであると同時に、受信から回答までを検索するcorrelation IDとして使う。別のcorrelation IDを重複して運ばない。`telemetry.tracestate`は任意、それ以外は必須である。未知のfieldを拒否することで、Signing SecretやBot tokenを誤ってQueueへ載せない。

```json
{
  "eventId": "...",
  "teamId": "...",
  "userId": "...",
  "channelId": "...",
  "rootTs": "...",
  "messageTs": "...",
  "question": "...",
  "telemetry": {
    "traceparent": "...",
    "tracestate": "..."
  }
}
```

固定resource名はQueue `slack-questions`、Table `state`、Cosmos database `knowledge`、container `chunks`、corpus `default`、Hosted Agent `knowledge-agent`とする。共有する設定名は`AZURE_STORAGE_ACCOUNT_NAME`、`COSMOS_ENDPOINT`、`FOUNDRY_PROJECT_ENDPOINT`、`EMBEDDING_MODEL_DEPLOYMENT_NAME`、`GITHUB_OWNER`、`GITHUB_REPOSITORY`、`GITHUB_DEFAULT_BRANCH`、`SLACK_ALLOWED_TEAM_ID`、`SLACK_ALLOWED_USER_ID`、`SLACK_SIGNING_SECRET`、`SLACK_BOT_TOKEN`、`CHUNKING_VERSION`である。

実装上の正確なkey、型、固定値は[`contracts.py`](../src/functions/knowledge_agent/contracts.py)と[`contracts.json`](../src/functions/tests/fixtures/contracts.json)をunit testで照合する。timestampはUTCのISO 8601、Git SHAは40文字の小文字hex、`threadKeyHash`は`${teamId}:${channelId}:${rootTs}`のUTF-8文字列に対するSHA-256小文字hexとする。

Queue Triggerの標準再試行とpoison queueを使い、独自のrelayや24時間再試行は作らない。`host.json`でQueue Triggerの`batchSize`を1にし、同じSlack threadへの連投で`previous_response_id`の読み書きが競合して会話が分岐することを避ける。Agent Workerが最終的に失敗した場合は再送や代替通知を行わず、次回の利用者メッセージで再試行する。

## Cosmos DB検索ストア

NoSQLの`chunks` containerを一つ作り、partition keyは`/corpusId`、MVP値は`default`に固定する。一corpusを単一logical partitionに置き、cross-partition vector retrievalを避ける。複数corpusまたは20 GB超の見込みが生じた時点で新containerへの移行を判断する。

`articleId`はfront matterの`slug`と同じ値にする。各chunkは`id = ${articleId}:${chunkIndex}`、`corpusId`、`articleId`、`chunkIndex`、`slug`、`title`、`emoji`、`articleType`、`topics`、`published`、nullableな`publishedAt`と`heading`、`sourcePath`、`sourceUrl`、`sourceRevision`、`sourceBlobSha`、`chunkingVersion`、`indexedAt`、`text`、`embedding`を持つ。`sourceBlobSha`と`chunkingVersion`は差分判定のキーである。記事更新は新chunkのupsertと余剰旧chunkのdeleteを一つの順序付きoperation列にする。100 operations以下かつSDK overheadを含む保守的な見積りが1.8 MiB以下の小記事は、同一logical partitionの一つのtransactional batchでatomicに置換する。どちらかの上限を超える記事だけを、高い`chunkIndex`からのupsert、残りのupsert、余剰deleteの順で複数batchへ分割する。途中で失敗した場合は全chunk manifestの連番・metadata不整合を次回Timerが検出して記事全体を再実行し、checkpointは持たない。削除時は該当articleのchunkを同じ上限で分割して削除する。

embedding deploymentのmodel、version、SKU、TPMは[プラットフォームと運用](platform-and-operations.md#採用設定)を正とする。vector fieldは`/embedding`、1536次元、`float32`、cosine、`quantizedFlat`とし、`/embedding/*`を通常indexから除外する。vector policy/indexはimmutableであり、MVPで1,000 vector未満のfull scanを許容する。

Cosmosのcosine値はdistanceであり、小さい値ほどqueryに近い。`knowledge_search`はdistance昇順を正とし、同値はCosmosから受け取った順序を保つ。記事のtitleと本文は信頼しないdataとして扱い、tool出力では明示的なuntrusted noticeとJSON文字列境界の内側へ置く。記事本文に含まれる命令、Markdown heading、delimiterをsystem / developer / tool指示へ昇格させない。citationだけは検証済みのcommit SHA固定GitHub URLから末尾の`## Sources` blockを組み立てる。

## Hosted Agentとidentity/RBAC

Hosted AgentはPython 3.13のAgent Frameworkで`ResponsesHostServer`を起動する。ChatはResponses protocol経路を使い、Agent内部の`FoundryChatClient`は`default_options={"store": false}`とする。会話履歴は外側のResponses protocolへ一元化する。reasoning effortは既定値で始め、回答品質が不足する場合だけ引き上げる。`knowledge_search`は同一processの`@tool`で、Bicep outputからazd経由で注入した`COSMOS_ENDPOINT`を`CosmosClient(DefaultAzureCredential())`へ渡し、Agent identityで検索する。tool instanceの生涯呼出し上限は置かず、Frameworkのrequest単位`max_function_calls = 3`でrunawayを抑止する。query embeddingはchat設定とは別の`EMBEDDING_MODEL_DEPLOYMENT_NAME`を使う。database `knowledge`とcontainer `chunks`は共有契約の固定値を使う。外部検索endpointは作らない。取得本文はuntrusted dataとして扱い、その中の命令を無視し、検証済みcommit URLだけを引用する。根拠不足時は断定しない。

| principal | 必要な権限 |
|---|---|
| Function App MI | Storage Blob Data Owner、Storage Queue Data Contributor、Storage Table Data Contributor、`chunks` container scopeのCosmos DB Built-in Data Contributor、Key Vault Secrets User、Foundry project scopeのFoundry Agent Consumer、Foundry account scopeのCognitive Services OpenAI User、Application Insights scopeのMonitoring Metrics Publisher |
| Hosted Agent identity | `chunks` container scopeのCosmos DB Built-in Data Reader、Foundry account scopeのCognitive Services OpenAI User、Application Insights scopeのMonitoring Metrics Publisher |
| Foundry Project MI | Foundry account scopeのFoundry User、Log Analytics workspace scopeのLog Analytics Data Reader、Application Insights scopeのMonitoring Metrics Publisher |
| deploy実行者 | Foundry project scopeのFoundry Project Manager、Key Vault Secrets Officer、`chunks` container scopeのCosmos DB Built-in Data Contributor、Storage Blob / Queue / Table Data Contributor |

deploy実行者へのdata-plane roleは、local authを無効にした結果として必要になるものである。Key Vault Secrets Officerがなければ[Bootstrap](platform-and-operations.md#bootstrap)のsecret投入ができず、CosmosとStorageのdata-plane roleがなければ[ローカル開発](platform-and-operations.md#ローカル開発)とliveゲートでの状態確認ができない。role assignmentの作成自体にOwnerまたはRBAC Administratorが要るため、これらは実行者が自分で付与できる権限を明示化したものであり、権限の拡大ではない。範囲はFunction App MIと同じcontainer scopeに揃え、Storageは実行者だけContributorにする。

Function App MIがproject scopeで行うのはAgent Workerのagent呼び出し（agent endpointへの`responses.create`）だけである。公式はこのruntime interactionに必要なdata actionを`Microsoft.CognitiveServices/accounts/AIServices/endpoints/interact/action`と定め、それだけを持つ`Foundry Agent Consumer`をagentを作成・変更しないprincipalの最小権限roleとして挙げている（[Hosted agent permissions reference](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agent-permissions)）。Workerはagentを作らないため、`Microsoft.CognitiveServices/*`を含む広い`Foundry User`ではなくこのroleを使う。Foundry Project MIはproject endpointからaccountのmodel deploymentを呼ぶためFoundry Userを使う。role名とscopeの根拠は[実装開始時の現行仕様確認](research/implementation-current-spec-2026-08-11.md#rbac差分)に記録する（同記録はFunction App MIについてFoundry Userと書いた時点のもので、現行はこの節を正とする）。

### embeddingがaccount scopeを要求する理由

OpenAI v1 APIとしてMicrosoftが文書化しているinference endpointは`https://<resource>.services.ai.azure.com/openai/v1/`であり、**project-scopedなinference routeは文書化されていない**（[Endpoints for Microsoft Foundry Models](https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-models/concepts/endpoints)）。SDKの`AIProjectClient.get_openai_client()`が既定で組み立てるproject route（`/api/projects/<project>/openai/v1/`）はchat completionsを返すが、embeddingsには**deployment名の実在にかかわらず本文が空の404**を返す。同じproject routeのchatに存在しないdeployment名を投げると`DeploymentNotFound`が返るので、この404はdeployment解決の失敗ではなくroute自体がoperationを持たないことを示す。したがってembeddingはaccount rootへ向ける。

RBACはaccountからprojectへは継承されるが、**projectからaccountへは遡らない**。Hosted Agent identityがprojectに対して持つimplicit access、およびFunction App MIのproject scopeのFoundry Agent Consumerは、いずれもaccount rootのinferenceに届かない。そのため両者へFoundry account scopeの`Cognitive Services OpenAI User`を別途付ける。embeddingを許す最小のroleであり、広いFoundry Userをaccount scopeへ広げる代わりに使っている。

Sync Functionのembedding呼び出しと、Agentの`knowledge_search`が検索前に行うquery embeddingの両方がこの経路を通る。付与が漏れると前者はindex更新の失敗、後者は検索そのものの失敗として現れる。

Agent principal IDはdeploy後に得るため、AgentへのCosmos data-plane role assignmentはローカルのpost-deploy scriptで作成する。詳細は[プラットフォームと運用](platform-and-operations.md#デプロイと復旧)を参照する。
