from __future__ import annotations

import json

from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status, viewsets
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework.views import exception_handler as drf_exception_handler

try:
    from drf_spectacular.utils import extend_schema
except ImportError:  # schema generation is optional

    def extend_schema(**_kwargs):
        def decorator(cls):
            return cls

        return decorator


from .models import Category, CheckoutAttempt, Collection, Order, Product, ProductVariant
from .serializers import (
    BeginCheckoutSerializer,
    CartAddItemSerializer,
    CartSerializer,
    CategorySerializer,
    CheckoutAttemptSerializer,
    CollectionSerializer,
    ConfirmPaymentSerializer,
    CouponActionSerializer,
    InventoryAdjustmentSerializer,
    OrderSerializer,
    ProductSerializer,
    RefundSerializer,
    StaffTransitionSerializer,
)
from .services import cart as cart_service
from .services import idempotency
from .services.checkout import begin_checkout, replay_finalization
from .services.exceptions import CommerceError, PermissionDenied
from .services.inventory import adjust_stock, variants_with_availability
from .services.orders import cancel_order, transition_fulfillment
from .services.payments import authorize_payment, confirm_payment
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


COMMERCE_ERROR_STATUS = {
    "out_of_stock": status.HTTP_409_CONFLICT,
    "checkout_state_error": status.HTTP_409_CONFLICT,
    "checkout_attempt_terminal": status.HTTP_409_CONFLICT,
    "compensation_required": status.HTTP_409_CONFLICT,
    "idempotency_in_progress": status.HTTP_409_CONFLICT,
    "payment_failed": status.HTTP_402_PAYMENT_REQUIRED,
    "payment_gateway_error": status.HTTP_402_PAYMENT_REQUIRED,
    "permission_denied": status.HTTP_403_FORBIDDEN,
}


def exception_handler(exc, context):
    if isinstance(exc, CommerceError):
        return Response(
            exc.as_dict(),
            status=COMMERCE_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
        )
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = {
            "code": "api_error",
            "message": "Request failed.",
            "field_errors": response.data,
        }
    return response


def require_idempotency_key(request) -> str:
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key:
        raise CommerceError("Idempotency-Key header is required.", code="idempotency_key_required")
    return key


def _json_safe(data):
    return json.loads(JSONRenderer().render(data)) if data is not None else {}


def idempotent_write(request, *, scope: str, key: str, produce):
    """Run ``produce`` under an IdempotencyRecord (spec §21 / ADR-0017).

    Replays return the winner's stored response; a still-running twin gets a clean
    ``409`` via IdempotencyInProgress raised by ``idempotency.begin``.
    """
    actor = request.user if request.user.is_authenticated else None
    session_key = request.session.session_key or ""
    # Derive the payload fingerprint from parsed data (reading request.body after DRF
    # has consumed the stream raises RawPostDataException).
    payload = json.dumps(request.data, sort_keys=True, default=str)
    record = idempotency.begin(scope, key, user=actor, session_key=session_key, payload=payload)
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
            status=COMMERCE_ERROR_STATUS.get(exc.code, status.HTTP_400_BAD_REQUEST),
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


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
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
                Q(name__icontains=search)
                | Q(description__icontains=search)
                | Q(variants__sku__icontains=search)
            )
        if category:
            queryset = queryset.filter(category__slug=category)
        if collection:
            queryset = queryset.filter(collections__slug=collection)
        if min_price:
            queryset = queryset.filter(variants__price__gte=min_price)
        if max_price:
            queryset = queryset.filter(variants__price__lte=max_price)
        return queryset.distinct()


class CategoryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CategorySerializer
    lookup_field = "slug"
    queryset = Category.objects.all()


class CollectionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CollectionSerializer
    lookup_field = "slug"
    queryset = Collection.objects.filter(active=True)


@extend_schema(responses=CartSerializer)
class CartView(APIView):
    def get(self, request):
        cart = cart_service.get_or_create_cart_for_request(request)
        return Response(CartSerializer(cart).data)


@extend_schema(request=CartAddItemSerializer, responses=CartSerializer)
class CartItemsView(APIView):
    def post(self, request):
        serializer = CartAddItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = cart_service.get_or_create_cart_for_request(request)
        variant = _variant_from_payload(serializer.validated_data)
        cart_service.add_item(cart, variant, serializer.validated_data["quantity"])
        cart.refresh_from_db()
        return Response(CartSerializer(cart).data, status=status.HTTP_201_CREATED)

    def patch(self, request):
        serializer = CartAddItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = cart_service.get_or_create_cart_for_request(request)
        variant = _variant_from_payload(serializer.validated_data)
        cart_service.set_item_quantity(cart, variant, serializer.validated_data["quantity"])
        cart.refresh_from_db()
        return Response(CartSerializer(cart).data)

    def delete(self, request):
        serializer = CartAddItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = cart_service.get_or_create_cart_for_request(request)
        variant = _variant_from_payload(serializer.validated_data)
        cart_service.remove_item(cart, variant)
        return Response(CartSerializer(cart).data)


@extend_schema(request=CouponActionSerializer, responses=CartSerializer)
class ApplyCouponView(APIView):
    def post(self, request):
        serializer = CouponActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        cart = cart_service.get_or_create_cart_for_request(request)
        cart_service.apply_coupon(cart, serializer.validated_data["code"])
        cart.refresh_from_db()
        return Response(CartSerializer(cart).data)


@extend_schema(request=None, responses=CartSerializer)
class RemoveCouponView(APIView):
    def post(self, request):
        cart = cart_service.get_or_create_cart_for_request(request)
        cart_service.remove_coupon(cart)
        cart.refresh_from_db()
        return Response(CartSerializer(cart).data)


@extend_schema(request=BeginCheckoutSerializer, responses=CheckoutAttemptSerializer)
class CheckoutAttemptsView(APIView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "checkout"

    def post(self, request):
        idempotency_key = require_idempotency_key(request)
        serializer = BeginCheckoutSerializer(data=request.data)
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


@extend_schema(request=ConfirmPaymentSerializer, responses=OrderSerializer)
class ConfirmPaymentView(APIView):
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
                mode=serializer.validated_data["authorize_mode"],
            )
            order = confirm_payment(
                payment,
                idempotency_key=f"{idempotency_key}:confirm",
                mode=serializer.validated_data["confirm_mode"],
            )
            return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)

        return idempotent_write(
            request, scope="checkout.confirm", key=f"{pk}:{idempotency_key}", produce=produce
        )


class OrderListView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(operation_id="orders_list", responses=OrderSerializer(many=True))
    def get(self, request):
        orders = (
            Order.objects.filter(user=request.user)
            .select_related("fulfillment")
            .prefetch_related("items")
            .order_by("-created_at")
        )
        return Response(OrderSerializer(orders, many=True).data)


@extend_schema(responses=OrderSerializer)
class OrderDetailView(APIView):
    def get(self, request, order_number):
        order = _order_for_request(request, order_number)
        return Response(OrderSerializer(order).data)


@extend_schema(responses=OrderSerializer)
class GuestOrderView(APIView):
    def get(self, request, order_token):
        order = get_object_or_404(
            Order.objects.select_related("fulfillment").prefetch_related("items"),
            order_token=order_token,
        )
        return Response(OrderSerializer(order).data)


@extend_schema(responses=OrderSerializer(many=True))
class StaffOrderListView(APIView):
    permission_classes = [IsTenantStaff]

    def get(self, request):
        orders = Order.objects.select_related("fulfillment").prefetch_related("items").order_by("-created_at")
        return Response(OrderSerializer(orders, many=True).data)


@extend_schema(request=StaffTransitionSerializer, responses=OrderSerializer)
class StaffTransitionView(APIView):
    permission_classes = [IsTenantStaff]

    def post(self, request, pk):
        serializer = StaffTransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = get_object_or_404(Order, pk=pk)
        fulfillment = transition_fulfillment(order, actor=request.user, **serializer.validated_data)
        return Response({"status": fulfillment.status})


@extend_schema(request=RefundSerializer, responses=OrderSerializer)
class StaffRefundView(APIView):
    permission_classes = [IsTenantManager]
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

        return idempotent_write(
            request, scope="refund", key=f"{pk}:{idempotency_key}", produce=produce
        )


@extend_schema(request=None, responses=OrderSerializer)
class StaffCancelView(APIView):
    permission_classes = [IsTenantManager]

    def post(self, request, pk):
        order = get_object_or_404(Order, pk=pk)
        order = cancel_order(
            order,
            actor=request.user,
            note=request.data.get("note", ""),
            restock=bool(request.data.get("restock", False)),
        )
        return Response(OrderSerializer(order).data)


@extend_schema(request=InventoryAdjustmentSerializer, responses=InventoryAdjustmentSerializer)
class InventoryAdjustmentView(APIView):
    permission_classes = [IsTenantManager]

    def post(self, request):
        serializer = InventoryAdjustmentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        variant = get_object_or_404(ProductVariant, pk=serializer.validated_data["variant_id"])
        movement = adjust_stock(
            variant,
            serializer.validated_data["delta"],
            actor=request.user,
            note=serializer.validated_data.get("note", ""),
        )
        return Response({"id": movement.pk, "quantity_delta": movement.quantity_delta})


@extend_schema(request=None, responses=OrderSerializer)
class StaffCheckoutReplayView(APIView):
    permission_classes = [IsTenantManager]

    def post(self, request, pk):
        attempt = get_object_or_404(CheckoutAttempt, pk=pk)
        order = replay_finalization(attempt)
        return Response(OrderSerializer(order).data)


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
    order = get_object_or_404(queryset, order_number=order_number)
    if request.user.is_authenticated and order.user_id == request.user.pk:
        return order
    if not request.user.is_authenticated and str(order.order_token) == request.GET.get("token", ""):
        return order
    raise PermissionDenied("You do not have access to this order.")
