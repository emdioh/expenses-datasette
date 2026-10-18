"""Load and validate the user's rules file (see DESIGN.md section 6)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TRIP_PATTERN = r"^[A-Z][A-Za-z]+(\d{2}|\d{4})$"
KINDS = ("expense", "income", "transfer")
REGEX_PREFIX = "regex:"

MATCH_FIELDS = ("payee", "comment", "category", "account", "tag")
SET_KEYS = (
    "category", "payee", "comment", "kind", "one_off", "outlier", "add_tags", "remove_tags",
)
PAYEE_RULE_SET_KEYS = ("category", "payee", "add_tags")
OVERRIDE_SET_KEYS = SET_KEYS


class RulesError(ValueError):
    """Invalid rules file; the message names the offending path."""


def split_category_path(path: str) -> tuple[str, str | None]:
    """Split "Top > Sub > Deeper" into ("Top", "Sub > Deeper")."""
    parts = [p.strip() for p in path.split(">")]
    top = parts[0]
    rest = [p for p in parts[1:] if p]
    return top, (" > ".join(rest) if rest else None)


# --- matching -------------------------------------------------------------

@dataclass(frozen=True)
class _Pattern:
    """A case-insensitive exact string or a `regex:` pattern."""

    text: str
    regex: re.Pattern[str] | None = None

    @classmethod
    def parse(cls, value: str, path: str) -> _Pattern:
        if value.startswith(REGEX_PREFIX):
            try:
                return cls(value, re.compile(value[len(REGEX_PREFIX):], re.IGNORECASE))
            except re.error as exc:
                raise RulesError(f"{path}: invalid regex ({exc.msg})") from None
        return cls(value)

    def test(self, candidate: str | None) -> bool:
        if candidate is None:
            return False
        if self.regex is not None:
            return self.regex.search(candidate) is not None
        return candidate.casefold() == self.text.casefold()


@dataclass(frozen=True)
class Match:
    """Conjunction of field conditions; at least one must be given."""

    payee: str | None = None
    comment: str | None = None
    category: str | None = None
    account: str | None = None
    tag: str | None = None
    _patterns: Mapping[str, _Pattern] = field(
        default_factory=dict, repr=False, compare=False, hash=False
    )

    def __post_init__(self) -> None:
        patterns = {
            name: _Pattern.parse(getattr(self, name), name)
            for name in MATCH_FIELDS
            if getattr(self, name) is not None
        }
        if not patterns:
            raise RulesError("match: at least one field is required")
        object.__setattr__(self, "_patterns", patterns)

    def matches(self, row: Mapping[str, Any]) -> bool:
        for name, pattern in self._patterns.items():
            if name == "category":
                ok = pattern.test(row.get("category_path")) or (
                    pattern.regex is None and pattern.test(row.get("category_top"))
                )
            elif name == "tag":
                ok = any(pattern.test(t) for t in row.get("tags") or ())
            else:
                ok = pattern.test(row.get(name))
            if not ok:
                return False
        return True


# --- actions --------------------------------------------------------------

@dataclass(frozen=True)
class Actions:
    """The `set:` part of a rule or override."""

    category: str | None = None
    payee: str | None = None
    comment: str | None = None
    kind: str | None = None
    one_off: bool | None = None
    outlier: bool | None = None
    add_tags: tuple[str, ...] = ()
    remove_tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class PayeeRule:
    match: Match
    set: Actions


@dataclass(frozen=True)
class TransferRule:
    match: Match


@dataclass(frozen=True)
class Override:
    uuid: str
    set: Actions


@dataclass(frozen=True)
class Dimensions:
    work_tags: tuple[str, ...] = ()
    country_tag: str | None = None
    country_name: str | None = None
    trip_tag_pattern: re.Pattern[str] = re.compile(DEFAULT_TRIP_PATTERN)
    extra_trip_tags: tuple[str, ...] = ()

    def trip_for(self, tags: Iterable[str]) -> str | None:
        """First trip tag (sorted for determinism), or None."""
        extra = {t.casefold() for t in self.extra_trip_tags}
        for tag in sorted(tags):
            if tag.casefold() in extra or self.trip_tag_pattern.search(tag):
                return tag
        return None

    def is_work(self, tags: Iterable[str]) -> bool:
        return bool({t.casefold() for t in self.work_tags} & {t.casefold() for t in tags})

    def in_country(self, tags: Iterable[str]) -> bool:
        return self.country_tag is not None and self.country_tag.casefold() in {
            t.casefold() for t in tags
        }


@dataclass(frozen=True)
class OneOff:
    uncategorized_threshold_eur: float = 10000
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class Outliers:
    method: str = "mad"
    threshold: float = 3.5
    min_samples: int = 8


@dataclass(frozen=True)
class Rules:
    dimensions: Dimensions = field(default_factory=Dimensions)
    payee_rules: tuple[PayeeRule, ...] = ()
    transfer_rules: tuple[TransferRule, ...] = ()
    account_aliases: Mapping[str, str] = field(default_factory=dict)
    one_off: OneOff = field(default_factory=OneOff)
    outliers: Outliers = field(default_factory=Outliers)
    overrides: tuple[Override, ...] = ()


# --- parsing helpers --------------------------------------------------------

def _type_name(value: Any) -> str:
    return type(value).__name__


def _mapping(value: Any, path: str, allowed: Sequence[str]) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RulesError(f"{path or 'rules'}: expected a mapping, got {_type_name(value)}")
    for key in value:
        if not isinstance(key, str) or key not in allowed:
            label = key if isinstance(key, str) and len(key) <= 40 else "<key>"
            raise RulesError(f"{path}.{label}: unknown key" if path else f"{label}: unknown key")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RulesError(f"{path}: expected a list, got {_type_name(value)}")
    return value


def _str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise RulesError(f"{path}: expected a string, got {_type_name(value)}")
    return value


def _opt_str(value: Any, path: str) -> str | None:
    return None if value is None else _str(value, path)


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise RulesError(f"{path}: expected a boolean, got {_type_name(value)}")
    return value


def _number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RulesError(f"{path}: expected a number, got {_type_name(value)}")
    return value


def _str_list(value: Any, path: str) -> tuple[str, ...]:
    return tuple(_str(item, f"{path}[{i}]") for i, item in enumerate(_list(value, path)))


def _parse_match(value: Any, path: str) -> Match:
    if value is None or not isinstance(value, Mapping):
        raise RulesError(f"{path}: expected a mapping, got {_type_name(value)}")
    data = _mapping(value, path, MATCH_FIELDS)
    kwargs = {k: _str(v, f"{path}.{k}") for k, v in data.items()}
    if not kwargs:
        raise RulesError(f"{path}: at least one field is required")
    for name, text in kwargs.items():
        _Pattern.parse(text, f"{path}.{name}")
    return Match(**kwargs)


def _parse_actions(value: Any, path: str, allowed: Sequence[str]) -> Actions:
    if value is None or not isinstance(value, Mapping):
        raise RulesError(f"{path}: expected a mapping, got {_type_name(value)}")
    data = _mapping(value, path, allowed)
    if not data:
        raise RulesError(f"{path}: at least one action is required")
    kwargs: dict[str, Any] = {}
    for key, raw in data.items():
        p = f"{path}.{key}"
        if key in ("category", "payee", "comment"):
            kwargs[key] = _str(raw, p)
        elif key == "kind":
            kind = _str(raw, p)
            if kind not in KINDS:
                raise RulesError(f"{p}: must be one of {', '.join(KINDS)}")
            kwargs[key] = kind
        elif key in ("one_off", "outlier"):
            kwargs[key] = _bool(raw, p)
        else:  # add_tags / remove_tags
            kwargs[key] = _str_list(raw, p)
    return Actions(**kwargs)


def _parse_dimensions(value: Any) -> Dimensions:
    data = _mapping(
        value,
        "dimensions",
        ("work_tags", "country_tag", "country_name", "trip_tag_pattern", "extra_trip_tags"),
    )
    pattern_text = _opt_str(data.get("trip_tag_pattern"), "dimensions.trip_tag_pattern")
    try:
        pattern = re.compile(pattern_text or DEFAULT_TRIP_PATTERN)
    except re.error as exc:
        raise RulesError(f"dimensions.trip_tag_pattern: invalid regex ({exc.msg})") from None
    return Dimensions(
        work_tags=_str_list(data.get("work_tags"), "dimensions.work_tags"),
        country_tag=_opt_str(data.get("country_tag"), "dimensions.country_tag"),
        country_name=_opt_str(data.get("country_name"), "dimensions.country_name"),
        trip_tag_pattern=pattern,
        extra_trip_tags=_str_list(data.get("extra_trip_tags"), "dimensions.extra_trip_tags"),
    )


def _parse_one_off(value: Any) -> OneOff:
    data = _mapping(value, "one_off", ("uncategorized_threshold_eur", "categories"))
    default = OneOff()
    threshold = default.uncategorized_threshold_eur
    if "uncategorized_threshold_eur" in data:
        threshold = _number(
            data["uncategorized_threshold_eur"], "one_off.uncategorized_threshold_eur"
        )
    return OneOff(
        uncategorized_threshold_eur=threshold,
        categories=_str_list(data.get("categories"), "one_off.categories"),
    )


def _parse_outliers(value: Any) -> Outliers:
    data = _mapping(value, "outliers", ("method", "threshold", "min_samples"))
    default = Outliers()
    method = default.method
    if "method" in data:
        method = _str(data["method"], "outliers.method")
        if method != "mad":
            raise RulesError("outliers.method: only 'mad' is supported")
    threshold = default.threshold
    if "threshold" in data:
        threshold = _number(data["threshold"], "outliers.threshold")
    min_samples = default.min_samples
    if "min_samples" in data:
        raw = data["min_samples"]
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise RulesError(f"outliers.min_samples: expected an integer, got {_type_name(raw)}")
        min_samples = raw
    return Outliers(method=method, threshold=threshold, min_samples=min_samples)


def _parse_rule_items(value: Any, name: str, keys: Sequence[str]) -> list[Mapping[str, Any]]:
    items = _list(value, name)
    return [_mapping(item, f"{name}[{i}]", keys) for i, item in enumerate(items)]


def parse_rules(data: Mapping[str, Any] | None) -> Rules:
    """Validate a parsed YAML document and build a `Rules` object."""
    top = _mapping(
        data,
        "",
        (
            "dimensions", "payee_rules", "transfer_rules", "account_aliases",
            "one_off", "outliers", "overrides",
        ),
    )

    payee_rules = []
    for i, item in enumerate(_parse_rule_items(top.get("payee_rules"), "payee_rules", ("match", "set"))):
        path = f"payee_rules[{i}]"
        payee_rules.append(
            PayeeRule(
                _parse_match(item.get("match"), f"{path}.match"),
                _parse_actions(item.get("set"), f"{path}.set", PAYEE_RULE_SET_KEYS),
            )
        )

    transfer_rules = []
    for i, item in enumerate(_parse_rule_items(top.get("transfer_rules"), "transfer_rules", ("match",))):
        transfer_rules.append(
            TransferRule(_parse_match(item.get("match"), f"transfer_rules[{i}].match"))
        )

    aliases_raw = top.get("account_aliases")
    if aliases_raw is not None and not isinstance(aliases_raw, Mapping):
        raise RulesError(f"account_aliases: expected a mapping, got {_type_name(aliases_raw)}")
    aliases: dict[str, str] = {}
    for key, target in (aliases_raw or {}).items():
        if not isinstance(key, str):
            raise RulesError(f"account_aliases: key must be a string, got {_type_name(key)}")
        aliases[key] = _str(target, "account_aliases.<value>")

    overrides = []
    for i, item in enumerate(_parse_rule_items(top.get("overrides"), "overrides", ("uuid", "set"))):
        path = f"overrides[{i}]"
        if "uuid" not in item:
            raise RulesError(f"{path}.uuid: required")
        overrides.append(
            Override(
                _str(item["uuid"], f"{path}.uuid"),
                _parse_actions(item.get("set"), f"{path}.set", OVERRIDE_SET_KEYS),
            )
        )

    return Rules(
        dimensions=_parse_dimensions(top.get("dimensions")),
        payee_rules=tuple(payee_rules),
        transfer_rules=tuple(transfer_rules),
        account_aliases=aliases,
        one_off=_parse_one_off(top.get("one_off")),
        outliers=_parse_outliers(top.get("outliers")),
        overrides=tuple(overrides),
    )


def load_rules(path: Path | None) -> Rules:
    """Load rules from YAML; None or a missing file gives the defaults."""
    if path is None or not Path(path).is_file():
        return Rules()
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)  # position only, never file content
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise RulesError(f"invalid YAML syntax{where}") from None
    return parse_rules(data)
