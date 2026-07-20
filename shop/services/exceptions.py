"""Typed CommerceError hierarchy for cart, checkout, payment, and plan limit failures"""
class CommerceError(Exception):
    code = "commerce_error"

    def __init__(self, message: str, *, code: str | None = None, field_errors: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code or self.code
        self.field_errors = field_errors or {}

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "field_errors": self.field_errors,
        }


class CartError(CommerceError):
    code = "cart_error"


class OutOfStock(CommerceError):
    code = "out_of_stock"


class InvalidCoupon(CommerceError):
    code = "invalid_coupon"


class CheckoutStateError(CommerceError):
    code = "checkout_state_error"


class PaymentGatewayError(CommerceError):
    code = "payment_gateway_error"


class PermissionDenied(CommerceError):
    code = "permission_denied"


class IdempotencyInProgress(CommerceError):
    code = "idempotency_in_progress"


class IdempotencyKeyReuseMismatch(CommerceError):
    code = "idempotency_key_reuse_mismatch"


class GiftCardError(CommerceError):
    code = "gift_card_error"


class PlanLimitError(CommerceError):
    code = "plan_limit_reached"
