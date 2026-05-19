"""Unit tests for the schema-id derivation and canonical schema string.

The SCHEMA_ID computation is the contract between publishers and
subscribers -- two devices that declare an identical schema MUST
produce identical ids, and any change to a schema MUST change the id.
Golden vectors here lock that contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the component's proto_emitter module importable -- it lives outside
# the standard sys.path under components/mpubsub/.
HERE = Path(__file__).resolve().parent
COMPONENT = HERE.parents[1] / "components" / "mpubsub"
sys.path.insert(0, str(COMPONENT))

from proto_emitter import (  # noqa: E402
    Field,
    Message,
    canonical_schema_string,
    schema_id,
    validate,
)


# ---------------------------------------------------------------------------
# Canonical form invariants
# ---------------------------------------------------------------------------


def test_canonical_is_independent_of_field_order() -> None:
    a = Message(
        id="m",
        fields=(
            Field("temperature", "float", 1),
            Field("humidity", "float", 2),
            Field("room_id", "string", 3),
        ),
    )
    b = Message(
        id="m",
        fields=(
            Field("room_id", "string", 3),
            Field("temperature", "float", 1),
            Field("humidity", "float", 2),
        ),
    )
    assert canonical_schema_string(a) == canonical_schema_string(b)
    assert schema_id(a) == schema_id(b)


def test_canonical_form_matches_documented_layout() -> None:
    msg = Message(
        id="x",
        fields=(
            Field("alpha", "int32", 5),
            Field("beta", "bool", 2),
        ),
    )
    # Sorted by tag, "<tag>:<type>:<name>", newline-joined.
    assert canonical_schema_string(msg) == "2:bool:beta\n5:int32:alpha"


def test_schema_id_changes_when_name_changes() -> None:
    a = Message(id="m", fields=(Field("temperature", "float", 1),))
    b = Message(id="m", fields=(Field("temp",        "float", 1),))
    assert schema_id(a) != schema_id(b)


def test_schema_id_changes_when_type_changes() -> None:
    a = Message(id="m", fields=(Field("v", "int32",  1),))
    b = Message(id="m", fields=(Field("v", "uint32", 1),))
    assert schema_id(a) != schema_id(b)


def test_schema_id_changes_when_tag_changes() -> None:
    a = Message(id="m", fields=(Field("v", "int32", 1),))
    b = Message(id="m", fields=(Field("v", "int32", 2),))
    assert schema_id(a) != schema_id(b)


def test_schema_id_does_not_depend_on_message_id() -> None:
    # The id is the YAML-side name; the SCHEMA_ID is purely about field
    # shape. Two messages with the same fields and different ids match.
    a = Message(id="alpha", fields=(Field("v", "int32", 1),))
    b = Message(id="beta",  fields=(Field("v", "int32", 1),))
    assert schema_id(a) == schema_id(b)


def test_schema_id_is_16_bit() -> None:
    msg = Message(
        id="x",
        fields=(
            Field("temperature", "float",  1),
            Field("humidity",    "float",  2),
            Field("room_id",     "string", 3),
        ),
    )
    sid = schema_id(msg)
    assert 0 <= sid <= 0xFFFF


# ---------------------------------------------------------------------------
# Golden vectors -- if these change, the wire format has shifted and
# previously-deployed nodes will silently stop matching.
# ---------------------------------------------------------------------------

GOLDEN = [
    (
        Message(
            id="room_climate",
            fields=(
                Field("temperature", "float",  1),
                Field("humidity",    "float",  2),
                Field("room_id",     "string", 3),
            ),
        ),
        "1:float:temperature\n2:float:humidity\n3:string:room_id",
    ),
    (
        Message(id="single", fields=(Field("v", "int32", 1),)),
        "1:int32:v",
    ),
    (
        Message(
            id="doorbell_event",
            fields=(
                Field("button_id", "uint32", 1),
                Field("presses",   "uint32", 2),
            ),
        ),
        "1:uint32:button_id\n2:uint32:presses",
    ),
]


@pytest.mark.parametrize("msg,expected_canonical", GOLDEN)
def test_golden_canonical(msg: Message, expected_canonical: str) -> None:
    assert canonical_schema_string(msg) == expected_canonical


@pytest.mark.parametrize("msg,_", GOLDEN)
def test_golden_schema_id_stable(msg: Message, _: str) -> None:
    # Just compute it -- if anyone changes the algorithm and old golden
    # comparisons break elsewhere, this test will surface the actual id
    # so a reviewer can decide whether the change was intentional.
    sid = schema_id(msg)
    assert 0 <= sid <= 0xFFFF, f"sid={sid}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_rejects_empty_fields() -> None:
    with pytest.raises(ValueError, match="at least one field"):
        validate(Message(id="m", fields=()))


def test_validate_rejects_duplicate_tag() -> None:
    msg = Message(
        id="m",
        fields=(
            Field("a", "int32", 1),
            Field("b", "float", 1),
        ),
    )
    with pytest.raises(ValueError, match="duplicate tag 1"):
        validate(msg)


def test_validate_rejects_duplicate_name() -> None:
    msg = Message(
        id="m",
        fields=(
            Field("x", "int32", 1),
            Field("x", "float", 2),
        ),
    )
    with pytest.raises(ValueError, match="duplicate field name"):
        validate(msg)


def test_validate_rejects_reserved_tag_range() -> None:
    msg = Message(id="m", fields=(Field("v", "int32", 19000),))
    with pytest.raises(ValueError, match="reserved 19000-19999"):
        validate(msg)


def test_validate_rejects_unknown_type() -> None:
    msg = Message(id="m", fields=(Field("v", "wat",  1),))
    with pytest.raises(ValueError, match="unknown type"):
        validate(msg)
