"""
Red-proof tests for TASK-04-MODEL-ROUTER (ADD §21, §22, §19.2).

Hermetic: stdlib + pytest only. No network, no filesystem writes
outside tmp_path (not used here), no sleeps, no env mutation.
"""
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from iadf.routing.model_router import (
    ModelBinding,
    ModelRouter,
    PriceBinding,
    PriceBindingExpiredError,
    ResidencyViolationError,
    TokenLedger,
    DEFAULT_REGISTRY,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_price(
    *,
    effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
    effective_to=datetime(2100, 1, 1, tzinfo=timezone.utc),
    input_per_mtok=Decimal("1.00"),
    cached_input_per_mtok=Decimal("0.50"),
    cache_write_per_mtok=Decimal("1.25"),
    output_per_mtok=Decimal("2.00"),
    regional_multiplier=Decimal("1"),
    currency="EUR",
    source_url="https://example.invalid/pricing",
    source_hash="deadbeef",
):
    return PriceBinding(
        currency=currency,
        effective_from=effective_from,
        effective_to=effective_to,
        input_per_mtok=input_per_mtok,
        cached_input_per_mtok=cached_input_per_mtok,
        cache_write_per_mtok=cache_write_per_mtok,
        output_per_mtok=output_per_mtok,
        regional_multiplier=regional_multiplier,
        source_url=source_url,
        source_hash=source_hash,
    )


def make_binding(
    *,
    alias="eu-main",
    provider="acme",
    model_id="acme-main-2024-06-01",
    endpoint_base_url="https://eu.acme.invalid",
    region="eu-west-1",
    eu_resident=True,
    retention_mode="zero-retention",
    allowed_data_classes=frozenset({"PUB", "INT", "CONF", "SRC"}),
    tier="main",
    price=None,
):
    return ModelBinding(
        alias=alias,
        provider=provider,
        model_id=model_id,
        endpoint_base_url=endpoint_base_url,
        region=region,
        eu_resident=eu_resident,
        retention_mode=retention_mode,
        allowed_data_classes=allowed_data_classes,
        tier=tier,
        price=price or make_price(),
    )


# --------------------------------------------------------------------------
# PriceBinding
# --------------------------------------------------------------------------

class TestPriceBinding:
    def test_valid_at_within_interval_true(self):
        pb = make_price()
        assert pb.valid_at(datetime(2025, 1, 1, tzinfo=timezone.utc)) is True

    def test_valid_at_before_interval_false(self):
        pb = make_price()
        assert pb.valid_at(datetime(2023, 1, 1, tzinfo=timezone.utc)) is False

    def test_valid_at_after_interval_false(self):
        pb = make_price()
        assert pb.valid_at(datetime(2101, 1, 1, tzinfo=timezone.utc)) is False

    @pytest.mark.parametrize(
        "field",
        [
            "input_per_mtok",
            "cached_input_per_mtok",
            "cache_write_per_mtok",
            "output_per_mtok",
        ],
    )
    def test_negative_rate_raises(self, field):
        kwargs = {field: Decimal("-0.01")}
        with pytest.raises(ValueError):
            make_price(**kwargs)

    def test_zero_regional_multiplier_raises(self):
        with pytest.raises(ValueError):
            make_price(regional_multiplier=Decimal("0"))

    def test_negative_regional_multiplier_raises(self):
        with pytest.raises(ValueError):
            make_price(regional_multiplier=Decimal("-1"))

    def test_default_currency_eur(self):
        pb = make_price()
        assert pb.currency == "EUR"

    def test_frozen_immutable(self):
        pb = make_price()
        with pytest.raises(Exception):
            pb.currency = "USD"


# --------------------------------------------------------------------------
# ModelBinding + DEFAULT_REGISTRY
# --------------------------------------------------------------------------

class TestDefaultRegistry:
    def test_at_least_four_entries(self):
        assert len(DEFAULT_REGISTRY) >= 4

    def test_all_tiers_covered(self):
        tiers = {b.tier for b in DEFAULT_REGISTRY}
        assert {"cheap", "main", "reviewer", "frontier"} <= tiers

    def test_at_least_one_non_eu(self):
        assert any(not b.eu_resident for b in DEFAULT_REGISTRY)

    def test_at_least_one_eu_conf_capable(self):
        assert any(
            b.eu_resident and "CONF" in b.allowed_data_classes
            for b in DEFAULT_REGISTRY
        )

    def test_unique_aliases(self):
        aliases = [b.alias for b in DEFAULT_REGISTRY]
        assert len(aliases) == len(set(aliases))

    def test_default_registry_constructs_router(self):
        # Should not raise (no duplicate aliases)
        router = ModelRouter(DEFAULT_REGISTRY)
        assert router is not None


# --------------------------------------------------------------------------
# ModelRouter
# --------------------------------------------------------------------------

class TestModelRouterConstruction:
    def test_duplicate_alias_raises(self):
        b1 = make_binding(alias="dup")
        b2 = make_binding(alias="dup")
        with pytest.raises(ValueError):
            ModelRouter([b1, b2])


class TestModelRouterRoute:
    def test_route_success_eu_conf(self):
        b = make_binding(alias="eu-main", eu_resident=True,
                          allowed_data_classes=frozenset({"CONF"}))
        router = ModelRouter([b])
        result = router.route("eu-main", data_class="CONF", require_eu=True)
        assert result is b

    def test_unknown_alias_raises_keyerror(self):
        router = ModelRouter([make_binding(alias="eu-main")])
        with pytest.raises(KeyError):
            router.route("does-not-exist")

    def test_require_eu_true_non_eu_binding_raises_residency(self):
        b = make_binding(alias="us-main", eu_resident=False)
        router = ModelRouter([b])
        with pytest.raises(ResidencyViolationError):
            router.route("us-main", data_class="CONF", require_eu=True)

    def test_require_eu_false_allows_non_eu(self):
        b = make_binding(
            alias="us-main", eu_resident=False,
            allowed_data_classes=frozenset({"PUB"}),
        )
        router = ModelRouter([b])
        result = router.route("us-main", data_class="PUB", require_eu=False)
        assert result is b

    def test_data_class_not_allowed_raises_valueerror(self):
        b = make_binding(
            alias="eu-main", eu_resident=True,
            allowed_data_classes=frozenset({"PUB"}),
        )
        router = ModelRouter([b])
        with pytest.raises(ValueError):
            router.route("eu-main", data_class="SEC", require_eu=True)

    def test_expired_price_binding_raises(self):
        expired_price = make_price(
            effective_from=datetime(2000, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2001, 1, 1, tzinfo=timezone.utc),
        )
        b = make_binding(
            alias="eu-main", eu_resident=True,
            allowed_data_classes=frozenset({"CONF"}), price=expired_price,
        )
        router = ModelRouter([b])
        with pytest.raises(PriceBindingExpiredError):
            router.route(
                "eu-main", data_class="CONF", require_eu=True,
                now=datetime(2025, 1, 1, tzinfo=timezone.utc),
            )

    def test_not_yet_effective_price_binding_raises(self):
        future_price = make_price(
            effective_from=datetime(2200, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2300, 1, 1, tzinfo=timezone.utc),
        )
        b = make_binding(
            alias="eu-main", eu_resident=True,
            allowed_data_classes=frozenset({"CONF"}), price=future_price,
        )
        router = ModelRouter([b])
        with pytest.raises(PriceBindingExpiredError):
            router.route(
                "eu-main", data_class="CONF", require_eu=True,
                now=datetime(2025, 1, 1, tzinfo=timezone.utc),
            )

    def test_order_residency_checked_before_data_class(self):
        # Non-EU AND wrong data class: residency error must win (checked first)
        b = make_binding(
            alias="us-main", eu_resident=False,
            allowed_data_classes=frozenset({"PUB"}),
        )
        router = ModelRouter([b])
        with pytest.raises(ResidencyViolationError):
            router.route("us-main", data_class="SEC", require_eu=True)

    def test_order_data_class_checked_before_price_expiry(self):
        expired_price = make_price(
            effective_from=datetime(2000, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2001, 1, 1, tzinfo=timezone.utc),
        )
        b = make_binding(
            alias="eu-main", eu_resident=True,
            allowed_data_classes=frozenset({"PUB"}), price=expired_price,
        )
        router = ModelRouter([b])
        with pytest.raises(ValueError):
            router.route(
                "eu-main", data_class="SEC", require_eu=True,
                now=datetime(2025, 1, 1, tzinfo=timezone.utc),
            )

    def test_no_fallback_route_raises_directly_not_alternate_binding(self):
        # Only one eligible EU binding exists; requesting an ineligible
        # non-EU alias must raise, never silently substitute the EU one.
        eu_binding = make_binding(
            alias="eu-main", eu_resident=True,
            allowed_data_classes=frozenset({"CONF"}),
        )
        us_binding = make_binding(
            alias="us-main", eu_resident=False,
            allowed_data_classes=frozenset({"CONF"}),
        )
        router = ModelRouter([eu_binding, us_binding])
        with pytest.raises(ResidencyViolationError):
            router.route("us-main", data_class="CONF", require_eu=True)


# --------------------------------------------------------------------------
# TokenLedger
# --------------------------------------------------------------------------

class TestTokenLedgerRecordAndTotals:
    def test_totals_unknown_alias_zero(self):
        ledger = TokenLedger()
        assert ledger.totals("nope") == (0, 0, 0, 0)

    def test_record_accumulates(self):
        ledger = TokenLedger()
        ledger.record("eu-main", 100, 10, 5, 20)
        ledger.record("eu-main", 50, 5, 0, 10)
        assert ledger.totals("eu-main") == (150, 15, 5, 30)

    def test_record_negative_uncached_raises(self):
        ledger = TokenLedger()
        with pytest.raises(ValueError):
            ledger.record("eu-main", -1, 0, 0, 0)

    def test_record_negative_cached_raises(self):
        ledger = TokenLedger()
        with pytest.raises(ValueError):
            ledger.record("eu-main", 0, -1, 0, 0)

    def test_record_negative_cache_write_raises(self):
        ledger = TokenLedger()
        with pytest.raises(ValueError):
            ledger.record("eu-main", 0, 0, -1, 0)

    def test_record_negative_output_raises(self):
        ledger = TokenLedger()
        with pytest.raises(ValueError):
            ledger.record("eu-main", 0, 0, 0, -1)

    def test_rejected_record_does_not_mutate(self):
        ledger = TokenLedger()
        ledger.record("eu-main", 10, 10, 10, 10)
        with pytest.raises(ValueError):
            ledger.record("eu-main", -5, 0, 0, 0)
        assert ledger.totals("eu-main") == (10, 10, 10, 10)


class TestTokenLedgerCost:
    def test_cost_computation_exact_decimal(self):
        price = make_price(
            input_per_mtok=Decimal("1.00"),
            cached_input_per_mtok=Decimal("0.50"),
            cache_write_per_mtok=Decimal("1.25"),
            output_per_mtok=Decimal("2.00"),
            regional_multiplier=Decimal("1"),
        )
        b = make_binding(alias="eu-main", price=price,
                          allowed_data_classes=frozenset({"CONF"}))
        router = ModelRouter([b])
        ledger = TokenLedger()
        ledger.record("eu-main", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        cost = ledger.cost("eu-main", router)
        expected = Decimal("1.00") + Decimal("0.50") + Decimal("1.25") + Decimal("2.00")
        assert cost == expected

    def test_cost_applies_regional_multiplier(self):
        price = make_price(
            input_per_mtok=Decimal("1.00"),
            cached_input_per_mtok=Decimal("0"),
            cache_write_per_mtok=Decimal("0"),
            output_per_mtok=Decimal("0"),
            regional_multiplier=Decimal("2"),
        )
        b = make_binding(alias="eu-main", price=price,
                          allowed_data_classes=frozenset({"CONF"}))
        router = ModelRouter([b])
        ledger = TokenLedger()
        ledger.record("eu-main", 1_000_000, 0, 0, 0)
        assert ledger.cost("eu-main", router) == Decimal("2.00")

    def test_cost_zero_usage_zero_cost(self):
        b = make_binding(alias="eu-main",
                          allowed_data_classes=frozenset({"CONF"}))
        router = ModelRouter([b])
        ledger = TokenLedger()
        assert ledger.cost("eu-main", router) == Decimal("0")

    def test_cost_unknown_alias_raises_keyerror(self):
        b = make_binding(alias="eu-main")
        router = ModelRouter([b])
        ledger = TokenLedger()
        with pytest.raises(KeyError):
            ledger.cost("does-not-exist", router)

    def test_cost_returns_decimal_type(self):
        b = make_binding(alias="eu-main",
                          allowed_data_classes=frozenset({"CONF"}))
        router = ModelRouter([b])
        ledger = TokenLedger()
        ledger.record("eu-main", 1, 1, 1, 1)
        assert isinstance(ledger.cost("eu-main", router), Decimal)


class TestTokenLedgerSnapshot:
    def test_snapshot_reflects_current_state(self):
        ledger = TokenLedger()
        ledger.record("eu-main", 1, 2, 3, 4)
        ledger.record("us-main", 5, 6, 7, 8)
        snap = ledger.snapshot()
        assert snap == {"eu-main": (1, 2, 3, 4), "us-main": (5, 6, 7, 8)}

    def test_snapshot_is_a_copy_not_live_view(self):
        ledger = TokenLedger()
        ledger.record("eu-main", 1, 1, 1, 1)
        snap = ledger.snapshot()
        ledger.record("eu-main", 100, 100, 100, 100)
        assert snap["eu-main"] == (1, 1, 1, 1)
        assert ledger.totals("eu-main") == (101, 101, 101, 101)

    def test_snapshot_empty_ledger(self):
        ledger = TokenLedger()
        assert ledger.snapshot() == {}


class TestTokenLedgerThreadSafety:
    def test_concurrent_record_no_lost_updates(self):
        ledger = TokenLedger()
        n_threads = 8
        n_iterations = 200

        def worker():
            for _ in range(n_iterations):
                ledger.record("eu-main", 1, 1, 1, 1)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected = n_threads * n_iterations
        assert ledger.totals("eu-main") == (expected, expected, expected, expected)
