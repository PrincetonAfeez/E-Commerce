"""Extend CSRF_TRUSTED_ORIGINS with active tenant storefront domains at startup."""
from __future__ import annotations


def extend_csrf_trusted_origins() -> None:
    from django.conf import settings

    try:
        from shop.models import Tenant
    except Exception:
        return

    extras = list(getattr(settings, "CSRF_TRUSTED_ORIGINS", []))
    try:
        for domain in (
            Tenant._base_manager.filter(active=True).exclude(primary_domain="").values_list("primary_domain", flat=True)
        ):
            domain = domain.strip().lower()
            if domain:
                extras.append(f"https://{domain}")
        for base in getattr(settings, "TENANT_PLATFORM_DOMAINS", []):
            base = base.strip().lower()
            if not base:
                continue
            extras.append(f"https://{base}")
            for slug in Tenant._base_manager.filter(active=True).values_list("slug", flat=True):
                if slug:
                    extras.append(f"https://{slug}.{base}")
    except Exception:
        return

    settings.CSRF_TRUSTED_ORIGINS = list(dict.fromkeys(o for o in extras if o))
