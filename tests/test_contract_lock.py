"""Pin released public schemas without changing them or depending on line endings."""

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from student_agent.contracts import ContractError, Contracts

SCHEMAS = Path(__file__).resolve().parents[1] / "contracts/schemas"
LOCK = {
    "l3a-output-v2.schema.json": "28cfa3b4e58ae274f971a728f7c4804aa1ca8b304036a9d94ffa2aead1820fd1",
    "l3b-output-v2.schema.json": "6f207b95b426675b65d8de24adf76d61d4ff87bddc62d4e8afee20437900bcc0",
    "mcp-evidence-response-v1.schema.json": (
        "0335d8c92c9484331b3460ed92c8645b0bd64617f90edcb4cfd36b8ba5dc884c"
    ),
    "submission-manifest-v2.schema.json": (
        "d5976f4b4c0bcebbfa3006d8afb2cdaa0f4b24e325d54682e5c51532c2ed8549"
    ),
    "trace-event-v1.schema.json": (
        "f07497ba189f9d394b7b97cbf1928bd435af08ece2cb1efd7b4145a58434e8c2"
    ),
}


@pytest.mark.parametrize("name,digest", LOCK.items())
def test_public_contract_is_unchanged(name, digest):
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    canonical = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == digest


@pytest.mark.parametrize("name", LOCK)
def test_public_contract_rejects_unknown_top_level_fields(name):
    with pytest.raises(ContractError, match="Additional properties"):
        Contracts(SCHEMAS).validate(name, {"debug_private_field": True}, name)
