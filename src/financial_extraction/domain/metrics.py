"""Explicit numeric policies and exact Decimal parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class NumericParseError(ValueError):
    """A lexical value cannot be interpreted under the supplied policy."""


@dataclass(frozen=True, slots=True)
class NumericPolicy:
    policy_id: str
    decimal_separator: str
    group_separator: str | None
    allow_parentheses_negative: bool = False
    allow_leading_sign: bool = True
    allow_trailing_sign: bool = False
    allowed_prefixes: tuple[str, ...] = ()
    allowed_suffixes: tuple[str, ...] = ()
    max_lexical_chars: int = 4_096

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("numeric policy id must be a non-empty string")
        if any(
            not isinstance(flag, bool)
            for flag in (
                self.allow_parentheses_negative,
                self.allow_leading_sign,
                self.allow_trailing_sign,
            )
        ):
            raise ValueError("numeric policy sign options must be booleans")
        if (
            not isinstance(self.max_lexical_chars, int)
            or isinstance(self.max_lexical_chars, bool)
            or self.max_lexical_chars <= 0
        ):
            raise ValueError("max_lexical_chars must be a positive integer")
        if not isinstance(self.allowed_prefixes, tuple) or not isinstance(
            self.allowed_suffixes, tuple
        ):
            raise ValueError("allowed prefixes and suffixes must be tuples")
        if any(
            not isinstance(token, str) or not token
            for token in (*self.allowed_prefixes, *self.allowed_suffixes)
        ):
            raise ValueError("allowed prefixes and suffixes must be non-empty strings")
        if not isinstance(self.decimal_separator, str) or len(self.decimal_separator) != 1:
            raise ValueError("decimal separator must be one character")
        if self.group_separator is not None:
            if not isinstance(self.group_separator, str) or len(self.group_separator) != 1:
                raise ValueError("group separator must be one character or None")
            if self.group_separator == self.decimal_separator:
                raise ValueError("decimal and group separators must differ")


def parse_numeric(value: str, policy: NumericPolicy) -> Decimal:
    """Parse one explicit numeric lexical form without guessing locale or currency.

    Prefixes/suffixes (including a currency symbol or percent sign) are accepted
    only when listed on the policy. The returned Decimal is the displayed amount;
    scale conversion is intentionally performed separately by the caller.
    """
    if not isinstance(value, str):
        raise NumericParseError("numeric lexical value must be a string")
    if len(value) > policy.max_lexical_chars:
        raise NumericParseError("numeric lexical value exceeds max_lexical_chars")
    lexical = value.strip()
    if not lexical or lexical.casefold() in {"-", "–", "—", "n/a", "na", "null"}:
        raise NumericParseError("empty, dash and N/A values are unresolved, not zero")

    prefix = next((part for part in policy.allowed_prefixes if lexical.startswith(part)), None)
    if prefix is not None:
        lexical = lexical[len(prefix) :].strip()
    suffix = next((part for part in policy.allowed_suffixes if lexical.endswith(part)), None)
    if suffix is not None:
        lexical = lexical[: -len(suffix)].strip()
    if any(lexical.startswith(part) for part in policy.allowed_prefixes if part != prefix):
        raise NumericParseError("multiple or unsupported prefixes")
    if any(lexical.endswith(part) for part in policy.allowed_suffixes if part != suffix):
        raise NumericParseError("multiple or unsupported suffixes")

    negative_parentheses = lexical.startswith("(") and lexical.endswith(")")
    if "(" in lexical or ")" in lexical:
        if not negative_parentheses or not policy.allow_parentheses_negative:
            raise NumericParseError("parentheses are not allowed by this numeric policy")
        lexical = lexical[1:-1].strip()

    sign = ""
    if lexical[:1] in {"+", "-"}:
        if not policy.allow_leading_sign:
            raise NumericParseError("leading sign is not allowed")
        sign, lexical = lexical[0], lexical[1:]
    if lexical[-1:] in {"+", "-"}:
        if not policy.allow_trailing_sign or sign:
            raise NumericParseError("trailing sign is not allowed")
        sign, lexical = lexical[-1], lexical[:-1]
    if negative_parentheses and sign:
        raise NumericParseError("parentheses and an explicit sign cannot be combined")
    if not lexical:
        raise NumericParseError("numeric lexical value has no digits")

    decimal = re.escape(policy.decimal_separator)
    group = re.escape(policy.group_separator) if policy.group_separator else None
    if group:
        integer = rf"(?:\d+|\d{{1,3}}(?:{group}\d{{3}})+)"
    else:
        integer = r"\d+"
    pattern = rf"{integer}(?:{decimal}\d+)?"
    if not re.fullmatch(pattern, lexical):
        raise NumericParseError("value does not match the explicit numeric separators")
    normalized = lexical.replace(policy.group_separator, "") if policy.group_separator else lexical
    normalized = normalized.replace(policy.decimal_separator, ".")
    if negative_parentheses or sign == "-":
        normalized = "-" + normalized
    try:
        result = Decimal(normalized)
    except InvalidOperation as exc:
        raise NumericParseError("invalid Decimal value") from exc
    if not result.is_finite():
        raise NumericParseError("non-finite Decimal values are not allowed")
    return result
