# Using Codex over your Open Notebook (practical guide)

Drive your self-hosted **Open Notebook** research from **Codex** using your ChatGPT/Codex subscription as the reasoning brain — no OpenAI API tokens. Codex connects to Open Notebook as an MCP server and can search, read, summarize, and write across your notebooks, from anywhere you have Tailscale.

> **Status:** LIVE since 2026-06-02. Infra: PR #710 (`docs/plans/open-notebook-tailnet-ingress.md`).

## How it works (the mental model)

You type a request in plain English in Codex. Codex's GPT model decides which Open Notebook **tools** to call, calls them over MCP, and weaves the results into its answer. You don't call tools by hand — you just ask.

```
You ──▶ Codex (GPT, your subscription)
            │  MCP over local stdio
            ▼
        open-notebook-mcp  (runs on your Mac via uvx)
            │  HTTPS REST over Tailscale
            ▼
        Open Notebook API  (home cluster, :5055)
            │  embeddings / chat
            ▼
        Mac-Ollama  (local models: qwen3.6, qwen3-coder, mxbai-embed-large)
```

Two things to internalise:
- **The GPT reasoning is Codex's** (your subscription). Open Notebook supplies the *knowledge* (your sources, notes, embeddings) and runs its *own* models locally on Mac-Ollama.
- It works **off-LAN over Tailscale** — as long as your laptop is **awake + lid-open** and home is reachable.

## One-time setup (already done on this Mac)

For reference / a new machine, the pieces are:
1. **`uv`** installed (`brew install uv`) — provides `uvx`.
2. **MCP server** registered in `~/.codex/config.toml`:
   ```toml
   [mcp_servers.open-notebook]
   command = "/opt/homebrew/bin/uvx"
   args = ["open-notebook-mcp"]
   startup_timeout_sec = 60

   [mcp_servers.open-notebook.env]
   OPEN_NOTEBOOK_URL = "https://open-notebook.tuxedo-halosaur.ts.net"
   ```
3. **Tailnet ingress** exposing Open Notebook's API onto the tailnet (cluster-side; see the plan doc).
4. **Sleep hardening** so the backend doesn't drop mid-session (`caffeinate` LaunchAgent + `sudo pmset -c sleep 0`).

## Confirm it's connected

In a **fresh** Codex session (Codex reads config at launch), run:
```
/mcp
```
You should see `open-notebook` with its tools: `search`, `ask_question`, `ask_simple`, `list_notebooks`, `get_notebook`, `list_sources`, `get_source`, `create_source`, `create_note`, `get_note`, `create_notebook`, `execute_chat`, `create_chat_session`, `get_chat_context`, `list_models`, `get_default_models`, `update_settings`, and more.

## Practical recipes

Just type these (or your own variants) in Codex — it picks the right tools.

### 1. Ask your research a question
> "Search my Open Notebook for what I've saved about **Ceph deep-scrub tuning** and summarize the key takeaways with citations."

Codex calls `search` (vector search over your embedded sources), then `ask_question` / `execute_chat` to synthesize a grounded answer. Because retrieval is grounded in *your* sources, answers cite your material rather than hallucinating.

### 2. Find which notebook something is in
> "Which of my notebooks mention **Pocket** recordings? List them."

Uses `list_notebooks` + `search` to locate the right notebook(s).

### 3. Summarize a source into a note
> "Open the latest source in my **Pocket Inbox** notebook, summarize it into 5 bullets, and save it as a note titled 'Summary — <topic>'."

Uses `list_sources` / `get_source` → reasons in Codex → `create_note`.

### 4. Cross-source synthesis
> "Across all sources in my **Homelab** notebook, what decisions have I made about backups? Pull the conflicting ones."

Uses `search` across the notebook, then GPT synthesizes — this is the kind of multi-source reasoning that's painful in the Open Notebook UI but natural here.

### 5. Capture something new
> "Create a notebook called **Travel Planning** and add this URL as a source: https://example.com/itinerary"

Uses `create_notebook` + `create_source` (Open Notebook then embeds it via Mac-Ollama in the background).

### 6. Start a persistent chat session
> "Start a chat session in my **Research** notebook and ask: what are the open questions I haven't resolved?"

Uses `create_chat_session` + `execute_chat`; the session persists in Open Notebook so you can continue it later in the UI.

## Tips

- **Be specific about the notebook.** Naming the notebook ("in my Homelab notebook") helps Codex scope `search`/`list_*` calls.
- **First call of a session can be slow** (~10–15s) — cold path warmup (Codex spawns the MCP server, Open Notebook may load a model on Mac-Ollama). Subsequent calls are faster.
- **Embedding is asynchronous.** A freshly added source isn't searchable until Open Notebook finishes embedding it on Mac-Ollama. If a brand-new source doesn't show in search, wait a moment and retry.
- **GPT reasoning vs. Open Notebook's own models.** Codex uses your subscription for the thinking. Open Notebook's *own* chat/embeddings run on Mac-Ollama. They're separate — you're getting GPT-quality orchestration over locally-embedded knowledge.

## Troubleshooting

| Symptom | Check |
|---|---|
| `/mcp` doesn't list `open-notebook` | You opened Codex before adding the config — start a fresh session. Confirm `~/.codex/config.toml` has the block. |
| Tools listed but every call errors/hangs | Reachability: `curl -sf https://open-notebook.tuxedo-halosaur.ts.net/api/models/by-provider/ollama` should return 200. If not: laptop on Tailscale? Home cluster up? |
| Works at home, fails travelling | Laptop must be **awake + lid-open** on the tailnet. Closed lid in a bag sleeps the Mac → backend (and its Ollama) goes offline. |
| Search returns nothing for a new source | Embedding still in progress on Mac-Ollama, or Mac-Ollama was unreachable when the source was added (Open Notebook fails embedding silently). |
| Slow every time, not just first call | Mac-Ollama under load or swapping models (`OLLAMA_MAX_LOADED_MODELS=2`). Check `ollama ps`. |

## Security note

The Open Notebook API (`:5055`) is **unauthenticated** and the tailnet ingress **bypasses Authentik SSO** (which only gates the web UI). The guard is the tailnet (membership + ACL). Anyone on your tailnet can read/write your notebooks via this path — acceptable for a personal tailnet of your own devices; revisit if you ever add untrusted devices (the plan's deferred "T6" would add an API key).

## See also

- [Open Notebook tailnet ingress plan](/docs/plans/open-notebook-tailnet-ingress) — the infrastructure + the Codex adversarial review that hardened it.
- [Pocket → Open Notebook pipeline](/docs/plans/pocket-to-open-notebook-pipeline) — how recordings get *into* Open Notebook in the first place.
