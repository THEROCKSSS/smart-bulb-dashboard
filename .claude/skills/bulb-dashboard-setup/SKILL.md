---
name: bulb-dashboard-setup
description: "Set up Smart Bulb Dashboard from zero: get Tuya device_id/local_key, install backend, run it."
---

# Smart Bulb Dashboard — Setup

## When to use
User wants to install/run this project on a new machine, add a new bulb to
an existing install, or is stuck getting a Tuya `device_id`/`local_key`.

## The procedure

### 1. Find the bulb and its protocol version
```bash
pip install tinytuya
python -m tinytuya scan
```
Note the `Device ID` and `Version` (3.1/3.3/3.4/3.5) from the output. If
nothing appears, the bulb may be on a 5GHz network — Tuya bulbs are almost
always 2.4GHz-only, so the scanning machine needs to be on the same 2.4GHz
network.

### 2. Get the local_key — prefer the cloud-assisted QR login
This avoids the Tuya IoT Platform's project-linking QR bug (scans but never
confirms — usually a data-center mismatch between the Cloud Project region
and the app account's region).

1. In Tuya Smart / SmartLife app: **Me → Settings → Account and Security**
   → note the **User Code**.
2. `pip install tuya-device-sharing-sdk qrcode[pil]`
3. Run:
   ```python
   from tuya_sharing import LoginControl
   import qrcode
   lc = LoginControl()
   resp = lc.qr_code("HA_3y9q4ak7g4ephrvke", "haauthorize", "USER_CODE_HERE")
   token = resp["result"]["qrcode"]
   qrcode.make(f"tuyaSmart--qrLogin?token={token}").save("login_qr.png")
   ```
4. Open `login_qr.png`, scan with the app's **in-app scanner** (Me → QR icon
   — NOT the phone's Camera app, that's the #1 reason scans do nothing).
   Confirm the popup that appears on the phone.
5. Complete login and list devices:
   ```python
   success, info = lc.login_result(token, "HA_3y9q4ak7g4ephrvke", "USER_CODE_HERE")
   from tuya_sharing import Manager, SharingDeviceListener, SharingTokenListener
   class NullDL(SharingDeviceListener):
       def update_device(self,*a,**k): pass
       def add_device(self,*a,**k): pass
       def remove_device(self,*a,**k): pass
   class NullTL(SharingTokenListener):
       def update_token(self,*a,**k): pass
   auth = {"user_code":"USER_CODE_HERE","terminal_id":info["terminal_id"],
           "endpoint":info["endpoint"],"token_info":{"t":info["t"],"uid":info["uid"],
           "expire_time":info["expire_time"],"access_token":info["access_token"],
           "refresh_token":info["refresh_token"]}}
   mgr = Manager("HA_3y9q4ak7g4ephrvke", auth["user_code"], auth["terminal_id"],
                 auth["endpoint"], auth["token_info"], NullTL())
   mgr.add_device_listener(NullDL())
   mgr.update_device_cache()
   for dev_id, d in mgr.device_map.items():
       print(dev_id, d.name, d.local_key)
   ```
Full details and the Option A (IoT Platform) / Option C (rooted emulator)
fallbacks are in the repo's `SETUP.md`.

### 3. Install and configure the backend
```bash
cd backend
python -m venv venv
./venv/Scripts/activate   # source venv/bin/activate on Linux/Mac
pip install -r requirements.txt
cp config.example.json config.json
```
Edit `config.json`, filling in `device_id`, `local_key`, `ip`, `version` from
steps 1-2 for each bulb. `config.json` is git-ignored — never commit it.

### 4. Run it
```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8500
```
Open `http://localhost:8500`.

## Pitfalls

1. **QR scan does nothing (Option A / IoT Platform only)** — data-center
   mismatch between Cloud Project region and app account region, or you're
   logged in as a shared "family member" not the device owner. Switch to
   the cloud-assisted login above (Option B) instead of debugging this.
2. **`Device Unreachable` right after setup** — the bulb dropped off Wi-Fi
   or lost power; this is a real, common bulb-side event, not a config bug.
   Power-cycle it and retry.
3. **Commands accepted (200 OK) but bulb doesn't change** — wrong `version`
   in `config.json`. Try 3.3 first, then 3.1/3.4/3.5.
4. **Installing deps into a shared/global venv** — always create a fresh
   `venv/` inside `backend/` for this project. Installing `tinytuya`,
   `tuya-device-sharing-sdk`, etc. into some other shared Python environment
   risks dependency-version conflicts with unrelated tools.

## Verification
```bash
curl -s http://localhost:8500/api/system/health
curl -s -X POST http://localhost:8500/api/devices/<id>/test-connection
```
`test-connection` should report `"tcp_6668_reachable": true` and
`"status_ok": true`. If either is false, see Pitfalls above before assuming
the dashboard code is broken.
