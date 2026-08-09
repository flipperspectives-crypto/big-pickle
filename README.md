[![Sponsor](https://img.shields.io/badge/Sponsor-❤%20flipperspectives-2ecc71)](https://github.com/sponsors/flipperspectives-crypto)

# Big Pickle

One API. Every model. Prepaid credits, automatic failover, and free local inference.

Big Pickle is an OpenAI-compatible inference gateway. Customers get one API key and prepaid credits; the gateway
routes each request across multiple providers (Groq, Cerebras, OpenAI, Anthropic, DeepInfra, Together, Fireworks,
Hugging Face, OpenRouter — plus any local Ollama models at $0) and transparently fails over when a provider is down.
Usage is metered per token and deducted from the customer's credit balance.

## Features

- **OpenAI-compatible** — swap the base URL and key; works with the OpenAI SDK, LangChain, curl, anything.
- **Prepaid billing** — balance per key, checked before every request (`402` when empty). No subscriptions.
- **Retail markup** — retail = upstream cost × `GATEWAY_MARKUP` (default 1.25), configurable per deployment.
- **Automatic failover** — providers are tried in order; a dead provider rolls to the next, `502` only if all fail.
- **Anthropic support** — Claude messages translated to OpenAI format, SSE streams converted.
- **Local models for free** — call `local:<model>` to route to a self-hosted Ollama server at $0 cost.
- **Admin API** — create customer keys, top up credits, view all usage.
- **Customer dashboard** — landing page with signup, playground, and API docs at `/`.

## Quickstart

```bash
pip install -r requirements.txt
# provider keys are optional (only needed for the providers you enable)
export GATEWAY_ADMIN_KEY="$(openssl rand -hex 16)"
export GATEWAY_CEREBRAS_KEY="csk-…"   # etc. per provider
uvicorn app.main:app --host 0.0.0.0 --port 8765
```

### Docker

```bash
docker build -t big-pickle .
docker run -p 7860:7860 \
  -e GATEWAY_ADMIN_KEY="$(openssl rand -hex 16)" \
  -e GATEWAY_CEREBRAS_KEY="csk-…" \
  -v gwdata:/data big-pickle
```

### Configuration (env vars)

| Var | Default | Description |
|---|---|---|
| `GATEWAY_ADMIN_KEY` | `admin-change-me` | admin key for `/v1/keys`, `/v1/credits` (header `x-admin-key`) |
| `GATEWAY_DB` | `/data/gateway.db` | SQLite path |
| `GATEWAY_MARKUP` | `1.25` | retail = cost × markup |
| `GATEWAY_<PROVIDER>_KEY` | — | e.g. `GATEWAY_GROQ_KEY`, `GATEWAY_OPENAI_KEY`, … |
| `HF_TOKEN` | — | optional Hugging Face key (`GATEWAY_HF_KEY` alias) |
| `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` | — | optional Stripe webhook verification |
| `STRIPE_API_KEY`, `STRIPE_PRICE_USD` | — | enable Stripe checkout top-ups (`/v1/checkout`) |

Local inference: set Ollama's OpenAI-compatible endpoint as provider `local`
(`http://127.0.0.1:11434/v1`). Any model id after `local:` is passed through to Ollama.

## API

| Endpoint | Auth | Purpose |
|---|---|---|
| `POST /v1/chat/completions` | customer key | OpenAI chat completions (streams supported) |
| `GET /v1/models` | customer key | list available canonical models |
| `GET /v1/usage` | customer key | tokens, cost, remaining balance |
| `POST /v1/signup` | public | create a new customer key |
| `POST /v1/checkout` | customer key | Stripe checkout URL to top up credits |
| `POST /v1/keys` | admin | create a key (returns `id` + `skey`) |
| `POST /v1/credits` | admin | top up a key's balance |
| `GET /v1/admin/usage` | admin | aggregate usage across all keys |
| `POST /v1/webhooks/stripe` | HMAC | Stripe checkout/paid webhook (top-up on payment) |
| `GET /health` | — | liveness |
| `GET /` | — | customer dashboard |

### Example

```bash
# create a key
curl -X POST https://YOUR_HOST/v1/signup -H "Content-Type: application/json" -d '{"name":"my-app"}'

# top up (admin)
curl -X POST https://YOUR_HOST/v1/credits \
  -H "x-admin-key: $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"key_id":"<id>","amount":5.00}'

# chat
curl https://YOUR_HOST/v1/chat/completions \
  -H "Authorization: Bearer $SKEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-oss-120b","messages":[{"role":"user","content":"hi"}]}'
```

## Providers

Routing is per canonical model in `app/providers.py` (`ROUTES`). Add a provider by adding its base URL,
pricing table, and model mapping. Pricing is per 1M tokens `(input, output)` in USD.

## Tests

```bash
python test_smoke.py     # API + billing flow against a live instance
python test_failover.py  # provider-down -> failover -> next provider serves
```
