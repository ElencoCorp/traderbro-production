import requests
import pyotp
import os
from dotenv import load_dotenv

load_dotenv()

CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
PIN = os.getenv("DHAN_PIN")
TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET")

# GENERATE LIVE TOTP
totp = pyotp.TOTP(TOTP_SECRET).now()

url = (
    "https://auth.dhan.co/app/generateAccessToken"
    f"?dhanClientId={CLIENT_ID}"
    f"&pin={PIN}"
    f"&totp={totp}"
)

response = requests.post(url)

data = response.json()

print(data)

if "accessToken" in data:

    token = data["accessToken"]

    with open("token.txt", "w") as f:
        f.write(token)

    print("✅ Token generated successfully")

else:

    print("❌ Failed")