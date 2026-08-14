# ADR 0001: GitHub同期は定期reconciliationとする

Status: Accepted

## Context

初期案はGitHub Appのpush webhookを起点に、job、outbox、relay、lease、checkpointで同期を保証するものだった。個人の記事repositoryには、即時性よりも故障時に自然回復する単純さが重要だった。

## Decision

Timer Functionがrepository全体を定期走査し、sourceとindexを望ましい状態へreconcileする。Webhook、同期queue、outbox、lease、独自checkpointは持たない。

## Consequences

反映は次のtimerまで遅れ、一時的な失敗も次回まで残る。一方、中断や削除を含めて次回の全体走査で収束するため、配送保証やjob状態を運用しなくてよい。
