"""Task 2.2 parity guard: `services/api/models.py` must mirror DATA_SPEC 2.3 / 3.1.

BUILD_ORDER 4.6 risk "Schema/code drift later" is mitigated here: the models are
compared field-by-field against the frozen JSON Schemas, so a schema edit that
is not reflected in the Pydantic ladder fails CI (and vice versa).
"""

from __future__ import annotations

import json
import pathlib
import re
import typing

import jsonschema
import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from services.api import models as m

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEME_SCHEMA = json.loads((ROOT / "schemas" / "scheme.schema.json").read_text(encoding="utf-8"))
PROFILE_SCHEMA = None
for _block in re.findall(r"```json\n(.*?)```", (ROOT / "docs" / "DATA_SPEC.md").read_text(encoding="utf-8"), re.S):
    _obj = json.loads(_block)
    if isinstance(_obj, dict) and _obj.get("title") == "ProfileContext":
        PROFILE_SCHEMA = _obj
        break

SEEDS = sorted((ROOT / "seeds").glob("*.json"))
FC = Draft202012Validator.FORMAT_CHECKER


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def resolve(prop: dict, defs: dict) -> dict:
    seen = 0
    while "$ref" in prop and seen < 8:
        prop = defs[prop["$ref"].split("/")[-1]]
        seen += 1
    return prop


def allows_null(prop: dict, defs: dict) -> bool:
    prop = resolve(prop, defs)
    if isinstance(prop.get("type"), list) and "null" in prop["type"]:
        return True
    if isinstance(prop.get("enum"), list) and None in prop["enum"]:
        return True
    for branch in prop.get("anyOf", []) + prop.get("oneOf", []):
        if branch.get("type") == "null":
            return True
        if allows_null(branch, defs):
            return True
    return False


def enum_values(prop: dict, defs: dict) -> set | None:
    """Literal/enum value set the schema allows, or None if unrestricted."""
    prop = resolve(prop, defs)
    if "enum" in prop:
        return {v for v in prop["enum"] if v is not None}
    if "const" in prop:
        return {prop["const"]}
    for branch in prop.get("anyOf", []) + prop.get("oneOf", []):
        got = enum_values(branch, defs)
        if got is not None:
            return got
    return None


def unwrap(annotation):
    """Strip Optional/Union-None so the underlying constrained type remains."""
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    return args[0] if len(args) == 1 and typing.get_args(annotation) else annotation


def model_enum_values(annotation) -> set | None:
    inner = unwrap(annotation)
    if isinstance(inner, type) and issubclass(inner, m.StrEnum):
        return set(inner)
    if typing.get_origin(inner) in (typing.Literal, None) and inner is not None:
        if typing.get_origin(inner) is typing.Literal:
            return set(typing.get_args(inner))
    if typing.get_args(inner) and typing.get_origin(inner) is typing.Literal:
        return set(typing.get_args(inner))
    return None


def is_defaulted(model, name: str) -> bool:
    field = model.model_fields[name]
    return not field.is_required()


def compare(model, schema_prop: dict, defs: dict, label: str, *, skip=()):
    """Field set + required set + nullability + enum values."""
    problems = []
    props = schema_prop["properties"]
    required = set(schema_prop.get("required", []))
    fields = set(model.model_fields)

    if fields != set(props):
        problems.append(
            f"{label}: field names differ  only-in-model={sorted(fields - set(props))} "
            f"only-in-schema={sorted(set(props) - fields)}"
        )

    for name in sorted(fields & set(props)):
        if name in skip:
            continue
        # JSON Schema: `required` wins over `default` (default is annotation
        # only), so a required field stays required even when it declares one.
        want_required = name in required
        got_required = model.model_fields[name].is_required()
        if want_required != got_required:
            problems.append(
                f"{label}.{name}: required={got_required}, schema wants {want_required}"
            )

        want_null = allows_null(props[name], defs)
        got_null = type(None) in typing.get_args(model.model_fields[name].annotation)
        if want_null != got_null:
            problems.append(
                f"{label}.{name}: nullable={got_null}, schema allows null={want_null}"
            )

        want_enum = enum_values(props[name], defs)
        got_enum = model_enum_values(model.model_fields[name].annotation)
        if want_enum is not None and got_enum is not None and want_enum != got_enum:
            problems.append(
                f"{label}.{name}: enum differs  missing={sorted(want_enum - got_enum, key=str)} "
                f"extra={sorted(got_enum - want_enum, key=str)}"
            )
    return problems


# --------------------------------------------------------------------------
# 1. extra="forbid" everywhere (additionalProperties: false)
# --------------------------------------------------------------------------
def test_every_model_forbids_extra_fields():
    classes = [
        obj
        for name, obj in vars(m).items()
        if isinstance(obj, type)
        and issubclass(obj, m.BaseModel)
        and obj is not m.BaseModel
        and not name.startswith("_")  # skip the shared _Object base
    ]
    assert classes, "no models discovered"
    for cls in classes:
        assert cls.model_config.get("extra") == "forbid", f"{cls.__name__} missing extra='forbid'"


# --------------------------------------------------------------------------
# 2. Scheme parity vs DATA_SPEC 2.3
# --------------------------------------------------------------------------
def test_scheme_field_parity():
    problems = compare(m.Scheme, SCHEME_SCHEMA, SCHEME_SCHEMA["$defs"], "Scheme")
    assert not problems, "\n".join(problems)


def test_scheme_required_count_matches_frozen_spec():
    assert len(SCHEME_SCHEMA["required"]) == 44
    strictly = sum(1 for f in m.Scheme.model_fields.values() if f.is_required())
    assert strictly == 44, strictly
    # The three schema-optional fields are exactly the non-required ones.
    optional = {n for n, f in m.Scheme.model_fields.items() if not f.is_required()}
    assert optional == {"name_hi", "academic_year", "alternate_urls"}, optional


def test_required_with_schema_default_is_not_optional():
    """`default` is annotation-only; omitting these must fail L1 (fail-closed)."""
    for key in ("conditional_gate_overrides", "domain_gates"):
        payload = _base_scheme()
        payload.pop(key)
        with pytest.raises(ValidationError, match=key):
            m.Scheme.model_validate(payload)


def test_scheme_properties_count():
    assert len(SCHEME_SCHEMA["properties"]) == len(m.Scheme.model_fields)


# --------------------------------------------------------------------------
# 3. Nested $defs parity
# --------------------------------------------------------------------------
NESTED = [
    (m.IntakeWindow, "IntakeWindow"),
    (m.Benefit, "Benefit"),
    (m.DocAlternative, "DocAlternative"),
    (m.RequiredDocument, "RequiredDocument"),
    (m.GateOverride, "GateOverride"),
    (m.DomainGate, "DomainGate"),
    (m.SoftClause, "SoftClause"),
    (m.SourceRef, "SourceRef"),
    (m.ParseMeta, "ParseMeta"),
]


@pytest.mark.parametrize("cls,def_name", NESTED, ids=[d for _, d in NESTED])
def test_nested_defs_parity(cls, def_name):
    problems = compare(cls, SCHEME_SCHEMA["$defs"][def_name], SCHEME_SCHEMA["$defs"], def_name)
    assert not problems, "\n".join(problems)


def test_gate_override_inner_objects_match_schema():
    gw = SCHEME_SCHEMA["$defs"]["GateOverride"]["properties"]
    when_props, set_props = gw["when"]["properties"], gw["set"]["properties"]
    assert set(m.GateOverrideWhen.model_fields) == set(when_props)
    assert set(m.GateOverrideSet.model_fields) == set(set_props)

    # Absent is legal for every key; explicit null is legal exactly when the
    # schema is nullable. Prove it behaviourally rather than by annotation
    # introspection, because an omitted optional key is modelled as None.
    for model, props in ((m.GateOverrideWhen, when_props), (m.GateOverrideSet, set_props)):
        for name, spec in props.items():
            nullable = allows_null(spec, SCHEME_SCHEMA["$defs"])
            payload = {name: None}
            if nullable:
                try:
                    model.model_validate(payload)
                except ValidationError as exc:  # pragma: no cover
                    pytest.fail(f"{model.__name__}.{name} is nullable but rejected null: {exc}")
            else:
                with pytest.raises(ValidationError, match="must not be null"):
                    model.model_validate(payload)


def test_absent_keys_are_not_materialised_as_null():
    """JSON absence vs null, and therefore byte-exact re-seeding."""
    when = m.GateOverrideWhen(social_category=[m.SocialCategory.OBC])
    assert when.model_dump(mode="json") == {"social_category": ["OBC"]}

    override = m.GateOverride(
        override_id="ovr_obc_income_ceiling",
        when=m.GateOverrideWhen(social_category=[m.SocialCategory.OBC]),
        set=m.GateOverrideSet(income_ceiling_annual=100000),
    )
    assert override.model_dump(mode="json") == {
        "override_id": "ovr_obc_income_ceiling",
        "when": {"social_category": ["OBC"]},
        "set": {"income_ceiling_annual": 100000},
    }


def test_explicit_null_is_still_emitted_when_supplied():
    """An explicitly passed null is data, not absence, and must survive the dump."""
    gw = m.GateOverrideSet(income_ceiling_annual=None, min_age=18)
    assert gw.model_dump(mode="json") == {"income_ceiling_annual": None, "min_age": 18}


# --------------------------------------------------------------------------
# 4. ProfileContext parity vs DATA_SPEC 3.1
# --------------------------------------------------------------------------
def test_profile_context_parity():
    assert PROFILE_SCHEMA is not None, "ProfileContext schema not found in DATA_SPEC"
    problems = compare(m.ProfileContext, PROFILE_SCHEMA, PROFILE_SCHEMA["$defs"], "ProfileContext")
    assert not problems, "\n".join(problems)


def test_profile_facts_parity():
    facts = PROFILE_SCHEMA["properties"]["facts"]
    defs = PROFILE_SCHEMA["$defs"]
    assert set(m.ProfileFacts.model_fields) == set(facts["properties"])
    assert facts.get("additionalProperties") is False
    for name, spec in facts["properties"].items():
        ann = m.ProfileFacts.model_fields[name].annotation
        got_null = type(None) in typing.get_args(ann)
        assert got_null == allows_null(spec, defs), f"ProfileFacts.{name} nullability drift"


# --------------------------------------------------------------------------
# 5. Round-trip: both canonical instances through the ladder
# --------------------------------------------------------------------------
@pytest.mark.parametrize("seed", SEEDS, ids=[p.name for p in SEEDS])
def test_seed_round_trips_byte_for_byte(seed):
    raw = json.loads(seed.read_text(encoding="utf-8"))
    # L1 (jsonschema) and L3-ish ladder (Pydantic) must both accept it.
    Draft202012Validator(SCHEME_SCHEMA, format_checker=FC).validate(raw)
    parsed = m.Scheme.model_validate(raw)
    assert parsed.model_dump(mode="json") == raw, f"{seed.name} does not round-trip"


# --------------------------------------------------------------------------
# 6. Fail-closed behaviour
# --------------------------------------------------------------------------
def _base_scheme() -> dict:
    return json.loads(SEEDS[0].read_text(encoding="utf-8"))


def test_extra_key_is_rejected():
    payload = _base_scheme()
    payload["unexpected_key"] = "nope"
    with pytest.raises(ValidationError, match="unexpected_key"):
        m.Scheme.model_validate(payload)


def test_missing_required_key_is_rejected():
    """A required-but-nullable field must still be present (fail-closed)."""
    payload = _base_scheme()
    del payload["min_age"]
    with pytest.raises(ValidationError):
        m.Scheme.model_validate(payload)


def test_extra_key_on_profile_context_is_rejected():
    payload = {
        "schema_version": "1.0.0",
        "age": 30,
        "gender": "MALE",
        "domicile_state": "DL",
        "education_level": "bachelor",
        "annual_household_income": 400000,
        "social_category": "GENERAL",
        "minority_status": False,
        "occupation": "salaried",
        "documents_in_hand": [],
        "aqqa": 1,
    }
    with pytest.raises(ValidationError, match="aqqa"):
        m.ProfileContext.model_validate(payload)


def test_invalid_enum_member_is_rejected():
    payload = _base_scheme()
    payload["verification_status"] = "OPEN"
    with pytest.raises(ValidationError):
        m.Scheme.model_validate(payload)


def test_government_host_pattern_is_enforced():
    payload = _base_scheme()
    payload["portal_url"] = "https://example.com/apply"
    with pytest.raises(ValidationError):
        m.Scheme.model_validate(payload)


def test_duplicate_set_item_is_rejected():
    payload = _base_scheme()
    payload["tags"] = ["flagship", "flagship"]
    with pytest.raises(ValidationError, match="unique"):
        m.Scheme.model_validate(payload)


# --------------------------------------------------------------------------
# 7. L3 cross-field invariants asked for in task 2.2
# --------------------------------------------------------------------------
def test_v_cf1_min_age_above_max_age_rejected():
    payload = _base_scheme()
    payload["min_age"], payload["max_age"] = 40, 30
    with pytest.raises(ValidationError, match="V-CF1"):
        m.Scheme.model_validate(payload)


def test_v_cf1_null_bound_is_allowed():
    payload = _base_scheme()
    payload["min_age"], payload["max_age"] = None, 30
    m.Scheme.model_validate(payload)


def test_v_cf8_benefit_sum_mismatch_rejected():
    payload = _base_scheme()
    payload["benefits_total_value_annual"] = (payload["benefits_total_value_annual"] or 0) + 1
    with pytest.raises(ValidationError, match="V-CF8"):
        m.Scheme.model_validate(payload)


def test_v_cf8_skipped_when_any_annual_value_is_null():
    payload = _base_scheme()
    payload["benefits"] = [dict(b, annual_value_inr=None) for b in payload["benefits"]]
    payload["benefits_total_value_annual"] = None
    m.Scheme.model_validate(payload)


def test_v_cf9_ceiling_without_scope_rejected():
    payload = _base_scheme()
    payload["income_ceiling_annual"] = 250000
    payload["income_ceiling_scope"] = None
    with pytest.raises(ValidationError, match="V-CF9"):
        m.Scheme.model_validate(payload)


def test_v_cf9_scope_without_ceiling_rejected():
    payload = _base_scheme()
    payload["income_ceiling_annual"] = None
    payload["income_ceiling_scope"] = "household"
    with pytest.raises(ValidationError, match="V-CF9"):
        m.Scheme.model_validate(payload)
