# mistral-gateway

A small local proxy that lets any OpenAI-compatible chat client (Open WebUI, editors, scripts) use the models included in your Mistral Vibe subscription. It is a single Python file with no dependencies beyond the standard library.

It uses the same key the official [Mistral Vibe CLI](https://github.com/mistralai/mistral-vibe) creates when you log in, sends requests to Mistral's public API and returns the answers in the exact shape OpenAI clients expect.

> **Unofficial project.** Not affiliated with, endorsed by or supported by Mistral AI. Read the "Terms of service" section below before using it.

## What it does

- Exposes `/v1/chat/completions` and `/v1/models` on `127.0.0.1:8788`, so a client only needs a base URL.
- Fixes wire-format differences that break OpenAI clients:
  - reasoning models return `content` as a list of typed parts; the gateway flattens it to plain text and moves the thinking into `reasoning_content`, in both streaming and non-streaming replies
  - a final `finish_reason: "stop"` after tool-call deltas is rewritten to `tool_calls`
  - `stream_options.include_usage` is added to streaming requests so clients get token counts
  - `max_tokens` defaults to 32768 when the client sends none (Mistral's default is 4096, which reasoning models can use up before writing any visible text)
  - `reasoning_effort: "high"` is added to turns that answer a tool result, because reasoning models sometimes end those turns with empty output otherwise
- Handles rate limits politely: on HTTP 429 it waits and retries the same key every 10 s for up to 60 s.
- Optional fallback to a Mistral AI Studio API key when the Vibe key is rate limited or its budget is spent.
- Records daily token usage per model in a local SQLite file (`usage.db`), readable at `/usage`.

## Setup

Requirements: Python 3.10+ and a Mistral plan that includes Vibe (Le Chat Pro, Team or similar).

1. Install the official Vibe CLI (see the [Mistral Vibe repository](https://github.com/mistralai/mistral-vibe) for other install methods):

   ```sh
   curl -LsSf https://mistral.ai/vibe/install.sh | bash
   ```

2. Log in with your Mistral account. Pick the browser login in the setup wizard:

   ```sh
   vibe --setup
   ```

   The CLI stores your plan key in `~/.vibe/.env`. The gateway reads it from there, so you never copy the key by hand.

3. Get the gateway and start it:

   ```sh
   git clone https://github.com/Classic298/mistral-gateway.git
   cd mistral-gateway
   python3 gateway.py
   ```

   The log shows your plan name when the key works:

   ```
   INFO vibe plan: ... (...)
   INFO mistral gateway on 127.0.0.1:8788 (keys: vibe)
   ```

4. Point your client at it:

   - Base URL: `http://127.0.0.1:8788/v1`
   - API key: anything (or the value of `GATEWAY_KEY` if you set one)
   - Model: any ID from `curl http://127.0.0.1:8788/v1/models`

   Quick test:

   ```sh
   curl http://127.0.0.1:8788/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model": "mistral-small-latest", "messages": [{"role": "user", "content": "Hello"}]}'
   ```

### Run it as a service (Linux, optional)

```sh
mkdir -p ~/.config/systemd/user
cp mistral-gateway.service.example ~/.config/systemd/user/mistral-gateway.service
# edit WorkingDirectory and ExecStart to point at your clone
systemctl --user daemon-reload
systemctl --user enable --now mistral-gateway
journalctl --user -u mistral-gateway -f
```

## Configuration

Settings come from environment variables or from a `gateway.env` file next to `gateway.py` (see `gateway.env.example`; `gateway.env` wins over the environment).

| Variable | Default | Meaning |
|---|---|---|
| `VIBE_KEY` | `MISTRAL_API_KEY` from `~/.vibe/.env` | Your Vibe plan key. Only set it if you do not want the CLI's file to be used. |
| `VIBE_ENV_FILE` | `~/.vibe/.env` | Where to look for the Vibe CLI's key. |
| `STUDIO_API_KEY` | unset | Optional pay-as-you-go fallback key from Mistral AI Studio. |
| `GATEWAY_KEY` | unset | If set, clients must send it as `Authorization: Bearer <key>`. |
| `BIND` | `127.0.0.1` | Listen address. Keep it on loopback (see below). |
| `PORT` | `8788` | Listen port. |
| `RETRY_429_SECONDS` / `RETRY_429_INTERVAL` | `60` / `10` | How long and how often to retry a rate-limited key. |
| `COOLDOWN_429` / `COOLDOWN_DEAD` | `60` / `21600` | Seconds a key is skipped after rate limiting / after 401 or 402. |
| `LOG_LEVEL` | `INFO` | Python logging level. |

Endpoints: `/v1/chat/completions`, `/v1/models`, `/health`, `/pool/status` (which key is serving, cooldowns) and `/usage?days=N`.

## Terms of service

Checked against the [Mistral AI Terms of Service for EU consumers](https://legal.mistral.ai/terms/eu-consumers-terms-of-service) (effective August 7, 2026) and the [Mistral AI Commercial Terms of Service](https://legal.mistral.ai/terms/commercial-terms-of-service) (effective August 5, 2026). Consumers outside the EU have [separate terms](https://legal.mistral.ai/terms). This section is the author's reading of those documents and is **not legal advice**.

**Why the gateway is designed to stay within those terms:**

- **Official key, official API.** The key comes from Mistral's own login flow in the official Vibe CLI. The gateway calls the same public API endpoint with the same standard `Authorization: Bearer` header the CLI uses. It does not scrape, impersonate the CLI, reverse engineer anything or touch any security mechanism. Mistral's [API key documentation](https://docs.mistral.ai/admin/identity-access/api-keys) lists Vibe keys as "Keys used by Vibe Code" and states that "Vibe-only users usually do not need API keys unless they also use Studio, the API, Vibe Code, or another developer tool."
- **Your plan's limits stay in force.** Every request counts against your plan exactly like a Vibe CLI request. A rate limit makes the gateway wait and retry the same key; it never switches to another account to get around a limit.
- **One person, one account.** The EU consumer terms state: "The creation or use of multiple Mistral AI accounts by a single individual is strictly prohibited, including to bypass rate limits or any other restrictions." The gateway therefore supports exactly one Vibe key.
- **Personal use only.** The consumer terms also state: "Your account is intended for your individual use only, and you may not share your account with any other person. [...] You may not make your account credentials available to third parties, [...] or resell or lease access to your account." The commercial terms (section 2.2) forbid to "buy, sell, or transfer API keys" and to "grant any third party access to the Mistral AI Products without our prior written authorization". The gateway therefore listens on `127.0.0.1` only. **Do not expose it to the internet or share it with other people**; doing so would hand them access to your account.

**Your responsibility.** Mistral can change its terms at any time. Before you use this gateway, and again whenever Mistral announces a change, read the current terms yourself and stop using the gateway if they no longer allow it. Mistral decides how to enforce its own terms, and nobody can promise that an account will not be restricted or suspended.

## Disclaimer

This software is provided "as is" under the [PolyForm Noncommercial License 1.0.0](LICENSE), without warranty of any kind. The author accepts no liability for any damage or loss arising from its use, including suspension, restriction or termination of your Mistral account, lost subscription fees or lost data.

## License

[PolyForm Noncommercial 1.0.0](LICENSE): free for personal, hobby, research and other noncommercial use. Commercial use is not permitted.
