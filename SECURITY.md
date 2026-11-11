# Security notes

## Threat model
Multi-user service on a server, holding valuable provider keys. Attackers considered: anonymous internet clients, malicious registered users, a stolen database/backup, a stolen log file, XSS via model output.

## What protects what
| Asset | Protection |
|---|---|
| Provider API keys | AES-256-GCM (AAD = `pk:<id>:<provider>`, so ciphertexts can't be swapped between rows); master key outside the DB; keys are only decrypted into process memory; fingerprint = last 4 chars; duplicate detection by HMAC; never in any API response, URL or log (redaction filter for known shapes + exact secrets). A canary check refuses to start with the wrong master key. |
| Gateway API keys | 256-bit random, HMAC-SHA256 at rest, shown once, revocable, expirable, per-key rpm / concurrency / model allow-list. |
| Passwords | scrypt N=2^15, per-user salt; min 12 chars; dummy hash on unknown user; lockout after 5 failures per IP/user per 15 min. |
| Sessions | random 256-bit, stored hashed, 7-day expiry, `HttpOnly`, `SameSite=Strict`, `Secure` + `__Host-` prefix when `cookie_secure`; password change signs out other sessions; deactivation kills sessions and cached API keys. |
| CSRF | per-session token header + `Origin` and `Sec-Fetch-Site` checks on unsafe methods. |
| XSS | strict CSP without `unsafe-inline`; UI builds DOM with `textContent` only; model output goes through a small safe Markdown renderer (links: http/https, `rel=noopener noreferrer nofollow`). |
| Credits | atomic conditional UPDATE; holds persisted and refunded after a crash; append-only ledger. |
| SSRF | base URLs only settable by admins, scheme/host validated, metadata IP blocked, redirects disabled. |
| Abuse | body size limit, per-key and per-user rpm + concurrency, invalid-key and signup throttles, bounded in-memory tables. |

## Operational checklist
- TLS in front; `APIGATE_COOKIE_SECURE=true`; bind the app to localhost behind the proxy.
- Master key from a secret store; keep an offline backup; rotate provider keys if `master.key` and the DB leak together.
- Delete any plaintext key file after `import-keys`.
- `data/` should be readable only by the service user (the DB and key file are created 0600 on POSIX; on Windows rely on folder ACLs).
- Review *Admin → Audit log* periodically.

## Known limits
- Single process (in-memory counters); see README “Scaling”.
- Throttling tables are per process and reset on restart.
- Chat content is stored in the database in plaintext (conversations are the user's); API request/response bodies are **not** stored.
- Token counts for reservations are estimates; real usage from the provider settles the books. A request without `max_tokens` can overshoot its credit hold; the charge is then capped at the user's remaining balance.
- No 2FA/SSO yet.

## Reporting a vulnerability
Please use GitHub's **private vulnerability reporting** (Security tab -> "Report a vulnerability") instead of a public issue.
Never post provider keys, `sk-gate-` keys, `master.key` or database files in issues.

## Running a public instance
- Keep `allow_signup=false` until credits/billing exist; otherwise anyone can create accounts.
- Pooling several free-tier keys may violate a provider's terms of service - check them before offering the gateway to third parties.
