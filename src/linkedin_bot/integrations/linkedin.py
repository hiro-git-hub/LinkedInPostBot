"""Offizielle LinkedIn-API: OAuth (3-legged), Profil-URN und Posts im Namen des Mitglieds."""

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
IMAGES_URL = "https://api.linkedin.com/rest/images?action=initializeUpload"
SCOPES = "openid profile w_member_social"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"

# Reservierte Zeichen des "little"-Formats – unmaskiert schneidet LinkedIn den Post ab oder interpretiert ihn.
LITTLE_RESERVED = re.compile(r"([\\|{}@\[\]()<>#*_~])")


class LinkedInError(Exception):
    pass


@dataclass
class LinkedInAuth:
    access_token: str
    expires_at: datetime
    person_urn: str
    name: str

    @property
    def expired(self) -> bool:
        return datetime.now(UTC) >= self.expires_at

    def expires_within(self, days: int) -> bool:
        return datetime.now(UTC) + timedelta(days=days) >= self.expires_at


def escape_little(text: str) -> str:
    return LITTLE_RESERVED.sub(r"\\\1", text)


def hashtag_template(tag: str) -> str:
    """Offizielles little-Hashtag-Template – ein roh geschriebenes #Tag würde durch escape_little unklickbar."""
    return "{hashtag|\\#|" + tag + "}"


def build_commentary(body: str, hashtags: list[str]) -> str:
    commentary = escape_little(body)
    if hashtags:
        commentary += "\n\n" + " ".join(hashtag_template(t) for t in hashtags)
    return commentary


def code_from_callback(params: dict[str, str], expected_state: str) -> str:
    """Prüft die Query-Parameter der Redirect-URL und liefert den Authorization Code."""
    if "error" in params:
        raise LinkedInError(f"LinkedIn hat abgelehnt: {params['error']} – {params.get('error_description', '')}")
    if params.get("state") != expected_state:
        raise LinkedInError("Ungültiger state-Parameter – Login abgebrochen (möglicher CSRF-Versuch).")
    if not params.get("code"):
        raise LinkedInError("Kein Code in der Adresse gefunden.")
    return params["code"]


def params_from_url(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url.strip()).query).items()}


def post_url(post_urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{post_urn}/"


class LinkedInClient:
    def __init__(self, api_version: str, http: httpx.Client | None = None):
        self.client_id = os.environ["LINKEDIN_CLIENT_ID"]
        self.client_secret = os.environ["LINKEDIN_CLIENT_SECRET"]
        self.redirect_uri = os.environ.get("LINKEDIN_REDIRECT_URI", DEFAULT_REDIRECT_URI)
        self.api_version = api_version
        self.http = http or httpx.Client(timeout=30)

    def authorize_url(self, state: str) -> str:
        return AUTH_URL + "?" + urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "scope": SCOPES,
        })

    def exchange_code(self, code: str) -> LinkedInAuth:
        response = self.http.post(TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        })
        self._raise_for_status(response, "Token-Austausch")
        token = response.json()
        expires_at = datetime.now(UTC) + timedelta(seconds=token["expires_in"])

        profile = self.http.get(USERINFO_URL, headers={"Authorization": f"Bearer {token['access_token']}"})
        self._raise_for_status(profile, "Profil abrufen")
        info = profile.json()
        return LinkedInAuth(token["access_token"], expires_at, f"urn:li:person:{info['sub']}", info.get("name", ""))

    def _headers(self, auth: LinkedInAuth) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {auth.access_token}",
            "LinkedIn-Version": self.api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def upload_image(self, auth: LinkedInAuth, data: bytes) -> str:
        """Registriert ein Bild, lädt die Bytes hoch und gibt die Bild-URN zurück."""
        response = self.http.post(IMAGES_URL, headers=self._headers(auth),
                                  json={"initializeUploadRequest": {"owner": auth.person_urn}})
        self._raise_for_status(response, "Bild-Upload vorbereiten")
        value = response.json()["value"]
        upload = self.http.put(value["uploadUrl"], content=data, headers={"Authorization": f"Bearer {auth.access_token}"})
        self._raise_for_status(upload, "Bild hochladen")
        return value["image"]

    def create_post(self, auth: LinkedInAuth, text: str, hashtags: list[str] | None = None,
                    image: bytes | None = None, alt_text: str = "", visibility: str = "PUBLIC") -> str:
        """Veröffentlicht einen Post (optional mit Bild) und gibt dessen URN zurück."""
        body = {
            "author": auth.person_urn,
            "commentary": build_commentary(text, hashtags or []),
            "visibility": visibility,
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image:
            body["content"] = {"media": {"id": self.upload_image(auth, image), "altText": alt_text}}
        response = self.http.post(POSTS_URL, headers=self._headers(auth), json=body)
        self._raise_for_status(response, "Post erstellen")
        return response.headers["x-restli-id"]

    @staticmethod
    def _raise_for_status(response: httpx.Response, step: str) -> None:
        if response.is_error:
            raise LinkedInError(f"{step} fehlgeschlagen ({response.status_code}): {response.text[:300]}")
