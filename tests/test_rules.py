import re
from pathlib import Path

import pytest
import yaml

from expenses.rules import (
    Match,
    Rules,
    RulesError,
    load_rules,
    parse_rules,
    split_category_path,
)

EXAMPLE = Path(__file__).resolve().parent.parent / "rules.example.yaml"


def row(**kw):
    base = {
        "payee": "City Tax Office",
        "comment": "Q1 payment",
        "category_path": "Taxes > Income tax",
        "category_top": "Taxes",
        "account": "Main Account",
        "tags": ["Work", "Paris2024"],
    }
    return {**base, **kw}


def test_defaults():
    rules = load_rules(None)
    assert rules == Rules()
    assert load_rules(Path("/nonexistent/rules.yaml")) == Rules()
    assert rules.one_off.uncategorized_threshold_eur == 10000
    assert rules.outliers.threshold == 3.5 and rules.outliers.min_samples == 8
    assert parse_rules({}) == Rules()
    assert parse_rules(None) == Rules()


def test_example_loads():
    rules = load_rules(EXAMPLE)
    assert rules.dimensions.work_tags == ("Work",)
    assert rules.payee_rules and rules.transfer_rules and rules.overrides
    assert rules.account_aliases == {"Old Wallet": "Main Account"}
    assert rules.overrides[1].set.outlier is False
    assert rules.payee_rules[0].match.matches(row())


def test_example_covers_every_section():
    data = yaml.safe_load(EXAMPLE.read_text())
    assert set(data) == {
        "dimensions", "payee_rules", "transfer_rules", "account_aliases",
        "one_off", "outliers", "overrides",
    }


def test_split_category_path():
    assert split_category_path("Food") == ("Food", None)
    assert split_category_path(" Food >  Coffee ") == ("Food", "Coffee")
    assert split_category_path("A > B>C") == ("A", "B > C")


def test_match_exact_case_insensitive():
    assert Match(payee="city tax office").matches(row())
    assert not Match(payee="city tax").matches(row())
    assert not Match(payee="x").matches(row(payee=None))


def test_match_regex():
    assert Match(payee="regex:tax").matches(row())
    assert Match(comment="regex:^q\\d").matches(row())
    assert not Match(comment="regex:^z").matches(row())


def test_match_category_full_or_top():
    assert Match(category="taxes").matches(row())
    assert Match(category="taxes > income tax").matches(row())
    assert not Match(category="Taxes > Other").matches(row())
    assert not Match(category="Tax").matches(row())
    # regex applies to the full path only
    assert Match(category="regex:Income").matches(row())
    assert not Match(category="regex:^Income").matches(row())


def test_match_tag_and_and_semantics():
    assert Match(tag="work").matches(row())
    assert not Match(tag="Home").matches(row())
    assert Match(tag="Work", account="main account").matches(row())
    assert not Match(tag="Work", account="Other").matches(row())
    assert not Match(tag="Work").matches(row(tags=[]))


def test_empty_match_rejected():
    with pytest.raises(RulesError):
        Match()
    with pytest.raises(RulesError, match=r"transfer_rules\[0\]\.match"):
        parse_rules({"transfer_rules": [{"match": {}}]})


def test_parse_full():
    rules = parse_rules(
        {
            "dimensions": {"work_tags": ["Work"], "country_tag": "Homeland",
                           "country_name": "Homeland", "extra_trip_tags": ["Roadtrip"]},
            "payee_rules": [{"match": {"payee": "A"}, "set": {"category": "X > Y", "add_tags": ["T"]}}],
            "transfer_rules": [{"match": {"payee": "Alex"}}],
            "one_off": {"uncategorized_threshold_eur": 500, "categories": ["Taxes"]},
            "overrides": [{"uuid": "u1", "set": {"kind": "income", "one_off": True,
                                                  "outlier": False, "remove_tags": ["Q"]}}],
        }
    )
    assert rules.one_off.uncategorized_threshold_eur == 500
    assert rules.payee_rules[0].set.add_tags == ("T",)
    assert rules.overrides[0].set.kind == "income"
    assert rules.overrides[0].set.remove_tags == ("Q",)


@pytest.mark.parametrize(
    ("data", "path"),
    [
        ({"bogus": 1}, "bogus: unknown key"),
        ({"dimensions": {"colour": "x"}}, "dimensions.colour: unknown key"),
        (
            {"payee_rules": [{"match": {"payee": "a"}, "set": {"category": "A"}},
                             {"match": {"payee": "a"}, "set": {"category": "A"}},
                             {"match": {"payee": "a"}, "set": {"colour": "red"}}]},
            "payee_rules[2].set.colour: unknown key",
        ),
        ({"payee_rules": [{"match": {"payee": "a"}, "set": {"kind": "expense"}}]},
         "payee_rules[0].set.kind: unknown key"),
        ({"payee_rules": [{"match": {"colour": "a"}, "set": {"category": "A"}}]},
         "payee_rules[0].match.colour: unknown key"),
        ({"transfer_rules": [{"match": {"payee": "a"}, "set": {}}]},
         "transfer_rules[0].set: unknown key"),
        ({"overrides": [{"set": {"outlier": True}}]}, "overrides[0].uuid: required"),
        ({"overrides": [{"uuid": "u", "set": {"outlier": "no"}}]},
         "overrides[0].set.outlier: expected a boolean"),
        ({"overrides": [{"uuid": "u", "set": {"kind": "bogus"}}]}, "overrides[0].set.kind"),
        ({"outliers": {"method": "zscore"}}, "outliers.method"),
        ({"outliers": {"min_samples": 1.5}}, "outliers.min_samples: expected an integer"),
        ({"one_off": {"uncategorized_threshold_eur": "big"}},
         "one_off.uncategorized_threshold_eur: expected a number"),
        ({"dimensions": {"work_tags": "Work"}}, "dimensions.work_tags: expected a list"),
        ({"dimensions": {"work_tags": [1]}}, "dimensions.work_tags[0]: expected a string"),
        ({"dimensions": {"trip_tag_pattern": "("}}, "dimensions.trip_tag_pattern: invalid regex"),
        ({"payee_rules": [{"match": {"payee": "regex:("}, "set": {"category": "A"}}]},
         "payee_rules[0].match.payee: invalid regex"),
        ({"transfer_rules": "x"}, "transfer_rules: expected a list"),
    ],
)
def test_errors_name_path(data, path):
    with pytest.raises(RulesError) as exc:
        parse_rules(data)
    assert path in str(exc.value)


def test_errors_do_not_echo_values():
    secret = "SuperSecretPayeeName"
    with pytest.raises(RulesError) as exc:
        parse_rules({"payee_rules": [{"match": {"payee": f"regex:({secret}"},
                                      "set": {"category": "A"}}]})
    assert secret not in str(exc.value)
    with pytest.raises(RulesError) as exc:
        parse_rules({"overrides": [{"uuid": secret, "set": {"kind": secret}}]})
    assert secret not in str(exc.value)
    with pytest.raises(RulesError) as exc:
        parse_rules({"outliers": {"threshold": secret}})
    assert secret not in str(exc.value)


def test_load_invalid_yaml(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("a: [unclosed\n")
    with pytest.raises(RulesError):
        load_rules(p)


def test_dimensions_helpers():
    dim = parse_rules(
        {"dimensions": {"work_tags": ["Work"], "country_tag": "Homeland",
                        "extra_trip_tags": ["Roadtrip"]}}
    ).dimensions
    assert isinstance(dim.trip_tag_pattern, re.Pattern)
    assert dim.trip_for(["Work", "Paris2024"]) == "Paris2024"
    assert dim.trip_for(["Rome23", "Paris2024"]) == "Paris2024"  # sorted, deterministic
    assert dim.trip_for(["Roadtrip"]) == "Roadtrip"
    assert dim.trip_for(["Work", "paris2024", "Year2024x"]) is None
    assert dim.is_work(["Work"]) and not dim.is_work(["Other"])
    assert dim.in_country(["Homeland"]) and not dim.in_country([])
    assert not Rules().dimensions.in_country(["Homeland"])
