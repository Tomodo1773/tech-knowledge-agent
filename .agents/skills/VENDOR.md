# vendorしたskillの由来

## microsoft-foundry

| | |
|---|---|
| 上流 | [microsoft/azure-skills](https://github.com/microsoft/azure-skills) `skills/microsoft-foundry` |
| pin | `19290671e4f061b2a8c54fa2d42cdbc979000106` (2026-08-12) |
| skill version | 1.2.6 |
| license | MIT |
| 取得日 | 2026-08-13 |

[CLAUDE.md](../../CLAUDE.md)がFoundry関連の作業でこのskillを先に読むよう指示しているため、環境に依存せず常に読める状態にする目的でrepoへ取り込んだ。上流のsubtreeを無改変でコピーしてあるので、差分は上流とそのまま比較できる。

### なぜplugin installではなくskill単体か

同じskillはClaude Code公式marketplaceの`azure` pluginでも配布されているが、pluginは3点でこのrepoの方針と衝突する。

| 衝突 | 内容 |
|---|---|
| supply chain | MCP serverが`npx -y @azure/mcp@latest`。pin無しでnpmから毎回取得し、`sfw`を通らない |
| deny list | `Bash(npx:*)`はユーザーのsettings.jsonでdeny済み |
| hook | `PostToolUse`で`hooks/scripts/track-telemetry.sh`が全tool呼び出しごとに走る |

加えてpluginは28 skillを一括でregisterする。ここで必要なのは`microsoft-foundry`だけなので、単体をpin付きでvendorするほうが供給網としても軽い。

### 動かない範囲

`SKILL.md`はFoundry MCP操作の前にAzure MCPの`foundry` toolを呼ぶよう指示しているが、**MCP serverは入れていないのでこの経路は使えない**。reference markdown（`foundry-agent/`、`models/`、`rbac/`など）は単体で読めるので、CLAUDE.mdが求めている「先に読む」用途には足りる。

`scripts/check-and-setup-dependencies.ps1`は`azd extension install microsoft.foundry`を実行するだけで、パッケージマネージャーは踏まない。`sfw`方針とは衝突しない。

### 更新手順

```sh
SHA=<上流の新しいcommit>
gh api "repos/microsoft/azure-skills/tarball/$SHA" > azure-skills.tar.gz
rm -rf .claude/skills/microsoft-foundry
tar -xzf azure-skills.tar.gz -C .claude/skills --strip-components=2 \
  "microsoft-azure-skills-${SHA:0:7}/skills/microsoft-foundry"
```

取り込んだらこのファイルのpinと取得日も更新する。
