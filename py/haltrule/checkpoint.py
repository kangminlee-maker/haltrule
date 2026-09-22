# Ported from ts/checkpoint.ts. Pure policy: no I/O, no timers, no clock.
# fixtures/checkpoint/v0.json is the contract both language ports must satisfy -
# do not change behavior here without checking it still passes that fixture.
# Behavior (not names) must match the TypeScript original; Python identifiers
# follow snake_case instead of the source's camelCase.
#
# Where JavaScript and Python disagree about a value, this file follows
# JavaScript, because the spec's value model was defined by what JavaScript
# can represent: 1.0 is the integer 1, map keys sort by UTF-16 code unit (not
# code point), and "is this missing?" - asked of what an artifact records -
# uses JavaScript falsiness, under which an empty list or dict is present.
#
# The arguments are typed: an argument of another type is refused (TypeError),
# and None or not given is the one way to be absent. A key the signature does
# not name is refused by the call itself.

from __future__ import annotations

import hashlib
import math
from typing import Any, Literal, Mapping, Optional, Union

from haltrule.messages import describe_number
from haltrule.verdict import VerdictLevel, is_verdict_level, verdict

# --------------------------------------------------------------- canonicalize

# Largest integer every implementation represents exactly: 2^53 - 1.
_MAX_SAFE_INTEGER = 9007199254740991

# Deepest nesting of lists and maps a digest input may have. A bound every
# language can reach without exhausting its stack, so all of them halt at the
# same depth instead of each crashing at its own.
_MAX_DEPTH = 100

DigestInputReason = Literal[
    "digest_input_float",
    "digest_input_int_range",
    "digest_input_unsupported",
]


class _DigestInputError(ValueError):
    """Internal: unwinds the recursive encoder to the public boundary, where
    it becomes a halt verdict. Its message names where the value sits
    (`$.a[2]`), a path being no thing two languages spell alike."""

    def __init__(self, reason: DigestInputReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# The seven characters with a two-character escape. Every other code point
# below U+0020 is written \u00xx (lowercase hex); everything else, "/",
# U+007F, U+2028 and U+2029 included, is written as itself - the same output
# as the TypeScript port's table and as json.dumps(..., ensure_ascii=False).
_SHORT_ESCAPES = {
    0x08: "\\b",
    0x09: "\\t",
    0x0A: "\\n",
    0x0C: "\\f",
    0x0D: "\\r",
    0x22: '\\"',
    0x5C: "\\\\",
}


def canonicalize(value: Any) -> dict[str, str]:
    """Canonical form of a value: an RFC 8785 subset.

    Map keys are sorted by UTF-16 code unit, there is no insignificant
    whitespace, integers are written in decimal, and strings use the escape
    table above. Strings are not Unicode-normalized: NFC and NFD spellings of
    the same text canonicalize differently.

    Accepted: None, booleans, integral numbers with |n| <= 2^53 - 1 (so 1.0 is
    the integer 1, as it is in JavaScript), strings of Unicode scalar values,
    lists, and dicts with str keys, nested at most 100 deep.

    Returns {"canonical": ...}, or a halt verdict for anything else. Like
    every verdict here it is a value, never a raise: the caller records it
    before it stops.
    """
    try:
        return {"canonical": _encode_value(value, "$", 0)}
    except _DigestInputError as error:
        return verdict("halt", error.reason, str(error))


def checkpoint_digest(value: Any) -> dict[str, str]:
    """Return {"digest": "sha256:" + the lowercase hex SHA-256 of the canonical
    form's UTF-8 bytes} - the value `printf '%s' "$canonical" | sha256sum`
    prints - or the halt canonicalize returned."""
    result = canonicalize(value)
    if "verdict" in result:
        return result
    return {
        "digest": "sha256:"
        + hashlib.sha256(result["canonical"].encode("utf-8")).hexdigest()
    }


def _utf16_key(key: str) -> bytes:
    """Sort key ordering strings by UTF-16 code unit, as JavaScript does.

    UTF-16-BE bytes compare exactly as code units do; surrogatepass lets an
    unpaired surrogate through to _encode_string, which rejects it.
    """
    return key.encode("utf-16-be", "surrogatepass")


def _encode_value(value: Any, at: str, depth: int) -> str:
    if value is None:
        return "null"
    # bool before int: bool is an int subclass, and True must not become 1.
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _encode_number(value, at)
    if isinstance(value, str):
        return _encode_string(value, at)
    if isinstance(value, (list, dict)) and depth >= _MAX_DEPTH:
        raise _DigestInputError(
            "digest_input_unsupported",
            f"{at}: nested deeper than {_MAX_DEPTH} lists and maps",
        )
    if isinstance(value, list):
        return (
            "["
            + ",".join(
                _encode_value(item, f"{at}[{index}]", depth + 1)
                for index, item in enumerate(value)
            )
            + "]"
        )
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise _DigestInputError(
                    "digest_input_unsupported", f"{at}: map key {key!r} is not a string"
                )
        keys = sorted(value, key=_utf16_key)
        return (
            "{"
            + ",".join(
                _encode_string(key, f"{at} key")
                + ":"
                + _encode_value(value[key], f"{at}.{key}", depth + 1)
                for key in keys
            )
            + "}"
        )
    raise _DigestInputError(
        "digest_input_unsupported",
        f"{at}: {type(value).__name__} is outside the digest value model",
    )


def _encode_number(value: Union[int, float], at: str) -> str:
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise _DigestInputError(
            "digest_input_float",
            f"{at}: {value!r} is not an integer; render it as a string if it belongs in a digest",
        )
    if abs(value) > _MAX_SAFE_INTEGER:
        raise _DigestInputError(
            "digest_input_int_range",
            f"{at}: {describe_number(value)} is outside ±(2^53 - 1)",
        )
    # int() also writes a negative zero float as "0".
    return str(int(value))


def _encode_string(value: str, at: str) -> str:
    out = ['"']
    for index, char in enumerate(value):
        point = ord(char)
        if 0xD800 <= point <= 0xDFFF:
            raise _DigestInputError(
                "digest_input_unsupported",
                f"{at}: string has an unpaired surrogate at index {index}",
            )
        escaped = _SHORT_ESCAPES.get(point)
        if escaped is not None:
            out.append(escaped)
        elif point < 0x20:
            out.append("\\u%04x" % point)
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


# -------------------------------------------------------------- reuse verdict

# The status vocabulary every artifact is read through. Only "complete" is
# reusable.
ArtifactStatus = Literal["complete", "partial", "failed", "blocked"]

# Used when the caller passes no status_map: the vocabulary maps to itself.
_IDENTITY_STATUS_MAP: Mapping[str, ArtifactStatus] = {
    "complete": "complete",
    "partial": "partial",
    "failed": "failed",
    "blocked": "blocked",
}


def _text(value: Any, what: str) -> Optional[str]:
    """An optional string argument: None when absent, refused when not text."""
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"{what} must be a string or null, got {type(value).__name__}")


def _is_map_of(value: Any, allowed: Any) -> bool:
    return isinstance(value, dict) and all(allowed(item) for item in value.values())


def evaluate_checkpoint_artifact(
    *,
    stage_id: str,
    artifact: Optional[Mapping[str, Any]] = None,
    subject_ref: Optional[str] = None,
    expected_contract_revision: Optional[str] = None,
    expected_stage_config_digest: Optional[str] = None,
    expected_dependency_digests: Optional[Mapping[str, str]] = None,
    required_resume_from_stage: Optional[str] = None,
    validation_issues: Optional[list[Mapping[str, Any]]] = None,
    status_map: Optional[Mapping[str, ArtifactStatus]] = None,
) -> list[dict[str, Any]]:
    """Decide whether a recorded artifact may be reused.

    Returns every issue found, in this order: status, contract revision,
    stage-config digest, dependency digests (by UTF-16 key order), the
    caller's validation issues. With none, returns one "valid" issue, so the
    result is never empty. `artifact` is None when it does not exist;
    `status_map` replaces the default identity map, and a status it does not
    name is not reusable. An expectation that is None is not checked; any
    string is compared, the empty one included.
    """
    if not isinstance(stage_id, str):
        raise TypeError(f"stage_id must be a string, got {type(stage_id).__name__}")
    subject_ref = _text(subject_ref, "subject_ref")
    resume = _text(required_resume_from_stage, "required_resume_from_stage")
    if resume is None:
        resume = stage_id
    expected_contract_revision = _text(
        expected_contract_revision, "expected_contract_revision"
    )
    expected_stage_config_digest = _text(
        expected_stage_config_digest, "expected_stage_config_digest"
    )
    if artifact is not None and not isinstance(artifact, dict):
        raise TypeError("artifact must be a map or null")
    if expected_dependency_digests is None:
        expected_dependency_digests = {}
    elif not _is_map_of(expected_dependency_digests, lambda v: isinstance(v, str)):
        raise TypeError("expected_dependency_digests must map ids to strings")
    if status_map is None:
        status_map = _IDENTITY_STATUS_MAP
    elif not _is_map_of(
        status_map, lambda v: isinstance(v, str) and v in _IDENTITY_STATUS_MAP
    ):
        raise TypeError("status_map must map statuses to the vocabulary")
    if validation_issues is None:
        validation_issues = []
    elif not isinstance(validation_issues, list) or not all(
        isinstance(issue, dict) for issue in validation_issues
    ):
        raise TypeError("validation_issues must be a list of maps")

    def base(
        level: VerdictLevel, reason: str, message: str, **details: Any
    ) -> dict[str, Any]:
        return {
            **verdict(level, reason, f"{stage_id}: {message}", resume),
            "stage_id": stage_id,
            "subject_ref": subject_ref,
            **details,
        }

    # A caller's issue is an argument like the rest: what is outside the contract is refused before
    # anything is judged, an absent artifact included. A null field is an absent one.
    for validation_issue in validation_issues:
        for key, field in validation_issue.items():
            if field is not None:
                _holds_what_a_verdict_promises(key, field)

    if artifact is None:
        return [base("halt", "artifact_missing", "nothing was recorded")]

    issues: list[dict[str, Any]] = []
    status = artifact.get("status")
    if _js_falsy(status):
        issues.append(
            base("halt", "artifact_status_missing", "what was recorded has no status")
        )
    elif _resolve_status(status, status_map) != "complete":
        issues.append(
            base(
                "halt",
                "artifact_status_not_reusable",
                "the recorded status is not one that may be reused",
                actual_status=status,
            )
        )

    revision = artifact.get("contract_revision")
    if expected_contract_revision is not None and _js_falsy(revision):
        issues.append(
            base(
                "halt",
                "contract_revision_missing",
                f"a contract revision of {expected_contract_revision!r} is expected"
                " and none is recorded",
                expected_contract_revision=expected_contract_revision,
            )
        )
    elif expected_contract_revision is not None and not _same_text(
        revision, expected_contract_revision
    ):
        issues.append(
            base(
                "halt",
                "contract_revision_mismatch",
                "the recorded contract revision is not the expected"
                f" {expected_contract_revision!r}",
                expected_contract_revision=expected_contract_revision,
                actual_contract_revision=revision,
            )
        )

    config = artifact.get("stage_config_digest")
    if expected_stage_config_digest is not None and not _same_text(
        config, expected_stage_config_digest
    ):
        issues.append(
            base(
                "halt",
                "stage_config_digest_mismatch",
                "the recorded stage config digest is not the expected"
                f" {expected_stage_config_digest!r}",
                expected_stage_config_digest=expected_stage_config_digest,
                actual_stage_config_digest=config,
            )
        )

    expected_dependencies = expected_dependency_digests
    recorded = artifact.get("dependency_digests")
    for dependency_id in sorted(expected_dependencies, key=_utf16_key):
        expected_digest = expected_dependencies[dependency_id]
        actual_digest = (
            recorded.get(dependency_id) if isinstance(recorded, dict) else None
        )
        if not _same_text(actual_digest, expected_digest):
            issues.append(
                base(
                    "halt",
                    "dependency_digest_mismatch",
                    f"the dependency {dependency_id!r} moved",
                    dependency_id=dependency_id,
                    expected_digest=expected_digest,
                    actual_digest=actual_digest,
                )
            )

    for validation_issue in validation_issues:
        given = {
            key: field for key, field in validation_issue.items() if field is not None
        }
        issues.append(
            {
                **base(
                    "halt",
                    "validation_issue",
                    "the caller's own validation found something",
                ),
                **given,
            }
        )

    if not issues:
        issues.append(base("ok", "checkpoint_valid", "the artifact may be reused"))
    # An ok verdict has no resume, whoever set it.
    for issue in issues:
        if issue["verdict"] == "ok":
            issue["resume"] = None
    return issues


_TEXT_FIELDS = ("reason", "message", "resume", "stage_id", "subject_ref")


def _holds_what_a_verdict_promises(key: str, field: Any) -> None:
    """Refuse a caller's field that would leave the issue outside the shape it
    is laid over. Everything the verdict does not name is the caller's own and
    is not judged."""
    if key == "spec":
        # A caller may disagree with a verdict, not sign one.
        raise TypeError(
            "a validation issue carries a spec, which only the library says"
        )
    if key == "verdict" and not is_verdict_level(field):
        raise TypeError("a validation issue's verdict is not one of the three")
    if key in _TEXT_FIELDS and not isinstance(field, str):
        raise TypeError(f"a validation issue's {key} is not text")


def _resolve_status(
    status: Any, status_map: Mapping[str, ArtifactStatus]
) -> Optional[str]:
    if not isinstance(status, str):
        return None
    return status_map.get(status)


def _js_falsy(value: Any) -> bool:
    """JavaScript falsiness over JSON values: null, false, 0, NaN and "".

    An empty list or dict is truthy in JavaScript, unlike Python.
    """
    if isinstance(value, (int, float)):  # a bool is an int: False is 0
        return value == 0 or value != value
    return value is None or value == ""


def _same_text(recorded: Any, expected: str) -> bool:
    """What an artifact records equals an expectation only when it is the same
    string: a recorded 1 is not "1", and a recorded ["v2"] is not "v2"."""
    return isinstance(recorded, str) and recorded == expected
