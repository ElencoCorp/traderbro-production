# test_token_renewal.py
import os
import re
import requests
from dotenv import load_dotenv

load_dotenv()

token     = os.getenv("ACCESS_TOKEN")
client_id = os.getenv("CLIENT_ID")

print(f"Current token (first 30 chars): {token[:30]}...")
print(f"Client ID: {client_id}")
print("\nCalling /v2/RenewToken...")

resp = requests.get(
    "https://api.dhan.co/v2/RenewToken",
    headers={
        "access-token": token,
        "dhanClientId": client_id,
    },
    timeout=15,
)

print(f"HTTP Status: {resp.status_code}")

if resp.status_code == 200:
    data      = resp.json()
    new_token = data.get("token") or data.get("accessToken") or data.get("access_token")
    expiry    = data.get("expiryTime", "unknown")

    print(f"✅ New token received. Expires: {expiry}")
    print(f"   First 30 chars: {new_token[:30]}...")

    # ── Write new token back to .env ──────────────────────────
    env_path = ".env"
    content  = open(env_path).read()
    updated  = re.sub(r"^ACCESS_TOKEN=.*$", f"ACCESS_TOKEN={new_token}", content, flags=re.MULTILINE)
    open(env_path, "w").write(updated)
    print(f"✅ .env updated with new token.")
    print(f"\n🎉 Run this script again — it should succeed with the new token.")

else:
    print(f"❌ Failed: {resp.text}")