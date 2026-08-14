# ADR 0005: 既存traceを主軸にしてlog収集を限定する

Status: Accepted

## Context

root loggerの収集はexporter自身のlogを再収集し、1日6万件超のnoiseとdaily capによるdependency欠落を起こした。既存のSDK・framework spanを捨ててcustom計装へ置き換える案では、外部依存の情報が減る。

## Decision

SDKとframeworkが生成するtraceを主軸とし、同じ事実をcustom spanやmetricsで再計装しない。logはspanにない事実だけをapplication logger配下から収集する。

## Consequences

自己増殖と不要な取り込みを避ける代わりに、近接したSDK spanの重複と、収集対象外SDK loggerのwarning欠落を受け入れる。必要な失敗分類はapplication境界で安全なlogとして補う。
