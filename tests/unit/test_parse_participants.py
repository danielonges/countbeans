"""Unit tests for parse_participants — the /addexpense split-mode parser.

Replaces the old has_split_suffix reject guard now that uneven splits are wired
through to compute_shares. Parses the @mention region into (mode, handles,
params); a malformed split raises ValueError (the handler turns that into a reply
*before* resolve_participants, so a rejected command creates no placeholder rows).
Sums (percent → 100, exact → amount) are the handler's job, not this parser's.
"""

import pytest

from countbeans.bot.utils.parsing import parse_participants


def test_equal_no_suffix() -> None:
    split = parse_participants("@alice @bob")
    assert split.mode == "equal"
    assert split.handles == ["alice", "bob"]
    assert split.params is None


def test_equal_empty_means_everyone() -> None:
    split = parse_participants("")
    assert split.mode == "equal"
    assert split.handles == []
    assert split.params is None


def test_exact_amounts_parse_to_cents() -> None:
    split = parse_participants("@alice:30 @bob:20.50")
    assert split.mode == "exact"
    assert split.handles == ["alice", "bob"]
    assert split.params == {"alice": 3000, "bob": 2050}


def test_percent_whole_numbers() -> None:
    split = parse_participants("@alice:60% @bob:40%")
    assert split.mode == "percent"
    assert split.params == {"alice": 60, "bob": 40}


def test_weighted_integer_weights() -> None:
    split = parse_participants("@alice:2x @bob:1x")
    assert split.mode == "weighted"
    assert split.params == {"alice": 2, "bob": 1}


def test_rejects_mixed_families() -> None:
    with pytest.raises(ValueError, match="mix"):
        parse_participants("@alice:30 @bob:40%")


def test_rejects_partial_suffix() -> None:
    with pytest.raises(ValueError, match="needs a share"):
        parse_participants("@alice:60% @bob")


def test_rejects_fractional_percent() -> None:
    with pytest.raises(ValueError, match="whole numbers"):
        parse_participants("@alice:33.33% @bob:66.67%")


def test_rejects_fractional_weight() -> None:
    with pytest.raises(ValueError, match="whole numbers"):
        parse_participants("@alice:1.5x @bob:1x")


def test_rejects_non_numeric_exact() -> None:
    with pytest.raises(ValueError, match="valid amount"):
        parse_participants("@alice:lots @bob:5")


def test_rejects_all_with_suffix() -> None:
    with pytest.raises(ValueError, match="@all"):
        parse_participants("@all:30 @bob:20")


def test_all_dropped_but_named_kept() -> None:
    # @all is the everyone-keyword — dropped from the list and excluded from the
    # family analysis, so a weighted split among the named ones still parses.
    split = parse_participants("@all @alice:2x @bob:1x")
    assert split.mode == "weighted"
    assert split.handles == ["alice", "bob"]
    assert split.params == {"alice": 2, "bob": 1}


def test_exact_string_dedup_keeps_first_value() -> None:
    # A repeated exact-string handle dedups (first wins) so the list stays 1:1 with
    # resolve_participants' id-deduped output. Exact amounts parse to cents.
    split = parse_participants("@alice:30 @alice:20 @bob:50")
    assert split.handles == ["alice", "bob"]
    assert split.params == {"alice": 3000, "bob": 5000}


def test_dotted_handle_with_suffix() -> None:
    split = parse_participants("@al.ice:40 @bob:60")
    assert split.mode == "exact"
    assert split.params == {"al.ice": 4000, "bob": 6000}
