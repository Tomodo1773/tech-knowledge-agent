# ADR 0004: Foundryの所有境界に従う

Status: Accepted

## Context

Foundryだけで呼び出しと権限付与をproject内へ完結させる案は成立しなかった。実環境ではproject endpointからのembeddingが404となり、Agent identityもAgent作成後まで存在しなかった。

## Decision

Agent呼び出しはproject、embeddingはaccountのendpointと権限を使う。Agent identityに必要なdata-plane権限は、作成後のpostdeployで付与する。これらを手書きの共通endpointやBicepだけへ無理に統合しない。

## Consequences

project側は最小権限にできるが、embeddingには広いaccount scopeが必要になる。postdeploy hookとrole反映待ちも残り、role反映前は一時的な認可失敗が起こり得る。
