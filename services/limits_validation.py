# services/limits_validation.py
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
from enum import Enum
from aiogram.utils.markdown import hbold, hitalic
from aiogram import types 

from services.subscription_system import SubscriptionService, SubscriptionTier, TierLimits 
from database.operations import DatabaseService 
from utils.error_handler import handle_errors

logger = logging.getLogger(__name__)

class LimitType(Enum):
    DAILY_MESSAGES = "daily_messages"
    PERSONA_ACCESS = "persona_access"
    SEXTING_LEVEL = "sexting_level"
    VOICE_MESSAGES = "voice_messages"
    FILE_UPLOADS = "file_uploads" 
    MEMORY_ENTRIES = "memory_entries" 
    AI_INSIGHTS = "ai_insights"
    CUSTOM_FANTASIES = "custom_fantasies"
    MESSAGE_LENGTH = "message_length" 

class ValidationResult:
    def __init__(self, allowed: bool, reason: Optional[str] = None, data: Optional[Dict[str, Any]] = None, user_message_override: Optional[str] = None):
        self.allowed = allowed
        self.reason = reason if reason else ("Allowed" if allowed else "Action Denied")
        self.data = data or {}
        self.user_message_override = user_message_override

    def to_dict(self) -> Dict[str, Any]:
        res = {"allowed": self.allowed, "reason": self.reason, **self.data}
        if self.user_message_override:
            res["user_message_override"] = self.user_message_override
        return res

class RateLimiter:
    def __init__(self, db_service: DatabaseService, default_limit: int = 10, default_window_seconds: int = 60):
        self.db_service = db_service
        self.default_limit = default_limit
        self.default_window_seconds = default_window_seconds
        # Напоминание: Периодическая очистка ОЧЕНЬ старых записей UserActionTimestamp
        # (например, старше нескольких дней) должна быть реализована и запускаться 
        # из основного цикла приложения (main.py) для предотвращения бесконечного роста таблицы.

    async def check_rate_limit(self, user_id_db: int, action_key: str,
                               limit: Optional[int] = None,
                               window_seconds: Optional[int] = None) -> ValidationResult:
        limit_to_check = limit if limit is not None else self.default_limit
        window_to_check = window_seconds if window_seconds is not None else self.default_window_seconds

        now_utc = datetime.now(timezone.utc)
        window_start_time = now_utc - timedelta(seconds=window_to_check)
        
        cleanup_older_than = window_start_time - timedelta(seconds=window_to_check) 
        await self.db_service.delete_old_user_action_timestamps(user_id_db, action_key, cleanup_older_than)

        current_count_in_window = await self.db_service.count_user_actions_in_window(
            user_id_db, action_key, window_start_time
        )

        if current_count_in_window >= limit_to_check:
            timestamps_in_window = await self.db_service.get_user_action_timestamps_in_window(
               user_id_db, action_key, window_start_time 
            ) 
            
            reset_in_seconds_calculated = window_to_check 
            if timestamps_in_window and len(timestamps_in_window) >= limit_to_check :
                index_of_expiring_ts = current_count_in_window - limit_to_check
                if 0 <= index_of_expiring_ts < len(timestamps_in_window):
                    timestamp_to_expire = timestamps_in_window[index_of_expiring_ts]
                    reset_in_seconds_calculated = (timestamp_to_expire + timedelta(seconds=window_to_check) - now_utc).total_seconds()
                else: 
                    reset_in_seconds_calculated = window_to_check / 2 
            else: 
                reset_in_seconds_calculated = 1 

            return ValidationResult(
                allowed=False,
                reason=f"Rate limit for '{action_key}' exceeded: {current_count_in_window}/{limit_to_check} per {window_to_check}s.",
                data={
                    "action_key": action_key, "current_count": current_count_in_window, "limit": limit_to_check,
                    "window_seconds": window_to_check,
                    "reset_in_seconds": max(1, round(reset_in_seconds_calculated)) 
                }
            )
        
        await self.db_service.add_user_action_timestamp(user_id_db, action_key, now_utc)
        
        return ValidationResult(
            allowed=True,
            data={
                "action_key": action_key, "current_count": current_count_in_window + 1, "limit": limit_to_check,
                "remaining": limit_to_check - (current_count_in_window + 1)
            }
        )

class AntiSpamSystem:
    BLOCK_TYPE_SPAM = "spam_activity" # Константа для типа блокировки

    def __init__(self, db_service: DatabaseService, config: Optional[Dict[str, Any]] = None):
        self.db_service = db_service
        self.rate_limiter = RateLimiter(db_service=self.db_service) 
        default_spam_config = {
            "messages_per_minute": 15, "duplicate_message_hashes": 3,
            "duplicate_message_window_seconds": 600, "long_message_threshold_chars": 3000,
            "long_messages_per_10_min": 3, "temp_block_duration_minutes": 5,
        }
        self.spam_config = {**default_spam_config, **(config or {})}
        # self.temp_blocks больше не используется, блокировки хранятся в БД.
        # Напоминание: Периодическая очистка истекших TemporaryBlock из БД должна
        # быть реализована и запускаться из main.py (через db_service.delete_expired_temporary_blocks).

    async def check_spam(self, user_id_db: int, message_text: str,
                        message_type: str = "text") -> ValidationResult:
        now_utc = datetime.now(timezone.utc)
        
        # Проверяем активную блокировку в БД
        active_block = await self.db_service.get_active_temporary_block(user_id_db, block_type=self.BLOCK_TYPE_SPAM)
        if active_block and active_block.blocked_until_utc > now_utc:
            remaining_seconds = (active_block.blocked_until_utc - now_utc).total_seconds()
            return ValidationResult(
                allowed=False, 
                reason=f"Temporary block active until {active_block.blocked_until_utc.isoformat()}.",
                user_message_override=f"⏳ Вы временно заблокированы из-за подозрительной активности. Пожалуйста, подождите {hbold(str(max(1, int(remaining_seconds / 60))))} мин.",
                data={"block_type": self.BLOCK_TYPE_SPAM, "block_remaining_seconds": round(remaining_seconds)}
            )

        general_rate_check = await self.rate_limiter.check_rate_limit(
            user_id_db, "any_message", self.spam_config["messages_per_minute"], 60)
        if not general_rate_check.allowed:
            await self._apply_temporary_block_db(user_id_db, "Too many messages per minute.")
            return ValidationResult(allowed=False, reason="Too many messages per minute (anti-spam).",
                                    user_message_override="⚡ Слишком много сообщений! Пожалуйста, помедленнее.",
                                    data=general_rate_check.data)

        message_hash = str(hash(message_text.strip().lower()))
        duplicate_check = await self.rate_limiter.check_rate_limit(
            user_id_db, f"msg_hash_{message_hash}", self.spam_config["duplicate_message_hashes"],
            self.spam_config["duplicate_message_window_seconds"])
        if not duplicate_check.allowed:
            await self._apply_temporary_block_db(user_id_db, "Too many duplicate messages.")
            return ValidationResult(allowed=False, reason="Too many duplicate messages (anti-spam).",
                                    user_message_override="🤔 Кажется, вы отправляете одно и то же сообщение. Попробуйте что-нибудь другое!",
                                    data=duplicate_check.data)

        if len(message_text) > self.spam_config["long_message_threshold_chars"]:
            long_message_rate_check = await self.rate_limiter.check_rate_limit(
                user_id_db, "long_message", self.spam_config["long_messages_per_10_min"], 600)
            if not long_message_rate_check.allowed:
                await self._apply_temporary_block_db(user_id_db, "Too many long messages.")
                return ValidationResult(allowed=False, reason="Too many long messages (anti-spam).",
                                        user_message_override=f"📏 Сообщения очень длинные (лимит: {self.spam_config['long_message_threshold_chars']} симв.). Отправляйте их реже.",
                                        data=long_message_rate_check.data)
        return ValidationResult(allowed=True)

    async def _apply_temporary_block_db(self, user_id_db: int, reason: str):
        """Применяет временную блокировку, сохраняя ее в БД."""
        block_duration_minutes = self.spam_config["temp_block_duration_minutes"]
        blocked_until = datetime.now(timezone.utc) + timedelta(minutes=block_duration_minutes)
        try:
            await self.db_service.add_temporary_block(
                user_id_db=user_id_db,
                block_type=self.BLOCK_TYPE_SPAM,
                blocked_until_utc=blocked_until,
                reason=reason
            )
            logger.warning(f"Пользователь (DB ID: {user_id_db}) временно заблокирован (в БД) на {block_duration_minutes} мин. Причина: {reason}")
        except Exception as e:
            logger.error(f"Ошибка при сохранении временной блокировки в БД для user_id_db {user_id_db}: {e}", exc_info=True)


class AdvancedLimitsValidator:
    def __init__(self, subscription_service: SubscriptionService, db_service: DatabaseService, validator_config: Optional[Dict[str, Any]] = None):
        self.subscription_service = subscription_service
        self.db_service = db_service 
        self.anti_spam = AntiSpamSystem(db_service, (validator_config or {}).get("anti_spam_config"))
        default_validator_config = {
            "grace_period_days_override": None, 
            "soft_limit_warning_ratio": 0.85, 
        }
        self.validator_config = {**default_validator_config, **(validator_config or {})}

    @handle_errors
    async def validate_message_send(self, user_id_tg: int, message_text: str,
                                  current_persona_id: str) -> ValidationResult:
        # Напоминание: Тщательно протестировать всю логику валидации.
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: 
            # Эта ситуация должна обрабатываться выше, в main.py, где пользователь создается/получается.
            # Если сюда пришел user_id_tg, для которого нет db_user, это ошибка в потоке данных.
            logger.error(f"Критическая ошибка: DBUser для TG_ID {user_id_tg} не был найден или создан перед вызовом validate_message_send.")
            return ValidationResult(allowed=False, reason="User not found for validation (critical error).")

        spam_check_result = await self.anti_spam.check_spam(db_user.id, message_text)
        if not spam_check_result.allowed: return spam_check_result
        
        user_subscription_data = await self.subscription_service.get_user_subscription(user_id_tg)
        
        status_check_result = self._check_subscription_status(user_subscription_data)
        if not status_check_result.allowed: return status_check_result
        
        message_limit_data = await self.subscription_service.check_message_limit(user_id_tg)
        if not message_limit_data.get("allowed", False): 
            return self._handle_daily_message_limit_exceeded(user_subscription_data, message_limit_data)
            
        persona_access_data = await self.subscription_service.check_feature_access(
            user_id_tg, LimitType.PERSONA_ACCESS.value, persona=current_persona_id
        )
        if not persona_access_data.get("allowed", False):
            tier_name = user_subscription_data.get('tier_name', 'Free')
            required_tier_for_persona = SubscriptionTier.BASIC.value 
            if "madina" in current_persona_id.lower() and \
               not ("basic" in current_persona_id.lower() or "friend" in current_persona_id.lower()): 
                 required_tier_for_persona = SubscriptionTier.PREMIUM.value 
            
            return ValidationResult(
                allowed=False, 
                reason=f"Access to persona '{current_persona_id.title()}' denied for tier '{tier_name}'.",
                user_message_override=(
                    f"🔒 Персона {hbold(current_persona_id.title())} недоступна на вашем текущем тарифе «{hbold(tier_name)}».\n"
                    f"Для доступа требуется тариф «{hbold(self.subscription_service._get_tier_name(required_tier_for_persona))}» или выше."
                ),
                data={"upgrade_required": True, "feature": LimitType.PERSONA_ACCESS.value, "required_tier": required_tier_for_persona}
            )
            
        warnings = self._get_limit_warnings(user_subscription_data, message_limit_data)
        return ValidationResult(allowed=True, data={"warnings": warnings, **message_limit_data})

    def _check_subscription_status(self, subscription_data: Dict[str, Any]) -> ValidationResult:
        status = subscription_data.get("status", "active")
        tier = subscription_data.get("tier", SubscriptionTier.FREE.value)
        expires_at_str = subscription_data.get("expires_at")
        tier_name = subscription_data.get('tier_name', tier.title() if isinstance(tier, str) else tier.value.title())

        if status == "expired":
            expiry_date_display = ""
            if expires_at_str:
                try: expiry_date_display = f" {datetime.fromisoformat(expires_at_str.replace('Z','+00:00')).strftime('%d.%m.%Y')}"
                except ValueError: pass 
            return ValidationResult(allowed=False, reason="Subscription expired.", 
                                    user_message_override=f"⏳ Ваша подписка «{hbold(tier_name)}» истекла{expiry_date_display}. Пожалуйста, продлите ее в разделе /premium, чтобы продолжить пользоваться всеми возможностями.",
                                    data={"upgrade_required": True, "status": "expired"})

        if status == "grace_period" and expires_at_str:
            try:
                expires_dt = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                if expires_dt.tzinfo is None: expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                grace_days_config = self.validator_config.get("grace_period_days_override") or getattr(self.subscription_service.config, 'grace_period_days', 3)
                grace_end_dt = expires_dt + timedelta(days=grace_days_config); days_left_grace = (grace_end_dt - datetime.now(timezone.utc)).days
                warning_msg = (f"⚠️ Ваша подписка «{hbold(tier_name)}» истекла! Осталось {hbold(str(max(0,days_left_grace)))} дн. льготного периода для продления в /premium. Некоторые функции могут быть ограничены.")
                return ValidationResult(allowed=True, reason="Grace period active.", data={"warning_message": warning_msg, "status": "grace_period"})
            except Exception as e: logger.warning(f"Ошибка расчета или отображения grace period: {e}")
        
        return ValidationResult(allowed=True, data={"status": status})

    def _handle_daily_message_limit_exceeded(self, subscription_data: Dict[str, Any], message_limit_data: Dict[str, Any]) -> ValidationResult:
        tier_name = subscription_data.get('tier_name', 'Free'); used = message_limit_data.get('used',0); limit = message_limit_data.get('effective_limit',0)
        bonus_available = message_limit_data.get('bonus_available', 0)
        limit_text = f"{hbold(str(used))}/{hbold(str(limit))}"
        if bonus_available > 0: limit_text += f" (включая {hbold(str(bonus_available))} бонусных)"
        return ValidationResult(allowed=False, reason="Daily message limit exceeded.",
                                user_message_override=(f"⏰ Дневной лимит сообщений для вашего тарифа «{hbold(tier_name)}» ({limit_text}) исчерпан.\n"
                                                       "Новый лимит будет доступен завтра. Чтобы общаться без ограничений, рассмотрите возможность перехода на /premium !"),
                                data={"upgrade_required": True, "limit_type": LimitType.DAILY_MESSAGES.value, **message_limit_data})

    def _get_limit_warnings(self, subscription_data: Dict[str, Any], message_limit_data: Dict[str, Any]) -> List[str]:
        warnings = []
        if not message_limit_data.get("unlimited", False):
            used = message_limit_data.get("used", 0); limit = message_limit_data.get("effective_limit", 1) 
            if limit > 0 and (used / limit) >= self.validator_config["soft_limit_warning_ratio"]:
                remaining = message_limit_data.get("remaining", 0)
                warnings.append(f"⚠️ Внимание! У вас осталось {hbold(str(remaining))} сообщений из дневного лимита ({hbold(str(limit))}).")
        expires_at_str = subscription_data.get("expires_at"); status = subscription_data.get("status")
        if expires_at_str and status == "active": 
            try:
                expires_dt = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                if expires_dt.tzinfo is None: expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                days_left = (expires_dt - datetime.now(timezone.utc)).days
                renewal_prompt_days_config = getattr(self.subscription_service.config, 'renewal_prompt_days', 7)
                if 0 <= days_left <= renewal_prompt_days_config:
                    tier_name = subscription_data.get('tier_name', 'ваша')
                    warnings.append(f"⏰ Ваша подписка «{hbold(tier_name)}» истекает через {hbold(str(days_left + 1))} дн. Не забудьте продлить ее через /premium !")
            except ValueError: logger.warning(f"Некорректная дата expires_at ('{expires_at_str}') для генерации предупреждения о продлении.")
        return warnings

    @handle_errors
    async def validate_feature_access(self, user_id_tg: int, feature_key: str, **kwargs) -> ValidationResult:
        # Напоминание: Тщательно протестировать логику доступа к фичам.
        user_subscription_data = await self.subscription_service.get_user_subscription(user_id_tg)
        status_check = self._check_subscription_status(user_subscription_data)
        if not status_check.allowed and user_subscription_data.get("status") != "grace_period":
            free_tier_limits: TierLimits = self.subscription_service.plans.PLANS[SubscriptionTier.FREE] 
            is_feature_free_check_result = self.subscription_service._is_feature_available_on_tier(feature_key, free_tier_limits, **kwargs)
            if not is_feature_free_check_result.get("allowed", False): return status_check 
        feature_access_data = await self.subscription_service.check_feature_access(user_id_tg, feature_key, **kwargs)
        if not feature_access_data.get("allowed", False):
            current_tier_name = user_subscription_data.get('tier_name', 'Free'); required_tier_display = "более высокий тариф"
            available_in_tiers_list: Optional[List[str]] = feature_access_data.get("available_in_tiers")
            if available_in_tiers_list:
                hierarchy = self.subscription_service.plans.TIER_HIERARCHY
                try: 
                    min_req_tier_val = min(available_in_tiers_list, key=lambda t_val_str: hierarchy.get(SubscriptionTier(t_val_str), float('inf')))
                    required_tier_display = f"«{hbold(self.subscription_service._get_tier_name(min_req_tier_val))}»"
                except (ValueError, TypeError) as e_min_tier: 
                    logger.warning(f"Не удалось определить минимальный требуемый тариф из {available_in_tiers_list}: {e_min_tier}")
            user_msg = (f"🔒 Функция ({hitalic(feature_key.replace('_', ' ').title())}) недоступна на вашем текущем тарифе «{hbold(current_tier_name)}».\n"
                        f"Для доступа требуется {required_tier_display} или выше. Пожалуйста, обновите вашу подписку через /premium.")
            if feature_key == LimitType.SEXTING_LEVEL.value:
                 max_allowed_level = feature_access_data.get('limit_value', 0) 
                 user_msg = (f"🔥 Ваш максимальный уровень страсти — {hbold(str(max_allowed_level))} на тарифе «{hbold(current_tier_name)}».\n"
                             f"Для запрошенного уровня {hbold(str(kwargs.get('level', 'N/A')))} требуется {required_tier_display} или выше. Повысьте вашу подписку через /premium.")
            return ValidationResult(allowed=False, reason=feature_access_data.get("reason", f"Feature '{feature_key}' not available."), 
                                    user_message_override=user_msg, data={"upgrade_required": True, "feature": feature_key, **feature_access_data})
        return ValidationResult(allowed=True, data=feature_access_data)
