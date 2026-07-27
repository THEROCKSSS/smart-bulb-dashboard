# Setup Guide

This walks through everything from zero: getting your bulb's local
credentials, installing the backend, and running the dashboard.

## Step 1 — Confirm your bulb is Tuya-based and find it on your network

Most cheap Wi-Fi RGB bulbs (Bytech, Merkury, Teckin, Feit, many Govee, and
dozens of other rebrands) run Tuya's firmware even if the app is branded
differently ("Smart Life", "Tuya Smart", or a manufacturer-specific app that's
secretly a reskin).

1. Find your bulb's IP address (check your router's DHCP client list, or use
   a LAN scanner).
2. Install `tinytuya` and scan for it:
   ```bash
   pip install tinytuya
   python -m tinytuya scan
   ```
   If it's a Tuya device, you'll see it listed with a `Device ID` and a
   protocol `Version` (e.g. `3.3`, `3.4`, `3.5`). Note both — you'll need
   them later. If nothing shows up, it may not be Tuya-based, or it's on a
   5GHz network your scanning machine isn't also on (Tuya bulbs are almost
   always 2.4GHz-only).

## Step 2 — Get the Local Key

This is the one hard part. The local key is a per-device secret that Tuya's
servers generate when the bulb is paired; it's required to send it any
command locally. There are three ways to get it, in order of how much we'd
recommend them:

### Option A — Tuya IoT Platform (iot.tuya.com) — official but sometimes flaky

1. Create a free account at [iot.tuya.com](https://iot.tuya.com), create a
   Cloud Project, and pick the data center matching your region.
2. Under **Devices**, link your Tuya Smart / Smart Life app account by
   scanning the QR code shown — **with the app's own in-app scanner**, not
   your phone's camera app (Me tab → QR icon, not the Camera app — this is
   the single most common reason the scan "does nothing").
3. Once linked, find your device in the Devices list, or use **Cloud → API
   Explorer → "Query Device Details"** with your `device_id` — the response
   includes `local_key`.
4. Known bug: this QR-link step sometimes scans successfully but never shows
   a confirmation popup on the phone. This is almost always a **data center
   mismatch** between your Cloud Project's region and the region your app
   account is registered under, or you're logged into the app as a shared
   "family member" instead of the account that actually owns the device.

### Option B — SmartLife/Tuya cloud-assisted login (recommended if Option A fights you)

This is the same official QR-login flow used by Home Assistant's built-in
Tuya integrations — it does **not** require creating an IoT Platform
project at all, and uses the app's normal scanner, so it sidesteps the
Option A bug entirely.

1. In the Tuya Smart / SmartLife app: **Me → Settings → Account and
   Security** → note the **User Code** shown there (case-sensitive).
2. Install the official device-sharing SDK:
   ```bash
   pip install tuya-device-sharing-sdk qrcode[pil]
   ```
3. Request a login QR token:
   ```python
   from tuya_sharing import LoginControl
   import qrcode

   CLIENT_ID = "HA_3y9q4ak7g4ephrvke"  # Home Assistant's public client id
   SCHEMA = "haauthorize"
   USER_CODE = "YOUR_USER_CODE_HERE"

   lc = LoginControl()
   resp = lc.qr_code(CLIENT_ID, SCHEMA, USER_CODE)
   token = resp["result"]["qrcode"]
   qrcode.make(f"tuyaSmart--qrLogin?token={token}").save("login_qr.png")
   ```
4. Open `login_qr.png` and scan it with the app's in-app scanner (Me → QR
   icon). Confirm the popup that appears on your phone.
5. Complete the login and pull your device list:
   ```python
   success, info = lc.login_result(token, CLIENT_ID, USER_CODE)
   # info now has access_token, refresh_token, terminal_id, endpoint, uid, t

   from tuya_sharing import Manager, SharingDeviceListener, SharingTokenListener

   class NullDL(SharingDeviceListener):
       def update_device(self, *a, **k): pass
       def add_device(self, *a, **k): pass
       def remove_device(self, *a, **k): pass

   class NullTL(SharingTokenListener):
       def update_token(self, *a, **k): pass

   auth = {
       "user_code": USER_CODE, "terminal_id": info["terminal_id"],
       "endpoint": info["endpoint"], "token_info": {
           "t": info["t"], "uid": info["uid"], "expire_time": info["expire_time"],
           "access_token": info["access_token"], "refresh_token": info["refresh_token"],
       },
   }
   manager = Manager(CLIENT_ID, auth["user_code"], auth["terminal_id"], auth["endpoint"], auth["token_info"], NullTL())
   manager.add_device_listener(NullDL())
   manager.update_device_cache()
   for dev_id, device in manager.device_map.items():
       print(dev_id, device.name, device.local_key)
   ```
6. Copy the `local_key` printed for your bulb.

This exact flow (steps 3–6) is implemented as a reusable one-off script
pattern in this project's Claude Code skill — see
`.claude/skills/bulb-dashboard-setup/`.

### Option C — Rooted Android emulator (fallback if both A and B fail)

Run the Tuya Smart / SmartLife app inside a rooted emulator (e.g. LDPlayer),
log into your account, let it sync your devices, then pull the local key
from the app's local storage/cache files via root file access. More manual,
but sidesteps both the cloud console and any app-level TLS pinning since
you're reading local disk, not intercepting network traffic.

## Step 3 — Install the backend

```bash
cd backend
python -m venv venv
./venv/Scripts/activate      # Windows; use `source venv/bin/activate` on Linux/Mac
pip install -r requirements.txt
```

## Step 4 — Configure your device

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
  "devices": [
    {
      "id": "bulb-1",
      "name": "Living Room Bulb",
      "device_id": "PASTE_DEVICE_ID_HERE",
      "local_key": "PASTE_LOCAL_KEY_HERE",
      "ip": "192.168.1.100",
      "version": 3.3,
      "gamma": 1.0
    }
  ],
  "groups": [
    { "id": "all", "name": "All Bulbs", "device_ids": ["bulb-1"] }
  ]
}
```

`version` must match what `tinytuya scan` reported in Step 1. If unsure,
try `3.3` first — it's the most common; if commands silently fail, try
`3.4` or `3.5`.

**`config.json` is git-ignored — never commit it.** It contains your bulb's
plaintext local key.

## Step 5 — Run it

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8500
```

Open **http://localhost:8500**. Go to the **Diagnostics** tab and click
**Run Connection Test** first — it should report `tcp_6668_reachable: true`
and `status_ok: true`. If not, see the Troubleshooting section below.

## Step 6 (optional) — Run via Docker

```bash
docker compose up -d --build
```

Note: `docker-compose.yml` uses `network_mode: host`, which only works on
Linux Docker hosts (not Docker Desktop on Mac/Windows). On Mac/Windows,
remove that line and rely on the `ports: 8500:8500` mapping instead — direct
bulb control still works fine over bridge networking; only the `/rescan`
UDP-broadcast discovery feature needs host networking.

## Troubleshooting

- **`Device Unreachable` / `tcp_6668_reachable: false`** — the bulb lost
  power or dropped off Wi-Fi. Physically check it's powered and the light is
  on, then retry. Cheap Wi-Fi bulbs do drop off Wi-Fi occasionally; this is
  normal, not a bug in this project.
- **Commands succeed but nothing happens on the bulb** — wrong `version` in
  config; try the other common values (3.1, 3.2, 3.3, 3.4, 3.5).
- **IP changed** — routers often reassign DHCP leases. Use the
  **Diagnostics → Rescan Network** button, or set a DHCP reservation for the
  bulb's MAC address in your router so its IP never changes.
- **Brightness slider flips the bulb to white/loses your color** — this was
  a real bug found and fixed during development (see `HANDOFF.md`); if
  you're on an old copy of this code, pull latest.
