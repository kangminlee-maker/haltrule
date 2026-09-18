# Ported from ts/checkpoint.ts. Pure policy: no I/O, no timers, no clock.
# fixtures/checkpoint/v0.json is the contract both language ports must satisfy -
# do not change behavior here without checking it still passes that fixture.
# Behavior (not names) must match the TypeScript original; Python identifiers
# follow snake_case instead of the source's camelCase.
#
# Where JavaScript and Python disagree about a value, this file follows
# JavaScript, because the spec's value model was defined by what JavaScript
# can represent: 1.0 is the integer 1, map keys sort by UTF-16 code unit (not
# code point), and "is this missing?" uses JavaScript falsiness, under which
# an empty list or dict is present.

from __future__ import annotations

import hashlib
import math
from typing import Any, Literal, Mapping, Optional, Sequence

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
    it becomes a halt value ({"halt": reason, "message": ...}). The message
    names where the value sits (`$.a[2]`) and is for people; only "halt" is
    part of the spec."""

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

    Returns {"canonical": ...}, or {"halt": reason, "message": ...} for
    anything else. Like every verdict here it is a value, never a raise: the
    caller records it before it stops.
    """
    try:
        return {"canonical": _encode_value(value, "$", 0)}
    except _DigestInputError as error:
        return {"halt": error.reason, "message": str(error)}


def checkpoint_digest(value: Any) -> dict[str, str]:
    """Return {"digest": "sha256:" + the lowercase hex SHA-256 of the canonical
    form's UTF-8 bytes} - the value `printf '%s' "$canonical" | sha256sum`
    prints - or the halt canonicalize returned."""
    result = canonicalize(value)
    if "halt" in result:
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


def _encode_number(value: int | float, at: str) -> str:
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise _DigestInputError(
            "digest_input_float",
            f"{at}: {value!r} is not an integer; render it as a string if it belongs in a digest",
        )
    if abs(value) > _MAX_SAFE_INTEGER:
        # Spelling out an integer past Python's int-to-str digit limit raises,
        # and a verdict must not, so a long one is described by its size.
        shown = (
            f"{value!r}"
            if isinstance(value, float) or value.bit_length() <= 64
            else f"a {value.bit_length()}-bit integer"
        )
        raise _DigestInputError(
            "digest_input_int_range", f"{at}: {shown} is outside ±(2^53 - 1)"
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


def evaluate_checkpoint_artifact(
    *,
    stage_id: str,
    artifact: Optional[Mapping[str, Any]],
    subject_ref: Optional[str] = None,
    expected_contract_revision: Optional[str] = None,
    expected_stage_config_digest: Optional[str] = None,
    expected_dependency_digests: Optional[Mapping[str, str]] = None,
    required_resume_from_stage: Optional[str] = None,
    validation_issues: Optional[Sequence[Mapping[str, Any]]] = None,
    status_map: Optional[Mapping[str, ArtifactStatus]] = None,
) -> list[dict[str, Any]]:
    """Decide whether a recorded artifact may be reused.

    Returns every issue found, in this order: status, contract revision,
    stage-config digest, dependency digests (by UTF-16 key order), the
    caller's validation issues. With none, returns one "valid" issue, so the
    result is never empty. `artifact` is None when it does not exist;
    `status_map` replaces the default identity map, and a status it does not
    name is not reusable.
    """
    resume = (
        required_resume_from_stage
        if required_resume_from_stage is not None
        else stage_id
    )

    def base(status: str, reason: str, **details: Any) -> dict[str, Any]:
        return {
            "stage_id": stage_id,
            "status": status,
            "reason": reason,
            "required_resume_from_stage": resume,
            "subject_ref": subject_ref,
            **details,
        }

    if artifact is None:
        return [base("missing", "artifact_missing")]

    issues: list[dict[str, Any]] = []
    status = artifact.get("status")
    if _js_falsy(status):
        issues.append(base("invalid", "artifact_status_missing"))
    elif (
        _resolve_status(
            status, status_map if status_map is not None else _IDENTITY_STATUS_MAP
        )
        != "complete"
    ):
        issues.append(
            base("invalid", "artifact_status_not_reusable", actual_status=status)
        )

    revision = artifact.get("contract_revision")
    if not _js_falsy(expected_contract_revision) and _js_falsy(revision):
        issues.append(
            base(
                "unknown_contract",
                "contract_revision_missing",
                expected_contract_revision=expected_contract_revision,
            )
        )
    elif not _js_falsy(expected_contract_revision) and not _js_strict_equal(
        revision, expected_contract_revision
    ):
        issues.append(
            base(
                "invalid",
                "contract_revision_mismatch",
                expected_contract_revision=expected_contract_revision,
                actual_contract_revision=revision,
            )
        )

    config = artifact.get("stage_config_digest")
    if not _js_falsy(expected_stage_config_digest) and not _js_strict_equal(
        config, expected_stage_config_digest
    ):
        issues.append(
            base(
                "invalid",
                "stage_config_digest_mismatch",
                expected_stage_config_digest=expected_stage_config_digest,
                actual_stage_config_digest=config,
            )
        )

    expected_dependencies = (
        expected_dependency_digests if expected_dependency_digests is not None else {}
    )
    recorded = artifact.get("dependency_digests")
    for dependency_id in sorted(expected_dependencies, key=_utf16_key):
        expected_digest = expected_dependencies[dependency_id]
        actual_digest = (
            recorded.get(dependency_id) if isinstance(recorded, dict) else None
        )
        if not _js_strict_equal(actual_digest, expected_digest):
            issues.append(
                base(
                    "invalid",
                    "dependency_digest_mismatch",
                    dependency_id=dependency_id,
                    expected_digest=expected_digest,
                    actual_digest=actual_digest,
                )
            )

    # Any sequence counts, as the parameter's type says: silently ignoring a
    # caller's validation issues because they arrived in an unexpected sequence
    # type would reuse an artifact the caller had already found invalid. Text
    # is a sequence of characters, never of issues, and is ignored as a
    # non-array is in TypeScript.
    for validation_issue in (
        validation_issues
        if isinstance(validation_issues, Sequence)
        and not isinstance(validation_issues, (str, bytes, bytearray))
        else ()
    ):
        reason = validation_issue.get("reason")
        issue_resume = validation_issue.get("required_resume_from_stage")
        issues.append(
            {
                **base("invalid", reason if reason is not None else "validation_issue"),
                "required_resume_from_stage": issue_resume
                if issue_resume is not None
                else resume,
                **validation_issue,
            }
        )

    if not issues:
        return [
            {**base("valid", "checkpoint_valid"), "required_resume_from_stage": None}
        ]
    return issues


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
    if value is None or value is False:
        return True
    if value is True:
        return False
    if isinstance(value, (int, float)):
        return value == 0 or value != value
    if isinstance(value, str):
        return value == ""
    return False


def _js_strict_equal(left: Any, right: Any) -> bool:
    """JavaScript `===` over JSON values.

    Scalars compare by kind and value (numbers by value, so 1 and 1.0 agree,
    and NaN equals nothing); a list or dict equals only itself, because
    JavaScript compares objects by identity.
    """
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return left is right and isinstance(left, (list, dict))
