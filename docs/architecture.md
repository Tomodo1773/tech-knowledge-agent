# アーキテクチャ

この文書は、MVPのアプリケーション、データ、identity/RBAC設計の正本である。Azureの採用設定と運用は[platform-and-operations.md](platform-and-operations.md)、観測と評価は[quality.md](quality.md)を参照する。

## 境界とフロー

### GitHub同期

1. GitHub Webhook Functionは`X-Hub-Signature-256`を検証し、`push`かつGitHub APIで解決したdefault branchへの更新だけを受け付ける。
2. `X-GitHub-Delivery`を冪等キーにjob stateとoutboxをTableへ永続化し、Queueにはjob参照だけを投入して速やかに2xxを返す。
3. Indexer Functionは`after` SHA時点のGitHub Contents APIを読み、Markdownをchunk化、embedding、Cosmos upsertする。
4. force pushは対象refを全件reconcileする。同期leaseと最新target SHAを確認し、遅延jobが新しい状態を上書きしないようにする。

Webhook payloadは本文を含まないため、本文取得をWebhook Functionで行わない。永続化後のQueue投入に失敗してもoutbox relayが再投入する。

### LINE質問

1. LINE Webhook Functionは署名を検証し、`webhookEventId`でjob stateとoutboxを作って2秒以内に2xxを返す。
2. Agent Worker Functionがjobを実行し、Hosted Agentの`knowledge_search` toolからCosmosを検索する。
3. 回答または最終失敗を永続化し、Push Message outboxを送る。

対象は1:1チャットだけとする。group / roomは署名検証後に2xxを返し、`unsupported_source_type`の監査記録だけを残す。`replyToken`は非同期最終返信に保存・使用しない。Loading APIは1:1でbest effortとし、失敗しても処理を継続する。

## データソース契約

対象はGitHubのdefault branchにある`articles/**/*.md`の全件で、`published: true/false`を問わずmetadataとして保持する。`draft/**`、`x-articles/**`、rootの補助Markdown、非Markdownは対象外とする。`books/**/*.md`は内容追加時に別途有効化する。

`title`、`emoji`、`type`、`topics[]`、`published`を必須front matterとし、`published_at`は任意、`slug`はfilenameとする。必須項目の欠落またはparse失敗は補完せず、対象外としてjob errorへ残す。各chunkにはcommit SHA固定のGitHub blob URLを`sourceUrl`に保存する。公開Zenn URLは将来のoptional metadataである。調査時の件数とfront matter確認結果は[調査記録](research/implementation-readiness-2026-08-11.md#統合判断)に残す。

画像参照は本文に残すが、MVPではOCR・画像本文indexを行わず、`images/**`だけの更新では再indexしない。

GitHub Appは対象repositoryにだけinstallし、`Contents: read-only`を与える。private keyとWebhook secretはKey Vaultで管理し、実repository名、App ID、installation ID、Webhook URLを文書やrepositoryへ記録しない。

## 永続化とメッセージ契約

Storage Queueと同じStorage AccountのTable Storageに`workItems` tableを置く。`PartitionKey = jobId`、`RowKey = state`または`outbox:{name}`とし、受信時にstateとoutboxを同じpartitionのtransactional batchで作る。Queueはat-least-onceであるため、本文・credential・reply tokenを置かず、job参照とtelemetry metadataだけを持つ。

| flow | jobId / 冪等キー | state | outbox |
|---|---|---|---|
| GitHub同期 | `github:{deliveryId}` | delivery、ref、before/after、forced、status、attempt、revision、content hash、trace context | `index`。relayが未配送を再enqueue |
| LINE質問 | `line:{webhookEventId}` | event、message ID/text、source、destination、status、result、trace context | `push`。payload hash、retry key、attempt、acceptedAt |

LINE Pushは同一payloadと`X-Line-Retry-Key`を用いる。timeoutまたはHTTP 500だけを指数backoffで24時間以内に再試行し、2xx/409を完了、その他4xxを非再試行とする。最終失敗時は重複送信せず、次回の利用者メッセージで再試行を促す。

## Cosmos DB検索ストア

NoSQLの`chunks` containerを一つ作り、partition keyは`/corpusId`、MVP値は`default`に固定する。一corpusを単一logical partitionに置き、cross-partition vector retrievalを避ける。複数corpusまたは20 GB超の見込みが生じた時点で新containerへの移行を判断する。

各chunkは`id = ${articleId}:${chunkIndex}`、`corpusId`、記事・見出し・source metadata、`sourceRevision`、`contentHash`、`chunkingVersion`、`indexedAt`、`text`、`embedding`を持つ。記事更新は既存articleのchunkを削除して新chunkをupsertし、削除時は該当articleのchunkを削除する。小さい記事は同一logical partitionのtransactional batchで置換し、100操作、2 MB、5秒を超える場合は同一jobでcheckpointを進める。

embedding deploymentのmodel、version、SKU、TPMは[プラットフォームと運用](platform-and-operations.md#採用設定)を正とする。vector fieldは`/embedding`、3072次元、`float32`、cosine、`quantizedFlat`とし、`/embedding/*`を通常indexから除外する。vector policy/indexはimmutableであり、MVPで1,000 vector未満のfull scanを許容する。

## Hosted Agentとidentity/RBAC

Hosted AgentはPython 3.13のAgent Frameworkで`ResponsesHostServer`を起動する。ChatはResponses API経路を使い、`FoundryChatClient` / `ResponsesHostServer`の既定値に`store: false`と`reasoning: { effort: max }`を設定する。`knowledge_search`は同一processの`@tool`で、`CosmosClient(DefaultAzureCredential())`によりAgent identityで検索する。外部検索endpointは作らない。回答には記事リンクを添える。

| principal | 必要な権限 |
|---|---|
| Function App MI | Storage Blob Data Owner、Storage Queue Data Contributor、Storage Table Data Contributor、Cosmos DB Built-in Data Contributor、Key Vault Secrets User |
| Hosted Agent identity | `chunks` container scopeのCosmos DB Built-in Data Reader |
| Foundry Project MI | Log Analytics Reader、必要時のPrivileged Monitoring Data Reader、project scopeのFoundry User |
| CI identity | target Cosmos accountでAgentへのdata-plane role assignmentに必要な最小権限 |

Agent principal IDはdeploy後に得るため、AgentへのCosmos reader assignmentは保護されたCIのpost-deploy control-plane操作で作成する。
