# Words for people. Nothing here decides anything: a verdict's `message` and a
# refusal's text are not part of conformance, so no fixture can tell one
# wording from another - and the mutation run leaves this file alone for that
# reason. Code that only words a message lives here; code that decides lives
# in the part.

from __future__ import annotations

from typing import Optional, Union


def show(used: int, limit: Optional[int]) -> str:
    """An amount against its cap."""
    return f"{used} of {'no cap' if limit is None else limit}"


def describe_number(value: Union[int, float]) -> str:
    """A number as a message may hold it. Spelling out an integer past
    Python's int-to-str digit limit raises, and a verdict must not, so a long
    one is described by its size."""
    if isinstance(value, float) or value.bit_length() <= 64:
        return f"{value!r}"
    return f"a {value.bit_length()}-bit integer"
