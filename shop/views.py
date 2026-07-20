"""Server-rendered views for storefront catalog, cart, checkout, accounts, and staff ops"""
from __future__ import annotations

import os
import re
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Avg, Count, Prefetch
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes, force_str
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_decode, urlsafe_base64_encode
from django.views.decorators.http import require_POST

from .decorators import tenant_staff_required
from .forms import RegistrationForm
from .models import (
    AccountProfile,
    Address,
    Category,
    CheckoutAttempt,
    Collection,
    CustomerSubscription,
    EmailDelivery,
    Fulfillment,
    Order,
    OrderItem,
    Plan,
    Product,
    ProductVariant,
    ReturnRequest,
    Review,
    StoreSettings,
    Subscription,
    Tenant,
    TenantMembership,
    WishlistItem,
)
from .ratelimit import ratelimit
from .services import cart as cart_service
from .services import credit as credit_service
from .services.analytics import dashboard_metrics
from .services.checkout import begin_checkout
from .services.exceptions import CommerceError
from .services.inventory import variants_with_availability
from .services.orders import cancel_order, transition_fulfillment
from .services.payments import authorize_payment, confirm_payment
from .services.plans import plan_usage
from .services.recommendations import also_bought, recently_viewed, track_recently_viewed
from .services.refunds import create_refund
from .services.returns import approve_return, reject_return, request_return
from .services.search import category_facets, search_products
from .tenancy import set_current_tenant


def _forbidden_response(request, message: str = "You do not have access to this resource."):
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse(
            {"code": "permission_denied", "message": message, "field_errors": {}},
            status=403,
        )
    return HttpResponseForbidden(
        f"<!DOCTYPE html><html><body><h1>Forbidden</h1><p>{message}</p></body></html>",
        content_type="text/html; charset=utf-8",
    )


_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def theme_css(request):
    """Serve the store's brand colors as CSS custom properties (CSP-safe, same-origin)."""
    store = StoreSettings.get_solo()
    primary = store.primary_color if _HEX_COLOR.match(store.primary_color or "") else "#3b6fe6"
    accent = store.accent_color if _HEX_COLOR.match(store.accent_color or "") else "#245c24"
    css = f":root{{--brand-primary:{primary};--brand-accent:{accent};}}"
    return HttpResponse(css, content_type="text/css")


def sitemap_xml(request):
    """Tenant-scoped sitemap (Product.objects is auto-filtered to the active store)."""
    base = f"{request.scheme}://{request.get_host()}"
    locs = [base + reverse("catalog:list")]
    locs += [
        base + reverse("catalog:detail", args=[slug])
        for slug in Product.objects.filter(status=Product.Status.ACTIVE).values_list("slug", flat=True)
    ]
    return render(request, "shop/sitemap.xml", {"locs": locs}, content_type="application/xml")


def legal_terms(request):
    """Static Terms of Service page (linked from signup for consent)."""
    return render(request, "shop/legal/terms.html", {"updated": "2026-07-02"})


def legal_privacy(request):
    """Static Privacy Policy page (linked from signup; details data rights)."""
    return render(request, "shop/legal/privacy.html", {"updated": "2026-07-02"})


def robots_txt(request):
    base = f"{request.scheme}://{request.get_host()}"
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /staff/",
        "Disallow: /admin/",
        "Disallow: /account/",
        f"Sitemap: {base}/sitemap.xml",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")


@ratelimit("tls_check", rate=os.environ.get("THROTTLE_TLS_CHECK", "30/min"))
def tls_check(request):
    """On-demand-TLS authorization endpoint for the reverse proxy (e.g. Caddy)."""
    from django.conf import settings
    from django.http import HttpResponseForbidden

    from .models import Tenant

    secret = getattr(settings, "TLS_CHECK_SECRET", "")
    if secret:
        provided = request.headers.get("X-TLS-Check-Secret", "") or request.GET.get("secret", "")
        if provided != secret:
            return HttpResponseForbidden("forbidden")

    domain = (request.GET.get("domain") or "").strip().lower()
    authorized = bool(domain and Tenant.objects.filter(active=True, primary_domain__iexact=domain).exists())
    if "application/json" in request.headers.get("Accept", ""):
        return JsonResponse({"authorized": authorized}, status=200 if authorized else 404)
    return HttpResponse("ok" if authorized else "unknown host", status=200 if authorized else 404)


def unsubscribe(request, token):
    """Honor a one-click marketing unsubscribe from a signed email link."""
    from django.core import signing

    from .models import EmailSuppression
    from .tenancy import default_tenant_id

    try:
        payload = signing.loads(token, salt="unsubscribe", max_age=60 * 60 * 24 * 90)
    except signing.BadSignature:
        return HttpResponse("This unsubscribe link is invalid or has expired.", status=400)
    if isinstance(payload, dict):
        email = payload.get("email", "")
        tenant_id = payload.get("tenant_id")
    else:
        email = payload
        tenant_id = None
    tenant_id = tenant_id or default_tenant_id()
    EmailSuppression.objects.get_or_create(tenant_id=tenant_id, email=email.strip().lower())
    return render(request, "shop/account/unsubscribed.html", {"email": email})


def healthz(request):
    """Liveness + DB/cache connectivity probe (spec §10/§29)."""
    from django.core.cache import cache

    checks = {"database": "ok", "cache": "ok"}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - report unhealthy rather than 500
        checks["database"] = "down"

    try:
        probe_key = "healthz:probe"
        cache.set(probe_key, "1", 10)
        if cache.get(probe_key) != "1":
            checks["cache"] = "down"
        else:
            cache.delete(probe_key)
    except Exception:  # noqa: BLE001 - report unhealthy rather than 500
        checks["cache"] = "down"

    cache_required = settings.IS_PRODUCTION and not getattr(settings, "RUNNING_TESTS", False)
    unhealthy = checks["database"] == "down" or (cache_required and checks["cache"] == "down")
    if unhealthy:
        return JsonResponse({"status": "error", **checks}, status=503)
    return JsonResponse({"status": "ok", **checks})


def readyz(request):
    """Readiness probe — app process is up and can serve traffic."""
    return JsonResponse({"status": "ready"})


def internal_metrics(request):
    """Authenticated platform metrics for on-call dashboards."""
    from django.conf import settings
    from django.http import HttpResponseForbidden

    from shop.services.ops_metrics import collect_ops_metrics

    secret = getattr(settings, "OPS_METRICS_SECRET", "")
    if secret:
        provided = request.headers.get("X-Ops-Metrics-Secret", "") or request.GET.get("secret", "")
        if provided != secret:
            return HttpResponseForbidden("forbidden")
    elif settings.IS_PRODUCTION:
        return HttpResponseForbidden("forbidden")
    return JsonResponse(collect_ops_metrics())


def register(request):
    if request.user.is_authenticated:
        return redirect("catalog:list")
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            AccountProfile.objects.get_or_create(user=user)
            send_verification_email(request, user)
            login(request, user)
            # A subsequent request merges any guest cart into the new user cart.
            messages.success(request, "Welcome! Check your email to verify your address.")
            return redirect("catalog:list")
    else:
        form = RegistrationForm()
    return render(request, "shop/account/register.html", {"form": form})


def send_verification_email(request, user) -> None:
    """Send a tokenized email-verification link (mirrors Django's password-reset flow)."""
    if not user.email:
        return
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    link = request.build_absolute_uri(reverse("verify_email", kwargs={"uidb64": uid, "token": token}))
    body = render_to_string("shop/account/verification_email.html", {"user": user, "verification_link": link})
    send_mail(
        subject="Verify your Aster Commerce email",
        message=body,
        from_email=None,  # DEFAULT_FROM_EMAIL
        recipient_list=[user.email],
        fail_silently=True,
    )
    EmailDelivery.objects.create(
        to_email_hash=_hash_email(user.email),
        template="account.verification_email",
        status=EmailDelivery.Status.SENT,
        sent_at=timezone.now(),
        tenant=getattr(request, "tenant", None),
    )


def verify_email(request, uidb64, token):
    User = get_user_model()
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None
    if user is not None and default_token_generator.check_token(user, token):
        profile, _ = AccountProfile.objects.get_or_create(user=user)
        if not profile.email_verified:
            profile.email_verified = True
            profile.email_verified_at = timezone.now()
            profile.save(update_fields=["email_verified", "email_verified_at", "updated_at"])
        messages.success(request, "Your email has been verified.")
    else:
        messages.error(request, "This verification link is invalid or has expired.")
    return redirect("catalog:list")


@login_required
@require_POST
def resend_verification(request):
    profile, _ = AccountProfile.objects.get_or_create(user=request.user)
    if profile.email_verified:
        messages.info(request, "Your email is already verified.")
    else:
        send_verification_email(request, request.user)
        messages.success(request, "Verification email sent.")
    return redirect("catalog:list")


def _hash_email(value: str) -> str:
    import hashlib

    return hashlib.sha256((value or "").strip().lower().encode("utf-8")).hexdigest()


# --- Saved addresses ---
@login_required
def address_list(request):
    return render(request, "shop/account/addresses.html", {"addresses": request.user.addresses.all()})


@login_required
@require_POST
def address_create(request):
    label = (request.POST.get("label") or "")[:60]
    name = (request.POST.get("name") or "").strip()
    address1 = (request.POST.get("address1") or "").strip()
    address2 = (request.POST.get("address2") or "")[:180]
    city = (request.POST.get("city") or "").strip()
    region = (request.POST.get("region") or "")[:120]
    postal_code = (request.POST.get("postal_code") or "").strip()
    country = (request.POST.get("country") or "US").strip().upper()[:2]
    phone = (request.POST.get("phone") or "")[:40]
    make_default = request.POST.get("is_default") == "on"

    errors = []
    if not name:
        errors.append("Name is required.")
    elif len(name) > 180:
        errors.append("Name is too long.")
    if not address1:
        errors.append("Address is required.")
    elif len(address1) > 180:
        errors.append("Address is too long.")
    if not city:
        errors.append("City is required.")
    elif len(city) > 120:
        errors.append("City is too long.")
    if not postal_code:
        errors.append("ZIP / postal code is required.")
    elif len(postal_code) > 32:
        errors.append("ZIP / postal code is too long.")
    if len(country) != 2:
        errors.append("Country must be a 2-letter code.")
    if errors:
        for message in errors:
            messages.error(request, message)
        return redirect("account:addresses")

    with transaction.atomic():
        if make_default:
            request.user.addresses.update(is_default=False)
        Address.objects.create(
            user=request.user,
            label=label,
            name=name,
            address1=address1,
            address2=address2,
            city=city,
            region=region,
            postal_code=postal_code,
            country=country,
            phone=phone,
            is_default=make_default or not request.user.addresses.exists(),
        )
    messages.success(request, "Address saved.")
    return redirect("account:addresses")


@login_required
@require_POST
def address_delete(request, pk):
    request.user.addresses.filter(pk=pk).delete()
    messages.success(request, "Address removed.")
    return redirect("account:addresses")


@login_required
@require_POST
def address_make_default(request, pk):
    with transaction.atomic():
        if request.user.addresses.filter(pk=pk).exists():
            request.user.addresses.update(is_default=False)
            request.user.addresses.filter(pk=pk).update(is_default=True)
    return redirect("account:addresses")


# --- Privacy: data export + account deletion (GDPR/CCPA) ---
@login_required
def account_data_export(request):
    import json

    from .services.accounts import export_user_data

    body = json.dumps(export_user_data(request.user), indent=2, default=str)
    response = HttpResponse(body, content_type="application/json")
    response["Content-Disposition"] = 'attachment; filename="my-data.json"'
    return response


@login_required
def account_delete(request):
    if request.method == "POST":
        if not request.user.check_password(request.POST.get("password", "")):
            messages.error(request, "Incorrect password. Account was not deleted.")
            return render(request, "shop/account/delete_account.html", {})
        from django.contrib.auth import logout

        from .services.accounts import delete_account

        user = request.user
        logout(request)
        delete_account(user)
        messages.success(request, "Your account and personal data have been deleted.")
        return redirect("catalog:list")
    return render(request, "shop/account/delete_account.html", {})


# --- Store credit & gift cards ---
@login_required
def store_credit_view(request):
    if request.method == "POST":
        try:
            amount = credit_service.redeem_gift_card(request.POST.get("code", ""), request.user)
            messages.success(request, f"Redeemed ${amount} to your store credit.")
        except CommerceError as exc:
            messages.error(request, exc.message)
        return redirect("account:store_credit")
    return render(
        request,
        "shop/account/store_credit.html",
        {
            "balance": credit_service.get_balance(request.user),
            "transactions": request.user.store_credit_transactions.all()[:25],
        },
    )


# --- Subscriptions ---
@login_required
def subscriptions_view(request):
    subs = (
        request.user.subscriptions.select_related("variant__product")
        .exclude(status=CustomerSubscription.Status.CANCELLED)
        .order_by("-created_at")
    )
    return render(request, "shop/account/subscriptions.html", {"subscriptions": subs})


@login_required
@require_POST
def cancel_subscription(request, pk):
    sub = get_object_or_404(CustomerSubscription, pk=pk, user=request.user)
    sub.status = CustomerSubscription.Status.CANCELLED
    sub.save(update_fields=["status", "updated_at"])
    messages.success(request, "Subscription cancelled.")
    return redirect("account:subscriptions")


# --- Wishlist ---
@login_required
def wishlist_view(request):
    items = request.user.wishlist_items.select_related("variant__product")
    return render(request, "shop/account/wishlist.html", {"items": items})


@login_required
@require_POST
def wishlist_toggle(request):
    variant = get_object_or_404(ProductVariant, pk=request.POST.get("variant_id"))
    existing = WishlistItem.objects.filter(user=request.user, variant=variant).first()
    if existing:
        existing.delete()
        messages.success(request, "Removed from wishlist.")
    else:
        WishlistItem.objects.get_or_create(user=request.user, variant=variant)
        messages.success(request, "Added to wishlist.")
    referer = request.META.get("HTTP_REFERER")
    if referer and url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect("catalog:list")


# --- Reorder ---
@login_required
@require_POST
def reorder(request, order_number):
    order = get_object_or_404(Order, order_number=order_number, user=request.user)
    cart = cart_service.get_or_create_cart_for_request(request)
    added = 0
    skipped = 0
    for item in order.items.select_related("variant"):
        if not item.variant_id or not item.variant.active:
            skipped += 1
            continue
        try:
            cart_service.add_item(cart, item.variant, item.quantity)
            added += 1
        except CommerceError:
            skipped += 1
    if added:
        messages.success(request, f"Added {added} item(s) back to your cart.")
    if skipped:
        messages.error(request, f"{skipped} item(s) were unavailable and skipped.")
    return redirect("cart:detail")


# --- Reviews ---
@login_required
@require_POST
@ratelimit("submit_review", rate=os.environ.get("THROTTLE_SUBMIT_REVIEW", "5/h"))
def submit_review(request, slug):
    product = get_object_or_404(Product, slug=slug, status=Product.Status.ACTIVE)
    try:
        rating = int(request.POST.get("rating") or 0)
    except ValueError:
        rating = 0
    if not 1 <= rating <= 5:
        messages.error(request, "Please choose a rating from 1 to 5.")
        return redirect("catalog:detail", slug=slug)
    verified = OrderItem.objects.filter(order__user=request.user, variant__product=product).exists()
    Review.objects.update_or_create(
        product=product,
        user=request.user,
        defaults={
            "rating": rating,
            "title": request.POST.get("title", "")[:140],
            "body": request.POST.get("body", ""),
            "author_name": request.user.get_username(),
            "verified_purchase": verified,
        },
    )
    messages.success(request, "Thanks for your review!")
    return redirect("catalog:detail", slug=slug)


# --- Returns / RMA ---
@login_required
def order_return(request, order_number):
    order = get_object_or_404(Order.objects.prefetch_related("items"), order_number=order_number, user=request.user)
    if request.method == "POST":
        lines = []
        for item in order.items.all():
            try:
                qty = int(request.POST.get(f"qty_{item.pk}") or 0)
            except ValueError:
                qty = 0
            if qty > 0:
                lines.append((item.pk, qty))
        try:
            request_return(order, user=request.user, lines=lines, reason=request.POST.get("reason", ""))
            messages.success(request, "Your return request was submitted.")
            return redirect("orders:detail", order_number=order.order_number)
        except CommerceError as exc:
            messages.error(request, exc.message)
    return render(request, "shop/orders/return_request.html", {"order": order})


@tenant_staff_required
def staff_returns(request):
    returns = ReturnRequest.objects.select_related("order").prefetch_related("lines").order_by("-created_at")
    return render(request, "shop/staff_ops/returns.html", {"returns": returns})


@tenant_staff_required(roles=["owner", "manager"])
@require_POST
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_approve_return(request, pk):
    rr = get_object_or_404(ReturnRequest, pk=pk)
    try:
        approve_return(
            rr,
            actor=request.user,
            restock=request.POST.get("restock") == "on",
            note=request.POST.get("note", ""),
        )
        messages.success(request, "Return approved and refunded.")
    except CommerceError as exc:
        messages.error(request, exc.message)
    return redirect("staff_ops:returns")


@tenant_staff_required(roles=["owner", "manager"])
@require_POST
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_reject_return(request, pk):
    rr = get_object_or_404(ReturnRequest, pk=pk)
    try:
        reject_return(rr, actor=request.user, note=request.POST.get("note", ""))
        messages.success(request, "Return rejected.")
    except CommerceError as exc:
        messages.error(request, exc.message)
    return redirect("staff_ops:returns")


@ratelimit("guest_order_lookup", rate=os.environ.get("THROTTLE_GUEST_ORDER_LOOKUP", "10/h"), field="email")
def guest_order_lookup(request):
    """Email the tokenized order link to a guest who provides their email + order number.

    Always responds the same way regardless of match, to avoid order enumeration (§28)."""
    if request.method == "POST":
        email = request.POST.get("email", "").strip().lower()
        order_number = request.POST.get("order_number", "").strip()
        order = (
            Order.objects.filter(order_number=order_number, guest_email__iexact=email).filter(user__isnull=True).first()
        )
        if order and email:
            link = request.build_absolute_uri(
                reverse("orders:detail", args=[order.order_number]) + f"?token={order.order_token}"
            )
            send_mail(
                subject=f"Your Aster Commerce order {order.order_number}",
                message=f"View your order here:\n{link}\n",
                from_email=None,
                recipient_list=[order.guest_email],
                fail_silently=True,
            )
        messages.success(request, "If that order exists, we've emailed a secure link to view it.")
        return redirect("orders:lookup")
    return render(request, "shop/orders/order_lookup.html", {})


def catalog_list(request):
    products = (
        Product.objects.filter(status=Product.Status.ACTIVE)
        .select_related("category")
        .prefetch_related(Prefetch("variants", queryset=variants_with_availability()), "images")
    )
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    collection = request.GET.get("collection", "").strip()
    min_price = _decimal_or_none(request.GET.get("min_price"))
    max_price = _decimal_or_none(request.GET.get("max_price"))
    if query:
        products = search_products(products, query)
    if category:
        products = products.filter(category__slug=category)
    if collection:
        products = products.filter(collections__slug=collection)
    if min_price is not None:
        products = products.filter(variants__price__gte=min_price)
    if max_price is not None:
        products = products.filter(variants__price__lte=max_price)
    facets = category_facets(products)
    paginator = Paginator(products.distinct(), 12)
    page = paginator.get_page(request.GET.get("page"))
    cart = cart_service.get_or_create_cart_for_request(request)
    # Featured merchandising is only shown on the unfiltered landing view.
    featured = []
    if not any([query, category, collection, min_price, max_price]):
        featured = list(
            Product.objects.filter(status=Product.Status.ACTIVE, featured=True).prefetch_related(
                Prefetch("variants", queryset=variants_with_availability()), "images"
            )[:4]
        )
    return render(
        request,
        "shop/catalog/product_list.html",
        {
            "page": page,
            "cart": cart,
            "query": query,
            "category": category,
            "collection": collection,
            "min_price": request.GET.get("min_price", ""),
            "max_price": request.GET.get("max_price", ""),
            "featured": featured,
            "categories": Category.objects.order_by("name"),
            "collections": Collection.objects.filter(active=True).order_by("name"),
            "facets": facets,
        },
    )


def product_detail(request, slug):
    product = get_object_or_404(
        Product.objects.select_related("category").prefetch_related(
            Prefetch("variants", queryset=variants_with_availability()), "images"
        ),
        slug=slug,
        status=Product.Status.ACTIVE,
    )
    cart = cart_service.get_or_create_cart_for_request(request)
    track_recently_viewed(request, product)
    related = list(
        product.related_products.filter(status=Product.Status.ACTIVE).prefetch_related(
            Prefetch("variants", queryset=variants_with_availability()), "images"
        )[:4]
    )
    if not related:
        related = also_bought(product, limit=4)
    seen = recently_viewed(request, exclude_id=product.id, limit=4)
    reviews = list(product.reviews.filter(approved=True).select_related("user"))
    rating_agg = product.reviews.filter(approved=True).aggregate(avg=Avg("rating"), n=Count("id"))
    wishlisted = set()
    can_review = False
    if request.user.is_authenticated:
        variant_ids = [v.id for v in product.variants.all()]
        wishlisted = set(
            request.user.wishlist_items.filter(variant_id__in=variant_ids).values_list("variant_id", flat=True)
        )
        can_review = OrderItem.objects.filter(order__user=request.user, variant__product=product).exists()
    return render(
        request,
        "shop/catalog/product_detail.html",
        {
            "product": product,
            "cart": cart,
            "related": related,
            "recently_viewed": seen,
            "reviews": reviews,
            "avg_rating": round(rating_agg["avg"], 1) if rating_agg["avg"] else None,
            "review_count": rating_agg["n"],
            "wishlisted": wishlisted,
            "can_review": can_review,
        },
    )


def cart_detail(request):
    cart = cart_service.get_or_create_cart_for_request(request)
    totals = cart_service.recalculate_cart(cart)
    return render(request, "shop/cart/cart_detail.html", {"cart": cart, "totals": totals})


@require_POST
@ratelimit("cart", rate=os.environ.get("THROTTLE_CART", "120/min"), methods=("POST",))
def add_to_cart(request):
    cart = cart_service.get_or_create_cart_for_request(request)
    variant = get_object_or_404(ProductVariant.objects.select_related("product"), pk=request.POST.get("variant_id"))
    try:
        quantity = int(request.POST.get("quantity") or 1)
    except (TypeError, ValueError):
        messages.error(request, "Enter a valid quantity.")
        return _cart_response(request, cart)
    try:
        cart_service.add_item(cart, variant, quantity)
        messages.success(request, "Added to cart.")
    except CommerceError as exc:
        messages.error(request, exc.message)
    return _cart_response(request, cart)


@require_POST
@ratelimit("cart", rate=os.environ.get("THROTTLE_CART", "120/min"), methods=("POST",))
def update_cart_item(request):
    cart = cart_service.get_or_create_cart_for_request(request)
    variant = get_object_or_404(ProductVariant, pk=request.POST.get("variant_id"))
    try:
        quantity = int(request.POST.get("quantity") or 0)
    except (TypeError, ValueError):
        messages.error(request, "Enter a valid quantity.")
        return _cart_response(request, cart)
    try:
        cart_service.set_item_quantity(cart, variant, quantity)
    except CommerceError as exc:
        messages.error(request, exc.message)
    return _cart_response(request, cart)


@require_POST
@ratelimit("cart", rate=os.environ.get("THROTTLE_CART", "120/min"), methods=("POST",))
def apply_coupon(request):
    cart = cart_service.get_or_create_cart_for_request(request)
    try:
        cart_service.apply_coupon(cart, request.POST.get("code", ""))
        messages.success(request, "Coupon applied.")
    except CommerceError as exc:
        messages.error(request, exc.message)
    return _cart_response(request, cart)


@require_POST
@ratelimit("cart", rate=os.environ.get("THROTTLE_CART", "120/min"), methods=("POST",))
def remove_coupon(request):
    cart = cart_service.get_or_create_cart_for_request(request)
    cart_service.remove_coupon(cart)
    return _cart_response(request, cart)


@ratelimit("checkout", rate=os.environ.get("THROTTLE_CHECKOUT", "20/min"), methods=("POST",))
def checkout_start(request):
    cart = cart_service.get_or_create_cart_for_request(request)
    totals = cart_service.recalculate_cart(cart)
    if request.method == "POST":
        if not request.user.is_authenticated and not request.POST.get("email", "").strip():
            messages.error(request, "Email is required for guest checkout.")
            default_address = None
            store_credit = Decimal("0.00")
            return render(
                request,
                "shop/checkout/checkout_start.html",
                {
                    "cart": cart,
                    "totals": totals,
                    "idempotency_key": uuid.uuid4().hex,
                    "default_address": default_address,
                    "store_credit": store_credit,
                },
            )
        if request.user.is_authenticated:
            profile, _ = AccountProfile.objects.get_or_create(user=request.user)
            if not profile.email_verified:
                messages.error(request, "Verify your email before checkout.")
                return redirect("resend_verification")
        try:
            attempt = begin_checkout(
                cart,
                idempotency_key=request.POST.get("idempotency_key") or uuid.uuid4().hex,
                contact={
                    "email": request.POST.get("email", ""),
                    "name": request.POST.get("name", ""),
                },
                shipping={
                    "name": request.POST.get("name", ""),
                    "address1": request.POST.get("address1", ""),
                    "address2": request.POST.get("address2", ""),
                    "city": request.POST.get("city", ""),
                    "region": request.POST.get("region", ""),
                    "postal_code": request.POST.get("postal_code", ""),
                    "country": request.POST.get("country", "US"),
                },
                shipping_method=request.POST.get("shipping_method", "Standard"),
                expected_subtotal=_decimal_or_none(request.POST.get("expected_subtotal")),
                use_store_credit=request.POST.get("use_store_credit") == "on",
            )
            return redirect("checkout:payment", pk=attempt.pk)
        except CommerceError as exc:
            messages.error(request, exc.message)
    default_address = None
    store_credit = Decimal("0.00")
    if request.user.is_authenticated:
        default_address = request.user.addresses.order_by("-is_default", "-updated_at").first()
        store_credit = credit_service.get_balance(request.user)
    return render(
        request,
        "shop/checkout/checkout_start.html",
        {
            "cart": cart,
            "totals": totals,
            "idempotency_key": uuid.uuid4().hex,
            "default_address": default_address,
            "store_credit": store_credit,
        },
    )


@ratelimit("payment", rate=os.environ.get("THROTTLE_PAYMENT", "20/min"), methods=("POST",))
def checkout_payment(request, pk):
    attempt = _attempt_for_request(request, pk)
    if request.method == "POST":
        try:
            payment = authorize_payment(
                attempt,
                idempotency_key=request.POST.get("payment_idempotency_key") or uuid.uuid4().hex,
                card_token=request.POST.get("card_token", "tok_visa"),
                mode=request.POST.get("authorize_mode", "approve"),
            )
            order = confirm_payment(
                payment,
                idempotency_key=request.POST.get("confirmation_idempotency_key") or uuid.uuid4().hex,
                mode=request.POST.get("confirm_mode", "approve"),
            )
            redirect_url = redirect("checkout:complete", order_number=order.order_number)
            if not order.user_id:
                redirect_url["Location"] += f"?token={order.order_token}"
            return redirect_url
        except CommerceError as exc:
            messages.error(request, exc.message)
            attempt.refresh_from_db()
    return render(
        request,
        "shop/checkout/payment.html",
        {
            "attempt": attempt,
            "payment_idempotency_key": uuid.uuid4().hex,
            "confirmation_idempotency_key": uuid.uuid4().hex,
            "gateway_test_modes": settings.GATEWAY_TEST_MODES_ENABLED,
        },
    )


def checkout_complete(request, order_number):
    order = get_object_or_404(
        Order.objects.select_related("checkout_attempt").prefetch_related("items"),
        order_number=order_number,
    )
    if not _can_view_order(request, order):
        return _forbidden_response(request)
    return render(request, "shop/checkout/complete.html", {"order": order})


def _can_view_order(request, order) -> bool:
    """Owner-scoped access: customer orders need the owner; guest orders need the
    owning session or the unguessable order token (spec §9 / §27.2)."""
    if order.user_id:
        return request.user.is_authenticated and order.user_id == request.user.pk
    session_key = request.session.session_key or ""
    if session_key and order.checkout_attempt.session_key == session_key:
        return True
    return str(order.order_token) == request.GET.get("token", "")


@login_required
def order_history(request):
    orders = Order.objects.filter(user=request.user).select_related("fulfillment").prefetch_related("items")
    page = Paginator(orders, 25).get_page(request.GET.get("page"))
    return render(request, "shop/orders/order_history.html", {"page": page})


def order_detail(request, order_number):
    order = get_object_or_404(
        Order.objects.select_related("fulfillment", "checkout_attempt").prefetch_related("items", "status_events"),
        order_number=order_number,
    )
    if not _can_view_order(request, order):
        return _forbidden_response(request)
    return render(request, "shop/orders/order_detail.html", {"order": order})


@tenant_staff_required
def staff_dashboard(request):
    metrics = dashboard_metrics()
    metrics["chart"] = _revenue_chart(metrics["series"])
    metrics["usage"] = plan_usage()
    return render(request, "shop/staff_ops/dashboard.html", metrics)


def _revenue_chart(series: list[dict]) -> dict:
    """Precompute SVG bar geometry (CSP-safe: no inline styles, attributes only)."""
    width, height, pad = 640, 160, 4
    values = [Decimal(row["revenue"]) for row in series] or [Decimal("0")]
    top = max(values) or Decimal("1")
    n = len(series) or 1
    bar_w = max((width - pad * (n + 1)) / n, 1)
    bars = []
    for i, row in enumerate(series):
        rev = Decimal(row["revenue"])
        h = int((rev / top) * (height - 20)) if top else 0
        bars.append(
            {
                "x": round(pad + i * (bar_w + pad), 2),
                "y": height - h,
                "w": round(bar_w, 2),
                "h": h,
                "day": row["day"],
                "revenue": row["revenue"],
            }
        )
    return {"width": width, "height": height, "bars": bars}


@tenant_staff_required
def staff_low_stock(request):
    metrics = dashboard_metrics()
    return render(request, "shop/staff_ops/low_stock.html", {"low_stock": metrics["low_stock"]})


@tenant_staff_required(roles=[TenantMembership.Role.OWNER, TenantMembership.Role.MANAGER])
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_settings(request):
    store = StoreSettings.get_solo()
    if request.method == "POST":
        store.store_name = request.POST.get("store_name", store.store_name).strip() or store.store_name
        store.tagline = request.POST.get("tagline", "")
        store.support_email = request.POST.get("support_email", "")
        for field in ("primary_color", "accent_color"):
            value = request.POST.get(field, "").strip()
            if _HEX_COLOR.match(value):
                setattr(store, field, value)
        store.currency = request.POST.get("currency", store.currency).strip()[:3] or store.currency
        if request.FILES.get("logo"):
            store.logo = request.FILES["logo"]
        store.save()
        messages.success(request, "Store settings saved.")
        return redirect("staff_ops:settings")
    return render(request, "shop/staff_ops/settings.html", {"store": store, "checklist": _onboarding_checklist()})


def _onboarding_checklist() -> list[dict]:
    store = StoreSettings.get_solo()
    from .models import ShippingRate

    return [
        {"label": "Name your store", "done": store.store_name != "Aster Commerce", "url": "staff_ops:settings"},
        {"label": "Add a product", "done": Product.objects.exists(), "url": "admin:shop_product_changelist"},
        {
            "label": "Configure shipping",
            "done": ShippingRate.objects.exists(),
            "url": "admin:shop_shippingrate_changelist",
        },
        {"label": "Set support email", "done": bool(store.support_email), "url": "staff_ops:settings"},
        {"label": "Choose a plan", "done": Subscription.get_solo().plan_id is not None, "url": "staff_ops:billing"},
    ]


@tenant_staff_required(roles=[TenantMembership.Role.OWNER, TenantMembership.Role.MANAGER])
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_billing(request):
    subscription = Subscription.get_solo()
    if request.method == "POST":
        plan = get_object_or_404(Plan, slug=request.POST.get("plan"), active=True)
        subscription.plan = plan
        subscription.status = Subscription.Status.ACTIVE
        subscription.save(update_fields=["plan", "status", "updated_at"])
        messages.success(request, f"You're now on the {plan.name} plan.")
        return redirect("staff_ops:billing")
    from .models import Invoice

    return render(
        request,
        "shop/staff_ops/billing.html",
        {
            "subscription": subscription,
            "plans": Plan.objects.filter(active=True),
            "usage": plan_usage(),
            "invoices": Invoice.objects.all()[:12],
        },
    )


# --- Self-serve store signup (the SaaS front door) ---
def store_signup(request):
    from django.utils.text import slugify

    from shop.feature_flags import is_enabled

    if not is_enabled("SELF_SERVE_SIGNUP"):
        return _forbidden_response(request, "Self-serve store signup is temporarily unavailable.")

    if request.method == "POST":
        User = get_user_model()
        store_name = request.POST.get("store_name", "").strip()
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")
        subdomain = slugify(request.POST.get("subdomain", "") or store_name)[:50]
        errors = []
        if not (store_name and email and password and subdomain):
            errors.append("All fields are required.")
        if subdomain and Tenant.objects.filter(slug=subdomain).exists():
            errors.append("That store address is already taken.")
        if email and (
            User.objects.filter(username=email).exists() or User.objects.filter(email__iexact=email).exists()
        ):
            errors.append("An account with that email already exists.")
        if errors:
            for e in errors:
                messages.error(request, e)
        else:
            host = request.get_host().split(":")[0]
            with transaction.atomic():
                tenant = Tenant.objects.create(
                    name=store_name, slug=subdomain, primary_domain=f"{subdomain}.{host}", active=True
                )
                set_current_tenant(tenant)
                owner = User.objects.create_user(username=email, email=email, password=password)
                AccountProfile.objects.create(user=owner)
                TenantMembership.objects.create(tenant=tenant, user=owner, role=TenantMembership.Role.OWNER)
                store = StoreSettings.get_solo()
                store.store_name = store_name
                store.save()
                subscription = Subscription.get_solo()
                starter = Plan.objects.filter(slug="starter").first()
                if starter:
                    subscription.plan = starter
                    subscription.status = Subscription.Status.TRIALING
                    subscription.save(update_fields=["plan", "status", "updated_at"])
            store_url = f"{request.scheme}://{tenant.primary_domain}/"
            return render(request, "shop/account/store_created.html", {"tenant": tenant, "store_url": store_url})
    return render(request, "shop/account/store_signup.html", {})


# --- Team management (owner/manager) ---
@tenant_staff_required(roles=[TenantMembership.Role.OWNER, TenantMembership.Role.MANAGER])
def staff_team(request):
    members = TenantMembership.objects.select_related("user").all()
    return render(request, "shop/staff_ops/team.html", {"members": members})


@tenant_staff_required(roles=[TenantMembership.Role.OWNER, TenantMembership.Role.MANAGER])
@require_POST
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_invite_member(request):
    User = get_user_model()
    email = request.POST.get("email", "").strip().lower()
    role = request.POST.get("role", TenantMembership.Role.STAFF)
    if role not in TenantMembership.Role.values:
        role = TenantMembership.Role.STAFF
    caller_role = getattr(request, "tenant_role", TenantMembership.Role.STAFF)
    if role == TenantMembership.Role.OWNER and caller_role != TenantMembership.Role.OWNER:
        messages.error(request, "Only owners can grant the owner role.")
        return redirect("staff_ops:team")
    if role == TenantMembership.Role.MANAGER and caller_role not in (
        TenantMembership.Role.OWNER,
        TenantMembership.Role.MANAGER,
    ):
        messages.error(request, "You cannot assign that role.")
        return redirect("staff_ops:team")
    if caller_role == TenantMembership.Role.MANAGER and role != TenantMembership.Role.STAFF:
        messages.error(request, "Managers can only invite staff members.")
        return redirect("staff_ops:team")
    if not email:
        messages.error(request, "Enter an email address.")
        return redirect("staff_ops:team")
    user, created = User.objects.get_or_create(username=email, defaults={"email": email})
    if created:
        user.set_unusable_password()
        user.save()
        AccountProfile.objects.get_or_create(user=user)
        send_verification_email(request, user)  # doubles as a set-up invite link path
    TenantMembership.objects.get_or_create(tenant=request.tenant, user=user, defaults={"role": role})
    messages.success(request, f"{email} was added to your team.")
    return redirect("staff_ops:team")


@tenant_staff_required(roles=[TenantMembership.Role.OWNER])
@require_POST
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_remove_member(request, pk):
    membership = get_object_or_404(TenantMembership, pk=pk)
    if membership.role == TenantMembership.Role.OWNER:
        messages.error(request, "You can't remove the owner.")
    else:
        membership.delete()
        messages.success(request, "Team member removed.")
    return redirect("staff_ops:team")


@tenant_staff_required
def staff_order_queue(request):
    orders = Order.objects.select_related("fulfillment").prefetch_related("items").order_by("-created_at")
    page = Paginator(orders, 25).get_page(request.GET.get("page"))
    return render(request, "shop/staff_ops/order_queue.html", {"page": page})


@tenant_staff_required
def staff_order_detail(request, pk):
    order = get_object_or_404(
        Order.objects.select_related("fulfillment").prefetch_related("items", "status_events", "payments", "refunds"),
        pk=pk,
    )
    return render(
        request,
        "shop/staff_ops/order_detail.html",
        {
            "order": order,
            "statuses": Fulfillment.Status,
            # Fresh per-render key: double-submitting THIS form is idempotent, but each
            # new refund the staffer opens gets its own key so partial refunds accrue.
            "refund_idempotency_key": f"{order.order_number}-refund-{uuid.uuid4().hex[:12]}",
            "cancel_idempotency_key": f"{order.order_number}-cancel-{uuid.uuid4().hex[:12]}",
        },
    )


@tenant_staff_required
@require_POST
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_transition_order(request, pk):
    order = get_object_or_404(Order, pk=pk)
    try:
        transition_fulfillment(
            order,
            target_status=request.POST.get("target_status", ""),
            carrier=request.POST.get("carrier", ""),
            tracking_number=request.POST.get("tracking_number", ""),
            actor=request.user,
            note=request.POST.get("note", ""),
        )
        messages.success(request, "Fulfillment updated.")
    except CommerceError as exc:
        messages.error(request, exc.message)
    return redirect("staff_ops:order_detail", pk=pk)


@tenant_staff_required(roles=["owner", "manager"])
@require_POST
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_refund_order(request, pk):
    order = get_object_or_404(Order, pk=pk)
    try:
        amount_raw = request.POST.get("amount") or "0"
        amount = Decimal(amount_raw)
    except InvalidOperation:
        messages.error(request, "Enter a valid refund amount.")
        return redirect("staff_ops:order_detail", pk=pk)
    idempotency_key = request.POST.get("idempotency_key", "").strip()
    if not idempotency_key:
        messages.error(request, "Idempotency key is required for refunds.")
        return redirect("staff_ops:order_detail", pk=pk)
    try:
        create_refund(
            order,
            amount=amount,
            idempotency_key=idempotency_key,
            restock=request.POST.get("restock") == "on",
            actor=request.user,
            reason=request.POST.get("reason", ""),
        )
        messages.success(request, "Refund created.")
    except CommerceError as exc:
        messages.error(request, exc.message)
    return redirect("staff_ops:order_detail", pk=pk)


@tenant_staff_required(roles=["owner", "manager"])
@require_POST
@ratelimit("staff", rate=os.environ.get("THROTTLE_STAFF", "60/min"), methods=("POST",))
def staff_cancel_order(request, pk):
    import json

    from .models import IdempotencyRecord
    from .services import idempotency
    from .services.exceptions import IdempotencyInProgress, IdempotencyKeyReuseMismatch

    order = get_object_or_404(Order, pk=pk)
    idempotency_key = request.POST.get("idempotency_key", "").strip()
    if not idempotency_key:
        messages.error(request, "Idempotency key is required to cancel.")
        return redirect("staff_ops:order_detail", pk=pk)

    payload = json.dumps(
        {
            "note": request.POST.get("note", ""),
            "restock": request.POST.get("restock") == "on",
        },
        sort_keys=True,
    )
    scope_key = f"{pk}:{idempotency_key}"

    try:
        record = idempotency.begin(
            "staff.cancel",
            scope_key,
            user=request.user,
            session_key=request.session.session_key or "",
            payload=payload,
        )
    except (IdempotencyInProgress, IdempotencyKeyReuseMismatch) as exc:
        messages.error(request, exc.message)
        return redirect("staff_ops:order_detail", pk=pk)

    if record.status in {
        IdempotencyRecord.Status.COMPLETED,
        IdempotencyRecord.Status.FAILED,
    }:
        body = record.response_body or {}
        if record.response_status == 200:
            messages.success(request, body.get("message", "Order cancelled."))
        else:
            messages.error(request, body.get("message", "Cancel failed."))
        return redirect("staff_ops:order_detail", pk=pk)

    try:
        cancel_order(
            order,
            actor=request.user,
            note=request.POST.get("note", ""),
            restock=request.POST.get("restock") == "on",
        )
        idempotency.complete(record, status=200, body={"message": "Order cancelled."})
        messages.success(request, "Order cancelled.")
    except CommerceError as exc:
        idempotency.fail(record, status=400, body=exc.as_dict())
        messages.error(request, exc.message)
    except Exception:
        idempotency.abandon(record)
        raise
    return redirect("staff_ops:order_detail", pk=pk)


def _decimal_or_none(value):
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def _cart_response(request, cart):
    cart.refresh_from_db()
    totals = cart_service.recalculate_cart(cart)
    if request.headers.get("HX-Request"):
        return render(request, "shop/cart/partials/_cart_panel.html", {"cart": cart, "totals": totals})
    return redirect("cart:detail")


def _attempt_for_request(request, pk):
    queryset = CheckoutAttempt.objects.select_related("cart", "user")
    if request.user.is_authenticated:
        return get_object_or_404(queryset, pk=pk, user=request.user)
    session_key = request.session.session_key or ""
    return get_object_or_404(queryset, pk=pk, session_key=session_key)
