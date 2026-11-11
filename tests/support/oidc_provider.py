"""An in-process OIDC provider for the #439 tests: discovery, keys and a token endpoint behind the
`Fetcher` interface, with ID tokens it signs with keys made per test. Every way of forging a
token or an answer that a real provider will not produce is a knob here."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from joserfc import jwt
from joserfc.jwk import ECKey, OctKey, RSAKey
from orvex.access.oidc.configuration import Provider
from orvex.access.oidc.discovery import failed

ISSUER = "https://idp.example.com/realms/corp"
CLIENT_ID = "bi-client"
SECRET = "client-secret-value-for-tests"


type Signer = Callable[[dict[str, Any], dict[str, Any]], str]


@dataclass
class Grant:
    nonce: str
    subject: str
    claims: dict[str, object]
    verifier_challenge: str


@dataclass
class FakeProvider:
    """One provider; `issuer` and the keys are its own."""

    issuer: str = ISSUER
    rsa: RSAKey = field(default_factory=lambda: RSAKey.generate_key(2048, parameters={"kid": "r1"}))
    ec: ECKey = field(default_factory=lambda: ECKey.generate_key("P-256", parameters={"kid": "e1"}))
    discovery_extra: dict[str, Any] = field(default_factory=dict)
    gets: list[str] = field(default_factory=list)
    posts: list[tuple[str, dict[str, str], tuple[str, str]]] = field(default_factory=list)
    grants: dict[str, Grant] = field(default_factory=dict)
    # Knobs for forged answers:
    sign: Signer | None = None
    token_answer: Callable[[str], bytes] | None = None
    claims_extra: dict[str, Any] = field(default_factory=dict)
    now: Callable[[], float] = time.time

    @property
    def host(self) -> str:
        return urlsplit(self.issuer).hostname or ""

    def discovery(self) -> dict[str, Any]:
        base = f"https://{self.host}"
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{base}/auth",
            "token_endpoint": f"{base}/token",
            "jwks_uri": f"{base}/certs",
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
            **self.discovery_extra,
        }

    def jwks(self) -> dict[str, Any]:
        return {"keys": [self.rsa.as_dict(private=False), self.ec.as_dict(private=False)]}

    def id_token(self, grant: Grant, **claims: object) -> str:
        now = int(self.now())
        body: dict[str, Any] = {
            "iss": self.issuer,
            "sub": grant.subject,
            "aud": CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "nonce": grant.nonce,
            **grant.claims,
            **self.claims_extra,
            **claims,
        }
        header = {"alg": "RS256", "kid": "r1"}
        if self.sign is not None:
            return self.sign(header, body)
        return jwt.encode(header, body, self.rsa)

    def authorize(self, location: str, subject: str, **claims: object) -> dict[str, str]:
        """The provider's answer to the browser: the callback's parameters."""
        query = {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()}
        code = secrets.token_urlsafe(16)
        self.grants[code] = Grant(query["nonce"], subject, claims, query["code_challenge"])
        return {"code": code, "state": query["state"], "iss": self.issuer}


def hmac_with_secret(header: dict[str, Any], body: dict[str, Any]) -> str:
    """The algorithm-confusion forgery: HS256 keyed with the client secret."""
    return jwt.encode({**header, "alg": "HS256"}, body, OctKey.import_key(SECRET))


def unsigned(header: dict[str, Any], body: dict[str, Any]) -> str:
    import base64

    def part(value: Mapping[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({**header, 'alg': 'none'})}.{part(body)}."


def signed_by_stranger(header: dict[str, Any], body: dict[str, Any]) -> str:
    stranger = RSAKey.generate_key(2048, parameters={"kid": "r1"})
    return jwt.encode(header, body, stranger)


class FakeFetcher:
    """The `Fetcher` the access tests use: answers from the fake providers by host."""

    def __init__(self, *providers: FakeProvider) -> None:
        self.providers = {provider.host: provider for provider in providers}

    def _of(self, url: str) -> FakeProvider:
        found = self.providers.get(urlsplit(url).hostname or "")
        if found is None:
            raise failed("network")
        return found

    def get(self, provider: Provider, url: str) -> bytes:
        fake = self._of(url)
        fake.gets.append(url)
        path = urlsplit(url).path
        if path.endswith("/.well-known/openid-configuration"):
            return json.dumps(fake.discovery()).encode()
        if path == "/certs":
            return json.dumps(fake.jwks()).encode()
        raise failed("network")

    def post_form(
        self, provider: Provider, url: str, form: Mapping[str, str], basic: tuple[str, str]
    ) -> bytes:
        import base64
        import hashlib

        fake = self._of(url)
        fake.posts.append((url, dict(form), basic))
        if fake.token_answer is not None:
            return fake.token_answer(form["code"])
        grant = fake.grants.pop(form.get("code", ""), None)
        verifier = form.get("code_verifier", "")
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if grant is None or grant.verifier_challenge != challenge or basic != (CLIENT_ID, SECRET):
            raise failed("token_exchange")
        return json.dumps({"id_token": fake.id_token(grant), "access_token": "opaque"}).encode()
