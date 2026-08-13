# Repository Guidelines

## Project Context

- 背景・目的: Azure AI関連スタックを学びながら、個人用の技術ナレッジ検索エージェントを作る。MVPではGitHubで管理する技術ブログをデータソースとし、Slackから検索・質問できるようにする。
- 利用規模: 個人利用・小規模を前提とする。大規模対応や高可用性は当面求めない。
- データ前提: 扱う技術ブログは公開されても問題ない内容を前提とする。ただし、credential、個人情報、その他の機密情報は含めない。記事本文は公開可だが、commit履歴に執筆過程が残るためsource repository自体は非公開とし、同期はKey Vault由来のGitHub tokenで認証する。
- コスト方針: 基本的にAzureの無料枠・低コスト枠を優先する。継続課金が必要なサービスや構成を提案する場合は、導入前にコストを明示する。
- 学習方針: 実用性だけでなく、Azure AI関連の主要なサービスや構成を一通り学べることも重視する。
- 技術選定方針: 学習価値を優先し、ベストプラクティス、モダンな技術、新しい技術、気になるプレビュー機能も積極的に採用候補とする。
- プレビュー採用の条件: IaC非対応による手動設定、環境差分、運用負荷、日常のCI/CDやAzureプロビジョニングの複雑化が大きい場合は、学習価値と運用コストを比較してから採用する。
- 配送方針: 日常のCI/CDはスムーズかつ再現可能に回せるようにし、Azureプロビジョニングも可能な限り自動化する。避けられない手動作業は初回bootstrapなどに限定し、理由と手順を明示する。

実装計画Step 0〜7は完了し、実resourceは稼働中である。Slack DM・GitHub同期・smoke evaluationの実環境確認も済んでいる。残作業は[残りのliveゲート](docs/implementation-plan.md#残りのliveゲート)にまとめてある。各Stepの到達点と残ったgateは[実装計画](docs/implementation-plan.md)の進捗とgateを正とする。

Azureへ接続せず再現できるローカル検証の一式は[ローカル開発](docs/platform-and-operations.md#ローカル開発)を参照する。変更を加えたらcommit前にこの一式を通す。依存の取得・更新は必ず`sfw`経由で行う。

検討の過程で既存ドキュメントの内容が膨らんだ場合は、適宜ドキュメントを分割する。文書の役割が曖昧になったり構成が扱いにくくなったりした場合は、既存ドキュメントの構成・役割も再設計する。

初期構想の完了に伴い、恒久的な開発・運用ポリシーは[リポジトリポリシー](docs/repository-policy.md)へ整理した。

Microsoft Foundry Agentの実装、構成、deploy、troubleshootでは、`.claude/skills/microsoft-foundry/`を先に読む。上流の`microsoft/azure-skills`からskill単体をpin付きでvendorしてある。MCP前提で使えない範囲と更新手順は[vendor記録](.claude/skills/VENDOR.md)を参照する。skillが扱っていない論点は、公式の現行資料で確認する。

`AGENTS.md` と `CLAUDE.md` は同期対象とし、内容を常に一致させる。どちらかを変更した場合は、必ずもう一方も同じ内容に更新する。
