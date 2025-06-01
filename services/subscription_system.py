# services/subscription_system.py
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum as PythonEnum # Используем псевдоним, чтобы не конфликтовать с SQLAlchemyEnum в models
from typing import Dict, Any, Optional, List, Tuple, Callable
from dataclasses import dataclass, field
import json

from aiogram import BaseMiddleware
from aiogram import types # For types.User
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.markdown import hbold, hitalic

from database.operations import DatabaseService
from database.models import User as DBUser, Subscription as DBSubscription # Импортируем модель Subscription
from database.enums import SubscriptionTier, SubscriptionStatus # <--- ИМПОРТ ИЗ НОВОГО ФАЙЛА ENUMS
from config.settings import BotConfig
from utils.error_handler import handle_errors
from utils.caching import async_ttl_cache # Убедимся, что используется правильный декоратор
from cachetools import TTLCache

# from services.notification_marketing_system import NotificationService # Type hinting

logger = logging.getLogger(__name__)

# --- Dataclasses (TierLimits остается, UserSubscription может быть упрощен или изменен) ---
@dataclass
class TierLimits:
    """Defines limits and features for a specific subscription tier."""
    tier_name: str
    price_stars_monthly: int
    price_stars_yearly: int
    daily_messages: int = 50
    memory_type: str = "session" # 'session', 'short_term', 'long_term', 'permanent'
    max_memory_entries: int = 20
    memory_retention_days: int = 1 # 0 для сессии, -1 для перманентной
    voice_messages_allowed: bool = False
    max_voice_duration_sec: int = 60
    custom_fantasies_allowed: bool = False
    max_fantasy_length_chars: int = 1000
    ai_insights_access: bool = False
    personas_access: List[str] = field(default_factory=lambda: ["diana_friend"])
    sexting_max_level: int = 0
    priority_support: bool = False
    trial_days_available: int = 0 # Сколько дней триала можно получить для этого тарифа (если применимо)
    additional_features: Dict[str, Any] = field(default_factory=dict)

@dataclass
class UserSubscriptionData: # Переименовано для ясности, это данные, которые могут храниться в UserPreference
    """Represents user's supplementary subscription data, primarily usage and bonuses, stored in UserPreference."""
    user_id_tg: int # Для идентификации, хотя user_id_db будет основным ключом в UserPreference
    
    # Данные, которые все еще могут храниться в UserPreference (JSON blob)
    usage: Dict[str, Any] = field(default_factory=lambda: {
        "daily_messages_used": 0,
        "last_message_date": None, # ISO date string ГГГГ-ММ-ДД
        "bonus_messages_total": 0,
        "bonus_messages_remaining": 0,
        "bonus_expiry_date": None # ISO format string
    })
    # Можно добавить другие флаги или мелкие настройки, не относящиеся к основной записи Subscription
    # Например, `has_seen_upgrade_prompt_for_tier_X: bool`

    def to_json(self) -> str:
        """Serializes to JSON string."""
        return json.dumps(self.__dict__, ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, json_str: str) -> 'UserSubscriptionData':
        """Deserializes from JSON string."""
        data = json.loads(json_str)
        return cls(**data)

class SubscriptionPlans:
    """Configuration for subscription plans and their hierarchy."""
    TIER_HIERARCHY: Dict[SubscriptionTier, int] = {
        SubscriptionTier.FREE: 0, SubscriptionTier.BASIC: 1,
        SubscriptionTier.PREMIUM: 2, SubscriptionTier.VIP: 3,
    }
    # Цены и лимиты остаются здесь как конфигурация по умолчанию
    DEFAULT_PLANS: Dict[SubscriptionTier, TierLimits] = {
        SubscriptionTier.FREE: TierLimits(
            tier_name="Free", price_stars_monthly=0, price_stars_yearly=0, daily_messages=20,
            memory_type="session", max_memory_entries=10, memory_retention_days=0,
            personas_access=["aeris_friend"], sexting_max_level=0),
        SubscriptionTier.BASIC: TierLimits(
            tier_name="Basic", price_stars_monthly=100, price_stars_yearly=1000, daily_messages=100,
            memory_type="short_term", max_memory_entries=50, memory_retention_days=7,
            voice_messages_allowed=True, max_voice_duration_sec=120, custom_fantasies_allowed=True,
            max_fantasy_length_chars=2000, personas_access=["aeris_friend", "luneth_basic"],
            sexting_max_level=5, trial_days_available=3),
        SubscriptionTier.PREMIUM: TierLimits(
            tier_name="Premium", price_stars_monthly=250, price_stars_yearly=2500, daily_messages=500,
            memory_type="long_term", max_memory_entries=200, memory_retention_days=30,
            voice_messages_allowed=True, max_voice_duration_sec=300, custom_fantasies_allowed=True,
            max_fantasy_length_chars=5000, ai_insights_access=True,
            personas_access=["aeris_friend", "aeris_companion", "luneth_basic", "luneth_advanced"],
            sexting_max_level=8, priority_support=True, trial_days_available=7),
        SubscriptionTier.VIP: TierLimits(
            tier_name="VIP", price_stars_monthly=500, price_stars_yearly=5000, daily_messages=-1, # -1 for unlimited
            memory_type="permanent", max_memory_entries=-1, memory_retention_days=-1,
            voice_messages_allowed=True, max_voice_duration_sec=-1, custom_fantasies_allowed=True,
            max_fantasy_length_chars=-1, ai_insights_access=True, personas_access=["all"], # "all" - специальный маркер
            sexting_max_level=10, priority_support=True,
            additional_features={"early_access_new_features": True, "custom_persona_requests": 1},
            trial_days_available=0 ), # VIP обычно не имеет триала, но можно настроить
    }
    def __init__(self, custom_plans: Optional[Dict[SubscriptionTier, TierLimits]] = None):
        self.PLANS = self.DEFAULT_PLANS.copy()
        if custom_plans: self.PLANS.update(custom_plans)

class SubscriptionService:
    """Manages user subscriptions, tiers, limits, and activation processes using the Subscription table."""
    USER_PREFERENCE_SUBSCRIPTION_USAGE_KEY = "subscription_usage_data" # Новый ключ для UserPreference
    USER_PREFERENCE_PERSONA_SYSTEM = "system" # Для системных настроек пользователя

    def __init__(self, db_service: DatabaseService, config: BotConfig, bot_instance: Optional[Any] = None):
        self.db_service = db_service
        self.config = config
        self.bot_instance = bot_instance # AICompanionBot instance
        self.plans = SubscriptionPlans(custom_plans=getattr(config, 'custom_subscription_plans', None))
        # Кэш для UserSubscriptionData (usage)
        self.usage_data_cache = TTLCache(maxsize=1000, ttl=60)


    async def _get_or_create_db_user(self, user_id_tg: int) -> Optional[DBUser]:
        """Helper to get or create DBUser. Returns None if creation fails critically."""
        user_db = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if user_db:
            return user_db
        
        logger.warning(f"DBUser for TG ID {user_id_tg} not found. Attempting to create for subscription context.")
        if not (self.bot_instance and hasattr(self.bot_instance, 'bot') and self.bot_instance.bot):
            logger.error(f"Cannot create user for TG ID {user_id_tg}: bot_instance or bot is unavailable.")
            return None
        try:
            # Пытаемся получить информацию о пользователе из Telegram API
            # Важно: get_chat может вернуть Chat, а не User, если это бот или канал.
            # Для реальных пользователей это должен быть User.
            aiogram_user_info = await self.bot_instance.bot.get_chat(user_id_tg)
            
            # Проверяем, что это действительно пользователь, а не бот/канал
            if isinstance(aiogram_user_info, types.User):
                user_db = await self.db_service.get_or_create_user(
                    telegram_id=user_id_tg, username=aiogram_user_info.username,
                    first_name=aiogram_user_info.first_name, last_name=aiogram_user_info.last_name,
                    language_code=aiogram_user_info.language_code
                )
                if user_db: 
                    logger.info(f"DBUser for TG ID {user_id_tg} created by SubscriptionService.")
                return user_db # get_or_create_user уже создает Free подписку
            else:
                logger.error(f"Failed to get Aiogram User object for TG ID {user_id_tg}. Type: {type(aiogram_user_info)}")
                return None
        except Exception as e:
            logger.error(f"Error fetching/creating user info for TG ID {user_id_tg}: {e}", exc_info=True)
            return None

    async def get_user_subscription(self, user_id_tg: int) -> Dict[str, Any]:
        """
        Retrieves user's full subscription data, combining DBSubscription and UserPreference (usage).
        Returns a dictionary representation.
        """
        user_db = await self._get_or_create_db_user(user_id_tg)
        if not user_db:
            # Возвращаем структуру по умолчанию для Free, если пользователь не может быть создан
            free_tier_limits = self.plans.PLANS[SubscriptionTier.FREE]
            return {
                "user_id_tg": user_id_tg, "tier": SubscriptionTier.FREE.value, "tier_name": free_tier_limits.tier_name,
                "status": SubscriptionStatus.ACTIVE.value, "activated_at": None, "expires_at": None,
                "is_trial": False, "trial_source": None, "payment_provider": None, "telegram_charge_id": None,
                "auto_renewal": False, "original_tier_before_expiry": None,
                "usage": {"daily_messages_used": 0, "last_message_date": None, "bonus_messages_total": 0, "bonus_messages_remaining": 0},
                "limits": free_tier_limits
            }

        # 1. Получаем основную информацию о подписке из таблицы Subscription
        active_db_sub: Optional[DBSubscription] = await self.db_service.get_active_subscription_for_user(user_db.id)
        
        # Проверяем и обновляем статус, если подписка истекла
        changed_in_validation = False
        if active_db_sub:
            changed_in_validation, _ = await self._validate_and_update_db_subscription_status(active_db_sub)
            if changed_in_validation: # Если статус изменился (например, на EXPIRED), перезапрашиваем
                active_db_sub = await self.db_service.get_active_subscription_for_user(user_db.id)

        # 2. Если активной подписки нет (или она только что истекла и стала неактивной), создаем/возвращаем Free
        if not active_db_sub:
            # Проверяем, есть ли вообще Free подписка. Если нет - создаем.
            free_sub_check = await self.db_service.get_user_subscription_by_tier(user_db.id, SubscriptionTier.FREE)
            if not free_sub_check:
                active_db_sub = DBSubscription(
                    user_id=user_db.id, tier=SubscriptionTier.FREE, status=SubscriptionStatus.ACTIVE,
                    activated_at=datetime.now(timezone.utc)
                )
                active_db_sub = await self.db_service.save_subscription(active_db_sub)
            else:
                active_db_sub = free_sub_check # Используем существующую Free
                if active_db_sub.status != SubscriptionStatus.ACTIVE: # Активируем, если была неактивна
                    active_db_sub.status = SubscriptionStatus.ACTIVE
                    active_db_sub.expires_at = None
                    active_db_sub = await self.db_service.save_subscription(active_db_sub)


        # 3. Получаем данные об использовании (usage) из UserPreference
        usage_data_dict = await self._get_usage_data(user_db.id, user_id_tg)
        
        # 4. Собираем итоговый результат
        current_tier_enum = active_db_sub.tier if active_db_sub else SubscriptionTier.FREE
        tier_limits = self.plans.PLANS.get(current_tier_enum, self.plans.PLANS[SubscriptionTier.FREE])
        
        # Обновляем daily_messages_used, если сменилась дата
        now_date_iso = datetime.now(timezone.utc).date().isoformat()
        if usage_data_dict.usage.get("last_message_date") != now_date_iso:
            usage_data_dict.usage["daily_messages_used"] = 0
            usage_data_dict.usage["last_message_date"] = now_date_iso
            await self._save_usage_data(user_db.id, usage_data_dict) # Сохраняем обновленное usage

        # Проверка истечения бонусных сообщений
        bonus_expiry_str = usage_data_dict.usage.get("bonus_expiry_date")
        if bonus_expiry_str:
            try:
                bonus_expiry_dt = datetime.fromisoformat(bonus_expiry_str.replace('Z','+00:00'))
                if bonus_expiry_dt.tzinfo is None: bonus_expiry_dt = bonus_expiry_dt.replace(tzinfo=timezone.utc)
                if bonus_expiry_dt < datetime.now(timezone.utc) and usage_data_dict.usage.get("bonus_messages_remaining", 0) > 0:
                    logger.info(f"Bonus messages expired for user DB ID {user_db.id}.")
                    usage_data_dict.usage["bonus_messages_remaining"] = 0
                    usage_data_dict.usage["bonus_messages_total"] = 0 # Можно сбрасывать и total, или оставить для истории
                    usage_data_dict.usage["bonus_expiry_date"] = None
                    await self._save_usage_data(user_db.id, usage_data_dict)
            except ValueError:
                logger.warning(f"Invalid bonus_expiry_date format '{bonus_expiry_str}' for user DB ID {user_db.id}.")
                usage_data_dict.usage["bonus_expiry_date"] = None; await self._save_usage_data(user_db.id, usage_data_dict)

        return {
            "user_id_tg": user_id_tg,
            "db_user_id": user_db.id, # Добавим для удобства
            "tier": active_db_sub.tier.value if active_db_sub else SubscriptionTier.FREE.value,
            "tier_name": tier_limits.tier_name,
            "status": active_db_sub.status.value if active_db_sub else SubscriptionStatus.ACTIVE.value,
            "activated_at": active_db_sub.activated_at.isoformat() if active_db_sub and active_db_sub.activated_at else None,
            "expires_at": active_db_sub.expires_at.isoformat() if active_db_sub and active_db_sub.expires_at else None,
            "is_trial": active_db_sub.is_trial if active_db_sub else False,
            "trial_source": active_db_sub.trial_source if active_db_sub else None,
            "payment_provider": active_db_sub.payment_provider if active_db_sub else None,
            "telegram_charge_id": active_db_sub.telegram_charge_id if active_db_sub else None,
            "auto_renewal": active_db_sub.auto_renewal if active_db_sub else False,
            "original_tier_before_expiry": active_db_sub.original_tier_before_expiry.value if active_db_sub and active_db_sub.original_tier_before_expiry else None,
            "usage": usage_data_dict.usage,
            "limits": tier_limits
        }

    async def _validate_and_update_db_subscription_status(self, db_sub: DBSubscription) -> Tuple[bool, Optional[SubscriptionTier]]:
        """Validates DBSubscription status, downgrades if expired. Returns (changed, old_tier_if_downgraded)."""
        changed = False
        old_tier_if_downgraded: Optional[SubscriptionTier] = None
        now_utc = datetime.now(timezone.utc)

        if db_sub.tier == SubscriptionTier.FREE:
            if db_sub.status != SubscriptionStatus.ACTIVE: db_sub.status = SubscriptionStatus.ACTIVE; changed = True
            if db_sub.expires_at is not None: db_sub.expires_at = None; changed = True
            return changed, None

        if not db_sub.expires_at: # Платный тариф без даты истечения - ошибка
            logger.warning(f"Paid tier {db_sub.tier.value} for user DB ID {db_sub.user_id} has no expiry. Downgrading to Free.")
            old_tier_if_downgraded = db_sub.tier
            self._downgrade_db_sub_to_free(db_sub, "missing_expiry_for_paid_tier")
            await self.db_service.save_subscription(db_sub)
            return True, old_tier_if_downgraded
        
        expires_dt = db_sub.expires_at.replace(tzinfo=timezone.utc) if db_sub.expires_at.tzinfo is None else db_sub.expires_at

        if expires_dt < now_utc: # Истекла
            old_tier_if_downgraded = db_sub.tier
            grace_end_dt = expires_dt + timedelta(days=self.config.grace_period_days)
            
            if now_utc < grace_end_dt and db_sub.status != SubscriptionStatus.GRACE_PERIOD:
                db_sub.status = SubscriptionStatus.GRACE_PERIOD; changed = True
                logger.info(f"Subscription ID {db_sub.id} for user DB ID {db_sub.user_id} (tier {db_sub.tier.value}) in GRACE_PERIOD until {grace_end_dt.isoformat()}")
            elif now_utc >= grace_end_dt and db_sub.status != SubscriptionStatus.EXPIRED:
                # Если уже не EXPIRED, то даунгрейдим
                if db_sub.status != SubscriptionStatus.EXPIRED: # Проверяем, чтобы не даунгрейдить уже даунгрейженное
                    self._downgrade_db_sub_to_free(db_sub, f"expired_tier_{old_tier_if_downgraded.value}_after_grace")
                    changed = True # Статус изменился на Free
            # Если статус уже EXPIRED, то он должен был быть даунгрейжен ранее.
            # Но на всякий случай, если он EXPIRED, но tier не FREE - даунгрейдим.
            elif db_sub.status == SubscriptionStatus.EXPIRED and db_sub.tier != SubscriptionTier.FREE:
                 self._downgrade_db_sub_to_free(db_sub, f"revalidating_expired_tier_{old_tier_if_downgraded.value}")
                 changed = True
                 
            if changed: await self.db_service.save_subscription(db_sub)
        
        return changed, old_tier_if_downgraded

    def _downgrade_db_sub_to_free(self, db_sub: DBSubscription, reason: str):
        """Helper to modify DBSubscription object to Free tier defaults."""
        logger.info(f"Downgrading subscription ID {db_sub.id} for user DB ID {db_sub.user_id} to Free. Reason: {reason}. Previous tier: {db_sub.tier.value}")
        if db_sub.tier != SubscriptionTier.FREE:
            db_sub.original_tier_before_expiry = db_sub.tier
        
        db_sub.tier = SubscriptionTier.FREE
        db_sub.status = SubscriptionStatus.ACTIVE # Free подписка всегда активна
        db_sub.expires_at = None
        db_sub.is_trial = False
        db_sub.trial_source = None
        # payment_provider и telegram_charge_id можно оставить для истории, или очистить
        # db_sub.payment_provider = None
        # db_sub.telegram_charge_id = None

    async def _get_usage_data(self, user_id_db: int, user_id_tg: int) -> UserSubscriptionData:
        """Retrieves or creates default usage data from/to UserPreference."""
        cache_key = (user_id_db, self.USER_PREFERENCE_SUBSCRIPTION_USAGE_KEY)
        cached_data = self.usage_data_cache.get(cache_key)
        if cached_data:
            logger.debug(f"CACHE HIT: Usage data for user_db_id={user_id_db}")
            return cached_data

        prefs = await self.db_service.get_user_preferences(user_id_db, persona=self.USER_PREFERENCE_PERSONA_SYSTEM)
        usage_json_str = prefs.get(self.USER_PREFERENCE_SUBSCRIPTION_USAGE_KEY)
        
        usage_s_data: UserSubscriptionData
        if usage_json_str and isinstance(usage_json_str, str):
            try:
                usage_s_data = UserSubscriptionData.from_json(usage_json_str)
                # Убедимся, что user_id_tg актуален, если он хранится в JSON
                if usage_s_data.user_id_tg != user_id_tg:
                    usage_s_data.user_id_tg = user_id_tg # Обновляем на всякий случай
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                logger.warning(f"Error decoding usage JSON for user_db_id {user_id_db}: {e}. Resetting. JSON: {usage_json_str}")
                usage_s_data = UserSubscriptionData(user_id_tg=user_id_tg)
                await self._save_usage_data(user_id_db, usage_s_data)
        else:
            usage_s_data = UserSubscriptionData(user_id_tg=user_id_tg)
            await self._save_usage_data(user_id_db, usage_s_data)
        
        self.usage_data_cache[cache_key] = usage_s_data
        return usage_s_data

    async def _save_usage_data(self, user_id_db: int, usage_s_data: UserSubscriptionData):
        """Saves usage data to UserPreference and updates cache."""
        await self.db_service.update_user_preference(
            user_id_db, self.USER_PREFERENCE_SUBSCRIPTION_USAGE_KEY, usage_s_data.to_json(),
            persona=self.USER_PREFERENCE_PERSONA_SYSTEM, preference_type='json'
        )
        cache_key = (user_id_db, self.USER_PREFERENCE_SUBSCRIPTION_USAGE_KEY)
        self.usage_data_cache[cache_key] = usage_s_data
        logger.debug(f"Saved and cached usage data for user_db_id={user_id_db}")

    async def activate_subscription(
        self, user_id_tg: int, new_tier_value: str, duration_days: int,
        payment_amount_stars: int, telegram_charge_id: str,
        payment_provider: str = "TelegramStars", is_trial_override: bool = False,
        trial_source_override: Optional[str] = None
    ) -> Dict[str, Any]:
        """Activates or upgrades a user's subscription by creating/updating a Subscription record."""
        user_db = await self._get_or_create_db_user(user_id_tg)
        if not user_db: return {"success": False, "message": "Ошибка профиля пользователя."}
        
        try: new_tier_enum = SubscriptionTier(new_tier_value)
        except ValueError: return {"success": False, "message": f"Неизвестный тариф '{new_tier_value}'."}

        new_tier_limits = self.plans.PLANS.get(new_tier_enum)
        if not new_tier_limits: return {"success": False, "message": f"Ошибка конфигурации для тарифа '{new_tier_value}'."}

        now_utc = datetime.now(timezone.utc)
        
        # Получаем текущую активную подписку, если есть
        current_active_sub: Optional[DBSubscription] = await self.db_service.get_active_subscription_for_user(user_db.id)
        
        start_date_for_new_period = now_utc
        # Если есть активная платная подписка и это не триал, и новый тариф выше или такой же, то продлеваем
        if current_active_sub and \
           current_active_sub.tier != SubscriptionTier.FREE and \
           not current_active_sub.is_trial and \
           current_active_sub.expires_at and \
           current_active_sub.expires_at > now_utc and \
           self.plans.TIER_HIERARCHY.get(new_tier_enum, -1) >= self.plans.TIER_HIERARCHY.get(current_active_sub.tier, -1):
            start_date_for_new_period = current_active_sub.expires_at
            logger.info(f"Продление существующей подписки ID {current_active_sub.id} для user {user_id_tg} с {start_date_for_new_period.isoformat()}")
            # Деактивируем старую подписку, если тариф меняется (апгрейд)
            if current_active_sub.tier != new_tier_enum:
                current_active_sub.status = SubscriptionStatus.CANCELLED # Или EXPIRED, если это апгрейд
                current_active_sub.expires_at = now_utc # Заканчиваем ее сейчас
                await self.db_service.save_subscription(current_active_sub)
                logger.info(f"Старая подписка ID {current_active_sub.id} (tier {current_active_sub.tier.value}) отменена из-за апгрейда.")


        new_expires_dt = start_date_for_new_period + timedelta(days=duration_days)
        
        # Создаем НОВУЮ запись Subscription для новой активации/апгрейда/триала
        new_sub_record = DBSubscription(
            user_id=user_db.id,
            tier=new_tier_enum,
            status=SubscriptionStatus.ACTIVE,
            activated_at=now_utc, # Время фактической активации
            expires_at=new_expires_dt,
            is_trial=is_trial_override,
            trial_source=trial_source_override if is_trial_override else None,
            payment_provider=payment_provider,
            telegram_charge_id=telegram_charge_id,
            payment_amount_stars=payment_amount_stars if not is_trial_override else 0
        )
        saved_new_sub = await self.db_service.save_subscription(new_sub_record)

        # Сбрасываем usage лимиты в UserPreference
        usage_s_data = await self._get_usage_data(user_db.id, user_id_tg)
        usage_s_data.usage["daily_messages_used"] = 0
        usage_s_data.usage["last_message_date"] = now_utc.date().isoformat()
        await self._save_usage_data(user_db.id, usage_s_data)
        
        # Инвалидация основного кэша подписки (get_active_subscription_for_user)
        await self.db_service.invalidate_subscription_data_cache(user_db.id)

        # Логирование и статистика (если не триал)
        if not is_trial_override:
            await self.db_service.save_statistic(
                metric_name='subscription_purchased', metric_value=float(payment_amount_stars),
                user_id=user_db.id, additional_data={'tier': new_tier_enum.value, 'duration_days': duration_days, 'provider': payment_provider}
            )
            if self.bot_instance and hasattr(self.bot_instance, 'stats'): # Обновляем статистику в рантайме бота
                self.bot_instance.stats['revenue_total_stars'] = self.bot_instance.stats.get('revenue_total_stars', 0.0) + float(payment_amount_stars)
                self.bot_instance.stats['subscriptions_sold'] = self.bot_instance.stats.get('subscriptions_sold', 0) + 1
        
        # Обновление памяти, если тариф изменился
        old_tier_for_memory_upgrade = current_active_sub.tier.value if current_active_sub else SubscriptionTier.FREE.value
        if self.bot_instance and hasattr(self.bot_instance, 'memory_service') and old_tier_for_memory_upgrade != new_tier_enum.value:
            try: 
                await self.bot_instance.memory_service.upgrade_memory_on_tier_change(user_id_tg, old_tier_for_memory_upgrade, new_tier_enum.value)
            except Exception as e_mem: 
                logger.error(f"Error in memory_service.upgrade_memory_on_tier_change for user {user_id_tg}: {e_mem}", exc_info=True)
        
        logger.info(f"Subscription ID {saved_new_sub.id} for user {user_id_tg} activated/updated to '{new_tier_value}'. Expires: {new_expires_dt.isoformat()}. Trial: {is_trial_override}")
        return {"success": True, "new_tier": new_tier_value, "message": f"Подписка «{new_tier_limits.tier_name}» успешно активирована!"}

    async def activate_trial_subscription(self, user_id_tg: int, trial_tier_value: str, trial_days: int, promocode_used: Optional[str] = None) -> Dict[str, Any]:
        try: requested_trial_tier_enum = SubscriptionTier(trial_tier_value)
        except ValueError: return {"success": False, "message": f"Неизвестный триальный тариф '{trial_tier_value}'."}
        
        if not await self.user_can_receive_trial(user_id_tg, requested_trial_tier_enum):
            return {"success": False, "message": "Вы уже использовали триал этого или более высокого уровня, или у вас есть активная платная подписка."}
        
        logger.info(f"Активация триала '{trial_tier_value}' на {trial_days} дней для user {user_id_tg}. Источник: {promocode_used or 'welcome'}")
        return await self.activate_subscription(
            user_id_tg, trial_tier_value, trial_days, 0, # 0 звезд для триала
            f"TRIAL_{promocode_used or 'WELCOME'}_{int(datetime.now(timezone.utc).timestamp())}", # Уникальный charge_id для триала
            "Trial", True, promocode_used or "welcome_bonus"
        )

    async def user_can_receive_trial(self, user_id_tg: int, requested_trial_tier: SubscriptionTier) -> bool:
        """Проверяет, может ли пользователь получить триал указанного уровня."""
        user_db = await self._get_or_create_db_user(user_id_tg)
        if not user_db: return False # Не можем проверить без пользователя в БД
        
        # 1. Проверяем, есть ли у пользователя активная НЕ ТРИАЛЬНАЯ платная подписка
        current_main_sub = await self.db_service.get_active_subscription_for_user(user_db.id)
        if current_main_sub and \
           current_main_sub.tier != SubscriptionTier.FREE and \
           not current_main_sub.is_trial:
            # Если есть активная платная, триал не даем (или даем только если запрашиваемый триал ВЫШЕ текущего платного)
            current_paid_level = self.plans.TIER_HIERARCHY.get(current_main_sub.tier, -1)
            requested_trial_level = self.plans.TIER_HIERARCHY.get(requested_trial_tier, -1)
            if requested_trial_level <= current_paid_level:
                logger.info(f"User {user_id_tg} (DB ID {user_db.id}) has active paid tier {current_main_sub.tier.value}. Cannot receive trial for {requested_trial_tier.value}.")
                return False

        # 2. Проверяем историю ВСЕХ подписок (включая истекшие и триалы)
        all_subs_history: List[DBSubscription] = await self.db_service.get_all_user_subscriptions_history(user_db.id)
        
        highest_tier_ever_had_level = -1 # Уровень самого высокого тарифа, который когда-либо был (платный или триал)
        
        for sub_entry in all_subs_history:
            tier_level = self.plans.TIER_HIERARCHY.get(sub_entry.tier, -1)
            if tier_level > highest_tier_ever_had_level:
                highest_tier_ever_had_level = tier_level
        
        requested_trial_level = self.plans.TIER_HIERARCHY.get(requested_trial_tier, -1)
        
        # Пользователь может получить триал, только если запрашиваемый уровень триала ВЫШЕ
        # любого уровня тарифа (платного или триального), который у него когда-либо был.
        # Это предотвращает получение триала Basic после того, как был Premium.
        can_receive = requested_trial_level > highest_tier_ever_had_level
        
        if not can_receive:
            logger.info(f"User {user_id_tg} (DB ID {user_db.id}) cannot receive trial {requested_trial_tier.value} (level {requested_trial_level}). Highest tier ever had level: {highest_tier_ever_had_level}")
        return can_receive

    async def check_message_limit(self, user_id_tg: int) -> Dict[str, Any]:
        """Проверяет лимит сообщений, учитывая тариф и бонусы."""
        sub_dict = await self.get_user_subscription(user_id_tg) # Получаем актуальные данные
        
        tier_limits = sub_dict.get("limits")
        if not isinstance(tier_limits, TierLimits): 
            tier_limits = self.plans.PLANS[SubscriptionTier.FREE] # Фоллбэк
            logger.error(f"TierLimits object missing in subscription data for user {user_id_tg} during message limit check. Using Free limits.")

        daily_limit_plan = tier_limits.daily_messages
        is_unlimited = (daily_limit_plan == -1)
        
        usage = sub_dict.get("usage", {})
        used_today = usage.get("daily_messages_used", 0)
        bonus_remaining = usage.get("bonus_messages_remaining", 0)
        
        effective_limit = daily_limit_plan
        if not is_unlimited:
            effective_limit += bonus_remaining # Бонусы добавляются к дневному лимиту
        
        allowed = is_unlimited or (used_today < effective_limit)
        remaining_messages = (effective_limit - used_today) if not is_unlimited else -1 # -1 для безлимита
        
        return {
            "allowed": allowed, "used": used_today, "limit_from_plan": daily_limit_plan, 
            "bonus_available": bonus_remaining, "effective_limit": effective_limit, 
            "remaining": remaining_messages, "unlimited": is_unlimited, 
            "reason": "Daily message limit reached." if not allowed else "OK"
        }

    async def increment_message_usage(self, user_id_tg: int, count: int = 1):
        """Увеличивает счетчик использованных сообщений, сначала списывая бонусы."""
        user_db = await self._get_or_create_db_user(user_id_tg)
        if not user_db: return

        usage_s_data = await self._get_usage_data(user_db.id, user_id_tg)
        
        bonus_rem = usage_s_data.usage.get("bonus_messages_remaining", 0)
        deducted_from_bonus = 0
        if bonus_rem > 0:
            deducted_from_bonus = min(count, bonus_rem)
            usage_s_data.usage["bonus_messages_remaining"] -= deducted_from_bonus
        
        deducted_from_daily = count - deducted_from_bonus
        if deducted_from_daily > 0:
            usage_s_data.usage["daily_messages_used"] = usage_s_data.usage.get("daily_messages_used", 0) + deducted_from_daily
        
        # Обновляем дату последнего сообщения, если она еще не сегодняшняя
        # Это уже должно делаться в get_user_subscription, но на всякий случай
        now_date_iso = datetime.now(timezone.utc).date().isoformat()
        if usage_s_data.usage.get("last_message_date") != now_date_iso:
            usage_s_data.usage["last_message_date"] = now_date_iso
            # usage["daily_messages_used"] должен был быть сброшен в get_user_subscription,
            # но если это первый инкремент за день, убедимся, что он не отрицательный.
            if usage_s_data.usage["daily_messages_used"] < 0: usage_s_data.usage["daily_messages_used"] = deducted_from_daily
        
        await self._save_usage_data(user_db.id, usage_s_data)
        logger.info(f"Message usage updated for user {user_id_tg}: used_today={usage_s_data.usage['daily_messages_used']}, bonus_remaining={usage_s_data.usage['bonus_messages_remaining']}")

    async def add_bonus_messages(self, user_id_tg: int, amount: int, source: str = "bonus", expires_in_days: Optional[int] = None):
        """Добавляет бонусные сообщения пользователю."""
        if amount <= 0: return
        user_db = await self._get_or_create_db_user(user_id_tg)
        if not user_db: return

        usage_s_data = await self._get_usage_data(user_db.id, user_id_tg)
        now_utc = datetime.now(timezone.utc)
        
        # Проверяем, не истекли ли текущие бонусы, перед добавлением новых
        current_bonus_rem = usage_s_data.usage.get("bonus_messages_remaining", 0)
        current_bonus_total = usage_s_data.usage.get("bonus_messages_total", 0)
        current_bonus_exp_str = usage_s_data.usage.get("bonus_expiry_date")

        if current_bonus_exp_str:
            try:
                current_bonus_exp_dt = datetime.fromisoformat(current_bonus_exp_str.replace('Z','+00:00'))
                if current_bonus_exp_dt.tzinfo is None: current_bonus_exp_dt = current_bonus_exp_dt.replace(tzinfo=timezone.utc)
                if current_bonus_exp_dt < now_utc: # Если текущие бонусы истекли
                    current_bonus_rem = 0; current_bonus_total = 0
                    usage_s_data.usage["bonus_expiry_date"] = None
            except ValueError: # Если дата некорректна, сбрасываем
                current_bonus_rem = 0; current_bonus_total = 0
                usage_s_data.usage["bonus_expiry_date"] = None
        
        usage_s_data.usage["bonus_messages_total"] = current_bonus_total + amount
        usage_s_data.usage["bonus_messages_remaining"] = current_bonus_rem + amount
        
        if expires_in_days is not None and expires_in_days > 0:
            new_bonus_exp_dt = now_utc + timedelta(days=expires_in_days)
            # Если уже есть дата истечения, и она позже новой, оставляем старую (более выгодную для пользователя)
            if usage_s_data.usage.get("bonus_expiry_date"):
                try:
                    existing_exp_dt = datetime.fromisoformat(usage_s_data.usage["bonus_expiry_date"].replace('Z','+00:00')) # type: ignore
                    if existing_exp_dt.tzinfo is None: existing_exp_dt = existing_exp_dt.replace(tzinfo=timezone.utc)
                    if existing_exp_dt > new_bonus_exp_dt:
                        new_bonus_exp_dt = existing_exp_dt 
                except ValueError: pass # Оставляем новую дату, если старая некорректна
            usage_s_data.usage["bonus_expiry_date"] = new_bonus_exp_dt.isoformat()
        elif expires_in_days == 0 or expires_in_days is None: # Бессрочные или если не указан срок
             usage_s_data.usage["bonus_expiry_date"] = None # Убираем срок, если он был
        
        await self._save_usage_data(user_db.id, usage_s_data)
        logger.info(f"{amount} bonus messages ({source}) added for user {user_id_tg}. Remaining: {usage_s_data.usage['bonus_messages_remaining']}. Expiry: {usage_s_data.usage['bonus_expiry_date']}")

    async def check_feature_access(self, user_id_tg: int, feature_key: str, **kwargs) -> Dict[str, Any]:
        """Проверяет доступ к функции на основе текущей подписки пользователя."""
        sub_dict = await self.get_user_subscription(user_id_tg)
        
        # Если подписка истекла (не в grace period), проверяем доступ по Free тарифу
        if sub_dict.get("status") == SubscriptionStatus.EXPIRED.value:
            free_limits = self.plans.PLANS[SubscriptionTier.FREE]
            is_feature_free_res = self._is_feature_available_on_tier(feature_key, free_limits, **kwargs)
            if not is_feature_free_res.get("allowed", False):
                return {"allowed": False, "reason": "Subscription expired; feature not on Free tier.", 
                        "available_in_tiers": is_feature_free_res.get("available_in_tiers", [])}
        
        current_limits = sub_dict.get("limits")
        if not isinstance(current_limits, TierLimits): 
            logger.error(f"TierLimits missing in sub_dict for feature check (user {user_id_tg}). Using Free.")
            current_limits = self.plans.PLANS[SubscriptionTier.FREE]
            
        return self._is_feature_available_on_tier(feature_key, current_limits, **kwargs)

    def _get_tiers_with_feature(self, feature_key: str, **kwargs) -> List[str]:
        """Helper to find tiers (excluding Free) offering a feature."""
        available_tiers = []
        for tier_enum, limits_config in self.plans.PLANS.items():
            if tier_enum != SubscriptionTier.FREE and \
               self._check_single_feature_on_tier(feature_key, limits_config, **kwargs).get("allowed"):
                available_tiers.append(tier_enum.value)
        return available_tiers

    def _is_feature_available_on_tier(self, feature_key: str, limits: TierLimits, **kwargs) -> Dict[str, Any]:
        """Checks feature on specific TierLimits, adds 'available_in_tiers' if disallowed."""
        result = self._check_single_feature_on_tier(feature_key, limits, **kwargs)
        if not result.get("allowed"):
            result["available_in_tiers"] = self._get_tiers_with_feature(feature_key, **kwargs)
        return result

    def _check_single_feature_on_tier(self, feature_key: str, limits_for_check: TierLimits, **kwargs) -> Dict[str, Any]:
        """Core logic to check a single feature against a TierLimits object."""
        allowed = False; reason = f"Feature '{feature_key}' not defined for tier '{limits_for_check.tier_name}'."; 
        limit_value: Any = None; current_value: Any = None

        if feature_key == "persona_access":
            persona_to_check = kwargs.get("persona"); current_value = persona_to_check
            limit_value = limits_for_check.personas_access
            if persona_to_check and ("all" in limit_value or persona_to_check in limit_value):
                allowed = True; reason = "OK"
            else: reason = f"Persona '{persona_to_check}' not allowed on tier '{limits_for_check.tier_name}'. Allowed: {limit_value}" if persona_to_check else "Persona not specified."
        elif feature_key == "sexting_level":
            level_to_check = kwargs.get("level"); current_value = level_to_check
            limit_value = limits_for_check.sexting_max_level
            if isinstance(level_to_check, int) and level_to_check <= limit_value:
                allowed = True; reason = "OK"
            else: reason = f"Requested sexting level {level_to_check} exceeds limit {limit_value} for tier '{limits_for_check.tier_name}'." if isinstance(level_to_check, int) else "Sexting level invalid."
        elif hasattr(limits_for_check, feature_key):
            limit_value = getattr(limits_for_check, feature_key)
            if isinstance(limit_value, bool): allowed = limit_value
            elif isinstance(limit_value, (int, float)) and limit_value == -1: allowed = True # Unlimited
            elif isinstance(limit_value, (int, float)) and limit_value > 0 : allowed = True # Specific positive limit
            reason = "OK" if allowed else f"Feature '{feature_key}' limit ({limit_value}) not met on tier '{limits_for_check.tier_name}'."
        
        return {"allowed": allowed, "reason": reason, "limit_value": limit_value, 
                "current_value": current_value, "tier_checked": limits_for_check.tier_name}

    async def get_subscription_menu(self, user_id_tg: int) -> Dict[str, Any]:
        """Generates text and markup for the user's subscription management menu."""
        sub_data = await self.get_user_subscription(user_id_tg) # Получаем актуальные данные
        tier_name = sub_data.get("tier_name", "Free")
        status_display = sub_data.get("status", "active").replace("_", " ").title()
        text = f"💎 {hbold('Моя подписка')}\n\nТекущий тариф: **{tier_name}**\nСтатус: **{status_display}**\n"
        
        if sub_data.get("expires_at") and sub_data.get("tier", "free") != SubscriptionTier.FREE.value:
            try:
                expires_dt = datetime.fromisoformat(sub_data["expires_at"].replace('Z','+00:00'))
                if expires_dt.tzinfo is None: expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                days_left = (expires_dt - datetime.now(timezone.utc)).days
                text += f"Действует до: {expires_dt.strftime('%d.%m.%Y %H:%M UTC')} "
                if days_left >=0 : text += f"({days_left +1} дн. осталось)\n" 
                else: text += "(истекла)\n"
            except ValueError: text += f"Дата истечения: {sub_data['expires_at']}\n"
        
        if sub_data.get("is_trial"): text += f"Тип: {hitalic('Триальная версия')}\n"
        if sub_data.get("trial_source"): text += f"Источник триала: {sub_data['trial_source']}\n"
        
        bonus_rem = sub_data.get("usage", {}).get("bonus_messages_remaining", 0)
        if bonus_rem > 0:
            text += f"Бонусных сообщений: **{bonus_rem}**"; bonus_exp = sub_data.get("usage", {}).get("bonus_expiry_date")
            if bonus_exp:
                try: 
                    bonus_exp_dt = datetime.fromisoformat(bonus_exp.replace('Z','+00:00'))
                    if bonus_exp_dt.tzinfo is None: bonus_exp_dt = bonus_exp_dt.replace(tzinfo=timezone.utc)
                    text += f" (истекают: {bonus_exp_dt.strftime('%d.%m.%Y')})\n"
                except ValueError: text += " (срок истечения неизвестен)\n"
            else: text += " (бессрочные)\n"
        text += "\n" 
        
        buttons: List[List[InlineKeyboardButton]] = []
        current_tier_val = sub_data.get("tier", SubscriptionTier.FREE.value)
        current_status_val = sub_data.get("status", SubscriptionStatus.ACTIVE.value)

        if current_tier_val == SubscriptionTier.FREE.value or \
           current_status_val == SubscriptionStatus.EXPIRED.value:
            text += "Хотите получить больше возможностей? Рассмотрите наши Premium тарифы!"
            buttons.append([InlineKeyboardButton(text="⭐ Улучшить до Premium/VIP", callback_data="nav_subscription_plans_view")])
        elif current_status_val == SubscriptionStatus.GRACE_PERIOD.value:
            text += "Ваша подписка истекла, но вы еще можете ее продлить!"
            buttons.append([InlineKeyboardButton(text="❗ Продлить подписку", callback_data="nav_subscription_plans_view")])
        elif current_status_val == SubscriptionStatus.ACTIVE.value: # Платная активная
            text += "Вы можете изменить или продлить вашу текущую подписку."
            buttons.append([InlineKeyboardButton(text="🔄 Сменить/Продлить тариф", callback_data="nav_subscription_plans_view")])
        
        buttons.append([InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="action_enter_promocode_start")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="nav_user_profile_view")]) # Или nav_main
        
        return {"text": text, "reply_markup": InlineKeyboardMarkup(inline_keyboard=buttons)}

    def _get_tier_name(self, tier_value: str) -> str:
        """Возвращает отображаемое имя тарифа."""
        try: return self.plans.PLANS[SubscriptionTier(tier_value)].tier_name
        except (ValueError, KeyError): return tier_value.title()

class SubscriptionMiddleware(BaseMiddleware):
    """Middleware to validate/update subscription status on user interactions."""
    def __init__(self, subscription_service: SubscriptionService):
        super().__init__()
        self.subscription_service = subscription_service

    async def __call__(self, handler: Callable[[types.TelegramObject, Dict[str, Any]], Any], 
                       event: types.TelegramObject, data: Dict[str, Any]) -> Any:
        user: Optional[types.User] = data.get('event_from_user') # Aiogram 3.x
        
        if user and isinstance(user, types.User): # Убедимся, что это объект пользователя
            # Этот вызов гарантирует, что данные о подписке загружены, проверены и кэшированы (если кэширование есть в get_user_subscription)
            # Также он обновит статус подписки, если она истекла.
            _ = await self.subscription_service.get_user_subscription(user.id)
        return await handler(event, data)
