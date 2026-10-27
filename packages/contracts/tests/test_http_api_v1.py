import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError
from sextant.contracts.http_api import v1

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "http-api" / "v1" / "examples"


def model_for(example: Path) -> type[BaseModel]:
    model: type[BaseModel] = getattr(v1, example.name.split(".", 1)[0])
    return model


@pytest.mark.parametrize("example", sorted(EXAMPLES.glob("*/*.json")), ids=lambda path: path.name)
def test_generated_models_agree_with_the_examples(example: Path) -> None:
    data = json.loads(example.read_text())
    if example.parent.name == "valid":
        model_for(example).model_validate(data)
    else:
        with pytest.raises(ValidationError):
            model_for(example).model_validate(data)


def test_error_envelope_round_trip() -> None:
    envelope = v1.ErrorEnvelope(
        error=v1.Error(code="NOT_FOUND", message="No route", details=[], request_id="r-1")
    )
    assert v1.ErrorEnvelope.model_validate_json(envelope.model_dump_json()) == envelope
