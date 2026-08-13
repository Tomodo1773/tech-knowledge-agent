# 品質と観測

この文書は、MVPのcontent記録、smoke evaluationと、MVP後の品質施策の正本である。何をどのsignalで記録するかの設計は[telemetry.md](telemetry.md)を正とする。

## Content記録と保護

個人利用のMVPでは、Agent versionの環境変数`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`を固定で`true`にし、built-in traceの質問・回答・tool入出力をApplication Insightsへ保存する。公開可能な技術記事と個人の技術質問だけを入力する前提であり、credential、個人情報、業務上の非公開情報はSlack質問にも記事にも含めない。これはbuilt-in `gen_ai` telemetryの設定であり、custom spanの属性・引数・戻り値を安全にするものではない。Hosted Agentの外側のResponses protocolが質問・回答と会話履歴をFoundry側で管理する一方、Agent内部のmodel callは`store: false`とし、model layerへ重複保存しない。

Slack Signing Secret、Bot token、Authorization header、event本文全文をcustom spanへ記録しない。Slack workspace、user、channel、threadの識別が必要なら[architecture.md](architecture.md#会話履歴)と同じ組み合わせをハッシュ化して使う。閲覧権限は自分のEntra IDと実行に必要なManaged Identityへ限定し、第三者OTLP backendへcontentを送信しない。保持期間とdaily capで保存費用を制御する。

## MVP評価

MVPでは通常traceを確認し、repository内でversion管理する固定dataset約10件によるsmoke evaluationを行う。各caseはquery、期待するsource記事を持ち、実行scriptは回答と引用を出力する。判定は引用に期待記事が含まれるかの確認と目視で足りる。LLM judgeとevaluator versionの記録はMVPでは行わない。

評価はdeployを止める品質gateにはしない。失敗は検索、tool利用、生成のどこにあるかをtraceと根拠記事で確認し、改善材料として扱う。

初期に観察する指標はSlack eventの3秒以内2xx率、`http_timeout`による再送件数、GitHub同期・Function・Agent・Slack送信の失敗率、Agent・vector検索・end-to-endの時間、最終同期時刻、検索結果なし率、引用付き回答率、token / RU / telemetry取り込み量である。数値thresholdは実測後に定める。

## MVP後

LLM judgeによる自動採点、production trace evaluation、recurring / continuous evaluation、evaluation結果のversion管理はMVP後に検討する。これらにはpreview機能を含むため、導入時に運用負荷と価値を再評価する。必要なRBACは[architecture.md](architecture.md#hosted-agentとidentityrbac)を正とする。
