"""The conformance kit of the transform interface contract (#486 G1): every case any
implementation must pass, in process and serialized, with their recorded count."""

from __future__ import annotations

from orvex.contracts.kit import Kit
from orvex.contracts.transform import kit_models, kit_runs, kit_schedule
from orvex.contracts.transform.interface import TRANSFORM

TRANSFORM_KIT = Kit(
    contract=TRANSFORM,
    recorded=37,
    cases=(*kit_models.CASES, *kit_runs.CASES, *kit_schedule.CASES),
)
