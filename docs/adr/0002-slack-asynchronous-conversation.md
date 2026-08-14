# ADR 0002: Slack threadを非同期会話の境界とする

Status: Accepted

## Context

当初候補のLINEは、一度しか使えず有効時間も短いreply tokenが長時間のAgent処理に合わなかった。Slackのevent受信にも短い応答期限があり、追い質問の文脈も管理する必要がある。

## Decision

Slack DMを入口とし、受信処理はevent IDをclaimしてQueueへ送り、workerが元messageのthreadへ返信する。Slack threadを会話単位とし、履歴はResponsesに持たせ、アプリケーションは最新response IDだけを保存する。

## Consequences

WebhookとAgent処理を分離でき、transcriptの複製も不要になる。代わりに、claim成功後・enqueue前に停止するとeventを失う小さな窓を受け入れ、利用者の再質問で回復する。一定期間を超えたResponses参照は引き継がず、新しい会話として扱う。
