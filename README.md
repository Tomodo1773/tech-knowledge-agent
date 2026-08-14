# 技術ナレッジ検索エージェント

自分の技術記事を検索し、Slack DMへ出典付きの回答を返す個人用Azure AIエージェント。

非公開GitHub repositoryの記事をAzure Functionsが日次でCosmos DBへ同期する。SlackのイベントはQueue経由でFoundry Hosted Agentへ渡し、同じthread内の追い質問ではResponsesの会話を継続する。

![技術ナレッジ検索エージェントの構成図](docs/architecture/architecture.svg)

初期開発と実環境での動作確認は完了している。このrepositoryは公開された開発記録であり、汎用テンプレートや第三者向けセットアップ手順は提供しない。

コードから復元できない設計判断だけを[ADR](docs/adr/)に残している。
