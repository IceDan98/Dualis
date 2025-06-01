# services/referral_ab_testing.py
import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple, Any

from database.operations import DatabaseService
from database.enums import SubscriptionTier
from services.promocode_system import PromoCodeService
from services.subscription_system import SubscriptionService
from config.settings import BotConfig
from utils.error_handler import handle_errors

logger = logging.getLogger(__name__)

class ReferralRewardType(Enum):
    BONUS_MESSAGES = "bonus_messages"           
    FREE_TRIAL_DAYS = "free_trial_days"         
    DISCOUNT_ON_PURCHASE = "discount_on_purchase" 

@dataclass
class ReferralRewardConfig:
    type: ReferralRewardType
    value: float 
    description: str 
    trial_tier: Optional[SubscriptionTier] = None 
    discount_duration_days: Optional[int] = 30 

@dataclass
class AppliedReferralReward:
    reward_type: str; reward_value: float; description: str
    granted_at: str; claimed_at: Optional[str] = None 
    extra_data: Dict[str, Any] = field(default_factory=dict)
    def to_dict(self) -> Dict:
        return {"reward_type": self.reward_type, "reward_value": self.reward_value, 
                "description": self.description, "granted_at": self.granted_at, 
                "claimed_at": self.claimed_at, "extra_data": self.extra_data,}

class ReferralService:
    REFERRAL_STATS_PREFERENCE_KEY = "referral_stats"
    REFERRED_BY_CODE_PREFERENCE_KEY = "referred_by_code" 
    REFERRER_ID_PREFERENCE_KEY = "referrer_db_id" 
    USER_APPLIED_REWARDS_PERSONA = "referral_rewards_applied" 

    DEFAULT_REFERRER_REWARD = ReferralRewardConfig(type=ReferralRewardType.BONUS_MESSAGES, value=50, description="50 бонусных сообщений за каждого друга, который зарегистрируется!")
    DEFAULT_REFEREE_REWARD = ReferralRewardConfig(type=ReferralRewardType.BONUS_MESSAGES, value=25, description="25 приветственных бонусных сообщений за регистрацию по приглашению!")
    SUCCESSFUL_REFERRAL_BONUS_FOR_REFERRER = ReferralRewardConfig(type=ReferralRewardType.DISCOUNT_ON_PURCHASE, value=15, description="Скидка 15% на следующую подписку за первую покупку вашего друга!", discount_duration_days=60)
    MILESTONE_REWARDS_CONFIG: Dict[int, ReferralRewardConfig] = {
        5: ReferralRewardConfig(type=ReferralRewardType.FREE_TRIAL_DAYS, value=7, description="7 дней тарифа 'Basic' бесплатно за 5 успешных приглашений!", trial_tier=SubscriptionTier.BASIC),
        15: ReferralRewardConfig(type=ReferralRewardType.FREE_TRIAL_DAYS, value=15, description="15 дней тарифа 'Premium' бесплатно за 15 успешных приглашений!", trial_tier=SubscriptionTier.PREMIUM),
        30: ReferralRewardConfig(type=ReferralRewardType.DISCOUNT_ON_PURCHASE, value=50, description="Супер-скидка 50% на любую подписку за 30 успешных приглашений!", discount_duration_days=90),
    }

    def __init__(self, db_service: DatabaseService, subscription_service: SubscriptionService,
                 promocode_service: PromoCodeService, config: BotConfig):
        self.db_service = db_service
        self.subscription_service = subscription_service
        self.promocode_service = promocode_service
        self.config = config

    async def generate_referral_code_for_user(self, user_id_db: int) -> str:
        existing_referral_code_obj = await self.db_service.get_referral_code_by_user_id(user_id_db)
        if existing_referral_code_obj:
            return existing_referral_code_obj.code
        
        for attempt in range(5): 
            timestamp_part = str(int(datetime.now(timezone.utc).timestamp() * 1000))[-5:]
            unique_input = f"REF-{user_id_db}-{timestamp_part}-{random.randint(1000,9999)}-{attempt}" # Добавил attempt для большей уникальности
            code_hash = hashlib.sha1(unique_input.encode()).hexdigest()[:7].upper()
            new_referral_code_str = f"INVITE{code_hash}"
            try:
                created_code_obj = await self.db_service.create_referral_code(user_id_db, new_referral_code_str)
                if created_code_obj:
                    await self._initialize_referral_stats(user_id_db)
                    logger.info(f"Сгенерирован и сохранен реф. код {created_code_obj.code} для user_id_db={user_id_db}")
                    return created_code_obj.code
            except DatabaseError as e: 
                if "уже существует" in str(e) or "unique constraint" in str(e).lower(): # Проверка на уникальность
                    logger.warning(f"Коллизия при генерации реф. кода '{new_referral_code_str}' (попытка {attempt+1}): {e}. Попытка еще раз.")
                    await asyncio.sleep(random.uniform(0.05, 0.2)) # Небольшая случайная задержка
                else:
                    logger.error(f"DatabaseError при создании реф. кода для user_id_db={user_id_db} (попытка {attempt+1}): {e}", exc_info=True)
                    raise # Перебрасываем другие ошибки БД
        
        logger.error(f"Не удалось сгенерировать уникальный реферальный код для user_id_db={user_id_db} после нескольких попыток.")
        raise Exception(f"Не удалось сгенерировать уникальный реферальный код для user_id_db={user_id_db}.")

    async def _get_user_referral_code(self, user_id_db: int) -> Optional[str]:
        ref_code_obj = await self.db_service.get_referral_code_by_user_id(user_id_db)
        return ref_code_obj.code if ref_code_obj else None

    async def _initialize_referral_stats(self, user_id_db: int):
        initial_stats = {'referrals_initiated_count': 0, 'referrals_completed_count': 0, 'rewards_earned_log': [], 'last_milestone_achieved': 0, 'created_at': datetime.now(timezone.utc).isoformat()}
        await self.db_service.update_user_preference(user_id_db=user_id_db, key=self.REFERRAL_STATS_PREFERENCE_KEY, value=initial_stats, persona='system', preference_type='json')

    async def get_referral_stats(self, user_id_db: int) -> Dict[str, Any]:
        prefs = await self.db_service.get_user_preferences(user_id_db, persona='system')
        stats = prefs.get(self.REFERRAL_STATS_PREFERENCE_KEY)
        if not stats or not isinstance(stats, dict):
            await self._initialize_referral_stats(user_id_db)
            new_prefs = await self.db_service.get_user_preferences(user_id_db, persona='system')
            stats = new_prefs.get(self.REFERRAL_STATS_PREFERENCE_KEY, {})
        return stats
    
    async def find_referrer_db_id_by_code(self, referral_code: str) -> Optional[int]:
        """Находит ID_DB реферера по его реферальному коду, используя новую таблицу."""
        if not referral_code or len(referral_code) < 5: # Базовая проверка
            logger.debug(f"Попытка поиска по некорректному реферальному коду: '{referral_code}'")
            return None
        referrer_user: Optional[DBUser] = await self.db_service.get_user_by_referral_code(referral_code.upper())
        if referrer_user:
            logger.info(f"Найден реферер ID_DB {referrer_user.id} для кода {referral_code.upper()}")
            return referrer_user.id
        else:
            logger.info(f"Реферер для кода {referral_code.upper()} не найден.")
            return None

    @handle_errors()
    async def process_referral_code_usage(self, referee_user_id_tg: int, referral_code_entered: str) -> Dict[str, Any]:
        referee_db_user = await self.db_service.get_or_create_user(telegram_id=referee_user_id_tg)
        if not referee_db_user: return {"success": False, "message": "Ошибка пользователя."}
        
        referral_code_entered_upper = referral_code_entered.strip().upper() # Убираем пробелы и приводим к верхнему регистру

        # Проверка, не использовал ли пользователь уже реферальный код
        referee_prefs = await self.db_service.get_user_preferences(referee_db_user.id, persona='system')
        if referee_prefs.get(self.REFERRED_BY_CODE_PREFERENCE_KEY):
            # Можно добавить проверку, не совпадает ли уже использованный код с новым, на случай если пользователь пытается ввести тот же код еще раз
            logger.info(f"Пользователь TG ID {referee_user_id_tg} уже использовал реферальный код: {referee_prefs.get(self.REFERRED_BY_CODE_PREFERENCE_KEY)}")
            return {"success": False, "message": "Вы уже использовали реферальный код ранее."}
        
        referrer_db_id = await self.find_referrer_db_id_by_code(referral_code_entered_upper)
        if not referrer_db_id: 
            return {"success": False, "message": "Введенный реферальный код не найден или недействителен."}
        
        if referrer_db_id == referee_db_user.id: 
            return {"success": False, "message": "Нельзя использовать свой собственный реферальный код."}
        
        # Сохраняем информацию о том, кто пригласил и каким кодом
        await self.db_service.update_user_preference(referee_db_user.id, self.REFERRED_BY_CODE_PREFERENCE_KEY, referral_code_entered_upper, 'system', 'string')
        await self.db_service.update_user_preference(referee_db_user.id, self.REFERRER_ID_PREFERENCE_KEY, referrer_db_id, 'system', 'int')
        
        # Обновляем статистику реферера
        referrer_stats = await self.get_referral_stats(referrer_db_id)
        referrer_stats['referrals_initiated_count'] = referrer_stats.get('referrals_initiated_count', 0) + 1
        await self.db_service.update_user_preference(referrer_db_id, self.REFERRAL_STATS_PREFERENCE_KEY, referrer_stats, 'system', 'json')
        
        # Применяем награды
        referee_reward_applied = await self._apply_reward_to_user(referee_db_user.id, referee_user_id_tg, self.DEFAULT_REFEREE_REWARD, f"welcome_bonus_for_code_{referral_code_entered_upper}")
        await self._apply_reward_to_user(referrer_db_id, None, self.DEFAULT_REFERRER_REWARD, f"new_referral_initiated_by_{referee_user_id_tg}") # user_id_tg для реферера не нужен здесь
        
        logger.info(f"Реферал обработан: реферер ID_DB={referrer_db_id} пригласил рефери TG_ID={referee_user_id_tg} кодом {referral_code_entered_upper}.")
        
        reward_message_for_referee = self.DEFAULT_REFEREE_REWARD.description
        if referee_reward_applied and referee_reward_applied.description: # Если есть специфичное описание примененной награды
            reward_message_for_referee = referee_reward_applied.description

        return {"success": True, 
                "message": f"Реферальный код {referral_code_entered_upper} успешно применен! {reward_message_for_referee}",
                "referee_reward_description": reward_message_for_referee
               }

    async def _apply_reward_to_user(self, user_id_db: int, user_id_tg: Optional[int], 
                                  reward_config: ReferralRewardConfig, reward_source_info: str
                                 ) -> Optional[AppliedReferralReward]:
        now_iso = datetime.now(timezone.utc).isoformat()
        applied_reward = AppliedReferralReward(reward_type=reward_config.type.value, reward_value=reward_config.value, description=reward_config.description, granted_at=now_iso, extra_data={"source": reward_source_info})
        action_taken = False
        
        # Получаем TG ID, если он не передан, но нужен
        user_tg_id_for_action = user_id_tg
        if not user_tg_id_for_action and reward_config.type in [ReferralRewardType.BONUS_MESSAGES, ReferralRewardType.FREE_TRIAL_DAYS]:
            user_obj = await self.db_service.get_user_by_db_id(user_id_db)
            if user_obj: user_tg_id_for_action = user_obj.telegram_id
            else: logger.error(f"Не найден TG ID для user_id_db={user_id_db} для награды типа {reward_config.type.value}."); return None

        if reward_config.type == ReferralRewardType.BONUS_MESSAGES:
            if not user_tg_id_for_action: return None # Проверка на всякий случай
            await self.subscription_service.add_bonus_messages(user_tg_id_for_action, int(reward_config.value), source=f"referral_{reward_source_info}"); action_taken = True
        elif reward_config.type == ReferralRewardType.FREE_TRIAL_DAYS:
            if not user_tg_id_for_action or not reward_config.trial_tier: return None
            trial_activation_result = await self.subscription_service.activate_trial_subscription(user_id_tg=user_tg_id_for_action, trial_tier_value=reward_config.trial_tier.value, trial_days=int(reward_config.value), promocode_used=f"REFERRAL_{reward_source_info}")
            action_taken = trial_activation_result.get("success", False)
            if action_taken: applied_reward.extra_data["trial_tier"] = reward_config.trial_tier.value
        elif reward_config.type == ReferralRewardType.DISCOUNT_ON_PURCHASE:
            unique_promo_code_str = self.promocode_service.generate_random_code(length=8, prefix="REFS")
            try:
                created_promo = await self.promocode_service.create_promocode(code=unique_promo_code_str, discount_type=PromoCodeDiscountType.PERCENTAGE, discount_value=reward_config.value, max_uses=1, max_uses_per_user=1, created_by=0, # created_by 0 (система)
                                                                             expires_in_days=reward_config.discount_duration_days, description=f"Реф. скидка {reward_config.value}% для user_id_db={user_id_db} (источник: {reward_source_info})", 
                                                                             user_facing_description=f"Ваша персональная скидка {reward_config.value}% по реферальной программе!")
                if created_promo: 
                    applied_reward.extra_data["discount_promocode"] = created_promo.code
                    applied_reward.extra_data["discount_value_percent"] = reward_config.value
                    action_taken = True
                    # Оповещение пользователя о выданном промокоде (если user_id_tg известен)
                    if user_tg_id_for_action:
                        try:
                            await self.bot_instance.bot.send_message( # type: ignore # Предполагается, что bot_instance передан
                                user_tg_id_for_action,
                                f"🎉 Поздравляем! Вы получили персональный промокод на скидку: `{created_promo.code}`\n"
                                f"Он дает скидку {reward_config.value}% на следующую подписку и действитеlen {reward_config.discount_duration_days} дней.\n"
                                f"Вы можете использовать его в разделе /premium при выборе тарифа."
                            )
                        except Exception as e_notify_promo:
                            logger.error(f"Не удалось оповестить user TG ID {user_tg_id_for_action} о промокоде {created_promo.code}: {e_notify_promo}")

                else: logger.error(f"Не создан скидочный промокод для реф. награды user_id_db={user_id_db}")
            except ValidationError: logger.error(f"Сгенерированный скидочный промокод {unique_promo_code_str} уже существует.")
            except Exception as e_promo_create: logger.error(f"Ошибка создания промокода для реф. награды user {user_id_db}: {e_promo_create}", exc_info=True)

        if action_taken:
            reward_log_key = f"applied_reward_{reward_config.type.value}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}_{random.randint(100,999)}" # Добавил рандом для уникальности ключа
            await self.db_service.update_user_preference(user_id_db=user_id_db, key=reward_log_key, value=applied_reward.to_dict(), persona=self.USER_APPLIED_REWARDS_PERSONA, preference_type='json')
            logger.info(f"Награда '{reward_config.description}' применена к user_id_db={user_id_db}. Источник: {reward_source_info}. Данные: {applied_reward.extra_data}")
            return applied_reward
        else:
            logger.warning(f"Не удалось применить награду '{reward_config.description}' для user_id_db={user_id_db}. Источник: {reward_source_info}")
        return None


    async def mark_referral_as_completed(self, referee_user_id_tg: int):
        referee_db_user = await self.db_service.get_user_by_telegram_id(referee_user_id_tg)
        if not referee_db_user: logger.error(f"Рефери с TG ID {referee_user_id_tg} не найден в БД."); return
        
        referee_prefs = await self.db_service.get_user_preferences(referee_db_user.id, persona='system')
        referrer_db_id = referee_prefs.get(self.REFERRER_ID_PREFERENCE_KEY)
        if not referrer_db_id or not isinstance(referrer_db_id, int): 
            logger.warning(f"Для рефери TG ID {referee_user_id_tg} не найден ID реферера в UserPreference.")
            return

        referrer_stats = await self.get_referral_stats(referrer_db_id)
        # Предотвращение повторного начисления за того же реферала, если логика не идеальна
        # Можно добавить проверку, не был ли этот referee_user_id_tg уже засчитан для referrer_db_id

        referrer_stats['referrals_completed_count'] = referrer_stats.get('referrals_completed_count', 0) + 1
        logger.info(f"Реферал TG ID {referee_user_id_tg} успешен для реферера DB ID {referrer_db_id}. Всего успешных: {referrer_stats['referrals_completed_count']}")
        
        bonus_reward_applied = await self._apply_reward_to_user(user_id_db=referrer_db_id, user_id_tg=None, # TG ID реферера для оповещения о промокоде нужен, получим его ниже
                                                              reward_config=self.SUCCESSFUL_REFERRAL_BONUS_FOR_REFERRER, 
                                                              reward_source_info=f"successful_referral_of_{referee_user_id_tg}")
        if bonus_reward_applied: 
            referrer_stats.setdefault('rewards_earned_log', []).append(bonus_reward_applied.to_dict())
        
        completed_count = referrer_stats['referrals_completed_count']; last_milestone = referrer_stats.get('last_milestone_achieved', 0)
        
        referrer_user_obj_for_tg_id = None # Для кеширования
        
        for milestone_count, reward_config in sorted(self.MILESTONE_REWARDS_CONFIG.items()):
            if completed_count >= milestone_count and last_milestone < milestone_count:
                referrer_tg_id_for_milestone: Optional[int] = None
                if reward_config.type == ReferralRewardType.FREE_TRIAL_DAYS or reward_config.type == ReferralRewardType.DISCOUNT_ON_PURCHASE : # Если награда требует оповещения или действия с TG ID
                    if not referrer_user_obj_for_tg_id: # Получаем объект пользователя реферера, если еще не получали
                         referrer_user_obj_for_tg_id = await self.db_service.get_user_by_db_id(referrer_db_id)
                    if referrer_user_obj_for_tg_id: referrer_tg_id_for_milestone = referrer_user_obj_for_tg_id.telegram_id
                    else: logger.error(f"Не найден TG ID для реферера DB ID {referrer_db_id} для майлстоун-награды."); continue
                
                milestone_reward_applied = await self._apply_reward_to_user(
                    user_id_db=referrer_db_id, 
                    user_id_tg=referrer_tg_id_for_milestone, 
                    reward_config=reward_config, 
                    reward_source_info=f"milestone_{milestone_count}_referrals"
                )
                if milestone_reward_applied: 
                    referrer_stats.setdefault('rewards_earned_log', []).append(milestone_reward_applied.to_dict())
                    referrer_stats['last_milestone_achieved'] = milestone_count
                break # Только одна майлстоун-награда за раз (следующая на следующем completed_referral)
        
        await self.db_service.update_user_preference(referrer_db_id, self.REFERRAL_STATS_PREFERENCE_KEY, referrer_stats, 'system', 'json')


    async def get_user_referral_dashboard_info(self, user_id_tg: int) -> Dict[str, Any]:
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: return {"error": "User not found."}
        
        referral_code = await self.generate_referral_code_for_user(db_user.id) 
        stats = await self.get_referral_stats(db_user.id)
        
        user_rewards_prefs = await self.db_service.get_user_preferences(db_user.id, persona=self.USER_APPLIED_REWARDS_PERSONA)
        applied_rewards_list: List[AppliedReferralReward] = []
        for key, reward_data_dict_or_list in user_rewards_prefs.items(): # reward_data может быть и словарем и списком словарей
            rewards_to_process = []
            if key.startswith("applied_reward_"):
                if isinstance(reward_data_dict_or_list, dict):
                    rewards_to_process.append(reward_data_dict_or_list)
                # elif isinstance(reward_data_dict_or_list, list): # Если бы rewards_earned_log хранился так
                #     rewards_to_process.extend(reward_data_dict_or_list)

            for reward_data_dict in rewards_to_process:
                try: 
                    # Убедимся, что все нужные поля есть перед созданием AppliedReferralReward
                    if all(k in reward_data_dict for k in ['reward_type', 'reward_value', 'description', 'granted_at']):
                        applied_rewards_list.append(AppliedReferralReward(**reward_data_dict))
                    else:
                         logger.warning(f"Неполные данные для десериализации награды: {key} для user_id_db {db_user.id}. Данные: {reward_data_dict}")
                except Exception as e_reward_parse: 
                    logger.warning(f"Ошибка десериализации награды: {key} для user_id_db {db_user.id}. Ошибка: {e_reward_parse}. Данные: {reward_data_dict}")

        # Также можно брать из referrer_stats['rewards_earned_log'], если он более полный
        if 'rewards_earned_log' in stats and isinstance(stats['rewards_earned_log'], list):
            for reward_data_dict in stats['rewards_earned_log']:
                 try: 
                    if all(k in reward_data_dict for k in ['reward_type', 'reward_value', 'description', 'granted_at']):
                         # Проверяем, нет ли уже такой награды в applied_rewards_list по granted_at и типу/описанию, чтобы избежать дублей
                         is_duplicate = any(
                             ar.granted_at == reward_data_dict['granted_at'] and 
                             ar.description == reward_data_dict['description'] 
                             for ar in applied_rewards_list
                         )
                         if not is_duplicate:
                             applied_rewards_list.append(AppliedReferralReward(**reward_data_dict))
                    else:
                         logger.warning(f"Неполные данные для десериализации награды из rewards_earned_log user_id_db {db_user.id}. Данные: {reward_data_dict}")
                 except Exception as e_reward_parse_log: 
                     logger.warning(f"Ошибка десериализации награды из rewards_earned_log user_id_db {db_user.id}. Ошибка: {e_reward_parse_log}. Данные: {reward_data_dict}")
        
        applied_rewards_list.sort(key=lambda r: r.granted_at, reverse=True) # Сортируем по дате получения

        next_milestone_info = None; completed_count = stats.get('referrals_completed_count', 0); last_milestone_val = stats.get('last_milestone_achieved', 0)
        for ms_count, ms_reward_config in sorted(self.MILESTONE_REWARDS_CONFIG.items()):
            if ms_count > last_milestone_val: 
                next_milestone_info = {"needed": max(0, ms_count - completed_count), "total_for_milestone": ms_count, "reward_description": ms_reward_config.description}; break
        
        bot_username = self.config.bot_username 
        referral_link = f"https://t.me/{bot_username}?start={referral_code}" if bot_username and bot_username != "YOUR_BOT_USERNAME_HERE" and bot_username != "default_bot_username_api_failed" else f"Код: {referral_code} (Ссылка будет доступна, когда имя бота будет настроено)"
        
        return {"referral_code": referral_code, 
                "initiated_referrals": stats.get('referrals_initiated_count', 0), 
                "completed_referrals": completed_count, 
                "applied_rewards": [ar.to_dict() for ar in applied_rewards_list], 
                "next_milestone": next_milestone_info, 
                "referral_link": referral_link}

# ================== A/B Testing Service (без изменений) ==================
class ABTestService:
    # ... (код класса ABTestService без изменений) ...
    def __init__(self, db_service: DatabaseService): 
        self.db_service = db_service
        self.active_tests: Dict[str, Dict[str, Any]] = {'welcome_message_variant': {'name': 'Тест приветственного сообщения', 'description': 'Сравниваем два варианта приветственного сообщения.', 'variants': {'control': {'weight': 50, 'data': {'message_key': 'welcome_default'}}, 'variant_A': {'weight': 50, 'data': {'message_key': 'welcome_variant_a'}},}, 'goal_metric': 'day1_retention', 'status': 'active', 'start_date': datetime.now(timezone.utc).isoformat(),}}
        self.USER_AB_TEST_ASSIGNMENTS_PERSONA = "ab_test_assignments"
    async def assign_user_to_test_variant(self, user_id_db: int, test_name: str) -> Optional[str]:
        if test_name not in self.active_tests or self.active_tests[test_name]['status'] != 'active': return 'control' 
        assignments = await self.db_service.get_user_preferences(user_id_db, persona=self.USER_AB_TEST_ASSIGNMENTS_PERSONA)
        assignment_key = f"test_{test_name}_variant"; existing_variant = assignments.get(assignment_key)
        if existing_variant: return existing_variant
        test_config = self.active_tests[test_name]; variants_config = test_config['variants']
        population = [variant_name for variant_name in variants_config.keys()]; weights = [details['weight'] for details in variants_config.values()]
        if not population or not weights or sum(weights) == 0: logger.warning(f"Некорректные веса A/B теста '{test_name}'."); return 'control'
        chosen_variant = random.choices(population, weights=weights, k=1)[0]
        await self.db_service.update_user_preference(user_id_db, assignment_key, chosen_variant, persona=self.USER_AB_TEST_ASSIGNMENTS_PERSONA, preference_type='string')
        logger.info(f"User ID_DB {user_id_db} назначен варианту '{chosen_variant}' теста '{test_name}'."); return chosen_variant
    async def get_user_variant_data(self, user_id_db: int, test_name: str) -> Optional[Dict[str, Any]]:
        variant_name = await self.assign_user_to_test_variant(user_id_db, test_name)
        if variant_name and test_name in self.active_tests: return self.active_tests[test_name]['variants'].get(variant_name, {}).get('data')
        return None
    async def track_test_goal_achieved(self, user_id_db: int, test_name: str, goal_metric_value: float = 1.0):
        assignments = await self.db_service.get_user_preferences(user_id_db, persona=self.USER_AB_TEST_ASSIGNMENTS_PERSONA)
        variant_assigned = assignments.get(f"test_{test_name}_variant")
        if variant_assigned and test_name in self.active_tests:
            metric_name_for_db = f"ab_test_{test_name}_{self.active_tests[test_name]['goal_metric']}"
            await self.db_service.save_statistic(metric_name=metric_name_for_db, metric_value=goal_metric_value, user_id=user_id_db, additional_data={'test_name': test_name, 'variant': variant_assigned})

class ABTestIntegration:
    # ... (код класса ABTestIntegration без изменений) ...
    def __init__(self, ab_service: ABTestService, db_service: DatabaseService):
        self.ab_service = ab_service; self.db_service = db_service 
    async def get_welcome_message_key_for_user(self, user_id_tg: int, default_key: str = 'welcome_default') -> str:
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: return default_key
        variant_data = await self.ab_service.get_user_variant_data(db_user.id, 'welcome_message_variant')
        return variant_data.get('message_key', default_key) if variant_data else default_key
    async def get_subscription_price_for_user(self, user_id_tg: int, tier: SubscriptionTier, duration: str = "monthly", default_price_stars: Optional[int] = None) -> int:
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: return default_price_stars or 0 
        if tier == SubscriptionTier.PREMIUM and duration == "monthly":
            variant_data = await self.ab_service.get_user_variant_data(db_user.id, 'premium_price_test') 
            if variant_data and 'price_stars' in variant_data: return int(variant_data['price_stars'])
        if default_price_stars is not None: return default_price_stars
        logger.warning(f"Не удалось определить цену для {tier.value} {duration} через A/B или дефолт."); return 0
