"""URL routes for storefront pages: catalog, cart, checkout, orders, reviews, and staff"""
from django.urls import include, path
from django.views.decorators.http import require_http_methods

from . import views

catalog_patterns = (
    [
        path("", views.catalog_list, name="list"),
        path("products/<slug:slug>/", views.product_detail, name="detail"),
    ],
    "catalog",
)

cart_patterns = (
    [
        path("", views.cart_detail, name="detail"),
        path("add/", views.add_to_cart, name="add"),
        path("update/", views.update_cart_item, name="update"),
        path("coupon/apply/", views.apply_coupon, name="apply_coupon"),
        path("coupon/remove/", views.remove_coupon, name="remove_coupon"),
    ],
    "cart",
)

account_patterns = (
    [
        path("addresses/", views.address_list, name="addresses"),
        path("addresses/add/", views.address_create, name="address_create"),
        path("addresses/<int:pk>/delete/", views.address_delete, name="address_delete"),
        path("addresses/<int:pk>/default/", views.address_make_default, name="address_default"),
        path("wishlist/", views.wishlist_view, name="wishlist"),
        path("wishlist/toggle/", views.wishlist_toggle, name="wishlist_toggle"),
        path("store-credit/", views.store_credit_view, name="store_credit"),
        path("subscriptions/", views.subscriptions_view, name="subscriptions"),
        path("subscriptions/<int:pk>/cancel/", views.cancel_subscription, name="cancel_subscription"),
        path("privacy/export/", views.account_data_export, name="data_export"),
        path("privacy/delete/", views.account_delete, name="delete"),
    ],
    "account",
)

checkout_patterns = (
    [
        path("", views.checkout_start, name="start"),
        path("<int:pk>/payment/", views.checkout_payment, name="payment"),
        path("complete/<str:order_number>/", views.checkout_complete, name="complete"),
    ],
    "checkout",
)

order_patterns = (
    [
        path("", views.order_history, name="history"),
        path("lookup/", views.guest_order_lookup, name="lookup"),
        path("<str:order_number>/", views.order_detail, name="detail"),
        path("<str:order_number>/reorder/", views.reorder, name="reorder"),
        path("<str:order_number>/return/", views.order_return, name="return"),
    ],
    "orders",
)

review_patterns = (
    [
        path("products/<slug:slug>/review/", views.submit_review, name="submit"),
    ],
    "reviews",
)

staff_patterns = (
    [
        path("", views.staff_dashboard, name="dashboard"),
        path("settings/", views.staff_settings, name="settings"),
        path("billing/", views.staff_billing, name="billing"),
        path("team/", views.staff_team, name="team"),
        path("team/invite/", views.staff_invite_member, name="team_invite"),
        path("team/<int:pk>/remove/", views.staff_remove_member, name="team_remove"),
        path("low-stock/", views.staff_low_stock, name="low_stock"),
        path("orders/", views.staff_order_queue, name="queue"),
        path("orders/<int:pk>/", views.staff_order_detail, name="order_detail"),
        path("orders/<int:pk>/transition/", views.staff_transition_order, name="transition"),
        path("orders/<int:pk>/refund/", require_http_methods(["POST"])(views.staff_refund_order), name="refund"),
        path("orders/<int:pk>/cancel/", require_http_methods(["POST"])(views.staff_cancel_order), name="cancel"),
        path("returns/", views.staff_returns, name="returns"),
        path("returns/<int:pk>/approve/", views.staff_approve_return, name="return_approve"),
        path("returns/<int:pk>/reject/", views.staff_reject_return, name="return_reject"),
    ],
    "staff_ops",
)

urlpatterns = [
    path("", include(catalog_patterns)),
    path("cart/", include(cart_patterns)),
    path("account/", include(account_patterns)),
    path("checkout/", include(checkout_patterns)),
    path("orders/", include(order_patterns)),
    path("reviews/", include(review_patterns)),
    path("staff/", include(staff_patterns)),
]
