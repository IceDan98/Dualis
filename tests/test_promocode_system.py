# tests/test_promocode_system.py
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call
from datetime import datetime, timedelta, timezone
import json
from typing import Optional, Dict, Any, List 
import logging 
import string 
import random 

# Импортируем тестируемый сервис и его зависимости
from services.promocode_system import (
    PromoCodeService,
    PromoCode, 
    PromoCodeDiscountType,
    PromoCodeType,
    ValidationError 
)
from services.subscription_system import SubscriptionService, SubscriptionTier
# Модели БД, которые мы будем имитировать или использовать для создания мок-объектов
from database.models import User as DBUser 
from database.models import PromoCode as DBPromoCode 
# from database.operations import DatabaseService # Будет мокнут
from config.settings import BotConfig 
from sqlalchemy.exc import IntegrityError # Для имитации ошибки уникальности кода

# --- Фикстуры ---

@pytest.fixture(scope="module")
def bot_config_instance_promo() -> BotConfig:
    return BotConfig() # type: ignore

@pytest_asyncio.fixture
async def mock_db_service_promo():
    """Мок для DatabaseService, адаптированный для PromoCodeService."""
    mock = AsyncMock()
    
    promocodes_db_storage: Dict[str, DBPromoCode] = {} 
    promocode_uses_log: Dict[tuple[int, int], int] = {} 
    user_prefs_promo_storage: Dict[tuple[int, str], Dict[str, Any]] = {}
    _next_promocode_id_counter = 1

    def _reset_storages():
        nonlocal _next_promocode_id_counter
        promocodes_db_storage.clear()
        promocode_uses_log.clear()
        user_prefs_promo_storage.clear()
        _next_promocode_id_counter = 1

    async def save_promocode_side_effect(db_promo_code_obj: DBPromoCode) -> DBPromoCode:
        nonlocal _next_promocode_id_counter
        code_upper = db_promo_code_obj.code.upper()
        
        if code_upper in promocodes_db_storage and \
           (not hasattr(db_promo_code_obj, 'id') or promocodes_db_storage[code_upper].id != db_promo_code_obj.id):
            logging.debug(f"MOCK DB IntegrityError: code {code_upper} for save_promocode.")
            raise IntegrityError(f"Mocked IntegrityError: code {code_upper} already exists", params={}, orig=Exception())

        if not db_promo_code_obj.id:
            db_promo_code_obj.id = _next_promocode_id_counter
            _next_promocode_id_counter +=1
        
        promocodes_db_storage[code_upper] = db_promo_code_obj
        logging.debug(f"MOCK DB: Saved/Updated promocode '{code_upper}' with ID {db_promo_code_obj.id}. Storage now: {list(promocodes_db_storage.keys())}")
        return db_promo_code_obj

    async def get_promocode_by_code_side_effect(code_str: str) -> Optional[DBPromoCode]:
        code_upper = code_str.upper()
        logging.debug(f"MOCK DB: get_promocode_by_code for '{code_upper}'. Available: {list(promocodes_db_storage.keys())}")
        return promocodes_db_storage.get(code_upper)

    async def get_promocode_by_id_side_effect(promo_id: int) -> Optional[DBPromoCode]:
        for pc in promocodes_db_storage.values():
            if pc.id == promo_id:
                return pc
        return None
        
    async def get_all_promocodes_side_effect(active_only: bool = False, for_user_id: Optional[int] = None):
        results = []
        now_utc = datetime.now(timezone.utc)
        for code_obj in promocodes_db_storage.values():
            if active_only:
                is_code_really_active = code_obj.is_active
                if code_obj.expires_at and code_obj.expires_at.replace(tzinfo=timezone.utc) < now_utc: is_code_really_active = False
                if code_obj.active_from and code_obj.active_from.replace(tzinfo=timezone.utc) > now_utc: is_code_really_active = False
                if code_obj.max_uses is not None and code_obj.uses_count >= code_obj.max_uses: is_code_really_active = False
                if not code_obj.is_active: is_code_really_active = False
                if not is_code_really_active: continue
            results.append(code_obj)
        return results

    async def increment_promocode_uses_side_effect(code_id: int, user_id_db: Optional[int] = None) -> bool:
        found_code_obj = None
        for pc_obj in promocodes_db_storage.values():
            if pc_obj.id == code_id: found_code_obj = pc_obj; break
        if found_code_obj:
            found_code_obj.uses_count += 1
            if user_id_db:
                 usage_key = (code_id, user_id_db)
                 promocode_uses_log[usage_key] = promocode_uses_log.get(usage_key, 0) + 1
            logging.debug(f"MOCK DB: Incremented uses for promocode ID {code_id} to {found_code_obj.uses_count}")
            return True
        logging.debug(f"MOCK DB: Promocode ID {code_id} not found for incrementing uses.")
        return False

    async def get_user_promocode_usage_count_side_effect(promocode_id: int, user_id_db: int) -> int:
        count = promocode_uses_log.get((promocode_id, user_id_db), 0)
        logging.debug(f"MOCK DB: Usage count for promo ID {promocode_id}, user ID {user_id_db} is {count}")
        return count
        
    async def get_prefs_promo_side_effect(user_id_db, persona=None):
        key = (user_id_db, persona or 'system_promocodes_usage_log')
        return user_prefs_promo_storage.get(key, {})
    async def update_prefs_promo_side_effect(user_id_db, key, value, persona=None, preference_type=None):
        storage_key = (user_id_db, persona or 'system_promocodes_usage_log')
        user_prefs_promo_storage.setdefault(storage_key, {})[key] = value
        return MagicMock(preference_key=key, preference_value=str(value))

    async def delete_promocode_db_side_effect(promocode_id: int) -> bool:
        code_to_delete = None
        for code, pc_obj in promocodes_db_storage.items():
            if pc_obj.id == promocode_id: code_to_delete = code; break
        if code_to_delete:
            del promocodes_db_storage[code_to_delete]
            keys_to_delete_from_log = [k for k in promocode_uses_log if k[0] == promocode_id]
            for k_log in keys_to_delete_from_log: del promocode_uses_log[k_log]
            return True
        return False

    mock.save_promocode.side_effect = save_promocode_side_effect
    mock.get_promocode_by_code.side_effect = get_promocode_by_code_side_effect
    mock.get_promocode_by_id.side_effect = get_promocode_by_id_side_effect
    mock.get_all_promocodes.side_effect = get_all_promocodes_side_effect
    mock.increment_promocode_uses.side_effect = increment_promocode_uses_side_effect
    mock.get_user_promocode_usage_count.side_effect = get_user_promocode_usage_count_side_effect
    mock.get_user_preferences.side_effect = get_prefs_promo_side_effect
    mock.update_user_preference.side_effect = update_prefs_promo_side_effect
    mock.delete_promocode_db.side_effect = delete_promocode_db_side_effect
    
    mock._reset_storages = _reset_storages
    return mock

@pytest_asyncio.fixture
async def mock_subscription_service_for_promo(bot_config_instance_promo: BotConfig):
    mock = AsyncMock(spec=SubscriptionService)
    mock.add_bonus_messages = AsyncMock(return_value=True)
    mock.activate_trial_subscription = AsyncMock(return_value={"success": True, "new_tier": "premium", "message": "Trial activated"})
    mock.check_feature_access = AsyncMock(return_value={"allowed": True}) 
    async def can_receive_trial_side_effect(user_id_tg, trial_tier): return True
    mock.user_can_receive_trial = AsyncMock(side_effect=can_receive_trial_side_effect)
    mock.config = bot_config_instance_promo
    return mock

@pytest_asyncio.fixture
async def promocode_service_instance(
    mock_db_service_promo: AsyncMock, 
    mock_subscription_service_for_promo: AsyncMock,
    bot_config_instance_promo: BotConfig
) -> PromoCodeService:
    mock_db_service_promo._reset_storages() # type: ignore
    return PromoCodeService(db_service=mock_db_service_promo, subscription_service=mock_subscription_service_for_promo, config=bot_config_instance_promo)

# --- Тест-кейсы ---

@pytest.mark.asyncio
async def test_create_percentage_promocode_success(promocode_service_instance: PromoCodeService, mock_db_service_promo: AsyncMock):
    code_str = "SALE20NEWYEAR" 
    expires_in_days = 30
    created_promo = await promocode_service_instance.create_promocode(
        code=code_str, discount_type=PromoCodeDiscountType.PERCENTAGE, discount_value=20.0,
        max_uses=50, expires_in_days=expires_in_days, description="New Year Sale 20%",
        code_type=PromoCodeType.PUBLIC, is_active=True, for_subscription_tier=SubscriptionTier.PREMIUM.value
    )
    assert created_promo is not None; assert created_promo.code == code_str.upper()
    assert created_promo.for_subscription_tier == SubscriptionTier.PREMIUM.value
    mock_db_service_promo.save_promocode.assert_called_once()
    saved_arg: DBPromoCode = mock_db_service_promo.save_promocode.call_args[0][0]
    assert saved_arg.code == code_str.upper()
    assert saved_arg.discount_type == PromoCodeDiscountType.PERCENTAGE.value
    assert saved_arg.for_subscription_tier == SubscriptionTier.PREMIUM.value

@pytest.mark.asyncio
async def test_create_promocode_code_already_exists_raises_validation_error(promocode_service_instance: PromoCodeService, mock_db_service_promo: AsyncMock):
    code_str = "EXISTINGCODE123"
    await promocode_service_instance.create_promocode(code=code_str, discount_type=PromoCodeDiscountType.PERCENTAGE, discount_value=10)
    with pytest.raises(ValidationError, match=f"Промокод '{code_str.upper()}' уже существует или другая ошибка сохранения."):
        await promocode_service_instance.create_promocode(code=code_str, discount_type=PromoCodeDiscountType.FIXED_AMOUNT, discount_value=5)

@pytest.mark.asyncio
async def test_create_promocode_with_all_fields(promocode_service_instance: PromoCodeService, mock_db_service_promo: AsyncMock):
    """Тест создания промокода со всеми возможными полями."""
    code_str = "FULLPROMO"
    user_specific_id = 123
    active_from = datetime.now(timezone.utc) + timedelta(days=1)
    expires_at = datetime.now(timezone.utc) + timedelta(days=31) # Явно задаем expires_at

    created_promo = await promocode_service_instance.create_promocode(
        code=code_str,
        discount_type=PromoCodeDiscountType.FREE_TRIAL,
        discount_value=7, # 7 дней
        trial_tier_target=SubscriptionTier.VIP.value,
        max_uses=10,
        max_uses_per_user=1,
        user_specific_id=user_specific_id,
        active_from_date=active_from,
        expires_at_date=expires_at, # Используем expires_at_date вместо expires_in_days
        description="Full VIP Trial Promo",
        user_facing_description="Получи 7 дней VIP!",
        code_type=PromoCodeType.USER_SPECIFIC,
        is_active=True,
        for_subscription_tier=SubscriptionTier.VIP.value, # Указываем, что он для VIP тарифа
        min_purchase_amount=100 # Минимальная сумма покупки для применения (если это скидка)
    )
    assert created_promo is not None
    assert created_promo.code == code_str.upper()
    assert created_promo.discount_type == PromoCodeDiscountType.FREE_TRIAL.value
    assert created_promo.trial_tier_target == SubscriptionTier.VIP.value
    assert created_promo.user_specific_id == user_specific_id
    assert created_promo.active_from == active_from
    assert created_promo.expires_at == expires_at
    assert created_promo.for_subscription_tier == SubscriptionTier.VIP.value
    assert created_promo.min_purchase_amount == 100

    # Проверка, что именно эти данные были переданы в мок сохранения
    saved_arg: DBPromoCode = mock_db_service_promo.save_promocode.call_args[0][0]
    assert saved_arg.user_specific_id == user_specific_id
    assert saved_arg.active_from == active_from
    assert saved_arg.expires_at == expires_at
    assert saved_arg.min_purchase_amount == 100

@pytest.mark.asyncio
async def test_validate_promocode_valid_public(promocode_service_instance: PromoCodeService, mock_db_service_promo: AsyncMock):
    code_str = "VALIDPUBLICCODE1"
    db_promo = DBPromoCode(
        id=1, code=code_str.upper(), discount_type=PromoCodeDiscountType.PERCENTAGE.value, discount_value=10.0,
        max_uses=10, uses_count=0, is_active=True, code_type=PromoCodeType.PUBLIC.value,
        expires_at=(datetime.now(timezone.utc) + timedelta(days=10)).replace(tzinfo=None), # Убираем tzinfo для SQLite мока, если он его не поддерживает
        active_from=(datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None)
    )
    mock_db_service_promo._promocodes_db_storage[code_str.upper()] = db_promo # type: ignore
    user_id_db = 1
    mock_db_service_promo.get_user_promocode_usage_count.return_value = 0
    promo_obj = await promocode_service_instance.validate_promocode(code_str, user_id_db)
    assert promo_obj is not None; assert promo_obj.code == code_str.upper()


@pytest.mark.asyncio
async def test_validate_promocode_specific_for_tier_success(promocode_service_instance: PromoCodeService, mock_db_service_promo: AsyncMock):
    """Тест валидации промокода, привязанного к определенному тарифу, для этого тарифа."""
    code_str = "PREMIUMONLY"
    db_promo = DBPromoCode(
        id=20, code=code_str.upper(), discount_type=PromoCodeDiscountType.PERCENTAGE.value, discount_value=10.0,
        max_uses=10, uses_count=0, is_active=True, code_type=PromoCodeType.PUBLIC.value,
        expires_at=datetime.now(timezone.utc) + timedelta(days=10),
        for_subscription_tier=SubscriptionTier.PREMIUM.value # Только для Premium
    )
    mock_db_service_promo._promocodes_db_storage[code_str.upper()] = db_promo # type: ignore
    promo_obj = await promocode_service_instance.validate_promocode(code_str, target_tier_for_purchase=SubscriptionTier.PREMIUM)
    assert promo_obj is not None

@pytest.mark.asyncio
async def test_validate_promocode_specific_for_tier_fail_other_tier(promocode_service_instance: PromoCodeService, mock_db_service_promo: AsyncMock):
    """Тест: промокод для Premium не должен работать для покупки Basic."""
    code_str = "PREMIUMONLYFAIL"
    db_promo = DBPromoCode(
        id=21, code=code_str.upper(), discount_type=PromoCodeDiscountType.PERCENTAGE.value, discount_value=10.0,
        is_active=True, expires_at=datetime.now(timezone.utc) + timedelta(days=10),
        for_subscription_tier=SubscriptionTier.PREMIUM.value # Только для Premium
    )
    mock_db_service_promo._promocodes_db_storage[code_str.upper()] = db_promo # type: ignore
    with pytest.raises(ValidationError, match=f"Промокод '{code_str.upper()}' не действителен для выбранного тарифа 'Basic'."):
        await promocode_service_instance.validate_promocode(code_str, target_tier_for_purchase=SubscriptionTier.BASIC)


@pytest.mark.asyncio
async def test_validate_promocode_min_purchase_amount_success(promocode_service_instance: PromoCodeService, mock_db_service_promo: AsyncMock):
    """Тест: промокод с мин. суммой покупки, сумма покупки удовлетворяет."""
    code_str = "MINPURCH100"
    db_promo = DBPromoCode(
        id=22, code=code_str.upper(), discount_type=PromoCodeDiscountType.FIXED_AMOUNT.value, discount_value=50.0,
        is_active=True, expires_at=datetime.now(timezone.utc) + timedelta(days=10),
        min_purchase_amount=100 # Мин. сумма 100 звезд
    )
    mock_db_service_promo._promocodes_db_storage[code_str.upper()] = db_promo # type: ignore
    promo_obj = await promocode_service_instance.validate_promocode(code_str, purchase_amount_stars=150)
    assert promo_obj is not None

@pytest.mark.asyncio
async def test_validate_promocode_min_purchase_amount_fail(promocode_service_instance: PromoCodeService, mock_db_service_promo: AsyncMock):
    """Тест: промокод с мин. суммой покупки, сумма покупки НЕ удовлетворяет."""
    code_str = "MINPURCHFAIL"
    db_promo = DBPromoCode(
        id=23, code=code_str.upper(), discount_type=PromoCodeDiscountType.FIXED_AMOUNT.value, discount_value=50.0,
        is_active=True, expires_at=datetime.now(timezone.utc) + timedelta(days=10),
        min_purchase_amount=100
    )
    mock_db_service_promo._promocodes_db_storage[code_str.upper()] = db_promo # type: ignore
    with pytest.raises(ValidationError, match=f"Промокод '{code_str.upper()}' действителен для покупок от 100 звезд."):
        await promocode_service_instance.validate_promocode(code_str, purchase_amount_stars=50)

@pytest.mark.asyncio
async def test_apply_effects_fixed_amount_discount(promocode_service_instance: PromoCodeService):
    """Тест применения промокода на фиксированную скидку."""
    user_id_tg = 123; db_user_id = 1
    fixed_amount = 50.0
    promo = PromoCode(
        id=15, code="FIXED50", discount_type=PromoCodeDiscountType.FIXED_AMOUNT.value,
        discount_value=fixed_amount, is_active=True,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)
    )
    effects = await promocode_service_instance.apply_promocode_effects(db_user_id, user_id_tg, promo, purchase_amount_stars=200)
    assert effects is not None
    assert effects.get("discount_type") == PromoCodeDiscountType.FIXED_AMOUNT.value
    assert effects.get("discount_value") == fixed_amount
    assert "фиксированная скидка" in effects.get("description", "").lower()
    assert effects.get("final_price_after_discount") == 150.0 # 200 - 50

@pytest.mark.asyncio
async def test_apply_effects_fixed_amount_discount_exceeds_price(promocode_service_instance: PromoCodeService):
    """Тест: фиксированная скидка больше суммы покупки, цена должна стать 0 (или мин. цена, если есть)."""
    user_id_tg = 123; db_user_id = 1
    promo = PromoCode(id=16, code="FIXED100", discount_type=PromoCodeDiscountType.FIXED_AMOUNT.value, discount_value=100.0, is_active=True, created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
    
    # Предполагаем, что в Telegram Stars минимальная цена не может быть 0, а например 1 звезда.
    # Если такой логики нет, то final_price будет 0.
    # В PromoCodeService нет явной логики мин. цены, так что ожидаем, что скидка просто применится.
    # Если бы цена покупки была меньше скидки, то result_price = 0.
    # Если цена 80, скидка 100, то цена 0.
    effects = await promocode_service_instance.apply_promocode_effects(db_user_id, user_id_tg, promo, purchase_amount_stars=80)
    assert effects is not None
    assert effects.get("final_price_after_discount") == 0 # Или минимально возможная цена, если она есть

# Остальные тесты из предыдущего ответа:
# test_validate_promocode_expired, test_validate_promocode_max_uses_reached,
# test_validate_promocode_max_user_uses_reached, test_apply_promocode_effects_bonus_messages,
# test_apply_promocode_effects_free_trial_success, test_apply_promocode_effects_trial_user_cannot_receive,
# test_mark_promocode_as_used_public_code, test_deactivate_promocode, test_delete_promocode_success
# (их можно скопировать из предыдущего ответа, адаптировав под _reset_storages)
