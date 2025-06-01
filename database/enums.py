# database/enums.py
import enum

class SubscriptionStatus(enum.Enum):
    """Represents the status of a user's subscription."""
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    GRACE_PERIOD = "grace_period"
    TRIAL = "trial"
    PENDING_PAYMENT = "pending_payment" # Если используется для ожидания платежа

class SubscriptionTier(enum.Enum):
    """Defines the available subscription tiers."""
    FREE = "free"
    BASIC = "basic"
    PREMIUM = "premium"
    VIP = "vip"

# Можно добавить другие Enum, если они будут использоваться в нескольких модулях,
# например, MemoryType, MemoryPriority, если они не вызывают циклических импортов
# в их текущем расположении.
