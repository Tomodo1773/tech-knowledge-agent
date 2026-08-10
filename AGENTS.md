# Repository Guidelines

## Purpose

Azure AI関連スタックを学ぶための、技術ナレッジ検索エージェント。public repositoryとして、再利用可能なコード、Bicep、設計資料だけを管理する。実際のAzure環境を特定できる情報は管理しない。

## Public repository boundary

- credential、Azure subscription / tenant / client / object ID、resource ID、実resource名、実endpoint、Webhook URLをcommitしない。
- 実値はGitHub Environment secrets、Azure Key Vault、またはGit管理外のローカル設定へ置く。非機密扱いされるIDもこのリポジトリではsecretとして扱う。
- `.azure/`、`.env`、`*.local.bicepparam`、deployment output、無加工のAzure CLI / `azd`ログをcommitしない。
- sample、test fixture、screenshot、Issue / PR、Actionsログにも実値を載せない。
- Bicepにはplaceholder、環境変数参照、決定的な命名規則だけを置き、デプロイ後の完全なresource名を逆輸入しない。
- 公開ActionsではAzure関連値をmaskし、deployment outputをログ、PR comment、artifactへ出さない。
- 誤ってcredentialをcommitした場合は、履歴を書き換える前に対象credentialを失効・rotateする。

## Infrastructure and delivery

- Azure Resource Managerで表現できる基盤はBicepをsource of truthとする。
- `azd provision`はBicep適用、`azd deploy`はFunctionsとHosted Agentのartifact配布に使う。
- 対応するAzure Verified Modules（AVM）があるresourceはAVMを第一候補とし、module versionを明示的に固定する。
- AVMを呼ぶだけの一対一wrapperは作らない。project固有moduleは複数resourceの関係を表す薄いcompositionに限定する。
- AVMが必要なAPI versionやpropertyを未サポートの場合だけraw Bicepを使い、理由と再評価条件をcode commentへ残す。
- public repositoryの情報境界に合わせ、AVMの`enableTelemetry`は`false`を明示する。
- 公開PRではAzureへログインしない。Bicep buildと静的検査だけを行う。
- 実環境のvalidate、what-if、deployは保護されたGitHub Environmentで行い、出力を公開しない。
- Azureサービス間認証はManaged Identityと最小権限RBACを優先し、account keyをapp settingsへ渡さない。

## Instruction file synchronization

- `AGENTS.md`と`CLAUDE.md`は別ファイルとして管理し、内容を完全に一致させる。
- 変更時は必ず両方を更新する。
- `.githooks/pre-commit`とCIで同期を検証する。

## Supply chain

- 依存関係を追加するときはmanifestとlockfileを管理する。
- 依存取得・更新はSocket Firewall対応時に`sfw`経由で行い、生のpackage manager installを実行しない。
- GitHub Actionsは完全長commit SHAへ固定し、versionをcommentで残す。
- `GITHUB_TOKEN`の権限はworkflowまたはjobで必要最小限にする。

## Validation

- repository policy: `pwsh -NoProfile -File scripts/check-repository-policy.ps1`
- whitespace: `git diff --check`
- コード追加後は、対象runtimeのlint、unit test、Bicep buildを実行する。
