# tests/test_referral_system.py
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch
from datetime import datetime, timedelta, timezone
import json
from typing import Optional, Dict, Any, List
import logging
import string 
import random 

# Импортируем тестируемый сервис и его зависимости
from services.referral_ab_testing import (
    ReferralService,
    ReferralRewardConfig,
    ReferralRewardType,
    AppliedReferralReward,
    ABTestService, 
    ABTestIntegration
)
from services.subscription_system import SubscriptionService, SubscriptionTier
from services.promocode_system import PromoCodeService, PromoCodeDiscountType, PromoCode as DataClassPromoCode, PromoCodeType
from database.models import User as DBUser, ReferralCode as DBReferralCode, PromoCode as DBPromoCode
from config.settings import BotConfig
from sqlalchemy.exc import IntegrityError # Для имитации ошибки БД

# --- Фикстуры ---

@pytest.fixture(scope="module")
def bot_config_instance_referral() -> BotConfig:
    config = BotConfig() # type: ignore
    config.bot_username = "TestCompanionBotForReferral" 
    return config

@pytest_asyncio.fixture
async def mock_db_service_referral():
    """Мок для DatabaseService, адаптированный для ReferralService."""
    mock = AsyncMock()
    
    users_storage: Dict[int, DBUser] = {} 
    referral_codes_storage: Dict[str, DBReferralCode] = {} 
    user_prefs_referral_storage: Dict[tuple[int, str], Dict[str, Any]] = {}
    _next_user_id_counter = 1
    _next_ref_code_id_counter = 1

    def _reset_storages_referral():
        nonlocal _next_user_id_counter, _next_ref_code_id_counter
        users_storage.clear()
        referral_codes_storage.clear()
        user_prefs_referral_storage.clear()
        _next_user_id_counter = 1
        _next_ref_code_id_counter = 1

    async def get_or_create_user_side_effect(telegram_id, **kwargs):
        nonlocal _next_user_id_counter
        for user_obj in users_storage.values():
            if user_obj.telegram_id == telegram_id:
                user_obj.last_activity = datetime.now(timezone.utc); user_obj.is_active = True
                for k, v in kwargs.items():
                    if hasattr(user_obj, k) and v is not None: setattr(user_obj, k, v)
                return user_obj
        
        new_user = DBUser(id=_next_user_id_counter, telegram_id=telegram_id, 
                          first_name=kwargs.get("first_name", f"TestU{_next_user_id_counter}"),
                          username=kwargs.get("username", f"testu{_next_user_id_counter}"),
                          is_active=True, last_activity=datetime.now(timezone.utc),
                          created_at=datetime.now(timezone.utc))
        users_storage[_next_user_id_counter] = new_user
        user_prefs_referral_storage.setdefault((_next_user_id_counter, 'system'), {})
        _next_user_id_counter += 1
        return new_user

    async def get_user_by_telegram_id_side_effect(telegram_id):
        for user_obj in users_storage.values():
            if user_obj.telegram_id == telegram_id: return user_obj
        return None
        
    async def get_user_by_db_id_side_effect(user_id_db):
        return users_storage.get(user_id_db)

    async def get_referral_code_by_user_id_side_effect(user_id_db):
        for rc_obj in referral_codes_storage.values():
            if rc_obj.user_id_db == user_id_db: return rc_obj
        return None

    async def create_referral_code_side_effect(user_id_db, code_str):
        nonlocal _next_ref_code_id_counter
        code_upper = code_str.upper()
        if code_upper in referral_codes_storage:
            raise IntegrityError(f"Mocked IntegrityError: ref code {code_upper} already exists", params={}, orig=Exception())
        if user_id_db not in users_storage:
             raise Exception(f"Mocked DB Error: User DB ID {user_id_db} not found for ref code creation.")
        new_rc = DBReferralCode(id=_next_ref_code_id_counter, user_id_db=user_id_db, code=code_upper, created_at=datetime.now(timezone.utc))
        referral_codes_storage[code_upper] = new_rc
        if user_id_db in users_storage: users_storage[user_id_db].referral_code_entry = new_rc # type: ignore
        _next_ref_code_id_counter += 1
        return new_rc

    async def get_user_by_referral_code_side_effect(code_str):
        rc_obj = referral_codes_storage.get(code_str.upper())
        if rc_obj: return users_storage.get(rc_obj.user_id_db)
        return None

    async def get_prefs_referral_side_effect(user_id_db, persona=None):
        key = (user_id_db, persona or 'system')
        return user_prefs_referral_storage.get(key, {})

    async def update_prefs_referral_side_effect(user_id_db, key, value, persona=None, preference_type=None):
        storage_key = (user_id_db, persona or 'system')
        user_prefs_referral_storage.setdefault(storage_key, {})[key] = value
        return MagicMock(preference_key=key, preference_value=json.dumps(value) if isinstance(value,dict) else str(value))

    mock.get_or_create_user.side_effect = get_or_create_user_side_effect
    mock.get_user_by_telegram_id.side_effect = get_user_by_telegram_id_side_effect
    mock.get_user_by_db_id.side_effect = get_user_by_db_id_side_effect
    mock.get_referral_code_by_user_id.side_effect = get_referral_code_by_user_id_side_effect
    mock.create_referral_code.side_effect = create_referral_code_side_effect
    mock.get_user_by_referral_code.side_effect = get_user_by_referral_code_side_effect
    mock.get_user_preferences.side_effect = get_prefs_referral_side_effect
    mock.update_user_preference.side_effect = update_prefs_referral_side_effect
    
    mock._reset_storages = _reset_storages_referral
    return mock

@pytest_asyncio.fixture
async def mock_subscription_service_referral(bot_config_instance_referral: BotConfig):
    mock = AsyncMock(spec=SubscriptionService)
    mock.add_bonus_messages = AsyncMock(return_value=True)
    mock.activate_trial_subscription = AsyncMock(return_value={"success": True, "new_tier": "premium", "message": "Trial activated"})
    async def get_sub_side_effect(user_id_tg):
        return {"tier": SubscriptionTier.FREE.value, "status": "active", "tier_name": "Free"}
    mock.get_user_subscription.side_effect = get_sub_side_effect
    mock.config = bot_config_instance_referral
    return mock

@pytest_asyncio.fixture
async def mock_promocode_service_referral():
    mock = AsyncMock(spec=PromoCodeService)
    async def create_promo_side_effect(code, discount_type, discount_value, **kwargs):
        return DataClassPromoCode(
            code=code, discount_type=discount_type.value, discount_value=discount_value,
            id=random.randint(1000,2000), max_uses=kwargs.get('max_uses', 1), uses_count=0, is_active=True,
            created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=kwargs.get('expires_in_days', 30)),
            code_type=kwargs.get('code_type', PromoCodeType.GENERIC.value)
        )
    mock.create_promocode.side_effect = create_promo_side_effect
    mock.generate_random_code.return_value = "REFS" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return mock

@pytest_asyncio.fixture
async def mock_bot_instance_for_referral(bot_config_instance_referral: BotConfig):
    mock_bot_actual = AsyncMock() 
    mock_bot_actual.send_message = AsyncMock()
    mock_ai_companion_bot = MagicMock() 
    mock_ai_companion_bot.bot = mock_bot_actual
    mock_ai_companion_bot.config = bot_config_instance_referral
    return mock_ai_companion_bot

@pytest_asyncio.fixture
async def referral_service_instance(
    mock_db_service_referral: AsyncMock, 
    mock_subscription_service_referral: AsyncMock,
    mock_promocode_service_referral: AsyncMock,
    bot_config_instance_referral: BotConfig,
    mock_bot_instance_for_referral: MagicMock
) -> ReferralService:
    mock_db_service_referral._reset_storages() # type: ignore
    service = ReferralService(
        db_service=mock_db_service_referral,
        subscription_service=mock_subscription_service_referral,
        promocode_service=mock_promocode_service_referral,
        config=bot_config_instance_referral,
        bot_instance=mock_bot_instance_for_referral
    )
    return service

# --- Тест-кейсы для ReferralService (Примеры) ---

@pytest.mark.asyncio
async def test_generate_referral_code_new_user(referral_service_instance: ReferralService, mock_db_service_referral: AsyncMock):
    user_id_db = 1
    mock_db_service_referral._users_storage[user_id_db] = DBUser(id=user_id_db, telegram_id=12345) # type: ignore
    new_code = await referral_service_instance.generate_referral_code_for_user(user_id_db)
    assert isinstance(new_code, str) and new_code.startswith("INVITE")
    mock_db_service_referral.create_referral_code.assert_called_once_with(user_id_db, new_code)
    assert any(call.kwargs.get('key') == ReferralService.REFERRAL_STATS_PREFERENCE_KEY for call in mock_db_service_referral.update_user_preference.call_args_list if call.kwargs.get('user_id_db') == user_id_db)

@pytest.mark.asyncio
async def test_process_referral_code_usage_new_referee(referral_service_instance: ReferralService, mock_db_service_referral: AsyncMock, mock_subscription_service_referral: AsyncMock):
    referee_tg_id = 12345; referee_db_id = 1
    referrer_db_id = 10; referral_code_used = "GOODCODE1"
    mock_db_service_referral.get_or_create_user.return_value = DBUser(id=referee_db_id, telegram_id=referee_tg_id, first_name="Referee")
    mock_db_service_referral._user_prefs_referral_storage[(referee_db_id, 'system')] = {} # type: ignore
    mock_db_service_referral.get_user_by_referral_code.return_value = DBUser(id=referrer_db_id, telegram_id=67890, first_name="Referrer")
    mock_db_service_referral._user_prefs_referral_storage[(referrer_db_id, 'system')] = {ReferralService.REFERRAL_STATS_PREFERENCE_KEY: {'referrals_initiated_count': 0, 'referrals_completed_count': 0, 'rewards_earned_log': [], 'last_milestone_achieved': 0}} # type: ignore

    result = await referral_service_instance.process_referral_code_usage(referee_tg_id, referral_code_used)
    assert result["success"] is True
    referee_final_prefs = mock_db_service_referral._user_prefs_referral_storage.get((referee_db_id, 'system'), {}) # type: ignore
    assert referee_final_prefs.get(ReferralService.REFERRED_BY_CODE_PREFERENCE_KEY) == referral_code_used.upper()
    referrer_final_stats = mock_db_service_referral._user_prefs_referral_storage.get((referrer_db_id, 'system'), {}).get(ReferralService.REFERRAL_STATS_PREFERENCE_KEY) # type: ignore
    assert referrer_final_stats['referrals_initiated_count'] == 1
    mock_subscription_service_referral.add_bonus_messages.assert_any_call(referee_tg_id, int(ReferralService.DEFAULT_REFEREE_REWARD.value), source=unittest.mock.ANY)
    mock_subscription_service_referral.add_bonus_messages.assert_any_call(None, int(ReferralService.DEFAULT_REFERRER_REWARD.value), source=unittest.mock.ANY)

@pytest.mark.asyncio
async def test_mark_referral_as_completed_applies_bonus_and_first_milestone(
    referral_service_instance: ReferralService, mock_db_service_referral: AsyncMock, 
    mock_subscription_service_referral: AsyncMock, mock_promocode_service_referral: AsyncMock,
    mock_bot_instance_for_referral: MagicMock):
    referee_tg_id = 45678; referee_db_id = 4
    referrer_tg_id = 56789; referrer_db_id = 5
    mock_db_service_referral.get_user_by_telegram_id.return_value = DBUser(id=referee_db_id, telegram_id=referee_tg_id)
    mock_db_service_referral.get_user_by_db_id.return_value = DBUser(id=referrer_db_id, telegram_id=referrer_tg_id, first_name="Referrer")
    mock_db_service_referral._user_prefs_referral_storage[(referee_db_id, 'system')] = {ReferralService.REFERRER_ID_PREFERENCE_KEY: referrer_db_id} # type: ignore
    initial_referrer_stats = {'referrals_initiated_count': 5, 'referrals_completed_count': 4, 'rewards_earned_log': [], 'last_milestone_achieved': 0}
    mock_db_service_referral._user_prefs_referral_storage[(referrer_db_id, 'system')] = {ReferralService.REFERRAL_STATS_PREFERENCE_KEY: initial_referrer_stats.copy()} # type: ignore

    await referral_service_instance.mark_referral_as_completed(referee_tg_id)
    final_referrer_stats = mock_db_service_referral._user_prefs_referral_storage.get((referrer_db_id, 'system'), {}).get(ReferralService.REFERRAL_STATS_PREFERENCE_KEY) # type: ignore
    assert final_referrer_stats['referrals_completed_count'] == 5
    assert final_referrer_stats['last_milestone_achieved'] == 5
    mock_promocode_service_referral.create_promocode.assert_any_call(code=unittest.mock.ANY, discount_type=PromoCodeDiscountType.PERCENTAGE, discount_value=ReferralService.SUCCESSFUL_REFERRAL_BONUS_FOR_REFERRER.value, max_uses=1, max_uses_per_user=1, created_by=0, expires_in_days=ReferralService.SUCCESSFUL_REFERRAL_BONUS_FOR_REFERRER.discount_duration_days, description=unittest.mock.ANY, user_facing_description=unittest.mock.ANY)
    mock_bot_instance_for_referral.bot.send_message.assert_any_call(referrer_tg_id, unittest.mock.ANY)
    milestone_reward_5 = ReferralService.MILESTONE_REWARDS_CONFIG[5]
    mock_subscription_service_referral.activate_trial_subscription.assert_called_once_with(user_id_tg=referrer_tg_id, trial_tier_value=milestone_reward_5.trial_tier.value, trial_days=int(milestone_reward_5.value), promocode_used=f"REFERRAL_milestone_5_referrals") # type: ignore
    assert len(final_referrer_stats['rewards_earned_log']) >= 2

# (Скопируйте сюда остальные примеры тестов из предыдущего ответа для ReferralService, 
#  адаптируя их при необходимости под обновленные моки)

# Не забудьте добавить тесты для:
# - get_user_referral_dashboard_info
# - Коллизии при генерации кода (когда все попытки неудачны)
# - Повторного завершения одного и того же реферала
# - Различных сценариев для _apply_reward_to_user
