"""
One-time login the way the Eduarte Student app does it, to get a refresh token.

The app logs in with an OAuth2 authorization code + PKCE flow at
login.educus.nl and then keeps itself signed in with a refresh token. That
refresh never touches Microsoft, so no MFA and no one-hour web session.
This script does the same login once, in a visible browser, and saves the
tokens so a scheduled job can keep refreshing them.

Usage (from the repo folder, with the GitHub CLI logged in):
    pip install -r requirements.txt
    playwright install chromium
    python scripts/app_login.py              # log in, upload to the secret, start a run
    python scripts/app_login.py --no-upload  # just write tokens.json for local use

Produces:
    tokens.json   -- access + refresh token. This is a login credential.
                     Never commit it (it's gitignored). After a successful
                     upload it's deleted, since local and CI use would clash.

What it prints is safe to share: token lifetimes, whether the refresh token
rotates, and the non-personal claims of the access token (issuer, audience,
scopes), which tell us which API the app talks to.
"""

import base64
import hashlib
import json
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

import requests
from playwright.sync_api import sync_playwright

AUTHORIZE_URL = "https://login.educus.nl/oauth2/authorize"
TOKEN_URL = "https://login.educus.nl/oauth2/token"
CLIENT_ID = "c42c95ad-1dc4-43f0-b975-63f0cc184cb1"
REDIRECT_URI = "eduartestudent://app/oauthCallback"
ORGANISATIE_UUID = "C1FA9594-1C13-48EE-B0B4-C44F669185B2"
TOKENS_PATH = Path("tokens.json")

SAFE_CLAIMS = ("iss", "aud", "azp", "client_id", "scope", "scp", "exp", "iat", "token_use", "typ")


def pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def describe(label, tok):
    print(f"\n{label}:")
    print("  fields:", ", ".join(sorted(tok)))
    for k in ("token_type", "expires_in", "refresh_expires_in", "scope"):
        if k in tok:
            print(f"  {k}: {tok[k]}")
    claims = jwt_claims(tok.get("access_token", ""))
    if claims is None:
        print("  access_token is not a JWT (opaque)")
    else:
        print("  access_token claims (safe subset):")
        for k in SAFE_CLAIMS:
            if k in claims:
                print(f"    {k}: {claims[k]}")
        other = sorted(set(claims) - set(SAFE_CLAIMS))
        print(f"    other claim names: {', '.join(other)}")


def get_code(challenge):
    params = {
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "fixed-organisatie": "1",
        "organisatieuuid": ORGANISATIE_UUID,
        "client_id": CLIENT_ID,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    found = {}

    def check(location):
        if location and location.startswith("eduartestudent://") and "code" not in found:
            q = parse_qs(urlsplit(location).query)
            if "code" in q:
                found["code"] = q["code"][0]
            elif "error" in q:
                found["error"] = q

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        context.on("response", lambda r: check(r.headers.get("location")))
        context.on("request", lambda r: check(r.url))
        page = context.new_page()
        print("Log in with your Summa account and approve the prompt on your phone.")
        print("The window closes by itself once the login is done.")
        try:
            page.goto(url)
        except Exception:
            pass
        deadline = time.time() + 300
        while "code" not in found and "error" not in found and time.time() < deadline:
            page.wait_for_timeout(500)
        browser.close()

    if "error" in found:
        sys.exit(f"Login returned an error: {found['error']}")
    if "code" not in found:
        sys.exit("No code received within 5 minutes. Run it again.")
    return found["code"]


def main():
    verifier, challenge = pkce_pair()
    code = get_code(challenge)
    print("\nGot an authorization code, exchanging it...")

    r = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    }, timeout=30)
    if r.status_code != 200:
        sys.exit(f"Token exchange failed: HTTP {r.status_code}\n{r.text[:500]}")
    tok = r.json()
    describe("Token response", tok)

    if "refresh_token" not in tok:
        TOKENS_PATH.write_text(json.dumps(tok, indent=2))
        sys.exit("\nNo refresh_token in the response. Send me the output above.")

    # Prove the refresh works right away, and see whether it rotates.
    r2 = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": CLIENT_ID,
    }, timeout=30)
    if r2.status_code != 200:
        TOKENS_PATH.write_text(json.dumps(tok, indent=2))
        sys.exit(f"\nRefresh failed: HTTP {r2.status_code}\n{r2.text[:500]}")
    tok2 = r2.json()
    rotated = "refresh_token" in tok2 and tok2["refresh_token"] != tok["refresh_token"]
    print(f"\nRefresh works. Refresh token rotates: {'yes' if rotated else 'no'}")
    if not rotated:
        tok2.setdefault("refresh_token", tok["refresh_token"])
    tok2["obtained_at"] = int(time.time())
    TOKENS_PATH.write_text(json.dumps(tok2, indent=2))
    print(f"Saved tokens to {TOKENS_PATH.resolve()} (keep this private).")

    if "--no-upload" in sys.argv:
        return
    upload(tok2["refresh_token"])


def upload(refresh_token):
    """Hand the token to GitHub Actions and start a roster run.

    Afterwards tokens.json is deleted: the token rotates on every use, so a
    local copy and the CI copy would invalidate each other.
    """
    if shutil.which("gh") is None:
        print("\nGitHub CLI (gh) not found. Paste the refresh_token from tokens.json into the "
              "EDUARTE_REFRESH_TOKEN secret yourself, then delete tokens.json.")
        return
    try:
        subprocess.run(["gh", "secret", "set", "EDUARTE_REFRESH_TOKEN"], input=refresh_token,
                       text=True, check=True)
    except subprocess.CalledProcessError:
        print("\nCouldn't set the secret (is `gh auth login` done, and are you in the repo folder?).")
        return
    TOKENS_PATH.unlink()
    print("Stored it in the EDUARTE_REFRESH_TOKEN secret and deleted the local tokens.json.")
    try:
        subprocess.run(["gh", "workflow", "run", "update-roster.yml", "--ref", "main"], check=True)
        print("Started a roster run; the calendar refreshes in a minute or two.")
    except subprocess.CalledProcessError:
        print("Secret is set, but starting the run failed. Start it from the Actions tab.")


if __name__ == "__main__":
    main()
