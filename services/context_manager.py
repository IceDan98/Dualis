import logging
from typing import List, Dict, Optional, Callable, Any 
from dataclasses import dataclass, field 
from datetime import datetime, timezone 

# Импорты из вашего проекта
from database.operations import DatabaseService
from database.models import ContextSummary as DBContextSummary # Модель SQLAlchemy для саммари

# LLMService будет передан как аргумент в нужные методы
# from .llm_service import LLMService 

logger = logging.getLogger(__name__)

@dataclass
class Message:
    """Представление сообщения в контексте"""
    role: str  
    content: str
    timestamp: datetime 
    persona: str = "diana" 
    metadata: Dict[str, Any] = field(default_factory=dict) 

@dataclass
class ContextSummaryData: # Переименовано из ContextSummary, чтобы не конфликтовать с моделью БД
    """Представление данных суммаризации для использования в логике"""
    summary: str
    message_count: int
    summary_period_start_at: datetime # Согласовано с моделью БД
    summary_period_end_at: datetime   # Согласовано с моделью БД
    persona: str 

class ContextManager:
    """Менеджер контекста для работы с историей диалога"""
    
    def __init__(self, 
                 db_service: DatabaseService, # Добавлен db_service
                 max_messages_in_context: int = 20, 
                 summary_creation_threshold: int = 30, 
                 max_tokens_for_llm: int = 3800 
                ):
        self.db_service = db_service # Сохраняем экземпляр DatabaseService
        self.max_messages_in_context = max_messages_in_context 
        self.summary_creation_threshold = summary_creation_threshold 
        self.max_tokens_for_llm = max_tokens_for_llm 
        
        # self._summaries больше не используется для хранения в памяти
        
        logger.info(
            f"ContextManager инициализирован с DBService: max_messages={self.max_messages_in_context}, "
            f"summary_threshold={self.summary_creation_threshold}, max_tokens={self.max_tokens_for_llm}"
        )
    
    async def prepare_context_for_llm(self, 
                       user_id_db: int, # Добавлен user_id_db для получения саммари
                       raw_db_messages: List[Dict], 
                       current_persona: str,
                       token_counter_func: Callable[[str, str], int], 
                       relevant_memories: Optional[List[str]] = None
                       ) -> List[Dict]: 
        """
        Подготавливает контекст для LLM, включая саммари из БД.
        """
        formatted_messages: List[Message] = [] 
        try:
            formatted_messages = self._format_db_messages_to_message_objects(raw_db_messages)
            
            # Загружаем саммари из БД
            context_with_summaries = await self._add_summaries_to_context_from_db(
                formatted_messages, user_id_db, current_persona
            )
            
            context_with_memories = self._inject_memories_into_context(
                context_with_summaries, relevant_memories, current_persona
            )
            
            windowed_context_objects = self._apply_sliding_window_to_messages(context_with_memories)
            
            llm_formatted_messages = self._convert_message_objects_to_llm_dicts(windowed_context_objects)
            optimized_llm_messages = self._optimize_context_by_tokens(
                llm_formatted_messages, 
                token_counter_func, 
                model_name='gemini' 
            )
            
            logger.info(f"Контекст для LLM (user_id_db: {user_id_db}, персона: {current_persona}) подготовлен: {len(optimized_llm_messages)} сообщений.")
            return optimized_llm_messages
            
        except Exception as e:
            logger.error(f"Ошибка подготовки контекста для LLM (user_id_db: {user_id_db}, персона: {current_persona}): {e}", exc_info=True)
            if not formatted_messages and raw_db_messages: 
                try:
                    fallback_raw_messages = raw_db_messages[-5:]
                    fallback_formatted_messages = self._format_db_messages_to_message_objects(fallback_raw_messages)
                    return self._convert_message_objects_to_llm_dicts(
                        self._apply_sliding_window_to_messages(fallback_formatted_messages)
                    )
                except Exception as fallback_e:
                    logger.error(f"Ошибка при создании fallback-контекста: {fallback_e}")
                    return [{"role": "user", "content": "Произошла ошибка при подготовке контекста. Пожалуйста, попробуйте снова."}]
            elif formatted_messages: 
                 return self._convert_message_objects_to_llm_dicts(
                    self._apply_sliding_window_to_messages(formatted_messages[-5:])
                )
            else: 
                return [{"role": "user", "content": "Нет доступной истории для формирования контекста."}]
    
    def _format_db_messages_to_message_objects(self, db_messages: List[Dict]) -> List[Message]:
        """Преобразует сообщения из БД (словари) в объекты Message."""
        formatted_msg_objects = []
        for msg_data in db_messages:
            try:
                timestamp_val = msg_data.get('timestamp', msg_data.get('created_at'))
                if timestamp_val is None: 
                    timestamp_dt = datetime.now(timezone.utc)
                    logger.warning(f"Отсутствует timestamp и created_at для сообщения: {msg_data.get('id', 'N/A')}. Используется текущее время.")
                elif isinstance(timestamp_val, str):
                    timestamp_dt = datetime.fromisoformat(timestamp_val.replace('Z', '+00:00'))
                elif isinstance(timestamp_val, datetime):
                    timestamp_dt = timestamp_val
                else: 
                    timestamp_dt = datetime.now(timezone.utc)
                    logger.warning(f"Некорректный тип timestamp ({type(timestamp_val)}) для сообщения: {msg_data.get('id', 'N/A')}. Используется текущее время.")

                if timestamp_dt.tzinfo is None:
                    timestamp_dt = timestamp_dt.replace(tzinfo=timezone.utc)

                formatted_msg_objects.append(Message(
                    role=msg_data.get('role', 'user'), 
                    content=str(msg_data.get('content', '')), 
                    timestamp=timestamp_dt,
                    persona=msg_data.get('persona', 'diana'), 
                    metadata=msg_data.get('metadata', {})    
                ))
            except Exception as e:
                logger.warning(f"Ошибка форматирования сообщения из БД: {msg_data}. Ошибка: {e}")
                continue 
        return formatted_msg_objects
    
    async def _add_summaries_to_context_from_db(self, messages: List[Message], user_id_db: int, current_persona: str) -> List[Message]:
        """Добавляет наиболее релевантные суммаризации из БД в начало контекста."""
        try:
            # Загружаем последние N суммаризаций из БД
            num_summaries_to_load = 1 # Количество саммари для добавления в контекст
            db_summaries: List[DBContextSummary] = await self.db_service.get_latest_context_summaries(
                user_id_db, current_persona, limit=num_summaries_to_load
            )

            if not db_summaries:
                return messages
            
            summary_message_objects: List[Message] = []
            for db_summary_item in db_summaries: # db_summaries уже отсортированы по убыванию даты окончания
                summary_message_objects.append(Message(
                    role='system', 
                    content=f"[Краткое содержание предыдущего диалога от {db_summary_item.summary_period_start_at.strftime('%Y-%m-%d %H:%M')}]\n{db_summary_item.summary_text}",
                    timestamp=db_summary_item.summary_period_end_at, # Используем время окончания саммари
                    persona=current_persona,
                    metadata={'type': 'context_summary_from_db', 'original_message_count': db_summary_item.message_count}
                ))
            
            # Добавляем саммари в начало списка сообщений (самые релевантные/последние - первыми)
            return summary_message_objects + messages
        except Exception as e:
            logger.error(f"Ошибка при добавлении суммаризаций из БД для user_id_db {user_id_db}, persona {current_persona}: {e}", exc_info=True)
            return messages # Возвращаем оригинальные сообщения в случае ошибки
    
    def _inject_memories_into_context(self, messages: List[Message], relevant_memories: Optional[List[str]], current_persona: str) -> List[Message]:
        """Добавляет релевантные воспоминания в контекст как системное сообщение."""
        if not relevant_memories:
            return messages
        
        memories_text_block = "[Важные факты и воспоминания для учета в ответе]\n"
        for i, memory_text in enumerate(relevant_memories[:3], 1): 
            memories_text_block += f"Факт {i}: {memory_text}\n"
        
        memory_message_object = Message(
            role='system', 
            content=memories_text_block.strip(),
            timestamp=datetime.now(timezone.utc), 
            persona=current_persona,
            metadata={'type': 'injected_memories', 'count': len(relevant_memories[:3])}
        )
        
        first_non_system_idx = 0
        for idx, msg in enumerate(messages):
            if msg.role != 'system':
                first_non_system_idx = idx
                break
        else: 
            first_non_system_idx = len(messages)
            
        messages.insert(first_non_system_idx, memory_message_object)
        return messages
    
    def _apply_sliding_window_to_messages(self, messages: List[Message]) -> List[Message]:
        """Применяет скользящее окно к сообщениям пользователя/ассистента, сохраняя системные сообщения."""
        system_message_objects: List[Message] = []
        dialogue_message_objects: List[Message] = []
        
        for msg_obj in messages:
            if msg_obj.role == 'system':
                system_message_objects.append(msg_obj)
            else: 
                dialogue_message_objects.append(msg_obj)
        
        if len(dialogue_message_objects) > self.max_messages_in_context:
            num_to_remove = len(dialogue_message_objects) - self.max_messages_in_context
            dialogue_message_objects = dialogue_message_objects[num_to_remove:]
            logger.debug(f"Скользящее окно применено: удалено {num_to_remove} старых сообщений диалога.")
        
        return system_message_objects + dialogue_message_objects
    
    def _convert_message_objects_to_llm_dicts(self, message_objects: List[Message]) -> List[Dict]:
        """Преобразует список объектов Message в список словарей для LLM."""
        return [{'role': msg_obj.role, 'content': msg_obj.content} for msg_obj in message_objects]

    def _optimize_context_by_tokens(self, 
                                   llm_messages: List[Dict], 
                                   token_counter_func: Callable[[str, str], int],
                                   model_name: str) -> List[Dict]:
        """Оптимизирует контекст (список словарей для LLM) по количеству токенов."""
        current_total_tokens = sum(token_counter_func(msg['content'], model_name) for msg in llm_messages)
        
        if current_total_tokens <= self.max_tokens_for_llm:
            return llm_messages 
        
        logger.info(f"Контекст превышает лимит токенов: {current_total_tokens} > {self.max_tokens_for_llm}. Начинается оптимизация.")
        
        system_llm_messages = [msg for msg in llm_messages if msg['role'] == 'system']
        dialogue_llm_messages = [msg for msg in llm_messages if msg['role'] != 'system']
        
        system_tokens = sum(token_counter_func(msg['content'], model_name) for msg in system_llm_messages)
        remaining_token_limit_for_dialogue = self.max_tokens_for_llm - system_tokens
        
        if remaining_token_limit_for_dialogue < 0: 
            logger.warning("Системные сообщения превышают общий лимит токенов. Попытка урезать системные сообщения.")
            if len(system_llm_messages) > 1:
                system_llm_messages = system_llm_messages[-1:] 
                system_tokens = sum(token_counter_func(msg['content'], model_name) for msg in system_llm_messages)
                remaining_token_limit_for_dialogue = self.max_tokens_for_llm - system_tokens
            
            if remaining_token_limit_for_dialogue < 0: 
                 logger.error("Невозможно сформировать контекст: системные сообщения слишком велики.")
                 return [{"role": "user", "content": "Ошибка: контекст для ответа слишком большой, не удалось его оптимизировать."}] 

        optimized_dialogue_messages: List[Dict] = []
        current_dialogue_tokens = 0
        
        for msg in reversed(dialogue_llm_messages):
            msg_tokens = token_counter_func(msg['content'], model_name)
            if current_dialogue_tokens + msg_tokens <= remaining_token_limit_for_dialogue:
                optimized_dialogue_messages.insert(0, msg) 
                current_dialogue_tokens += msg_tokens
            else:
                logger.debug(f"Сообщение '{msg['content'][:30]}...' ({msg_tokens} токенов) не помещается в лимит токенов для диалога.")
        
        final_optimized_messages = system_llm_messages + optimized_dialogue_messages
        final_tokens = system_tokens + current_dialogue_tokens
        
        logger.info(f"Контекст оптимизирован: {len(final_optimized_messages)} сообщений, {final_tokens} токенов.")
        return final_optimized_messages
    
    async def try_create_and_add_summary(self, 
                                     user_id_db: int, # Добавлен user_id_db
                                     all_raw_db_messages: List[Dict], 
                                     current_persona: str, 
                                     llm_service_instance: Any, 
                                     force_summary: bool = False):
        """
        Пытается создать и сохранить суммаризацию в БД, если достигнут порог сообщений.
        """
        if not self.should_create_summary(len(all_raw_db_messages)) and not force_summary:
            return

        messages_for_summary_input = all_raw_db_messages[-self.summary_creation_threshold:]
        if not messages_for_summary_input:
            return

        dialogue_text_for_summary = "\n".join([
            f"{msg.get('role', 'unknown')}: {msg.get('content', '')}" 
            for msg in messages_for_summary_input
        ])

        try:
            logger.info(f"Попытка создания суммаризации для {len(messages_for_summary_input)} сообщений (user_id_db: {user_id_db}, персона: {current_persona}).")
            
            # Получение имени пользователя для промпта суммаризации (опционально)
            # user_tg_obj = await llm_service_instance.bot_instance.bot.get_chat(user_id_tg) # Если нужен user_id_tg
            # dynamic_summary_context = {"user_name": user_tg_obj.first_name or "собеседник"}
            
            summary_text = await llm_service_instance.create_summary(
                messages_text=dialogue_text_for_summary, 
                persona=current_persona
                # dynamic_context_info=dynamic_summary_context 
            )
            
            if summary_text and not summary_text.startswith("[Ошибка создания резюме"):
                first_msg_ts_str = messages_for_summary_input[0].get('created_at')
                last_msg_ts_str = messages_for_summary_input[-1].get('created_at')

                start_ts = datetime.fromisoformat(first_msg_ts_str.replace('Z', '+00:00')) if first_msg_ts_str else datetime.now(timezone.utc) - timedelta(minutes=10) # Фоллбэк
                end_ts = datetime.fromisoformat(last_msg_ts_str.replace('Z', '+00:00')) if last_msg_ts_str else datetime.now(timezone.utc)

                if start_ts.tzinfo is None: start_ts = start_ts.replace(tzinfo=timezone.utc)
                if end_ts.tzinfo is None: end_ts = end_ts.replace(tzinfo=timezone.utc)
                
                # Сохраняем саммари в БД
                await self.db_service.save_context_summary(
                    user_id_db=user_id_db,
                    persona=current_persona,
                    summary_text=summary_text,
                    message_count=len(messages_for_summary_input),
                    summary_period_start_at=start_ts,
                    summary_period_end_at=end_ts,
                    # tokens_saved можно рассчитать, если необходимо
                )
                logger.info(f"Создана и сохранена в БД новая суммаризация для user_id_db {user_id_db}, персона {current_persona}.")
            else:
                logger.warning(f"Не удалось создать валидную суммаризацию для user_id_db {user_id_db}, персона {current_persona}.")
        except Exception as e:
            logger.error(f"Ошибка при создании и сохранении суммаризации для user_id_db {user_id_db}: {e}", exc_info=True)
    
    def should_create_summary(self, total_dialogue_messages: int) -> bool:
        """Определяет, нужно ли создавать новую суммаризацию."""
        return total_dialogue_messages >= self.summary_creation_threshold
    
    async def get_context_stats(self, user_id_db: Optional[int] = None, persona: Optional[str] = None) -> Dict:
        """
        Возвращает статистику по управлению контекстом.
        Если user_id_db и persona указаны, пытается получить кол-во саммари из БД для них.
        """
        stats = {
            'max_messages_in_context_window': self.max_messages_in_context,
            'summary_creation_threshold_messages': self.summary_creation_threshold,
            'max_tokens_for_llm_request': self.max_tokens_for_llm,
            'db_summaries_count_for_query': 0,
            'latest_summary_end_timestamp_for_query': None
        }
        if user_id_db and persona:
            try:
                # Это потребует нового метода в db_service для подсчета или получения всех саммари
                # Для примера, получим только последние
                latest_summaries = await self.db_service.get_latest_context_summaries(user_id_db, persona, limit=1000) # Условно много
                stats['db_summaries_count_for_query'] = len(latest_summaries)
                if latest_summaries:
                    stats['latest_summary_end_timestamp_for_query'] = latest_summaries[0].summary_period_end_at.isoformat()
            except Exception as e:
                logger.warning(f"Не удалось получить статистику саммари из БД для user_id_db {user_id_db}, persona {persona}: {e}")
        return stats

    async def clear_summaries_from_db(self, user_id_db: int, persona: Optional[str] = None, older_than_days: int = 0):
        """Очищает суммаризации из БД для пользователя (и персоны, если указана)."""
        # older_than_days = 0 означает удаление всех (или почти всех, в зависимости от логики delete_old_context_summaries)
        try:
            deleted_count = await self.db_service.delete_old_context_summaries(
                user_id_db=user_id_db, 
                persona=persona, 
                older_than_days=older_than_days 
            )
            logger.info(f"Удалено {deleted_count} суммаризаций из БД для user_id_db {user_id_db}, persona {persona or 'all'}.")
        except Exception as e:
            logger.error(f"Ошибка при очистке суммаризаций из БД для user_id_db {user_id_db}: {e}", exc_info=True)
