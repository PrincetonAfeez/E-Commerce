"""Payment gateway package exports and factory entry point."""
from .factory import get_payment_gateway
from .protocol import AUTHORIZED, CONFIRMED, FAILED, GatewayResult, PaymentGateway
from .simulated import SimulatedPaymentGateway

__all__ = [
    "AUTHORIZED",
    "CONFIRMED",
    "FAILED",
    "GatewayResult",
    "PaymentGateway",
    "SimulatedPaymentGateway",
    "get_payment_gateway",
]
