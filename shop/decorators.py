"""tenant_staff_required decorator restricting views to current-tenant staff or superusers"""
from __future__ import annotations

import functools

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def tenant_staff_required(view=None, *, roles=None):
    """Allow only members of the *current* tenant (or platform superusers).

    This is the multi-tenant replacement for @staff_member_required: a user who is staff
    of store A must NOT be able to operate store B just by visiting its host.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if user.is_superuser:
                return func(request, *args, **kwargs)
            from .models import TenantMembership

            tenant = getattr(request, "tenant", None)
            membership = TenantMembership.objects.filter(user=user, tenant=tenant).first() if tenant else None
            if membership and (roles is None or membership.role in roles):
                request.tenant_role = membership.role
                return func(request, *args, **kwargs)
            raise PermissionDenied

        return wrapped

    return decorator(view) if view else decorator
