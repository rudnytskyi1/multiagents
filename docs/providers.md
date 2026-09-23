# Providers

A provider is a bundle of: an Anthropic-compatible endpoint, a key source, default models,
aliases and prices. Two ship built in; you can add your own.

## fireworks (default)

- Endpoint: `https://api.fireworks.ai/inference` (US infrastructure)
- Key: Keychain `fireworks-api` → env `FIREWORKS_API_KEY` → `~/.multiagents/fireworks.key`
- Default model, every role: `accounts/fireworks/models/deepseek-v4p1-flash`
  (DeepSeek V4.1-Flash: multimodal input, 1M context)
- Alias: `flash`
- Prefilled price: $0.22 / $0.007 / $0.66 per 1M input / cached input / output
- Find other ids with `multiagents models` (serverless catalog varies by account)

## deepseek

- Endpoint: `https://api.deepseek.com/anthropic` (DeepSeek's first-party API, hosted in China —
  see the data-residency note in [security.md](security.md))
- Models listing: `https://api.deepseek.com/models`
- Key: Keychain `deepseek-api` → env `DEEPSEEK_API_KEY` → `~/.multiagents/deepseek.key`
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
multiagents provider deepseek            # user-wide default
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
OpenAI-style `{"data": [{"id": …}]}` (used only by `doctor`/`models`). Run
`multiagents doctor` after adding one — the self-test and model probes cover the new surface.
