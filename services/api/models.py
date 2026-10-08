"""Pydantic v2 models mirroring the frozen canonical JSON Schemas.

BUILD_ORDER Step 2, task 2.2. Mirrors:
  * ``Scheme``            -- DATA_SPEC 2.3 (44 required fields, 26 ``$defs``)
  * ``ProfileContext``    -- DATA_SPEC 3.1 (schema_version + 9 fields + facts{})

Parity rules used throughout
----------------------------
``extra="forbid"`` is set on **every** model because every object in both
schemas declares ``additionalProperties: false``; an unknown key is a schema
violation, which the pipeline treats as fail-closed (DATA_SPEC 7.5).

Null discipline -- the one place this file deliberately diverges from a naive
reading of "use ``Type | None = None``":

    A field listed in the schema's ``required`` array stays REQUIRED here even
    when its type is ``["<type>", "null"]``.  Nullable != optional.  Writing
    ``min_age: StrictInt | None = None`` would let ``Scheme`` accept a document with
    no ``min_age`` key, while ``jsonschema`` rejects it -- a fail-open drift
    between layer L1 and the Pydantic ladder.  ``= None`` is therefore used
    only where the schema omits the field from ``required`` (``name_hi``,
    ``academic_year``) or declares an explicit default (the ``default: []`` /
    ``default: {}`` cases, which tasks 2.2/4.1 call out as list/fact defaults).

``date`` / ``datetime`` are used wherever the schema declares
``format: "date"`` / ``"date-time"``, which is stricter than ``str`` and
round-trips byte-identically through ``model_dump(mode="json")``.

Cross-field invariants (layer L3, DATA_SPEC 2.4) enforced here: V-CF1,
V-CF8, V-CF9.  The remaining nine are enforced by
``python -m tools.validate_examples``; see its module docstring.

Scalar typing
-------------
``bool`` / ``int`` / ``float`` fields are declared as ``StrictBool`` /
``StrictInt`` / ``StrictFloat`` so that this ladder rejects exactly what JSON
Schema rejects.  Under Pydantic's default lax mode ``is_active: "true"`` and
``penalty_per_document: "0.10"`` would be coerced and accepted, whereas
``type: boolean`` / ``type: number`` fail them -- a silent fail-open path into
an eligibility engine.  ``StrictFloat`` still takes a JSON integer for a
``type: number`` field, matching jsonschema.  Enums and ``date``/``datetime``
stay lax because JSON legitimately carries them as strings; membership and
format are enforced by the enum classes and by the schema's ``format`` keyword.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    StrictBool,
    StrictFloat,
    StrictInt,
    model_serializer,
    model_validator,
)

# --------------------------------------------------------------------------
# Patterns (verbatim from DATA_SPEC 2.3; JSON Schema anchors with ^...$)
# --------------------------------------------------------------------------
_STATE_PATTERN = r"^(AN|AP|AR|AS|BR|CH|CG|DH|DL|GA|GJ|HR|HP|JH|JK|KA|KL|LA|LD|MP|MH|ML|MZ|NL|OR|PB|PY|RJ|SK|TN|TS|TR|UP|UK|WB)$"
_SCHEME_ID_PATTERN = r"^sch_[a-z0-9]+(?:_[a-z0-9]+)*$"
_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
_YEAR_PATTERN = r"^\d{4}-\d{2}$"
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
_GOV_HOST_PATTERN = r"^https://[^/]+\.(gov\.in|nic\.in)(:\d+)?(/.*)?$"
_DOC_ID_PATTERN = r"^doc_[a-z0-9_]+$"
_BENEFIT_ID_PATTERN = r"^ben_[a-z0-9_]+$"
_CLAUSE_ID_PATTERN = r"^cl_[a-z0-9_]+$"
_RULE_ID_PATTERN = r"^dg_[a-z0-9_]+$"
_OVERRIDE_ID_PATTERN = r"^ovr_[a-z0-9_]+$"
_CONTENT_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"

# Schema keyword shorthands.
StateCode = Annotated[str, StringConstraints(pattern=_STATE_PATTERN)]
SchemeStateCode = Literal["ALL"] | StateCode
DocId = Annotated[str, StringConstraints(pattern=_DOC_ID_PATTERN)]
GovernedUri = Annotated[str, StringConstraints(pattern=_GOV_HOST_PATTERN)]

# DATA_SPEC 2.5 -- an integer enum (0..4), kept as Literal so JSON ints
# round-trip exactly instead of being boxed into an IntEnum member.
DocumentTier = Literal[0, 1, 2, 3, 4]
RequirementWeight = Literal[0.0, 0.5, 1.0]
AlternativeWeight = Literal[0.0, 0.5]


# --------------------------------------------------------------------------
# Enums -- every restricted string field in DATA_SPEC 2.3 / 3.1
# --------------------------------------------------------------------------
class SchemeType(StrEnum):
    SCHOLARSHIP = "scholarship"
    FEE_REIMBURSEMENT = "fee_reimbursement"
    DIRECT_BENEFIT_TRANSFER = "direct_benefit_transfer"
    SUBSIDY = "subsidy"
    HEALTHCARE = "healthcare"
    SKILL_DEVELOPMENT = "skill_development"
    EMPLOYMENT = "employment"
    AGRICULTURE = "agriculture"
    HOUSING = "housing"
    SOCIAL_WELFARE = "social_welfare"
    PENSION = "pension"
    INSURANCE = "insurance"
    FOOD_SUBSIDY = "food_subsidy"
    OTHER = "other"


class WelfareCategory(StrEnum):
    EDUCATION = "education"
    AGRICULTURE = "agriculture"
    HEALTH = "health"
    EMPLOYMENT = "employment"
    HOUSING = "housing"
    SOCIAL_WELFARE = "social_welfare"
    SKILL_DEVELOPMENT = "skill_development"
    FINANCE = "finance"
    RURAL_DEVELOPMENT = "rural_development"


class SocialCategory(StrEnum):
    GENERAL = "GENERAL"
    OBC = "OBC"
    SC = "SC"
    ST = "ST"
    EWS = "EWS"


class Gender(StrEnum):
    """Scheme.gender -- includes ALL for unrestricted schemes."""

    MALE = "MALE"
    FEMALE = "FEMALE"
    TRANSGENDER = "TRANSGENDER"
    ALL = "ALL"


class ProfileGender(StrEnum):
    """ProfileContext.gender -- deliberately no ALL; a citizen has one gender."""

    MALE = "MALE"
    FEMALE = "FEMALE"
    TRANSGENDER = "TRANSGENDER"


class EducationLevel(StrEnum):
    BELOW_10 = "below_10"
    TENTH = "10th"
    TWELFTH = "12th"
    DIPLOMA = "diploma"
    BACHELOR_1ST_YEAR = "bachelor_1st_year"
    BACHELOR = "bachelor"
    POST_GRAD = "post_grad"
    PHD = "phd"


class SchemeStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"


class IndexState(StrEnum):
    INDEXED = "indexed"
    PENDING_REVIEW = "pending_review"
    FETCH_UNREACHABLE = "fetch_unreachable"
    SUPERSEDED = "superseded"


class VerificationStatus(StrEnum):
    INTAKE_OPEN = "INTAKE_OPEN"
    INTAKE_CLOSED = "INTAKE_CLOSED"
    UNREACHABLE = "UNREACHABLE"
    BLOCKED = "BLOCKED"
    UNVERIFIED = "UNVERIFIED"
    PENDING = "PENDING"


class CycleStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UPCOMING = "upcoming"
    UNKNOWN = "unknown"


class ApplicationMode(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    HYBRID = "hybrid"


class BenefitType(StrEnum):
    TUITION_REIMBURSEMENT = "tuition_reimbursement"
    MAINTENANCE_ALLOWANCE = "maintenance_allowance"
    DIRECT_CASH_TRANSFER = "direct_cash_transfer"
    FEE_WAIVER = "fee_waiver"
    EQUIPMENT_SUBSIDY = "equipment_subsidy"
    INPUT_SUBSIDY = "input_subsidy"
    INSURANCE_COVER = "insurance_cover"
    FOOD_SUBSIDY = "food_subsidy"
    HOUSING_SUBSIDY = "housing_subsidy"
    TOOLKIT_IN_KIND = "toolkit_in_kind"
    PENSION = "pension"


class BenefitBasis(StrEnum):
    FIXED = "fixed"
    ACTUAL_UPTO_CAP = "actual_upto_cap"
    VARIABLE = "variable"


class Frequency(StrEnum):
    ONE_TIME = "one_time"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"
    PER_ACADEMIC_YEAR = "per_academic_year"
    PER_CYCLE = "per_cycle"


class DisbursementMode(StrEnum):
    DBT = "dbt"
    INSTITUTION_PAYABLE = "institution_payable"
    REIMBURSEMENT = "reimbursement"
    IN_KIND = "in_kind"


class IncomeCeilingScope(StrEnum):
    INDIVIDUAL = "individual"
    HOUSEHOLD = "household"
    FAMILY = "family"
    BENEFICIARY = "beneficiary"


class Occupation(StrEnum):
    """ProfileContext.occupation."""

    FARMER = "farmer"
    TENANT_FARMER = "tenant_farmer"
    CULTIVATING_FARMER = "cultivating_farmer"
    SALARIED = "salaried"
    SELF_EMPLOYED = "self_employed"
    DAILY_WAGE_LABOURER = "daily_wage_labourer"
    STUDENT = "student"
    HOMEMAKER = "homemaker"
    UNEMPLOYED = "unemployed"
    RETIRED = "retired"
    OTHER = "other"


class AlternativePolicy(StrEnum):
    PROVISIONAL = "provisional"
    FULL = "full"


class DiscoveredBy(StrEnum):
    TINYFISH_SEARCH = "tinyfish_search"
    MANUAL = "manual"
    SITEMAP = "sitemap"
    RSS = "rss"


class FetchTier(StrEnum):
    TINYFISH_FETCH = "tinyfish_fetch"
    SITEMAP = "sitemap"
    RSS = "rss"
    DIRECT = "direct"


class DomainSubject(StrEnum):
    LANDHOLDING_HECTARES = "landholding_hectares"
    FAMILY_SIZE = "family_size"
    YEARS_OF_RESIDENCE = "years_of_residence"
    DISABILITY_PERCENTAGE = "disability_percentage"
    EMPLOYMENT_TENURE_YEARS = "employment_tenure_years"


class DomainOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    LTE = "lte"
    GTE = "gte"
    LT = "lt"
    GT = "gt"


class DomainUnit(StrEnum):
    HECTARE = "hectare"
    PERSON = "person"
    YEAR = "year"
    PERCENT = "percent"


class OnMissingProfileFact(StrEnum):
    ASSUME_UNRESTRICTED_FLAG = "assume_unrestricted_flag"
    ROUTE_MANUAL_REVIEW = "route_manual_review"


class SoftClauseMode(StrEnum):
    JUDGE_ONLY = "judge_only"
    JUDGE_WITH_FACTS = "judge_with_facts"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
class _Object(BaseModel):
    """Base for every object in DATA_SPEC 2.3 / 3.1.

    JSON Schema models *absence* as a missing key, never as ``null``.  Pydantic
    necessarily materialises an omitted optional field as its default (usually
    ``None``), so a naive ``model_dump()`` would turn ``{"social_category":
    ["OBC"]}`` into an object carrying four extra ``null`` keys.  That is not
    merely cosmetic: BUILD_ORDER 4.4 makes re-seeding a no-op when
    ``content_hash`` is unchanged, so the dump of a validated payload has to be
    byte-identical to the payload that was read.

    Dropping keys that were never supplied restores exact round-trip
    fidelity while still emitting an explicitly-passed ``None``.
    """

    @model_serializer(mode="wrap")
    def _omit_unset_keys(self, handler):
        data = handler(self)
        if isinstance(data, dict):
            for key in [k for k in data if k not in self.model_fields_set]:
                data.pop(key)
        return data


def _reject_explicit_null(model: BaseModel, *names: str) -> None:
    """JSON Schema: optional-but-non-nullable means "omit it, do not pass null"."""
    for name in names:
        if name in model.model_fields_set and getattr(model, name) is None:
            raise ValueError(f"{name} may be omitted but must not be null (not nullable in schema)")


def _require_non_empty_when(value: object, model_name: str, field: str) -> None:
    """JSON Schema ``minProperties: 1`` for an object whose keys are optional."""
    if not value:
        raise ValueError(f"{model_name}.{field} must populate at least one key (minProperties: 1)")


def _require_unique(seq: list[object], field: str) -> None:
    """JSON Schema ``uniqueItems: true``."""
    if len(set(seq)) != len(seq):
        seen: set[object] = set()
        dupes = []
        for item in seq:
            key = item if isinstance(item, (str, int, float, bool)) else str(item)
            if key in seen and key not in dupes:
                dupes.append(key)
            seen.add(key)
        raise ValueError(f"{field} must contain unique items; duplicates: {dupes}")


def _require_absolute_uri(value: str) -> str:
    """JSON Schema ``format: "uri"`` (absolute URI with a scheme)."""
    parts = urlsplit(value)
    if not parts.scheme or not value.startswith(f"{parts.scheme}://"):
        raise ValueError(f"not an absolute URI: {value!r}")
    return value


# --------------------------------------------------------------------------
# Nested models
# --------------------------------------------------------------------------
class IntakeWindow(_Object):
    model_config = ConfigDict(extra="forbid")

    opens_on: date | None
    closes_on: date | None
    cycle_status: CycleStatus
    source_note: Annotated[str, StringConstraints(min_length=1, max_length=500)]


class Benefit(_Object):
    model_config = ConfigDict(extra="forbid")

    benefit_id: Annotated[str, StringConstraints(pattern=_BENEFIT_ID_PATTERN)]
    benefit_type: BenefitType
    description: Annotated[str, StringConstraints(min_length=10, max_length=400)]
    basis: BenefitBasis
    amount_inr: StrictInt | None = Field(..., ge=0)
    cap_amount_inr: StrictInt | None = Field(..., ge=0)
    annual_value_inr: StrictInt | None = Field(..., ge=0)
    frequency: Frequency
    disbursement_mode: DisbursementMode
    installment_count: StrictInt | None = Field(..., ge=1, le=24)
    installment_amount_inr: StrictInt | None = Field(..., ge=0)
    eligibility_note: Annotated[str, StringConstraints(min_length=1, max_length=400)]


class DocAlternative(_Object):
    model_config = ConfigDict(extra="forbid")

    document_id: DocId
    policy: AlternativePolicy
    weight: AlternativeWeight


class RequiredDocument(_Object):
    model_config = ConfigDict(extra="forbid")

    document_id: DocId
    name: Annotated[str, StringConstraints(min_length=2, max_length=160)]
    tier: DocumentTier
    penalty_per_document: StrictFloat = Field(..., ge=0.0, le=0.35)
    requirement_weight: RequirementWeight
    is_mandatory: StrictBool
    issuance_authority: Annotated[str, StringConstraints(min_length=2, max_length=200)]
    applies_when: str | None = Field(..., max_length=200)
    alternatives: list[DocAlternative]
    guide_url: GovernedUri | None
    notes: Annotated[str, StringConstraints(max_length=400)]


class GateOverrideWhen(_Object):
    model_config = ConfigDict(extra="forbid")

    social_category: list[SocialCategory] | None = Field(default=None, min_length=1)
    gender: list[Gender] | None = Field(default=None, min_length=1)
    domicile_state: list[SchemeStateCode] | None = Field(default=None, min_length=1)
    education_level: list[EducationLevel] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _min_properties(self) -> "GateOverrideWhen":
        # All four keys are optional arrays (omit allowed) but none are
        # nullable ({"type": "array"}), so an explicit null must be refused.
        _reject_explicit_null(
            self, "social_category", "gender", "domicile_state", "education_level"
        )
        # JSON minProperties counts *present* keys (a null value still counts),
        # so consult model_fields_set rather than the resolved values.
        _require_non_empty_when(
            set(self.model_fields_set), "GateOverrideWhen", "when"
        )
        return self


class GateOverrideSet(_Object):
    model_config = ConfigDict(extra="forbid")

    income_ceiling_annual: StrictInt | None = Field(default=None, ge=0)
    min_age: StrictInt | None = Field(default=None, ge=0, le=120)
    max_age: StrictInt | None = Field(default=None, ge=0, le=120)
    allowed_categories: list[SocialCategory] | None = Field(default=None, min_length=1)
    min_education_level: EducationLevel | None = None
    allowed_occupations: list[str] | None = None

    @model_validator(mode="after")
    def _min_properties(self) -> "GateOverrideSet":
        # `allowed_categories` is a plain {"type": "array"} (not nullable);
        # the scalar keys are all ["<type>", "null"] and may be null.
        _reject_explicit_null(self, "allowed_categories")
        _require_non_empty_when(
            set(self.model_fields_set), "GateOverrideSet", "set"
        )
        return self


class GateOverride(_Object):
    model_config = ConfigDict(extra="forbid")

    override_id: Annotated[str, StringConstraints(pattern=_OVERRIDE_ID_PATTERN)]
    when: GateOverrideWhen
    set: GateOverrideSet


class DomainGate(_Object):
    model_config = ConfigDict(extra="forbid")

    rule_id: Annotated[str, StringConstraints(pattern=_RULE_ID_PATTERN)]
    subject: DomainSubject
    operator: DomainOperator
    # Schema type is ["number", "string"]; int stays int so JSON round-trips.
    value: StrictInt | StrictFloat | str
    unit: DomainUnit
    description: Annotated[str, StringConstraints(min_length=10, max_length=400)]
    on_missing_profile_fact: OnMissingProfileFact


class SoftClause(_Object):
    model_config = ConfigDict(extra="forbid")

    clause_id: Annotated[str, StringConstraints(pattern=_CLAUSE_ID_PATTERN)]
    text: Annotated[str, StringConstraints(min_length=10, max_length=600)]
    source: str | None = Field(..., max_length=300)
    mode: SoftClauseMode


class SourceRef(_Object):
    model_config = ConfigDict(extra="forbid")

    url: str
    discovered_by: DiscoveredBy
    first_seen_at: datetime
    last_crawled_at: datetime
    fetch_tier: FetchTier
    content_hash: Annotated[str, StringConstraints(pattern=_CONTENT_HASH_PATTERN)]
    parser_version: Annotated[str, StringConstraints(max_length=64)]

    @model_validator(mode="after")
    def _url_is_absolute(self) -> "SourceRef":
        self.url = _require_absolute_uri(self.url)
        return self


class ParseMeta(_Object):
    model_config = ConfigDict(extra="forbid")

    parse_confidence: StrictFloat = Field(..., ge=0.0, le=1.0)
    ocr_used: StrictBool
    repair_attempts: StrictInt = Field(..., ge=0, le=1)
    review_reasons: list[Annotated[str, StringConstraints(max_length=200)]]

    @model_validator(mode="after")
    def _unique_items(self) -> "ParseMeta":
        _require_unique(self.review_reasons, "ParseMeta.review_reasons")
        return self


# --------------------------------------------------------------------------
# ProfileContext
# --------------------------------------------------------------------------
class ProfileFacts(_Object):
    """DATA_SPEC 3.1 -- additive optional block read by ``domain_gates``.

    Every field defaults to null, which triggers Phase 1's
    ``assume_unrestricted`` rule (DATA_SPEC 0 / ARCHITECTURE 6.2.1).
    """

    model_config = ConfigDict(extra="forbid")

    landholding_hectares: StrictFloat | None = Field(default=None, ge=0)
    family_size: StrictInt | None = Field(default=None, ge=1)
    years_of_residence: StrictInt | None = Field(default=None, ge=0)
    disability_percentage: StrictFloat | None = Field(default=None, ge=0, le=100)
    employment_tenure_years: StrictInt | None = Field(default=None, ge=0)


class ProfileContext(_Object):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"]
    age: StrictInt = Field(..., ge=0, le=120)
    gender: ProfileGender
    domicile_state: StateCode
    education_level: EducationLevel
    annual_household_income: StrictInt = Field(..., ge=0, le=100_000_000)
    social_category: SocialCategory
    minority_status: StrictBool
    occupation: Occupation
    # `required` wins over `default` in JSON Schema: the default is annotation
    # only, so L1 must still fail when the key is absent (see Scheme below).
    documents_in_hand: list[DocId]
    # `facts` is the one genuinely optional block (absent from `required`).
    facts: ProfileFacts = Field(default_factory=ProfileFacts)

    @model_validator(mode="after")
    def _unique_items(self) -> "ProfileContext":
        _require_unique(self.documents_in_hand, "ProfileContext.documents_in_hand")
        return self


# --------------------------------------------------------------------------
# Scheme
# --------------------------------------------------------------------------
class Scheme(_Object):
    model_config = ConfigDict(extra="forbid")

    # -- identity -------------------------------------------------------
    schema_version: Literal["1.0.0"]
    scheme_id: Annotated[
        str, StringConstraints(pattern=_SCHEME_ID_PATTERN, min_length=8, max_length=128)
    ]
    slug: Annotated[str, StringConstraints(pattern=_SLUG_PATTERN, max_length=160)]
    name: Annotated[str, StringConstraints(min_length=3, max_length=200)]
    name_hi: str | None = Field(default=None, max_length=300)
    department: Annotated[str, StringConstraints(min_length=3, max_length=240)]
    sponsoring_body: Annotated[str, StringConstraints(min_length=3, max_length=240)]
    scheme_type: SchemeType
    category: WelfareCategory
    fiscal_year: Annotated[str, StringConstraints(pattern=_YEAR_PATTERN)]
    academic_year: Annotated[str, StringConstraints(pattern=_YEAR_PATTERN)] | None = None
    status: SchemeStatus
    is_active: StrictBool
    index_state: IndexState

    # -- hard gates (G_hard, DATA_SPEC 2.2) -----------------------------
    min_age: StrictInt | None = Field(..., ge=0, le=120)
    max_age: StrictInt | None = Field(..., ge=0, le=120)
    gender: list[Gender] = Field(..., min_length=1)
    domicile_state: list[SchemeStateCode] = Field(..., min_length=1)
    income_ceiling_annual: StrictInt | None = Field(..., ge=0, le=100_000_000)
    income_ceiling_scope: IncomeCeilingScope | None
    allowed_categories: list[SocialCategory] = Field(..., min_length=1)
    requires_minority_flag: StrictBool
    min_education_level: EducationLevel | None
    allowed_occupations: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] | None
    # `required` + `default: []` in DATA_SPEC 2.3.  JSON Schema treats `default`
    # as annotation only, so `required` wins and the key must be present --
    # which is why these are modelled as required rather than with a
    # default_factory.  If they defaulted, L1 (powered by these models per
    # BUILD_ORDER 2.2) would accept a payload that jsonschema rejects, i.e. a
    # fail-open structural check.  `alternate_urls` below IS optional and does
    # keep its default.
    conditional_gate_overrides: list[GateOverride]
    domain_gates: list[DomainGate]

    # -- semantic / benefit content ------------------------------------
    soft_clauses: list[SoftClause]
    benefits: list[Benefit] = Field(..., min_length=1)
    benefits_summary: Annotated[str, StringConstraints(min_length=20, max_length=400)]
    benefits_total_value_annual: StrictInt | None = Field(..., ge=0)
    eligibility_text: Annotated[str, StringConstraints(min_length=40, max_length=4000)]
    required_documents: list[RequiredDocument]

    # -- location & application ----------------------------------------
    portal_url: GovernedUri
    alternate_urls: list[str] = Field(default_factory=list)
    application_mode: ApplicationMode
    intake_window: IntakeWindow

    # -- live verification state ---------------------------------------
    verification_status: VerificationStatus
    verified_at: datetime | None
    extracted_deadline: date | None
    snapshot_url: str | None = Field(..., max_length=512)
    verification_job_id: str | None = Field(..., max_length=64)
    last_known_status: VerificationStatus | None
    source: SourceRef
    parse: ParseMeta

    # -- bookkeeping ----------------------------------------------------
    tags: list[Annotated[str, StringConstraints(min_length=1, max_length=48)]] = Field(
        ..., max_length=32
    )
    created_at: datetime
    updated_at: datetime

    # -- L3 cross-field invariants (DATA_SPEC 2.4) ----------------------
    @model_validator(mode="after")
    def v_cf1_age_bounds(self) -> "Scheme":
        """V-CF1: if both bounds are non-null then min_age <= max_age."""
        if self.min_age is not None and self.max_age is not None and self.min_age > self.max_age:
            raise ValueError(f"V-CF1: min_age {self.min_age} > max_age {self.max_age}")
        return self

    @model_validator(mode="after")
    def v_cf8_benefits_total(self) -> "Scheme":
        """V-CF8: when every benefit carries an annual value, the stated total must equal the sum."""
        values = [b.annual_value_inr for b in self.benefits]
        if values and all(v is not None for v in values):
            expected = sum(values)
            if self.benefits_total_value_annual != expected:
                raise ValueError(
                    "V-CF8: benefits_total_value_annual "
                    f"{self.benefits_total_value_annual!r} != sum(annual_value_inr) {expected}"
                )
        return self

    @model_validator(mode="after")
    def v_cf9_income_scope(self) -> "Scheme":
        """V-CF9 + DATA_SPEC 2.2: a ceiling and its scope are non-null together ("iff")."""
        if self.income_ceiling_annual is not None and self.income_ceiling_scope is None:
            raise ValueError(
                "V-CF9: income_ceiling_annual is set but income_ceiling_scope is null"
            )
        if self.income_ceiling_annual is None and self.income_ceiling_scope is not None:
            raise ValueError(
                "V-CF9: income_ceiling_scope is set but income_ceiling_annual is null"
            )
        return self

    @model_validator(mode="after")
    def _unique_set_semantics(self) -> "Scheme":
        """DATA_SPEC 2.3 ``uniqueItems`` on every set-semantics list."""
        _require_unique(self.gender, "Scheme.gender")
        _require_unique(self.domicile_state, "Scheme.domicile_state")
        _require_unique(self.allowed_categories, "Scheme.allowed_categories")
        if self.allowed_occupations is not None:
            _require_unique(self.allowed_occupations, "Scheme.allowed_occupations")
        _require_unique(self.tags, "Scheme.tags")
        return self

    @model_validator(mode="after")
    def _absolute_urls(self) -> "Scheme":
        """DATA_SPEC 2.3 ``format: "uri"`` -- checked without re-writing the value."""
        for i, url in enumerate(self.alternate_urls):
            self.alternate_urls[i] = _require_absolute_uri(url)
        return self
