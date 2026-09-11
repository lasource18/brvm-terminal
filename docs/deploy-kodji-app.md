# Deploying kodji.app

The production runbook. One 4 GB VPS (Vultr, 2 vCPU, Ubuntu 24.04), one
process, one SQLite file, Cloudflare in front. Written 2026-09-09 against
`main`; follow it top to bottom the first time. Every step ends with a check —
do not move on until the check passes.

Litestream replication to R2 landed 10 Sep 2026 (§8), and the job-missed
watchdog the same day (§8, *Watching it*). Cloudflare Tunnel is dropped for
good: ufw + the origin cert already close the origin off.

## Shape

```
browser ──TLS──▶ Cloudflare  (DNS · WAF · per-IP rate limit · DDoS · hides the origin IP)
                     │  TLS with a Cloudflare Origin CA cert; :443 open ONLY to Cloudflare ranges
                     ▼
              Caddy on the VPS
                     │  http://127.0.0.1:8765
                     ▼
     uvicorn kodji.apps.web.main:app     (systemd · user kodji · MemoryMax=384M · APScheduler inside)
                     │
                     ▼
     /opt/kodji-terminal/data/kodji.sqlite   (+ data/filings/ if you copy the corpus)
```

Why these choices, briefly:

- **Origin CA certificate, not Let's Encrypt.** No port 80, no renewal for
  15 years, and it pairs with Cloudflare's *Full (strict)* mode. Only
  Cloudflare trusts it — which is the point, since only Cloudflare can reach
  :443.
- **Cloudflare now, not later.** The per-IP rate limit on `/login` lives
  there (the app deliberately does not do per-IP behind a proxy), and moving
  DNS is cheapest before there are users.
- **Bare systemd, not Docker.** One process on a 4 GB box; the compose file
  in the repo is a skeleton, not the deploy path.

## 0. Before you start

Have all of these open before touching anything:

| | |
| --- | --- |
| VPS | Vultr, 4 GB plan, Ubuntu 24.04, your SSH key added **at creation**, its IPv4 (and IPv6) noted — see the Vultr notes below |
| Cloudflare | An account (free plan) |
| Namecheap | Logged in — you will change nameservers there |
| Resend | An API key with **Sending access** scoped to `mail.kodji.app`, copied at creation time |
| Anthropic | `ANTHROPIC_API_KEY` |
| Discord | Webhook URL, optional |
| Your Mac | The repo at `main`, and `data/kodji.sqlite` (11 MB — the months of quotes, news, fundamentals you want on the box) |

Time: about two hours the first time, most of it waiting on DNS.

### Vultr notes

- **Plan:** 4 GB RAM is the floor — `MemoryMax=384M` for the app plus
  scrapers, Caddy and the OS assume it. 2 vCPU is plenty. 40 GB+ disk only
  matters if you copy the filings corpus.
- **Region:** Paris or Amsterdam. Users in Abidjan and Dakar reach Europe in
  ~100 ms; Johannesburg is farther from West Africa than Paris is. Cloudflare
  serves the static assets from its edge either way.
- **SSH:** add your key on the create form and, after first login, set
  `PasswordAuthentication no` in `/etc/ssh/sshd_config.d/00-kodji.conf` and
  `systemctl reload ssh`. Vultr images allow root password login by default.
  The `00-` prefix matters: sshd takes the *first* value it reads, and the
  image ships `50-cloud-init.conf` saying `yes`.
- **Two logins.** Vultr installs your key for `root` only. After §2c, copy it
  to the app user too (`install -d -o kodji -g kodji -m 700 /home/kodji/.ssh
  && install -o kodji -g kodji -m 600 /root/.ssh/authorized_keys
  /home/kodji/.ssh/authorized_keys`) and add both to `~/.ssh/config` on the
  Mac: `kodji-vps` (User `kodji` — git pull, uv, just) and `kodji-root`
  (User `root` — systemd, caddy, litestream, ufw). `kodji` deliberately has
  no sudo: the app runs as that user, and a compromised app must not be a
  root shell.
- **Hostname:** if you `hostnamectl set-hostname`, add `127.0.1.1 <name>` to
  `/etc/hosts` too, or `sudo` complains on every call.
- **Swap:** the Vultr image already has a swap file; skip §2b.
- **Firewall:** Vultr's network firewall (Products → Firewall) can sit in
  front of `ufw`. If you use it, one rule for SSH from your IP and one for
  TCP 443 with **Cloudflare** as the source — Vultr maintains that IP list
  for you, which removes the "re-run the ufw loop when ranges change" chore.
  Keep `ufw` anyway; belt and braces.
- **Backups:** skip the paid automatic-backup add-on. What it protects is
  rebuildable from this runbook in an hour, and the data is replicated
  off-box continuously by Litestream (§8). Do take **one manual snapshot**
  right after §7 passes — billed per GB stored, cents a month for this box
  — so a restore is a click instead of a runbook.

## 1. Snapshot what exists

### 1a. DNS inventory

Run this **on your Mac** and save the output. It is the reference you compare
Cloudflare's import against in §5.

```bash
NS=pdns1.registrar-servers.com
dig +short MX    kodji.app @$NS
dig +short TXT   kodji.app @$NS
dig +short TXT   default._domainkey.kodji.app @$NS
dig +short CNAME autoconfig.kodji.app @$NS
dig +short CNAME autodiscover.kodji.app @$NS
dig +short SRV   _autodiscover._tcp.kodji.app @$NS
dig +short CNAME send.mail.kodji.app @$NS
dig +short TXT   resend._domainkey.mail.kodji.app @$NS
dig +short TXT   _dmarc.kodji.app @$NS
```

Also screenshot Namecheap → Domain List → `kodji.app` → Manage → **Advanced
DNS**, the whole table. `dig` can't show you records you don't know to ask
for; the screenshot can.

### 1b. Database snapshot

On the Mac, a consistent copy (never `cp` a live SQLite file — the WAL may
hold writes the main file doesn't yet):

```bash
cd ~/brvm-terminal
sqlite3 data/kodji.sqlite ".backup /tmp/kodji-seed.sqlite"
ls -lh /tmp/kodji-seed.sqlite      # ~11 MB
```

## 2. Prepare the VPS

SSH in as root (or your sudo user) for this section.

### 2a. Base system

```bash
apt update && apt upgrade -y
apt install -y git curl sqlite3 ufw unattended-upgrades ca-certificates
dpkg-reconfigure -plow unattended-upgrades     # answer Yes
timedatectl set-timezone UTC                   # the app stores UTC; jobs use Africa/Abidjan internally
```

### 2b. Swap (2 GB)

The box has 4 GB. Scrapers and the fundamentals extractor are fine, but a
swap file turns a memory spike into slowness instead of an OOM kill:

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl vm.swappiness=10 && echo 'vm.swappiness=10' >> /etc/sysctl.d/99-kodji.conf
```

### 2c. The `kodji` user

The service runs as an unprivileged user that owns `/opt/kodji-terminal`:

```bash
adduser --disabled-password --gecos "" kodji
mkdir -p /opt/kodji-terminal && chown kodji:kodji /opt/kodji-terminal
```

### 2d. Firewall — SSH, and :443 only from Cloudflare

This is what makes the origin certificate safe and keeps anyone from
reaching the box around Cloudflare's rate limit:

```bash
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
for ip in $(curl -s https://www.cloudflare.com/ips-v4) $(curl -s https://www.cloudflare.com/ips-v6); do
  ufw allow proto tcp from "$ip" to any port 443 comment cloudflare
done
ufw --force enable
ufw status | head -20
```

Port 80 stays closed — nothing needs it. Cloudflare's ranges change rarely;
if the site ever returns a Cloudflare **522** for no other reason, re-run the
loop.

**Check:** `ufw status` shows `22/tcp ALLOW Anywhere` and a block of
`443/tcp ALLOW <cloudflare range>` lines. Nothing else.

### 2e. Tooling for the `kodji` user

```bash
sudo -iu kodji
curl -LsSf https://astral.sh/uv/install.sh | sh
curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to ~/.local/bin
source ~/.bashrc
uv --version && just --version
exit
```

## 3. Install the app

Everything in this section runs as `kodji`: `sudo -iu kodji`.

### 3a. Code and dependencies

```bash
cd /opt/kodji-terminal
git clone https://github.com/lasource18/brvm-terminal.git .
uv sync --no-dev                    # runtime deps only; no pytest/ruff on the box
ls .venv/bin/uvicorn                # the path the systemd unit uses
```

### 3b. Seed the database

From the **Mac**, push the snapshot from §1b:

```bash
ssh kodji@<VPS-IP> 'mkdir -p /opt/kodji-terminal/data/filings'   # data/ is git-ignored, not in the clone
scp /tmp/kodji-seed.sqlite kodji@<VPS-IP>:/opt/kodji-terminal/data/kodji.sqlite
```

Optional — the 4.5 GB filings
corpus. Not needed for the app to work; extracted numbers are already in the
SQLite. It feeds future OCR/extraction runs. Resumable, so start it and
forget it:

```bash
rsync -avz --partial --progress ~/brvm-terminal/data/filings/ kodji@<VPS-IP>:/opt/kodji-terminal/data/filings/
```

The corpus must land at exactly `data/filings/` under the project: the DB
stores project-relative paths (`services/filings._relativize`).

### 3c. Production `.env`

Back on the VPS as `kodji`:

```bash
cd /opt/kodji-terminal
cp env.example .env && chmod 600 .env
nano .env
```

Change these; leave the rest at their defaults (full annotated template in
Appendix A):

```bash
APP_ENV=prod                                   # turns on the Secure cookie flag
DB_PATH=/opt/kodji-terminal/data/kodji.sqlite  # absolute, so cwd never matters
FILINGS_ROOT=/opt/kodji-terminal/data/filings
ANTHROPIC_API_KEY=sk-ant-...
RESEND_API_KEY=re_...                          # sending-only, scoped to mail.kodji.app
EMAIL_FROM=Kodji <connexion@mail.kodji.app>
EMAIL_REPLY_TO=support@kodji.app
PUBLIC_BASE_URL=https://kodji.app              # REQUIRED here; blank only on a laptop
AUTH_REQUIRED=true                             # see 3e first
DISCORD_WEBHOOK_URL=                           # optional
HTTP_USER_AGENT=kodji-terminal/0.1 (+contact: support@kodji.app)
```

Rules for values: no quotes needed; if you quote, straight ASCII `"` on both
ends; no `#` inside a value (systemd's `EnvironmentFile` and python-dotenv
disagree on it).

### 3d. Migrate

```bash
uv run --no-dev python scripts/migrate.py --check      # lists what is pending
uv run --no-dev python scripts/migrate.py              # applies it
uv run --no-dev python scripts/migrate.py --check      # "[migrate] up to date"
```

`--no-dev` on every `uv run` on the box: without it uv quietly syncs the
dev group (pytest, ruff, mypy — 30 MB) into the production venv.

The seed came from a machine already at the current schema, so expect
"up to date" straight away. The app refuses to start if this ever says
otherwise — that is the startup guard from #79, working as intended.

### 3e. Claim the operator account — do not skip

Migration 0017 seeded **account 1** with everything the database held
before multi-tenancy (your watchlists, alert rules, notes) and 0019 put it
on the paid plan. But no user row points at it. With `AUTH_REQUIRED=true`,
your first sign-in would create a fresh *free* account and none of your
data would be in it.

```bash
uv run --no-dev python scripts/claim_owner.py you@example.com
# [claim-owner] you@example.com (user 1) now owns account 1 [paid]
```

(`just claim-owner` is the same thing, minus `--no-dev`.)

Use the exact address you will sign in with. Idempotent; run it again if
unsure.

### 3f. First run, in the foreground

```bash
uv run --no-dev uvicorn kodji.apps.web.main:app --host 127.0.0.1 --port 8765
```

You want to see, in order: no `PendingMigrations`, then
`scheduler started: ['snapshot_market_hours', ...]` (20 job ids). In a
second SSH session:

```bash
curl -s 127.0.0.1:8765/health
# {"status":"ok","version":"0.1.0","utc":"...","market_open":false}
```

`Ctrl-C` the foreground run. `exit` back to root.

## 4. Run it under systemd

As root:

```bash
cp /opt/kodji-terminal/deploy/kodji-terminal.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kodji-terminal
sleep 3
systemctl status kodji-terminal --no-pager | head -12
journalctl -u kodji-terminal -n 30 --no-pager
curl -s 127.0.0.1:8765/health
```

**Check:** `active (running)`, the `scheduler started` line in the journal,
and the health JSON. If the unit is `failed` with `PendingMigrations` in the
journal, go back to §3d; the unit gives up after three tries in two minutes
instead of looping.

The app is now running but unreachable from outside — ufw only admits
Cloudflare, and Cloudflare doesn't know about it yet.

## 5. Put Cloudflare in front

### 5a. Add the site

Cloudflare dashboard → **Add a site** → `kodji.app` → **Free** plan.
Cloudflare scans and imports the records it can find. **Compare against
§1a line by line** — it misses some (typically `_dmarc`, the Resend DKIM,
`send.mail`). Add whatever is missing. The complete target list is
Appendix B; the two rules that matter:

- **Every mail-related record is DNS only (grey cloud).** MX, SPF/DKIM/
  DMARC TXT, `send.mail`, autoconfig/autodiscover. A proxied CNAME stops
  being a CNAME, and Resend verification fails the same day.
- **`A kodji.app → <VPS IPv4>` and `CNAME www → kodji.app` are Proxied
  (orange cloud).** That is what hides the origin and applies the WAF.

Add those two records now if the scan didn't (there was no A record
before). AAAA with the IPv6, proxied, is optional and nice.

### 5b. Switch the nameservers

Cloudflare shows two nameservers (`*.ns.cloudflare.com`). Namecheap →
Domain List → `kodji.app` → **Manage** → **Nameservers** → **Custom DNS**
→ paste both → save.

Propagation is minutes to a few hours. Check from the Mac:

```bash
dig +short NS kodji.app @1.1.1.1          # the two cloudflare names
dig +short A  kodji.app @1.1.1.1          # Cloudflare IPs (104.x / 172.x), NOT the VPS IP
dig +short MX kodji.app @1.1.1.1          # mx1/mx2.privateemail.com — mail still yours
dig +short TXT kodji.app @1.1.1.1         # v=spf1 include:spf.privateemail.com ~all
dig +short CNAME send.mail.kodji.app @1.1.1.1   # send.forge.rmta.net
```

Cloudflare emails you when the zone is active. Namecheap's Advanced DNS tab
is inert from here on — all edits happen in Cloudflare.

### 5c. Origin certificate

**SSL/TLS → Origin Server → Create Certificate.** Private key type RSA
(2048), hostnames `kodji.app` and `*.kodji.app`, validity 15 years. It
shows the certificate and the private key **once** — save both before
closing the dialog; there is no way to retrieve the key afterwards, only to
create a new certificate. On the VPS as root:

```bash
mkdir -p /etc/caddy/certs
nano /etc/caddy/certs/kodji.app.pem     # paste "Origin Certificate"
nano /etc/caddy/certs/kodji.app.key     # paste "Private Key"
chmod 640 /etc/caddy/certs/* && chown root:caddy /etc/caddy/certs/*   # caddy user exists after §6
```

(Do §6 first if the `caddy` group doesn't exist yet, then come back for
the `chown`.)

### 5d. Settings to flip

| Where | Setting | Value | Why |
| --- | --- | --- | --- |
| SSL/TLS → Overview | Encryption mode | **Full (strict)** | Validates the origin cert; anything less is TLS to Cloudflare and plaintext to you. |
| SSL/TLS → Edge Certificates | Always Use HTTPS | On | |
| SSL/TLS → Edge Certificates | Minimum TLS Version | 1.2 | |
| SSL/TLS → Edge Certificates | HSTS | **Off** for now | Irreversible for months once on. Turn on after a week of stable HTTPS. |
| Speed → Optimization → Content | Rocket Loader | **Off** | It rewrites inline `<script>` and breaks HTMX. |
| Scrape Shield | Email Address Obfuscation | **Off** | It injects JS into any page showing an address (`support@kodji.app`). |
| Security → WAF → Rate limiting rules | new rule `login-form` | see below | The per-IP layer the app deliberately leaves to the edge. |

The rate-limiting rule. On the free plan the counting period and the block
are both fixed at 10 seconds; the threshold is yours:

- *If incoming requests match:* `URI Path` equals `/login` **AND**
  `Request Method` equals `POST`
- *With the same:* IP
- *When rate exceeds:* **3** requests per **10 seconds**
- *Then:* Block, for 10 seconds

It throttles a burst; the app's global send budget
(`LOGIN_MAX_SENDS_PER_HOUR` / `_PER_DAY`) is what bounds the damage.

## 6. Caddy with the origin certificate

As root:

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy

cp /opt/kodji-terminal/deploy/Caddyfile.example /etc/caddy/Caddyfile
chown root:caddy /etc/caddy/certs/*          # now that the group exists
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
systemctl status caddy --no-pager | head -5
```

The Caddyfile is short and worth reading once: `kodji.app` terminates TLS
with the origin cert and proxies to `127.0.0.1:8765`; `www.kodji.app`
redirects to the apex. Caddy sets `X-Forwarded-For/Proto`, and uvicorn
trusts them from `127.0.0.1` by default.

**Check:** `journalctl -u caddy -n 20` has no `tls` errors, and from the
VPS itself:

```bash
curl -sk --resolve kodji.app:443:127.0.0.1 https://kodji.app/health
```

returns the health JSON (`-k` because the origin cert isn't in your local
trust store — that's expected).

## 7. Smoke test

From the Mac, in order. Each line says what you should see.

```bash
curl -sI https://kodji.app | head -8
#   HTTP/2 200, server: cloudflare, a cf-ray header
curl -s https://kodji.app/health
#   {"status":"ok",...}
curl -sI http://kodji.app | head -3
#   301 → https://kodji.app/  (Always Use HTTPS). A *timeout* here means that
#   setting is still off: Cloudflare then tries the origin on :80, which ufw
#   drops.
curl -sI https://www.kodji.app | head -3
#   308 → https://kodji.app/  (Caddy)
curl -m 5 -sk https://<VPS-IP>/health || echo "blocked, as intended"
#   times out: ufw admits only Cloudflare
```

Then in a browser:

1. `https://kodji.app` → the overview renders **without** signing in. That
   is by design: public market data is the free tier, gating doesn't put a
   login wall in front of it. `https://kodji.app/watchlists` is the one
   that must bounce you to `/login` — that's `AUTH_REQUIRED` working.
2. On `/login`, enter the address you claimed in §3e → "Check your email"
   page, showing the 20-minute expiry.
3. The mail: from `Kodji <connexion@mail.kodji.app>`, Reply-To
   `support@kodji.app`, link starting `https://kodji.app/login/t/`.
   **In the inbox, not spam** — if spam, see Appendix C.
4. Click the link → confirm button → you land on the overview, **and your
   watchlists are there.** If they aren't, the claim didn't take: `just
   claim-owner` again on the VPS, sign out, sign in.
5. Sign out → back at `/login`, no "cross-origin" error.
6. Open a paid tab (Chart, Brief, Alerts) — visible, because account 1 is
   paid.

On the VPS:

```bash
journalctl -u kodji-terminal -n 50 --no-pager | grep -E "login|session|scheduler"
#   "challenge sent to ...", "session minted for ... (account=1)"
sudo -iu kodji bash -c 'cd /opt/kodji-terminal && uv run --no-dev python scripts/migrate.py --check'
#   [migrate] up to date
```

If it's a weekday inside 09:00–15:30 Abidjan, wait for the next
`snapshot_market_hours` tick and confirm the overview's "last updated"
badge moves. Otherwise trigger one snapshot by hand as `kodji`:
`just snapshot`.

Finally, Resend → Domains → `mail.kodji.app` → **Verified**, and the
sign-in mail listed under Emails as Delivered.

## 8. Day 2

### Updating the app

```bash
sudo -iu kodji
cd /opt/kodji-terminal
git pull --ff-only
uv sync --no-dev
if ! uv run --no-dev python scripts/migrate.py --check; then
  sqlite3 data/kodji.sqlite ".backup data/pre-migrate-$(date +%F-%H%M).sqlite"
  uv run --no-dev python scripts/migrate.py
fi
exit
sudo systemctl restart kodji-terminal && journalctl -u kodji-terminal -n 20 --no-pager
```

The pre-migrate backup is the rollback: migrations are forward-only.

### Rollback

```bash
sudo systemctl stop kodji-terminal
sudo -iu kodji bash -c 'cd /opt/kodji-terminal && git checkout <previous-sha> && uv sync --no-dev'
# only if a migration was applied and you're going back across it:
sudo -iu kodji cp /opt/kodji-terminal/data/pre-migrate-<stamp>.sqlite /opt/kodji-terminal/data/kodji.sqlite
sudo systemctl start kodji-terminal
```

### Backups — Litestream to R2 (continuous) + a nightly local copy

**Litestream** ships every WAL change to a Cloudflare R2 bucket within
10 seconds and keeps 3 days of point-in-time history. Installed 10 Sep
2026; this is the backup that counts. Setup on a fresh box, as root:

```bash
V=0.5.17
curl -fsSLO https://github.com/benbjohnson/litestream/releases/download/v$V/litestream-$V-linux-x86_64.deb
curl -fsSLO https://github.com/benbjohnson/litestream/releases/download/v$V/checksums.txt
grep "linux-x86_64.deb" checksums.txt | sha256sum -c - && dpkg -i litestream-$V-linux-x86_64.deb

cp /opt/kodji-terminal/deploy/litestream/litestream.yml.example /etc/litestream.yml
sed -i 's/<R2_ACCOUNT_ID>/<your account id>/' /etc/litestream.yml
printf 'LITESTREAM_ACCESS_KEY_ID=...\nLITESTREAM_SECRET_ACCESS_KEY=...\n' > /etc/litestream.env && chmod 600 /etc/litestream.env
install -d /etc/systemd/system/litestream.service.d
cp /opt/kodji-terminal/deploy/litestream/override.conf /etc/systemd/system/litestream.service.d/
systemctl daemon-reload && systemctl enable --now litestream
journalctl -u litestream -n 5      # "replicating to ... bucket=kodji-litestream", then "snapshot complete"
```

R2 side: bucket `kodji-litestream`, and an R2 API token with *Object Read &
Write* on that bucket only. The account id is in the R2 dashboard URL.

**Prove it restores** — do this once after setup and again whenever you
touch the config. Never trust a backup you haven't restored:

```bash
set -a; . /etc/litestream.env; set +a
sudo -u kodji --preserve-env=LITESTREAM_ACCESS_KEY_ID,LITESTREAM_SECRET_ACCESS_KEY \
  litestream restore -config /etc/litestream.yml -o /tmp/restore-test.sqlite /opt/kodji-terminal/data/kodji.sqlite
sqlite3 /tmp/restore-test.sqlite "PRAGMA integrity_check; SELECT count(*) FROM securities;"
rm /tmp/restore-test.sqlite
```

**Disaster restore** (box gone, new box built through §2–§4 but *before*
starting the service): install Litestream as above, then

```bash
systemctl stop kodji-terminal 2>/dev/null || true
sudo -u kodji --preserve-env=LITESTREAM_ACCESS_KEY_ID,LITESTREAM_SECRET_ACCESS_KEY \
  litestream restore -config /etc/litestream.yml -o /opt/kodji-terminal/data/kodji.sqlite /opt/kodji-terminal/data/kodji.sqlite
systemctl start litestream kodji-terminal
```

Add `-timestamp 2026-09-10T15:00:00Z` to restore to a point in time within
the 3-day window (a bad migration, a script that deleted the wrong rows).

**Nightly local copy** stays as belt-and-braces — a consistent `.backup`
on the box at 03:15 UTC, 14 days kept, in `/opt/kodji-terminal/backups/`.
It is what you reach for first when the mistake is small and recent; it
is not off-box, so it is not the disaster plan. As `kodji`, `crontab -l`
shows the line; recreate it with `crontab -e` if missing:

```
15 3 * * * sqlite3 /opt/kodji-terminal/data/kodji.sqlite ".backup /opt/kodji-terminal/backups/kodji-$(date +\%F).sqlite" && find /opt/kodji-terminal/backups -name "kodji-*.sqlite" -mtime +14 -delete
```

Litestream and the cron coexist: `.backup` is a reader, and Litestream
owns checkpointing — never run `PRAGMA wal_checkpoint(TRUNCATE)` by hand
while it is running.

### Watching it

```bash
journalctl -u kodji-terminal -f                          # live log
journalctl -u kodji-terminal --since today | grep -E "ERROR|WARN"
systemctl status kodji-terminal | grep Memory            # vs MemoryMax=384M
```

Lines worth a look: `global send cap hit` (someone spraying `/login`, or a
launch), `rejected cross-origin POST`, `scheduled ... failed`.

Point a free uptime monitor (UptimeRobot, Better Stack) at
`https://kodji.app/health` every 5 minutes. That covers "the box is down".

**"A job silently didn't run"** is the watchdog (PR-AB part 2, 10 Sep
2026). Every scheduled job records its runs in `job_runs`; the
`job_watchdog` job checks them against each job's cron every 15 minutes
and alerts on a **missed**, **failed** or **stuck** job — once when it
appears, once a day while it lasts, once when it clears. It needs one
of these in `.env` (both is fine):

```bash
OPS_ALERT_EMAIL=cmguinan@yahoo.fr     # sent through the Resend sign-in sender
DISCORD_WEBHOOK_URL=https://...       # same webhook the alerts use
```

then `systemctl restart kodji-terminal`. With neither, the alert is only
an `ERROR` line in the journal. Ship it in the same step as migration
`0020_job_runs` — the app refuses to start until that is applied.

The verdict is also on `/health` as `"jobs": {"status": "ok", ...}`, which
lets the uptime monitor be the second channel for free: add a second
UptimeRobot monitor of type *Keyword* on the same URL, keyword
`"status": "ok", "open"` (with the spaces — the endpoint deliberately
uses standard `json.dumps` spacing and a test pins it), alert when the
keyword is **absent**. It fires
on `degraded` (a problem is open), `stale` (the scheduler thread died
while uvicorn kept answering) and `unknown` (DB unreadable).

From the shell, as `kodji` in `/opt/kodji-terminal`:

```bash
just jobs-status     # every job: next due, last run, status, duration, note
just jobs-check      # what the watchdog would flag now; exit 1 if anything
```

Both are read-only and safe beside the running service. Typical
follow-up to a `brief_daily missed` alert: `just brief-run` by hand. The
watchdog only sees runs that went through the scheduler, so the problem
clears — with a recovery notice — at the next scheduled run that succeeds.

### After a week

- DMARC: change `_dmarc` from `p=none` to `p=quarantine` once the reports
  at `dmarc@kodji.app` show only PrivateEmail and Resend as sources.
- HSTS on, 6 months, once you're sure HTTPS is never coming off.
- Re-check `MemoryMax` against actual RSS; raise to 448M if the extractor
  gets killed.

## Appendix A — production `.env`

```bash
# --- app ---
APP_ENV=prod
LOG_LEVEL=INFO
DB_PATH=/opt/kodji-terminal/data/kodji.sqlite

# --- data provider (withdrawn; scrapers are the source) ---
BRVM_API_BASE=
BRVM_API_KEY=

# --- LLM ---
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
LLM_DAILY_CAP_CENTS=100
LLM_EXTRACT_DAILY_CAP_CENTS=200
BRIEF_MODEL=claude-haiku-4-5-20251001
NOTES_MODEL=claude-sonnet-4-6

# --- filings / OCR ---
FILINGS_ROOT=/opt/kodji-terminal/data/filings
# OCR needs `apt install ocrmypdf tesseract-ocr-fra` and ~1 GB RAM per file.
# Leave the binary name as-is; without it installed the OCR job no-ops.
OCR_BINARY=ocrmypdf

# --- alerts + ops (the job watchdog uses either; set at least one) ---
DISCORD_WEBHOOK_URL=
OPS_ALERT_EMAIL=cmguinan@yahoo.fr
OPS_ALERT_REPEAT_HOURS=24

# --- auth + email ---
RESEND_API_KEY=re_...
EMAIL_FROM=Kodji <connexion@mail.kodji.app>
EMAIL_REPLY_TO=support@kodji.app
PUBLIC_BASE_URL=https://kodji.app
LOGIN_TOKEN_TTL_MINUTES=20
LOGIN_CODE_MAX_ATTEMPTS=5
LOGIN_MAX_PER_HOUR=5
LOGIN_MAX_SENDS_PER_HOUR=30
LOGIN_MAX_SENDS_PER_DAY=80
SESSION_TTL_DAYS=30
AUTH_REQUIRED=true

# --- scraper etiquette ---
HTTP_USER_AGENT=kodji-terminal/0.1 (+contact: support@kodji.app)
HTTP_TIMEOUT_S=15
```

## Appendix B — DNS records in Cloudflare

| Type | Name | Content | Proxy |
| --- | --- | --- | --- |
| A | `kodji.app` | VPS IPv4 | **Proxied** |
| AAAA | `kodji.app` | VPS IPv6 | Proxied (optional) |
| CNAME | `www` | `kodji.app` | **Proxied** |
| MX | `kodji.app` | `mx1.privateemail.com`, priority 10 | DNS only |
| MX | `kodji.app` | `mx2.privateemail.com`, priority 10 | DNS only |
| TXT | `kodji.app` | `v=spf1 include:spf.privateemail.com ~all` | DNS only |
| TXT | `default._domainkey` | PrivateEmail DKIM — copy from Namecheap *if present* | DNS only |
| CNAME | `autoconfig` / `autodiscover` | as Namecheap had them *if present* | DNS only |
| CNAME | `send.mail` | `send.forge.rmta.net` | DNS only |
| TXT | `resend._domainkey.mail` | `p=MIGf…` (Resend dashboard shows it) | DNS only |
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:dmarc@kodji.app` | DNS only |

Cloudflare's Name field is relative to `kodji.app`, like Namecheap's Host
was — the same values carry over unchanged.

## Appendix C — troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Cloudflare **522** | Cloudflare can't reach :443 — Caddy down, or ufw blocking | `systemctl status caddy`; re-run the ufw loop in §2d |
| Cloudflare **525 / 526** | Origin cert problem | Mode must be Full (strict); cert+key paths in the Caddyfile; `journalctl -u caddy` |
| Cloudflare **502** | Caddy up, app down | `journalctl -u kodji-terminal -n 50` |
| Unit `failed`, journal says `PendingMigrations` | Code ahead of DB | §3d, then `systemctl restart kodji-terminal` |
| "cross-origin request refused" | Browser host ≠ `PUBLIC_BASE_URL` | You're hitting the box by IP or a tunnel while `PUBLIC_BASE_URL=https://kodji.app`; use the real URL |
| Login → "We couldn't send that email", log `http 401` | Resend key invalid | New key, copied at creation; restart |
| Log `http 403 ... domain is not verified` | Resend hasn't verified `mail.kodji.app` | Resend → Domains → Restart verification; check `send.mail` is DNS-only |
| "Sign-in is temporarily unavailable" | Global send cap tripped | `journalctl … \| grep "send cap"`. A spray? Cloudflare rule on? A launch? Raise `LOGIN_MAX_SENDS_*` |
| Signed in, but no watchlists | First sign-in before the claim | `just claim-owner`, sign out, sign in |
| Sign-in mail lands in spam | DKIM/SPF/DMARC alignment | `dig TXT resend._domainkey.mail.kodji.app`; check Resend shows DKIM verified; give DMARC reports a few days |
| Cloudflare **redirect loop** | Encryption mode "Flexible" | Set Full (strict) |
| Pages render but HTMX swaps don't | Rocket Loader | Turn it off (§5d) |
