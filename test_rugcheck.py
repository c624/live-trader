"""Entry-time on-chain facts, and what counts as a reason to refuse.

The paper study says the median trade merely loses the round-trip cost while
the mean is dragged to -14.8% by positions that lose ninety per cent or more.
The left tail is the problem worth solving, so these facts are read at entry
and recorded for every arm -- acted on only where an arm asks -- and scored
against outcomes later rather than trusted up front.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_trader.src.rugcheck import RugCheck, is_dangerous


class _Rpc:
    def __init__(self, info=None, holders=None):
        self.info, self.holders = info, holders
        self.info_calls = 0

    def mint_info(self, mint):
        self.info_calls += 1
        return self.info

    def largest_holders(self, mint):
        return self.holders


SAFE = {"mintAuthority": None, "freezeAuthority": None,
        "supply": "1000", "decimals": 6}


def test_a_revoked_mint_with_spread_holders_is_not_refused():
    rpc = _Rpc(SAFE, [{"amount": "300"}, {"amount": "200"}])
    features = RugCheck(rpc).features("M", now=0)

    assert features["mint_authority"] is False
    assert features["freeze_authority"] is False
    assert features["top1_share"] == pytest.approx(0.3)
    assert is_dangerous(features) is None


def test_an_open_mint_can_dilute_a_holder_to_nothing():
    rpc = _Rpc(dict(SAFE, mintAuthority="SomeAuthority"), [{"amount": "1"}])
    assert is_dangerous(RugCheck(rpc).features("M", now=0)) == "mint_open"


def test_a_freezable_token_cannot_be_sold_on_demand():
    rpc = _Rpc(dict(SAFE, freezeAuthority="SomeAuthority"), [{"amount": "1"}])
    assert is_dangerous(RugCheck(rpc).features("M", now=0)) == "freezable"


def test_one_account_holding_almost_everything_is_refused():
    rpc = _Rpc(SAFE, [{"amount": "990"}, {"amount": "10"}])
    assert is_dangerous(RugCheck(rpc).features("M", now=0)) == "concentrated"


def test_unknown_is_reported_as_unknown_and_never_as_safe():
    """A mint the RPC would not describe must not read as a clean bill."""
    assert is_dangerous(None) == "unknown"


def test_an_unreadable_supply_leaves_the_share_unknown_not_zero():
    """A zero share would read as perfectly distributed -- the opposite."""
    rpc = _Rpc(dict(SAFE, supply="0"), [{"amount": "5"}])
    features = RugCheck(rpc).features("M", now=0)

    assert features["top1_share"] is None
    assert is_dangerous(features) is None, "unknown concentration is not damning"


def test_the_same_mint_is_not_read_twice_while_it_is_fresh():
    """Every arm evaluates the same launches; one read has to serve them all."""
    rpc = _Rpc(SAFE, [{"amount": "1"}])
    check = RugCheck(rpc)
    check.features("M", now=0)
    check.features("M", now=10)

    assert rpc.info_calls == 1
    assert check.hits == 1


def test_a_stale_reading_is_taken_again():
    rpc = _Rpc(SAFE, [{"amount": "1"}])
    check = RugCheck(rpc, ttl_seconds=60)
    check.features("M", now=0)
    check.features("M", now=61)

    assert rpc.info_calls == 2


def test_a_failed_read_is_remembered_so_it_is_not_retried_in_a_hot_loop():
    rpc = _Rpc(None, None)
    check = RugCheck(rpc)
    assert check.features("M", now=0) is None
    assert check.features("M", now=1) is None
    assert rpc.info_calls == 1 and check.failures == 1
