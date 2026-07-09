# Request ID and tenant resolution middleware scoping ORM queries per storefront host
from __future__ import annotations

import uuid


class RequestIDMiddleware:
    """Attach a correlation id to every request and echo it on the response.

    The id is available as ``request.request_id`` for structured logging and audit
    trails, and returned as ``X-Request-ID`` (accepting an inbound one if provided).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = request.headers.get("X-Request-ID", "").strip() or uuid.uuid4().hex
        request.request_id = request_id
        response = self.get_response(request)
        response["X-Request-ID"] = request_id
        return response


class TenantMiddleware:
    """Resolve the active tenant from the request host and scope all ORM queries to it.

    Admin paths are exempt so platform operators see every tenant. Unmatched hosts fall
    back to the default tenant. The context is always cleared after the request so it
    never leaks across requests on a reused worker thread.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .models import Tenant
        from .tenancy import clear_current_tenant, default_tenant_id, set_current_tenant

        if request.path_info.startswith("/admin"):
            clear_current_tenant()
            request.tenant = None
            try:
                return self.get_response(request)
            finally:
                clear_current_tenant()

        host = request.get_host().split(":")[0].lower()
        tenant = Tenant.objects.filter(active=True, primary_domain__iexact=host).first()
        if tenant is None:
            subdomain = host.split(".")[0]
            tenant = Tenant.objects.filter(active=True, slug=subdomain).first()
        if tenant is None:
            tenant = Tenant.objects.filter(pk=default_tenant_id()).first()
        request.tenant = tenant
        set_current_tenant(tenant)
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant()
