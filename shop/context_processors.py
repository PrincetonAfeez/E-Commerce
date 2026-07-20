"""Injects store branding settings and tenant-staff flag into every template"""
from __future__ import annotations

from .models import StoreSettings, TenantMembership


def store_settings(request):
    """Expose store branding + whether the user is staff of the *current* tenant."""
    user = getattr(request, "user", None)
    is_tenant_staff = False
    if user is not None and user.is_authenticated:
        if user.is_superuser:
            is_tenant_staff = True
        else:
            tenant = getattr(request, "tenant", None)
            if tenant is not None:
                is_tenant_staff = TenantMembership.objects.filter(user=user, tenant=tenant).exists()
    return {"store": StoreSettings.get_solo(), "is_tenant_staff": is_tenant_staff}
