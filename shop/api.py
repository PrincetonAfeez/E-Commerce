"""DRF API views with tenant-scoped permissions for catalog, cart, checkout, and staff ops"""
from __future__ import annotations

import json

from django.core.mail import send_mail
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from django.urls import reverse
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.views import exception_handler as drf_exception_handler

try:
    from drf_spectacular.utils import (
        OpenApiParameter,
        PolymorphicProxySerializer,
        extend_schema,
        extend_schema_view,
        inline_serializer,
    )
except ImportError:  # schema generation is optional

    def extend_schema(**_kwargs):
        def decorator(cls):
            return cls

        return decorator

    def extend_schema_view(**_kwargs):
        def decorator(cls):
            return cls

        return decorator

    class OpenApiParameter:  # noqa: N801
        QUERY = "query"
        HEADER = "header"

        def __init__(self, *args, **kwargs):
            pass

    def PolymorphicProxySerializer(**_kwargs):
        return None

    def inline_serializer(name, fields):
        return None


from .models import Category, CheckoutAttempt, Collection, Order, Product, ProductVariant
from .pagination import CreatedAtCursorPagination
from .serializers import (
    AuthorizePaymentSerializer,
    BeginCheckoutSerializer,
    CartAddItemSerializer,
    CartSerializer,
    CategorySerializer,
    CheckoutAttemptSerializer,
    CollectionSerializer,
    ConfirmPaymentSerializer,
    CouponActionSerializer,
    GuestOrderLookupResponseSerializer,
    GuestOrderLookupSerializer,
    GuestOrderSerializer,
    InventoryAdjustmentResponseSerializer,
    InventoryAdjustmentSerializer,
    OrderSerializer,
    PaymentSerializer,
    ProductSerializer,
    RefundResponseSerializer,
    RefundSerializer,
    StaffCancelSerializer,
    StaffTransitionSerializer,
)
from .services import cart as cart_service
from .services import idempotency
from .services.checkout import begin_checkout, replay_finalization
from .services.exceptions import CommerceError, PaymentGatewayError, PermissionDenied
from .services.inventory import adjust_stock, variants_with_availability
from .services.orders import cancel_order, transition_fulfillment
from .services.payments import authorize_payment, confirm_payment
from .services.psp_webhooks import receive_gateway_webhook
from .services.refunds import create_refund


class IsTenantStaff(permissions.BasePermission):
    """A platform superuser, OR a staff member of the *request's* tenant.

    Critical for multi-tenancy: authorization is scoped to request.tenant, so a staffer
    of store A cannot operate store B by switching host (unlike a global is_staff check).
    """

    required_roles = None  # None = any role

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_superuser:
            return True
        from .models import TenantMembership

        tenant = getattr(request, "tenant", None)
        if tenant is None:
            return False
        membership = TenantMembership.objects.filter(user=user, tenant=tenant).first()
        if membership is None:
            return False
        return self.required_roles is None or membership.role in self.required_roles


class IsTenantManager(IsTenantStaff):
    """Owner/manager of the request's tenant (for money/inventory-sensitive actions)."""

    required_roles = ["owner", "manager"]


class RequireTenant(permissions.BasePermission):
    """Reject API calls that did not resolve a storefront tenant from the request host."""

    message = "A valid store hostname is required."

    def has_permission(self, request, view):
        return getattr(request, "tenant", None) is not None


_PUBLIC_API = [RequireTenant, permissions.AllowAny]


_COMMERCE_ERROR_STATUS = {
    "out_of_stock": status.HTTP_409_CONFLICT,
    "checkout_state_error": status.HTTP_409_CONFLICT,
    "checkout_attempt_terminal": status.HTTP_409_CONFLICT,
    "compensation_required": status.HTTP_409_CONFLICT,
    "idempotency_in_progress": status.HTTP_409_CONFLICT,
    "idempotency_key_reuse_mismatch": status.HTTP_409_CONFLICT,
    "email_not_verified": status.HTTP_403_FORBIDDEN,
    "payment_failed": status.HTTP_402_PAYMENT_REQUIRED,
    "payment_gateway_error": status.HTTP_402_PAYMENT_REQUIRED,
    "permission_denied": status.HTTP_403_FORBIDDEN,
}


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _decimal_or_none(value):
    from decimal import Decimal, InvalidOperation

    if value in (None, ""):
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def exception_handler(exc, context):
    if isinstance(exc, CommerceError):
        return Response(
            exc.as_dict(),
            status=_COMMERCE_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        )
    response = drf_exception_handler(exc, context)
    if response is not None:
        data = response.data
        if isinstance(data, dict) and "code" in data and "message" in data:
            response.data = data
        else:
            status_code = response.status_code
            if status_code == status.HTTP_401_UNAUTHORIZED:
                code = "authentication_required"
                message = "Authentication credentials were not provided or are invalid."
            elif status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                code = "rate_limited"
                message = "Request was throttled. Please try again later."
            elif status_code == status.HTTP_400_BAD_REQUEST:
                code = "validation_error"
                message = "Request validation failed."
            else:
                code = "api_error"
                message = "Request failed."
            response.data = {
                "code": code,
                "message": message,
                "field_errors": data if isinstance(data, dict) else {},
            }
    return response


_IDEMPOTENCY_HEADER = OpenApiParameter(
    name="Idempotency-Key",
    type=str,
    location=OpenApiParameter.HEADER,
    required=True,
)

_GUEST_ORDER_LOOKUP_MESSAGE = "If that order exists, we've emailed a secure link to view it."

_ORDER_RESPONSE_SCHEMA = PolymorphicProxySerializer(
    component_name="OrderResponse",
    serializers=[OrderSerializer, GuestOrderSerializer],
    resource_type_field_name=None,
)

_PAGINATED_ORDER_LIST_SCHEMA = inline_serializer(
    name="PaginatedOrderList",
    fields={
        "next": serializers.CharField(allow_null=True, required=False),
        "previous": serializers.CharField(allow_null=True, required=False),
        "results": OrderSerializer(many=True),
    },
)

_CURSOR_PARAMS = [
    OpenApiParameter(
        name="cursor",
        type=str,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Opaque cursor for the next page of results.",
    ),
    OpenApiParameter(
        name="page_size",
        type=int,
        location=OpenApiParameter.QUERY,
        required=False,
        description="Number of results per page.",
    ),
]

_PRODUCT_LIST_PARAMS = [
    OpenApiParameter(name="q", type=str, location=OpenApiParameter.QUERY, required=False),
    OpenApiParameter(name="category", type=str, location=OpenApiParameter.QUERY, required=False),
    OpenApiParameter(name="collection", type=str, location=OpenApiParameter.QUERY, required=False),
    OpenApiParameter(name="min_price", type=str, location=OpenApiParameter.QUERY, required=False),
    OpenApiParameter(name="max_price", type=str, location=OpenApiParameter.QUERY, required=False),
]


def require_idempotency_key(request):
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        raise CommerceError("Idempotency-Key header is required.", code="idempotency_key_required")
    return key


def _order_response(order):
    if order.user_id is None:
        return GuestOrderSerializer(order).data
    return OrderSerializer(order).data


def _json_safe(data):
    return json.loads(JSONRenderer().render(data)) if data is not None else {}


def idempotent_write(request, *, scope: str, key: str, produce):
    """Run ``produce`` under an IdempotencyRecord (spec §21 / ADR-0017)."""
    from .services.cart import ensure_session_key

    actor = request.user if request.user.is_authenticated else None
    if not actor:
        ensure_session_key(request)
    session_key = request.session.session_key or ""
    payload = json.dumps(request.data, sort_keys=True, default=str)
    try:
        record = idempotency.begin(scope, key, user=actor, session_key=session_key, payload=payload)
    except RuntimeError as exc:
        raise CommerceError(str(exc), code="checkout_state_error") from exc
    if record.status in {
        idempotency.IdempotencyRecord.Status.COMPLETED,
        idempotency.IdempotencyRecord.Status.FAILED,
    }:
        return Response(record.response_body, status=record.response_status or status.HTTP_200_OK)
    try:
        response = produce()
    except CommerceError as exc:
        # Deterministic domain failure: cache it so replays return the same result.
        idempotency.fail(
            record,
            status=_COMMERCE_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
            body=exc.as_dict(),
        )
        raise
    except Exception:
        # Unexpected/transient failure: release the lock so the key can be retried
        # rather than caching a 500 or bricking the key (spec §21 TTL semantics).
        idempotency.abandon(record)
        raise
    idempotency.complete(record, status=response.status_code, body=_json_safe(response.data))
    return response


@extend_schema_view(list=extend_schema(parameters=_PRODUCT_LIST_PARAMS))
class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = _PUBLIC_API
    serializer_class = ProductSerializer
    lookup_field = "slug"

    def get_queryset(self):
        queryset = (
            Product.objects.filter(status=Product.Status.ACTIVE)
            .select_related("category")
            .prefetch_related(Prefetch("variants", queryset=variants_with_availability()), "images")
        )
        search = self.request.query_params.get("q", "").strip()
        category = self.request.query_params.get("category", "").strip()
        collection = self.request.query_params.get("collection", "").strip()
        min_price = self.request.query_params.get("min_price")
        max_price = self.request.query_params.get("max_price")
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(description__icontains=search) | Q(variants__sku__icontains=search)
            )
        if category:
            queryset = queryset.filter(category__slug=category)
        if collection:
            queryset = queryset.filter(collections__slug=collection)
        min_price = _decimal_or_none(min_price)
        max_price = _decimal_or_none(max_price)
        if min_price is not None:
            queryset = queryset.filter(variants__price__gte=min_price)
        if max_price is not None:
            queryset = queryset.filter(variants__price__lte=max_price)
        return queryset.distinct()


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = _PUBLIC_API
    serializer_class = CategorySerializer
    lookup_field = "slug"
    queryset = Category.objects.all()


class CollectionViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = _PUBLIC_API
    serializer_class = CollectionSerializer
    lookup_field = "slug"
    queryset = Collection.objects.filter(active=True)


@extend_schema(responses=CartSerializer)
class CartView(APIView):
    permission_classes = _PUBLIC_API

    def get(self, request):
        cart = cart_service.get_or_create_cart_for_request(request)
        return Response(CartSerializer(cart).data)


@extend_schema(request=CartAddItemSerializer, responses=CartSerializer)
class CartItemsView(APIView):
    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "cart"

    def post(self, request):
        serializer = CartAddItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = cart_service.get_or_create_cart_for_request(request)
        variant = _variant_from_payload(serializer.validated_data)
        cart_service.add_item(cart, variant, serializer.validated_data["quantity"])
        cart.refresh_from_db()
        return Response(CartSerializer(cart).data, status=status.HTTP_201_CREATED)

    @extend_schema(request=CartAddItemSerializer, responses=CartSerializer)
    def patch(self, request):
        serializer = CartAddItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = cart_service.get_or_create_cart_for_request(request)
        variant = _variant_from_payload(serializer.validated_data)
        cart_service.set_item_quantity(cart, variant, serializer.validated_data["quantity"])
        cart.refresh_from_db()
        return Response(CartSerializer(cart).data)

    @extend_schema(request=CartAddItemSerializer, responses=CartSerializer)
    def delete(self, request):
        payload = dict(request.data) if request.data else {}
        if not payload.get("variant_id") and not payload.get("sku"):
            variant_id = request.query_params.get("variant_id")
            sku = request.query_params.get("sku")
            if variant_id:
                payload["variant_id"] = variant_id
            elif sku:
                payload["sku"] = sku
        serializer = CartAddItemSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        cart = cart_service.get_or_create_cart_for_request(request)
        variant = _variant_from_payload(serializer.validated_data)
        cart_service.remove_item(cart, variant)
        return Response(CartSerializer(cart).data)


@extend_schema(request=CouponActionSerializer, responses=CartSerializer)
class ApplyCouponView(APIView):
    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "cart"

    def post(self, request):
        serializer = CouponActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = cart_service.get_or_create_cart_for_request(request)
        cart_service.apply_coupon(cart, serializer.validated_data["code"])
        cart.refresh_from_db()
        return Response(CartSerializer(cart).data)


@extend_schema(request=None, responses=CartSerializer)
class RemoveCouponView(APIView):
    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "cart"

    def post(self, request):
        cart = cart_service.get_or_create_cart_for_request(request)
        cart_service.remove_coupon(cart)
        cart.refresh_from_db()
        return Response(CartSerializer(cart).data)


@extend_schema(
    request=BeginCheckoutSerializer,
    responses=CheckoutAttemptSerializer,
    parameters=[_IDEMPOTENCY_HEADER],
)
class CheckoutAttemptsView(APIView):
    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "checkout"

    def post(self, request):
        idempotency_key = require_idempotency_key(request)
        serializer = BeginCheckoutSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        cart = cart_service.get_or_create_cart_for_request(request)

        def produce():
            attempt = begin_checkout(
                cart,
                idempotency_key=idempotency_key,
                contact={"email": data.get("email", ""), "name": data.get("name", "")},
                shipping={
                    "name": data.get("name", ""),
                    "address1": data.get("address1", ""),
                    "address2": data.get("address2", ""),
                    "city": data.get("city", ""),
                    "region": data.get("region", ""),
                    "postal_code": data.get("postal_code", ""),
                    "country": data.get("country", "US"),
                },
                shipping_method=data["shipping_method"],
                expected_subtotal=data.get("expected_subtotal"),
                use_store_credit=data.get("use_store_credit", False),
            )
            return Response(CheckoutAttemptSerializer(attempt).data, status=status.HTTP_201_CREATED)

        return idempotent_write(request, scope="checkout.begin", key=idempotency_key, produce=produce)


@extend_schema(
    request=AuthorizePaymentSerializer,
    responses=PaymentSerializer,
    parameters=[_IDEMPOTENCY_HEADER],
)
class AuthorizePaymentView(APIView):
    """Authorize payment only — confirmation via confirm-payment or inbound PSP webhook."""

    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment"

    def post(self, request, pk):
        idempotency_key = require_idempotency_key(request)
        serializer = AuthorizePaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attempt = _attempt_for_request(request, pk)

        def produce():
            payment = authorize_payment(
                attempt,
                idempotency_key=f"{idempotency_key}:authorize",
                card_token=serializer.validated_data["card_token"],
                mode=serializer.validated_data.get("authorize_mode", "approve"),
            )
            return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)

        return idempotent_write(request, scope="checkout.authorize", key=f"{pk}:{idempotency_key}", produce=produce)


@extend_schema(
    request=ConfirmPaymentSerializer,
    responses=_ORDER_RESPONSE_SCHEMA,
    parameters=[_IDEMPOTENCY_HEADER],
)
class ConfirmPaymentView(APIView):
    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment"

    def post(self, request, pk):
        idempotency_key = require_idempotency_key(request)
        serializer = ConfirmPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        attempt = _attempt_for_request(request, pk)

        def produce():
            payment = authorize_payment(
                attempt,
                idempotency_key=f"{idempotency_key}:authorize",
                card_token=serializer.validated_data["card_token"],
                mode=serializer.validated_data.get("authorize_mode", "approve"),
            )
            order = confirm_payment(
                payment,
                idempotency_key=f"{idempotency_key}:confirm",
                mode=serializer.validated_data.get("confirm_mode", "approve"),
            )
            return Response(_order_response(order), status=status.HTTP_201_CREATED)

        return idempotent_write(request, scope="checkout.confirm", key=f"{pk}:{idempotency_key}", produce=produce)


class OrderListView(APIView):
    permission_classes = [RequireTenant, permissions.IsAuthenticated]
    pagination_class = CreatedAtCursorPagination

    @extend_schema(
        operation_id="orders_list",
        responses=_PAGINATED_ORDER_LIST_SCHEMA,
        parameters=_CURSOR_PARAMS,
    )
    def get(self, request):
        orders = (
            Order.objects.filter(user=request.user, tenant=request.tenant)
            .select_related("fulfillment")
            .prefetch_related("items")
            .order_by("-created_at")
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(orders, request, view=self)
        return paginator.get_paginated_response(OrderSerializer(page, many=True).data)


@extend_schema(responses=_ORDER_RESPONSE_SCHEMA)
class OrderDetailView(APIView):
    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "guest_order"

    def get(self, request, order_number):
        order = _order_for_request(request, order_number)
        return Response(_order_response(order))


@extend_schema(
    request=GuestOrderLookupSerializer,
    responses=GuestOrderLookupResponseSerializer,
)
class GuestOrderLookupView(APIView):
    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "guest_order_lookup"

    def post(self, request):
        serializer = GuestOrderLookupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()
        order_number = serializer.validated_data["order_number"].strip()
        order = Order.objects.filter(order_number=order_number, guest_email__iexact=email, user__isnull=True).first()
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
        return Response({"message": _GUEST_ORDER_LOOKUP_MESSAGE})


@extend_schema(responses=GuestOrderSerializer)
class GuestOrderView(APIView):
    permission_classes = _PUBLIC_API
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "guest_order"

    def get(self, request, order_token):
        order = get_object_or_404(
            Order.objects.select_related("fulfillment").prefetch_related("items"),
            order_token=order_token,
            user__isnull=True,
        )
        return Response(GuestOrderSerializer(order).data)


@extend_schema(responses=_PAGINATED_ORDER_LIST_SCHEMA, parameters=_CURSOR_PARAMS)
class StaffOrderListView(APIView):
    permission_classes = [RequireTenant, IsTenantStaff]
    pagination_class = CreatedAtCursorPagination

    def get(self, request):
        orders = Order.objects.select_related("fulfillment").prefetch_related("items").order_by("-created_at")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(orders, request, view=self)
        return paginator.get_paginated_response(OrderSerializer(page, many=True).data)


@extend_schema(request=StaffTransitionSerializer, responses=OrderSerializer, parameters=[_IDEMPOTENCY_HEADER])
class StaffTransitionView(APIView):
    permission_classes = [RequireTenant, IsTenantStaff]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "refund"

    def post(self, request, pk):
        idempotency_key = require_idempotency_key(request)
        serializer = StaffTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_object_or_404(Order, pk=pk)

        def produce():
            transition_fulfillment(order, actor=request.user, **serializer.validated_data)
            order.refresh_from_db()
            return Response(OrderSerializer(order).data)

        return idempotent_write(request, scope="staff.transition", key=f"{pk}:{idempotency_key}", produce=produce)


@extend_schema(request=RefundSerializer, responses=RefundResponseSerializer, parameters=[_IDEMPOTENCY_HEADER])
class StaffRefundView(APIView):
    permission_classes = [RequireTenant, IsTenantManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "refund"

    def post(self, request, pk):
        idempotency_key = require_idempotency_key(request)
        serializer = RefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_object_or_404(Order, pk=pk)

        def produce():
            refund = create_refund(
                order, idempotency_key=idempotency_key, actor=request.user, **serializer.validated_data
            )
            return Response({"id": refund.pk, "status": refund.status, "amount": str(refund.amount)})

        return idempotent_write(request, scope="refund", key=f"{pk}:{idempotency_key}", produce=produce)


@extend_schema(request=StaffCancelSerializer, responses=OrderSerializer, parameters=[_IDEMPOTENCY_HEADER])
class StaffCancelView(APIView):
    permission_classes = [RequireTenant, IsTenantManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "refund"

    def post(self, request, pk):
        idempotency_key = require_idempotency_key(request)
        serializer = StaffCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_object_or_404(Order, pk=pk)
        data = serializer.validated_data

        def produce():
            cancelled = cancel_order(
                order,
                actor=request.user,
                note=data.get("note", ""),
                restock=data.get("restock", False),
            )
            return Response(OrderSerializer(cancelled).data)

        return idempotent_write(request, scope="staff.cancel", key=f"{pk}:{idempotency_key}", produce=produce)


@extend_schema(
    request=InventoryAdjustmentSerializer,
    responses=InventoryAdjustmentResponseSerializer,
    parameters=[_IDEMPOTENCY_HEADER],
)
class InventoryAdjustmentView(APIView):
    permission_classes = [RequireTenant, IsTenantManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "refund"

    def post(self, request):
        idempotency_key = require_idempotency_key(request)
        serializer = InventoryAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        variant_id = serializer.validated_data["variant_id"]

        def produce():
            variant = get_object_or_404(ProductVariant, pk=variant_id)
            movement = adjust_stock(
                variant,
                serializer.validated_data["delta"],
                actor=request.user,
                note=serializer.validated_data.get("note", ""),
            )
            return Response({"id": movement.pk, "quantity_delta": movement.quantity_delta})

        return idempotent_write(
            request,
            scope="inventory.adjust",
            key=f"{variant_id}:{idempotency_key}",
            produce=produce,
        )


@extend_schema(request=None, responses=OrderSerializer, parameters=[_IDEMPOTENCY_HEADER])
class StaffCheckoutReplayView(APIView):
    permission_classes = [RequireTenant, IsTenantManager]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "refund"

    def post(self, request, pk):
        idempotency_key = require_idempotency_key(request)
        attempt = get_object_or_404(CheckoutAttempt, pk=pk)

        def produce():
            order = replay_finalization(attempt)
            return Response(OrderSerializer(order).data)

        return idempotent_write(request, scope="staff.replay", key=f"{pk}:{idempotency_key}", produce=produce)


def _variant_from_payload(payload):
    if payload.get("variant_id"):
        return get_object_or_404(ProductVariant.objects.select_related("product"), pk=payload["variant_id"])
    return get_object_or_404(
        ProductVariant.objects.select_related("product"),
        sku=payload["sku"],
    )


def _attempt_for_request(request, pk):
    queryset = CheckoutAttempt.objects.select_related("cart", "user")
    if request.user.is_authenticated:
        return get_object_or_404(queryset, pk=pk, user=request.user)
    session_key = request.session.session_key or ""
    return get_object_or_404(queryset, pk=pk, session_key=session_key)


def _order_for_request(request, order_number):
    queryset = Order.objects.select_related("fulfillment").prefetch_related("items")
    order = queryset.filter(order_number=order_number).first()
    if order is None:
        raise PermissionDenied("You do not have access to this order.")
    if request.user.is_authenticated and order.user_id == request.user.pk:
        return order
    token = request.GET.get("token", "")
    if order.user_id is None:
        if token and str(order.order_token) == token:
            return order
        session_key = request.session.session_key or ""
        if session_key and order.checkout_attempt_id and order.checkout_attempt.session_key == session_key:
            return order
    raise PermissionDenied("You do not have access to this order.")


class PaymentWebhookView(APIView):
    """Inbound PSP webhook receiver (simulated provider supported for integration tests)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment"

    def post(self, request, provider: str):
        signature = request.headers.get("X-Payment-Signature", "")
        tenant_id = request.headers.get("X-Tenant-ID")
        tid = int(tenant_id) if tenant_id and tenant_id.isdigit() else None
        try:
            record = receive_gateway_webhook(
                provider=provider,
                body=request.body,
                signature=signature,
                tenant_id=tid,
            )
        except PaymentGatewayError as exc:
            code = getattr(exc, "code", "webhook_error")
            status_code = status.HTTP_403_FORBIDDEN if code == "invalid_signature" else status.HTTP_400_BAD_REQUEST
            return Response({"code": code, "message": str(exc)}, status=status_code)
        return Response(
            {"id": record.pk, "status": record.status, "processing_result": record.processing_result},
            status=status.HTTP_202_ACCEPTED,
        )
