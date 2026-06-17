# Cloudflare Access — Securing the Dashboards

The dashboards are exposed to the public internet via the **`starks-dashboards`** Cloudflare
Tunnel (`~/.cloudflared/config.yml`). A tunnel alone is **not auth** — anyone who lands on the URL
gets in. **Cloudflare Access** sits in front of a hostname and gates it to only your identity, so
the trade-control buttons (carry deny, flags, flatten) are protected.

| Hostname | Tunnel route | Access (auth) |
|---|---|---|
| `apex.clawbotinator.trade` → :8533 | ✅ | ✅ **gated** (app "apex", policy "Only me") |
| `tricity.clawbotinator.trade` → :8503 | ✅ | ⬜ public (lock via the wildcard below) |
| `darkcity.clawbotinator.trade` → :8501 | ✅ | ⬜ public |
| `compounder.clawbotinator.trade` → :8502 | ✅ | ⬜ public |

- **Cloudflare team domain:** `still-shadow-1663.cloudflareaccess.com`
- **Allowed identity:** `clawbotinator@proton.me` (Zero Trust Free)
- **Login method:** email **one-time PIN** (no password — Cloudflare emails a 6-digit code)
- **The "Only me" policy** (Allow · Include → Emails → `clawbotinator@proton.me`) is reusable
  across apps — don't rebuild it.

**What login looks like:** visit a gated URL → Cloudflare login → enter your email → 6-digit PIN
arrives by email → enter it → you're in for 24 h.

---

## TODO — lock the remaining dashboards (one wildcard app)

A wildcard app gates `tricity` / `darkcity` / `compounder` (and any future dashboard) in one shot,
reusing the existing **Only me** policy. The specific `apex` app keeps precedence — no conflict.

1. **Cloudflare One → Access controls → Applications → Create new application → Self-hosted → Continue**
2. **Application details → Destinations → Public hostnames:**
   - **Subdomain:** `*`  *(if the field rejects it, click "Switch to custom input" → `*.clawbotinator.trade`)*
   - **Domain:** `clawbotinator.trade` · **Path:** empty
3. **Details:** Name `All dashboards (wildcard)` · Session `24 hours`
4. **Access policies → Add existing policy** dropdown → select **Only me**
   *(do NOT "Create new policy" — just reuse it)*
5. **Authentication:** confirm **"Accept all available identity providers"** is **ON**
6. Preview should read **Policies: Only me · Destinations: \*.clawbotinator.trade** → **Create**
7. **Verify:** open `https://tricity.clawbotinator.trade` → should bounce to the Cloudflare login

> Bonus: with the wildcard in place, any new dashboard you tunnel later is gated by default.

---

## Adding a NEW dashboard to the tunnel (reference)

1. Add an ingress rule to `~/.cloudflared/config.yml` (before the `404` catch-all):
   ```yaml
     - hostname: NEW.clawbotinator.trade
       service: http://localhost:PORT
   ```
2. Add the DNS route:
   ```bash
   cloudflared tunnel route dns starks-dashboards NEW.clawbotinator.trade
   ```
3. Restart the tunnel (it's a manual process, not a service):
   ```bash
   kill $(pgrep -f 'run starks-dashboards'); sleep 2
   nohup cloudflared tunnel --config ~/.cloudflared/config.yml run starks-dashboards \
     > ~/.cloudflared/tunnel.log 2>&1 &
   ```
4. With the wildcard Access app in place, the new hostname is auto-gated. (Without it, create a
   per-host Access app reusing the **Only me** policy.)

---

## Verify gating (from the terminal)

```bash
curl -s -i https://HOST.clawbotinator.trade/ | grep -i 'cloudflareaccess\|location'
# 🔒 gated → redirects to *.cloudflareaccess.com   ·   ⚠️ public → serves the app HTML directly
```
