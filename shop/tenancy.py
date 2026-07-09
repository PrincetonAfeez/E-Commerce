from __future__ import annotations

import contextlib
import contextvars

from django.db import models

# The active tenant for the current request/task. None means "no tenant context" —
# queries are then UNFILTERED (admin, management commands, tests, shell), which keeps
# the app backward-compatible with its single-tenant history.
_current_tenant_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_tenant_id", default=None
)

_default_tenant_id_cache: int | None = None


def set_current_tenant(tenant) -> int | None:
    tid = getattr(tenant, "pk", tenant)
    _current_tenant_id.set(tid)
    return tid


def get_current_tenant_id() -> int | None:
    return _current_tenant_id.get()


def clear_current_tenant() -> None:
    _current_tenant_id.set(None)


@contextlib.contextmanager
def tenant_context(tenant):
    """Temporarily scope ORM queries to a tenant (used by background jobs that iterate
    across tenants). Restores the previous context on exit, so it nests correctly."""
    token = _current_tenant_id.set(getattr(tenant, "pk", tenant))
    try:
        yield
    finally:
        _current_tenant_id.reset(token)


def default_tenant_id() -> int:
    """Cached id of the fallback tenant that owns pre-multi-tenant / context-less data."""
    global _default_tenant_id_cache
    if _default_tenant_id_cache is not None:
        return _default_tenant_id_cache
    from shop.models import Tenant

    tenant = (
        Tenant.objects.filter(slug="default").first() or Tenant.objects.order_by("id").first()
    )
    if tenant is None:
        tenant = Tenant.objects.create(name="Default Store", slug="default")
    _default_tenant_id_cache = tenant.pk
    return _default_tenant_id_cache


def reset_default_tenant_cache() -> None:
    global _default_tenant_id_cache
    _default_tenant_id_cache = None


class TenantManager(models.Manager):
    """Default manager that scopes rows to the active tenant when one is set.

    With no tenant context (None) it returns every row — so admin, jobs and tests keep
    working. Django auto-creates an unfiltered ``_base_manager`` for cascades/refresh,
    so those internals are never affected by this filter.
    """

    def get_queryset(self):
        qs = super().get_queryset()
        tid = get_current_tenant_id()
        if tid is not None:
            return qs.filter(tenant_id=tid)
        return qs
