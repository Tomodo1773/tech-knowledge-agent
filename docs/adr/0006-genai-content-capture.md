# ADR 0006: GenAI contentを記録する

Status: Accepted

## Context

contentを記録しない案では、回答品質の問題が会話、検索、生成のどこで生じたかを実環境から判別できない。現在の入力は、公開可能な技術記事と個人の技術質問に限定している。

## Decision

Functions側で質問と最終回答、Hosted Agent側で検索queryと取得chunkをApplication Insightsへ記録する。この許可は現在のデータ境界にだけ適用する。

## Consequences

問題をend-to-endで追える代わりに、会話と記事断片がtelemetryへ保存される。個人情報、credential、非公開情報を扱う場合はこの決定を無効とし、content記録を停止またはredactionを設計する。
