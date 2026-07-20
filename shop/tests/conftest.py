"""Pytest fixtures clearing cache and tenant context between tests"""
import pytest
from django.core.cache import cache

from shop.tenancy import clear_current_tenant, reset_default_tenant_cache, set_current_tenant


def ensure_verified_profile(user):
    from shop.models import AccountProfile

    profile, _ = AccountProfile.objects.get_or_create(user=user)
    if not profile.email_verified:
        profile.email_verified = True
        profile.save(update_fields=["email_verified", "updated_at"])
    return profile


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset cache + tenant context between tests so state never leaks across tests."""
    cache.clear()
    clear_current_tenant()
    reset_default_tenant_cache()
    from shop.models import Tenant

    tenant = Tenant.objects.filter(slug="default").first() or Tenant.objects.order_by("id").first()
    if tenant is not None:
        set_current_tenant(tenant)
    yield
    cache.clear()
    clear_current_tenant()
    reset_default_tenant_cache()
