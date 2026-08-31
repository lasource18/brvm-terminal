# Kodji Terminal — productization plan

**30 Aug 2026.** Turning a single-user BRVM terminal into a multi-tenant PWA with a
free tier, a paid tier, and a downloadable TUI — without outgrowing one 4 GB VPS.

`brvm-terminal` → `kodji-terminal` · 754 tests · 16 migrations · 0 users

> A rendered version of this document is published at
> https://claude.ai/code/artifact/fbe26916-86b3-4e1f-afbc-ed1475917214
> This file is canonical; update it first.

## What's already decided

| Question | Decision | Consequence |
| --- | --- | --- |
| Hosting | **Hetzner CX22 origin, Cloudflare free tier in front** | Cloudflare Workers would be a rewrite, not a deploy target — APScheduler needs a long-lived process, `selectolax` is a C extension, SQLite-the-file becomes D1-the-API. |
| Data source | **Scrape-only; commercial BRVM feed not used** | Delete the unused `BRVM_API_*` path rather than leave dead credentials in `env.example`. |
| What's paid | **Only your own transformations** | Raw facts stay free; charts, ratios, brief, alerts, analyst view are the product. |
| Payments | **Flutterwave primary**, mobile money + cards, XOF | Stripe is likely unavailable to a CI entity — see P0. |
| TUI | **Ships to paying users** as a synced local replica | Cheap, because every service already resolves the DB through `settings.db_path`. |

## P0 · Two things to settle before writing any code

### Stripe almost certainly won't onboard a Côte d'Ivoire entity

**BLOCKER.** Stripe operates in a fixed list of supported countries, and Côte d'Ivoire
is not on it. If you incorporate in Abidjan you cannot open a Stripe account against
that entity — so "integrate Stripe as well for overseas users" may not be available at
all. **Verify against Stripe's current country list before you incorporate**: this is
very cheap to design around now and very expensive later.

| Route | Covers | Cost of adopting |
| --- | --- | --- |
| **Flutterwave only** *(recommended at launch)* | CI-native, settles XOF, takes Orange Money / Wave / MTN MoMo *and* international Visa/Mastercard — so it serves overseas users too. | One integration, no second provider to reconcile. |
| **Merchant of Record** (Paddle, Lemon Squeezy) | They become seller of record, handle global VAT/sales tax, pay you out — works with a CI entity where Stripe doesn't. | Higher take rate. Add later *only* if overseas card volume justifies it. |
| **Incorporate elsewhere** | Unlocks Stripe directly. | Changes your tax and regulatory position. Don't let a payment processor pick your jurisdiction. |

Whichever you pick, keep the subscription layer provider-agnostic: one `subscriptions`
table with `provider`, `provider_ref` and a normalized status, and never let a webhook's
payload shape leak into the domain. A second provider is then an adapter, not a refactor.

### XOF is a zero-decimal currency

There are no centimes in the CFA franc. Payment APIs treat XOF as zero-decimal, so
`5000 XOF` is sent as `5000`, not `500000`. Store prices as **INTEGER francs**.

Don't reuse the `usd_micros` convention from `briefs` / `analyst_notes` for pricing —
that scale exists for fractions of a cent of LLM spend. Peg is fixed at
1 EUR = 655.957 XOF, so 5 000 XOF/month ≈ €7.62.

### One note on data rights

Gating only your own transformations is the right line and materially lowers exposure
versus reselling a licensed feed. Residual: the *free* tier still republishes scraped
quotes and news, so sikafinance's and brvm.org's terms still apply to that surface.
Worth reading their terms once and settling an attribution/cache policy. Not a blocker.

## P1 · The rename, done alone and first

Two completely different kinds of "brvm" string; only one may change.

| Kind of reference | Sites | Action |
| --- | ---: | --- |
| `from brvm…` / `import brvm` | 785 | **Rename → kodji** |
| `BRVMC`, `BRVM Composite`, `brvm.org`, `sources/brvm_org.py` | 1 122 | **Leave untouched** |

**Do not run a blanket `sed s/brvm/kodji/g`.** It would rewrite the exchange's own name,
the index ticker `BRVMC`, the live scraper URLs, and `sources/brvm_org.py` — the module
that exists precisely to scrape brvm.org. Rename by *identity*, not by string. BRVM
stays as the name of the market you cover.

Checklist:

- `src/brvm/` → `src/kodji/`, and all 785 import sites with it
- `pyproject.toml`: `name`, and `[tool.hatch.build.targets.wheel] packages`
- `settings.db_path`: `./data/brvm.sqlite` → `./data/kodji.sqlite`, plus a one-time file
  move. `_schema_migrations` is unaffected, so no migration needed.
- `deploy/brvm-terminal.service` → `deploy/kodji-terminal.service`; update the Caddyfile
  example and `docker-compose.yml`
- Delete `BRVM_API_BASE` / `BRVM_API_KEY` from `env.example` and `config.py`, and retire
  the `ApiProvider` / `FallbackProvider` path. PR-K's F-13 fallback exists only to
  survive a half-configured feed; keeping dead credentials invites exactly the
  misconfiguration F-13 was about. Update `tests/test_config.py` and
  `tests/test_provider_selection.py`.
- `CLAUDE.md`: demote the commercial API from source priority #1 to "not used"
- Rename the GitHub repo (keeps redirects), then `git remote set-url`

Ship as one mechanical PR, no behaviour change, green suite. First — rebasing a 785-site
rename across in-flight feature branches is painful, and every phase below adds branches.

**Check before committing to the name:** `.ci` and `.com` domains, plus a trademark
search at **OAPI** (single filing covers all 17 member states incl. Côte d'Ivoire).
You're renaming to avoid a naming-rights problem; don't land in a second one.

## P2 · Users and ownership

No user concept exists today. And a live landmine in the first feature you'd expose to
a second person:

```sql
-- migrations/0002_watchlists.sql
slug TEXT NOT NULL UNIQUE   -- global namespace: two users both
                            -- creating "Banks" collide on day one
```

New migrations:

- `users` — id, email, created_utc, locale, tz (FR/EN + Abidjan/Montreal already exist
  as settings to hang off this)
- `sessions` — token hash, user_id, expires_utc
- `subscriptions` — user_id, plan, provider, provider_ref, status, current_period_end_utc
- `push_subscriptions` — user_id, endpoint, keys (for P5)
- `owner_id` on `watchlists` and `alert_rules`; drop the global slug constraint for
  `UNIQUE(owner_id, slug)`
- Backfill: existing rows become user #1

Then scope every query in `store/watchlists.py` and `store/alerts.py`. This is the bulk
of the work — mechanical, but where a single missed `WHERE owner_id` becomes one
customer reading another's data.

**Technique:** make repo functions *require* an owner argument rather than defaulting it.
A required positional turns "did I miss a call site?" into a type-checker and
test-collection error, and you have 754 tests to surface them.

**Stays shared — do NOT add `owner_id`:** `securities`, `quotes`, `daily_bars`, `news`,
`filings`, `financials`, `briefs`, `analyst_notes`. That decision is the entire economic
story — see P3.

## P3 · Why this stays cheap

Both expensive features are already keyed as shared artifacts:

```sql
-- migrations/0010_briefs.sql
day        TEXT PRIMARY KEY          -- one brief per day, for everyone

-- migrations/0011_notes.sql
PRIMARY KEY (ticker, week_start)     -- one note per ticker per week
```

- **Anthropic spend is flat in user count.** One brief a day whether you have 1 user or
  10 000; you gate *reading*, not generating. `llm_daily_cap_cents` keeps working
  unchanged, and gross margin improves with every signup.
- **Scrape volume is flat too.** The politeness posture (10–15 min in-hours, custom UA,
  backoff) survives a userbase — users read a shared cache rather than triggering fetches.
- **Per-user writes are tiny.** A session row, a few watchlist items, some alert rules.

**Therefore: stay on SQLite.** WAL with a single writer is comfortable into the low
thousands of users at this write profile. Migrating to Postgres now costs weeks, buys
nothing.

Caveat: don't write to `sessions` on every request. A `last_seen` touch turns every page
view into a write and is the one change most likely to make you regret SQLite. Update it
lazily, or not at all.

## P4 · Gating

Raw facts free, your work paid.

| Free | Paid |
| --- | --- |
| Quotes and index levels | Chart (history + plotting) |
| News feed (untagged listing) | Daily brief |
| Security directory and description | Analyst view |
| Bond reference — Overview tab | Ratios and Peers |
| | Yield & Duration, Cash flow |
| | Alerts |

Insertion point already exists: `TabSpec` in `apps/web/tabs.py` carries
`hidden_for_kinds`. Add `min_plan: str = "free"` beside it and widen `visible_for(kind)`
to `visible_for(kind, plan)`. One registry, one choke point; the TUI mirror stays in sync
the same way it does for kinds today.

**Hiding a tab is not access control.** Removing a tab leaves `/s/SNTS/yield` reachable
by URL, and so do the HTMX fragments and the JSON API. Enforce with a FastAPI dependency
across all three route families: `routes/pages.py`, `routes/fragments.py` (partials are
the easy ones to forget), `routes/api.py`.

Write one parametrized test walking every `TabSpec` with `min_plan != "free"`, asserting
anonymous and free-plan requests both get 402/redirect on each route family. That test is
what stops a gating regression the day you add a tab and forget the dependency.

## P5 · PWA and push

- Manifest, icon set, service worker for the installable shell. Server-rendered HTMX, so
  cache the *shell* and let fragments come from the network.
- **Web Push replaces the Discord webhook** for user-facing alerts. Keep Discord as your
  ops channel — you want it for "the 15:45 brief job didn't run".
- iOS delivers Web Push only once the user adds to home screen (16.4+). Plan the install
  prompt around that; keep email as fallback so a non-installing iOS user still gets alerts.
- Alert fan-out changes shape: `alert_events` carries one delivery row per rule today.
  Per-user rules make delivery per-subscription — re-check the batch cap PR-K's F-16 fix
  protects, since the volume assumption behind it changes.

## P6 · The TUI as a paid download

Cheaper than it looks, because of a detail of your own architecture:

```python
# services/market.py:17
def _db_path() -> Path:
    return Path(settings.db_path)
```

Every service reaches the DB through one settings value, so the TUI does **not** need
rewriting into an API client — point it at a synced local replica and it runs unchanged.

1. New `kodji sync` authenticates with a subscription token and pulls a server-built
   SQLite snapshot, filtered to the user's plan.
2. TUI runs against that file, no changes to `services/` or `store/`.
3. Package with `uv tool install`, or PyInstaller for a no-Python binary.

Consequences to accept:

- **No revocation after download.** The subscriber has the data locally. Use short-lived
  sync tokens so a lapsed subscription stops *updating*, not stops *working* — also the
  more honest thing to sell.
- **First sync is large** with 25y of OHLCV. Trim the default to a recent window; make
  full history an explicit flag.
- **Write features need a decision.** Watchlists and notes go read-only, or push back
  through the API. Read-only is the much smaller v1.

**Verdict: do it, after launch.** A real terminal client is a genuine differentiator and
the architecture supports it, but it isn't on the critical path to first revenue.

## P7 · Deploy and ops

- **Litestream** replicating the SQLite file to Cloudflare R2 or Backblaze B2. Stops
  being optional the moment someone pays.
- **Caddy** for TLS at the origin, **Cloudflare Tunnel** so the VPS needs no inbound
  ports open.
- Secrets off the box's `.env` into a systemd `EnvironmentFile` at 0600, or Docker secrets.
- **Keep the CX22.** ~500 MB RSS is dominated by scrapers and jobs (fixed cost). Sessions
  and auth barely move it, and P3 explains why user growth doesn't either.
- Extend the health endpoint with an external uptime check, and alert yourself when a
  scheduled job silently doesn't run — the 15:45 brief and 16:00 BOC reconcile matter most.

## Sequence

Continuing the PR-letter convention from where PR-U left off.

| | Ships | Note |
| --- | --- | --- |
| **P0** | Verify, don't build | Stripe country list · Flutterwave CI onboarding · kodji domain + OAPI trademark. All three can change what you build. |
| **PR-V** | Rename to kodji | 785 import sites, zero behaviour change, green suite. Alone and first. |
| **PR-W** | Users, sessions, ownership | Migrations 0017+, owner scoping, magic-link auth. The big one. |
| **PR-X** | Plan gating | `TabSpec.min_plan` + route-level enforcement on pages/fragments/API, with the leak test. |
| **PR-Y** | Flutterwave billing | Provider-agnostic subscriptions, webhook adapter, XOF integer pricing. |
| **PR-Z** | PWA shell and Web Push | Manifest, service worker, push subscriptions; Discord demoted to ops-only. |
| **PR-AA** | Ops hardening | Litestream, Cloudflare Tunnel, job-missed alerting. Before you take money. |
| **PR-AB** | TUI sync client | Post-launch. `kodji sync` plus packaging. |

## Open questions

- **Price point in XOF?** Anchor against what a Sikafinance or Richbourse subscription
  costs locally, rather than converting a dollar price that won't land in the market
  you're selling to.
- **What limits the free tier?** Delayed quotes, a watchlist cap, or purely which tabs
  are visible. Tab-only is simplest to build and easiest to explain.
- **Teams, ever?** If organizations are even plausible, make the ownership column
  `account_id` rather than `user_id` in PR-W, with a personal account auto-created per
  user. Costs nothing now; retrofitting after you have paying customers is a migration
  across every scoped table.
