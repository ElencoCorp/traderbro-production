import requests
import pyotp
import os
import json
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

CLIENT_ID = os.getenv("DHAN_CLIENT_ID")
PIN = os.getenv("DHAN_PIN")
TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET")

TOKEN_FILE = "token_data.json"


def save_token(data):

    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=4)


def load_token():

    if not os.path.exists(TOKEN_FILE):
        return None

    with open(TOKEN_FILE, "r") as f:
        return json.load(f)


def generate_new_token():

    totp = pyotp.TOTP(TOTP_SECRET).now()

    url = (
        "https://auth.dhan.co/app/generateAccessToken"
        f"?dhanClientId={CLIENT_ID}"
        f"&pin={PIN}"
        f"&totp={totp}"
    )

    response = requests.post(url)

    data = response.json()

    if "accessToken" in data:

        token_data = {
            "access_token": data["accessToken"],
            "expiry": data["expiryTime"],
            "created_at": str(datetime.now())
        }

        save_token(token_data)

        print("✅ NEW TOKEN GENERATED")

        return token_data["access_token"]

    else:

        print("❌ TOKEN GENERATION FAILED")
        print(data)

        return None


def renew_token(current_token):

    url = "https://api.dhan.co/v2/RenewToken"

    headers = {
        "access-token": current_token,
        "dhanClientId": CLIENT_ID
    }

    response = requests.get(url, headers=headers)

    data = response.json()

    if "accessToken" in data:

        token_data = {
            "access_token": data["accessToken"],
            "expiry": data["expiryTime"],
            "created_at": str(datetime.now())
        }

        save_token(token_data)

        print("✅ TOKEN RENEWED")

        return token_data["access_token"]

    else:

        print("⚠ Renewal failed → generating new token")

        return generate_new_token()


def get_access_token():

    token_data = load_token()

    if not token_data:

        return generate_new_token()

    return renew_token(token_data["access_token"])


if __name__ == "__main__":

    token = get_access_token()

    print("ACTIVE TOKEN:")
    print(token)