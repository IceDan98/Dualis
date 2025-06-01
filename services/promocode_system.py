# services/promocode_system.py
import logging
import random
import string
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional, Dict, List, Any, Tuple, Union # Добавил Union
from dataclasses import dataclass, field
import json
import asyncio

# Импорты из вашего проекта
from sqlalchemy.exc import IntegrityError
from database.operations import DatabaseService
from database.models import PromoCode as DBPromoCode, User as DBUser
# Используем Enum из общего файла
from database.enums import SubscriptionTier, SubscriptionStatus # Добавил SubscriptionStatus
from utils.error_handler import handle_errors, DatabaseError
from config.settings import BotConfig
# from utils.subscription_utils import SubscriptionUtils # Если будет использоваться
# from services.notification_marketing_system import NotificationService # Если будет использоваться

logger = logging.getLogger(__name__)

class PromoCodeType(Enum):
    PUBLIC = "public"
    USER_SPECIFIC = "user_specific"
    GENERIC = "generic" # Общий, не персональный, но может быть не публичным широко
    REFERRAL_BONUS = "referral_bonus" # Для промокодов, выдаваемых по реферальной программе

class PromoCodeDiscountType(Enum):
    PERCENTAGE = "percentage"
    FIXED_AMOUNT = "fixed_amount" # Сумма в звездах
    BONUS_MESSAGES = "bonus_messages"
    FREE_TRIAL = "free_trial"
    FEATURE_UNLOCK = "feature_unlock" # Пока не реализован эффект

class ValidationError(Exception):
    """Пользовательское исключение для ошибок валидации промокода."""
    pass

@dataclass
class PromoCode: # Датакласс для работы внутри сервиса, создается из DBPromoCode
    code: str
    discount_type: str # Используем значения из PromoCodeDiscountType
    discount_value: float
    id: Optional[int] = None
    max_uses: Optional[int] = None
    uses_count: int = 0
    max_uses_per_user: Optional[int] = 1
    user_specific_id: Optional[int] = None # DB ID пользователя, если промокод персональный
    active_from: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    description: Optional[str] = None # Внутреннее описание
    user_facing_description: Optional[str] = None # Описание для пользователя
    code_type: str = PromoCodeType.GENERIC.value # Используем значения из PromoCodeType
    trial_tier_target: Optional[str] = None # Значение из SubscriptionTier, если тип FREE_TRIAL
    for_subscription_tier: Optional[str] = None # Значение из SubscriptionTier, если промокод для конкретного тарифа
    min_purchase_amount: Optional[int] = None # Минимальная сумма покупки (в звездах)
    
    # Новые поля, синхронизированные с DBPromoCode
    is_for_first_time_users: bool = False
    is_for_upgrade_only: bool = False
    is_seasonal: bool = False
    seasonal_event: Optional[str] = None
    min_account_age_days: Optional[int] = None
    allowed_user_segments: List[str] = field(default_factory=list) # Список сегментов
    allowed_countries: List[str] = field(default_factory=list) # Список кодов стран
    blocked_countries: List[str] = field(default_factory=list) # Список кодов стран
    bonus_message_expiry_days: Optional[int] = 30 # Срок жизни бонусных сообщений
    feature_unlock_target: Optional[str] = None # Какую фичу разблокировать (пока не используется)

    @classmethod
    def from_db_model(cls, db_promo: DBPromoCode) -> 'PromoCode':
        data = {
            "id": db_promo.id,
            "code": db_promo.code,
            "discount_type": db_promo.discount_type,
            "discount_value": db_promo.discount_value,
            "max_uses": db_promo.max_uses,
            "uses_count": db_promo.uses_count,
            "max_uses_per_user": db_promo.max_uses_per_user,
            "user_specific_id": db_promo.user_specific_id,
            "active_from": db_promo.active_from.replace(tzinfo=timezone.utc) if db_promo.active_from and db_promo.active_from.tzinfo is None else db_promo.active_from,
            "expires_at": db_promo.expires_at.replace(tzinfo=timezone.utc) if db_promo.expires_at and db_promo.expires_at.tzinfo is None else db_promo.expires_at,
            "is_active": db_promo.is_active,
            "created_at": db_promo.created_at.replace(tzinfo=timezone.utc) if db_promo.created_at and db_promo.created_at.tzinfo is None else (db_promo.created_at or datetime.now(timezone.utc)),
            "updated_at": db_promo.updated_at.replace(tzinfo=timezone.utc) if db_promo.updated_at and db_promo.updated_at.tzinfo is None else (db_promo.updated_at or datetime.now(timezone.utc)),
            "description": db_promo.description,
            "user_facing_description": db_promo.user_facing_description,
            "code_type": db_promo.code_type,
            "trial_tier_target": db_promo.trial_tier_target,
            "for_subscription_tier": db_promo.for_subscription_tier,
            "min_purchase_amount": db_promo.min_purchase_amount,
            "is_for_first_time_users": db_promo.is_for_first_time_users,
            "is_for_upgrade_only": db_promo.is_for_upgrade_only,
            "is_seasonal": db_promo.is_seasonal,
            "seasonal_event": db_promo.seasonal_event,
            "min_account_age_days": db_promo.min_account_age_days,
            "bonus_message_expiry_days": db_promo.bonus_message_expiry_days,
            # "feature_unlock_target": db_promo.feature_unlock_target # Если поле будет добавлено в DBPromoCode
        }
        # JSON поля
        for json_field_name in ["allowed_user_segments", "allowed_countries", "blocked_countries"]:
            json_val = getattr(db_promo, json_field_name, None)
            if isinstance(json_val, str):
                try: data[json_field_name] = json.loads(json_val)
                except json.JSONDecodeError: data[json_field_name] = []
            elif isinstance(json_val, list): # Если уже список (например, из-за ORM)
                 data[json_field_name] = json_val
            else:
                 data[json_field_name] = []
        return cls(**data)


class PromoCodeService:
    USER_PROMO_USAGE_PERSONA = "promocode_usage_log" # Для UserPreference

    def __init__(self, db_service: DatabaseService,
                 subscription_service: Any, # Заменить Any на SubscriptionService когда он будет готов
                 config: BotConfig):
        self.db_service = db_service
        self.subscription_service = subscription_service
        self.config = config
        self.security_manager = PromocodeSecurityManager(db_service, config)

    def _get_tier_display_name(self, tier_value: Optional[str]) -> str:
        """Возвращает отображаемое имя тарифа."""
        if not tier_value: return "Любой"
        try:
            tier_enum = SubscriptionTier(tier_value)
            # Предполагаем, что SubscriptionService имеет доступ к конфигурации планов
            return self.subscription_service.plans.PLANS[tier_enum].tier_name
        except (ValueError, KeyError):
            return tier_value.title()

    async def _get_user_promocode_usage_info(self, user_id_db: int, promocode_id: int) -> int:
        """Получает количество использований промокода пользователем из UserPreference."""
        try:
            # Этот метод уже есть в DatabaseService и использует UserPreference
            return await self.db_service.get_user_promocode_usage_count(user_id_db, promocode_id)
        except Exception as e:
            logger.error(f"Ошибка получения информации об использовании промокода ID {promocode_id} пользователем DB ID {user_id_db}: {e}", exc_info=True)
            return 999 # Возвращаем большое число, чтобы предотвратить использование в случае ошибки

    async def _validate_business_rules(
        self,
        promo: PromoCode,
        user_db: Optional[DBUser], # DBUser объект
        target_tier_for_purchase: Optional[SubscriptionTier] # Enum
    ):
        """Применяет кастомные бизнес-правила для валидации промокода."""
        now_utc = datetime.now(timezone.utc)
        
        if promo.is_for_first_time_users:
            if not user_db:
                raise ValidationError(f"Для промокода '{promo.code}' (только для новых) требуется информация о пользователе.")
            
            # Проверяем, были ли у пользователя платные подписки или активные триалы ранее
            # Это потребует метода в db_service для получения истории подписок пользователя
            user_subscription_history = await self.db_service.get_all_user_subscriptions_history(user_db.id)
            has_had_paid_or_trial_sub = any(
                sub.tier != SubscriptionTier.FREE or sub.is_trial
                for sub in user_subscription_history
            )
            if has_had_paid_or_trial_sub:
                raise ValidationError(f"Промокод '{promo.code}' действителен только для пользователей, не имевших ранее платных подписок или триалов.")

        if promo.is_for_upgrade_only:
            if not user_db or not target_tier_for_purchase:
                raise ValidationError(f"Для промокода '{promo.code}' (только для апгрейда) требуется информация о пользователе и целевом тарифе.")
            
            current_sub_data = await self.subscription_service.get_user_subscription(user_db.telegram_id)
            current_tier_value = current_sub_data.get("tier", SubscriptionTier.FREE.value)
            try:
                current_tier_enum = SubscriptionTier(current_tier_value)
                current_level = self.subscription_service.plans.TIER_HIERARCHY.get(current_tier_enum, 0)
                target_level = self.subscription_service.plans.TIER_HIERARCHY.get(target_tier_for_purchase, 0)
                if not (target_level > current_level):
                    raise ValidationError(f"Промокод '{promo.code}' действителен только при повышении текущего тарифа.")
            except ValueError:
                raise ValidationError(f"Ошибка определения текущего или целевого тарифа для промокода '{promo.code}'.")

        if promo.is_seasonal and promo.seasonal_event:
            # TODO: Реализовать логику проверки активности сезонного события
            # Например, сверка promo.seasonal_event с текущими активными событиями из конфига или БД
            logger.debug(f"Промокод '{promo.code}' сезонный ({promo.seasonal_event}), проверка активности события пока не реализована.")
            # if not self.is_seasonal_event_active(promo.seasonal_event):
            #     raise ValidationError(f"Сезонный промокод '{promo.code}' для события '{promo.seasonal_event}' сейчас не активен.")

        if promo.min_account_age_days is not None and user_db:
            account_age_days = (now_utc - user_db.created_at.replace(tzinfo=timezone.utc)).days
            if account_age_days < promo.min_account_age_days:
                raise ValidationError(f"Для использования промокода '{promo.code}' ваш аккаунт должен быть старше {promo.min_account_age_days} дней.")

        if promo.allowed_user_segments and user_db:
            # TODO: Реализовать получение сегмента пользователя и проверку
            # user_segment = await self.user_segmentation_service.get_user_segment(user_db.id)
            # if user_segment not in promo.allowed_user_segments:
            #     raise ValidationError(f"Промокод '{promo.code}' не действителен для вашего сегмента пользователей.")
            logger.debug(f"Промокод '{promo.code}' для сегментов {promo.allowed_user_segments}, проверка сегмента пользователя не реализована.")

        if user_db: # Гео-ограничения
            # Предполагаем, что у User есть поле country_code (нужно добавить в модель User, если нет)
            user_country = getattr(user_db, 'country_code', None) 
            if user_country:
                if promo.allowed_countries and user_country.upper() not in [c.upper() for c in promo.allowed_countries]:
                    raise ValidationError(f"Промокод '{promo.code}' не действителен для вашей страны.")
                if promo.blocked_countries and user_country.upper() in [c.upper() for c in promo.blocked_countries]:
                    raise ValidationError(f"Промокод '{promo.code}' не доступен в вашей стране.")
            elif promo.allowed_countries or promo.blocked_countries: # Если гео-ограничения есть, а страна пользователя неизвестна
                logger.warning(f"Не удалось проверить гео-ограничения для промокода '{promo.code}' для user_id_db {user_db.id} - страна не указана.")
                # Можно либо разрешить, либо запретить по умолчанию. Пока разрешаем.

    @handle_errors(reraise_as=ValidationError)
    async def validate_promocode(
        self,
        code: str,
        user_id_db: Optional[int] = None,
        user_id_tg: Optional[int] = None, # Добавлен user_id_tg для security_manager
        target_tier_for_purchase: Optional[SubscriptionTier] = None, # Enum
        purchase_amount_stars: Optional[int] = None,
        context_for_abuse_check: Optional[Dict[str, Any]] = None # Для PromocodeSecurityManager
    ) -> PromoCode:
        code_upper = code.strip().upper()
        if not code_upper:
            raise ValidationError("Промокод не может быть пустым.")

        # Проверка на злоупотребление перед запросом к БД
        if user_id_tg: # user_id_tg обязателен для security_manager
            abuse_check_result = await self.security_manager.detect_promocode_abuse(
                user_id_tg=user_id_tg, promocode_str=code_upper, context=context_for_abuse_check)
            if abuse_check_result.get("recommended_action") == "block":
                raise ValidationError(abuse_check_result.get("message_for_user", "Превышен лимит попыток ввода промокодов. Попробуйте позже."))
            if abuse_check_result.get("recommended_action") == "verify": # Пока verify трактуем как временный блок
                raise ValidationError(abuse_check_result.get("message_for_user", "Подозрительная активность. Пожалуйста, попробуйте позже или обратитесь в поддержку."))
        
        db_promo_model = await self.db_service.get_promocode_by_code(code_upper)
        if not db_promo_model:
            raise ValidationError(f"Промокод '{code_upper}' не найден.")
        
        promo = PromoCode.from_db_model(db_promo_model)
        now_utc = datetime.now(timezone.utc)

        if not promo.is_active:
            raise ValidationError(f"Промокод '{promo.code}' временно деактивирован.")
        if promo.active_from and now_utc < promo.active_from:
            activation_date_str = promo.active_from.strftime("%d.%m.%Y в %H:%M UTC")
            raise ValidationError(f"Промокод '{promo.code}' будет активен с {activation_date_str}.")
        if promo.expires_at and now_utc > promo.expires_at:
            expiry_date_str = promo.expires_at.strftime("%d.%m.%Y в %H:%M UTC")
            raise ValidationError(f"Срок действия промокода '{promo.code}' истек {expiry_date_str}.")
        
        if promo.max_uses is not None and promo.uses_count >= promo.max_uses:
            raise ValidationError(f"Промокод '{promo.code}' исчерпал общий лимит использований.")
        
        if promo.code_type == PromoCodeType.USER_SPECIFIC.value:
            if user_id_db is None or promo.user_specific_id != user_id_db:
                raise ValidationError(f"Промокод '{promo.code}' является персональным и не предназначен для вас.")
        
        if user_id_db and promo.max_uses_per_user is not None:
            if promo.id is None: # Этого не должно быть, если промокод из БД
                 logger.error(f"Промокод '{promo.code}' не имеет ID, не могу проверить лимит на пользователя.")
                 raise ValidationError("Ошибка проверки промокода: отсутствует ID.")
            user_usage_count = await self._get_user_promocode_usage_info(user_id_db, promo.id)
            if user_usage_count >= promo.max_uses_per_user:
                limit_msg = f"Вы уже использовали промокод '{promo.code}'." if promo.max_uses_per_user == 1 \
                            else f"Вы достигли лимита ({promo.max_uses_per_user}) использований промокода '{promo.code}'."
                raise ValidationError(limit_msg)

        if promo.min_purchase_amount is not None and purchase_amount_stars is not None:
            if purchase_amount_stars < promo.min_purchase_amount:
                raise ValidationError(
                    f"Для промокода '{promo.code}' минимальная сумма покупки {promo.min_purchase_amount} ⭐. "
                    f"Ваша текущая сумма: {purchase_amount_stars} ⭐."
                )
        elif promo.min_purchase_amount is not None and purchase_amount_stars is None and \
             promo.discount_type in [PromoCodeDiscountType.PERCENTAGE.value, PromoCodeDiscountType.FIXED_AMOUNT.value]:
            # Если промокод требует мин. суммы, а сумма покупки не передана (например, при вводе промокода до выбора тарифа)
            # пока не бросаем ошибку, это может быть проверено позже, при формировании инвойса.
            logger.debug(f"Промокод '{promo.code}' требует мин. суммы покупки, но purchase_amount_stars не передан для валидации скидки на этом этапе.")

        if promo.for_subscription_tier and target_tier_for_purchase:
            if promo.for_subscription_tier != target_tier_for_purchase.value: # Сравниваем значения Enum
                promo_tier_name = self._get_tier_display_name(promo.for_subscription_tier)
                target_tier_name = self._get_tier_display_name(target_tier_for_purchase.value)
                raise ValidationError(
                    f"Промокод '{promo.code}' действителен только для тарифа «{promo_tier_name}», "
                    f"а не для «{target_tier_name}»."
                )
        
        user_db_obj = await self.db_service.get_user_by_db_id(user_id_db) if user_id_db else None
        await self._validate_business_rules(promo, user_db_obj, target_tier_for_purchase)
        
        logger.info(
            f"Промокод '{promo.code}' (ID: {promo.id}) успешно прошел валидацию для user_db_id={user_id_db}, "
            f"target_tier={(target_tier_for_purchase.value if target_tier_for_purchase else 'Any')}, "
            f"purchase_amount={purchase_amount_stars if purchase_amount_stars is not None else 'N/A'}."
        )
        return promo

    @handle_errors(reraise_as=ValidationError)
    async def apply_promocode_effects(self, user_id_db: int, user_id_tg: int, promo: PromoCode,
                                    purchase_amount_stars: Optional[int] = None) -> Dict[str, Any]:
        """Применяет эффекты промокода. Для скидок возвращает рассчитанные цены, для бонусов/триалов - активирует их."""
        effects: Dict[str, Any] = {
            "promocode": promo.code,
            "discount_type": promo.discount_type,
            "discount_value": promo.discount_value,
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "user_id_tg": user_id_tg, # Сохраняем TG ID для логов/уведомлений
            "success": False, # По умолчанию false, станет true если эффект успешно применен/рассчитан
            "description": f"Эффект промокода '{promo.code}' не определен или не применен."
        }
        try:
            discount_type_enum = PromoCodeDiscountType(promo.discount_type) # Преобразуем строку в Enum
            
            if discount_type_enum == PromoCodeDiscountType.PERCENTAGE:
                effects.update(await self._apply_percentage_discount(promo, purchase_amount_stars))
            elif discount_type_enum == PromoCodeDiscountType.FIXED_AMOUNT:
                effects.update(await self._apply_fixed_amount_discount(promo, purchase_amount_stars))
            elif discount_type_enum == PromoCodeDiscountType.BONUS_MESSAGES:
                effects.update(await self._apply_bonus_messages(user_id_tg, promo))
                # Для бонусных сообщений и триалов отметка об использовании происходит здесь,
                # так как эффект применяется немедленно, а не при оплате.
                if promo.id is not None: # Убедимся, что ID есть
                    await self.mark_promocode_as_used(promo.id, user_id_db, order_id=f"BONUS_MSG_{promo.code}")
            elif discount_type_enum == PromoCodeDiscountType.FREE_TRIAL:
                effects.update(await self._apply_free_trial(user_id_tg, promo))
                if promo.id is not None:
                     await self.mark_promocode_as_used(promo.id, user_id_db, order_id=f"FREE_TRIAL_{promo.code}")
            elif discount_type_enum == PromoCodeDiscountType.FEATURE_UNLOCK:
                # effects.update(await self._apply_feature_unlock(user_id_tg, promo)) # Заглушка
                logger.warning(f"Эффект FEATURE_UNLOCK для промокода {promo.code} пока не реализован.")
                effects["description"] = f"Эффект разблокировки функции для промокода '{promo.code}' пока не реализован."
                # success остается False, если эффект не реализован
            else:
                logger.warning(f"Неподдерживаемый тип скидки '{promo.discount_type}' для промокода {promo.code}")
                raise ValueError(f"Неподдерживаемый тип скидки: {promo.discount_type}")
            
            effects["success"] = True # Если дошли сюда без исключений (и эффект не заглушка)
            logger.info(
                f"Эффекты промокода '{promo.code}' успешно рассчитаны/применены для user_id_tg={user_id_tg}. "
                f"Тип: {effects.get('discount_type')}, Описание: {effects.get('description')}"
            )

        except ValidationError as ve: # Ошибки валидации, специфичные для применения эффекта
            logger.warning(f"Ошибка валидации при применении эффектов промокода '{promo.code}' для user_id_tg={user_id_tg}: {ve}")
            effects.update({"success": False, "error": str(ve), "description": str(ve)})
        except Exception as e: # Другие неожиданные ошибки
            logger.error(f"Неожиданная ошибка при применении эффектов промокода '{promo.code}' для user_id_tg={user_id_tg}: {e}", exc_info=True)
            effects.update({"success": False, "error": str(e), "description": "Внутренняя ошибка применения промокода."})
        
        return effects

    async def _apply_percentage_discount(self, promo: PromoCode, purchase_amount_stars: Optional[int]) -> Dict[str, Any]:
        if purchase_amount_stars is None or purchase_amount_stars <= 0:
            raise ValidationError("Сумма покупки должна быть указана и быть положительной для применения процентной скидки.")
        
        discount_percentage = min(max(0.0, promo.discount_value), 99.0) # Ограничиваем скидку (0-99%)
        if discount_percentage == 0: # Если скидка 0%, не меняем цену
             return {
                "original_price_stars": purchase_amount_stars, "discount_percentage": 0.0,
                "discount_applied_stars": 0, "final_price_after_discount": purchase_amount_stars,
                "description": "Скидка 0% не изменила цену."}

        discount_amount = int(round(purchase_amount_stars * (discount_percentage / 100.0)))
        final_price = max(1, purchase_amount_stars - discount_amount) # Минимальная цена 1 звезда
        actual_discount_applied = purchase_amount_stars - final_price # Реальная скидка с учетом мин. цены

        return {
            "original_price_stars": purchase_amount_stars,
            "discount_percentage": discount_percentage,
            "discount_applied_stars": actual_discount_applied, # Сколько звезд было сэкономлено
            "final_price_after_discount": final_price,
            "description": f"Скидка {discount_percentage}% применена. Новая цена: {final_price} ⭐."
        }

    async def _apply_fixed_amount_discount(self, promo: PromoCode, purchase_amount_stars: Optional[int]) -> Dict[str, Any]:
        if purchase_amount_stars is None or purchase_amount_stars <= 0:
            raise ValidationError("Сумма покупки должна быть указана и быть положительной для применения фиксированной скидки.")
        
        discount_to_apply = int(promo.discount_value)
        if discount_to_apply <= 0:
            raise ValidationError("Сумма скидки по промокоду должна быть положительной.")

        # Скидка не может быть больше, чем (сумма покупки - 1 звезда)
        actual_discount_applied = min(discount_to_apply, purchase_amount_stars - 1) 
        if actual_discount_applied < 0: actual_discount_applied = 0 # Если сумма покупки 1 звезда, скидка 0

        final_price = purchase_amount_stars - actual_discount_applied
        
        return {
            "original_price_stars": purchase_amount_stars,
            "discount_applied_stars": actual_discount_applied,
            "final_price_after_discount": final_price,
            "description": f"Скидка {actual_discount_applied} ⭐ применена. Новая цена: {final_price} ⭐."
        }

    async def _apply_bonus_messages(self, user_id_tg: int, promo: PromoCode) -> Dict[str, Any]:
        bonus_count = int(promo.discount_value)
        if bonus_count <= 0:
            raise ValidationError("Количество бонусных сообщений должно быть положительным.")
        
        expiry_days = promo.bonus_message_expiry_days if promo.bonus_message_expiry_days is not None and promo.bonus_message_expiry_days > 0 else 30 # Дефолт 30 дней
        
        # Используем SubscriptionService для добавления бонусов
        await self.subscription_service.add_bonus_messages(
            user_id_tg, bonus_count, source=f"promocode_{promo.code}", expires_in_days=expiry_days
        )
        return {
            "bonus_messages_added": bonus_count,
            "description": f"Вам начислено {bonus_count} бонусных сообщений (действуют {expiry_days} дней)!"
        }

    async def _apply_free_trial(self, user_id_tg: int, promo: PromoCode) -> Dict[str, Any]:
        trial_days = int(promo.discount_value)
        if trial_days <= 0:
            raise ValidationError("Длительность триального периода должна быть положительной.")
        if not promo.trial_tier_target:
            raise ValidationError("Целевой тариф для триала не указан в промокоде.")
        
        try:
            trial_tier_enum = SubscriptionTier(promo.trial_tier_target)
        except ValueError:
            raise ValidationError(f"Некорректный целевой тариф для триала: '{promo.trial_tier_target}'.")

        # Проверка, может ли пользователь получить этот триал
        can_receive_check = await self.subscription_service.user_can_receive_trial(user_id_tg, trial_tier_enum)
        if not can_receive_check:
            # Сообщение об ошибке будет более конкретным из user_can_receive_trial
            raise ValidationError("Вы не можете активировать этот триальный период (возможно, уже использовали похожий или более высокий уровень подписки, или у вас активна платная подписка).")

        trial_activation_result = await self.subscription_service.activate_trial_subscription(
            user_id_tg=user_id_tg,
            trial_tier_value=trial_tier_enum.value,
            trial_days=trial_days,
            promocode_used=promo.code
        )
        
        if trial_activation_result.get("success"):
            tier_display_name = self._get_tier_display_name(trial_tier_enum.value)
            return {
                "trial_activated": True,
                "activated_trial_tier": trial_tier_enum.value,
                "trial_days": trial_days,
                "description": trial_activation_result.get("message", f"Пробный период «{tier_display_name}» на {trial_days} дней успешно активирован!")
            }
        else:
            logger.error(f"Ошибка активации триала для user_id_tg={user_id_tg} по промокоду '{promo.code}': {trial_activation_result.get('message')}")
            raise ValidationError(trial_activation_result.get("message", "Не удалось активировать триальный период."))

    async def _apply_feature_unlock(self, user_id_tg: int, promo: PromoCode) -> Dict[str, Any]:
        """ЗАГЛУШКА: Применяет разблокировку специальной функции."""
        # TODO: Реализовать логику разблокировки фичи.
        # Это может включать установку флага в UserPreference или изменение прав доступа.
        feature_name = promo.feature_unlock_target or "special_feature_access"
        unlock_duration_days = int(promo.discount_value) if promo.discount_value and promo.discount_value > 0 else 0 # 0 = навсегда
        
        logger.info(f"ЗАГЛУШКА: Разблокировка функции '{feature_name}' для user {user_id_tg} на {unlock_duration_days if unlock_duration_days > 0 else 'постоянно'}.")
        # Пример: await self.user_service.grant_feature_access(user_id_tg, feature_name, unlock_duration_days)
        
        return {
            "feature_unlocked": feature_name,
            "unlock_duration_days": unlock_duration_days,
            "description": f"Функция '{feature_name}' разблокирована {'на ' + str(unlock_duration_days) + ' дней' if unlock_duration_days > 0 else 'навсегда'}!"
        }


    @handle_errors(reraise_as=DatabaseError)
    async def mark_promocode_as_used(self, promocode_id: int, user_id_db: int, order_id: Optional[str] = None):
        """Отмечает промокод как использованный и логгирует использование в UserPreference."""
        success_increment = await self.db_service.increment_promocode_uses(promocode_id, user_id_db_for_log=user_id_db)
        if not success_increment:
            # Это может произойти, если промокод был исчерпан или деактивирован между валидацией и применением.
            # В идеале, такие случаи должны быть редки при правильной блокировке/транзакциях.
            logger.warning(f"Не удалось увеличить счетчик использований для промокода ID {promocode_id} (возможно, исчерпан или деактивирован). Пользователь DB_ID {user_id_db}.")
            # Можно рассмотреть вариант бросить исключение, если это критично.
            # raise ValidationError(f"Не удалось применить промокод ID {promocode_id}, возможно, он только что закончился.")

        # Логгирование использования в UserPreference для отслеживания "кто когда какой промокод использовал"
        # Ключ должен быть уникальным для каждого использования
        usage_log_key = f"promocode_used_{promocode_id}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        usage_data: Dict[str, Any] = {"used_at": datetime.now(timezone.utc).isoformat()}
        if order_id: # ID транзакции или заказа, если применимо
            usage_data["order_id"] = order_id
        
        await self.db_service.update_user_preference(
            user_id_db=user_id_db,
            key=usage_log_key,
            value=usage_data, # Сохраняем как JSON
            persona=self.USER_PROMO_USAGE_PERSONA_FOR_PROMOCODE, # Специальная "персона" для логов промокодов
            preference_type='json'
        )
        logger.info(f"Промокод ID {promocode_id} помечен как использованный пользователем DB_ID {user_id_db}. Order ID: {order_id}. Log key: {usage_log_key}")


    def generate_random_code(self, length: int = 8, prefix: str = "PROMO") -> str:
        """Генерирует случайный промокод с заданным префиксом и длиной случайной части."""
        random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))
        return f"{prefix.upper().strip()}{random_part}"

    @handle_errors(reraise_as=ValidationError)
    async def create_promocode(
        self,
        discount_type: PromoCodeDiscountType, # Enum
        discount_value: float,
        code: Optional[str] = None,
        max_uses: Optional[int] = 1,
        max_uses_per_user: Optional[int] = 1,
        user_specific_id: Optional[int] = None, # DB ID пользователя
        expires_in_days: Optional[int] = None,
        expires_at_date: Optional[datetime] = None, # Явная дата истечения
        active_from_date: Optional[datetime] = None, # Явная дата начала активности
        description: Optional[str] = None,
        user_facing_description: Optional[str] = None,
        code_type: PromoCodeType = PromoCodeType.GENERIC, # Enum
        trial_tier_target: Optional[SubscriptionTier] = None, # Enum
        for_subscription_tier: Optional[SubscriptionTier] = None, # Enum
        min_purchase_amount: Optional[int] = None,
        is_active: bool = True,
        created_by_admin_id: Optional[int] = None, # DB ID админа или 0 для системы
        # Новые поля
        is_for_first_time_users: bool = False,
        is_for_upgrade_only: bool = False,
        is_seasonal: bool = False,
        seasonal_event: Optional[str] = None,
        min_account_age_days: Optional[int] = None,
        allowed_user_segments: Optional[List[str]] = None,
        allowed_countries: Optional[List[str]] = None,
        blocked_countries: Optional[List[str]] = None,
        bonus_message_expiry_days: Optional[int] = 30,
        feature_unlock_target: Optional[str] = None # Какую фичу разблокировать
    ) -> PromoCode: # Возвращает датакласс PromoCode
        
        if code:
            code_upper = code.strip().upper()
            if not code_upper: # Проверка на пустую строку после strip
                raise ValidationError("Предоставленный код промокода не может быть пустым после очистки.")
        else: # Генерируем код, если не предоставлен
            prefix_map = {
                PromoCodeDiscountType.PERCENTAGE: "SALE",
                PromoCodeDiscountType.FIXED_AMOUNT: "CASH",
                PromoCodeDiscountType.BONUS_MESSAGES: "BONUS",
                PromoCodeDiscountType.FREE_TRIAL: "TRIAL",
                PromoCodeDiscountType.FEATURE_UNLOCK: "UNLOCK"
            }
            final_prefix = prefix_map.get(discount_type, "PROMO")
            # Пытаемся сгенерировать уникальный код
            for _ in range(5): # 5 попыток
                generated_code = self.generate_random_code(length=8, prefix=final_prefix)
                if not await self.db_service.get_promocode_by_code(generated_code):
                    code_upper = generated_code
                    break
            else: # Если не удалось за 5 попыток
                logger.error("Не удалось сгенерировать уникальный промокод после нескольких попыток.")
                raise DatabaseError("Не удалось сгенерировать уникальный промокод.")
            logger.info(f"Сгенерирован новый промокод: {code_upper}")

        # Валидация параметров
        if discount_type == PromoCodeDiscountType.FREE_TRIAL and not trial_tier_target:
            raise ValidationError("Для триального промокода необходимо указать trial_tier_target (целевой тариф).")
        if trial_tier_target and discount_type != PromoCodeDiscountType.FREE_TRIAL:
            logger.warning(f"trial_tier_target ('{trial_tier_target.value if trial_tier_target else None}') указан, но тип скидки не FREE_TRIAL ({discount_type.value}). Поле будет проигнорировано.")
            trial_tier_target = None # Сбрасываем, если нерелевантно
        
        if code_type == PromoCodeType.USER_SPECIFIC and user_specific_id is None:
            raise ValidationError("Для USER_SPECIFIC промокода необходимо указать user_specific_id (DB ID пользователя).")

        # Обработка дат
        final_expires_at: Optional[datetime] = None
        if expires_at_date:
            final_expires_at = expires_at_date.replace(tzinfo=timezone.utc) if expires_at_date.tzinfo is None else expires_at_date
        elif expires_in_days is not None and expires_in_days > 0:
            final_expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        
        final_active_from: Optional[datetime] = None
        if active_from_date:
            final_active_from = active_from_date.replace(tzinfo=timezone.utc) if active_from_date.tzinfo is None else active_from_date

        # Подготовка данных для сохранения в DBPromoCode
        db_promo_data = {
            "code": code_upper,
            "discount_type": discount_type.value, # Сохраняем значение Enum
            "discount_value": discount_value,
            "max_uses": max_uses,
            "uses_count": 0, # Новый промокод
            "max_uses_per_user": max_uses_per_user,
            "user_specific_id": user_specific_id,
            "active_from": final_active_from,
            "expires_at": final_expires_at,
            "is_active": is_active,
            "description": description,
            "user_facing_description": user_facing_description,
            "code_type": code_type.value, # Сохраняем значение Enum
            "trial_tier_target": trial_tier_target.value if trial_tier_target else None,
            "for_subscription_tier": for_subscription_tier.value if for_subscription_tier else None,
            "min_purchase_amount": min_purchase_amount,
            "created_by_admin_id": created_by_admin_id,
            "is_for_first_time_users": is_for_first_time_users,
            "is_for_upgrade_only": is_for_upgrade_only,
            "is_seasonal": is_seasonal,
            "seasonal_event": seasonal_event,
            "min_account_age_days": min_account_age_days,
            "allowed_user_segments": json.dumps(allowed_user_segments) if allowed_user_segments else None,
            "allowed_countries": json.dumps(allowed_countries) if allowed_countries else None,
            "blocked_countries": json.dumps(blocked_countries) if blocked_countries else None,
            "bonus_message_expiry_days": bonus_message_expiry_days,
            # "feature_unlock_target": feature_unlock_target # Если поле будет добавлено в DBPromoCode
        }
        # Убираем None значения, которые не должны быть переданы в конструктор DBPromoCode, если у них нет default
        db_promo_data_cleaned = {k: v for k, v in db_promo_data.items() if v is not None or k in [
            "max_uses", "max_uses_per_user", "user_specific_id", "active_from", "expires_at",
            "description", "user_facing_description", "trial_tier_target", "for_subscription_tier",
            "min_purchase_amount", "created_by_admin_id", "seasonal_event", "min_account_age_days",
            "allowed_user_segments", "allowed_countries", "blocked_countries", "bonus_message_expiry_days"
            # "feature_unlock_target"
        ]}

        try:
            db_promo_model_to_save = DBPromoCode(**db_promo_data_cleaned)
            saved_db_promo = await self.db_service.save_promocode(db_promo_model_to_save)
            logger.info(f"Промокод '{saved_db_promo.code}' (ID: {saved_db_promo.id}) успешно создан в БД.")
            return PromoCode.from_db_model(saved_db_promo)
        except IntegrityError as e: # Ошибка уникальности кода
            logger.error(f"Ошибка IntegrityError при создании промокода '{code_upper}': {e}. Возможно, такой код уже существует.")
            raise ValidationError(f"Промокод '{code_upper}' уже существует или другая ошибка уникальности.") from e
        except DatabaseError as e: # Другие ошибки БД
            logger.error(f"Ошибка DatabaseError при создании промокода '{code_upper}': {e}", exc_info=True)
            raise # Перебрасываем, чтобы было обработано выше
        except Exception as e: # Неожиданные ошибки
            logger.error(f"Неожиданная ошибка при создании промокода '{code_upper}': {e}", exc_info=True)
            raise ValidationError(f"Не удалось создать промокод '{code_upper}'.") from e

    @handle_errors(log_level="INFO", reraise_as=None) # Не перебрасываем ошибку, т.к. это админская команда
    async def get_all_promocodes_admin(self, active_only: bool = False, page: int = 1, page_size: int = 20) -> Tuple[List[PromoCode], int]:
        """Получает все промокоды (для админ-панели) с пагинацией."""
        db_promos, total_count = await self.db_service.get_all_promocodes_paginated(
            active_only=active_only, page=page, page_size=page_size
        )
        return [PromoCode.from_db_model(db_p) for db_p in db_promos], total_count

    @handle_errors(log_level="INFO", reraise_as=ValidationError)
    async def deactivate_promocode(self, promocode_code_or_id: Union[str, int]) -> Optional[PromoCode]:
        """Деактивирует промокод (устанавливает is_active = False)."""
        db_promo: Optional[DBPromoCode] = None
        if isinstance(promocode_code_or_id, str):
            db_promo = await self.db_service.get_promocode_by_code(promocode_code_or_id.upper())
            if not db_promo:
                raise ValidationError(f"Промокод '{promocode_code_or_id.upper()}' не найден для деактивации.")
        elif isinstance(promocode_code_or_id, int):
            db_promo = await self.db_service.get_promocode_by_id(promocode_code_or_id)
            if not db_promo:
                raise ValidationError(f"Промокод с ID {promocode_code_or_id} не найден для деактивации.")
        else:
            raise ValueError("Некорректный идентификатор промокода для деактивации.")

        if not db_promo.is_active:
            logger.info(f"Промокод '{db_promo.code}' (ID: {db_promo.id}) уже деактивирован.")
            return PromoCode.from_db_model(db_promo) 

        db_promo.is_active = False
        db_promo.updated_at = datetime.now(timezone.utc)
        
        try:
            updated_db_promo = await self.db_service.save_promocode(db_promo) 
            logger.info(f"Промокод '{updated_db_promo.code}' (ID: {updated_db_promo.id}) успешно деактивирован.")
            return PromoCode.from_db_model(updated_db_promo)
        except Exception as e:
            logger.error(f"Ошибка при сохранении деактивированного промокода '{db_promo.code}': {e}", exc_info=True)
            raise DatabaseError(f"Не удалось обновить статус промокода '{db_promo.code}'.")


    @handle_errors(log_level="INFO", reraise_as=ValidationError)
    async def delete_promocode(self, promocode_code_or_id: Union[str, int]) -> bool:
        """Удаляет промокод из базы данных."""
        promo_id_to_delete: Optional[int] = None
        code_for_log: str = ""

        if isinstance(promocode_code_or_id, str):
            code_upper = promocode_code_or_id.upper()
            code_for_log = code_upper
            db_promo = await self.db_service.get_promocode_by_code(code_upper)
            if not db_promo:
                raise ValidationError(f"Промокод '{code_upper}' не найден для удаления.")
            promo_id_to_delete = db_promo.id
        elif isinstance(promocode_code_or_id, int):
            promo_id_to_delete = promocode_code_or_id
            db_promo_check = await self.db_service.get_promocode_by_id(promo_id_to_delete)
            if not db_promo_check:
                 raise ValidationError(f"Промокод с ID {promo_id_to_delete} не найден для удаления.")
            code_for_log = db_promo_check.code
        else:
            raise ValueError("Некорректный идентификатор промокода для удаления.")

        if promo_id_to_delete is None: 
            raise ValidationError("Не удалось определить ID промокода для удаления.")

        deleted_success = await self.db_service.delete_promocode_db(promo_id_to_delete)

        if deleted_success:
            logger.info(f"Промокод '{code_for_log}' (ID: {promo_id_to_delete}) успешно удален.")
            return True
        else:
            logger.warning(f"Не удалось удалить промокод '{code_for_log}' (ID: {promo_id_to_delete}) из БД. Возможно, он уже был удален.")
            return False


class PromocodeSecurityManager:
    """Управляет безопасностью и предотвращением злоупотреблений с промокодами."""
    PROMOCODE_VALIDATE_ATTEMPT_KEY_10M = "promo_validate_attempt_10m" # Ключ для UserActionTimestamp
    PROMOCODE_VALIDATE_ATTEMPT_KEY_1H = "promo_validate_attempt_1h"  # Ключ для UserActionTimestamp

    def __init__(self, db_service: DatabaseService, config: BotConfig):
        self.db_service = db_service
        self.config = config
        # Лимиты из BotConfig или значения по умолчанию
        self.attempt_limit_10m = getattr(config, 'promocode_attempt_limit_10m', 10)
        self.attempt_limit_1h = getattr(config, 'promocode_attempt_limit_1h', 30)

    @handle_errors(reraise_as=None) # Не перебрасываем, чтобы вернуть результат анализа
    async def detect_promocode_abuse(self, user_id_tg: int, promocode_str: str,
                                   context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Обнаруживает потенциальные злоупотребления с промокодами."""
        abuse_signals: Dict[str, Any] = {
            "risk_score": 0.0, "detected_patterns": [],
            "recommended_action": "allow", # 'allow', 'monitor', 'verify', 'block'
            "message_for_user": None # Сообщение для пользователя, если действие не 'allow'
        }
        context = context or {} # Дополнительный контекст (например, IP, user_agent - если доступно)
        
        # 1. Получаем DB ID пользователя
        user_db = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not user_db:
            logger.warning(f"Promocode abuse check: User TG {user_id_tg} not found in DB. Risk slightly increased.")
            abuse_signals["risk_score"] = 0.1 # Небольшое повышение риска
            abuse_signals["detected_patterns"].append("user_not_found_in_db_for_security_check")
            # Не блокируем сразу, но это подозрительно, если пользователь пытается ввести промокод до создания записи в БД.
            return abuse_signals 

        now_utc = datetime.now(timezone.utc)

        # 2. Rate Limiting на основе UserActionTimestamp
        # Записываем текущую попытку
        await self.db_service.add_user_action_timestamp(user_db.id, self.PROMOCODE_VALIDATE_ATTEMPT_KEY_10M, now_utc)
        await self.db_service.add_user_action_timestamp(user_db.id, self.PROMOCODE_VALIDATE_ATTEMPT_KEY_1H, now_utc)

        # Считаем попытки за последние 10 минут и час
        count_10m = await self.db_service.count_user_actions_in_window(
            user_db.id, self.PROMOCODE_VALIDATE_ATTEMPT_KEY_10M, now_utc - timedelta(minutes=10))
        count_1h = await self.db_service.count_user_actions_in_window(
            user_db.id, self.PROMOCODE_VALIDATE_ATTEMPT_KEY_1H, now_utc - timedelta(hours=1))

        if count_10m > self.attempt_limit_10m:
            abuse_signals["risk_score"] += 0.5
            abuse_signals["detected_patterns"].append(f"rate_limit_10m_exceeded ({count_10m}/{self.attempt_limit_10m})")
            abuse_signals["recommended_action"] = "block"
            abuse_signals["message_for_user"] = f"Слишком много попыток ввода промокода. Пожалуйста, подождите около 10 минут."
        
        if count_1h > self.attempt_limit_1h and abuse_signals["recommended_action"] != "block": # Если уже не заблокирован по 10м
            abuse_signals["risk_score"] += 0.3
            abuse_signals["detected_patterns"].append(f"rate_limit_1h_exceeded ({count_1h}/{self.attempt_limit_1h})")
            abuse_signals["recommended_action"] = "verify" # verify - более мягкая мера, чем block
            if not abuse_signals["message_for_user"]: # Если еще нет сообщения
                 abuse_signals["message_for_user"] = "Вы слишком часто пытаетесь ввести промокоды. Попробуйте позже."
        
        # 3. Анализ самого промокода (длина, паттерны - базовая проверка)
        if len(promocode_str) < 4 or len(promocode_str) > 25: # Слишком короткий или длинный
            abuse_signals["risk_score"] += 0.1
            abuse_signals["detected_patterns"].append("promocode_suspicious_length")
        
        # TODO: Добавить более сложные проверки паттернов, если необходимо
        # - Использование известных "мусорных" последовательностей
        # - Сверка с базой известных "слитых" промокодов (если есть)

        # 4. Анализ истории пользователя (если есть)
        # - Частота успешного применения промокодов
        # - История блокировок или предупреждений
        # user_promo_history = await self.db_service.get_user_promocode_application_history(user_db.id, limit=10)
        # if user_promo_history and len(user_promo_history) > 5 and abuse_signals["risk_score"] < 0.5:
        #      # Если пользователь часто успешно применяет промокоды, это может быть нормально
        #      pass # Не повышаем риск, если это "промокод-хантер", но не злоумышленник
        
        # Финальное решение на основе risk_score, если не было явного блока по rate limit
        abuse_signals["risk_score"] = min(max(0.0, abuse_signals["risk_score"]), 1.0) # Нормализуем 0-1

        if abuse_signals["recommended_action"] == "allow": # Если еще не block или verify
            if abuse_signals["risk_score"] >= 0.7:
                abuse_signals["recommended_action"] = "block"
                if not abuse_signals["message_for_user"]: abuse_signals["message_for_user"] = "Ввод промокодов временно ограничен из-за подозрительной активности."
            elif abuse_signals["risk_score"] >= 0.4:
                abuse_signals["recommended_action"] = "verify"
                if not abuse_signals["message_for_user"]: abuse_signals["message_for_user"] = "Для продолжения может потребоваться дополнительная проверка."
            elif abuse_signals["risk_score"] >= 0.2: # Небольшой риск - просто мониторим
                abuse_signals["recommended_action"] = "monitor"
        
        if abuse_signals["risk_score"] >= 0.2 or abuse_signals["recommended_action"] != "allow":
            logger.warning(
                f"Promocode abuse detection for user TG ID {user_id_tg}, code '{promocode_str}': "
                f"Risk Score: {abuse_signals['risk_score']:.2f}, Patterns: {abuse_signals['detected_patterns']}, "
                f"Recommended Action: {abuse_signals['recommended_action']}"
            )
            # TODO: Рассмотреть возможность логирования в отдельную таблицу SecurityLog или BotStatistics
            # await self.db_service.save_security_event(...)
            
        return abuse_signals


class PromocodeMonitoringService: # Заглушка, как в Roadmap
    """Сервис для мониторинга активности промокодов в реальном времени."""
    def __init__(self, db_service: DatabaseService, config: BotConfig, notification_service: Optional[Any] = None):
        self.db_service = db_service
        self.config = config
        self.notification_service = notification_service # Для отправки алертов
        self.monitoring_task: Optional[asyncio.Task] = None
        self.last_monitoring_run: Optional[datetime] = None

    async def start_monitoring(self):
        if self.monitoring_task and not self.monitoring_task.done():
            logger.info("Мониторинг системы промокодов уже запущен.")
            return
        self.monitoring_task = asyncio.create_task(self._monitor_activity_loop())
        logger.info("Сервис мониторинга промокодов запущен.")

    async def stop_monitoring(self):
        if self.monitoring_task and not self.monitoring_task.done():
            self.monitoring_task.cancel()
            try:
                await self.monitoring_task
            except asyncio.CancelledError:
                logger.info("Мониторинг системы промокодов остановлен.")
            self.monitoring_task = None
        else:
            logger.info("Мониторинг системы промокодов не был запущен или уже остановлен.")

    async def _monitor_activity_loop(self):
        """Основной цикл мониторинга."""
        check_interval_seconds = getattr(self.config, 'promocode_monitor_interval_sec', 300) # Из BotConfig
        logger.info(f"Цикл мониторинга промокодов будет выполняться каждые {check_interval_seconds} секунд.")
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                logger.info(f"Запуск проверки мониторинга промокодов: {now_utc.isoformat()}")
                self.last_monitoring_run = now_utc
                
                # TODO: Реализовать логику мониторинга:
                # 1. _monitor_abuse_patterns(): Проверка на аномально частое использование/попытки ввода.
                #    - Получить статистику попыток ввода (UserActionTimestamp) за последние N минут.
                #    - Получить статистику успешных применений за последние N минут.
                #    - Сравнить с пороговыми значениями.
                # 2. _monitor_unusual_spikes(): Всплески использования конкретных промокодов.
                #    - Сравнить текущее использование промокодов с их средним использованием.
                # 3. _monitor_high_value_usage(): Использование промокодов с очень большой скидкой.
                #    - Отслеживать применение промокодов с discount_value > X%.
                # 4. _monitor_system_health(): Общее состояние системы промокодов (например, ошибки валидации).
                
                logger.debug("Проверка мониторинга промокодов завершена (заглушка).")
                
                await asyncio.sleep(check_interval_seconds)
            except asyncio.CancelledError:
                logger.info("Цикл мониторинга промокодов прерван.")
                break
            except Exception as e:
                logger.error(f"Ошибка в цикле мониторинга промокодов: {e}", exc_info=True)
                # В случае ошибки, ждем немного дольше перед следующей попыткой
                await asyncio.sleep(min(check_interval_seconds, 300)) # Не чаще, чем раз в 5 минут при ошибках

    async def get_monitoring_status(self) -> Dict[str, Any]:
        """Возвращает статус сервиса мониторинга."""
        return {
            "is_running": self.monitoring_task is not None and not self.monitoring_task.done(),
            "last_run_at": self.last_monitoring_run.isoformat() if self.last_monitoring_run else None,
            "check_interval_seconds": getattr(self.config, 'promocode_monitor_interval_sec', 300)
            # TODO: Добавить сюда ключевые показатели мониторинга, если они агрегируются
        }
