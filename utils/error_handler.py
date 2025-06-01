# utils/error_handler.py
import logging
import traceback
from typing import Optional, Dict, Any, Callable, Type, Union # Added Union
from functools import wraps
from datetime import datetime, timezone, timedelta
import asyncio
import random
import json
from aiogram import types # For type hinting in try_send_error_to_user

# Основной логгер для этого модуля
logger = logging.getLogger(__name__)
# Специализированный логгер для ошибок API
api_logger = logging.getLogger('api_errors')

# --- Базовые классы исключений для бота ---

class BotError(Exception):
    """
    Базовый класс для всех пользовательских ошибок, специфичных для бота.

    Attributes:
        message (str): Внутреннее сообщение об ошибке для логов и разработчиков.
        error_code (Optional[str]): Уникальный код ошибки для идентификации.
        user_message (Optional[str]): Сообщение, предназначенное для отображения пользователю.
    """
    def __init__(self, message: str, error_code: Optional[str] = None, user_message: Optional[str] = None):
        super().__init__(message)
        self.error_code = error_code
        self.user_message = user_message or "Произошла непредвиденная ошибка. Пожалуйста, попробуйте позже или обратитесь в поддержку."

class APIError(BotError):
    """Ошибка, возникающая при взаимодействии с внешними API (LLM, TTS и т.д.)."""
    pass

class DatabaseError(BotError):
    """Ошибка, возникающая при операциях с базой данных."""
    pass

class ValidationError(BotError):
    """Ошибка, связанная с невалидными данными."""
    pass

class ConfigurationError(BotError):
    """Ошибка, указывающая на проблемы с конфигурацией приложения."""
    pass

class RateLimitError(APIError):
    """Ошибка из-за превышения лимитов запросов к внешнему API."""
    def __init__(self, message: str = "Превышен лимит запросов к API.",
                 error_code: str = 'RATE_LIMIT_EXCEEDED',
                 user_message: Optional[str] = None):
        super().__init__(
            message,
            error_code,
            user_message or "Слишком много запросов. Пожалуйста, подождите немного и попробуйте снова."
        )

class InsufficientPermissionsError(BotError):
    """Недостаток прав у пользователя для выполнения действия."""
    def __init__(self, message: str = "Недостаточно прав для выполнения этого действия.",
                 error_code: str = 'INSUFFICIENT_PERMISSIONS',
                 user_message: Optional[str] = None):
        super().__init__(
            message,
            error_code,
            user_message or "У вас нет прав для выполнения этого действия."
        )

# --- Централизованный обработчик ошибок ---

class ErrorHandler:
    """
    Централизованный обработчик ошибок. Логирует, собирает статистику,
    предоставляет сообщения пользователю.
    """
    
    def __init__(self, app_config: Optional[Any] = None):
        self.error_stats: Dict[str, Any] = {
            'total_errors': 0, 'api_errors': 0, 'database_errors': 0,
            'validation_errors': 0, 'configuration_errors': 0,
            'rate_limit_errors': 0, 'unknown_errors': 0,
            'last_error_at': None, 'last_reset_at': datetime.now(timezone.utc)
        }
        
        self.user_messages_map: Dict[str, str] = {
            'APIError': "🤖 Возникла проблема при обращении к внешнему AI-сервису. Попробуйте через минуту!",
            'DatabaseError': "💾 Произошла ошибка при работе с базой данных. Ваши данные в безопасности, но операция могла не завершиться.",
            'ValidationError': "⚠️ Пожалуйста, проверьте корректность введенных данных.",
            'RateLimitError': "⏰ Слишком много запросов. Пожалуйста, подождите немного и попробуйте снова.",
            'ConfigurationError': "⚙️ Обнаружена ошибка конфигурации. Администраторы уже уведомлены.",
            'InsufficientPermissionsError': "🚫 У вас недостаточно прав для выполнения этого действия.",
            'TimeoutError': "⌛ Время ожидания ответа от сервера истекло. Попробуйте еще раз.",
            'default': "😵 Что-то пошло не так... Мы уже разбираемся в причине!"
        }
        self.config = app_config

    def log_error(self,
                  error: Exception,
                  context: Optional[Dict[str, Any]] = None,
                  user_id: Optional[Union[int, str]] = None,
                  severity: str = 'ERROR') -> str:
        """
        Логирует ошибку, обновляет статистику и возвращает уникальный ID ошибки.
        """
        timestamp_now = datetime.now(timezone.utc)
        # Генерируем ID ошибки, включающий временную метку и хэш для уникальности
        error_id = f"ERR_{timestamp_now.strftime('%Y%m%d_%H%M%S_%f')}_{hash(str(error) + str(context)) % 1000000:06d}"
        
        error_info = {
            'error_id': error_id, 'error_type': type(error).__name__,
            'error_message': str(error), 'user_id': user_id,
            'context': context or {}, 'timestamp': timestamp_now.isoformat(),
            'traceback': traceback.format_exc(limit=10) # Ограничиваем длину трейсбека
        }
        
        self.error_stats['total_errors'] += 1
        self.error_stats['last_error_at'] = error_info['timestamp']
        
        log_message_parts = [
            f"Error Logged [{error_id}]",
            f"User={user_id}" if user_id else "",
            f"Type={error_info['error_type']}",
            f"Msg='{error_info['error_message']}'"
        ]
        if context:
            try:
                context_str = json.dumps(context, default=str, ensure_ascii=False, indent=None)
                log_message_parts.append(f"Context={context_str}")
            except TypeError:
                log_message_parts.append(f"Context_Type_Error (unable_to_serialize)")

        log_message = ", ".join(filter(None, log_message_parts))

        # Обновление счетчиков по типам ошибок
        if isinstance(error, RateLimitError): self.error_stats['rate_limit_errors'] += 1
        if isinstance(error, APIError): self.error_stats['api_errors'] += 1
        elif isinstance(error, DatabaseError): self.error_stats['database_errors'] += 1
        elif isinstance(error, ValidationError): self.error_stats['validation_errors'] += 1
        elif isinstance(error, ConfigurationError): self.error_stats['configuration_errors'] += 1
        else: self.error_stats['unknown_errors'] += 1
        
        target_logger = api_logger if isinstance(error, APIError) and api_logger.handlers else logger
        log_func_to_call = getattr(target_logger, severity.lower(), target_logger.error)
        # Передаем exc_info=False, так как трейсбек уже включен в error_info['traceback']
        # и будет доступен через extra в обработчиках логов, если они настроены на его использование.
        # Для стандартного вывода в консоль или файл, traceback.format_exc() в error_info уже достаточен.
        log_func_to_call(log_message, extra=error_info) 

        # TODO: [P2] Реализовать уведомление администраторов о CRITICAL ошибках (self.notify_admins) - будет сделано в рамках задачи по настройке системы оповещений
        return error_id
    
    def get_user_friendly_message(self, error: Exception) -> str:
        """Возвращает сообщение об ошибке, адаптированное для пользователя."""
        if isinstance(error, BotError) and error.user_message:
            return error.user_message
        error_type_name = type(error).__name__
        if isinstance(error, asyncio.TimeoutError):
            error_type_name = 'TimeoutError'
        return self.user_messages_map.get(error_type_name, self.user_messages_map['default'])
    
    def get_error_stats(self) -> Dict[str, Any]:
        return self.error_stats.copy()
    
    def reset_error_stats(self):
        self.error_stats = {
            'total_errors': 0, 'api_errors': 0, 'database_errors': 0,
            'validation_errors': 0, 'configuration_errors': 0,
            'rate_limit_errors': 0, 'unknown_errors': 0,
            'last_error_at': None, 'last_reset_at': datetime.now(timezone.utc)
        }
        logger.info("Статистика ошибок сброшена.")

# --- Декоратор для обработки ошибок ---
def handle_errors(
    log_level: str = "ERROR", 
    reraise_as: Optional[Type[BotError]] = None,
    default_user_message: Optional[str] = None,
    send_to_user: bool = True
):
    """
    Декоратор для автоматической обработки исключений.
    """
    def decorator(func: Callable):
        async def get_error_handler_instance(args, kwargs) -> ErrorHandler:
            # Логика получения экземпляра ErrorHandler
            eh_instance = kwargs.get('error_handler_instance')
            if isinstance(eh_instance, ErrorHandler): return eh_instance
            if args and hasattr(args[0], 'error_handler') and isinstance(args[0].error_handler, ErrorHandler):
                return args[0].error_handler
            if args and hasattr(args[0], 'bot_instance') and hasattr(args[0].bot_instance, 'error_handler') and isinstance(args[0].bot_instance.error_handler, ErrorHandler):
                 return args[0].bot_instance.error_handler
            
            # Попытка получить глобальный экземпляр, если он определен в этом модуле
            # или если он передан через DI в другом месте и доступен глобально (менее предпочтительно)
            # global_error_handler_module_level = globals().get('error_handler') # Если бы он был определен в этом файле
            # if isinstance(global_error_handler_module_level, ErrorHandler): return global_error_handler_module_level

            logger.warning("Экземпляр ErrorHandler не найден в контексте, используется временный. "
                           "Рекомендуется передавать error_handler явно или через DI.")
            return ErrorHandler() 

        async def try_send_error_to_user(target_event: Any, message_text: str, user_id_for_log: Any):
            """Пытается отправить сообщение об ошибке пользователю."""
            if not target_event: return
            target_to_answer: Optional[Union[types.Message, types.CallbackQuery]] = None
            if isinstance(target_event, types.Message):
                target_to_answer = target_event
            elif isinstance(target_event, types.CallbackQuery):
                # Для CallbackQuery отвечаем на .message, если оно есть, чтобы отправить сообщение в чат.
                # Иначе, если .message нет, отвечаем на сам callback (всплывающее уведомление).
                target_to_answer = target_event.message if target_event.message else target_event
            
            if hasattr(target_to_answer, 'answer') and callable(target_to_answer.answer):
                try:
                    if isinstance(target_event, types.CallbackQuery) and not target_event.message:
                        # Всплывающее уведомление для CallbackQuery без message
                        await target_event.answer(message_text.split('\n')[0][:200], show_alert=True) # Краткое сообщение
                    elif target_to_answer: # Для Message или CallbackQuery.message
                         await target_to_answer.answer(message_text, parse_mode="Markdown")
                except Exception as send_err:
                    logger.error(f"Не удалось отправить сообщение об ошибке пользователю {user_id_for_log}: {send_err}")
            else:
                logger.warning(f"Объект {type(target_event)} не имеет подходящего метода 'answer' для отправки сообщения об ошибке.")

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            current_eh = await get_error_handler_instance(args, kwargs)
            user_id_ctx: Optional[Union[int, str]] = None
            event_ctx: Optional[Any] = None 
            
            if args:
                first_arg = args[0]
                if isinstance(first_arg, (types.Message, types.CallbackQuery)):
                    event_ctx = first_arg
                    if first_arg.from_user: user_id_ctx = first_arg.from_user.id
                elif len(args) > 1 and isinstance(args[1], (types.Message, types.CallbackQuery)):
                    event_ctx = args[1]
                    if event_ctx.from_user: user_id_ctx = event_ctx.from_user.id
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                log_ctx = {'function': func.__name__}
                try: # Безопасное логирование аргументов
                    log_ctx['args_preview'] = [str(a)[:100] for a in args] 
                    log_ctx['kwargs_preview'] = {k: str(v)[:100] for k, v in kwargs.items()}
                except Exception: log_ctx['args_preview'] = "Error serializing args"
                
                error_id = current_eh.log_error(e, context=log_ctx, user_id=user_id_ctx, severity=log_level.upper())
                user_msg_for_display = default_user_message or current_eh.get_user_friendly_message(e)
                final_user_msg = f"{user_msg_for_display}\nКод ошибки: `{error_id}`"

                if send_to_user and event_ctx:
                    await try_send_error_to_user(event_ctx, final_user_msg, user_id_ctx)
                
                if reraise_as:
                    raise reraise_as(
                        f"Ошибка в {func.__name__}: {e}. Original: {type(e).__name__}. ID: {error_id}",
                        error_code=getattr(e, 'error_code', type(e).__name__.upper() + '_IN_FUNC'),
                        user_message=user_msg_for_display 
                    ) from e
                return None # Если не перебрасываем, возвращаем None

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # Синхронная версия декоратора.
            # Логика получения ErrorHandler должна быть синхронной или использовать временный.
            current_eh_sync: ErrorHandler = kwargs.get('error_handler_instance')
            if not isinstance(current_eh_sync, ErrorHandler):
                 # global_eh_sync = globals().get('error_handler') # Если есть глобальный синхронный
                 # if isinstance(global_eh_sync, ErrorHandler): current_eh_sync = global_eh_sync
                 # else: current_eh_sync = ErrorHandler()
                 current_eh_sync = ErrorHandler() # Временный для простоты

            user_id_ctx_sync: Optional[Union[int, str]] = None
            # ... (логика извлечения user_id для синхронных функций, если возможно) ...
            try:
                return func(*args, **kwargs)
            except Exception as e:
                log_ctx_sync = {'function': func.__name__}
                error_id = current_eh_sync.log_error(e, context=log_ctx_sync, user_id=user_id_ctx_sync, severity=log_level.upper())
                user_msg = default_user_message or current_eh_sync.get_user_friendly_message(e)
                logger.info(f"Синхронная функция {func.__name__} завершилась с ошибкой {error_id}. Сообщение для пользователя (не отправлено): {user_msg}")
                if reraise_as:
                    raise reraise_as(
                        f"Ошибка в {func.__name__}: {e}. Original: {type(e).__name__}. ID: {error_id}",
                        error_code=getattr(e, 'error_code', type(e).__name__.upper() + '_IN_FUNC'),
                        user_message=user_msg
                    ) from e
                return None
        
        return async_wrapper if asyncio.iscoroutinefunction(func) else sync_wrapper
    return decorator

# --- Функция для безопасных вызовов API с ретраями ---
async def safe_api_call(
    api_func: Callable[..., Any],
    error_handler_instance: ErrorHandler, 
    max_retries: int = 3,
    initial_retry_delay_seconds: float = 1.0,
    max_retry_delay_seconds: float = 30.0,
    *args: Any,
    **kwargs: Any
) -> Any:
    """
    Выполняет безопасный вызов API с ретраями и экспоненциальной задержкой.
    """
    last_exception: Optional[Exception] = None
    
    for attempt in range(max_retries):
        try:
            if asyncio.iscoroutinefunction(api_func):
                result = await api_func(*args, **kwargs)
            else:
                result = await asyncio.to_thread(api_func, *args, **kwargs)
            return result
            
        except Exception as e:
            last_exception = e
            error_str_lower = str(e).lower()
            
            non_retryable_http_codes = ['400', '401', '403', '404', 'bad request', 'unauthorized', 'forbidden', 'not found']
            is_non_retryable_bot_error = isinstance(e, (ValidationError, ConfigurationError, InsufficientPermissionsError)) or \
                                         (hasattr(e, 'error_code') and isinstance(e.error_code, str) and \
                                          any(phrase in e.error_code.lower() for phrase in ['auth', 'validation', 'permission', 'bad_request']))
            contains_non_retryable_phrase = any(phrase in error_str_lower for phrase in non_retryable_http_codes)

            if is_non_retryable_bot_error or contains_non_retryable_phrase:
                logger.warning(f"Неповторяемая ошибка при вызове API '{api_func.__name__}' (попытка {attempt + 1}): {type(e).__name__} - {e}")
                break 
            
            logger.warning(
                f"Ошибка вызова API '{api_func.__name__}' (попытка {attempt + 1}/{max_retries}), повтор... Ошибка: {type(e).__name__} - {e}"
            )
            if attempt < max_retries - 1:
                delay = min(
                    initial_retry_delay_seconds * (2 ** attempt) + (random.uniform(0.1, 0.5) * initial_retry_delay_seconds),
                    max_retry_delay_seconds
                )
                await asyncio.sleep(delay)
            else: 
                logger.error(f"Вызов API '{api_func.__name__}' провалился после {max_retries} попыток.")
    
    final_error_message = f"API call to '{api_func.__name__}' failed after {max_retries} attempts."
    if last_exception:
        final_error_message += f" Last error: {type(last_exception).__name__} - {last_exception}"

    error_id = error_handler_instance.log_error(
        last_exception if last_exception else APIError(final_error_message, error_code='API_CALL_MAX_RETRIES'),
        context={'function': api_func.__name__, 'attempts': max_retries, 
                 'args_preview': str(args)[:100], 'kwargs_preview': str(kwargs)[:100]},
        severity='ERROR'
    )
    user_msg_for_final_error = error_handler_instance.get_user_friendly_message(
        last_exception if last_exception else APIError("Сервис временно недоступен после нескольких попыток.")
    )
    user_msg_with_id = f"{user_msg_for_final_error} (Код отслеживания: {error_id})"

    raise APIError(
        message=final_error_message,
        error_code=getattr(last_exception, 'error_code', 'API_CALL_FAILED'),
        user_message=user_msg_with_id
    ) from last_exception

# --- Паттерн "Автоматический выключатель" (Circuit Breaker) ---
class CircuitBreaker:
    """
    Реализация паттерна "Автоматический выключатель" (Circuit Breaker).
    """
    STATE_CLOSED = "CLOSED"
    STATE_OPEN = "OPEN"
    STATE_HALF_OPEN = "HALF_OPEN"

    def __init__(self,
                 name: str,
                 failure_threshold: int = 5,
                 recovery_timeout_seconds: int = 60,
                 half_open_success_threshold: int = 2,
                 error_handler_instance: Optional[ErrorHandler] = None):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.half_open_success_threshold = half_open_success_threshold
        self.error_handler = error_handler_instance
        
        self._state = self.STATE_CLOSED
        self._failure_count = 0
        self._success_count_half_open = 0
        self._last_failure_time: Optional[datetime] = None
        self._lock = asyncio.Lock()

        logger.info(f"CircuitBreaker '{self.name}' инициализирован: failure_threshold={failure_threshold}, "
                    f"recovery_timeout={recovery_timeout_seconds}s, half_open_success_threshold={half_open_success_threshold}")

    @property
    async def state(self) -> str:
        async with self._lock:
            if self._state == self.STATE_OPEN and self._last_failure_time:
                if (datetime.now(timezone.utc) - self._last_failure_time) > timedelta(seconds=self.recovery_timeout_seconds):
                    logger.info(f"CircuitBreaker '{self.name}': Таймаут восстановления истек. Переход в HALF_OPEN.")
                    self._state = self.STATE_HALF_OPEN
                    self._success_count_half_open = 0
            return self._state

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        current_cb_state = await self.state

        if current_cb_state == self.STATE_OPEN:
            logger.warning(f"CircuitBreaker '{self.name}' в состоянии OPEN. Вызов {func.__name__} отклонен.")
            raise APIError(
                f"Сервис '{self.name}' временно недоступен (Circuit Breaker открыт). Попробуйте позже.",
                error_code='CIRCUIT_OPEN',
                user_message=f"Сервис '{self.name}' сейчас испытывает трудности. Пожалуйста, повторите попытку через несколько минут."
            )
        
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = await asyncio.to_thread(func, *args, **kwargs)
            await self._on_success()
            return result
        except Exception as e:
            if self.error_handler:
                self.error_handler.log_error(
                    e, context={'cb_name': self.name, 'cb_state_before_failure': current_cb_state, 'func_name': func.__name__},
                    severity='WARNING'
                )
            else:
                logger.warning(f"CircuitBreaker '{self.name}': Исключение при вызове {func.__name__} в состоянии {current_cb_state}: {e}", exc_info=True)
            await self._on_failure()
            raise 

    async def _on_success(self):
        async with self._lock:
            if self._state == self.STATE_HALF_OPEN:
                self._success_count_half_open += 1
                if self._success_count_half_open >= self.half_open_success_threshold:
                    self._state = self.STATE_CLOSED; self._failure_count = 0; self._success_count_half_open = 0
                    logger.info(f"CircuitBreaker '{self.name}': Успешные вызовы в HALF_OPEN. Цепь ЗАМКНУТА (CLOSED).")
                else:
                    logger.info(f"CircuitBreaker '{self.name}': Успешный вызов в HALF_OPEN ({self._success_count_half_open}/{self.half_open_success_threshold}).")
            elif self._state == self.STATE_CLOSED and self._failure_count > 0:
                 logger.debug(f"CircuitBreaker '{self.name}': Успешный вызов в CLOSED после {self._failure_count} ошибок. Счетчик ошибок сброшен.")
                 self._failure_count = 0

    async def _on_failure(self):
        async with self._lock:
            if self._state == self.STATE_HALF_OPEN:
                logger.warning(f"CircuitBreaker '{self.name}': Ошибка в HALF_OPEN. Цепь снова РАЗОМКНУТА (OPEN).")
                self._state = self.STATE_OPEN; self._last_failure_time = datetime.now(timezone.utc)
                self._failure_count = self.failure_threshold; self._success_count_half_open = 0
            elif self._state == self.STATE_CLOSED:
                self._failure_count += 1; self._last_failure_time = datetime.now(timezone.utc)
                if self._failure_count >= self.failure_threshold:
                    self._state = self.STATE_OPEN
                    logger.warning(f"CircuitBreaker '{self.name}': Достигнут порог ошибок ({self._failure_count}/{self.failure_threshold}). Цепь РАЗОМКНУТА (OPEN).")
                else:
                    logger.info(f"CircuitBreaker '{self.name}': Зафиксирована ошибка в CLOSED ({self._failure_count}/{self.failure_threshold}).")

# Глобальные экземпляры Circuit Breaker (лучше инициализировать в main.py с передачей error_handler и конфигов)
gemini_circuit_breaker = CircuitBreaker(name="GeminiAPI_CB", failure_threshold=3, recovery_timeout_seconds=45)
minimax_circuit_breaker = CircuitBreaker(name="MinimaxTTS_API_CB", failure_threshold=4, recovery_timeout_seconds=90)

# Глобальный error_handler (антипаттерн, лучше DI)
# error_handler = ErrorHandler()
