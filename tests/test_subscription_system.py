# tests/test_subscription_system.py
import pytest
import pytest_asyncio # Для async фикстур, если версия pytest-asyncio < 0.17
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone
import json

# Импортируем тестируемый сервис и его зависимости
from services.subscription_system import (
    SubscriptionService,
    SubscriptionTier,
    SubscriptionPlans, # Для доступа к DEFAULT_PLANS
    SubscriptionStatus,
    TierLimits,
    UserSubscriptionData # Для типизации возвращаемых данных
)
# Предполагаем, что эти модели и сервисы доступны для импорта
# from database.models import User as DBUser # Не нужен напрямую, мокаем db_service
# from services.memory_service import MemoryService # Мокаем
# from services.referral_ab_testing import ReferralService # Мокаем
from config.settings import BotConfig, load_config # Для создания BotConfig

# --- Фикстуры ---
@pytest.fixture(scope="module")
def bot_config_instance() -> BotConfig:
    """Фикстура для загрузки конфигурации один раз на модуль."""
    # Предполагаем, что load_config() может быть вызван без аргументов
    # или вы можете передать путь к тестовому .env файлу
    # В данном случае, мы просто создаем экземпляр с дефолтными значениями,
    # если какие-то специфичные конфиги не нужны для тестов SubscriptionService
    try:
        return load_config()
    except Exception: # Если load_config требует .env, которого нет в тестовом окружении
        logger = logging.getLogger(__name__) # type: ignore
        logger.warning("Не удалось загрузить BotConfig из .env, используется базовый экземпляр.")
        return BotConfig() # type: ignore


@pytest_asyncio.fixture
async def mock_db_service():
    """Мок для DatabaseService."""
    mock = AsyncMock()
    
    # Дефолтные возвращаемые значения для основных методов
    mock.get_or_create_user.return_value = MagicMock(id=1, telegram_id=12345) # Возвращаем объект с атрибутом id
    
    # get_user_preferences будет хранить 'данные' в словаре для имитации
    user_prefs_storage = {}

    async def get_prefs_side_effect(user_id_db, persona=None):
        # logger.debug(f"MOCK DB: get_user_preferences called for user_id_db={user_id_db}, persona={persona}. Storage: {user_prefs_storage}")
        key = (user_id_db, persona or 'system') # 'system' для subscription_data
        return user_prefs_storage.get(key, {})

    async def update_prefs_side_effect(user_id_db, key, value, persona=None, preference_type=None):
        # logger.debug(f"MOCK DB: update_user_preference called for user_id_db={user_id_db}, key={key}, value={value}, persona={persona}")
        storage_key = (user_id_db, persona or 'system')
        if storage_key not in user_prefs_storage:
            user_prefs_storage[storage_key] = {}
        user_prefs_storage[storage_key][key] = value
        return MagicMock(preference_key=key, preference_value=json.dumps(value) if isinstance(value, dict) else str(value))

    mock.get_user_preferences.side_effect = get_prefs_side_effect
    mock.update_user_preference.side_effect = update_prefs_side_effect
    
    # Для get_user_by_telegram_id
    async def get_user_by_tg_id_side_effect(telegram_id):
        if telegram_id == 12345: # Для основного тестового пользователя
            return MagicMock(id=1, telegram_id=12345, first_name="TestUser")
        return None # Для других ID
    mock.get_user_by_telegram_id.side_effect = get_user_by_tg_id_side_effect
    
    # Для ReferralService
    mock.get_user_referral_code_by_user_id.return_value = None # По умолчанию кода нет
    mock.create_referral_code.return_value = MagicMock(code="TESTREF123")

    return mock

@pytest_asyncio.fixture
async def mock_memory_service():
    """Мок для MemoryService."""
    mock = AsyncMock()
    mock.upgrade_memory_on_tier_change = AsyncMock(return_value=None)
    return mock

@pytest_asyncio.fixture
async def mock_referral_service():
    """Мок для ReferralService."""
    mock = AsyncMock()
    mock.mark_referral_as_completed = AsyncMock(return_value=None)
    return mock

@pytest_asyncio.fixture
async def subscription_service_instance(
    mock_db_service: AsyncMock, 
    bot_config_instance: BotConfig # Используем фикстуру конфига
) -> SubscriptionService:
    """Фикстура для создания экземпляра SubscriptionService с моками."""
    # Мокаем также bot_instance, если он используется напрямую в SubscriptionService
    # (например, для отправки уведомлений, хотя это лучше делать через NotificationService)
    mock_bot_instance = MagicMock()
    mock_bot_instance.config = bot_config_instance # Передаем реальный BotConfig

    service = SubscriptionService(
        db_service=mock_db_service, 
        config=bot_config_instance, # Передаем реальный BotConfig
        # bot_instance=mock_bot_instance # Если используется
    )
    # Для тестов, где важны конкретные планы, можно их переопределить здесь
    # service.plans = SubscriptionPlans() # Оставляем дефолтные планы
    return service

# --- Тесты ---

@pytest.mark.asyncio
async def test_get_user_subscription_new_user(subscription_service_instance: SubscriptionService, mock_db_service: AsyncMock):
    """Тест получения подписки для нового пользователя (должен быть Free)."""
    user_id_tg = 12345
    mock_db_service.get_user_preferences.return_value = {} # Новый пользователь, нет сохраненных данных
    
    sub_data = await subscription_service_instance.get_user_subscription(user_id_tg)
    
    assert sub_data["user_id_tg"] == user_id_tg
    assert sub_data["tier"] == SubscriptionTier.FREE.value
    assert sub_data["status"] == SubscriptionStatus.ACTIVE.value
    assert sub_data["tier_name"] == "Free" 
    assert "expires_at" not in sub_data # Или None
    assert sub_data.get("limits", {}).get("daily_messages") == subscription_service_instance.plans.PLANS[SubscriptionTier.FREE].daily_messages

@pytest.mark.asyncio
async def test_activate_subscription_existing_free_user(subscription_service_instance: SubscriptionService, mock_db_service: AsyncMock):
    """Тест активации платной подписки для существующего Free пользователя."""
    user_id_tg = 12345
    db_user_id = 1 # Предполагаемый ID пользователя в БД
    
    # Устанавливаем начальное состояние - Free user
    initial_free_sub_data = {
        "user_id_tg": user_id_tg, "tier": SubscriptionTier.FREE.value, "status": SubscriptionStatus.ACTIVE.value,
        "tier_name": "Free", "activated_at": datetime.now(timezone.utc).isoformat(),
        "usage": {"daily_messages_used": 0, "last_message_date": None}
    }
    mock_db_service.get_user_preferences.return_value = {
        "subscription_data": initial_free_sub_data # json.dumps(initial_free_sub_data) если бы мы мокали сам возврат из БД
    }
    
    new_tier = SubscriptionTier.BASIC
    duration_days = 30
    payment_amount = 100 # (звезды)
    provider_payment_charge_id = "test_charge_id_basic"

    activation_result = await subscription_service_instance.activate_subscription(
        user_id_tg=user_id_tg,
        new_tier_value=new_tier.value,
        duration_days=duration_days,
        payment_amount_stars=payment_amount,
        telegram_charge_id=provider_payment_charge_id,
        payment_provider="TelegramStars" # Пример
    )
    
    assert activation_result["success"] is True
    assert activation_result["new_tier"] == new_tier.value
    
    # Проверяем, что update_user_preference был вызван с правильными данными
    # Последний вызов update_user_preference должен быть для subscription_data
    # (первый может быть для инициализации, если пользователь "новый" для подписки)
    
    # Ищем вызов для 'subscription_data'
    saved_sub_data_call = None
    for call_args in mock_db_service.update_user_preference.call_args_list:
        if call_args.kwargs.get('key') == 'subscription_data':
            saved_sub_data_call = call_args
            break
    
    assert saved_sub_data_call is not None
    saved_sub_data = saved_sub_data_call.kwargs['value'] # Это уже словарь
    
    assert saved_sub_data["tier"] == new_tier.value
    assert saved_sub_data["status"] == SubscriptionStatus.ACTIVE.value
    assert saved_sub_data["telegram_charge_id"] == provider_payment_charge_id
    assert "expires_at" in saved_sub_data
    expires_at_dt = datetime.fromisoformat(saved_sub_data["expires_at"].replace('Z', '+00:00'))
    expected_expiry = datetime.now(timezone.utc) + timedelta(days=duration_days)
    assert abs((expires_at_dt - expected_expiry).total_seconds()) < 60 # Погрешность в 1 минуту
    
    # Проверяем, что usage сброшен
    assert saved_sub_data.get("usage", {}).get("daily_messages_used") == 0
    
    # Проверяем, что вызвалась статистика (опционально, если ваш mock это проверяет)
    mock_db_service.save_statistic.assert_any_call(
        metric_name='subscription_purchased',
        metric_value=float(payment_amount),
        user_id=db_user_id, # Используем db_user_id, так как get_or_create_user мокнут с ним
        persona=None, # Или "system", в зависимости от вашей логики
        additional_data={'tier': new_tier.value, 'duration_days': duration_days, 'provider': "TelegramStars"}
    )

@pytest.mark.asyncio
async def test_check_feature_access_various_tiers(subscription_service_instance: SubscriptionService, mock_db_service: AsyncMock):
    """Тест проверки доступа к функциям для разных тарифов."""
    user_id_tg = 12345
    
    async def set_user_tier(tier: SubscriptionTier):
        # Имитируем, что пользователь имеет определенный тариф
        sub_data_mock = {
            "tier": tier.value, "status": SubscriptionStatus.ACTIVE.value,
            "tier_name": tier.name.title().replace("_", " "),
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat() if tier != SubscriptionTier.FREE else None
        }
        # get_user_preferences должен возвращать словарь {key: value}, где value - это распарсенный JSON, если тип json
        mock_db_service.get_user_preferences.return_value = {"subscription_data": sub_data_mock}

    # FREE Tier
    await set_user_tier(SubscriptionTier.FREE)
    assert not (await subscription_service_instance.check_feature_access(user_id_tg, "voice_messages"))["allowed"]
    assert not (await subscription_service_instance.check_feature_access(user_id_tg, "ai_insights"))["allowed"]
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "persona_access", persona="diana_friend"))["allowed"] # Предполагаем, что diana_friend доступна для Free
    assert not (await subscription_service_instance.check_feature_access(user_id_tg, "persona_access", persona="madina_basic"))["allowed"]
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "sexting_level", level=0))["allowed"] # Уровень 0 для Мадины (если доступна)
    assert not (await subscription_service_instance.check_feature_access(user_id_tg, "sexting_level", level=5))["allowed"]

    # BASIC Tier
    await set_user_tier(SubscriptionTier.BASIC)
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "voice_messages"))["allowed"]
    assert not (await subscription_service_instance.check_feature_access(user_id_tg, "ai_insights"))["allowed"]
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "persona_access", persona="madina_basic"))["allowed"]
    can_access_luneth_level_5 = await subscription_service_instance.check_feature_access(user_id_tg, 'luneth_level_5')
    assert can_access_luneth_level_5 is True # Basic имеет доступ к Luneth 5
    can_access_luneth_level_10 = await subscription_service_instance.check_feature_access(user_id_tg, 'luneth_level_10')
    assert can_access_luneth_level_10 is False # Basic не имеет доступа к Luneth 10

    # PREMIUM Tier
    await set_user_tier(SubscriptionTier.PREMIUM)
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "ai_insights"))["allowed"]
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "persona_access", persona="madina_advanced"))["allowed"] # Предполагаем
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "sexting_level", level=8))["allowed"]
    assert not (await subscription_service_instance.check_feature_access(user_id_tg, "permanent_memory"))["allowed"] # VIP фича

    # VIP Tier
    await set_user_tier(SubscriptionTier.VIP)
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "permanent_memory"))["allowed"]
    can_access_luneth_level_10 = await subscription_service_instance.check_feature_access(user_id_tg, 'luneth_level_10')
    assert can_access_luneth_level_10 is True

@pytest.mark.asyncio
async def test_activate_trial_subscription(subscription_service_instance: SubscriptionService, mock_db_service: AsyncMock):
    user_id_tg = 12345
    # Сначала пользователь Free
    mock_db_service.get_user_preferences.return_value = {
        "subscription_data": {"tier": SubscriptionTier.FREE.value, "status": SubscriptionStatus.ACTIVE.value}
    }
    
    trial_tier = SubscriptionTier.PREMIUM
    trial_days = 7
    
    result = await subscription_service_instance.activate_trial_subscription(user_id_tg, trial_tier.value, trial_days, "test_trial_promo")
    
    assert result["success"] is True
    assert result["new_tier"] == trial_tier.value
    
    # Ищем вызов для 'subscription_data'
    saved_sub_data_call = None
    for call_args in mock_db_service.update_user_preference.call_args_list:
        if call_args.kwargs.get('key') == 'subscription_data':
            saved_sub_data_call = call_args; break
    assert saved_sub_data_call is not None
    saved_data = saved_sub_data_call.kwargs['value']
    
    assert saved_data["tier"] == trial_tier.value
    assert saved_data["status"] == SubscriptionStatus.ACTIVE.value # Триалы тоже активны
    assert saved_data.get("is_trial") is True
    assert saved_data.get("trial_source") == "test_trial_promo"
    
    expires_at_dt = datetime.fromisoformat(saved_data["expires_at"].replace('Z', '+00:00'))
    expected_expiry = datetime.now(timezone.utc) + timedelta(days=trial_days)
    assert abs((expires_at_dt - expected_expiry).total_seconds()) < 60

@pytest.mark.asyncio
async def test_subscription_expiry_and_downgrade(subscription_service_instance: SubscriptionService, mock_db_service: AsyncMock):
    user_id_tg = 12345
    expired_sub_data = {
        "tier": SubscriptionTier.BASIC.value, "status": SubscriptionStatus.ACTIVE.value,
        "tier_name": "Basic",
        "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat() + "Z", # Истекла вчера
        "activated_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat() + "Z",
        "usage": {"daily_messages_used": 10, "last_message_date": (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()}
    }
    mock_db_service.get_user_preferences.return_value = {"subscription_data": expired_sub_data}
    
    # Вызов get_user_subscription должен инициировать проверку и даунгрейд
    current_sub = await subscription_service_instance.get_user_subscription(user_id_tg)
    
    assert current_sub["tier"] == SubscriptionTier.FREE.value
    assert current_sub["status"] == SubscriptionStatus.ACTIVE.value # После даунгрейда статус Free - активен
    assert "expires_at" not in current_sub or current_sub["expires_at"] is None
    assert current_sub.get("original_tier_before_expiry") == SubscriptionTier.BASIC.value # Проверяем, что предыдущий тариф записан
    
    # Проверяем, что usage сброшен
    assert current_sub.get("usage", {}).get("daily_messages_used") == 0

@pytest.mark.asyncio
async def test_grace_period_logic(subscription_service_instance: SubscriptionService, mock_db_service: AsyncMock):
    user_id_tg = 12345
    # Истекает очень скоро, но еще не истекла полностью для grace period
    grace_period_days = subscription_service_instance.config.grace_period_days # Обычно 3
    
    # Подписка истекла, но мы в пределах grace_period_days
    # Например, grace_period = 3 дня, подписка истекла 1 день назад
    expires_at_for_grace = datetime.now(timezone.utc) - timedelta(days=1) 
    
    sub_data_in_grace = {
        "tier": SubscriptionTier.PREMIUM.value, "status": SubscriptionStatus.ACTIVE.value, # Статус еще active до первой проверки
        "tier_name": "Premium",
        "expires_at": expires_at_for_grace.isoformat() + "Z",
        "activated_at": (expires_at_for_grace - timedelta(days=30)).isoformat() + "Z"
    }
    mock_db_service.get_user_preferences.return_value = {"subscription_data": sub_data_in_grace}
    
    # Первый вызов get_user_subscription - должен перевести в grace_period
    sub_after_first_check = await subscription_service_instance.get_user_subscription(user_id_tg)
    assert sub_after_first_check["status"] == SubscriptionStatus.GRACE_PERIOD.value
    assert sub_after_first_check["tier"] == SubscriptionTier.PREMIUM.value # Тариф пока сохраняется
    
    # Доступ к фичам в grace period должен сохраняться
    assert (await subscription_service_instance.check_feature_access(user_id_tg, "ai_insights"))["allowed"] 
    
    # Имитируем, что прошло время и grace period истек
    # Для этого мокаем expires_at так, чтобы он был раньше, чем (now - grace_period_days)
    completely_expired_expires_at = datetime.now(timezone.utc) - timedelta(days=grace_period_days + 1)
    sub_data_grace_expired = {
        "tier": SubscriptionTier.PREMIUM.value, "status": SubscriptionStatus.GRACE_PERIOD.value, # Уже в grace
        "tier_name": "Premium", "expires_at": completely_expired_expires_at.isoformat() + "Z",
        "activated_at": (completely_expired_expires_at - timedelta(days=30)).isoformat() + "Z"
    }
    mock_db_service.get_user_preferences.return_value = {"subscription_data": sub_data_grace_expired}
    
    sub_after_grace_expired = await subscription_service_instance.get_user_subscription(user_id_tg)
    assert sub_after_grace_expired["status"] == SubscriptionStatus.ACTIVE.value # Статус Free
    assert sub_after_grace_expired["tier"] == SubscriptionTier.FREE.value

@pytest.mark.asyncio
async def test_add_bonus_messages(subscription_service_instance: SubscriptionService, mock_db_service: AsyncMock):
    user_id_tg = 12345
    db_user_id = 1
    
    # Начинаем с Free пользователя
    initial_sub_data = {"tier": SubscriptionTier.FREE.value, "status": SubscriptionStatus.ACTIVE.value, "usage": {}}
    mock_db_service.get_user_preferences.return_value = {"subscription_data": initial_sub_data}
    
    bonus_amount = 50
    await subscription_service_instance.add_bonus_messages(user_id_tg, bonus_amount, "test_bonus_source")
    
    # Проверяем, что update_user_preference был вызван с обновленными данными
    saved_sub_data_call = None
    for call_args in mock_db_service.update_user_preference.call_args_list:
        if call_args.kwargs.get('key') == 'subscription_data': saved_sub_data_call = call_args; break
    assert saved_sub_data_call is not None
    saved_data = saved_sub_data_call.kwargs['value']
    
    assert saved_data.get("usage", {}).get("bonus_messages_total", 0) == bonus_amount
    assert saved_data.get("usage", {}).get("bonus_messages_remaining", 0) == bonus_amount
    
    # Проверяем лимит сообщений - он должен увеличиться на бонус
    limits_after_bonus = await subscription_service_instance.check_message_limit(user_id_tg)
    free_limit = subscription_service_instance.plans.PLANS[SubscriptionTier.FREE].daily_messages
    assert limits_after_bonus.get("effective_limit") == free_limit + bonus_amount
    assert limits_after_bonus.get("remaining") == free_limit + bonus_amount # Если еще не использовал

    # Имитируем использование сообщений
    # Этот тест сложнее, т.к. check_message_limit сам обновляет usage.
    # Проще проверить, что бонус учитывается в get_user_subscription и check_message_limit.
    # Сейчас проверим, что после второго добавления бонус суммируется
    await subscription_service_instance.add_bonus_messages(user_id_tg, 20, "another_bonus")
    
    # Обновляем мок, чтобы он вернул последние сохраненные данные для следующего get_user_subscription
    mock_db_service.get_user_preferences.return_value = {"subscription_data": saved_data} # saved_data уже содержит первый бонус
    
    # Получаем данные еще раз, чтобы SubscriptionService обновил свои внутренние данные о подписке из мока
    # Это немного искусственно, но нужно для имитации последовательных вызовов
    _ = await subscription_service_instance.get_user_subscription(user_id_tg) 
    
    # Ищем последний вызов update_user_preference
    last_saved_sub_data_call = None
    for call_args in mock_db_service.update_user_preference.call_args_list:
        if call_args.kwargs.get('key') == 'subscription_data': last_saved_sub_data_call = call_args # Просто берем последний
    assert last_saved_sub_data_call is not None
    final_saved_data = last_saved_sub_data_call.kwargs['value']
    
    assert final_saved_data.get("usage", {}).get("bonus_messages_total", 0) == bonus_amount + 20
    assert final_saved_data.get("usage", {}).get("bonus_messages_remaining", 0) == bonus_amount + 20


@pytest.mark.asyncio
async def test_process_successful_payment_calls_dependencies(
    subscription_service_instance: SubscriptionService, 
    mock_db_service: AsyncMock,
    mock_memory_service: AsyncMock,
    mock_referral_service: AsyncMock
):
    """Тест проверяет, что process_successful_payment вызывает нужные сервисы."""
    user_id_tg = 12345
    db_user_id = 1 # Соответствует моку get_or_create_user
    
    # Мокаем _get_or_create_db_user_for_subscription для простоты, чтобы он возвращал известный db_user_id
    # Или убеждаемся, что mock_db_service.get_or_create_user возвращает нужный id.
    # Сейчас он возвращает MagicMock(id=1, telegram_id=12345)
    
    # Имитируем, что пользователь был приглашен
    initial_prefs_for_referral = {
        "subscription_data": {"tier": SubscriptionTier.FREE.value, "status": SubscriptionStatus.ACTIVE.value},
        # Добавляем REFERRER_ID_PREFERENCE_KEY, как будто пользователь был приглашен
        subscription_service_instance.referral_service.REFERRER_ID_PREFERENCE_KEY: 999 # ID реферера
    }
    mock_db_service.get_user_preferences.return_value = initial_prefs_for_referral
    
    # Передаем моки в сервис (обычно это делается через конструктор, но для теста можно так)
    subscription_service_instance.memory_service = mock_memory_service
    subscription_service_instance.referral_service = mock_referral_service

    await subscription_service_instance.process_successful_payment(
        user_id_tg=user_id_tg,
        new_tier=SubscriptionTier.PREMIUM, # Активируем Premium
        duration_days=30,
        payment_amount_stars=500,
        telegram_charge_id="charge_premium_test",
        payment_provider="TelegramStars",
        is_first_paid_purchase_override=True # Явно указываем, что это первая покупка
    )

    # Проверка вызова MemoryService
    mock_memory_service.upgrade_memory_on_tier_change.assert_called_once_with(
        user_id_tg, SubscriptionTier.FREE.value, SubscriptionTier.PREMIUM.value # От Free к Premium
    )
    
    # Проверка вызова ReferralService
    mock_referral_service.mark_referral_as_completed.assert_called_once_with(user_id_tg)

# TODO: Тест на апгрейд подписки (например, с Basic на Premium)
# TODO: Тест на корректность работы _get_tier_name
# TODO: Тест на get_subscription_menu (проверить структуру кнопок и текста)
