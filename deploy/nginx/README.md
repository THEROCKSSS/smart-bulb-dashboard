# nginx reverse proxy + certificate renewal (W2-032, W2-033)

`smart-bulb-dashboard.conf` is the nginx equivalent of
[`../caddy/Caddyfile`](../caddy/Caddyfile). Functionally they land in the
same place. The difference that matters is this one:

> **Caddy issues and renews certificates by itself. nginx does not.**
> With Caddy there is no renewal to set up, nothing to verify, and nothing
> to forget. With nginx you own that, and if you get it wrong you find out
> 90 days later when the site stops loading.

If you have no existing nginx investment, use Caddy. Everything below
exists for people who already run nginx and would rather not add a second
web server to the box.

## 1. Install the config

```bash
sudo cp smart-bulb-dashboard.conf /etc/nginx/conf.d/
sudo sed -i 's/yourname\.duckdns\.org/YOUR-ACTUAL-NAME.duckdns.org/g' \
    /etc/nginx/conf.d/smart-bulb-dashboard.conf
sudo mkdir -p /var/www/certbot
```

Don't run `nginx -t` yet — the config references certificate files that
don't exist until step 2, and nginx will (correctly) refuse it.

## 2. Get the first certificate

```bash
sudo apt install certbot python3-certbot-nginx      # Debian/Ubuntu
sudo certbot certonly --webroot -w /var/www/certbot \
    -d YOUR-ACTUAL-NAME.duckdns.org \
    --agree-tos -m you@example.com --no-eff-email
sudo nginx -t && sudo systemctl reload nginx
```

`--webroot` (rather than `--nginx`) is deliberate: it leaves your config
alone instead of rewriting it, and it uses the
`/.well-known/acme-challenge/` location already in the file. That location
is served over plain HTTP and **must not** be redirected to HTTPS — if you
"tidy up" the `:80` server block into a blanket redirect, issuance still
works today and every future renewal fails silently.

DuckDNS has no rate limit of its own, but Let's Encrypt does: 5 failed
validations per hostname per hour. Add `--dry-run` while you're still
fighting with router port forwarding.

## 3. Renewal automation — the part that actually matters

Both certbot packages install a systemd timer that runs twice daily and
renews anything inside 30 days of expiry. Confirm it exists rather than
assuming:

```bash
systemctl list-timers 'certbot*'
systemctl status certbot.timer
```

If your distro ships a cron job instead, it's `/etc/cron.d/certbot`.
Either is fine. What is *not* fine is having neither, which is what
happens if you installed certbot from a snap or pip and skipped the
service unit.

**nginx needs a reload after renewal, and certbot does not do that for
you** unless you tell it to. This is the single most common way an nginx
TLS setup dies: renewal succeeds, the new cert sits on disk, and nginx
keeps serving the old one from memory until it expires.

```bash
sudo mkdir -p /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh >/dev/null <<'EOF'
#!/bin/sh
# Runs only after a certificate is actually renewed, not on every check.
/usr/sbin/nginx -t && /bin/systemctl reload nginx
EOF
sudo chmod +x /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
```

Then prove the whole path works, before you're relying on it:

```bash
sudo certbot renew --dry-run
```

That exercises validation and the deploy hook against Let's Encrypt's
staging environment. If it passes, renewal is genuinely set up. If you
skip this step you have configured renewal on the same evidentiary basis
as not configuring it.

## 4. Expiry monitoring (W2-046)

Renewal automation fails quietly — the failure mode is nothing happening.
The post-deploy smoke test reports days-to-expiry so you notice:

```bash
python3 ../smoke-test.py --base-url https://YOUR-ACTUAL-NAME.duckdns.org
```

For a standing check, that same command in a weekly cron job is enough; it
exits non-zero when the certificate has under 14 days left. Let's Encrypt
also emails the address from step 2 at 20 and 7 days — don't opt out of
those, they are the backstop for the case where your monitoring is the
thing that broke.

## 5. LAN-only, no public domain (W2-042)

Use `./make-selfsigned-cert.sh`, which builds a certificate with the
Subject Alternative Names browsers actually check:

```bash
./make-selfsigned-cert.sh bulbs.lan 192.168.1.20 /etc/nginx/ssl
```

Then point `ssl_certificate` / `ssl_certificate_key` at the generated
`.crt` / `.key`, drop the `:80` ACME location, and skip steps 2–4 entirely.
Note that self-signed encrypts the connection but authenticates nothing,
and every browser warns on first visit.
[`../caddy/Caddyfile.lan-selfsigned`](../caddy/Caddyfile.lan-selfsigned)
does this better if you're open to Caddy — its local CA can be installed
once and then every cert it issues is trusted with no per-host warning.

## 6. Required backend setting

Whichever certificate path you take, the dashboard must run with:

```
SBD_TRUSTED_PROXIES=127.0.0.1,::1
```

Without it the backend ignores every `X-Forwarded-*` header this config
sets, keys its per-IP lockout to `127.0.0.1`, and one attacker's five wrong
PIN guesses lock out every remote user at once. See
[`../../docs/deployment.md`](../../docs/deployment.md) for why it's opt-in
and what happens if you set it too broadly. Verify after deploying:

```bash
curl -s https://YOUR-ACTUAL-NAME.duckdns.org/api/system/proxy-status | jq
```

`client_ip` must be your real address, not `127.0.0.1`.
