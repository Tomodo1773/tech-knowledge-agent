# ADR 0003: runtime境界を増やさない

Status: Accepted

## Context

Sync、Slack受信、workerは必要な権限と負荷特性が異なり、検索にも独立APIを置ける。しかし個人規模では、deploy、identity、通信、監視の境界を増やす負担が、障害分離やleast privilegeの利益を上回る。

## Decision

Sync、Slack受信、workerは一つのFunction Appとidentityを共有する。記事検索も独立APIにせず、Hosted Agentのprocess内toolからCosmos DBへ直接行う。runtime境界はFunction AppとHosted Agentの二つに留める。

## Consequences

deployと運用は単純になるが、Functionの権限、障害、scaleは用途別に分離できず、検索処理もAgent artifactへ結合する。独立したsecurity、scale、failure、deploy lifecycleが必要になった時点で分割する。
