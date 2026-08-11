# アーキテクチャ

この文書は、MVPのアプリケーション、データ、identity/RBAC設計の正本である。Azureの採用設定と運用は[platform-and-operations.md](platform-and-operations.md)、観測と評価は[quality.md](quality.md)を参照する。

## 境界とフロー

Sync Function（Timer Trigger）、Slack Events Function（HTTP Trigger）、Agent Worker Function（Queue Trigger）の3関数は、単一のFunction Appにまとめる。Bicep module、deploy、Managed Identityも1つに統一し、複数Function Appへの分割は行わない。この結果、Function App MIはSyncのCosmos書き込み・embedding呼び出しからSlack eventのQueue書き込みまでを合わせ持つ。個人利用のMVPでは、least privilegeのための分割よりも構成の単純さを優先する。

### GitHub同期

Sync FunctionはTimer Triggerで日次起動し、同期を一つの実行で完結させる。同期用のQueue、job table、leaseは持たない。

1. 公開GitHub repositoryのdefault branchのcommit SHAを認証なしのGitHub APIで確認する。最終同期済みSHAと同じなら何もせず終了する。
2. Git Trees APIを`recursive=1`で一度呼び、`articles/**/*.md`のpathとblob SHAの一覧を得る。
3. Cosmosから`articleId`と`sourceBlobSha`の一覧を取得し、tree側と突き合わせる。
4. 追加された記事、blob SHAが変わった記事、`chunkingVersion`が現行と異なる記事だけを再indexする。tree側に存在しない記事のchunkは削除する。
5. 最終同期SHAと実行結果を記録する。

blob SHAはGitが内容から決める識別子なので、自前のcontent hashは計算しない。この突き合わせはforce push、初回実行、前回実行の途中失敗のいずれでも同じ手順で収束するため、checkpointや部分再開を持たない。失敗した実行は次回Timerがやり直す。手動同期はMVPの必須機能にせず、Timerの再実行で復旧する。

### Slack質問

1. Slack Events Functionはraw request body、`X-Slack-Request-Timestamp`、`X-Slack-Signature`を使って署名と5分以内のtimestampを検証する。Events APIの`url_verification`はQueueへ入れず、その場でchallengeを返す。HTTP triggerは`authLevel: anonymous`とし、Request URLへ関数キーを載せない。保護は署名検証とallowlistで行う。
2. `event_callback`では、許可したworkspaceと利用者からの`message.im`であり、`subtype`と`bot_id`を持たない通常のテキストメッセージであることを確認する。外側の`event_id`を`event` tableへInsert Entityで書き込み、409なら再送とみなして何も投入せず2xxを返す。Insert成功時だけStorage Queueへ投入して2xxを返す。read-then-writeは使わず、Insertの成否そのものを排他とする。Insert成功後にQueue投入が失敗したeventは再送でも復旧しないが、利用者が質問し直せば足りるため補償処理は持たない。
3. Agent Worker Functionは処理開始時に`reactions.add`で利用者メッセージへ`eyes`を付け、受け付けたことを示す。回答後も外さず、受信の記録として残す。この呼び出しはbest effortとし、失敗しても回答処理を止めない。
4. Agent Worker FunctionがHosted Agentを呼び、Agentの`knowledge_search` toolがCosmosを検索する。
5. Agent Worker FunctionがSlack Web APIの`chat.postMessage`で、元メッセージを親とするスレッドへ回答する。`rootTs`は`event.thread_ts`があればその値、なければ`event.ts`とする。回答は`text`ではなく`markdown_text`で送り、Block Kitは採用しない。回答は根拠記事のURLを末尾に残したまま4,000文字以内へ整形し、`unfurl_links`と`unfurl_media`は`false`にする。

`markdown_text`を使うのは、Agentが生成するのが標準Markdownだからである。`text`フィールドはSlack独自の`mrkdwn`として解釈され、`**太字**`はそのまま表示され、`[題名](URL)`はリンクにならない。根拠記事へのリンクを壊さないため、標準Markdownをそのまま受け付ける`markdown_text`を正とし、Agent出力をmrkdwnへ変換する処理は書かない。`markdown_text`は`text`・`blocks`と併用できないため、`text`は指定しない。上限は12,000文字だが、読みやすさのため4,000文字で整形する。

Slack Events APIは3秒以内に2xxを返せない場合、ほぼ即時、1分後、5分後に最大3回再送する。さらに、60分間の配信試行の95%超が失敗するとevent subscriptionが一時的に無効化される。Flex ConsumptionのPython Functionはコールドスタートで3秒を超えることがあるが、最初の要求でFunction Appが起動すれば後続の再送を処理できる可能性が高く、個人利用の頻度では無効化の閾値には届かない。`event_id`のInsertにより再送を重複投入させない。これは確実な配信保証ではないため、always-ready instanceはMVPでは設定せず、実際に困った場合だけ追加する。

重複、allowlist外、DM以外を含め、Slackへは常に2xxを返す。Slackは2xx応答を再送しないため、`X-Slack-No-Retry`ヘッダは使わない。

`eyes`のreactionは、回答までの十数秒から数十秒のあいだ画面が無反応になることへの最小限の手当てである。本来これに相当するのはSlackの`assistant.threads.setStatus`だが、採用可否は[プラットフォームと運用](platform-and-operations.md#slack-agent機能を採用しない理由)を正とする。reactionは失敗の切り分けにも使え、`eyes`が付いたのに回答が来なければ受信は成功しWorkerで落ちたと判断できる。

コールドスタートを短くするため、Function Appのトップレベルでは重い依存をimportしない。Cosmos、Foundry、Agent関連のSDKは各ハンドラの内部でimportし、Slack event受信の経路が他機能の依存を読み込まないようにする。

対象は単一workspaceで許可した利用者とのDMだけとする。`team_id`と`user`をallowlistで限定し、allowlist外は`unauthorized_source`、DM以外は`unsupported_conversation_type`の監査記録だけを残して2xxを返し、Queueへ投入しない。これはmodel tokenとFoundryへ保存されるcontentを想定外の相手に消費させないための制限でもある。allowlist値はdeploy時の設定として与え、実値を文書、repository、ログへ記録しない。Slack Appは自分のworkspaceへ手動installし、複数workspace向けOAuth install flowは持たない。

## 会話履歴

Agent Worker FunctionがHosted AgentのResponses endpointに対するクライアントとなる。外側のResponses protocolが応答と会話履歴を管理し、Workerは呼び出しの戻りにあるresponse idを記録して、次の質問で`previous_response_id`として同じendpointへ渡す。Agent container内部の`FoundryChatClient`によるmodel callは`store: false`とし、model layerへ会話履歴を重複保存しない。自前で会話履歴を組み立てて毎回送る方式は採らない。

Table StorageにSlack threadごとの最新`responseId`と更新時刻だけを持つ。partition keyは`conversation`、row keyは`${teamId}:${channelId}:${rootTs}`をSHA-256でハッシュ化した値とする。

会話の境界はSlackのthread構造を一次的な根拠とする。トップレベルのDMは新しい会話を始め、同じ`rootTs`を持つスレッド内のメッセージだけが会話を継続する。利用者がどこへ返信したかが意思表示なので、内容や経過時間から話題の切れ目を推定しない。

経過時間による打ち切りは話題の境界判定ではなく、安全網として残す。最終更新から7日以上経過している場合は参照を捨て、同じSlack thread内でも新しい会話として開始する。狙いは、長寿命のスレッドでcontextとtokenが際限なく積み上がることの抑止と、Foundry側の保持期間を過ぎて無効化された`responseId`を渡さないことである。実際の保持期間はdeploy時に確認し、7日がそれを下回っていることを確かめる。

Hosted AgentのResponses protocolが管理する質問と回答はFoundry側に保存される。個人利用のMVPではこれを許容し、[quality.md](quality.md#content記録と保護)のcontent記録方針と同じ扱いとする。

## データソース契約

対象はGitHubのdefault branchにある`articles/**/*.md`の全件で、`published: true/false`を問わずmetadataとして保持する。`draft/**`、`x-articles/**`、rootの補助Markdown、非Markdownは対象外とする。`books/**/*.md`は内容追加時に別途有効化する。

`title`、`emoji`、`type`、`topics[]`、`published`を必須front matterとし、`published_at`は任意、`slug`はfilenameとする。必須項目の欠落またはparse失敗は補完せず、対象外として同期結果のerrorへ残す。各chunkにはcommit SHA固定のGitHub blob URLを`sourceUrl`に保存する。公開Zenn URLは将来のoptional metadataである。調査時の件数とfront matter確認結果は[調査記録](research/implementation-readiness-2026-08-11.md#統合判断)に残す。

画像参照は本文に残すが、MVPではOCR・画像本文indexを行わない。`images/**`の変更はblob SHA比較の対象外なので、再indexを誘発しない。

同期対象は公開repositoryとし、GitHub credentialを持たない。owner、repository、default branchはdeploy時の非機密設定として与え、実値をこの文書へ記録しない。

## 状態とメッセージ契約

Storage Queueと同じStorage AccountのTable Storageに`state` tableを一つ置き、次の3種類だけを保持する。outbox、relay、job status machineは作らない。

job storeを持たないため、Agent Workerが必要とする情報はQueue messageが運ぶ。messageはSlackの`eventId`、`teamId`、`userId`、`channelId`、`rootTs`、質問文、telemetry metadataを持ち、Signing SecretとBot tokenは置かない。Slack IDとtimestampは送信先と会話識別に必要なので生の値を運ぶ。Queueは同じStorage Account内にあり、Managed Identityでのみ読み書きされ、保存時に暗号化される。

| partition | key | 保持する値 |
|---|---|---|
| `sync` | `github` | 最終同期成功SHA、最終実行時刻、最終実行結果 |
| `event` | `{eventId}` | 受信時刻。Slack event再送の重複投入抑止だけに使う |
| `conversation` | `{threadKeyHash}` | Slack threadごとの直近の`responseId`、更新時刻 |

Queue Triggerの標準再試行とpoison queueを使い、独自のrelayや24時間再試行は作らない。`host.json`でQueue Triggerの`batchSize`を1にし、同じSlack threadへの連投で`previous_response_id`の読み書きが競合して会話が分岐することを避ける。Agent Workerが最終的に失敗した場合は再送や代替通知を行わず、次回の利用者メッセージで再試行する。

## Cosmos DB検索ストア

NoSQLの`chunks` containerを一つ作り、partition keyは`/corpusId`、MVP値は`default`に固定する。一corpusを単一logical partitionに置き、cross-partition vector retrievalを避ける。複数corpusまたは20 GB超の見込みが生じた時点で新containerへの移行を判断する。

各chunkは`id = ${articleId}:${chunkIndex}`、`corpusId`、記事・見出し・source metadata、`sourceRevision`、`sourceBlobSha`、`chunkingVersion`、`indexedAt`、`text`、`embedding`を持つ。`sourceBlobSha`と`chunkingVersion`は差分判定のキーである。記事更新は既存articleのchunkを削除して新chunkをupsertし、削除時は該当articleのchunkを削除する。小さい記事は同一logical partitionのtransactional batchで置換し、batch制限を超える記事は複数batchで記事全体を置換する。途中で失敗した場合はcheckpointを持たず、次回Timerが記事単位で再実行する。

embedding deploymentのmodel、version、SKU、TPMは[プラットフォームと運用](platform-and-operations.md#採用設定)を正とする。vector fieldは`/embedding`、1536次元、`float32`、cosine、`quantizedFlat`とし、`/embedding/*`を通常indexから除外する。vector policy/indexはimmutableであり、MVPで1,000 vector未満のfull scanを許容する。

## Hosted Agentとidentity/RBAC

Hosted AgentはPython 3.13のAgent Frameworkで`ResponsesHostServer`を起動する。ChatはResponses protocol経路を使い、Agent内部の`FoundryChatClient`は`default_options={"store": false}`とする。会話履歴は外側のResponses protocolへ一元化する。reasoning effortは既定値で始め、回答品質が不足する場合だけ引き上げる。`knowledge_search`は同一processの`@tool`で、`CosmosClient(DefaultAzureCredential())`によりAgent identityで検索する。外部検索endpointは作らない。回答には記事リンクを添える。

| principal | 必要な権限 |
|---|---|
| Function App MI | Storage Blob Data Owner、Storage Queue Data Contributor、Storage Table Data Contributor、Cosmos DB Built-in Data Contributor、Key Vault Secrets User、Foundry project scopeでembedding呼び出しとAgent呼び出しに必要なdata-plane role |
| Hosted Agent identity | `chunks` container scopeのCosmos DB Built-in Data Reader、`knowledge_search`のクエリ埋め込みに必要なFoundry embedding data-plane role |
| Foundry Project MI | Log Analytics Reader |

Function App MIに必要なFoundryのdata-plane roleは、Sync Functionのembedding呼び出しとAgent Workerのagent呼び出しの両方に必要である。`knowledge_search`は受け取ったクエリ文字列を埋め込んでからvector queryを実行するため、Hosted Agent identityもCosmosに加えてembeddingを呼べる必要がある。実際のrole名はdeploy時のFoundryのRBACモデルで確認する。

Agent principal IDはdeploy後に得るため、AgentへのCosmos data-plane role assignmentはローカルのpost-deploy scriptで作成する。詳細は[プラットフォームと運用](platform-and-operations.md#デプロイと復旧)を参照する。
