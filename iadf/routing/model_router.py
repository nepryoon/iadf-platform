"""
Model routing with EU residency, price binding, and atomic token ledger.

Implements ADD §21, §22, §19.2 of the IADF specification.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from threading import Lock
from typing import Dict, FrozenSet, Iterable, Optional, Tuple


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class ResidencyViolationError(Exception):
    """Raised when EU residency requirement is violated."""
    pass


class PriceBindingExpiredError(Exception):
    """Raised when price binding is not valid at the requested time."""
    pass


# --------------------------------------------------------------------------
# PriceBinding
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PriceBinding:
    """
    Price binding for a model with effective time interval (ADD §22.5).
    
    All rates are per million tokens (mtok). Staleness is enforced by
    the effective_from/effective_to interval.
    """
    effective_from: datetime
    effective_to: datetime
    input_per_mtok: Decimal
    cached_input_per_mtok: Decimal
    cache_write_per_mtok: Decimal
    output_per_mtok: Decimal
    currency: str = "EUR"
    regional_multiplier: Decimal = Decimal("1")
    source_url: str = ""
    source_hash: str = ""
    
    def __post_init__(self):
        """Validate that all rates are non-negative and multiplier is positive."""
        if self.input_per_mtok < 0:
            raise ValueError("input_per_mtok must be non-negative")
        if self.cached_input_per_mtok < 0:
            raise ValueError("cached_input_per_mtok must be non-negative")
        if self.cache_write_per_mtok < 0:
            raise ValueError("cache_write_per_mtok must be non-negative")
        if self.output_per_mtok < 0:
            raise ValueError("output_per_mtok must be non-negative")
        if self.regional_multiplier <= 0:
            raise ValueError("regional_multiplier must be positive")
    
    def valid_at(self, now: datetime) -> bool:
        """Check if this price binding is valid at the given time."""
        return self.effective_from <= now <= self.effective_to


# --------------------------------------------------------------------------
# ModelBinding
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelBinding:
    """
    Model binding with provider, region, residency, and price (ADD §22.5).
    
    The model_id is an exact pinned snapshot (§22.4).
    """
    alias: str
    provider: str
    model_id: str
    endpoint_base_url: str
    region: str
    eu_resident: bool
    retention_mode: str
    allowed_data_classes: FrozenSet[str]
    tier: str
    price: PriceBinding


# --------------------------------------------------------------------------
# DEFAULT_REGISTRY
# --------------------------------------------------------------------------

DEFAULT_REGISTRY: Tuple[ModelBinding, ...] = (
    ModelBinding(
        alias="eu-cheap",
        provider="acme",
        model_id="acme-cheap-2024-06-01",
        endpoint_base_url="https://eu.acme.invalid",
        region="eu-west-1",
        eu_resident=True,
        retention_mode="zero-retention",
        allowed_data_classes=frozenset({"PUB", "INT", "CONF", "SRC"}),
        tier="cheap",
        price=PriceBinding(
            currency="EUR",
            effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2100, 1, 1, tzinfo=timezone.utc),
            input_per_mtok=Decimal("0.50"),
            cached_input_per_mtok=Decimal("0.25"),
            cache_write_per_mtok=Decimal("0.60"),
            output_per_mtok=Decimal("1.00"),
            regional_multiplier=Decimal("1"),
            source_url="https://example.invalid/pricing",
            source_hash="deadbeef",
        ),
    ),
    ModelBinding(
        alias="eu-main",
        provider="acme",
        model_id="acme-main-2024-06-01",
        endpoint_base_url="https://eu.acme.invalid",
        region="eu-west-1",
        eu_resident=True,
        retention_mode="zero-retention",
        allowed_data_classes=frozenset({"PUB", "INT", "CONF", "SRC"}),
        tier="main",
        price=PriceBinding(
            currency="EUR",
            effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2100, 1, 1, tzinfo=timezone.utc),
            input_per_mtok=Decimal("1.00"),
            cached_input_per_mtok=Decimal("0.50"),
            cache_write_per_mtok=Decimal("1.25"),
            output_per_mtok=Decimal("2.00"),
            regional_multiplier=Decimal("1"),
            source_url="https://example.invalid/pricing",
            source_hash="deadbeef",
        ),
    ),
    ModelBinding(
        alias="eu-reviewer",
        provider="acme",
        model_id="acme-reviewer-2024-06-01",
        endpoint_base_url="https://eu.acme.invalid",
        region="eu-west-1",
        eu_resident=True,
        retention_mode="zero-retention",
        allowed_data_classes=frozenset({"PUB", "INT", "CONF", "SRC"}),
        tier="reviewer",
        price=PriceBinding(
            currency="EUR",
            effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2100, 1, 1, tzinfo=timezone.utc),
            input_per_mtok=Decimal("2.00"),
            cached_input_per_mtok=Decimal("1.00"),
            cache_write_per_mtok=Decimal("2.50"),
            output_per_mtok=Decimal("4.00"),
            regional_multiplier=Decimal("1"),
            source_url="https://example.invalid/pricing",
            source_hash="deadbeef",
        ),
    ),
    ModelBinding(
        alias="eu-frontier",
        provider="acme",
        model_id="acme-frontier-2024-06-01",
        endpoint_base_url="https://eu.acme.invalid",
        region="eu-west-1",
        eu_resident=True,
        retention_mode="zero-retention",
        allowed_data_classes=frozenset({"PUB", "INT", "CONF", "SRC"}),
        tier="frontier",
        price=PriceBinding(
            currency="EUR",
            effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2100, 1, 1, tzinfo=timezone.utc),
            input_per_mtok=Decimal("5.00"),
            cached_input_per_mtok=Decimal("2.50"),
            cache_write_per_mtok=Decimal("6.25"),
            output_per_mtok=Decimal("10.00"),
            regional_multiplier=Decimal("1"),
            source_url="https://example.invalid/pricing",
            source_hash="deadbeef",
        ),
    ),
    ModelBinding(
        alias="us-main",
        provider="acme",
        model_id="acme-main-us-2024-06-01",
        endpoint_base_url="https://us.acme.invalid",
        region="us-east-1",
        eu_resident=False,
        retention_mode="standard",
        allowed_data_classes=frozenset({"PUB"}),
        tier="main",
        price=PriceBinding(
            currency="EUR",
            effective_from=datetime(2024, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2100, 1, 1, tzinfo=timezone.utc),
            input_per_mtok=Decimal("0.80"),
            cached_input_per_mtok=Decimal("0.40"),
            cache_write_per_mtok=Decimal("1.00"),
            output_per_mtok=Decimal("1.60"),
            regional_multiplier=Decimal("1"),
            source_url="https://example.invalid/pricing",
            source_hash="deadbeef",
        ),
    ),
)


# --------------------------------------------------------------------------
# ModelRouter
# --------------------------------------------------------------------------

class ModelRouter:
    """
    Static model router with deterministic eligibility checks (ADD §22).
    
    No fallback: violations raise exceptions immediately (FR-IADF-039).
    """
    
    def __init__(self, registry: Iterable[ModelBinding]):
        """
        Initialize router from a registry of model bindings.
        
        Args:
            registry: Iterable of ModelBinding instances
            
        Raises:
            ValueError: If duplicate aliases are found
        """
        self._bindings: Dict[str, ModelBinding] = {}
        for binding in registry:
            if binding.alias in self._bindings:
                raise ValueError(f"Duplicate alias: {binding.alias}")
            self._bindings[binding.alias] = binding
    
    def route(
        self,
        alias: str,
        data_class: str = "CONF",
        require_eu: bool = True,
        now: Optional[datetime] = None,
    ) -> ModelBinding:
        """
        Route to a model binding with deterministic eligibility checks.
        
        Checks are performed in this exact order:
        1. Alias exists (KeyError if not)
        2. EU residency requirement (ResidencyViolationError if violated)
        3. Data class allowed (ValueError if not)
        4. Price binding valid (PriceBindingExpiredError if not)
        
        Args:
            alias: Model alias to route to
            data_class: Data classification (PUB/INT/CONF/SRC/SEC)
            require_eu: Whether EU residency is required
            now: Time to check price binding validity (default: utcnow)
            
        Returns:
            The matching ModelBinding
            
        Raises:
            KeyError: Unknown alias
            ResidencyViolationError: EU residency requirement violated
            ValueError: Data class not allowed
            PriceBindingExpiredError: Price binding not valid at requested time
        """
        # 1. Check alias exists
        if alias not in self._bindings:
            raise KeyError(alias)
        
        binding = self._bindings[alias]
        
        # 2. Check EU residency
        if require_eu and not binding.eu_resident:
            raise ResidencyViolationError(
                f"Model {alias} is not EU-resident but require_eu=True"
            )
        
        # 3. Check data class
        if data_class not in binding.allowed_data_classes:
            raise ValueError(
                f"Data class {data_class} not allowed for model {alias}"
            )
        
        # 4. Check price binding validity
        check_time = now if now is not None else datetime.now(timezone.utc)
        if not binding.price.valid_at(check_time):
            raise PriceBindingExpiredError(
                f"Price binding for {alias} not valid at {check_time}"
            )
        
        return binding


# --------------------------------------------------------------------------
# TokenLedger
# --------------------------------------------------------------------------

class TokenLedger:
    """
    Thread-safe, append-only token usage ledger (§19.2, §31).
    
    Tracks four token categories per model alias:
    - uncached_input: tokens read without cache hit
    - cached_input: tokens read with cache hit
    - cache_write: tokens written to cache
    - output: tokens generated as output
    """
    
    def __init__(self):
        """Initialize an empty ledger."""
        self._lock = Lock()
        self._data: Dict[str, Tuple[int, int, int, int]] = {}
    
    def record(
        self,
        alias: str,
        uncached_input: int,
        cached_input: int,
        cache_write: int,
        output: int,
    ) -> None:
        """
        Record token usage for a model (append-only accumulation).
        
        Args:
            alias: Model alias
            uncached_input: Uncached input tokens
            cached_input: Cached input tokens
            cache_write: Cache write tokens
            output: Output tokens
            
        Raises:
            ValueError: If any value is negative
        """
        # Validate before acquiring lock (fail fast, no state mutation)
        if uncached_input < 0:
            raise ValueError("uncached_input must be non-negative")
        if cached_input < 0:
            raise ValueError("cached_input must be non-negative")
        if cache_write < 0:
            raise ValueError("cache_write must be non-negative")
        if output < 0:
            raise ValueError("output must be non-negative")
        
        with self._lock:
            if alias not in self._data:
                self._data[alias] = (0, 0, 0, 0)
            
            current = self._data[alias]
            self._data[alias] = (
                current[0] + uncached_input,
                current[1] + cached_input,
                current[2] + cache_write,
                current[3] + output,
            )
    
    def totals(self, alias: str) -> Tuple[int, int, int, int]:
        """
        Get total token usage for a model.
        
        Args:
            alias: Model alias
            
        Returns:
            Tuple of (uncached_input, cached_input, cache_write, output)
            Returns (0, 0, 0, 0) if alias is unknown
        """
        with self._lock:
            return self._data.get(alias, (0, 0, 0, 0))
    
    def cost(self, alias: str, router: ModelRouter) -> Decimal:
        """
        Calculate total cost for a model using Decimal arithmetic.
        
        Formula:
        (uncached/1_000_000 * input_per_mtok
         + cached/1_000_000 * cached_input_per_mtok
         + cache_write/1_000_000 * cache_write_per_mtok
         + output/1_000_000 * output_per_mtok)
        * regional_multiplier
        
        Args:
            alias: Model alias
            router: ModelRouter to look up price binding
            
        Returns:
            Total cost as Decimal
            
        Raises:
            KeyError: If alias not found in router
        """
        binding = router._bindings[alias]  # Raises KeyError if not found
        price = binding.price
        
        uncached, cached, cache_write, output = self.totals(alias)
        
        # All arithmetic in Decimal (never float)
        uncached_cost = (Decimal(uncached) / Decimal(1_000_000)) * price.input_per_mtok
        cached_cost = (Decimal(cached) / Decimal(1_000_000)) * price.cached_input_per_mtok
        cache_write_cost = (Decimal(cache_write) / Decimal(1_000_000)) * price.cache_write_per_mtok
        output_cost = (Decimal(output) / Decimal(1_000_000)) * price.output_per_mtok
        
        subtotal = uncached_cost + cached_cost + cache_write_cost + output_cost
        return subtotal * price.regional_multiplier
    
    def snapshot(self) -> Dict[str, Tuple[int, int, int, int]]:
        """
        Get an atomic snapshot of all ledger data.
        
        Returns:
            Dictionary mapping alias to (uncached, cached, cache_write, output)
        """
        with self._lock:
            return dict(self._data)
