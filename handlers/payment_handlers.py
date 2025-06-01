# handlers/payment_handlers.py
import logging
import json
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, TYPE_CHECKING, Union, Optional, List

from aiogram import Router, F, types, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command, StateFilter
from aiogram.utils.markdown import hbold, hitalic
from aiogram.exceptions import TelegramAPIError

# Импорты из вашего проекта
from config.settings import BotConfig
from services.subscription_system import SubscriptionService, SubscriptionTier, UserSubscriptionData, SubscriptionStatus
from services.promocode_system import PromoCodeService, ValidationError as PromoCodeValidationError, PromoCode as DataClassPromoCode, PromoCodeDiscountType
from services.referral_ab_testing import ReferralService # Добавлен импорт
from utils.navigation import navigation
from utils.error_handler import handle_errors, ErrorHandler

if TYPE_CHECKING:
    from main import AICompanionBot
    # from services.achievement_service import AchievementService # Пример для будущих сервисов
    # from services.personalization_service import PersonalizationService # Пример
    # from services.marketing_service import MarketingService # Пример

logger = logging.getLogger(__name__)
payment_router = Router()

# --- FSM для ввода промокода ---
class PromoCodeFSM(StatesGroup):
    """States for the FSM of entering a promo code before payment."""
    waiting_for_code_entry = State()
    code_applied_waiting_payment_confirmation = State()

# --- Утилитарные функции для Payload ---

def get_subscription_payload(user_id_tg: int, tier_value: str,
                           duration_months: int, payment_payload_secret: str,
                           promocode: Optional[str] = None) -> str:
    """
    Generate unique tracking payload for payment.
    Uses a secret key from BotConfig for signature.
    """
    payload_data = {
        "user_id_tg": user_id_tg,
        "tier_value": tier_value,
        "duration_months": duration_months,
        "promocode": promocode,
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "version": "1.0"
    }
    payload_json = json.dumps(payload_data, sort_keys=True, ensure_ascii=False)
    if not payment_payload_secret:
        logger.critical("PAYMENT_PAYLOAD_SECRET не установлен в конфигурации! Подпись payload не будет безопасной.")
        signature_input = payload_json + "TEMPORARY_FALLBACK_SECRET_CHANGE_ME"
    else:
        signature_input = payload_json + payment_payload_secret
    signature = hashlib.sha256(signature_input.encode('utf-8')).hexdigest()[:16]
    payload_data["sig"] = signature
    return json.dumps(payload_data, ensure_ascii=False)

def parse_subscription_payload(payload_str: str, payment_payload_secret: str) -> Optional[Dict[str, Any]]:
    """
    Parse and validate payment payload.
    Uses a secret key from BotConfig for signature verification.
    """
    try:
        data = json.loads(payload_str)
        signature = data.pop("sig", None)
        if signature is None:
            logger.warning(f"Payload signature missing: {payload_str}")
            return None
        payload_json = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if not payment_payload_secret:
            logger.critical("PAYMENT_PAYLOAD_SECRET не установлен для проверки подписи payload!")
            expected_signature_input = payload_json + "TEMPORARY_FALLBACK_SECRET_CHANGE_ME"
        else:
            expected_signature_input = payload_json + payment_payload_secret
        expected_sig = hashlib.sha256(expected_signature_input.encode('utf-8')).hexdigest()[:16]
        if signature != expected_sig:
            logger.warning(f"Invalid payload signature: '{signature}' vs expected '{expected_sig}'. Payload: {payload_str}")
            return None
        required_keys = ["user_id_tg", "tier_value", "duration_months", "timestamp"]
        if not all(key in data for key in required_keys):
            logger.error(f"Missing required keys in parsed payload: {data}")
            return None
        if not (isinstance(data["user_id_tg"], int) and
                isinstance(data["tier_value"], str) and
                isinstance(data["duration_months"], int) and data["duration_months"] > 0 and
                isinstance(data["timestamp"], int)):
            logger.error(f"Invalid data types or values in parsed payload: {data}")
            return None
        return data
    except json.JSONDecodeError as e:
        logger.error(f"Payload JSON decoding error: {e}. Payload: {payload_str}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error parsing payload: {e}. Payload: {payload_str}", exc_info=True)
        return None

# --- Вспомогательная функция для уведомления администратора ---
async def _notify_admin_critical_error(bot: Bot, admin_user_ids: List[int], message_text: str, context: Optional[Dict[str, Any]] = None):
    """Sends a critical error notification to admin(s)."""
    if not admin_user_ids:
        logger.warning("ADMIN_USER_IDS не настроены. Критическое уведомление не отправлено.")
        return
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    full_message = f"🚨 {hbold('CRITICAL PAYMENT SYSTEM ERROR')} 🚨\n\n"
    full_message += f"**Time**: {timestamp}\n"
    full_message += f"**Error**: {message_text}\n"
    if context:
        full_message += "\n**Context**:\n"
        try:
            context_str = json.dumps(context, indent=2, ensure_ascii=False, default=str)
            full_message += f"```json\n{context_str}\n```\n"
        except Exception:
            full_message += f"{str(context)[:1000]}\n"
    for admin_id in admin_user_ids:
        try:
            await bot.send_message(admin_id, full_message, parse_mode="Markdown")
            logger.info(f"Критическое уведомление об ошибке платежной системы отправлено администратору {admin_id}.")
        except Exception as e:
            logger.error(f"Не удалось отправить критическое уведомление администратору {admin_id}: {e}")

# --- Создание Invoice ---
async def create_invoice_link(
    bot: Bot,
    config: BotConfig,
    user_id_tg: int,
    tier_enum: SubscriptionTier,
    duration_months: int,
    subscription_service: SubscriptionService,
    promocode: Optional[str] = None,
    applied_effects: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    Создание invoice для Telegram Stars.
    """
    try:
        tier_config = subscription_service.plans.PLANS.get(tier_enum)
        if not tier_config:
            logger.error(f"Конфигурация для тарифа {tier_enum.value} не найдена.")
            await _notify_admin_critical_error(bot, config.admin_user_ids, f"Missing tier config for {tier_enum.value}", {"user_id_tg": user_id_tg})
            return None

        base_price = 0
        description_duration_text = ""
        if duration_months == 1:
            base_price = tier_config.price_stars_monthly
            description_duration_text = "1 месяц"
        elif duration_months == 12:
            base_price = tier_config.price_stars_yearly
            description_duration_text = "1 год"
        else:
            logger.error(f"Неподдерживаемая длительность {duration_months} мес. для тарифа {tier_enum.value}, user {user_id_tg}")
            await _notify_admin_critical_error(bot, config.admin_user_ids, f"Unsupported duration {duration_months} for tier {tier_enum.value}", {"user_id_tg": user_id_tg})
            return None

        if base_price <= 0 and tier_enum != SubscriptionTier.FREE:
            logger.error(f"Нулевая или отрицательная цена ({base_price}) для платного тарифа {tier_enum.value} ({description_duration_text}), user {user_id_tg}.")
            await _notify_admin_critical_error(bot, config.admin_user_ids, f"Zero/Negative price for paid tier {tier_enum.value}", {"user_id_tg": user_id_tg, "base_price": base_price})
            return None

        final_price = base_price
        discount_applied_description = ""
        if applied_effects and 'final_price_after_discount' in applied_effects:
            final_price = int(applied_effects['final_price_after_discount'])
            original_price_for_effect = applied_effects.get('original_price_stars', base_price)
            discount_val_for_effect = applied_effects.get('discount_applied_stars', 0) # Инициализируем 0
            if not discount_val_for_effect and applied_effects.get('discount_percentage'): # Если скидка в %, а не в звездах
                 discount_val_for_effect = original_price_for_effect * (applied_effects.get('discount_percentage', 0)/100.0)

            if discount_val_for_effect > 0 :
                 discount_applied_description = f"\nСкидка по промокоду '{promocode}' применена!"
                 if applied_effects.get('description'):
                     discount_applied_description += f" ({applied_effects['description']})"

        if final_price <= 0 and tier_enum != SubscriptionTier.FREE:
            logger.info(f"Финальная цена для тарифа {tier_enum.value} равна {final_price} звезд (промокод: {promocode}). Активация без инвойса для user {user_id_tg}.")
            return "PROMO_ACTIVATED_FREE"

        payload_str = get_subscription_payload(
            user_id_tg, tier_enum.value, duration_months,
            config.payment_payload_secret, promocode
        )
        invoice_title = f"🌟 Подписка: {tier_config.tier_name}"
        invoice_description = (
            f"Доступ к тарифу «{tier_config.tier_name}» на {description_duration_text}.\n"
            f"Разблокируйте все эксклюзивные функции и возможности AI Companion! ✨"
            f"{discount_applied_description}"
        )[:255]
        prices = [types.LabeledPrice(label=f"{tier_config.tier_name} - {description_duration_text}", amount=final_price)]
        
        invoice_link = await bot.create_invoice_link(
            title=invoice_title, description=invoice_description, payload=payload_str,
            provider_token="", currency="XTR", prices=prices,
            need_email=False, need_phone_number=False, need_shipping_address=False, is_flexible=False
        )
        logger.info(f"Invoice link created for user {user_id_tg}: tier={tier_enum.value}, duration={duration_months} мес., price={final_price} XTR, promocode={promocode}. Link: {invoice_link}")
        return invoice_link
    except TelegramAPIError as e:
        logger.error(f"Telegram API error during invoice creation for user {user_id_tg}: {e}. Method: {e.method}, Message: {e.message}", exc_info=True)
        await _notify_admin_critical_error(bot, config.admin_user_ids, f"Telegram API Error creating invoice for user {user_id_tg}: {e.method} - {e.message}", {"user_id_tg": user_id_tg, "tier": tier_enum.value})
        return f"ERROR_API_{e.method}"
    except Exception as e:
        logger.error(f"Unexpected error creating invoice link for user {user_id_tg}: {e}", exc_info=True)
        await _notify_admin_critical_error(bot, config.admin_user_ids, f"Unexpected error creating invoice for user {user_id_tg}", {"user_id_tg": user_id_tg, "error": str(e)})
        return None

# --- Обработчики FSM для промокода ---
@payment_router.callback_query(F.data == "action_enter_promocode_start")
async def enter_promocode_start_handler(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not callback.message: return await callback.answer("Ошибка отображения.")
    await state.set_state(PromoCodeFSM.waiting_for_code_entry)
    await state.update_data(
        last_menu_message_id=callback.message.message_id,
        last_menu_chat_id=callback.message.chat.id,
        target_tier_for_promo=None
    )
    await callback.message.edit_text(
        "🎁 Введите ваш промокод или нажмите 'Отмена':",
        reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="⬅️ Отмена", callback_data="action_cancel_promocode_entry")]
        ])
    )
    await callback.answer()

@payment_router.message(PromoCodeFSM.waiting_for_code_entry, F.text)
async def process_promocode_entry_handler(message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not message.text or not message.from_user: return
    promocode_str = message.text.strip().upper()
    user_id_tg = message.from_user.id
    db_user = await bot_instance.db_service.get_user_by_telegram_id(user_id_tg)
    if not db_user:
        await message.reply("Произошла ошибка с вашим профилем. Попробуйте /start и затем снова.")
        return

    promocode_service: PromoCodeService = bot_instance.promocode_service
    fsm_data = await state.get_data()
    target_tier_for_promo_value = fsm_data.get("target_tier_for_promo")
    target_tier_enum = SubscriptionTier(target_tier_for_promo_value) if target_tier_for_promo_value else None

    try:
        validated_promo: DataClassPromoCode = await promocode_service.validate_promocode(
            code=promocode_str, user_id_db=db_user.id, target_tier_for_purchase=target_tier_enum
        )
        applied_effects = await promocode_service.apply_promocode_effects(
            user_id_db=db_user.id, user_id_tg=user_id_tg, promo=validated_promo, purchase_amount_stars=None
        )
        await state.update_data(applied_promocode=promocode_str, applied_effects=applied_effects)
        success_message = f"✅ Промокод {hbold(promocode_str)} успешно применен!\n\n"
        success_message += f"{hitalic(applied_effects.get('description', 'Эффект промокода будет учтен при оплате.'))}\n\n"
        buttons: List[List[types.InlineKeyboardButton]] = []

        if applied_effects.get("discount_type") in [PromoCodeDiscountType.FREE_TRIAL.value, PromoCodeDiscountType.BONUS_MESSAGES.value]:
            if applied_effects.get("discount_type") == PromoCodeDiscountType.FREE_TRIAL.value:
                 success_message += "🎉 Ваш триальный период активирован! Наслаждайтесь!"
            elif applied_effects.get("discount_type") == PromoCodeDiscountType.BONUS_MESSAGES.value:
                 success_message += "✨ Бонусные сообщения уже начислены на ваш аккаунт!"
            buttons.append([types.InlineKeyboardButton(text="🎮 В главное меню", callback_data="nav_main")])
            if validated_promo.id:
                await promocode_service.mark_promocode_as_used(validated_promo.id, db_user.id)
            await state.clear()
        else:
            await state.set_state(PromoCodeFSM.code_applied_waiting_payment_confirmation)
            success_message += "Теперь вы можете перейти к выбору и оплате тарифа со скидкой."
            buttons.extend([
                [types.InlineKeyboardButton(text="💳 К выбору тарифа", callback_data="nav_subscription_plans_view")],
                [types.InlineKeyboardButton(text="🚫 Отменить промокод", callback_data="action_cancel_applied_promocode")]
            ])
        await message.answer(success_message, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    except PromoCodeValidationError as e:
        await message.reply(f"😕 Ошибка: {str(e)}\nПожалуйста, проверьте промокод или попробуйте другой.")
    except Exception as e:
        logger.error(f"Ошибка применения промокода {promocode_str} для user {user_id_tg}: {e}", exc_info=True)
        error_handler: ErrorHandler = bot_instance.error_handler_instance
        error_id = error_handler.log_error(e, context={"promocode": promocode_str, "user_id_tg": user_id_tg})
        await message.reply(f"Произошла внутренняя ошибка при проверке промокода (Код: `{error_id}`). Попробуйте позже.", parse_mode="Markdown")

@payment_router.callback_query(F.data == "action_cancel_promocode_entry", StateFilter(PromoCodeFSM.waiting_for_code_entry))
@payment_router.callback_query(F.data == "action_cancel_applied_promocode", StateFilter(PromoCodeFSM.code_applied_waiting_payment_confirmation))
async def cancel_promocode_fsm_handler(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not callback.message: return await callback.answer("Ошибка")
    await state.clear()
    nav_handler = bot_instance.navigation_handlers_instance
    current_persona = await bot_instance._get_current_persona(callback.from_user.id)
    await nav_handler._show_menu_node(callback, callback.from_user.id, "profile_premium_main", current_persona) # type: ignore
    await callback.answer("Ввод/применение промокода отменено.")

# --- Вспомогательная функция для post-payment действий ---
async def _execute_post_payment_actions(
    bot_instance: 'AICompanionBot',
    user_id_tg: int,
    tier_value: str, # Новый активированный тариф
    duration_months: int,
    payment_amount_stars: int,
    telegram_charge_id: str,
    promocode_used: Optional[str]
):
    """Выполняет действия после успешной оплаты и активации подписки."""
    logger.info(f"Executing post-payment actions for user {user_id_tg}, new tier {tier_value}.")
    actions_results: Dict[str, Any] = {"referral_processed": False, "achievements_updated": False}

    try:
        # 1. Реферальная система
        referral_service: ReferralService = bot_instance.referral_service
        # Проверяем, первая ли это платная покупка пользователя, и был ли он приглашен
        # Эта логика должна быть частью referral_service.mark_referral_as_completed
        # или вызываться оттуда.
        # Для примера, если referral_service.mark_referral_as_completed сам проверяет первую покупку:
        await referral_service.mark_referral_as_completed(user_id_tg) # Предполагаем, что этот метод сам проверит, нужно ли что-то делать
        actions_results["referral_processed"] = True # Условно, т.к. сам метод не возвращает успех
        logger.info(f"Post-payment: Referral completion processed for user {user_id_tg}.")

    except Exception as e_referral:
        logger.error(f"Post-payment: Error processing referral completion for user {user_id_tg}: {e_referral}", exc_info=True)
        actions_results["referral_error"] = str(e_referral)

    # 2. Достижения и геймификация (заглушка)
    try:
        # achievement_service: 'AchievementService' = bot_instance.achievement_service # Если есть
        # await achievement_service.unlock_payment_achievement(user_id_tg, tier_value, payment_amount_stars)
        logger.info(f"Post-payment: Achievement system (stub) called for user {user_id_tg}.")
        actions_results["achievements_updated"] = True # Заглушка
    except AttributeError:
        logger.debug("Post-payment: AchievementService not available on bot_instance.")
    except Exception as e_achieve:
        logger.error(f"Post-payment: Error updating achievements for user {user_id_tg}: {e_achieve}", exc_info=True)
        actions_results["achievement_error"] = str(e_achieve)

    # 3. Персонализация и рекомендации (заглушка)
    try:
        # personalization_service: 'PersonalizationService' = bot_instance.personalization_service # Если есть
        # await personalization_service.update_user_profile_on_purchase(user_id_tg, tier_value)
        logger.info(f"Post-payment: Personalization system (stub) called for user {user_id_tg}.")
    except AttributeError:
        logger.debug("Post-payment: PersonalizationService not available on bot_instance.")
    except Exception as e_personalize:
        logger.error(f"Post-payment: Error updating personalization for user {user_id_tg}: {e_personalize}", exc_info=True)

    # 4. Маркетинговые действия (заглушка)
    try:
        # marketing_service: 'MarketingService' = bot_instance.marketing_service # Если есть
        # await marketing_service.trigger_post_purchase_campaign(user_id_tg, tier_value)
        logger.info(f"Post-payment: Marketing system (stub) called for user {user_id_tg}.")
    except AttributeError:
        logger.debug("Post-payment: MarketingService not available on bot_instance.")
    except Exception as e_marketing:
        logger.error(f"Post-payment: Error triggering marketing actions for user {user_id_tg}: {e_marketing}", exc_info=True)

    logger.info(f"Post-payment actions execution summary for user {user_id_tg}: {actions_results}")


# --- Обработчики платежей ---
@payment_router.pre_checkout_query()
async def pre_checkout_query_handler(query: types.PreCheckoutQuery, bot_instance: 'AICompanionBot'):
    payload_str = query.invoice_payload
    error_handler: ErrorHandler = bot_instance.error_handler_instance
    config: BotConfig = bot_instance.config

    async def answer_fail(error_message_user: str, log_message: Optional[str] = None):
        final_log_message = log_message or error_message_user
        logger.error(f"PreCheckoutQuery FAILED for user {query.from_user.id}, payload '{payload_str}': {final_log_message}")
        try:
            await bot_instance.bot.answer_pre_checkout_query(query.id, ok=False, error_message=error_message_user)
        except TelegramAPIError as e_api:
            logger.error(f"TelegramAPIError answering FAIL PreCheckoutQuery {query.id}: {e_api}")

    parsed_payload = parse_subscription_payload(payload_str, config.payment_payload_secret)
    if not parsed_payload:
        return await answer_fail("Ошибка данных заказа. Пожалуйста, попробуйте создать новый счет.", f"Invalid payload for PreCheckoutQuery: '{payload_str}'")

    user_id_tg_from_payload = parsed_payload["user_id_tg"]
    tier_value_from_payload = parsed_payload["tier_value"]
    promocode_from_payload = parsed_payload.get("promocode")

    if query.from_user.id != user_id_tg_from_payload:
        return await answer_fail("Ошибка проверки заказа. Попробуйте сформировать заказ заново.", f"User ID mismatch: query.from_user.id ({query.from_user.id}) != payload.user_id_tg ({user_id_tg_from_payload})")

    try:
        target_tier_enum = SubscriptionTier(tier_value_from_payload)
    except ValueError:
        return await answer_fail("Выбранный тариф недействителен. Пожалуйста, выберите тариф заново.", f"Unknown tier '{tier_value_from_payload}' in payload.")

    if promocode_from_payload:
        promocode_service: PromoCodeService = bot_instance.promocode_service
        db_user = await bot_instance.db_service.get_user_by_telegram_id(query.from_user.id)
        if not db_user:
            return await answer_fail("Ошибка профиля пользователя. Попробуйте /start.", f"User TG ID {query.from_user.id} not found in DB for promocode validation.")
        try:
            await promocode_service.validate_promocode(
                code=promocode_from_payload, user_id_db=db_user.id,
                target_tier_for_purchase=target_tier_enum, purchase_amount_stars=query.total_amount
            )
        except PromoCodeValidationError as e_promo:
            return await answer_fail(f"Промокод '{promocode_from_payload}' недействителен: {str(e_promo)}", f"Promocode '{promocode_from_payload}' validation failed: {e_promo}")
        except Exception as e_val_promo:
            error_id = error_handler.log_error(e_val_promo, {"payload": payload_str, "user_id": query.from_user.id, "promocode": promocode_from_payload})
            return await answer_fail(f"Ошибка проверки промокода (Код: `{error_id}`). Попробуйте без него.", f"Exception during promocode validation (ID: {error_id}): {e_val_promo}")
    try:
        await bot_instance.bot.answer_pre_checkout_query(query.id, ok=True)
        logger.info(f"PreCheckoutQuery for user {query.from_user.id} (payload: {payload_str}) successfully confirmed.")
    except Exception as e:
        error_id = error_handler.log_error(e, {"payload": payload_str, "user_id": query.from_user.id})
        await answer_fail(f"Внутренняя ошибка обработки заказа (Код: `{error_id}`).", f"Unexpected error answering OK PreCheckoutQuery (ID: {error_id}): {e}")

@payment_router.message(F.successful_payment)
async def successful_payment_handler(message: types.Message, bot_instance: 'AICompanionBot'):
    if not message.from_user or not message.successful_payment:
        logger.warning("Получено сообщение SuccessfulPayment без from_user или successful_payment данных.")
        return

    payment_info = message.successful_payment
    payload_str = payment_info.invoice_payload
    user_id_tg = message.from_user.id
    error_handler: ErrorHandler = bot_instance.error_handler_instance
    config: BotConfig = bot_instance.config

    logger.info(f"Успешный платеж от user {user_id_tg}: {payment_info.total_amount} {payment_info.currency}, ID платежа Telegram: {payment_info.telegram_payment_charge_id}, Payload: {payload_str}")

    parsed_payload = parse_subscription_payload(payload_str, config.payment_payload_secret)
    if not parsed_payload:
        error_msg_for_log = f"Критическая ошибка: не удалось распарсить payload '{payload_str}' из SuccessfulPayment от user {user_id_tg}."
        error_id = error_handler.log_error(ValueError(error_msg_for_log), {"payload": payload_str, "user_id_tg": user_id_tg})
        await message.answer(f"Произошла ошибка при обработке вашего платежа (Код: `{error_id}`). Пожалуйста, свяжитесь с поддержкой.", parse_mode="Markdown")
        return

    if parsed_payload["user_id_tg"] != user_id_tg:
        error_msg_for_log = f"Критическая ошибка: user_id_tg в payload ({parsed_payload['user_id_tg']}) не совпадает с user_id_tg в SuccessfulPayment ({user_id_tg})."
        error_id = error_handler.log_error(ValueError(error_msg_for_log), {"payload": payload_str, "user_id_tg": user_id_tg})
        await message.answer(f"Произошла ошибка несоответствия данных платежа (Код: `{error_id}`). Пожалуйста, свяжитесь с поддержкой.", parse_mode="Markdown")
        return

    tier_value = parsed_payload["tier_value"]
    duration_months = parsed_payload["duration_months"]
    promocode_used = parsed_payload.get("promocode")
    duration_days = duration_months * 30
    if duration_months == 12: duration_days = 365

    subscription_service: SubscriptionService = bot_instance.subscription_service
    promocode_service: PromoCodeService = bot_instance.promocode_service
    db_service = bot_instance.db_service # Убрана типизация ': DatabaseService', т.к. TYPE_CHECKING не всегда работает для атрибутов экземпляра

    try:
        activation_result = await subscription_service.activate_subscription(
            user_id_tg=user_id_tg, new_tier_value=tier_value, duration_days=duration_days,
            payment_amount_stars=payment_info.total_amount,
            telegram_charge_id=payment_info.telegram_payment_charge_id,
            payment_provider="TelegramStars"
        )

        if activation_result.get("success"):
            success_msg = activation_result.get("message", f"🎉 Подписка «{subscription_service._get_tier_name(tier_value)}» успешно активирована!")
            if promocode_used:
                success_msg += f"\nПромокод {hbold(promocode_used)} учтен."
                db_user = await db_service.get_user_by_telegram_id(user_id_tg)
                if db_user:
                    promo_obj_db = await promocode_service.db_service.get_promocode_by_code(promocode_used)
                    if promo_obj_db and promo_obj_db.id:
                        await promocode_service.mark_promocode_as_used(promo_obj_db.id, db_user.id, order_id=payment_info.telegram_payment_charge_id)
                    else: logger.error(f"Не удалось найти промокод {promocode_used} в БД для отметки использования.")
                else: logger.error(f"Не удалось найти пользователя {user_id_tg} в БД для отметки промокода.")
            
            await message.answer(success_msg, parse_mode="Markdown")
            logger.info(f"Подписка успешно активирована для user {user_id_tg}. Тариф: {tier_value}, Дней: {duration_days}")
            
            # Вызов post-payment actions
            await _execute_post_payment_actions(
                bot_instance, user_id_tg, tier_value, duration_months,
                payment_info.total_amount, payment_info.telegram_payment_charge_id, promocode_used
            )
        else:
            error_msg_for_user = activation_result.get("message", "Не удалось активировать подписку после оплаты.")
            error_id = error_handler.log_error(
                Exception(f"Ошибка активации подписки: {error_msg_for_user}"),
                {"payload": payload_str, "user_id_tg": user_id_tg, "activation_result": activation_result}
            )
            await message.answer(f"Произошла ошибка при активации вашей подписки (Код: `{error_id}`). Средства были списаны. Свяжитесь с поддержкой.", parse_mode="Markdown")
    except Exception as e:
        error_id = error_handler.log_error(e, {"payload": payload_str, "user_id_tg": user_id_tg, "payment_info": payment_info.model_dump_json(exclude_none=True)})
        logger.critical(f"Критическая ошибка обработки SuccessfulPayment для user {user_id_tg} (ID: {error_id}): {e}", exc_info=True)
        await message.answer(f"Критическая ошибка обработки платежа (Код: `{error_id}`). Средства списаны. Свяжитесь с поддержкой.", parse_mode="Markdown")

# --- Обработчик кнопок подписки ---
@payment_router.callback_query(F.data.startswith("nav_subscribe_"))
async def handle_subscribe_button(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not callback.message or not callback.from_user: return await callback.answer("Ошибка")
    action_parts = callback.data.split("_")
    if len(action_parts) < 4:
        logger.warning(f"Некорректный callback для кнопки подписки: {callback.data}")
        return await callback.answer("Ошибка выбора тарифа.", show_alert=True)
    try:
        tier_str = action_parts[2].lower()
        duration_type = action_parts[3].lower()
        tier_enum = SubscriptionTier(tier_str)
        duration_months = 1 if duration_type == "monthly" else 12
    except ValueError:
        logger.error(f"Не удалось распознать тариф или длительность из callback: {callback.data}")
        return await callback.answer("Ошибка выбора тарифа.", show_alert=True)

    user_id_tg = callback.from_user.id
    subscription_service: SubscriptionService = bot_instance.subscription_service
    config: BotConfig = bot_instance.config
    fsm_data = await state.get_data()
    applied_promocode = fsm_data.get("applied_promocode")
    applied_effects = fsm_data.get("applied_effects")

    invoice_link_or_flag = await create_invoice_link(
        bot=bot_instance.bot, config=config, user_id_tg=user_id_tg,
        tier_enum=tier_enum, duration_months=duration_months,
        subscription_service=subscription_service, promocode=applied_promocode, applied_effects=applied_effects
    )

    if invoice_link_or_flag == "PROMO_ACTIVATED_FREE":
        # Если промокод дал 100% скидку или триал, подписка уже могла быть активирована в apply_promocode_effects
        # или будет активирована сейчас без платежа.
        # Убедимся, что промокод отмечен как использованный, если он привел к бесплатной активации.
        if applied_promocode and applied_effects:
            db_user = await bot_instance.db_service.get_user_by_telegram_id(user_id_tg)
            promo_service: PromoCodeService = bot_instance.promocode_service
            validated_promo = await promo_service.validate_promocode(applied_promocode, db_user.id if db_user else None, target_tier_enum) # type: ignore
            if db_user and validated_promo and validated_promo.id:
                 # Убедимся, что promocode_id это int
                promocode_id_to_mark = validated_promo.id
                if isinstance(promocode_id_to_mark, int):
                    await promo_service.mark_promocode_as_used(promocode_id_to_mark, db_user.id, order_id=f"PROMO_FREE_{tier_enum.value}")
                else:
                    logger.error(f"Некорректный ID промокода для отметки: {promocode_id_to_mark}")

        await callback.answer("Ваша подписка по промокоду успешно активирована!", show_alert=True)
        await state.clear()
        nav_handler = bot_instance.navigation_handlers_instance
        await nav_handler.show_my_subscription_view(callback)
        return

    if invoice_link_or_flag and not invoice_link_or_flag.startswith("ERROR_API_"):
        payment_button = types.InlineKeyboardButton(text="💳 Оплатить через Telegram Stars", url=invoice_link_or_flag)
        back_button = types.InlineKeyboardButton(text="⬅️ К выбору тарифов", callback_data="nav_subscription_plans_view")
        markup = types.InlineKeyboardMarkup(inline_keyboard=[[payment_button], [back_button]])
        tier_config = subscription_service.plans.PLANS.get(tier_enum)
        if not tier_config:
            await callback.answer("Ошибка конфигурации тарифа.", show_alert=True); return
        price = tier_config.price_stars_monthly if duration_months == 1 else tier_config.price_stars_yearly
        if applied_effects and "final_price_after_discount" in applied_effects:
            price = int(applied_effects["final_price_after_discount"])
        text_to_send = (f"Вы выбрали: {hbold(tier_config.tier_name)} на {'1 месяц' if duration_months == 1 else '1 год'}.\n"
                        f"Сумма к оплате: {hbold(str(price))} ⭐ (Telegram Stars).\n\n"
                        "Нажмите кнопку ниже, чтобы перейти к оплате.")
        if applied_promocode:
            text_to_send += f"\n\n{hitalic(f'Промокод {applied_promocode} будет применен.')}"
        try: await callback.message.edit_text(text_to_send, reply_markup=markup, parse_mode="Markdown")
        except Exception as e_edit:
             logger.warning(f"Не удалось отредактировать сообщение для инвойса: {e_edit}. Отправка нового.")
             await callback.message.answer(text_to_send, reply_markup=markup, parse_mode="Markdown")
        await state.clear()
        await callback.answer()
    else:
        error_msg_display = "Не удалось создать счет на оплату. Пожалуйста, попробуйте позже."
        if invoice_link_or_flag and invoice_link_or_flag.startswith("ERROR_API_"):
            logger.error(f"Ошибка API при создании счета для user {user_id_tg}: {invoice_link_or_flag}")
        await callback.answer(error_msg_display, show_alert=True)
        nav_handler = bot_instance.navigation_handlers_instance
        current_persona = await bot_instance._get_current_persona(user_id_tg)
        await nav_handler._show_menu_node(callback, user_id_tg, "subscription_plans_view", current_persona) # type: ignore

# --- Обработчик для сравнения планов ---
@payment_router.callback_query(F.data == "action_compare_plans")
async def handle_compare_plans_callback(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not callback.message or not callback.from_user: return await callback.answer("Ошибка")
    subscription_service: SubscriptionService = bot_instance.subscription_service
    text = f"{hbold('💎 Сравнение Тарифов AI Companion Bot')}\n\n"
    for tier_enum, limits in subscription_service.plans.PLANS.items():
        text += f"--- {hbold(limits.tier_name)} ---\n"
        text += f"Цена (мес/год): {limits.price_stars_monthly}⭐ / {limits.price_stars_yearly}⭐\n"
        text += f"Сообщения/день: {'Безлимит' if limits.daily_messages == -1 else limits.daily_messages}\n"
        text += f"Память: {limits.memory_type} ({'Безлимит' if limits.max_memory_entries == -1 else limits.max_memory_entries} зап., "
        text += f"{'Постоянно' if limits.memory_retention_days == -1 else (str(limits.memory_retention_days) + ' дн.')})\n"
        text += f"Голосовые: {'Да' if limits.voice_messages_allowed else 'Нет'}\n"
        text += f"AI-Инсайты: {'Да' if limits.ai_insights_access else 'Нет'}\n"
        text += f"Макс. уровень Luneth: {limits.sexting_max_level}\n"
        text += f"Доступ к персонам: {', '.join(limits.personas_access)}\n"
        if limits.priority_support: text += "Приоритетная поддержка: Да\n"
        if limits.additional_features:
            text += "Доп. функции: " + ", ".join([f"{k.replace('_',' ').title()}" for k,v in limits.additional_features.items() if v is True]) + "\n"
        text += "\n"
    markup = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="💳 Выбрать тариф", callback_data="nav_subscription_plans_view")],
        [types.InlineKeyboardButton(text="⬅️ Назад в меню подписок", callback_data="nav_my_subscription_view")]
    ])
    try: await callback.message.edit_text(text, reply_markup=markup, parse_mode="Markdown")
    except Exception as e_edit:
        logger.warning(f"Не удалось отредактировать сообщение для сравнения тарифов: {e_edit}. Отправка нового.")
        await callback.message.answer(text, reply_markup=markup, parse_mode="Markdown")
    await callback.answer()
