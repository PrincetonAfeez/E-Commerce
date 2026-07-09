# Pytest fixtures clearing cache and tenant context between tests
import pytest
from django.core.cache import cache

from shop.tenancy import clear_current_tenant, reset_default_tenant_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset cache + tenant context between tests so state never leaks across tests."""
    cache.clear()
    clear_current_tenant()
    reset_default_tenant_cache()
    yield
    cache.clear()
    clear_current_tenant()
    reset_default_tenant_cache()
