# Repository Guidelines

## Project

- Slackから個人の技術記事を検索する、単一利用者・単一環境のAzure AIエージェント。低コストと、Azure AIを実地で学ぶ価値を両立させる。
- 必要性が実測されるまで、高可用性、複数利用者・環境、自動rollbackは増やさない。preview機能は、学習価値がコストと運用負荷を上回る場合に限り候補とする。

## Boundaries

- アプリケーションコードは公開する。記事本文は公開可能でも執筆履歴を含むsource repositoryは非公開とする。credential、個人情報、実resourceのID・名前・endpoint、deployment outputをcommitやCI log、artifact、PR commentへ残さない。
- Azure resourceはBicepを正本とし、provisionとdeployを再現可能に保つ。AVM version変更時は既定値を再監査する。依存の取得・更新は必ず`sfw`経由で行う。

## Working agreements

- `.github/workflows/ci.yml`を検証の正本とし、変更後は該当する同等のローカル検証を行う。公開CIからAzureへlogin、provision、deployしない。
- Microsoft Foundry Agentの実装、構成、deploy、troubleshootでは、先に`.claude/skills/microsoft-foundry/`を読む。

## Documentation

- コードから復元できない重要な意思決定だけを、結論・理由・捨てた案・代償に絞って残す。コードから分かること、一般知識、計画・進捗・手順は文書化しない。
- `README.md`はアプリ紹介として、概要、開発の背景、主な機能、設計上のポイント、システム構成、技術スタックを扱う。背景と、紹介する機能・強調点は作者への確認で決め、推測で足さない。`docs/adr/`は設計判断の履歴だけを扱う。`AGENTS.md`と`CLAUDE.md`は常に同一内容にする。
- `AGENTS.md`と`CLAUDE.md`はシンボリックリンクではない別ファイルとして管理し、内容を完全に一致させる。`.githooks/pre-commit`でステージ済み内容を、GitHub Actionsの`repository-policy` workflowでCI上の内容を検証する。
- `.agents/skills`と`.claude/skills`は別の実体として管理し、配下の内容を完全に一致させる。
