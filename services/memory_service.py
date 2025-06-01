# services/memory_service.py
import logging
from typing import List, Dict, Optional, Tuple, Any, TYPE_CHECKING
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from enum import Enum

# Используем Enum из общего файла, если он создан, иначе оставляем локально
try:
    from database.enums import SubscriptionTier # Предполагаем, что SubscriptionTier есть в enums
except ImportError:
    # Фоллбэк, если enums.py еще не создан или SubscriptionTier там нет
    class SubscriptionTier(Enum): # type: ignore
        FREE = "free"; BASIC = "basic"; PREMIUM = "premium"; VIP = "vip"

from database.operations import DatabaseService
from utils.error_handler import handle_errors, DatabaseError
from database.models import Memory as DBMemory, User as DBUser # Добавил DBUser

if TYPE_CHECKING:
    from main import AICompanionBot 
    from services.subscription_system import SubscriptionService, TierLimits # TierLimits нужен для _analyze_memory_upgrade_benefits

logger = logging.getLogger(__name__)

class MemoryType(Enum):
    """Content types of memories."""
    # Типы хранения (определяются тарифом)
    SESSION = "session" 
    SHORT_TERM = "short_term" 
    LONG_TERM = "long_term" 
    PERMANENT = "permanent" 

    # Типы контента (определяются при сохранении)
    INSIGHT = "insight"         # Мысли, озарения пользователя
    PREFERENCE = "preference"   # Предпочтения пользователя
    EVENT = "event"             # События из жизни пользователя или диалога
    EMOTION = "emotion"         # Зафиксированное эмоциональное состояние
    INTIMATE = "intimate"       # Интимные детали, фантазии
    PERSONAL = "personal"       # Общие личные данные
    USER_FACT = "user_fact"     # Конкретные факты о пользователе
    GENERATED_STORY = "generated_story" # Истории, созданные через FSM
    GENERAL = "general"         # Общее, не классифицированное

class MemoryPriority(Enum):
    """Priorities for memory items."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4
    PERMANENT = 5 # Для воспоминаний, которые не должны удаляться по лимиту (но могут по сроку хранения, если он не -1)

class MemoryService:
    """Manages user memories, considering subscription tiers and relevance."""

    def __init__(self, db_service: DatabaseService, 
                 subscription_service: Any, # Заменить Any на SubscriptionService
                 bot_instance: Optional['AICompanionBot'] = None): 
        self.db_service = db_service
        self.subscription_service = subscription_service
        self.bot_instance = bot_instance 

        # Веса для расчета важности воспоминания
        self._importance_weights = {
            'emotional_keywords': 0.3, 'personal_details': 0.25,
            'preferences': 0.2, 'frequency_of_topic': 0.15, # Будет сложнее реализовать без NLP
            'recency': 0.1, 'user_marked_important': 0.4, # Если пользователь явно пометил
            'explicit_request_to_remember': 0.35, # Если пользователь сказал "запомни"
        }
        # Ключевые слова для определения эмоциональной окраски и типа контента
        self._emotional_keywords: Dict[str, List[str]] = {
            'positive': ['люблю', 'нравится', 'обожаю', 'восхищаюсь', 'радуюсь', 'счастлив', 'прекрасно', 'отлично', 'замечательно', 'чудесно'],
            'negative': ['ненавижу', 'не нравится', 'раздражает', 'злюсь', 'грущу', 'печально', 'ужасно', 'плохо', 'бесит', 'разочарован'],
            'intimate': ['хочу тебя', 'желаю тебя', 'возбуждает', 'страстно', 'интимно', 'секс', 'оргазм', 'ласки', 'поцелуи', 'объятия'],
            'important': ['важно', 'значимо', 'помню', 'запомни', 'никогда не забуду', 'особенное', 'ключевое', 'главное'],
            'fear': ['боюсь', 'страшно', 'опасаюсь', 'тревожно'],
            'surprise': ['удивлен', 'неожиданно', 'ого', 'вот это да'],
            'preference_markers': ["мне нравится", "я люблю", "я предпочитаю", "я не люблю", "мой любимый", "я всегда", "я никогда", "обожаю", "терпеть не могу"],
            'user_fact_markers': ["я живу в", "моя работа", "у меня есть", "я родился", "мое хобби", "мой возраст", "меня зовут", "я являюсь", "я работаю"],
            'event_markers': ["помню когда", "однажды я", "в прошлом году", "это случилось", "мы ездили", "я был", "я сделала", "вчера", "недавно"],
            'insight_markers': ["я понял", "я осознал", "мне пришло в голову", "вывод такой", "оказывается"],
        }

    @handle_errors(reraise_as=None) # Не перебрасываем, чтобы бот мог продолжить работу
    async def save_memory(self, user_id_tg: int, persona: str, content: str,
                         memory_content_type: MemoryType, # Используем Enum
                         tags: Optional[List[str]] = None,
                         context: Optional[str] = None,
                         relevance_score_override: Optional[float] = None,
                         emotional_weight_override: Optional[float] = None,
                         user_marked_important: bool = False,
                         explicit_request_to_remember: bool = False
                         ) -> Optional[DBMemory]:
        
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg) # get_user_by_telegram_id, а не get_or_create
        if not db_user:
            # Попытка создать пользователя, если он не существует (например, первое сообщение)
            if self.bot_instance and hasattr(self.bot_instance, '_get_or_create_user_with_new_flag'):
                # Нужен объект AiogramUser, которого здесь нет. Это проблема.
                # Лучше, чтобы user_id_tg всегда соответствовал существующему DBUser.
                # Пока что, если юзера нет, память не сохраняем.
                logger.error(f"User with telegram_id={user_id_tg} not found for saving memory. Memory not saved.")
                return None
            else:
                logger.error(f"User with telegram_id={user_id_tg} not found and bot_instance not available to create user. Memory not saved.")
                return None


        # Получаем текущую подписку и лимиты
        # SubscriptionService должен быть инициализирован и доступен через self.subscription_service
        if not hasattr(self.subscription_service, 'get_user_subscription') or \
           not hasattr(self.subscription_service, 'plans'):
            logger.error("SubscriptionService не инициализирован или не имеет необходимых атрибутов. Невозможно определить лимиты памяти.")
            return None
            
        subscription_data = await self.subscription_service.get_user_subscription(user_id_tg)
        current_tier_value_str = subscription_data.get("tier", SubscriptionTier.FREE.value)
        try:
            current_tier_enum = SubscriptionTier(current_tier_value_str)
        except ValueError:
            logger.warning(f"Unknown tier '{current_tier_value_str}' for user {user_id_tg}. Using FREE tier limits for memory.")
            current_tier_enum = SubscriptionTier.FREE

        # Используем TierLimits из SubscriptionService.plans
        sub_limits: 'TierLimits' = self.subscription_service.plans.PLANS.get(
            current_tier_enum, self.subscription_service.plans.PLANS[SubscriptionTier.FREE]
        )
        
        # Определяем тип хранилища и срок хранения на основе тарифа
        current_memory_storage_type = MemoryType(sub_limits.memory_type) # e.g., "short_term", "permanent"
        max_entries = sub_limits.max_memory_entries
        retention_days = sub_limits.memory_retention_days

        # 1. Очистка старых/истекших воспоминаний перед сохранением нового
        await self._cleanup_expired_memories(db_user.id, persona)

        # 2. Проверка лимита на количество записей (если не безлимит)
        if max_entries != -1: # -1 означает безлимит
            # Оптимизированный подсчет через DatabaseService
            current_active_mem_count = await self.db_service.get_active_memory_count_for_user(db_user.id, persona)
            
            if current_active_mem_count >= max_entries:
                # Если лимит достигнут, удаляем самые старые/низкоприоритетные
                await self._cleanup_low_priority_memories(db_user.id, max_entries, persona)
                # Повторно проверяем количество после очистки
                current_active_mem_count = await self.db_service.get_active_memory_count_for_user(db_user.id, persona)
                if current_active_mem_count >= max_entries:
                    logger.warning(f"Memory limit ({max_entries}) for user {db_user.id} (tier {current_tier_enum.value}) reached even after cleanup. New memory not saved.")
                    return None # Не сохраняем, если лимит все еще превышен

        # Расчет важности и приоритета
        importance_score = relevance_score_override if relevance_score_override is not None \
                           else await self._calculate_memory_importance(content, memory_content_type.value, 
                                                                        user_marked_important=user_marked_important,
                                                                        explicit_request_to_remember=explicit_request_to_remember)
        
        priority_value = self._determine_priority_from_importance(importance_score, current_memory_storage_type)
        
        # Расчет даты истечения
        expires_at_dt = self._calculate_expiration_date(current_memory_storage_type, retention_days)
        
        tags_list_final = tags or []
        # Можно добавить автоматическое тегирование на основе контента, если нужно
        # tags_list_final.extend(self._auto_tag_content(content))
        tags_str = ",".join(list(set(tags_list_final))) # Уникальные теги

        memory_data_for_db = {
            'user_id': db_user.id, 
            'persona': persona, 
            'content': content,
            'memory_type': memory_content_type.value, # Сохраняем значение Enum
            'relevance_score': importance_score, # Используем рассчитанную или переданную важность
            'emotional_weight': emotional_weight_override if emotional_weight_override is not None else importance_score, # По умолчанию равно важности
            'tags': tags_str, 
            'context': context,
            'tier_created': current_tier_enum.value, # Тариф, на котором создано
            'expires_at': expires_at_dt,
            'priority': priority_value
        }
        try:
            saved_memory_db_instance = await self.db_service.save_memory(**memory_data_for_db)
            logger.info(f"Saved memory ID {saved_memory_db_instance.id} for user {db_user.id} (TG: {user_id_tg}), "
                        f"content type: {memory_content_type.value}, storage type: {current_memory_storage_type.value}, "
                        f"importance: {importance_score:.2f}, priority: {priority_value}")
            return saved_memory_db_instance
        except Exception as e:
            logger.error(f"Error saving memory to DB for user_id {db_user.id}: {e}", exc_info=True)
            return None

    def _determine_priority_from_importance(self, importance_score: float, memory_storage_type: MemoryType) -> int:
        """Определяет приоритет воспоминания на основе его важности и типа хранения."""
        if memory_storage_type == MemoryType.PERMANENT:
            return MemoryPriority.PERMANENT.value
        
        # Более гранулированное определение приоритета
        if importance_score >= 0.85: return MemoryPriority.CRITICAL.value
        elif importance_score >= 0.65: return MemoryPriority.HIGH.value
        elif importance_score >= 0.4: return MemoryPriority.MEDIUM.value
        else: return MemoryPriority.LOW.value

    @handle_errors(reraise_as=None)
    async def get_relevant_memories(self, user_id_tg: int, persona: str,
                                  current_context_text: str, limit: int = 5) -> List[Dict]:
        """Получает релевантные воспоминания для текущего контекста."""
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: return []
        
        # Получаем все активные (не истекшие) воспоминания пользователя для данной персоны
        # Лимит можно увеличить, чтобы было из чего выбирать, но не слишком большой для производительности
        all_active_memories = await self.db_service.get_memories(
            user_id=db_user.id, persona=persona, limit=100, # Увеличим немного выборку для анализа
            sort_by_priority_desc=True, # Сначала более приоритетные
            sort_by_last_accessed_desc=False # Сначала давно не использованные (или true для часто используемых)
        )
        if not all_active_memories: return []

        context_words = set(current_context_text.lower().split()) # TODO: Более продвинутый анализ контекста (NLP)
        
        scored_memories = []
        for db_memory_item in all_active_memories:
            context_relevance = self._calculate_context_relevance(db_memory_item, context_words)
            
            # Учитываем время с последнего доступа - чем давнее, тем менее релевантно (если не приоритетное)
            recency_factor = 1.0
            if db_memory_item.last_accessed and db_memory_item.priority < MemoryPriority.CRITICAL.value:
                days_since_access = (datetime.now(timezone.utc) - db_memory_item.last_accessed.replace(tzinfo=timezone.utc)).days
                recency_factor = max(0.1, 1.0 - (days_since_access / 60.0)) # Штраф за давность (макс через 2 месяца)

            # Финальный скор с учетом приоритета, релевантности контента и свежести
            final_score = (context_relevance * 0.5 + 
                           db_memory_item.relevance_score * 0.3 + # Используем сохраненный relevance_score (важность)
                           (db_memory_item.priority / MemoryPriority.PERMANENT.value) * 0.2 # Бонус за приоритет
                          ) * recency_factor
            
            if final_score > 0.15: # Порог релевантности
                scored_memories.append({
                    'id': db_memory_item.id, 
                    'content': db_memory_item.content,
                    'score': final_score, 
                    'created_at': db_memory_item.created_at.isoformat(),
                    'tags': db_memory_item.tags.split(',') if db_memory_item.tags else [],
                    'memory_type': db_memory_item.memory_type, 
                    'priority': db_memory_item.priority
                })
        
        scored_memories.sort(key=lambda x: x['score'], reverse=True)
        
        result_memories = []
        for item in scored_memories[:limit]: # Берем топ N
            await self.db_service.update_memory_access(item['id']) # Обновляем время доступа
            result_memories.append(item)
            
        logger.info(f"Found {len(result_memories)} relevant memories for user {user_id_tg} (persona: {persona})")
        return result_memories

    @handle_errors(reraise_as=DatabaseError)
    async def upgrade_memory_on_tier_change(
        self,
        user_id_tg: int,
        old_tier_str: str,
        new_tier_str: str
    ) -> Dict[str, Any]:
        """Обновляет параметры памяти пользователя при смене тарифа."""
        logger.info(f"Initiating memory upgrade check for user TG ID {user_id_tg} from tier '{old_tier_str}' to '{new_tier_str}'.")
        try:
            old_tier_enum = SubscriptionTier(old_tier_str)
            new_tier_enum = SubscriptionTier(new_tier_str)
        except ValueError as ve:
            logger.error(f"Invalid tier string during memory upgrade for user {user_id_tg}: {ve}", exc_info=True)
            return {"success": False, "message": f"Memory upgrade failed due to invalid tier: {str(ve)}"}

        if old_tier_enum == new_tier_enum:
            logger.info(f"Same tier renewal or no change for user {user_id_tg}: {old_tier_str}. No memory retention changes needed based on tier type.")
            return {"success": True, "message": "Same tier - no memory retention changes needed based on tier type."}

        user_db = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not user_db:
            logger.error(f"User not found for memory upgrade: TG ID {user_id_tg}")
            return {"success": False, "message": "User not found for memory upgrade."}

        old_limits: 'TierLimits' = self.subscription_service.plans.PLANS.get(old_tier_enum, self.subscription_service.plans.PLANS[SubscriptionTier.FREE])
        new_limits: 'TierLimits' = self.subscription_service.plans.PLANS.get(new_tier_enum, self.subscription_service.plans.PLANS[SubscriptionTier.FREE])

        upgrade_benefits = await self._analyze_memory_upgrade_benefits(old_limits, new_limits)
        upgrade_results = await self._apply_memory_upgrades(user_db.id, new_limits, upgrade_benefits)

        if upgrade_benefits.get("has_improvements") or upgrade_results.get("memories_extended_count", 0) > 0:
            await self._notify_user_about_memory_upgrade(
                user_id_tg, old_tier_str, new_tier_str, upgrade_benefits, upgrade_results
            )
        logger.info(f"Memory parameters considered for user {user_id_tg} on tier change: {old_tier_str} -> {new_tier_str}. Results: {upgrade_results}")
        return {"success": True, "message": "Memory parameters updated according to new tier.", "benefits_analyzed": upgrade_benefits, "results_applied": upgrade_results}

    async def _analyze_memory_upgrade_benefits(self, old_limits: 'TierLimits', new_limits: 'TierLimits') -> Dict[str, Any]:
        """Анализирует улучшения памяти при переходе на новый тариф."""
        benefits: Dict[str, Any] = {
            "has_improvements": False,
            "retention_days_increase": 0, 
            "old_retention_days": old_limits.memory_retention_days,
            "new_retention_days": new_limits.memory_retention_days,
            "old_memory_type": old_limits.memory_type,
            "new_memory_type": new_limits.memory_type,
            "capacity_increase": False, # Флаг увеличения лимита записей
            "old_max_entries": old_limits.max_memory_entries,
            "new_max_entries": new_limits.max_memory_entries,
            "quality_upgrade": False, # Флаг улучшения "качества" (пока по типу памяти)
            "new_features": [] # Список новых фич памяти (заглушка)
        }
        old_ret = old_limits.memory_retention_days; new_ret = new_limits.memory_retention_days
        if (new_ret == -1 and old_ret != -1) or \
           (new_ret != -1 and old_ret != -1 and new_ret > old_ret):
            benefits["has_improvements"] = True
            benefits["retention_days_increase"] = float('inf') if new_ret == -1 else (new_ret - old_ret)
        
        old_max = old_limits.max_memory_entries; new_max = new_limits.max_memory_entries
        if (new_max == -1 and old_max != -1) or \
           (new_max != -1 and old_max != -1 and new_max > old_max):
            benefits["has_improvements"] = True; benefits["capacity_increase"] = True
        
        # "Качество" пока оцениваем по типу памяти (permanent > long_term > short_term > session)
        memory_type_hierarchy = {MemoryType.SESSION.value: 0, MemoryType.SHORT_TERM.value: 1, MemoryType.LONG_TERM.value: 2, MemoryType.PERMANENT.value: 3}
        if memory_type_hierarchy.get(new_limits.memory_type, -1) > memory_type_hierarchy.get(old_limits.memory_type, -1):
            benefits["has_improvements"] = True; benefits["quality_upgrade"] = True
        
            
        logger.debug(f"Analyzed memory benefits: {benefits}")
        return benefits

    async def _apply_memory_upgrades(
        self, user_id_db: int, new_tier_limits: 'TierLimits', benefits_analyzed: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Применяет обновления памяти, в основном продление срока хранения."""
        results: Dict[str, Any] = {"memories_extended_count": 0, "settings_updated": False, "features_activated": []}
        try:
            new_retention_days = benefits_analyzed.get("new_retention_days")
            old_retention_days = benefits_analyzed.get("old_retention_days")
            should_extend_retention = (new_retention_days == -1 and old_retention_days != -1) or \
                                    (new_retention_days is not None and old_retention_days is not None and 
                                     new_retention_days != -1 and old_retention_days != -1 and 
                                     new_retention_days > old_retention_days)

            if should_extend_retention:
                new_calculated_expires_at = self._calculate_expiration_date(
                    MemoryType(new_tier_limits.memory_type), new_tier_limits.memory_retention_days
                )
                extended_count = await self.db_service.update_all_user_memories_expiration(
                    user_id_db=user_id_db, new_expires_at=new_calculated_expires_at, only_if_longer=True
                )
                results["memories_extended_count"] = extended_count
                logger.info(f"{extended_count} existing memories' expiration updated for user DB ID {user_id_db} to {new_calculated_expires_at or 'permanent'}.")
            else:
                logger.info(f"No retention extension needed or applicable for user DB ID {user_id_db} based on tier change analysis.")
            
            # Обновление общих настроек памяти пользователя (если они есть и зависят от тарифа)
            # await self._update_user_general_memory_settings(user_id_db, new_tier_limits)
            # results["settings_updated"] = True # Если что-то обновили

            # Активация новых фич (заглушка)
            # if benefits_analyzed.get("new_features"):
            #     for feature_name in benefits_analyzed["new_features"]:
            #         await self._activate_specific_memory_feature(user_id_db, feature_name)
            #         results["features_activated"].append(feature_name)
            
            # Оптимизация существующих воспоминаний (заглушка)
            # if benefits_analyzed.get("quality_upgrade"):
            #    await self._optimize_existing_memories_for_quality(user_id_db, new_tier_limits.memory_type)

        except Exception as e:
            logger.error(f"Error applying memory upgrades for user DB ID {user_id_db}: {e}", exc_info=True)
            results["error"] = str(e)
        return results

    async def _notify_user_about_memory_upgrade(
        self, user_id_tg: int, old_tier_str: str, new_tier_str: str,
        benefits: Dict[str, Any], results: Dict[str, Any]
    ):
        """Отправляет пользователю уведомление об улучшениях памяти."""
        if not self.bot_instance or not hasattr(self.bot_instance, 'bot'):
            logger.error("Cannot send memory upgrade notification: bot_instance or bot_instance.bot is not available."); return

        notification_parts = []
        new_tier_display_name = self.subscription_service._get_tier_name(new_tier_str)
        old_tier_display_name = self.subscription_service._get_tier_name(old_tier_str)

        if benefits.get("has_improvements"):
            notification_parts.append(f"🎉 Поздравляем с переходом на тариф «{new_tier_display_name}»! Ваша Память улучшена:")

            if benefits.get("retention_days_increase", 0) > 0 or \
               (benefits.get("new_retention_days") == -1 and benefits.get("old_retention_days") != -1) :
                if benefits.get("new_retention_days") == -1:
                    notification_parts.append(f"  💾 Теперь ваши воспоминания хранятся **постоянно**!")
                else:
                    notification_parts.append(f"  💾 Срок хранения воспоминаний увеличен до **{benefits['new_retention_days']} дней**.")
                if results.get("memories_extended_count", 0) > 0:
                    notification_parts.append(f"     (для {results['memories_extended_count']} существующих записей срок продлен)")
            
            if benefits.get("capacity_increase"):
                new_max_entries_display = "безлимитного" if benefits.get("new_max_entries") == -1 else str(benefits.get("new_max_entries"))
                notification_parts.append(f"  🗂️ Лимит записей увеличен до **{new_max_entries_display}**.")

            if benefits.get("quality_upgrade"):
                notification_parts.append(f"  ✨ Качество обработки и тип хранения памяти улучшены до уровня «{benefits.get('new_memory_type','N/A').replace('_','-').title()}».")
            
            # if benefits.get("new_features"): # Заглушка
            #     notification_parts.append(f"  💡 Разблокированы новые функции памяти: {', '.join(benefits['new_features'])}.")
            
            notification_parts.append(f"\n🚀 Все улучшения уже активны!")
        else: # Если улучшений нет (например, даунгрейд или переход на эквивалентный по памяти тариф)
            notification_parts.append(f"ℹ️ Вы перешли с тарифа «{old_tier_display_name}» на «{new_tier_display_name}». Параметры вашей Памяти были соответствующим образом скорректированы.")


        if not notification_parts: return
        final_notification_text = "\n".join(notification_parts)
        try:
            await self.bot_instance.bot.send_message(user_id_tg, final_notification_text, parse_mode="Markdown")
            logger.info(f"Уведомление об улучшении/изменении памяти отправлено пользователю {user_id_tg}.")
        except Exception as e:
            logger.error(f"Error sending memory upgrade notification to user {user_id_tg}: {e}", exc_info=True)


    def _calculate_expiration_date(self, memory_storage_type: MemoryType, retention_days: int) -> Optional[datetime]:
        """Рассчитывает дату истечения на основе типа хранилища и срока хранения."""
        if retention_days == -1: # Перманентное хранение
            return None 
        elif retention_days == 0: # Сессионное (например, истекает через 1 день для простоты)
            return datetime.now(timezone.utc) + timedelta(days=1) 
        else: # Конкретное количество дней
            return datetime.now(timezone.utc) + timedelta(days=retention_days)

    async def _calculate_memory_importance(self, content: str, memory_content_type_str: str, **kwargs) -> float:
        """Рассчитывает "важность" воспоминания на основе его контента и метаданных."""
        importance = 0.3 # Базовая важность
        content_lower = content.lower()

        # Анализ по ключевым словам
        for category, keywords in self._emotional_keywords.items():
            if any(word in content_lower for word in keywords):
                if category in self._importance_weights: # Если для категории есть вес
                    importance += self._importance_weights.get(category, 0)
                elif category.endswith("_markers"): # Для маркеров типов контента
                     pass # Не добавляем вес напрямую, это для определения типа
                else: # Общий вес для эмоциональных ключевых слов
                    importance += self._importance_weights.get('emotional_keywords', 0.3) * 0.1 # Небольшой бонус

        # Бонус за тип контента
        try:
            mem_type_enum = MemoryType(memory_content_type_str)
            if mem_type_enum == MemoryType.PREFERENCE: importance += 0.15
            elif mem_type_enum == MemoryType.INSIGHT: importance += 0.20
            elif mem_type_enum == MemoryType.USER_FACT: importance += 0.15
            elif mem_type_enum == MemoryType.INTIMATE: importance += 0.10
            elif mem_type_enum == MemoryType.EVENT and "важно" in content_lower : importance += 0.05 # Если важное событие
        except ValueError:
            pass # Неизвестный тип контента

        # Явные указания пользователя
        if kwargs.get('user_marked_important', False):
            importance += self._importance_weights.get('user_marked_important', 0.4)
        if kwargs.get('explicit_request_to_remember', False):
            importance += self._importance_weights.get('explicit_request_to_remember', 0.35)
        
        # Длина контента (очень длинные могут быть менее важны для быстрого доступа, или наоборот)
        if len(content) > 200: importance += 0.05
        if len(content) < 30 : importance -= 0.05 # Короткие, менее информативные

        return min(max(importance, 0.0), 1.0) # Нормализуем 0.0-1.0

    def _calculate_context_relevance(self, db_memory_item: DBMemory, context_words: set) -> float:
        """Рассчитывает релевантность воспоминания текущему контексту (упрощенно)."""
        memory_words = set(db_memory_item.content.lower().split())
        intersection = len(context_words & memory_words)
        union = len(context_words | memory_words)
        word_similarity = (intersection / union) if union > 0 else 0.0
        
        tag_bonus = 0.0
        if db_memory_item.tags:
            memory_tags_set = set(tag.strip().lower() for tag in db_memory_item.tags.split(','))
            if memory_tags_set:
                tag_intersection = len(context_words & memory_tags_set)
                # Бонус зависит от количества совпадающих тегов и общего количества тегов у воспоминания
                tag_bonus = (tag_intersection / len(memory_tags_set)) * 0.3 if len(memory_tags_set) > 0 else 0.0
        
        # Упрощенный расчет, можно добавить TF-IDF или другие методы для более точной оценки
        relevance = word_similarity * 0.7 + tag_bonus * 0.3
        return min(max(relevance, 0.0), 1.0)

    async def _cleanup_expired_memories(self, user_id_db: int, persona: Optional[str] = None):
        """Удаляет истекшие воспоминания для пользователя."""
        now_utc = datetime.now(timezone.utc)
        # Получаем только ID истекших воспоминаний для эффективности
        expired_memories_ids = await self.db_service.get_expired_memories_ids(user_id_db, persona, now_utc) # Новый метод в DBService
        
        deleted_count = 0
        if expired_memories_ids:
            for mem_id in expired_memories_ids:
                if await self.db_service.delete_memory(mem_id):
                    deleted_count += 1
        if deleted_count > 0:
            logger.info(f"Удалено {deleted_count} устаревших воспоминаний для пользователя ID {user_id_db}, персона: {persona or 'all'}")

    async def _cleanup_low_priority_memories(self, user_id_db: int, limit_to_enforce: int, persona: Optional[str] = None):
        """Удаляет воспоминания с низким приоритетом, если превышен лимит записей."""
        if limit_to_enforce == -1: return # Безлимит

        # Получаем воспоминания, отсортированные по приоритету (возр) и дате создания (возр)
        # Загружаем чуть больше лимита, чтобы было что удалять
        memories_to_consider = await self.db_service.get_memories(
            user_id=user_id_db, persona=persona if persona else "",
            limit=limit_to_enforce + 50, # Загружаем с запасом
            sort_by_priority_asc=True, # Сначала самые низкоприоритетные
            sort_by_created_at_asc=True  # Среди них - самые старые
        )
        
        current_active_count = await self.db_service.get_active_memory_count_for_user(user_id_db, persona) # Точный подсчет
        
        if current_active_count > limit_to_enforce:
            num_to_delete = current_active_count - limit_to_enforce
            deleted_ids = []
            # Удаляем из списка memories_to_consider, который уже отсортирован как надо
            for mem_to_del in memories_to_consider:
                if len(deleted_ids) >= num_to_delete: break # Удалили достаточно
                if mem_to_del.priority == MemoryPriority.PERMANENT.value: continue # Перманентные не трогаем
                
                if mem_to_del.id is not None: 
                    if await self.db_service.delete_memory(mem_to_del.id):
                        deleted_ids.append(mem_to_del.id)
            
            if deleted_ids:
                logger.info(f"Удалено {len(deleted_ids)} воспоминаний с низким приоритетом для user ID {user_id_db} (персона: {persona or 'all'}) для соблюдения лимита {limit_to_enforce}. IDs: {deleted_ids}")

    async def get_memory_stats(self, user_id_tg: int) -> Dict[str, Any]:
        """Возвращает статистику по памяти пользователя."""
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: return {"error": "User not found"}
        
        subscription = await self.subscription_service.get_user_subscription(user_id_tg)
        current_tier_value_str = subscription.get("tier", SubscriptionTier.FREE.value)
        try: current_tier_enum = SubscriptionTier(current_tier_value_str)
        except ValueError: current_tier_enum = SubscriptionTier.FREE
        
        sub_limits: 'TierLimits' = self.subscription_service.plans.PLANS.get(
            current_tier_enum, self.subscription_service.plans.PLANS[SubscriptionTier.FREE]
        )
        current_memory_storage_type = MemoryType(sub_limits.memory_type)
        max_entries = sub_limits.max_memory_entries
        retention_days = sub_limits.memory_retention_days
        
        # Используем новый метод для точного подсчета
        total_active_memories_count = await self.db_service.get_active_memory_count_for_user(db_user.id)
        
        # Для детальной статистики по типам и приоритетам, если нужно, можно загрузить все
        # Но для общего обзора достаточно и подсчета.
        # Если нужна детализация, то:
        # all_user_active_memories = await self.db_service.get_memories(user_id=db_user.id, limit=1_000_000)
        # ... (дальнейший анализ all_user_active_memories)
        # Пока оставим упрощенно, без загрузки всех воспоминаний для статистики.
        
        type_counts: Dict[str, int] = await self.db_service.get_memory_type_distribution(db_user.id) # Новый метод в DBService
        priority_counts: Dict[int, int] = await self.db_service.get_memory_priority_distribution(db_user.id) # Новый метод в DBService
        avg_emotional_weight, total_accesses = await self.db_service.get_memory_aggregate_stats(db_user.id) # Новый метод

        return {
            "current_storage_type": current_memory_storage_type.value,
            "storage_type_description": self._get_memory_storage_type_description(current_memory_storage_type),
            "total_active_memories": total_active_memories_count,
            "max_entries_limit": "Безлимит" if max_entries == -1 else max_entries,
            "usage_percentage": (total_active_memories_count / max_entries * 100) if max_entries != -1 and max_entries > 0 and total_active_memories_count > 0 else (0 if max_entries != -1 else 100),
            "is_unlimited_entries": max_entries == -1,
            "retention_days_display": "Постоянно" if retention_days == -1 else (f"{retention_days} дн." if retention_days > 0 else "Сессия"),
            "is_permanent_retention": retention_days == -1,
            "content_type_breakdown": type_counts,
            "priority_breakdown": {MemoryPriority(k).name if k in MemoryPriority._value2member_map_ else f"UNKNOWN_{k}":v for k,v in priority_counts.items()},
            "avg_emotional_weight": round(avg_emotional_weight, 2),
            "total_accesses": total_accesses,
        }

    def _get_memory_storage_type_description(self, memory_storage_type: MemoryType) -> str:
        """Возвращает описание типа хранения памяти."""
        descriptions = {
            MemoryType.SESSION: "Сессионная (хранение ограничено текущим днем или несколькими часами)",
            MemoryType.SHORT_TERM: "Краткосрочная (хранение несколько дней)",
            MemoryType.LONG_TERM: "Долгосрочная (хранение до нескольких месяцев)",
            MemoryType.PERMANENT: "Постоянная (без срока истечения)"
        }
        return descriptions.get(memory_storage_type, "Неизвестный тип хранения")

    async def extract_memories_from_conversation(self,
                                               messages: List[Dict], # Список словарей сообщений
                                               user_id_tg: int,
                                               current_persona: str # Добавлена текущая персона
                                               ) -> List[Dict]:
        """Извлекает потенциальные воспоминания из истории диалога."""
        # db_user = await self.db_service.get_user_by_telegram_id(user_id_tg) # get_or_create не нужен здесь
        # if not db_user:
        #     logger.error(f"User TG ID {user_id_tg} not found for memory extraction.")
        #     return []
        
        memories_to_save_candidates = []
        for message_dict in messages: # message теперь словарь
            if message_dict.get("role") != "user": continue # Интересуют только сообщения пользователя
            
            content = message_dict.get("content", "")
            if len(content) < 15: continue # Слишком короткие сообщения вряд ли содержат ценную память
            
            # Анализ потенциала сообщения для сохранения в память
            memory_potential = await self._analyze_memory_potential(content)
            
            if memory_potential.get("should_save", False):
                candidate_data = {
                    "user_id_tg": user_id_tg, # Для передачи в save_memory
                    "persona": current_persona, # Используем текущую персону диалога
                    "content": content,
                    "memory_content_type": MemoryType(memory_potential.get("type", MemoryType.GENERAL.value)), # Enum
                    "tags": memory_potential.get("tags", []),
                    "relevance_score_override": memory_potential.get("importance", 0.5), # Используем importance как relevance
                    "emotional_weight_override": memory_potential.get("importance", 0.5), # И как emotional_weight
                    "context": f"Из диалога с {current_persona} от {message_dict.get('timestamp', datetime.now(timezone.utc).isoformat())}",
                    "explicit_request_to_remember": memory_potential.get("explicit_request", False)
                }
                memories_to_save_candidates.append(candidate_data)
                
        logger.info(f"Извлечено {len(memories_to_save_candidates)} кандидатов в воспоминания для user_id_tg {user_id_tg}")
        return memories_to_save_candidates

    async def _analyze_memory_potential(self, content: str) -> Dict[str, Any]:
        """Анализирует текст на предмет потенциала для сохранения в память."""
        content_lower = content.lower()
        importance = 0.1 # Базовая важность, если ничего не найдено
        memory_content_type_val = MemoryType.GENERAL.value
        tags = set()
        explicit_request = False

        if any(phrase in content_lower for phrase in self._emotional_keywords.get("important", []) + ["запомни это", "сохрани это", "не забудь", "напомни мне"]):
            importance += self._importance_weights.get('explicit_request_to_remember', 0.35)
            tags.add("explicit_request")
            explicit_request = True
            if memory_content_type_val == MemoryType.GENERAL.value : memory_content_type_val = MemoryType.INSIGHT.value # Если явно просят запомнить, вероятно это инсайт

        # Определение типа контента и базовой важности по маркерам
        type_priority_map = { # (Тип, Приоритет определения типа)
            MemoryType.PREFERENCE.value: 3, MemoryType.USER_FACT.value: 3,
            MemoryType.INTIMATE.value: 3, MemoryType.INSIGHT.value: 4,
            MemoryType.EVENT.value: 2, MemoryType.EMOTION.value: 2
        }
        current_type_priority_score = 0

        for marker_category, type_to_set_val in [
            ("preference_markers", MemoryType.PREFERENCE.value),
            ("user_fact_markers", MemoryType.USER_FACT.value),
            ("event_markers", MemoryType.EVENT.value),
            ("insight_markers", MemoryType.INSIGHT.value),
            ("intimate", MemoryType.INTIMATE.value) # Используем категорию из emotional_keywords
        ]:
            if any(marker in content_lower for marker in self._emotional_keywords.get(marker_category, [])):
                if type_priority_map.get(type_to_set_val, 0) > current_type_priority_score:
                    memory_content_type_val = type_to_set_val
                    current_type_priority_score = type_priority_map.get(type_to_set_val,0)
                tags.add(type_to_set_val.lower()) # Добавляем тег по типу контента
                importance += self._importance_weights.get(marker_category.split('_')[0], 0.1) # Общий вес для категории

        # Эмоциональная окраска (добавляет к важности и может уточнить тип EMOTION)
        for category, keywords in self._emotional_keywords.items():
            if category.endswith("_markers") or category == "important": continue # Уже обработаны или для другого
            if any(word in content_lower for word in keywords):
                importance += self._importance_weights.get('emotional_keywords', 0.3) * 0.15 # Бонус за эмоцию
                tags.add(category)
                if current_type_priority_score < type_priority_map.get(MemoryType.EMOTION.value,0) : # Если не определен более специфичный тип
                    memory_content_type_val = MemoryType.EMOTION.value
        
        # Длина контента
        if len(content) > 150: importance += 0.05
        if len(content) > 300: importance += 0.05
        if len(content) < 25 : importance -= 0.1 # Короткие менее важны, если не явный запрос

        # Порог для сохранения (можно вынести в конфиг)
        # Если был явный запрос запомнить, сохраняем с более низким порогом важности
        threshold_to_save = 0.25 if explicit_request else 0.40 
        should_save = importance >= threshold_to_save
        
        return {
            "should_save": should_save, 
            "importance": min(max(importance, 0.0), 1.0), # Нормализуем 0.0-1.0
            "type": memory_content_type_val, 
            "tags": list(tags),
            "explicit_request": explicit_request
        }

    async def search_memories(self, user_id_tg: int, query_text: str, persona: str = "",
                            limit: int = 5) -> List[Dict]:
        """Ищет воспоминания по текстовому запросу."""
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: return []
        
        # Используем метод DatabaseService, который поддерживает полнотекстовый поиск (если реализован)
        # или поиск по icontains.
        found_db_memories = await self.db_service.get_memories(
            user_id=db_user.id, 
            persona=persona, 
            query=query_text, # Передаем текст запроса
            limit=limit,
            sort_by_relevance_desc=True # Предполагаем, что БД может сортировать по релевантности (например, если используется FTS)
                                      # Если нет, сортировка будет в get_relevant_memories
        )
        results = []
        for db_mem in found_db_memories:
            results.append({
                "id": db_mem.id, "content": db_mem.content,
                "relevance_score": db_mem.relevance_score, # Это важность, а не релевантность к запросу
                "created_at": db_mem.created_at.isoformat(),
                "memory_content_type": db_mem.memory_type,
                "tags": db_mem.tags.split(',') if db_mem.tags else [],
                "priority": db_mem.priority
            })
            # Можно добавить обновление last_accessed для найденных воспоминаний
            await self.db_service.update_memory_access(db_mem.id)

        logger.info(f"Поиск по запросу '{query_text}' для user_id_tg {user_id_tg} дал {len(results)} результатов.")
        return results

    async def delete_memory_by_id(self, user_id_tg: int, memory_id: int) -> bool:
        """Удаляет воспоминание по ID, проверяя принадлежность пользователю."""
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user:
            logger.warning(f"Попытка удаления воспоминания для несуществующего пользователя TG ID {user_id_tg}")
            return False
        
        memory_to_delete = await self.db_service.get_memory_by_id(memory_id)
        if not memory_to_delete or memory_to_delete.user_id != db_user.id:
            logger.warning(f"Попытка удаления чужого или несуществующего воспоминания ID {memory_id} пользователем TG ID {user_id_tg}")
            return False
        
        try:
            deleted_success = await self.db_service.delete_memory(memory_id)
            if deleted_success:
                logger.info(f"Удалено воспоминание ID {memory_id} пользователя TG ID {user_id_tg}")
            return deleted_success
        except Exception as e:
            logger.error(f"Ошибка удаления воспоминания ID {memory_id} для пользователя TG ID {user_id_tg}: {e}", exc_info=True)
            return False

    async def get_memory_insights(self, user_id_tg: int) -> Dict[str, Any]:
        """Генерирует AI-инсайты на основе воспоминаний пользователя."""
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: return {"error": "User not found"}
        
        # Проверка доступа к фиче AI-инсайтов
        insights_access = await self.subscription_service.check_feature_access(user_id_tg, "ai_insights_access")
        if not insights_access.get("allowed", False):
            logger.info(f"User {user_id_tg} does not have access to AI insights based on their tier.")
            return {
                "error": "AI insights not available on your current subscription tier.",
                "upgrade_required": True,
                "message": "Функция AI-инсайтов доступна на более высоких тарифах. Обновите подписку для доступа.",
                "available_in_tiers": insights_access.get("available_in_tiers", [])
            }
            
        all_user_memories = await self.db_service.get_memories(user_id=db_user.id, persona="", limit=500) # Лимит для анализа
        if not all_user_memories or len(all_user_memories) < 5 : # Минимальное количество воспоминаний для анализа
            return {"message": "Накопите больше воспоминаний (хотя бы 5) для глубокого анализа."}

        type_analysis: Dict[str, int] = await self.db_service.get_memory_type_distribution(db_user.id)
        emotion_tags_analysis: Dict[str, int] = {} # Заполним на основе тегов
        topic_keywords: Dict[str, int] = {} # Анализ ключевых слов

        for mem_item in all_user_memories:
            if mem_item.tags:
                for tag in mem_item.tags.split(','):
                    # Собираем статистику по тегам, которые могут указывать на эмоции
                    if tag in self._emotional_keywords: # Если тег - это категория эмоций
                        emotion_tags_analysis[tag] = emotion_tags_analysis.get(tag, 0) + 1
            # Простой подсчет слов для тем (можно улучшить с NLP)
            for word in mem_item.content.lower().split():
                if len(word) > 4 and word.isalpha() and word not in ["это", "который", "потому", "чтобы", "также"]: # Исключаем стоп-слова
                    topic_keywords[word] = topic_keywords.get(word, 0) + 1
        
        sorted_topics = sorted(topic_keywords.items(), key=lambda item: item[1], reverse=True)[:7] # Топ-7 тем
        
        insights = {
            "total_memories_analyzed": len(all_user_memories),
            "memory_content_types_distribution": type_analysis,
            "emotional_tags_profile": {k:v for k,v in emotion_tags_analysis.items() if v > 0},
            "top_recurring_topics": [{"topic": t[0], "count": t[1]} for t in sorted_topics],
            "behavioral_patterns": self._generate_behavior_patterns(all_user_memories), # Использует created_at
            "personalized_recommendations": self._generate_recommendations(type_analysis, emotion_tags_analysis)
        }
        logger.info(f"Сгенерированы AI-инсайты для пользователя {user_id_tg}.")
        return insights

    def _generate_behavior_patterns(self, memories: List[DBMemory]) -> List[str]:
        """Генерирует возможные паттерны поведения на основе времени создания воспоминаний."""
        patterns = []
        if not memories: return patterns
        
        time_periods = {"утро (06-12)": 0, "день (12-18)": 0, "вечер (18-00)": 0, "ночь (00-06)": 0}
        for memory in memories:
            if not memory.created_at: continue # Пропускаем, если нет даты создания
            # Убедимся, что created_at это datetime объект
            created_at_dt = memory.created_at
            if isinstance(created_at_dt, str): # На случай, если из какого-то старого формата пришла строка
                try: created_at_dt = datetime.fromisoformat(created_at_dt.replace('Z','+00:00'))
                except ValueError: continue
            if created_at_dt.tzinfo is None: created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
            
            hour = created_at_dt.astimezone(timezone(timedelta(hours=self.bot_instance.config.user_local_tz_offset_hours if self.bot_instance else 0))).hour # Примерное локальное время

            if 6 <= hour < 12: time_periods["утро (06-12)"] += 1
            elif 12 <= hour < 18: time_periods["день (12-18)"] += 1
            elif 18 <= hour < 24: time_periods["вечер (18-00)"] += 1
            else: time_periods["ночь (00-06)"] += 1
            
        if any(time_periods.values()): # Если есть хоть какие-то данные
            most_active_period = max(time_periods, key=time_periods.get) # type: ignore
            if time_periods[most_active_period] > len(memories) * 0.25: # Если более 25% в один период
                 patterns.append(f"Вы наиболее склонны сохранять или обсуждать важные моменты в {most_active_period}.")
        
        type_counts: Dict[str, int] = {}
        for mem_item in memories: type_counts[mem_item.memory_type] = type_counts.get(mem_item.memory_type, 0) + 1
        if type_counts and len(memories) > 0 : # Проверка, что есть воспоминания
            dominant_type = max(type_counts, key=type_counts.get) # type: ignore
            if type_counts[dominant_type] > len(memories) * 0.3: # Если более 30% одного типа
                 patterns.append(f"Чаще всего вы сохраняете воспоминания типа: {MemoryType(dominant_type).name.replace('_',' ').title()}.")
        
        if not patterns:
            patterns.append("Пока не удалось выявить четких паттернов. Продолжайте делиться моментами!")
        return patterns

    def _generate_recommendations(self, type_analysis: Dict[str, int], emotion_analysis: Dict[str, int]) -> List[str]:
        """Генерирует персональные рекомендации на основе анализа типов и эмоций."""
        recommendations = []
        if type_analysis.get(MemoryType.PREFERENCE.value, 0) < 2 and \
           type_analysis.get(MemoryType.USER_FACT.value, 0) < 2 :
            recommendations.append("💡 Расскажите больше о своих предпочтениях и фактах о себе, чтобы я лучше вас понимала.")
        
        negative_emotions_sum = emotion_analysis.get("negative",0) + emotion_analysis.get("fear",0)
        positive_emotions_sum = emotion_analysis.get("positive",0) + emotion_analysis.get("surprise",0)
        if negative_emotions_sum > positive_emotions_sum * 1.2 and negative_emotions_sum > 2 : # Если негативных значительно больше
            recommendations.append("🌟 Попробуйте фокусироваться и сохранять больше позитивных моментов. Это может улучшить ваше настроение!")
        
        if type_analysis.get(MemoryType.INSIGHT.value, 0) > 3:
            recommendations.append("🔍 Вы часто делитесь глубокими мыслями. Продолжайте эту практику саморефлексии!")
        elif type_analysis.get(MemoryType.INSIGHT.value, 0) < 1 and type_analysis.get(MemoryType.EVENT.value, 0) > 3:
            recommendations.append("✍️ После интересных событий, попробуйте фиксировать свои выводы или озарения. Это помогает лучше понять себя.")
            
        if not recommendations:
            recommendations.append("Продолжайте использовать память, чтобы я могла давать более точные рекомендации.")
        return recommendations

    async def _activate_specific_memory_feature(self, user_id_db: int, feature_name: str):
        """ЗАГЛУШКА: Активирует специфическую функцию памяти для пользователя."""
        # Например, установка флага в UserPreference
        # await self.db_service.update_user_preference(user_id_db, f"memory_feature_{feature_name}_enabled", True, persona="system")
        logger.info(f"ЗАГЛУШКА: Активация функции памяти '{feature_name}' для user_id_db {user_id_db}.")
        pass

    async def _optimize_existing_memories_for_quality(self, user_id_db: int, new_memory_quality_type: str):
        """ЗАГЛУШКА: Оптимизирует существующие воспоминания под новый уровень качества."""
        # Это может включать:
        # - Переиндексацию для семантического поиска
        # - Автоматическое добавление тегов или категорий
        # - Обогащение контекстом
        logger.info(f"ЗАГЛУШКА: Оптимизация существующих воспоминаний для user_id_db {user_id_db} под качество '{new_memory_quality_type}'.")
        pass
