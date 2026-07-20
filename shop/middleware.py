"""Request ID and tenant resolution middleware scoping ORM queries per storefront host"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.http import HttpResponseNotFound

# Platform routes that must not require a resolved storefront tenant.
_PLATFORM_PREFIXES = (
    "/admin",
    "/healthz",
    "/readyz",
    "/internal/tls-check",
    "/internal/metrics",
    "/api/v1/webhooks",
    "/signup",
    "/accounts",
    "/legal",
    "/api/v1/schema",
    "/api/v1/docs",
    "/api/v1/redoc",
    "/static",
    "/media",
)


class RequestIDMiddleware:
    """Attach a correlation id to every request and echo it on the response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-Request-ID", "").strip() or uuid.uuid4().hex
        request.request_id = request_id
        response = self.get_response(request)
        response["X-Request-ID"] = request_id
        return response


def _resolve_tenant(host: str):
    from .models import Tenant
    from .tenancy import default_tenant_id

    tenant = Tenant.objects.filter(active=True, primary_domain__iexact=host).first()
    if tenant is not None:
        return tenant

    platform_domains = getattr(settings, "TENANT_PLATFORM_DOMAINS", [])
    for base in platform_domains:
        base = base.lower().strip()
        if not base:
            continue
        suffix = f".{base}"
        if host.endswith(suffix) and host != base:
            prefix = host[: -len(suffix)]
            slug_candidates: list[str] = []
            if "." not in prefix:
                slug_candidates.append(prefix)
            dashed = prefix.replace(".", "-")
            if dashed not in slug_candidates:
                slug_candidates.append(dashed)
            for slug in slug_candidates:
                if not slug:
                    continue
                tenant = Tenant.objects.filter(active=True, slug=slug).first()
                if tenant is not None:
                    return tenant

    if (settings.DEBUG or getattr(settings, "RUNNING_TESTS", False)) and host in {
        "localhost",
        "127.0.0.1",
        "testserver",
        "web",
    }:
        return Tenant.objects.filter(active=True, pk=default_tenant_id()).first()

    return None


class TenantMiddleware:
    """Resolve the active tenant from the request host and scope all ORM queries to it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .tenancy import clear_current_tenant, set_current_tenant

        path = request.path_info
        if any(path.startswith(prefix) for prefix in _PLATFORM_PREFIXES):
            clear_current_tenant()
            request.tenant = None
            try:
                return self.get_response(request)
            finally:
                clear_current_tenant()

        host = request.get_host().split(":")[0].lower()
        platform_domains = {d.lower().strip() for d in getattr(settings, "TENANT_PLATFORM_DOMAINS", []) if d.strip()}
        if host in platform_domains:
            if not any(path.startswith(prefix) for prefix in _PLATFORM_PREFIXES):
                return HttpResponseNotFound("Not found.")
            clear_current_tenant()
            request.tenant = None
            try:
                return self.get_response(request)
            finally:
                clear_current_tenant()

        tenant = _resolve_tenant(host)
        if tenant is None:
            return HttpResponseNotFound("Store not found.")
        request.tenant = tenant
        set_current_tenant(tenant)
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant()
