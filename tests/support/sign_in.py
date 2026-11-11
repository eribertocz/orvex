"""Sign-in test helpers (#438): a clock the test moves, and users with a password of their own."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from orvex.access.authentication.service import SignIn

from tests.support.access_directory import World

ADDRESS = "203.0.113.7"
PASSWORD = "correct horse battery"


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __call__(self) -> datetime:
        return self.now

    def move(self, **amount: float) -> None:
        self.now += timedelta(**amount)


@dataclass
class Accounts:
    world: World
    sign_in: SignIn
    clock: Clock

    def with_password(self, name: str, password: str = PASSWORD) -> str:
        """A user who signed in with their temporary password and set `password`."""
        created = self.world.directory.create_user(self.world.admin, name, name.title())
        first = self.sign_in.login(name, created.temporary_password, ADDRESS)
        self.sign_in.change_password(
            first.session_id, created.temporary_password, password, ADDRESS
        )
        return created.id
