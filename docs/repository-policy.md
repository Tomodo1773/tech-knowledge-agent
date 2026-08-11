# Repository Policy

この文書は、初期構想の検討中に`AGENTS.md` / `CLAUDE.md`から分離して保管する恒久ポリシーである。アーキテクチャ、Azure設定、品質運用の設計値はそれぞれの設計文書を正とする。

## Design scope and proportionality

- このprojectは個人開発であり、利用者は自分一人を前提とする。企業向けサービス相当の可用性、運用成熟度、品質保証を目標にしない。
- Azure AI関連スタックを広く学ぶことは主要目的である。低コストかつ運用負荷を抑えられる場合は、実利用に必須でなくても高度な機能を学習目的で採用してよい。
- 機能や品質施策を提案するときは、現在の個人利用で解決する問題、学習価値、金銭コスト、実装・継続運用の複雑さを比較する。価値が複雑さを明確に上回らない場合は、MVPから除外または延期する。
- 将来の多人数利用を先回りした高可用性、複数環境、無停止deploy、rollback機構、重い承認フロー、網羅的な品質gateは、具体的な必要性が生じるまで作らない。一時的な停止と手動復旧を許容する。
- credential保護、機密情報を公開しないこと、sourceから復元できないデータを守るための基本的な整合性は、簡素化の対象にしない。
- 既存設計も定期的に見直し、単独利用に対して過剰な機能や運用を削る。不要な複雑さの削減は品質低下ではなく、このprojectにおける設計改善として扱う。

## Public repository boundary

- credential、Azure subscription / tenant / client / object ID、resource ID、実resource名、実endpoint、Webhook URLをcommitしない。
- 実値はGitHub Environment secrets、Azure Key Vault、またはGit管理外のローカル設定へ置く。非機密扱いのIDもこのrepositoryではsecretとして扱う。
- `.azure/`、`.env`、`*.local.bicepparam`、deployment output、無加工のAzure CLI / `azd`ログをcommitしない。
- sample、test fixture、screenshot、Issue / PR、Actionsログにも実値を載せない。
- Webhook payload、job state、outbox payloadは運用データであり、repositoryへcommitしない。custom telemetryにはcredentialを記録しない。
- Bicepにはplaceholder、環境変数参照、決定的な命名規則だけを置き、デプロイ後の完全なresource名を逆輸入しない。
- 公開ActionsではAzure関連値をmaskし、deployment outputをログ、PR comment、artifactへ出さない。
- credentialを誤ってcommitした場合は、履歴を書き換える前に対象credentialを失効・rotateする。

## Infrastructure and delivery

- Azure Resource Managerで表現できる基盤はBicepをsource of truthとする。
- `azd provision`はBicep適用、`azd deploy`はapplication artifact配布に使う。
- 対応するAzure Verified Modules (AVM) を第一候補とし、module versionを明示的に固定する。
- AVMを呼ぶだけの一対一wrapperは作らない。project固有moduleは複数resourceの関係を表す薄いcompositionに限定する。
- AVMが必要なAPI versionやpropertyを未サポートの場合だけraw Bicepを使い、理由と再評価条件をcode commentへ残す。
- public repositoryの情報境界に合わせ、AVMの`enableTelemetry`は`false`を明示する。
- 公開PRではAzureへログインせず、Bicep buildと静的検査だけを行う。実環境のvalidate、what-if、deployは保護されたGitHub Environmentで行い、出力を公開しない。
- Azureサービス間認証はManaged Identityと最小権限RBACを優先し、account keyをapp settingsへ渡さない。
- Bicepで表せないdata-plane操作は保護されたdeploy workflowで行い、実行結果を公開しない。

## Shared repository policy

このrepositoryはTomodo1773の関連repository共通ポリシーの対象とする。project固有の公開境界はこの文書を優先し、共通ポリシーに例外を設ける場合は理由と適用範囲をここへ記録する。

- `AGENTS.md`と`CLAUDE.md`は別ファイルとして管理し、内容を完全に一致させる。`.agents/skills`と`.claude/skills`を追加する場合も、別実体として内容を一致させる。
- ステージ前はpre-commit hook、リモートではCIで、指示ファイルとスキルの同期を検証する。
- 依存関係はmanifestとlockfileを管理し、対応するpackage managerの取得・更新は`sfw`経由にする。package manager versionはmanifestで固定し、CIで二重管理しない。
- install lifecycle scriptは既定で実行せず、必要なpackageだけを理由付きで明示的に許可する。
- Dependabotは使用中のpackage ecosystemとGitHub Actionsを対象にし、通常更新とsecurity updateを分け、weeklyで確認する。security updateは待機期間で遅延させない。
- GitHub Actionsは完全長commit SHAへ固定し、参照元versionをcommentで残す。`GITHUB_TOKEN`の権限はworkflowまたはjobごとに最小限にする。

現時点では実行時のpackage ecosystemを持たないため、依存取得に関する設定は未適用とする。依存関係を追加する場合はこの方針に従う。

## Validation

- repository policy: `pwsh -NoProfile -File scripts/check-repository-policy.ps1`
- whitespace: `git diff --check`
- コード追加後は、対象runtimeのlint、unit test、Bicep buildを実行する。
