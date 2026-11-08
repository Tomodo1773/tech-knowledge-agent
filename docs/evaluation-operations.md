# AI評価の手動運用

評価は定期実行せず、Agentを変更したときや、本番会話を見直したいときに手動で行う。

## 前提

- Hosted AgentがFoundryへdeploy済みである。
- 固定10問と8評価器の定義が`eval/`に用意されている。
- `FOUNDRY_PROJECT_ENDPOINT`と`AZURE_AI_MODEL_DEPLOYMENT_NAME`が環境変数へ読み込まれている。
- Azureへsign in済みで、実行者がFoundry projectを操作できる。
- Foundry projectにApp Insightsが接続され、Agentのtraceが記録されている。
- traceからデータセットを作る場合、projectのmanaged identityに`Monitoring Reader`と`Log Analytics Reader`が付与されている。

## Agentを変更したとき

1. 変更したAgentをdeployする。
2. repository rootで固定10問の評価を実行する。

   ```powershell
   sfw uv run --project src/functions --no-sync python scripts/run-foundry-evaluation.py
   ```

3. CLIが完了するまで待ち、表示された結果とreport URLを確認する。
4. Foundryの「Evaluations」で最新runを開き、8評価器のscore、失敗したcase、前回runとの差を見る。
5. 問題があればAgentを直し、同じ固定10問で再評価する。

この評価は、期待する振る舞いと出典を人が定義した固定データセットを使う。

## 本番のSlack会話を見直すとき

1. App Insightsで、評価実行を含まない対象期間を決める。
2. その期間のAgent traceから、Foundryのデータセットを手動で生成する。現在、この操作のCLIはrepositoryにない。
3. Foundryの「Data / Datasets」で生成結果を開き、質問と実際の回答を数件確認する。
4. 挨拶、雑談、テスト発言を除き、固定10問との入れ替え候補にする技術質問を選ぶ。
5. 選んだ質問へ期待する振る舞いと出典を付け、既存の低価値な質問と入れ替える。
6. 固定10問の評価と同じ手順で、更新後のデータセットを評価する。

本番traceのデータセットは、過去の質問と回答を確認するための材料であり、既存の8評価器へそのまま渡すものではない。
