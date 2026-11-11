# apigate

One OpenAI-compatible endpoint over **pools of API keys** from many providers (Gemini, OpenAI, Claude, DeepSeek, Groq, Mistral, xAI, OpenRouter, Together, any OpenAI-compatible server).
Per-key limits (requests and tokens per second / minute / hour / day / week), key-selection strategies, credits shared by API and web chat, and a web UI for users and admins.

```
client (OpenAI SDK) ──► /v1/chat/completions ──► pick key (round-robin | random | fill-to-70% | least-loaded)
web chat / admin UI ──►  credits hold ─► limits reserve ─► provider ─► settle (limits + credits ledger)
                                         └─ on 429/5xx/auth error: cool the key down, try the next key / alias target
```

## Quick start

Python ≥ 3.11.

```bash
pip install -e .            # fastapi, uvicorn, httpx, cryptography
apigate init                # creates ./data (database + master.key)
apigate create-admin admin  # prompts for a password (min 12 chars)

# add provider keys (prompted, never on the command line):
apigate add-key gemini --label account-1
# or bulk-import a file with "label:key" per line (a local file that is git-ignored), then DELETE the file:
apigate import-keys gemini keys.txt

# limits: ready-made Gemini free-tier preset (whitelist; edit to match your AI Studio table), then check what is served:
apigate load-limits presets/gemini-free.toml
apigate models gemini
# or single rules, e.g. Gemini free tier (values from AI Studio):
apigate set-limit gemini "gemini-*-flash" --rpm 5 --tpm 250000 --rpd 20
apigate set-limit gemini "gemini-*-flash-lite*" --rpm 15 --tpm 250000 --rpd 500
apigate set-limit gemini "gemini-2.5-pro" --rpd 0          # 0 = this key cannot serve it -> hidden

apigate serve --port 8080   # UI at http://127.0.0.1:8080, API at /v1
```

Open the UI, sign in, then **Admin → Providers & keys → Refresh models** (also runs on start and every 6 h).
Everything above can also be done in the Admin UI.

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="sk-gate-…")
print([m.id for m in c.models.list()])           # gemini/gemini-2.5-flash, anthropic/claude-…, openai/gpt-…
c.chat.completions.create(model="gemini/gemini-2.5-flash", messages=[{"role": "user", "content": "hi"}], stream=True)
```

Model ids are `provider/model`. A bare id works when it is unambiguous. **Aliases** (Admin → Routing) such as `smart` map to several models with `fallback`, `round_robin` or `random`.

### In-process use (e.g. from your agent library)

```python
from apigate import Gateway, Settings
gw = Gateway(Settings.load()); await gw.start()
res = await gw.chat(gw.internal_principal(), {"model": "smart", "messages": [...]})   # no credits, no HTTP
```

## Key selection

Per provider (Admin → Providers) or globally:

| strategy | behaviour |
|---|---|
| `round_robin` | rotate over usable keys |
| `random` | uniformly random usable key |
| `fill` | stay on the first key (by priority) until **any** of its limits is ≥ 70 % used (threshold is configurable), then the next; when all are ≥ 70 % continue with the least loaded |
| `least_loaded` | always the key with the lowest fill ratio |

A key is skipped when: disabled, rejected by the provider (401 → disabled until an admin resets it), cooling down after a 429/5xx, not able to see that model (per-key discovery), or when a limit would be exceeded. If the chosen key fails, the next one is tried (up to `max_attempts`, default 4), then the next alias target. If everything is at capacity the client gets `429` with `Retry-After`; short waits (default ≤ 5 s) are absorbed by queueing instead.

## Limits

Metrics: `rps rpm rph rpd rpw` (requests) · `tps tpm tph tpd tpw` (tokens) · `conc` (in-flight). `0` = the key cannot serve the model. For each metric the most specific matching rule wins (key > provider-wide; exact model > glob > `*`). `scope=model` keeps a counter per model, `scope=key` shares one counter among all models a rule matches (use it for account-wide caps).
Counters are bucketed sliding windows, enforced **before** the request is sent; tokens are reserved from an estimate and corrected with real usage. Usage is persisted per minute, so day/week windows survive restarts. Upstream rate-limit headers and `Retry-After` also trigger cooldowns.

## Credits

One balance per user, used by API keys *and* the web chat. Unit: 1 credit = 1 000 000 micro-credits. A request first **holds** its worst-case cost (atomic, balance can never go negative, concurrent requests cannot overspend), then settles to the real cost and refunds the rest. Stale holds are refunded on startup. Every charge is a row in the ledger. Admins top up with *Users → Credits…* or `apigate grant`; `unlimited` users are metered but not charged.
Prices (credits per 1M tokens, Admin → Routing) are placeholders (1 in / 3 out) until you define what a credit is. **There is no mechanism for users to obtain credits yet** — `Credits.adjust()` is the single hook to call from a future top-up flow.

## Security model (summary — see SECURITY.md)

- Provider keys: AES-256-GCM at rest, bound to their row id, master key from `APIGATE_MASTER_KEY` or `data/master.key` (0600); never returned by any endpoint, scrubbed from logs/errors.
- Gateway API keys (`sk-gate-…`): shown once, stored only as HMAC-SHA256. Optional per-key expiry, rpm/concurrency caps and model allow-lists. API keys can never reach admin/session routes.
- Passwords: scrypt. Sessions: random, HMAC-stored, `HttpOnly; SameSite=Strict` (+`Secure` and `__Host-` prefix when `cookie_secure`). CSRF token + Origin/Sec-Fetch-Site checks on every state-changing session request.
- Login throttling per IP and per user, constant-time-ish unknown-user path; invalid-API-key throttling per IP.
- Strict CSP (no inline script/style, no CDN), `nosniff`, `frame-ancestors none`, no-store on API, request body limit, per-key/per-user concurrency and rpm caps.
- SSRF guard on provider base URLs (admin only; `http://` only for loopback; metadata/link-local blocked); redirects never followed.
- Admin actions are written to an audit log.

## Deploying on a server

1. Put TLS in front (nginx/Caddy on the host), set `APIGATE_COOKIE_SECURE=true`, `APIGATE_HSTS=true`, `APIGATE_PUBLIC_ORIGIN=https://llm-apis.example.org`, and `APIGATE_TRUSTED_PROXIES=<proxy ip>` so client IPs (throttling, audit) come from `X-Forwarded-For`.
2. Pass the master key from a secret store: `APIGATE_MASTER_KEY=$(…)` and delete `data/master.key`. **Back it up** — without it stored provider keys are unrecoverable.
3. Proxy buffering must be off for streaming (`X-Accel-Buffering: no` is already sent).
4. Back up `data/gate.db` (SQLite WAL; use `sqlite3 .backup`).

**Scaling:** run **one process** (the default). Limit counters, cooldowns and the credit hold path are in memory/SQLite of that process; asyncio handles many concurrent users and streams. To scale beyond one process, move counters to Redis first.

Settings: `config.example.toml`, or `APIGATE_<NAME>` environment variables.

## Docker / VPS (one container + nginx on the host)

The container is limited to 1 CPU / 2 GB and listens on `127.0.0.1:18080` only; TLS and domain routing are done by the host's nginx
(config in `deploy/nginx/`). Step-by-step guide (Polish): [`docs/TUTORIAL.pl.md`](docs/TUTORIAL.pl.md).

```bash
cp .env.example .env     # set DOMAIN and APIGATE_MASTER_KEY
docker compose up -d --build
docker compose exec apigate apigate create-admin <name>
python scripts/smoke.py --base https://$DOMAIN --key sk-gate-...
```

Data (SQLite + usage history) lives in the `apigate-data` volume - back it up together with `APIGATE_MASTER_KEY`.
Transient upstream 5xx ("high demand") are retried on the same key before failing over.

## Tests

```bash
pip install -e .[dev] && pytest        # 49 tests against an in-process fake of OpenAI/Gemini/Claude upstreams
```

Real providers have not been exercised by the test-suite (no keys in CI): do a first run with one key per provider and watch `apigate status` / the Providers tab.

## Terms-of-service note

Spreading one workload over many accounts to get around a provider's free-tier quotas can violate that provider's terms (Google's included). The gateway enforces *each key's own* limits and never exceeds them, but whether pooling keys across accounts is allowed is your responsibility.
