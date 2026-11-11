"""The world of the access OIDC tests (#439): one in-process provider, the configuration naming
it, and the sign-in service over the world's store."""

from __future__ import annotations

from dataclasses import dataclass

from orvex.access.authentication.service import SignedIn, SignIn
from orvex.access.oidc.configuration import Configuration
from orvex.access.oidc.identities import Identities
from orvex.access.oidc.service import OidcSignIn

from tests.support.access_directory import World
from tests.support.oidc_provider import CLIENT_ID, ISSUER, SECRET, FakeFetcher, FakeProvider
from tests.support.sign_in import Clock

ORIGIN = "https://bi.example.com"


def document(**provider: object) -> dict[str, object]:
    return {
        "providers": {
            "corp": {
                "issuer": ISSUER,
                "client_id": CLIENT_ID,
                "client_secret_variable": "CORP_SECRET",
                "algorithms": ["RS256", "ES256"],
                **provider,
            }
        },
        "mapping": [
            {
                "provider": "corp",
                "claim": "groups",
                "value": "finance",
                "project": "p1",
                "group": "finance",
            },
            {
                "provider": "corp",
                "claim": "groups",
                "value": "ops",
                "project": "p1",
                "group": "ops",
            },
        ],
    }


ENVIRONMENT = {"CORP_SECRET": SECRET}


@dataclass
class Oidc:
    world: World
    provider: FakeProvider
    fetcher: FakeFetcher
    configuration: Configuration
    service: OidcSignIn
    identities: Identities
    sign_in: SignIn
    clock: Clock

    def sign_in_as(self, subject: str, **claims: object) -> SignedIn:
        started = self.service.start("corp")
        answer = self.provider.authorize(started.location, subject, **claims)
        return self.service.callback("corp", answer, started.reference)
