# codex-team

Run Codex CLI as a *mixed* team: the lead stays on your official
ChatGPT-authenticated Codex session, and persistent teammates run on any
OpenAI-compatible model endpoint you already have — self-hosted or hosted.

The bridge speaks MCP to the lead and drives one child
`codex app-server` whose `CODEX_HOME` is configured for your own provider.
Each teammate is a native Codex thread that keeps its context across turns:
the lead follows up, steers a running turn, or cancels one worker without
touching the others. Workers can also message each other directly by name
through a small worker-side MCP, without relaying through the lead.

## What you get

- **Persistent teammates** — `team_start` opens a native Codex thread on your
  provider; `team_send` steers its running turn or continues it with full
  retained context; `team_cancel` interrupts only that worker.
- **Named peer messaging** — every worker gets a `team_message` tool; a worker
  can hand a task to another named worker in-place instead of asking the lead
  to relay.
- **Local dashboard** — a per-session localhost page where you watch each
  worker's stream, send a follow-up into the same thread, or cancel one.
- **No credential mixing** — the worker spawn scrubs the lead's auth
  environment variables; teammates only ever see your own provider's config.
- **One TOML file** names your endpoint, its auth, and the alias → model
  mapping. Tokens live in environment variables, never in the config.

## Quick start

Requires Codex CLI and Python 3.11+.

```bash
cp team.config.example.toml team.config.toml   # edit: your endpoint + models
```

Point the lead at the bridge by adding this to your `~/.codex/config.toml`
(see `codex-config.example.toml` for a fuller template with instructions
that make the lead prefer the team tools):

```toml
[mcp_servers.codex_team]
command = "python"
args = ["C:/path/to/codex-team/bridge.py"]
startup_timeout_sec = 15
default_tools_approval_mode = "auto"
```

Start Codex as usual and ask it to start teammates, e.g. "start a teammate
named `builder` on model alias `fast` to implement X". The bridge returns a
dashboard link; workers reply in their own threads.

Run the offline test suite:

```bash
python -m unittest discover -s tests
```

## Lifecycle and limits

- The bridge and its workers exist only while the lead's session is running.
  Closing the lead ends the bridge and its worker processes. Native thread
  histories remain on disk; automatically restoring a whole team after a
  restart is not implemented.
- Named routing saves the lead from re-briefing context; workers re-reading
  their own history still consumes provider tokens, and the lead still
  consumes official quota.
- `team_status` returns bounded output to protect the lead's context; read
  full results from the dashboard.

## Why this exists

Codex CLI already has experimental multi-agent features. In testing against
Codex CLI 0.160.1, native subagents still called the ChatGPT endpoint even
when configured with another provider and failed with an unsupported-model
error, while this bridge path (separate provider config, separate
`CODEX_HOME`, separate app-server child) worked. That is a version-specific
observation, not a claim that native provider mixing never works — check
your CLI version first; if native mixing works for you, prefer it.

The sibling project [claude-team-router](https://github.com/u6vu06Ou/claude-team-router)
does the same job for Claude Code via HTTP translation. This repo is the
Codex CLI counterpart, via MCP plus the app-server protocol instead.

**Risk note:** OpenAI does not officially support driving Codex teammates
through a second provider this way. It works because the app-server accepts
`model_provider` per thread; treat it as your own risk, and expect breakage
if that protocol changes.

## 繁中速覽

把這個 MCP bridge 掛進 Codex 設定：lead 用你官方 ChatGPT 登入的 Codex
session，持續隊員走你自己設定的任何 OpenAI 相容端點（自架或買的）。
`team.config.toml` 填端點與 alias→模型對照 → lead 用 `team_start` 開
隊員、`team_send` 追問或插話、`team_cancel` 單獨取消 → bridge 回傳本機
看板連結可直接對每位隊員補需求。隊員之間可用 `team_message` 按名字
直接傳話，不用每則都經 lead 轉述。注意：關掉 lead 整隊即結束、不會
自動恢復；與其他作法（Codex 原生多 agent、claude-team-router）的差別
見上方說明。

## License

MIT — see [LICENSE](LICENSE).
