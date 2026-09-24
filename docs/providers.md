# Providers

A provider is a bundle of: an endpoint (Anthropic-compatible, or OpenAI-compatible through the
local bridge), a key source, default models, aliases and prices. Three ship built in; you can add
your own.

## fireworks (default)

- Endpoint: `https://api.fireworks.ai/inference` (US infrastructure)
- Key: env `FIREWORKS_API_KEY` → Keychain `fireworks-api` → `~/.multiagents/fireworks.key`
- Default model, every role: `accounts/fireworks/models/deepseek-v4p1-flash`
  (DeepSeek V4.1-Flash: multimodal input, 1M context)
- Alias: `flash`
- Prefilled price: $0.22 / $0.007 / $0.66 per 1M input / cached input / output
- Find other ids with `multiagents models` (serverless catalog varies by account)

## hive

- Endpoint: `https://api-cdn.thehive.ai/api/v3` (Hive, US; Virginia customers can point
  `base_url` at `https://api-va1.thehive.ai/api/v3`)
- API: **OpenAI-compatible only** (`/chat/completions`) — workers reach it through the local
  bridge (below)
- Key: env `HIVE_API_KEY` → Keychain `hive-api` → `~/.multiagents/hive.key`
  (create a V3 key under Service API Keys at thehive.ai; it is the "Secret Key")
- Default model, every role: `deepseek-ai/deepseek-v4.1-flash` (text + image input, 1M context)
- Alias: `flash`
- Prefilled price: $0.12 / $0.0024 / $0.48 per 1M input / cached input / output — Hive's
  discounted rate at the time of writing (list price $0.30 / $0.006 / $1.20); adjust `prices`
  if it changes
- No model-list endpoint: `multiagents models` prints the configured models and `doctor` checks
  each model with a real test request (with a tool and Claude Code's 32k output cap, the shape
  workers send)
- Limits and errors (per Hive's docs): 5 requests/second by default (429s are retried by Claude
  Code with backoff); HTTP 405 means the organization is paused — usually out of credit

### The bridge

Workers are Claude Code processes, and Claude Code speaks only Anthropic's Messages API. For a
provider with `"api": "openai"`, `multiagents run` (and `doctor`) start a translation bridge
inside the CLI process, on `127.0.0.1` with a random port, for the length of the run:

```
worker (claude -p) ──Anthropic API──► bridge 127.0.0.1:<port> ──OpenAI API──► Hive
     token: random, one run              holds the Hive key
```

- The provider key is read by the CLI and stays in the bridge. The worker's `apiKeyHelper`
  prints a random one-run token from a 0600 file under `~/.multiagents/run/` that is deleted
  when the run ends; the bridge refuses requests without it.
- Upstream requests are built from scratch — no header from the worker is forwarded.
- Translated: system prompt, text, images (tool screenshots included), tool definitions, tool
  calls and results, `tool_choice`, streaming, stop reasons, and token usage (input / cached /
  output, so `status` costs stay right).
- Everything streams through as the model produces it — reasoning (as thinking blocks, the way
  Anthropic-API providers of DeepSeek return it), text, and tool-call arguments. When the
  provider goes quiet, SSE pings and (every 30 s) an event that changes nothing keep Claude
  Code's stream watchdogs from giving up on a slow answer. When the worker hangs up, the bridge
  drops the provider's stream instead of paying for the rest of the answer.
- Within a tool-calling turn the reasoning goes back to the provider as `reasoning_content`, as
  DeepSeek expects. If the provider rejects a request with a 400 that names that field, the
  bridge retries without it, and if that works, leaves it out for the rest of the run.
- A stream that breaks off, an answer the provider cut short (a `finish_reason` such as
  `insufficient_system_resource`) and an empty answer are reported to the worker as retryable
  errors, not passed on as complete answers; Claude Code retries them.
- Each round writes `NN-<role>-bridge.log` next to its other logs: one line per API call
  (status, seconds, tokens) plus the provider's error messages — not the conversation, never
  the key (should a provider quote the key in an error, it is blanked out there and in what
  the worker is told).
- Provider options (user config, inside the provider block): `max_output_tokens` caps Claude
  Code's `max_tokens` for providers with a lower limit; `extra_body` is merged into every
  request (e.g. `{"reasoning_effort": "high"}` where the provider supports it).

## deepseek

- Endpoint: `https://api.deepseek.com/anthropic` (DeepSeek's first-party API, hosted in China —
  see the data-residency note in [security.md](security.md))
- Models listing: `https://api.deepseek.com/models`
- Key: env `DEEPSEEK_API_KEY` → Keychain `deepseek-api` → `~/.multiagents/deepseek.key`
  (create one at platform.deepseek.com)
- Default model, every role: `deepseek-flash` (V4.1-Flash — vision, 1M context, 384K max output)
- Aliases: `flash` → `deepseek-flash`, `pro` → `deepseek-v4-pro` (stronger, thinking effort
  levels, **no vision** — screenshots in feedback won't be seen if you put the fixer on it)
- Prefilled prices (peak, USD/1M): flash $0.30 / $0.006 / $1.20; pro $1.32 / $0.044 / $3.96.
  Off-peak is 50% of peak (peak = Mon–Fri 01:00–04:00 and 06:00–10:00 UTC, excluding Chinese
  public holidays). Estimates in `status` use peak; your bill may be half that.
- Discontinued: `deepseek-chat` and `deepseek-reasoner` were retired in July 2026 — don't use
  ids from older tutorials.
- Endpoint quirks (per DeepSeek's docs): `anthropic-version`/`anthropic-beta` headers are
  ignored, caching is automatic (no `cache_control`), unknown Claude model names silently map
  to `deepseek-flash`.

## Switching

```bash
multiagents provider hive                # user-wide default
MULTIAGENTS_PROVIDER=deepseek …          # one invocation
```

```json
// <repo>/.claude/multiagents.json — per project, committable
{ "provider": "deepseek" }
```

`multiagents doctor` always names the active provider; the leak self-test re-runs automatically
on a provider/endpoint change (it is part of the cache key).

## Custom providers

Any Anthropic-compatible endpoint works. In `~/.multiagents/config.json` (user config only —
project files cannot define providers):

```json
{
  "providers": {
    "myrouter": {
      "label": "My router",
      "base_url": "https://example.com/anthropic",
      "models_url": "https://example.com/v1/models",
      "keychain_service": "myrouter-api",
      "key_file": "/Users/me/.multiagents/myrouter.key",
      "key_env": "MYROUTER_API_KEY",
      "models": { "coder": "some-model", "fixer": "some-model", "reviewer": "some-model",
                  "scout": "some-model", "background": "some-model" },
      "aliases": { "flash": "some-model" },
      "prices": { "some-model": { "input": 0.5, "cached_input": 0.05, "output": 1.5 } }
    }
  },
  "provider": "myrouter"
}
```

Requirements for the endpoint: `POST <base_url>/v1/messages` (Anthropic Messages schema,
streaming), auth via `x-api-key` and/or `Authorization: Bearer`. `models_url` must return
OpenAI-style `{"data": [{"id": …}]}` (used only by `doctor`/`models`); set it to `""` if the
provider has no model list. Run `multiagents doctor` after adding one — the self-test and model
probes cover the new surface.

**OpenAI-compatible endpoints** work too: add `"api": "openai"` and give the base URL that
`/chat/completions` hangs off (e.g. `https://example.com/v1`). Workers then go through the
bridge described under [hive](#the-bridge). `models_url` defaults to `<base_url>/models`. The
endpoint needs streaming chat completions with tool calling; `doctor` checks exactly that.
