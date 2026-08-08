# Security

This repository connects to a live brokerage account. Credential handling is
treated as a first-class concern rather than an afterthought.

## Credential handling

All secrets are supplied through environment variables, loaded from a local
`.env` file that is git-ignored. `.env.example` documents the required keys with
no values.

| Variable | Purpose |
|---|---|
| `FYERS_CLIENT_ID` | Broker API client identifier |
| `FYERS_SECRET_ID` | Broker API secret |
| `FYERS_REDIRECT_URI` | OAuth redirect target |
| `TELEGRAM_BOT_TOKEN` | Operator interface bot token |
| `TELEGRAM_CHAT_ID` | Authorised chat — the only chat that may issue commands |
| `DB_USER` / `DB_PASS` / `DB_NAME` / `DB_HOST` / `DB_PORT` | PostgreSQL connection |

The broker access token is cached to `data/access_token.txt`, which is
git-ignored, and is refreshed through the OAuth flow when it expires.

## What is never logged

- Access tokens, secrets, and the Telegram bot token
- The contents of `.env`
- Full broker API request bodies containing authentication headers

Session logs do contain symbols, quantities, prices and order identifiers,
because they are needed to reconstruct what the system did. Logs are git-ignored
and stay on the host.

## Command authorisation

Every Telegram command and inline button press is checked against
`TELEGRAM_CHAT_ID` before it is acted on. An unauthorised sender is logged and
ignored. This limits the damage if the bot token is ever exposed: possessing the
token is not sufficient to issue commands.

## Scanning

`gitleaks` runs over the **full git history**, not just the working tree, in CI
and on a weekly schedule. History is where a deleted credential still lives, and
a working-tree-only scan gives false assurance.

```bash
gitleaks detect --source . --redact --log-opts="--all"
```

Dependencies are audited with `pip-audit`, source with `bandit`, and container
images with `trivy`.

## Current findings

A full-history scan on 2026-08-07 returned **2 findings**. Both are in files that
were deleted from the working tree long ago but remain reachable in history.
Evidence (redacted): [`evidence/gitleaks-full-history.txt`](evidence/gitleaks-full-history.txt).

| # | Artefact | Commit | Status |
|---|---|---|---|
| 1 | PostgreSQL password for a local `botuser` role | `9b2f2158` (2026-04-06) | **Rotation pending** |
| 2 | Fyers OAuth `auth_code` (JWT) | `6557df96` (2026-01-30) | Expired 2026-01-09; single-use by design, no action required |

Finding 2 is inert: Fyers auth codes are single-use and expire within minutes of
issue, so a code from January carries no residual access.

Finding 1 is a real exposure and is tracked for rotation. The affected role is
bound to `localhost`, so exploitation additionally requires access to the host.

**Remediation procedure for a leaked database credential:**

```sql
ALTER USER botuser WITH PASSWORD '<new-strong-password>';
```

then update `DB_PASS` in `.env`. Nothing else reads the value —
`shortcircuit/state/database.py` resolves it from `DB_PASS`/`DB_PASSWORD` at
connection time.

History rewriting is deliberately **not** part of the remediation. Rotating the
credential renders the historical value worthless, and rewriting 227 commits
would break every existing clone for no additional security benefit.

## Accepted vulnerability exceptions

Three HIGH findings from the container scan are suppressed in `.trivyignore`,
with the reasoning recorded there rather than silently filtered.

| CVE | Package | Why it is accepted |
|---|---|---|
| CVE-2026-23949 | `jaraco.context` 5.3.0 | Vendored inside setuptools |
| CVE-2026-24049 | `wheel` 0.45.1 | Vendored inside setuptools |
| CVE-2025-47273 | `setuptools` 70.3.0 | Vendored copy |

None are our own dependencies. Our direct pins scan clean: `wheel` 0.47.0 and
`setuptools` 79.0.1 both report zero findings. What Trivy flags is the older
copies setuptools bundles in its `_vendor/` directory.

Replacing them requires upgrading setuptools past 79.0.1, which is pinned for a
hard runtime reason: setuptools 80.x removes the `pkg_resources` shim, Python
3.12 removed `pkg_resources` from the standard library, and `fyers_apiv3`'s
WebSocket client imports it. Upgrading breaks the market data feed outright.

The exploit paths are archive extraction and package-index traversal. The
trading runtime performs neither and never imports those modules. This is
re-evaluated whenever `fyers_apiv3` drops its `pkg_resources` dependency.

Five further HIGH findings are suppressed for a different and harder reason:
`fyers_apiv3` declares its dependencies with `==` rather than `>=`.

| CVE | Package | Pinned by the SDK to |
|---|---|---|
| CVE-2024-30251, CVE-2025-69223, CVE-2026-69244 | `aiohttp` | `==3.9.3` |
| CVE-2025-4565, CVE-2026-0994 | `protobuf` | `==5.29.3` |

Requesting a patched version of either makes `requirements.txt` unsatisfiable
against **every published fyers release**. pip returns `ResolutionImpossible`
rather than a warning, so there is no version of the broker SDK that permits the
fixed versions. This is not a decision we can make differently; it is a
constraint the vendor imposes.

Reachability was assessed rather than assumed. The aiohttp advisories are
server-side, covering malformed POST parsing and a zip bomb through
`auto_decompress`; nothing here runs an aiohttp server, it is a client against
Fyers and Telegram. The protobuf advisories are unbounded recursion while parsing
untrusted messages, and the only protobuf this process decodes arrives over an
authenticated Fyers market data socket.

`msgpack` was genuinely fixable and was bumped rather than suppressed.

## Dev tooling is not shipped to production

The runtime image contains the trading dependencies and nothing else. This began
as a scan finding and turned out to be worth fixing on its own terms.

An earlier revision installed `requirements-dev.txt` into the same virtualenv
the runtime used, which put pytest, ruff, mypy, bandit and pre-commit into the
image that places orders. Rust-built dev wheels also ship
[PEP 770](https://peps.python.org/pep-0770/) SBOMs at `.dist-info/sboms/`
declaring their vendored crates, which scanners read, so the dev toolchain
brought its own reporting surface with it.

`deploy/Dockerfile` now builds two virtualenvs from a shared base. The default
target is `runtime`; tests build `--target test`.

The runtime stage also deletes pip. pip vendors its own dependencies under
`pip/_vendor/` — including `msgpack` and `pkg_resources` — and scanners report
those bundled copies by version. The `python:3.12-slim` base ships pip 25.0.1,
whose vendored msgpack is 1.1.2, and no pin on our side reaches it: our own
msgpack is 1.2.1 and scans clean. The same vendored tree was the source of the
earlier `setuptools 70.3.0` finding. Removing pip resolves both, and a container
that places real orders has no business being able to install packages.

`setuptools` is deliberately kept: 79.0.1 supplies the `pkg_resources` shim that
`fyers_apiv3`'s WebSocket client imports.

## Reporting a vulnerability

Open a GitHub issue for anything non-sensitive. For anything involving a
credential or an exploitable defect, email the address on the repository owner's
GitHub profile rather than filing publicly.
