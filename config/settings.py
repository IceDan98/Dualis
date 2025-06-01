import os
from dataclasses import dataclass, field
from typing import Optional, List
import logging
import sys
from dotenv import load_dotenv

# Загружаем переменные из .env файла в окружение
# Убедитесь, что .env файл находится в корневой директории проекта или укажите путь к нему
# load_dotenv(dotenv_path=Path(__file__).parent.parent / '.env') # Пример, если .env в корне проекта
load_dotenv()

class ConfigurationError(Exception):
    """Пользовательское исключение для ошибок конфигурации."""
    pass

@dataclass
class BotConfig:
    """Конфигурация бота"""
    # Критически важные переменные из env.txt
    telegram_bot_token: str
    gemini_api_key: str
    minimax_api_key: str
    minimax_group_id: str
    minimax_voice_id: str
    bot_username: str
    payment_payload_secret: str # Добавлено для секрета payload

    admin_user_ids: List[int] = field(default_factory=list)

    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/bot_database.db")) # asyncpg для PostgreSQL

    max_context_messages: int = field(default_factory=lambda: int(os.getenv("MAX_CONTEXT_MESSAGES", "20")))
    context_summary_threshold: int = field(default_factory=lambda: int(os.getenv("CONTEXT_SUMMARY_THRESHOLD", "30"))) # Изменено на 30 согласно Roadmap
    max_tokens_per_request: int = field(default_factory=lambda: int(os.getenv("MAX_TOKENS_PER_REQUEST", "3800"))) # Изменено согласно Roadmap

    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "./logs/bot.log"))
    version: str = field(default_factory=lambda: os.getenv("APP_VERSION", "1.1.0-dev"))
    default_persona: str = field(default_factory=lambda: os.getenv("DEFAULT_PERSONA", "aeris"))
    grace_period_days: int = field(default_factory=lambda: int(os.getenv("GRACE_PERIOD_DAYS", "3")))
    renewal_prompt_days: int = field(default_factory=lambda: int(os.getenv("RENEWAL_PROMPT_DAYS", "7")))

    llm_request_timeout: int = field(default_factory=lambda: int(os.getenv("LLM_REQUEST_TIMEOUT", "90"))) # Увеличено
    llm_connect_timeout: int = field(default_factory=lambda: int(os.getenv("LLM_CONNECT_TIMEOUT", "15"))) # Увеличено
    llm_max_output_tokens: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "2000"))) # Увеличено
    llm_temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.7")))
    gemini_model_name: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL_NAME", "gemini-1.5-flash-latest")) # Добавлено

    tts_request_timeout: int = field(default_factory=lambda: int(os.getenv("TTS_REQUEST_TIMEOUT", "120")))
    tts_connect_timeout: int = field(default_factory=lambda: int(os.getenv("TTS_CONNECT_TIMEOUT", "10")))
    tts_max_text_length: int = field(default_factory=lambda: int(os.getenv("TTS_MAX_TEXT_LENGTH", "450")))

    db_prefs_cache_maxsize: int = field(default_factory=lambda: int(os.getenv("DB_PREFS_CACHE_MAXSIZE", "2000")))
    db_prefs_cache_ttl_sec: int = field(default_factory=lambda: int(os.getenv("DB_PREFS_CACHE_TTL_SEC", "300")))
    db_conv_settings_cache_maxsize: int = field(default_factory=lambda: int(os.getenv("DB_CONV_SETTINGS_CACHE_MAXSIZE", "2000")))
    db_conv_settings_cache_ttl_sec: int = field(default_factory=lambda: int(os.getenv("DB_CONV_SETTINGS_CACHE_TTL_SEC", "120")))

    db_pool_size: int = field(default_factory=lambda: int(os.getenv("DB_POOL_SIZE", "10")))
    db_max_overflow: int = field(default_factory=lambda: int(os.getenv("DB_MAX_OVERFLOW", "20")))
    db_pool_timeout: int = field(default_factory=lambda: int(os.getenv("DB_POOL_TIMEOUT", "30")))
    db_pool_recycle: int = field(default_factory=lambda: int(os.getenv("DB_POOL_RECYCLE", "1800")))
    db_echo_sql: bool = field(default_factory=lambda: os.getenv("DB_ECHO_SQL", "False").lower() == "true")
    db_query_timeout_sec: float = field(default_factory=lambda: float(os.getenv("DB_QUERY_TIMEOUT_SEC", "30.0")))
    db_slow_query_threshold_ms: float = field(default_factory=lambda: float(os.getenv("DB_SLOW_QUERY_THRESHOLD_MS", "1000.0")))

    persona_files_dir: str = field(default_factory=lambda: os.getenv("PERSONA_FILES_DIR", "personas"))
    validator_config: Optional[dict] = None # Можно загружать из JSON-строки в .env

    # Дополнительные настройки из Roadmap, если они нужны глобально
    promocode_attempt_limit_10m: int = field(default_factory=lambda: int(os.getenv("PROMOCODE_ATTEMPT_LIMIT_10M", "10")))
    promocode_attempt_limit_1h: int = field(default_factory=lambda: int(os.getenv("PROMOCODE_ATTEMPT_LIMIT_1H", "30")))
    promocode_monitor_interval_sec: int = field(default_factory=lambda: int(os.getenv("PROMOCODE_MONITOR_INTERVAL_SEC", "300")))


    def __post_init__(self):
        # Загрузка ADMIN_USER_IDS
        admin_ids_str = os.getenv("ADMIN_USER_IDS")
        if admin_ids_str:
            try:
                self.admin_user_ids = [int(admin_id.strip()) for admin_id in admin_ids_str.split(',') if admin_id.strip()]
            except ValueError:
                logging.error("Ошибка парсинга ADMIN_USER_IDS. Используется пустой список. Убедитесь, что это список чисел, разделенных запятой.")
                self.admin_user_ids = []
        else:
            # Если ADMIN_USER_IDS не задан в .env, используем пустой список или значение по умолчанию, если оно было
            logging.warning("Переменная ADMIN_USER_IDS не найдена в .env. Устанавливается пустой список.")
            self.admin_user_ids = []


        # Создание директорий, если они не существуют
        # Для SQLite
        if self.database_url.startswith("sqlite"):
            # Извлекаем путь к файлу БД
            # Пример: "sqlite+aiosqlite:///./data/bot_database.db" -> "./data/bot_database.db"
            db_file_path_part = self.database_url.split("///")[-1]
            db_dir = os.path.dirname(db_file_path_part)
            if db_dir and not os.path.exists(db_dir):
                try:
                    os.makedirs(db_dir, exist_ok=True)
                    logging.info(f"Создана директория для SQLite: {os.path.abspath(db_dir)}")
                except OSError as e:
                     logging.error(f"Не удалось создать директорию для SQLite {db_dir}: {e}")

        # Для лог-файла
        log_dir = os.path.dirname(self.log_file)
        if log_dir and not os.path.exists(log_dir):
            try:
                os.makedirs(log_dir, exist_ok=True)
                logging.info(f"Создана директория для логов: {os.path.abspath(log_dir)}")
            except OSError as e:
                 logging.error(f"Не удалось создать директорию для логов {log_dir}: {e}")

def load_config() -> BotConfig:
    """Загружает конфигурацию из переменных окружения, используя значения из env.txt как источник."""
    try:
        # Значения напрямую из вашего env.txt
        # Эти значения должны быть установлены как переменные окружения перед запуском,
        # либо .env файл должен быть загружен через load_dotenv() в самом начале.
        # load_dotenv() уже вызван в начале файла.

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN_AERIS")
        gemini_key = os.getenv("GEMINI_API_KEY_AERIS")
        minimax_key = os.getenv("MINIMAX_API_KEY")
        minimax_group = os.getenv("MINIMAX_GROUP_ID")
        minimax_voice = os.getenv("MINIMAX_VOICE_ID")
        bot_username_val = os.getenv("BOT_USERNAME")
        payment_secret = os.getenv("PAYMENT_PAYLOAD_SECRET") # Загружаем новый секрет

        required_vars = {
            "TELEGRAM_BOT_TOKEN_AERIS": bot_token,
            "GEMINI_API_KEY_AERIS": gemini_key,
            "MINIMAX_API_KEY": minimax_key,
            "MINIMAX_GROUP_ID": minimax_group,
            "MINIMAX_VOICE_ID": minimax_voice,
            "BOT_USERNAME": bot_username_val,
            "PAYMENT_PAYLOAD_SECRET": payment_secret # Проверяем наличие секрета
        }

        missing = [name for name, val in required_vars.items() if not val]
        if missing:
            error_message = f"Критические переменные окружения не найдены или пусты: {', '.join(missing)}. Пожалуйста, проверьте ваш .env файл."
            # Логгируем ошибку перед тем, как бросить исключение
            # На этом этапе логгер может быть еще не настроен, поэтому используем print
            print(f"CRITICAL CONFIGURATION ERROR: {error_message}")
            raise ConfigurationError(error_message)

        # Проверка на значения-заглушки (если они есть в вашем env.txt)
        if bot_username_val == "YOUR_BOT_USERNAME_HERE" or not bot_username_val: # Добавил проверку на пустую строку
            warning_msg = "Переменная BOT_USERNAME имеет значение по умолчанию или пуста в .env. Реферальные ссылки и некоторые функции могут работать некорректно. Укажите реальное имя пользователя бота."
            print(f"WARNING: {warning_msg}")
            logging.warning(warning_msg) # Логгируем, если логгер уже настроен

        if payment_secret == "your_random_32_character_secret_key_here" or not payment_secret: # Проверка на заглушку и пустоту
            error_msg_secret = "Критическая ошибка: PAYMENT_PAYLOAD_SECRET не установлен или имеет значение по умолчанию. Это необходимо для безопасности платежей."
            print(f"CRITICAL CONFIGURATION ERROR: {error_msg_secret}")
            raise ConfigurationError(error_msg_secret)


        return BotConfig(
            telegram_bot_token=bot_token, # type: ignore
            gemini_api_key=gemini_key, # type: ignore
            minimax_api_key=minimax_key, # type: ignore
            minimax_group_id=minimax_group, # type: ignore
            minimax_voice_id=minimax_voice, # type: ignore
            bot_username=bot_username_val, # type: ignore
            payment_payload_secret=payment_secret # type: ignore
            # Остальные параметры будут загружены через os.getenv с их значениями по умолчанию в __post_init__ или field(default_factory=...)
        )
    except ConfigurationError as e:
        raise
    except Exception as e:
        error_msg = f"Непредвиденная ошибка загрузки конфигурации: {e}"
        print(f"CRITICAL ERROR: {error_msg}")
        raise ConfigurationError(error_msg) from e


def setup_logging(config: BotConfig):
    """Настраивает систему логирования на основе конфигурации."""
    log_level_int = getattr(logging, config.log_level, logging.INFO)
    if not isinstance(log_level_int, int):
        print(f"WARNING: Некорректный LOG_LEVEL: {config.log_level}. Установлен INFO.")
        log_level_int = logging.INFO

    log_dir = os.path.dirname(config.log_file)
    if log_dir and not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir, exist_ok=True)
        except OSError as e:
            print(f"ERROR: Не удалось создать директорию для логов {log_dir}: {e}")
            # Можно перенаправить логи в stdout, если создание директории не удалось
            config.log_file = "" # Путь к файлу пуст, FileHandler не будет создан или выдаст ошибку

    handlers_list = [logging.StreamHandler(sys.stdout)]
    if config.log_file: # Добавляем FileHandler только если путь к файлу указан и директория создана (или была)
        try:
            file_handler = logging.FileHandler(config.log_file, encoding='utf-8')
            handlers_list.append(file_handler)
        except Exception as e_fh:
            print(f"ERROR: Не удалось создать FileHandler для {config.log_file}: {e_fh}. Логи будут только в stdout.")


    # Очищаем существующие хендлеры корневого логгера, чтобы избежать дублирования
    # Это важно, если setup_logging может вызываться несколько раз (например, при перезагрузке)
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    logging.basicConfig(
        level=log_level_int,
        format='%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s',
        handlers=handlers_list,
        # force=True # Для Python 3.8+ можно использовать force=True для перенастройки
    )

    # Настройка отдельного логгера для ошибок API (если нужно)
    # api_errors_logger = logging.getLogger('api_errors')
    # ... (настройка хендлеров для api_errors_logger) ...

    # Уменьшаем уровень логирования для слишком "шумных" библиотек
    noisy_loggers = ["httpx", "httpcore", "aiosqlite", "sqlalchemy.engine", "watchfiles"]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)

    return logging.getLogger(__name__) # Возвращаем логгер для текущего модуля (settings.py)

if __name__ == "__main__":
    try:
        print("Тестирование config/settings.py...")
        # Убедитесь, что у вас есть .env файл с необходимыми переменными, включая PAYMENT_PAYLOAD_SECRET
        # Пример генерации секрета, если его нет:
        # import secrets
        # print(f"Пример PAYMENT_PAYLOAD_SECRET='{secrets.token_urlsafe(32)}'")

        config = load_config()
        logger_main = setup_logging(config)
        logger_main.info("Конфигурация успешно загружена и логирование настроено.")
        logger_main.info(f"Bot Username: {config.bot_username}")
        logger_main.info(f"Admin IDs: {config.admin_user_ids}")
        logger_main.info(f"Database URL (from config object): {config.database_url}")
        logger_main.info(f"Payment Payload Secret: {'SET' if config.payment_payload_secret else 'NOT SET'}") # Проверка

    except ConfigurationError as e:
        print(f"ОШИБКА КОНФИГУРАЦИИ при тестировании settings.py: {e}")
    except Exception as e:
        print(f"НЕПРЕДВИДЕННАЯ ОШИБКА при тестировании settings.py: {e}")
