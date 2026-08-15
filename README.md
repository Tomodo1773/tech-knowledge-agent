# 技術ナレッジ検索エージェント

自分の技術記事を検索し、Slack DMへ出典付きの回答を返す個人用Azure AIエージェント。

![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)
![Azure Functions](https://img.shields.io/badge/Azure%20Functions-Flex%20Consumption-0062AD?logo=azurefunctions&logoColor=white)
![Microsoft Foundry](https://img.shields.io/badge/Microsoft%20Foundry-Hosted%20Agent-0078D4?logo=microsoftazure&logoColor=white)
![Azure Cosmos DB](https://img.shields.io/badge/Azure%20Cosmos%20DB-Vector%20Search-0078D4?logo=microsoftazure&logoColor=white)
![Slack](https://img.shields.io/badge/Slack-DM-4A154B?logo=slack&logoColor=white)

## 1. 概要

作者ひとりが使うことを前提にした、個人用の技術ナレッジ検索エージェント。
Slack DMへ技術的な質問を送ると、自分が書いた技術記事だけを根拠にした回答が出典付きで返る。
同じthread内であれば追い質問も続けられる。

記事は非公開のGitHub repositoryに置いてあり、Azure Functionsが日次で走査して
Azure Cosmos DBのvector indexへ同期する。回答を作るのはMicrosoft FoundryのHosted Agentで、
質問のたびにindexを検索してから答える。

## 2. 開発の背景

AzureのAIスタックを勉強する環境として、何か動くものを作りたかった。
特に試したかったのは、Hosted AgentとCosmos DBのvector検索、それにFoundryのtraceとevaluation。

RAGの題材は、最悪外へ漏れても困らないものとして自分の技術ブログを選んだ。

## 3. 主な機能

### Slackから、自分のブログを踏まえた回答を得る

Slack DMで質問すると、自分が書いた記事を根拠にした回答が返る。
回答の末尾には、根拠にした記事のタイトルと、commitを固定したGitHub URLが出典として付く。
根拠になる記事が見つからないときは、無いと答える。

### GitHubの記事を自動でvector化する

記事を置いた非公開GitHub repositoryを日次で走査し、追加・更新・削除をindexへ反映する。
Markdownの見出し構造をもとにchunkへ分け、embeddingを作ってCosmos DBへ入れるところまで自動で行う。

### Foundryで回答品質を評価する

質問データセットをFoundryのbatch evaluationへ流し、デプロイ済みのHosted Agentが返した回答を採点する。
判定は、期待する振る舞いを満たしているかのjudge採点と、期待した記事を出典に挙げたかの
決定的な判定の二つ。採点基準を変えると別の評価枠になるため、基準の違うrunが混ざらない。

## 4. 主な特徴・設計上のポイント

### コールドスタートを前提にした非同期化

個人開発ではコールドスタートが壁になる。Slackのevent受信には短い応答期限があるが、
Flex ConsumptionのFunction AppとHosted Agentは、起動から回答生成までその期限に収まらない。

そこで受信と回答生成を切り離している。受信Functionは署名検証とallowlist確認だけを行い、
event IDをTable Storageへ一度だけclaimしてQueueへ積み、すぐに応答を返す。
回答はQueue triggerのworkerが作り、元messageのthreadへ返信する。

会話履歴はResponses側に持たせ、アプリケーションが保存するのは最新のresponse IDだけにしている。

### 一つの質問を、一本のtraceとして追う

Slack受信、queue、記事検索、回答生成は別々の実行単位で動くが、
Foundryからは一本のtraceとして確認できる。Slack受信spanのW3C traceparentを
Queue messageへ載せ、worker側でその文脈を継続し、
Agent側はSDKのinstrumentationが生成するspanがそこへ繋がる。

span名は固定し、属性は識別子・件数・結果だけのallowlistに絞っている。
SDKとframeworkが出すtraceを主軸とし、同じ事実をcustom計装で作り直さない。
logはspanに無い失敗原因だけを、application logger配下に限って収集する。

## 5. システム構成

![技術ナレッジ検索エージェントの構成図](docs/architecture/architecture.svg)

runtime境界はFunction AppとHosted Agentの二つ。同期、Slack受信、workerは
一つのFunction Appとidentityを共有し、記事検索も独立したAPIにせず、
Hosted Agentのprocess内toolからCosmos DBへ直接引いている。

## 6. 技術スタック

| 分類 | 技術 | 役割 |
| --- | --- | --- |
| 入口 | Slack Events API（DM） | 質問の受け付けと、threadへの回答返信 |
| 実行基盤 | Azure Functions（Flex Consumption / Python 3.13） | 記事同期、Slackイベント受信、回答worker |
| Agent | Microsoft Foundry Hosted Agent / Agent Framework（Responses） | 回答生成と会話状態の保持 |
| 検索・永続化 | Azure Cosmos DB for NoSQL | 記事chunkの保存とvector検索 |
| 非同期・状態 | Azure Storage（Queue / Table） | 回答処理のqueueingと、event IDの重複排除 |
| 記事のsource | GitHub（非公開repository） | Zenn形式のMarkdown記事の取得元 |
| 観測 | Application Insights / OpenTelemetry | traceとlogの収集 |
| 評価 | Foundry batch evaluation | 回答品質の採点 |
| IaC・デプロイ | Bicep（AVM） / Azure Developer CLI | Azure resourceのprovisionとdeploy |
| CI | GitHub Actions | lint、テスト、Bicep build、方針チェック |

## 7. 補足

コードから復元できない設計判断だけを[ADR](docs/adr/)に残している。

アプリケーションコードは公開するが、記事本文のsource repositoryは非公開とし、
credential、個人情報、実resourceのID・名前・endpointは置かない。

単一利用者・単一環境で動かしている個人用のアプリで、
汎用テンプレートや第三者向けのセットアップ手順は提供しない。
