"""Strava OAuth 2.0 per-user authorization flow."""

import os
import requests

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"


def get_auth_url(chat_id: str) -> str:
    client_id = os.environ["STRAVA_CLIENT_ID"]
    public_domain = os.environ.get("PUBLIC_DOMAIN", "localhost")
    redirect_uri = f"https://{public_domain}/strava/callback"
    params = (
        f"client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&approval_prompt=auto"
        f"&scope=activity%3Aread_all"
        f"&state={chat_id}"
    )
    return f"{STRAVA_AUTH_URL}?{params}"


def exchange_code(code: str, chat_id: str) -> dict:
    resp = requests.post(STRAVA_TOKEN_URL, data={
        "client_id": os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
        "athlete_id": data.get("athlete", {}).get("id"),
        "athlete_firstname": data.get("athlete", {}).get("firstname", ""),
    }


def register_webhook(verify_token: str, public_domain: str) -> dict:
    resp = requests.post(
        "https://www.strava.com/api/v3/push_subscriptions",
        data={
            "client_id": os.environ["STRAVA_CLIENT_ID"],
            "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
            "callback_url": f"https://{public_domain}/strava/webhook",
            "verify_token": verify_token,
        },
        timeout=15,
    )
    return resp.json()


def get_webhook_subscription() -> dict:
    resp = requests.get(
        "https://www.strava.com/api/v3/push_subscriptions",
        params={
            "client_id": os.environ["STRAVA_CLIENT_ID"],
            "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def delete_webhook_subscription(subscription_id: int) -> None:
    requests.delete(
        f"https://www.strava.com/api/v3/push_subscriptions/{subscription_id}",
        params={
            "client_id": os.environ["STRAVA_CLIENT_ID"],
            "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
        },
        timeout=10,
    )
