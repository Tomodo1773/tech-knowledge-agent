# アーキテクチャ

この文書は、MVPのアプリケーション、データ、identity/RBAC設計の正本である。Azureの採用設定と運用は[platform-and-operations.md](platform-and-operations.md)、観測と評価は[quality.md](quality.md)を参照する。

## 境界とフロー

### GitHub同期

1. Sync Scheduler FunctionはTimer Triggerで起動し、公開GitHub repositoryのdefault branchのcommit SHAを認証なしのGitHub APIで確認する。
2. SHAが最終同期済みrevisionと異なる場合だけ、target SHAを持つ同期jobをTableへ保存し、job参照をStorage Queueへ直接投入する。Queue投入に失敗した実行は失敗として記録し、次回Timerで再試行する。
3. Indexer Functionはtarget SHA時点のGitHub Contents APIを読み、対象記事を全件reconcileしてMarkdownのchunk化、embedding、Cosmos upsertと削除反映を行う。
4. 同期中はleaseで並行実行を防ぐ。処理中にdefault branchが更新された場合は、次回Timerが新しいSHAを検出して再同期する。

定期確認ではcommit SHAが変わらなければ本文を取得しない。手動同期はMVPの必須機能にせず、Timerの再実行で復旧する。

### LINE質問

1. LINE Webhook Functionは署名を検証し、`webhookEventId`を冪等キーにjob stateを作り、job参照をStorage Queueへ直接投入して2秒以内に2xxを返す。Queue投入に失敗した場合は2xxを返さず、Webhook再送で再試行する。再送で既存の未完了jobを受けた場合は同じjob参照を再投入し、完了済みなら何も投入せず2xxを返す。
2. Agent Worker Functionがjobを実行し、Hosted Agentの`knowledge_search` toolからCosmosを検索する。
3. Agent Worker Functionが同一の`X-Line-Retry-Key`でPush Messageを直接送信し、回答または最終失敗をjob stateへ保存する。

対象は1:1チャットだけとする。group / roomは署名検証後に2xxを返し、`unsupported_source_type`の監査記録だけを残す。`replyToken`は非同期最終返信に保存・使用しない。Loading APIは1:1でbest effortとし、失敗しても処理を継続する。

## データソース契約

対象はGitHubのdefault branchにある`articles/**/*.md`の全件で、`published: true/false`を問わずmetadataとして保持する。`draft/**`、`x-articles/**`、rootの補助Markdown、非Markdownは対象外とする。`books/**/*.md`は内容追加時に別途有効化する。

`title`、`emoji`、`type`、`topics[]`、`published`を必須front matterとし、`published_at`は任意、`slug`はfilenameとする。必須項目の欠落またはparse失敗は補完せず、対象外としてjob errorへ残す。各chunkにはcommit SHA固定のGitHub blob URLを`sourceUrl`に保存する。公開Zenn URLは将来のoptional metadataである。調査時の件数とfront matter確認結果は[調査記録](research/implementation-readiness-2026-08-11.md#統合判断)に残す。

画像参照は本文に残すが、MVPではOCR・画像本文indexを行わず、`images/**`だけの更新では再indexしない。

同期対象は公開repositoryとし、GitHub credentialを持たない。owner、repository、default branchはdeploy時の非機密設定として与え、実値をこの文書へ記録しない。

## job stateとメッセージ契約

Storage Queueと同じStorage AccountのTable Storageに`workItems` tableを置き、同期状況、LINE jobの冪等性と処理結果だけを保持する。outboxとrelayは作らない。Queueはat-least-onceであるため、本文・credential・reply tokenを置かず、job参照とtelemetry metadataだけを持つ。

| flow | key | state |
|---|---|---|
| GitHub同期元 | `github:default` | last observed / successful SHA、active job ID、status |
| GitHub同期job | `github:{targetSha}` | target SHA、status、attempt、content hash、trace context |
| LINE質問 | `line:{webhookEventId}` | event、message ID/text、source、destination、status、result、retry key、attempt、trace context |

Queue Triggerの標準再試行とpoison queueを使い、独自のrelayや24時間再試行は作らない。LINE Pushは再実行時も同一payloadと`X-Line-Retry-Key`を用い、2xx/409を完了、その他4xxを非再試行とする。最終失敗時は重複送信せず、次回の利用者メッセージで再試行を促す。

## Cosmos DB検索ストア

NoSQLの`chunks` containerを一つ作り、partition keyは`/corpusId`、MVP値は`default`に固定する。一corpusを単一logical partitionに置き、cross-partition vector retrievalを避ける。複数corpusまたは20 GB超の見込みが生じた時点で新containerへの移行を判断する。

各chunkは`id = ${articleId}:${chunkIndex}`、`corpusId`、記事・見出し・source metadata、`sourceRevision`、`contentHash`、`chunkingVersion`、`indexedAt`、`text`、`embedding`を持つ。記事更新は既存articleのchunkを削除して新chunkをupsertし、削除時は該当articleのchunkを削除する。小さい記事は同一logical partitionのtransactional batchで置換し、batch制限を超える記事は複数batchで記事全体を置換する。途中で失敗した場合はcheckpointを持たず、記事単位で再実行する。

embedding deploymentのmodel、version、SKU、TPMは[プラットフォームと運用](platform-and-operations.md#採用設定)を正とする。vector fieldは`/embedding`、1536次元、`float32`、cosine、`quantizedFlat`とし、`/embedding/*`を通常indexから除外する。vector policy/indexはimmutableであり、MVPで1,000 vector未満のfull scanを許容する。

## Hosted Agentとidentity/RBAC

Hosted AgentはPython 3.13のAgent Frameworkで`ResponsesHostServer`を起動する。ChatはResponses API経路を使い、`FoundryChatClient` / `ResponsesHostServer`の既定値に`store: false`と`reasoning: { effort: max }`を設定する。`knowledge_search`は同一processの`@tool`で、`CosmosClient(DefaultAzureCredential())`によりAgent identityで検索する。外部検索endpointは作らない。回答には記事リンクを添える。

| principal | 必要な権限 |
|---|---|
| Function App MI | Storage Blob Data Owner、Storage Queue Data Contributor、Storage Table Data Contributor、Cosmos DB Built-in Data Contributor、Key Vault Secrets User |
| Hosted Agent identity | `chunks` container scopeのCosmos DB Built-in Data Reader |
| Foundry Project MI | Log Analytics Reader、必要時のPrivileged Monitoring Data Reader、project scopeのFoundry User |
| CI identity | target Cosmos accountでAgentへのdata-plane role assignmentに必要な最小権限 |

Agent principal IDはdeploy後に得るため、AgentへのCosmos reader assignmentは保護されたCIのpost-deploy control-plane操作で作成する。
