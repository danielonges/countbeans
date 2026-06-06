"""Unit tests for has_split_suffix — the uneven-split reject guard.

Uneven splits (@alice:40 exact, @alice:60% percentage, @alice:2x weight) aren't
implemented yet; the /addexpense handler uses this predicate to reject them rather
than silently equal-splitting and dropping the suffix (a money error). The check
runs on the mention-region text *after* the quoted description is removed, so a
colon inside the description must not trigger it.
"""

from countbeans.bot.utils.parsing import has_split_suffix


def test_exact_amount_suffix_detected() -> None:
    assert has_split_suffix("@alice:40 @bob:10") is True


def test_percentage_suffix_detected() -> None:
    assert has_split_suffix("@alice:60% @bob:40%") is True


def test_weight_suffix_detected() -> None:
    assert has_split_suffix("@alice:2x @bob:1x") is True


def test_plain_mentions_not_detected() -> None:
    assert has_split_suffix("@alice @bob") is False


def test_colon_inside_description_not_in_mention_region() -> None:
    # The handler strips the quoted description first, so the mention region for
    # `/addexpense 50 "Time: 5pm dinner" @alice @bob` is just the bare mentions —
    # the colon lived in the description and never reaches this check.
    assert has_split_suffix(" @alice @bob") is False


def test_single_mention_with_suffix_detected() -> None:
    assert has_split_suffix("@alice:30") is True


def test_empty_mention_region_not_detected() -> None:
    assert has_split_suffix("") is False


def test_dotted_handle_with_suffix_detected() -> None:
    # Handles may contain dots; the suffix prefix still matches.
    assert has_split_suffix("@al.ice:40") is True
