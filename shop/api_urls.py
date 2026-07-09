# DRF router and REST URL patterns for catalog, cart, checkout, and staff endpoints
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import api

router = DefaultRouter()
router.register("catalog/products", api.ProductViewSet, basename="api-products")
router.register("catalog/categories", api.CategoryViewSet, basename="api-categories")
router.register("catalog/collections", api.CollectionViewSet, basename="api-collections")

urlpatterns = [
    path("", include(router.urls)),
    path("cart/", api.CartView.as_view(), name="api-cart"),
    path("cart/items/", api.CartItemsView.as_view(), name="api-cart-items"),
    path("cart/apply-coupon/", api.ApplyCouponView.as_view(), name="api-cart-apply-coupon"),
    path("cart/remove-coupon/", api.RemoveCouponView.as_view(), name="api-cart-remove-coupon"),
    path("checkout/attempts/", api.CheckoutAttemptsView.as_view(), name="api-checkout-attempts"),
    path(
        "checkout/attempts/<int:pk>/confirm-payment/",
        api.ConfirmPaymentView.as_view(),
        name="api-checkout-confirm-payment",
    ),
    path("orders/", api.OrderListView.as_view(), name="api-orders"),
    path("orders/<str:order_number>/", api.OrderDetailView.as_view(), name="api-order-detail"),
    path("guest/orders/<uuid:order_token>/", api.GuestOrderView.as_view(), name="api-guest-order"),
    path("staff/orders/", api.StaffOrderListView.as_view(), name="api-staff-orders"),
    path("staff/orders/<int:pk>/transition/", api.StaffTransitionView.as_view(), name="api-staff-order-transition"),
    path("staff/orders/<int:pk>/refund/", api.StaffRefundView.as_view(), name="api-staff-order-refund"),
    path("staff/orders/<int:pk>/cancel/", api.StaffCancelView.as_view(), name="api-staff-order-cancel"),
    path("staff/inventory/adjustments/", api.InventoryAdjustmentView.as_view(), name="api-staff-inventory-adjust"),
    path(
        "staff/checkout-attempts/<int:pk>/replay-finalization/",
        api.StaffCheckoutReplayView.as_view(),
        name="api-staff-checkout-replay",
    ),
]
