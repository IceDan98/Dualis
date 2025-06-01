# database/operations.py
import logging
from typing import List, Dict, Optional, Any, Tuple, Union, Callable, Sequence
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
import json
import random 

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, AsyncEngine
from sqlalchemy.orm import sessionmaker, selectinload, load_only 
from sqlalchemy.pool import QueuePool
from sqlalchemy import select, update, delete, func, desc, asc, and_, or_, text, cast, Date, case, extract, BigInteger, Interval, text, literal_column
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError

from .models import (
    Base, User, Conversation, Message, UserPreference, UserInsight,
    JournalEntry, Memory, ContextSummary, FileUpload, BotStatistics,
    ErrorLog, ReferralCode, UserActionTimestamp, PromoCode as DBPromoCode,
    TemporaryBlock, Subscription
)
from database.enums import SubscriptionTier, SubscriptionStatus 
# Импорт PromoCodeDiscountType перенесен в функции для избежания циклического импорта

from utils.error_handler import handle_errors, DatabaseError
from utils.caching import async_ttl_cache
from cachetools import TTLCache
from config.settings import BotConfig

logger = logging.getLogger(__name__)

USER_PREFERENCE_PERSONA_SYSTEM_FOR_SUBSCRIPTION = "system"
SUBSCRIPTION_DATA_KEY_FOR_SUBSCRIPTION = "subscription_data" 
USER_PROMO_USAGE_PERSONA_FOR_PROMOCODE = "promocode_usage_log"


class DatabaseConnectionManager:
    # ... (код класса DatabaseConnectionManager остается без изменений) ...
    def __init__(self, database_url: str, bot_config: BotConfig):
        self.database_url = database_url
        self.engine: Optional[AsyncEngine] = None
        self.session_factory: Optional[sessionmaker] = None # type: ignore
        self.bot_config = bot_config
        self._connection_pool_size = self.bot_config.db_pool_size
        self._max_overflow = self.bot_config.db_max_overflow
        self._pool_timeout = self.bot_config.db_pool_timeout
        self._pool_recycle = self.bot_config.db_pool_recycle

    async def initialize(self):
        try:
            db_url_to_use = self.database_url
            if 'sqlite' in db_url_to_use and not db_url_to_use.startswith('sqlite+aiosqlite://'):
                db_url_to_use = db_url_to_use.replace('sqlite:///', 'sqlite+aiosqlite:///').replace('sqlite://', 'sqlite+aiosqlite://')
                if not db_url_to_use.startswith('sqlite+aiosqlite:///'): # pragma: no cover
                    db_url_to_use = f"sqlite+aiosqlite:///{db_url_to_use.split(':', 1)[-1]}"

            # Ensure asyncpg driver is used for PostgreSQL if not specified
            if db_url_to_use.startswith('postgresql://') and '+asyncpg' not in db_url_to_use:
                db_url_to_use = db_url_to_use.replace('postgresql://', 'postgresql+asyncpg://', 1)
            elif db_url_to_use.startswith('postgres://') and '+asyncpg' not in db_url_to_use: # Common alias
                db_url_to_use = db_url_to_use.replace('postgres://', 'postgresql+asyncpg://', 1)

            engine_kwargs: Dict[str, Any] = {
                'echo': self.bot_config.db_echo_sql, 'future': True,
                'pool_pre_ping': True, 'pool_recycle': self._pool_recycle,
            }
            if 'postgresql' in db_url_to_use:
                # Use AsyncAdaptedQueuePool for PostgreSQL with asyncio
                from sqlalchemy.pool import AsyncAdaptedQueuePool
                engine_kwargs.update({
                    'poolclass': AsyncAdaptedQueuePool, 'pool_size': self._connection_pool_size,
                    'max_overflow': self._max_overflow, 'pool_timeout': self._pool_timeout,
                })
            elif 'mysql' in db_url_to_use: # pragma: no cover
                # For MySQL, QueuePool might still be appropriate if using a sync connector under the hood
                # or if a specific async MySQL pool is available and configured.
                # This part might need adjustment based on the specific MySQL async driver being used.
                engine_kwargs.update({
                    'poolclass': QueuePool, 'pool_size': self._connection_pool_size,
                    'max_overflow': self._max_overflow, 'pool_timeout': self._pool_timeout,
                })
            self.engine = create_async_engine(db_url_to_use, **engine_kwargs)
            self.session_factory = sessionmaker(
                self.engine, class_=AsyncSession, expire_on_commit=False,
                autoflush=False, autocommit=False ) # type: ignore
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await self._health_check()
            db_display_url = db_url_to_use.split('@')[-1] if '@' in db_url_to_use else db_url_to_use
            logger.info(f"Database connection manager инициализирован для URL: {db_display_url}")
        except Exception as e: # pragma: no cover
            logger.critical(f"Критическая ошибка инициализации Database Manager: {e}", exc_info=True)
            raise DatabaseError(f"Не удалось инициализировать БД: {e}")

    async def _health_check(self): # pragma: no cover
        try:
            async with self.get_session() as session: # type: ignore
                result = await session.execute(text("SELECT 1"))
                if result.scalar_one() != 1:
                    raise DatabaseError("Проверка подключения к БД не удалась (неверный результат SELECT 1).")
            logger.info("Проверка подключения к БД прошла успешно.")
        except Exception as e:
            logger.error(f"Проверка подключения к БД не удалась: {e}", exc_info=True)
            raise DatabaseError(f"Проверка подключения к БД не удалась: {e}")

    @asynccontextmanager
    async def get_session(self) -> AsyncSession: # type: ignore # pragma: no cover
        if not self.session_factory:
            logger.error("DatabaseConnectionManager не инициализирован (session_factory is None).")
            raise DatabaseError("База данных не инициализирована.")
        session: AsyncSession = self.session_factory() # type: ignore
        try: yield session
        except SQLAlchemyError as e:
            await session.rollback(); logger.error(f"Ошибка сессии SQLAlchemy: {e}", exc_info=True)
            raise DatabaseError(f"Ошибка сессии БД: {e}") from e
        except Exception as e:
            await session.rollback(); logger.error(f"Непредвиденная ошибка в сессии БД: {e}", exc_info=True)
            raise
        finally: await session.close()

    @asynccontextmanager
    async def get_transaction(self) -> AsyncSession: # type: ignore # pragma: no cover
        async with self.get_session() as session: # type: ignore
            try: yield session; await session.commit()
            except SQLAlchemyError as e:
                await session.rollback(); logger.error(f"Ошибка транзакции SQLAlchemy (автоматический роллбэк): {e}", exc_info=True)
                raise DatabaseError(f"Ошибка транзакции: {e}") from e
            except Exception as e:
                await session.rollback(); logger.error(f"Непредвиденная ошибка транзакции (автоматический роллбэк): {e}", exc_info=True)
                raise

    async def close(self): # pragma: no cover
        if self.engine:
            await self.engine.dispose()
            logger.info("Соединения с БД успешно закрыты (engine disposed).")
            self.engine = None; self.session_factory = None

class DatabaseService:
    def __init__(self, bot_config: BotConfig):
        self.bot_config = bot_config
        self.connection_manager = DatabaseConnectionManager(self.bot_config.database_url, self.bot_config)
        self.performance_stats: Dict[str, Any] = {
            'total_queries': 0, 'slow_queries': 0, 'failed_queries': 0,
            'avg_query_time_ms': 0.0, 'last_slow_query': None,
        }
        self.default_timeout_seconds = self.bot_config.db_query_timeout_sec
        self.slow_query_threshold_ms = self.bot_config.db_slow_query_threshold_ms
        self.user_preferences_cache = TTLCache(
            maxsize=self.bot_config.db_prefs_cache_maxsize, ttl=self.bot_config.db_prefs_cache_ttl_sec)
        self.conv_settings_cache = TTLCache(
            maxsize=self.bot_config.db_conv_settings_cache_maxsize, ttl=self.bot_config.db_conv_settings_cache_ttl_sec)
        self.subscription_data_cache = TTLCache(maxsize=1000, ttl=60) 

    async def initialize(self): # pragma: no cover
        await self.connection_manager.initialize()
        logger.info("DatabaseService инициализирован и готов к работе.")

    async def _execute_with_monitoring(self, query_func: Callable, query_name: str = "unknown_query") -> Any: # pragma: no cover
        start_time = datetime.now(timezone.utc)
        self.performance_stats['total_queries'] += 1
        try:
            result = await query_func()
            execution_time_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
            total_q = self.performance_stats['total_queries']
            current_avg = self.performance_stats['avg_query_time_ms']
            self.performance_stats['avg_query_time_ms'] = ((current_avg * (total_q - 1)) + execution_time_ms) / total_q if total_q > 0 else execution_time_ms
            if execution_time_ms > self.slow_query_threshold_ms:
                self.performance_stats['slow_queries'] += 1
                self.performance_stats['last_slow_query'] = {
                    'name': query_name, 'time_ms': execution_time_ms, 'timestamp': start_time.isoformat()}
                logger.warning(f"Медленный запрос '{query_name}': {execution_time_ms:.2f} мс")
            return result
        except Exception as e:
            self.performance_stats['failed_queries'] += 1
            logger.error(f"Ошибка выполнения запроса '{query_name}': {e}", exc_info=True)
            raise

    # --- USER OPERATIONS ---
    # ... (методы User, UserPreference, Conversation, Message, Memory, TemporaryBlock, ContextSummary, UserActionTimestamp, ReferralCode, Statistics - без изменений) ...
    @handle_errors(reraise_as=DatabaseError)
    async def get_or_create_user(self, telegram_id: int, **user_data) -> User:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt = select(User).options(
                    selectinload(User.preferences),
                    selectinload(User.subscriptions) 
                ).where(User.telegram_id == telegram_id)
                result = await session.execute(stmt); user = result.scalar_one_or_none()
                now_utc = datetime.now(timezone.utc)
                if user:
                    user.last_activity = now_utc
                    if not user.is_active: user.is_active = True; logger.info(f"User telegram_id={telegram_id} (DB ID: {user.id}) is active again.")
                    updated_fields = False
                    for key, value in user_data.items():
                        if hasattr(user, key) and value is not None and getattr(user, key) != value: setattr(user, key, value); updated_fields = True
                    if updated_fields: logger.info(f"Данные пользователя telegram_id={telegram_id} обновлены: {list(user_data.keys())}")
                else:
                    cleaned_user_data = {k: v for k, v in user_data.items() if hasattr(User, k) and v is not None}
                    user = User(telegram_id=telegram_id, created_at=now_utc, last_activity=now_utc, is_active=True, **cleaned_user_data)
                    session.add(user); await session.flush()
                    logger.info(f"Новый пользователь создан: telegram_id={telegram_id}, db_id={user.id}")
                    
                    free_sub = Subscription(user_id=user.id, tier=SubscriptionTier.FREE, status=SubscriptionStatus.ACTIVE, activated_at=now_utc)
                    session.add(free_sub)
                    await session.flush() 
                    logger.info(f"Для нового пользователя telegram_id={telegram_id} создана Free подписка ID {free_sub.id}")
                
                await session.refresh(user, attribute_names=['preferences'] if user.preferences else None) # type: ignore
                await session.refresh(user, attribute_names=['subscriptions'] if user.subscriptions else None) # type: ignore
                return user
        return await self._execute_with_monitoring(_query, f"get_or_create_user_tg_{telegram_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[User]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(User).where(User.telegram_id == telegram_id)
                result = await session.execute(stmt); return result.scalar_one_or_none()
        return await self._execute_with_monitoring(_query, f"get_user_by_telegram_id_{telegram_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_user_by_db_id(self, user_id_db: int) -> Optional[User]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(User).where(User.id == user_id_db)
                result = await session.execute(stmt); return result.scalar_one_or_none()
        return await self._execute_with_monitoring(_query, f"get_user_by_db_id_{user_id_db}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def update_user_activity_status(self, user_id_db: int, is_active: bool, reason_inactive: Optional[str] = None) -> bool:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt = update(User).where(User.id == user_id_db).values(is_active=is_active, last_activity=datetime.now(timezone.utc))
                if not is_active and reason_inactive: logger.info(f"Пользователь DB ID {user_id_db} помечен как неактивный. Причина: {reason_inactive}")
                elif is_active: logger.info(f"Пользователь DB ID {user_id_db} помечен как активный.")
                result = await session.execute(stmt); return result.rowcount > 0
        return await self._execute_with_monitoring(_query, f"update_user_activity_status_db_{user_id_db}") # type: ignore

    # Note: BotConfig instantiation moved to avoid TypeError - cache will be configured at runtime
    @handle_errors(reraise_as=DatabaseError)
    async def get_user_preferences(self, user_id_db: int, persona: Optional[str] = None) -> Dict[str, Any]:
        logger.debug(f"DB CALL (get_user_preferences): user_id_db={user_id_db}, persona={persona}")
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(UserPreference).where(UserPreference.user_id == user_id_db)
                if persona: stmt = stmt.where(UserPreference.persona == persona)
                stmt = stmt.options(load_only(UserPreference.preference_key, UserPreference.preference_value, UserPreference.preference_type))
                result = await session.execute(stmt); preferences_db = result.scalars().all(); prefs_dict: Dict[str, Any] = {}
                for pref_db in preferences_db:
                    value_to_store = pref_db.preference_value
                    try:
                        if pref_db.preference_type == 'json': value_to_store = json.loads(pref_db.preference_value) if pref_db.preference_value else None
                        elif pref_db.preference_type == 'bool' or pref_db.preference_type == 'bool_str': value_to_store = pref_db.preference_value.lower() == 'true' if pref_db.preference_value else False
                        elif pref_db.preference_type == 'int': value_to_store = int(pref_db.preference_value) if pref_db.preference_value else None
                        elif pref_db.preference_type == 'float': value_to_store = float(pref_db.preference_value) if pref_db.preference_value else None
                    except (json.JSONDecodeError, ValueError, TypeError) as e: # pragma: no cover
                        logger.warning(f"Ошибка конвертации значения для ключа '{pref_db.preference_key}' (user: {user_id_db}, type: {pref_db.preference_type}): {e}. Значение: '{pref_db.preference_value[:100]}'")
                        if pref_db.preference_type == 'bool_str' and pref_db.preference_value and pref_db.preference_value.lower() not in ['true', 'false']: pass
                        else: continue 
                    prefs_dict[pref_db.preference_key] = value_to_store
                return prefs_dict
        return await self._execute_with_monitoring(_query, f"get_user_preferences_db_{user_id_db}_{persona or 'all'}") # type: ignore

    async def invalidate_user_preferences_cache(self, user_id_db: int, persona: Optional[str] = None): # pragma: no cover
        original_func = getattr(self.get_user_preferences, '_original_func', self.get_user_preferences)
        cache_invalidator = getattr(original_func, 'invalidate_key', None)
        if cache_invalidator:
            await cache_invalidator(self, user_id_db, persona=persona)
            logger.info(f"Кэш UserPreferences инвалидирован для user_id_db={user_id_db}, persona={persona}")
        else: # pragma: no cover
            logger.warning(f"Не удалось найти метод invalidate_key для get_user_preferences. Кэш может быть не инвалидирован.")

    @handle_errors(reraise_as=DatabaseError)
    async def update_user_preference(self, user_id_db: int, key: str, value: Any,
                                   persona: str = 'diana', preference_type: Optional[str] = None):
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt = select(UserPreference).where(UserPreference.user_id == user_id_db, UserPreference.preference_key == key, UserPreference.persona == persona)
                result = await session.execute(stmt); db_preference = result.scalar_one_or_none()
                actual_pref_type = preference_type; str_value: Optional[str]
                if value is None: 
                    str_value = None
                    if actual_pref_type is None: actual_pref_type = 'string' 
                elif actual_pref_type is None:
                    if isinstance(value, bool): actual_pref_type, str_value = 'bool', str(value).lower()
                    elif isinstance(value, int): actual_pref_type, str_value = 'int', str(value)
                    elif isinstance(value, float): actual_pref_type, str_value = 'float', str(value)
                    elif isinstance(value, (dict, list)): actual_pref_type, str_value = 'json', json.dumps(value, ensure_ascii=False, default=str)
                    else: actual_pref_type, str_value = 'string', str(value)
                elif actual_pref_type == 'json' and not isinstance(value, str): str_value = json.dumps(value, ensure_ascii=False, default=str)
                elif actual_pref_type == 'bool_str': str_value = 'true' if value else 'false'
                else: str_value = str(value)
                
                if db_preference:
                    db_preference.preference_value = str_value; db_preference.preference_type = actual_pref_type # type: ignore
                    db_preference.updated_at = datetime.now(timezone.utc) # type: ignore
                else:
                    db_preference = UserPreference(user_id=user_id_db, persona=persona, preference_key=key, preference_value=str_value, preference_type=actual_pref_type)
                    session.add(db_preference)
                return db_preference
        result = await self._execute_with_monitoring(_query, f"update_user_preference_db_{user_id_db}_{key}") # type: ignore
        await self.invalidate_user_preferences_cache(user_id_db, persona=persona)
        if persona == USER_PREFERENCE_PERSONA_SYSTEM_FOR_SUBSCRIPTION: 
            await self.invalidate_user_preferences_cache(user_id_db, persona=None) 
            await self.invalidate_subscription_data_cache(user_id_db) 
        return result

    # --- SUBSCRIPTION OPERATIONS ---
    @async_ttl_cache(TTLCache(maxsize=1000, ttl=60)) 
    @handle_errors(reraise_as=DatabaseError)
    async def get_active_subscription_for_user(self, user_id_db: int) -> Optional[Subscription]:
        logger.debug(f"DB CALL (get_active_subscription_for_user): user_id_db={user_id_db}")
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                now_utc = datetime.now(timezone.utc)
                stmt = select(Subscription).where(
                    Subscription.user_id == user_id_db,
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.TRIAL]),
                    or_(Subscription.expires_at == None, Subscription.expires_at > now_utc) # type: ignore
                ).order_by(desc(Subscription.activated_at)) 
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        return await self._execute_with_monitoring(_query, f"get_active_sub_for_user_{user_id_db}") # type: ignore

    async def invalidate_subscription_data_cache(self, user_id_db: int): # pragma: no cover
        original_func = getattr(self.get_active_subscription_for_user, '_original_func', self.get_active_subscription_for_user)
        cache_invalidator = getattr(original_func, 'invalidate_key', None)
        if cache_invalidator:
            await cache_invalidator(self, user_id_db) 
            logger.info(f"Кэш SubscriptionData инвалидирован для user_id_db={user_id_db}")
        else:
            logger.warning("Не удалось найти метод invalidate_key для get_active_subscription_for_user.")

    @handle_errors(reraise_as=DatabaseError)
    async def save_subscription(self, subscription_obj: Subscription) -> Subscription:
        """Сохраняет или обновляет объект Subscription в БД. Учитывает applied_promocode_id и discount_applied_stars."""
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                # Если передается объект с ID, пытаемся его обновить
                if subscription_obj.id:
                    existing_sub = await session.get(Subscription, subscription_obj.id)
                    if existing_sub:
                        # Обновляем все поля из переданного объекта
                        for column in Subscription.__table__.columns:
                            col_name = column.name
                            if col_name not in ['id', 'created_at', 'user_id'] and hasattr(subscription_obj, col_name):
                                setattr(existing_sub, col_name, getattr(subscription_obj, col_name))
                        existing_sub.updated_at = datetime.now(timezone.utc)
                        session.add(existing_sub) # SQLAlchemy отследит изменения
                        await session.flush()
                        await session.refresh(existing_sub)
                        logger.info(f"Подписка ID {existing_sub.id} для user_id {existing_sub.user_id} обновлена.")
                        return existing_sub
                    else: # Если ID есть, но объекта нет - это ошибка или новый объект с заданным ID
                        logger.warning(f"Попытка обновить несуществующую подписку ID {subscription_obj.id}. Создается новая.")
                        subscription_obj.id = None # Сбрасываем ID для создания новой
                
                # Если ID нет или не удалось обновить, добавляем как новый
                session.add(subscription_obj)
                await session.flush() # Чтобы получить ID для нового объекта
                await session.refresh(subscription_obj)
                logger.info(f"Новая подписка ID {subscription_obj.id} для user_id {subscription_obj.user_id} сохранена.")
                return subscription_obj
        
        saved_sub = await self._execute_with_monitoring(_query, f"save_subscription_user_{subscription_obj.user_id}") # type: ignore
        if saved_sub: # Убедимся, что объект был сохранен
            await self.invalidate_subscription_data_cache(saved_sub.user_id) # Инвалидируем кэш
        return saved_sub


    @handle_errors(reraise_as=DatabaseError)
    async def get_all_user_subscriptions_history(self, user_id_db: int) -> List[Subscription]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(Subscription).where(Subscription.user_id == user_id_db).order_by(desc(Subscription.activated_at))
                result = await session.execute(stmt)
                return list(result.scalars().all())
        return await self._execute_with_monitoring(_query, f"get_all_user_subs_history_{user_id_db}") # type: ignore
    
    @handle_errors(reraise_as=DatabaseError)
    async def get_user_subscription_by_tier(self, user_id_db: int, tier: SubscriptionTier) -> Optional[Subscription]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(Subscription).where(
                    Subscription.user_id == user_id_db,
                    Subscription.tier == tier
                ).order_by(desc(Subscription.activated_at))
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        return await self._execute_with_monitoring(_query, f"get_user_sub_by_tier_{user_id_db}_{tier.value}") # type: ignore


    # --- CONVERSATION OPERATIONS ---
    @handle_errors(reraise_as=DatabaseError)
    async def get_or_create_conversation(self, user_id_db: int, persona: str) -> Conversation:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt = select(Conversation).where(Conversation.user_id == user_id_db, Conversation.persona == persona)
                result = await session.execute(stmt); conversation = result.scalar_one_or_none()
                if not conversation:
                    conversation = Conversation(user_id=user_id_db, persona=persona)
                    session.add(conversation); await session.flush()
                    logger.info(f"Создан новый диалог ID {conversation.id} для user_id_db={user_id_db}, persona='{persona}'")
                return conversation
        return await self._execute_with_monitoring(_query, f"get_or_create_conv_db_{user_id_db}_{persona}") # type: ignore

    # Note: BotConfig instantiation moved to avoid TypeError - cache will be configured at runtime
    @handle_errors(reraise_as=DatabaseError)
    async def get_conversation_settings(self, user_id_db: int, persona: str) -> Dict[str, Any]:
        logger.debug(f"DB CALL (get_conversation_settings): user_id_db={user_id_db}, persona={persona}")
        async def _query():
            conv = await self.get_or_create_conversation(user_id_db, persona)
            return {'current_vibe': conv.current_vibe, 'sexting_level': conv.sexting_level, 'conversation_id': conv.id}
        return await self._execute_with_monitoring(_query, f"get_conv_settings_db_{user_id_db}_{persona}") # type: ignore

    async def invalidate_conversation_settings_cache(self, user_id_db: int, persona: str): # pragma: no cover
        original_func = getattr(self.get_conversation_settings, '_original_func', self.get_conversation_settings)
        cache_invalidator = getattr(original_func, 'invalidate_key', None)
        if cache_invalidator:
            await cache_invalidator(self, user_id_db, persona=persona)
            logger.info(f"Кэш ConversationSettings инвалидирован для user_id_db={user_id_db}, persona={persona}")
        else: # pragma: no cover
            logger.warning(f"Не удалось найти метод invalidate_key для get_conversation_settings.")

    @handle_errors(reraise_as=DatabaseError)
    async def update_conversation_settings(self, user_id_db: int, persona: str, settings_to_update: Dict[str, Any]):
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                conversation = await self.get_or_create_conversation(user_id_db, persona)
                update_data = {k: v for k, v in settings_to_update.items() if v is not None and hasattr(Conversation, k)}
                if not update_data: logger.info(f"Нет валидных данных для обновления настроек диалога user_db_id={user_id_db}, persona={persona}"); return conversation
                update_data['updated_at'] = datetime.now(timezone.utc)
                stmt = update(Conversation).where(Conversation.id == conversation.id).values(**update_data)
                await session.execute(stmt)
                for key, value in update_data.items(): setattr(conversation, key, value)
                logger.info(f"Настройки диалога ID {conversation.id} (user_db_id={user_id_db}, persona={persona}) обновлены: {update_data}")
                return conversation
        result = await self._execute_with_monitoring(_query, f"update_conv_settings_db_{user_id_db}_{persona}") # type: ignore
        await self.invalidate_conversation_settings_cache(user_id_db, persona)
        return result

    # --- MESSAGE OPERATIONS ---
    @handle_errors(reraise_as=DatabaseError)
    async def save_message(self, conversation_id: int, role: str, content: str,
                         message_type: str = 'text', tokens_count: int = 0,
                         message_metadata: Optional[Dict] = None) -> Message:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                new_message = Message(conversation_id=conversation_id, role=role, content=content, message_type=message_type, tokens_count=tokens_count, message_metadata=message_metadata)
                session.add(new_message); await session.flush(); return new_message
        return await self._execute_with_monitoring(_query, f"save_message_conv_{conversation_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_recent_messages(self, user_id_db: int, persona: str, limit: int = 20) -> List[Dict]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                conversation = await self.get_or_create_conversation(user_id_db, persona)
                stmt = (select(Message.role, Message.content, Message.created_at.label("timestamp"), Message.message_type, Message.message_metadata)
                        .where(Message.conversation_id == conversation.id).order_by(desc(Message.created_at)).limit(limit))
                result = await session.execute(stmt)
                messages_from_db = [{"role": row.role, "content": row.content, "timestamp": row.timestamp.isoformat(), # type: ignore
                                     "message_type": row.message_type, "metadata": row.message_metadata} for row in result.all()]
                return list(reversed(messages_from_db))
        return await self._execute_with_monitoring(_query, f"get_recent_messages_db_{user_id_db}_{persona}") # type: ignore

    # --- MEMORY OPERATIONS ---
    @handle_errors(reraise_as=DatabaseError)
    async def save_memory(self, **memory_data) -> Memory:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                new_memory = Memory(**memory_data); session.add(new_memory); await session.flush(); return new_memory
        return await self._execute_with_monitoring(_query, f"save_memory_user_{memory_data.get('user_id')}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_memories(self, user_id: int, persona: str = "", query: Optional[str] = None,
                         limit: int = 10, sort_by_priority_desc: bool = False,
                         sort_by_last_accessed_desc: bool = False, sort_by_relevance_desc: bool = False,
                         sort_by_created_at_asc: bool = False) -> List[Memory]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(Memory).where(Memory.user_id == user_id)
                if persona: stmt = stmt.where(Memory.persona == persona)
                stmt = stmt.where(or_(Memory.expires_at == None, Memory.expires_at > datetime.now(timezone.utc))) # type: ignore
                if query: stmt = stmt.where(Memory.content.icontains(query)) # type: ignore
                order_clauses = []
                if sort_by_priority_desc: order_clauses.append(desc(Memory.priority))
                if sort_by_last_accessed_desc: order_clauses.append(desc(Memory.last_accessed))
                if sort_by_relevance_desc: order_clauses.append(desc(Memory.relevance_score))
                if sort_by_created_at_asc: order_clauses.append(asc(Memory.created_at))
                if not order_clauses: order_clauses.append(desc(Memory.created_at)) 
                stmt = stmt.order_by(*order_clauses).limit(limit)
                result = await session.execute(stmt); return list(result.scalars().all())
        return await self._execute_with_monitoring(_query, f"get_memories_user_{user_id}_{persona}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def update_memory_access(self, memory_id: int) -> bool:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt = update(Memory).where(Memory.id == memory_id).values(
                    last_accessed=datetime.now(timezone.utc), access_count=Memory.access_count + 1)
                result = await session.execute(stmt); return result.rowcount > 0
        return await self._execute_with_monitoring(_query, f"update_memory_access_{memory_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def delete_memory(self, memory_id: int) -> bool:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt = delete(Memory).where(Memory.id == memory_id)
                result = await session.execute(stmt); return result.rowcount > 0
        return await self._execute_with_monitoring(_query, f"delete_memory_{memory_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_memory_by_id(self, memory_id: int) -> Optional[Memory]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                return await session.get(Memory, memory_id)
        return await self._execute_with_monitoring(_query, f"get_memory_by_id_{memory_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_expired_memories_ids(self, user_id_db: int, persona: Optional[str], now_utc: datetime) -> List[int]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(Memory.id).where(
                    Memory.user_id == user_id_db, Memory.expires_at != None, Memory.expires_at < now_utc) # type: ignore
                if persona: stmt = stmt.where(Memory.persona == persona)
                result = await session.execute(stmt); return list(result.scalars().all())
        return await self._execute_with_monitoring(_query, f"get_expired_mem_ids_user_{user_id_db}_{persona or 'all'}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_active_memory_count_for_user(self, user_id_db: int, persona: Optional[str] = None) -> int:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(func.count(Memory.id)).where(
                    Memory.user_id == user_id_db,
                    or_(Memory.expires_at == None, Memory.expires_at > datetime.now(timezone.utc)) # type: ignore
                )
                if persona: stmt = stmt.where(Memory.persona == persona)
                result = await session.execute(stmt)
                return result.scalar_one_or_none() or 0
        return await self._execute_with_monitoring(_query, f"get_active_mem_count_user_{user_id_db}_{persona or 'all'}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_memory_type_distribution(self, user_id_db: int, persona: Optional[str] = None) -> Dict[str, int]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(Memory.memory_type, func.count(Memory.id)).where(
                    Memory.user_id == user_id_db,
                    or_(Memory.expires_at == None, Memory.expires_at > datetime.now(timezone.utc)) # type: ignore
                )
                if persona: stmt = stmt.where(Memory.persona == persona)
                stmt = stmt.group_by(Memory.memory_type)
                result = await session.execute(stmt)
                return {row[0]: row[1] for row in result.all()}
        return await self._execute_with_monitoring(_query, f"get_mem_type_dist_user_{user_id_db}_{persona or 'all'}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_memory_priority_distribution(self, user_id_db: int, persona: Optional[str] = None) -> Dict[int, int]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(Memory.priority, func.count(Memory.id)).where(
                    Memory.user_id == user_id_db,
                    or_(Memory.expires_at == None, Memory.expires_at > datetime.now(timezone.utc)) # type: ignore
                )
                if persona: stmt = stmt.where(Memory.persona == persona)
                stmt = stmt.group_by(Memory.priority)
                result = await session.execute(stmt)
                return {row[0]: row[1] for row in result.all()}
        return await self._execute_with_monitoring(_query, f"get_mem_prio_dist_user_{user_id_db}_{persona or 'all'}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_memory_aggregate_stats(self, user_id_db: int, persona: Optional[str] = None) -> Tuple[float, int]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(
                    func.avg(Memory.emotional_weight).label("avg_emotion"),
                    func.sum(Memory.access_count).label("total_access")
                ).where(
                    Memory.user_id == user_id_db,
                    or_(Memory.expires_at == None, Memory.expires_at > datetime.now(timezone.utc)) # type: ignore
                )
                if persona: stmt = stmt.where(Memory.persona == persona)
                result = await session.execute(stmt)
                row = result.one_or_none()
                avg_emotion = float(row.avg_emotion) if row and row.avg_emotion is not None else 0.0
                total_access = int(row.total_access) if row and row.total_access is not None else 0
                return avg_emotion, total_access
        return await self._execute_with_monitoring(_query, f"get_mem_agg_stats_user_{user_id_db}_{persona or 'all'}") # type: ignore


    @handle_errors(reraise_as=DatabaseError)
    async def update_all_user_memories_expiration(self, user_id_db: int, new_expires_at: Optional[datetime], only_if_longer: bool = False) -> int:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt_update = update(Memory).where(Memory.user_id == user_id_db)
                conditions_to_update = []
                if new_expires_at is None: 
                    conditions_to_update.append(Memory.expires_at != None) # type: ignore
                else: 
                    if only_if_longer:
                        conditions_to_update.append(or_(Memory.expires_at == None, Memory.expires_at < new_expires_at)) # type: ignore
                if conditions_to_update: 
                    stmt_update = stmt_update.where(and_(*conditions_to_update))
                stmt_update = stmt_update.values(expires_at=new_expires_at, updated_at=datetime.now(timezone.utc))
                result = await session.execute(stmt_update)
                updated_count = result.rowcount
                logger.info(f"Обновлено expires_at для {updated_count} воспоминаний user_id_db {user_id_db} на {new_expires_at or 'permanent'}.")
                return updated_count
        return await self._execute_with_monitoring(_query, f"update_mem_expiration_user_{user_id_db}") # type: ignore

    # --- TEMPORARY BLOCK OPERATIONS ---
    @handle_errors(reraise_as=DatabaseError)
    async def add_temporary_block(self, user_id_db: int, block_type: str,
                                  blocked_until_utc: datetime, reason: Optional[str] = None) -> TemporaryBlock:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt_delete_old = delete(TemporaryBlock).where(
                    TemporaryBlock.user_id_db == user_id_db, TemporaryBlock.block_type == block_type) 
                await session.execute(stmt_delete_old)
                new_block = TemporaryBlock(
                    user_id_db=user_id_db, block_type=block_type,
                    blocked_until_utc=blocked_until_utc, reason=reason)
                session.add(new_block); await session.flush()
                logger.info(f"Пользователь DB ID {user_id_db} временно заблокирован (тип: {block_type}) до {blocked_until_utc.isoformat()}. Причина: {reason or 'N/A'}")
                return new_block
        return await self._execute_with_monitoring(_query, f"add_temp_block_user_{user_id_db}_{block_type}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_active_temporary_block(self, user_id_db: int, block_type: Optional[str] = None) -> Optional[TemporaryBlock]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(TemporaryBlock).where(
                    TemporaryBlock.user_id_db == user_id_db,
                    TemporaryBlock.blocked_until_utc > datetime.now(timezone.utc)
                ).order_by(desc(TemporaryBlock.blocked_until_utc))
                if block_type: stmt = stmt.where(TemporaryBlock.block_type == block_type)
                result = await session.execute(stmt); return result.scalars().first()
        return await self._execute_with_monitoring(_query, f"get_active_temp_block_user_{user_id_db}_{block_type or 'any'}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def delete_expired_temporary_blocks(self, older_than_utc: Optional[datetime] = None) -> int: # pragma: no cover
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                cutoff_time = older_than_utc or datetime.now(timezone.utc)
                stmt = delete(TemporaryBlock).where(TemporaryBlock.blocked_until_utc < cutoff_time)
                result = await session.execute(stmt); deleted_count = result.rowcount
                if deleted_count > 0:
                    logger.info(f"Удалено {deleted_count} истекших временных блокировок (старше {cutoff_time.isoformat()}).")
                return deleted_count
        return await self._execute_with_monitoring(_query, "delete_expired_temp_blocks") # type: ignore

    # --- CONTEXT SUMMARY OPERATIONS ---
    @handle_errors(reraise_as=DatabaseError)
    async def save_context_summary(self, user_id_db: int, persona: str, summary_text: str,
                                   message_count: int, summary_period_start_at: datetime,
                                   summary_period_end_at: datetime, tokens_saved: int = 0) -> ContextSummary:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                new_summary = ContextSummary(
                    user_id=user_id_db, persona=persona, summary_text=summary_text, message_count=message_count,
                    summary_period_start_at=summary_period_start_at, summary_period_end_at=summary_period_end_at,
                    tokens_saved=tokens_saved)
                session.add(new_summary); await session.flush()
                logger.info(f"Сохранена суммаризация ID {new_summary.id} для user_db_id {user_id_db}, persona {persona}.")
                return new_summary
        return await self._execute_with_monitoring(_query, f"save_context_summary_user_{user_id_db}_{persona}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_latest_context_summaries(self, user_id_db: int, persona: str, limit: int = 1) -> List[ContextSummary]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(ContextSummary).where(
                    ContextSummary.user_id == user_id_db, ContextSummary.persona == persona
                ).order_by(desc(ContextSummary.summary_period_end_at)).limit(limit)
                result = await session.execute(stmt); return list(result.scalars().all())
        return await self._execute_with_monitoring(_query, f"get_latest_summaries_user_{user_id_db}_{persona}_{limit}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def delete_old_context_summaries(self, user_id_db: Optional[int] = None,
                                         persona: Optional[str] = None,
                                         older_than_days: int = 30) -> int: # pragma: no cover
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                cutoff_date = datetime.now(timezone.utc) - timedelta(days=older_than_days)
                stmt = delete(ContextSummary).where(ContextSummary.created_at < cutoff_date)
                if user_id_db: stmt = stmt.where(ContextSummary.user_id == user_id_db)
                if persona: stmt = stmt.where(ContextSummary.persona == persona)
                result = await session.execute(stmt); deleted_count = result.rowcount
                if deleted_count > 0:
                    logger.info(f"Удалено {deleted_count} старых суммаризаций (старше {older_than_days} дней) "
                                f"для user_id_db={user_id_db or 'all'}, persona={persona or 'all'}.")
                return deleted_count
        return await self._execute_with_monitoring(_query, f"delete_old_summaries_user_{user_id_db or 'all'}_{persona or 'all'}") # type: ignore

    # --- UserActionTimestamp (Rate Limiter) ---
    @handle_errors(reraise_as=DatabaseError)
    async def add_user_action_timestamp(self, user_id_db: int, action_key: str, timestamp: datetime):
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                action = UserActionTimestamp(user_id_db=user_id_db, action_key=action_key, timestamp=timestamp)
                session.add(action)
        return await self._execute_with_monitoring(_query, f"add_user_action_ts_{user_id_db}_{action_key}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def count_user_actions_in_window(self, user_id_db: int, action_key: str, window_start_time: datetime) -> int:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(func.count(UserActionTimestamp.id)).where(
                    UserActionTimestamp.user_id_db == user_id_db, UserActionTimestamp.action_key == action_key,
                    UserActionTimestamp.timestamp >= window_start_time)
                result = await session.execute(stmt); return result.scalar_one_or_none() or 0
        return await self._execute_with_monitoring(_query, f"count_user_actions_{user_id_db}_{action_key}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_user_action_timestamps_in_window(self, user_id_db: int, action_key: str, window_start_time: datetime) -> List[datetime]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(UserActionTimestamp.timestamp).where(
                    UserActionTimestamp.user_id_db == user_id_db, UserActionTimestamp.action_key == action_key,
                    UserActionTimestamp.timestamp >= window_start_time
                ).order_by(asc(UserActionTimestamp.timestamp))
                result = await session.execute(stmt); return [ts for ts, in result.all()] # type: ignore
        return await self._execute_with_monitoring(_query, f"get_user_action_ts_list_{user_id_db}_{action_key}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def delete_old_user_action_timestamps(self, user_id_db: Optional[int] = None,
                                              action_key: Optional[str] = None,
                                              older_than_time: Optional[datetime] = None) -> int: # pragma: no cover
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt = delete(UserActionTimestamp)
                conditions = []
                if user_id_db: conditions.append(UserActionTimestamp.user_id_db == user_id_db)
                if action_key: conditions.append(UserActionTimestamp.action_key == action_key)
                
                retention_days_config = getattr(self.bot_config, 'user_action_log_retention_days', 7)
                final_older_than_time = older_than_time or (datetime.now(timezone.utc) - timedelta(days=retention_days_config))
                conditions.append(UserActionTimestamp.timestamp < final_older_than_time)
                
                if not conditions: 
                     logger.error("Критическая ошибка: попытка delete_old_user_action_timestamps без каких-либо условий. Операция отменена.")
                     return 0
                
                stmt = stmt.where(and_(*conditions))
                result = await session.execute(stmt); deleted_count = result.rowcount
                if deleted_count > 0: logger.info(f"Удалено {deleted_count} старых записей UserActionTimestamp. Условия: user_id_db={user_id_db}, action_key={action_key}, older_than={final_older_than_time}")
                return deleted_count
        return await self._execute_with_monitoring(_query, f"delete_old_action_ts_{user_id_db or 'all'}_{action_key or 'all'}") # type: ignore

    # --- PROMOCODE OPERATIONS ---
    @handle_errors(reraise_as=DatabaseError)
    async def save_promocode(self, db_promo_code_obj: DBPromoCode) -> DBPromoCode:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                if db_promo_code_obj.id:
                    existing_promo = await session.get(DBPromoCode, db_promo_code_obj.id)
                    if existing_promo:
                        for column in DBPromoCode.__table__.columns:
                            if column.name not in ['id', 'created_at'] and hasattr(db_promo_code_obj, column.name):
                                setattr(existing_promo, column.name, getattr(db_promo_code_obj, column.name))
                        existing_promo.updated_at = datetime.now(timezone.utc)
                        session.add(existing_promo) 
                        logger.info(f"Промокод ID {existing_promo.id} (код: {existing_promo.code}) обновлен.")
                        await session.flush(); await session.refresh(existing_promo)
                        return existing_promo
                    else: # pragma: no cover
                        logger.warning(f"Попытка обновить несуществующий промокод ID {db_promo_code_obj.id}. Создается новый.")
                        db_promo_code_obj.id = None; session.add(db_promo_code_obj)
                else:
                    session.add(db_promo_code_obj)
                await session.flush(); await session.refresh(db_promo_code_obj)
                return db_promo_code_obj
        return await self._execute_with_monitoring(_query, f"save_promocode_code_{db_promo_code_obj.code}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_promocode_by_code(self, code_str: str) -> Optional[DBPromoCode]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(DBPromoCode).where(func.upper(DBPromoCode.code) == func.upper(code_str))
                result = await session.execute(stmt); return result.scalar_one_or_none()
        return await self._execute_with_monitoring(_query, f"get_promocode_by_code_{code_str}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_promocode_by_id(self, promocode_id: int) -> Optional[DBPromoCode]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                return await session.get(DBPromoCode, promocode_id)
        return await self._execute_with_monitoring(_query, f"get_promocode_by_id_{promocode_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_all_promocodes_paginated(self, active_only: bool = False, page: int = 1, page_size: int = 20) -> Tuple[List[DBPromoCode], int]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt_select = select(DBPromoCode); stmt_count = select(func.count(DBPromoCode.id))
                conditions = []
                if active_only:
                    conditions.append(DBPromoCode.is_active == True)
                    conditions.append(or_(DBPromoCode.expires_at == None, DBPromoCode.expires_at > datetime.now(timezone.utc))) # type: ignore
                    conditions.append(or_(DBPromoCode.max_uses == None, DBPromoCode.uses_count < DBPromoCode.max_uses)) # type: ignore
                if conditions:
                    stmt_select = stmt_select.where(and_(*conditions)); stmt_count = stmt_count.where(and_(*conditions))
                total_count_res = await session.execute(stmt_count)
                total_count = total_count_res.scalar_one_or_none() or 0
                offset = (page - 1) * page_size
                stmt_select = stmt_select.order_by(desc(DBPromoCode.created_at)).limit(page_size).offset(offset)
                promos_res = await session.execute(stmt_select)
                promos_list = list(promos_res.scalars().all())
                return promos_list, total_count
        return await self._execute_with_monitoring(_query, f"get_all_promocodes_paginated_active_{active_only}_page_{page}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def increment_promocode_uses(self, promocode_id: int, user_id_db_for_log: Optional[int] = None) -> bool:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                promo = await session.get(DBPromoCode, promocode_id)
                if not promo: logger.warning(f"Промокод ID {promocode_id} не найден для инкремента."); return False
                if not promo.is_active: logger.warning(f"Промокод ID {promocode_id} (код: {promo.code}) не активен. Инкремент отменен."); return False
                if promo.expires_at and promo.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
                    logger.warning(f"Промокод ID {promocode_id} (код: {promo.code}) истек. Инкремент отменен.")
                    promo.is_active = False; await session.flush(); return False
                if promo.max_uses is not None and promo.uses_count >= promo.max_uses:
                    logger.warning(f"Промокод ID {promocode_id} (код: {promo.code}) исчерпал лимит ({promo.uses_count}/{promo.max_uses}). Инкремент отменен.")
                    promo.is_active = False; await session.flush(); return False
                promo.uses_count += 1
                promo.updated_at = datetime.now(timezone.utc)
                if promo.max_uses is not None and promo.uses_count >= promo.max_uses: promo.is_active = False
                await session.flush()
                logger.info(f"Счетчик использований для промокода ID {promocode_id} (код: {promo.code}) увеличен до {promo.uses_count}.")
                return True
        return await self._execute_with_monitoring(_query, f"increment_promocode_uses_{promocode_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_user_promocode_usage_count(self, user_id_db: int, promocode_id: int) -> int:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(func.count(UserPreference.id)).where( 
                    UserPreference.user_id == user_id_db,
                    UserPreference.persona == USER_PROMO_USAGE_PERSONA_FOR_PROMOCODE,
                    UserPreference.preference_key.like(f"promocode_used_{promocode_id}_%") # type: ignore
                )
                result = await session.execute(stmt); return result.scalar_one_or_none() or 0
        return await self._execute_with_monitoring(_query, f"get_user_promo_usage_count_user_{user_id_db}_promo_{promocode_id}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def delete_promocode_db(self, promocode_id: int) -> bool:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt_delete_logs = delete(UserPreference).where(
                    UserPreference.persona == USER_PROMO_USAGE_PERSONA_FOR_PROMOCODE,
                    UserPreference.preference_key.like(f"promocode_used_{promocode_id}_%") # type: ignore
                )
                deleted_logs_result = await session.execute(stmt_delete_logs)
                logger.info(f"Удалено {deleted_logs_result.rowcount} логов использования для промокода ID {promocode_id}.")
                stmt_delete_promo = delete(DBPromoCode).where(DBPromoCode.id == promocode_id)
                result = await session.execute(stmt_delete_promo); return result.rowcount > 0
        return await self._execute_with_monitoring(_query, f"delete_promocode_id_{promocode_id}") # type: ignore

    # --- REFERRAL OPERATIONS ---
    @handle_errors(reraise_as=DatabaseError)
    async def get_referral_code_by_user_id(self, user_id_db: int) -> Optional[ReferralCode]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(ReferralCode).where(ReferralCode.user_id_db == user_id_db)
                result = await session.execute(stmt); return result.scalar_one_or_none()
        return await self._execute_with_monitoring(_query, f"get_ref_code_by_user_id_{user_id_db}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def create_referral_code(self, user_id_db: int, code_str: str) -> ReferralCode:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                new_rc = ReferralCode(user_id_db=user_id_db, code=code_str.upper())
                session.add(new_rc); await session.flush(); return new_rc
        return await self._execute_with_monitoring(_query, f"create_ref_code_user_{user_id_db}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_user_by_referral_code(self, code_str: str) -> Optional[User]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(User).join(ReferralCode).where(func.upper(ReferralCode.code) == func.upper(code_str))
                result = await session.execute(stmt); return result.scalar_one_or_none()
        return await self._execute_with_monitoring(_query, f"get_user_by_ref_code_{code_str}") # type: ignore

    # --- STATISTICS OPERATIONS ---
    @handle_errors(reraise_as=DatabaseError)
    async def save_statistic(self, metric_name: str, metric_value: float,
                           user_id: Optional[int] = None, persona: Optional[str] = None,
                           additional_data: Optional[Dict[str, Any]] = None) -> BotStatistics:
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                new_stat = BotStatistics(
                    metric_name=metric_name, metric_value=metric_value, user_id=user_id,
                    persona=persona, additional_data=additional_data, date=datetime.now(timezone.utc).date())
                session.add(new_stat); await session.flush(); return new_stat
        return await self._execute_with_monitoring(_query, f"save_statistic_{metric_name}") # type: ignore

    # --- ADMIN PANEL DATA ---
    @handle_errors(reraise_as=DatabaseError)
    async def get_admin_dashboard_data(self) -> Dict[str, Any]: # pragma: no cover
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                total_users_res = await session.execute(select(func.count(User.id)))
                active_24h_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                active_users_24h_res = await session.execute(select(func.count(User.id)).where(User.last_activity >= active_24h_cutoff, User.is_active == True))
                total_messages_res = await session.execute(select(func.count(Message.id)))
                messages_24h_res = await session.execute(select(func.count(Message.id)).where(Message.created_at >= active_24h_cutoff))
                
                stmt_active_paid_subs = select(func.count(Subscription.id)).where(
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.TRIAL]), 
                    Subscription.tier != SubscriptionTier.FREE,
                    or_(Subscription.expires_at == None, Subscription.expires_at > datetime.now(timezone.utc)) # type: ignore
                )
                active_paid_subs_res = await session.execute(stmt_active_paid_subs)
                active_paid_subs_count = active_paid_subs_res.scalar_one_or_none() or 0
                
                return {
                    "total_users": total_users_res.scalar_one_or_none() or 0,
                    "active_users_24h": active_users_24h_res.scalar_one_or_none() or 0,
                    "total_messages": total_messages_res.scalar_one_or_none() or 0,
                    "messages_24h": messages_24h_res.scalar_one_or_none() or 0,
                    "active_paid_subscriptions": active_paid_subs_count
                }
        return await self._execute_with_monitoring(_query, "get_admin_dashboard_data") # type: ignore

    # --- АНАЛИТИЧЕСКИЕ МЕТОДЫ ---
    @handle_errors(reraise_as=DatabaseError)
    async def get_total_active_users_count(self) -> int:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(func.count(User.id)).where(User.is_active == True)
                result = await session.execute(stmt); return result.scalar_one_or_none() or 0
        return await self._execute_with_monitoring(_query, "get_total_active_users_count") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_new_users_count_for_period(self, start_date: datetime, end_date: datetime) -> int:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(func.count(User.id)).where(User.created_at >= start_date, User.created_at < end_date)
                result = await session.execute(stmt); return result.scalar_one_or_none() or 0
        return await self._execute_with_monitoring(_query, f"get_new_users_count_period_{start_date.date()}_{end_date.date()}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_average_dau_for_period(self, start_date: datetime, end_date: datetime) -> float:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                num_days = (end_date.date() - start_date.date()).days + 1
                if num_days <= 0: return 0.0
                
                stmt_dau_subquery = select(
                    cast(Message.created_at, Date).label("activity_date"),
                    func.count(func.distinct(Conversation.user_id)).label("daily_users")
                ).join(Conversation, Message.conversation_id == Conversation.id).where(
                    Message.created_at >= start_date,
                    Message.created_at < end_date, 
                    Message.role == 'user' 
                ).group_by(cast(Message.created_at, Date)).subquery()

                stmt_avg_dau = select(func.avg(stmt_dau_subquery.c.daily_users))
                result = await session.execute(stmt_avg_dau)
                avg_dau = result.scalar_one_or_none() or 0.0
                
                if avg_dau == 0.0: 
                    logger.debug("DAU по сообщениям равен 0, используется фоллбэк на last_activity.")
                    stmt_unique_users_in_period = select(func.count(func.distinct(User.id))).where(
                        User.last_activity >= start_date, User.last_activity < end_date, User.is_active == True)
                    result_unique_users = await session.execute(stmt_unique_users_in_period)
                    total_unique_users_in_period = result_unique_users.scalar_one_or_none() or 0
                    return float(total_unique_users_in_period / num_days) if total_unique_users_in_period > 0 else 0.0
                return float(avg_dau)
        return await self._execute_with_monitoring(_query, f"get_avg_dau_period_{start_date.date()}_{end_date.date()}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_mau_for_period(self, end_date: datetime, period_days: int = 30) -> int:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                start_date = end_date - timedelta(days=period_days)
                stmt_mau = select(func.count(func.distinct(Conversation.user_id))
                ).join(Message, Message.conversation_id == Conversation.id).where(
                    Message.created_at >= start_date,
                    Message.created_at < end_date,
                    Message.role == 'user'
                )
                result = await session.execute(stmt_mau)
                mau = result.scalar_one_or_none() or 0
                if mau == 0:
                    logger.debug("MAU по сообщениям равен 0, используется фоллбэк на last_activity.")
                    stmt_fallback = select(func.count(func.distinct(User.id))).where(
                        User.last_activity >= start_date, User.last_activity < end_date, User.is_active == True)
                    result_fallback = await session.execute(stmt_fallback)
                    return result_fallback.scalar_one_or_none() or 0
                return mau
        return await self._execute_with_monitoring(_query, f"get_mau_end_date_{end_date.date()}_period_{period_days}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_avg_session_duration_for_period(self, start_date: datetime, end_date: datetime) -> float:
        """Рассчитывает среднюю длительность сессии на основе сообщений."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                session_timeout_minutes = self.bot_config.user_session_timeout_minutes or 30
                
                if 'postgresql' not in self.connection_manager.database_url:
                    logger.warning("get_avg_session_duration_for_period: точный расчет через SQL доступен только для PostgreSQL. Используется Python-based эвристика (может быть медленной).")
                    # Python-based эвристика (может быть медленной на больших данных)
                    active_users_in_period_stmt = select(func.distinct(Conversation.user_id)).join(Message).where(
                        Message.created_at >= start_date, Message.created_at < end_date, Message.role == 'user')
                    active_users_res = await session.execute(active_users_in_period_stmt)
                    active_user_ids = [uid for uid, in active_users_res.all()]

                    if not active_user_ids: return 0.0
                    
                    total_session_duration_seconds_sum = 0
                    total_sessions_count_sum = 0

                    for user_id in active_user_ids:
                        user_messages_stmt = select(Message.created_at).join(Conversation).where(
                            Conversation.user_id == user_id,
                            Message.created_at >= start_date,
                            Message.created_at < end_date
                        ).order_by(asc(Message.created_at))
                        user_messages_res = await session.execute(user_messages_stmt)
                        timestamps = [ts for ts, in user_messages_res.all()]

                        if len(timestamps) < 2: continue

                        current_session_start_ts = timestamps[0]
                        for i in range(1, len(timestamps)):
                            if (timestamps[i] - timestamps[i-1]) > timedelta(minutes=session_timeout_minutes):
                                session_duration = (timestamps[i-1] - current_session_start_ts).total_seconds()
                                if session_duration >= 10 : # Учитываем сессии дольше 10 сек
                                    total_session_duration_seconds_sum += session_duration
                                    total_sessions_count_sum += 1
                                current_session_start_ts = timestamps[i] 
                        
                        last_session_duration = (timestamps[-1] - current_session_start_ts).total_seconds()
                        if last_session_duration >= 10:
                            total_session_duration_seconds_sum += last_session_duration
                            total_sessions_count_sum += 1
                    
                    avg_duration = (total_session_duration_seconds_sum / total_sessions_count_sum) if total_sessions_count_sum > 0 else 0.0
                    logger.info(f"Средняя длительность сессии (Python эвристика): {avg_duration:.2f} сек за период {start_date.date()} - {end_date.date()}.")
                    return avg_duration

                # Запрос для PostgreSQL
                raw_sql = text(f"""
                    WITH UserMessageLag AS (
                        SELECT
                            c.user_id,
                            m.created_at,
                            LAG(m.created_at, 1, m.created_at) OVER (PARTITION BY c.user_id ORDER BY m.created_at) as prev_message_at
                        FROM messages m
                        JOIN conversations c ON m.conversation_id = c.id
                        WHERE m.created_at >= :start_date AND m.created_at < :end_date AND m.role = 'user'
                    ),
                    SessionBoundaries AS (
                        SELECT
                            user_id,
                            created_at,
                            prev_message_at,
                            CASE
                                WHEN EXTRACT(EPOCH FROM (created_at - prev_message_at)) > (:timeout_seconds) THEN 1
                                ELSE 0
                            END as is_new_session_start
                        FROM UserMessageLag
                    ),
                    SessionGroups AS (
                        SELECT
                            user_id,
                            created_at,
                            SUM(is_new_session_start) OVER (PARTITION BY user_id ORDER BY created_at) as session_group_id
                        FROM SessionBoundaries
                    ),
                    SessionDurations AS (
                        SELECT
                            user_id,
                            session_group_id,
                            EXTRACT(EPOCH FROM (MAX(created_at) - MIN(created_at))) as session_duration_seconds
                        FROM SessionGroups
                        GROUP BY user_id, session_group_id
                        HAVING COUNT(created_at) > 1 
                    )
                    SELECT COALESCE(AVG(session_duration_seconds), 0.0)
                    FROM SessionDurations
                    WHERE session_duration_seconds >= 10; 
                """)
                result = await session.execute(raw_sql, {
                    "start_date": start_date, "end_date": end_date, 
                    "timeout_seconds": session_timeout_minutes * 60
                })
                avg_duration_sql = result.scalar_one_or_none() or 0.0
                logger.info(f"Средняя длительность сессии (PostgreSQL): {avg_duration_sql:.2f} сек за период {start_date.date()} - {end_date.date()}.")
                return float(avg_duration_sql)
        
        return await self._execute_with_monitoring(_query, f"get_avg_session_duration_{start_date.date()}_{end_date.date()}") # type: ignore


    @handle_errors(reraise_as=DatabaseError)
    async def get_avg_messages_per_active_user_for_period(self, start_date: datetime, end_date: datetime) -> float:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt_msg_count = select(func.count(Message.id)).join(Conversation).join(User).where(
                    Message.created_at >= start_date, Message.created_at < end_date,
                    Message.role == 'user', User.is_active == True)
                total_messages_res = await session.execute(stmt_msg_count)
                total_messages = total_messages_res.scalar_one_or_none() or 0
                
                stmt_active_users = select(func.count(func.distinct(User.id))).join(Conversation).join(Message).where(
                    Message.created_at >= start_date, Message.created_at < end_date,
                    Message.role == 'user', User.is_active == True)
                active_users_res = await session.execute(stmt_active_users)
                active_users_count = active_users_res.scalar_one_or_none() or 0
                return (total_messages / active_users_count) if active_users_count > 0 else 0.0
        return await self._execute_with_monitoring(_query, f"get_avg_msg_per_active_user_{start_date.date()}_{end_date.date()}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_usage_count_for_feature(self, feature_key: str, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Подсчитывает использование фичи на основе UserActionTimestamp."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                action_key_feature = f"feature_used_{feature_key}" 
                
                stmt_total_uses = select(func.count(UserActionTimestamp.id)).where(
                    UserActionTimestamp.action_key == action_key_feature,
                    UserActionTimestamp.timestamp >= start_date,
                    UserActionTimestamp.timestamp < end_date
                )
                total_uses_res = await session.execute(stmt_total_uses)
                total_uses = total_uses_res.scalar_one_or_none() or 0

                stmt_unique_users = select(func.count(func.distinct(UserActionTimestamp.user_id_db))).where(
                    UserActionTimestamp.action_key == action_key_feature,
                    UserActionTimestamp.timestamp >= start_date,
                    UserActionTimestamp.timestamp < end_date
                )
                unique_users_res = await session.execute(stmt_unique_users)
                unique_users = unique_users_res.scalar_one_or_none() or 0
                
                if total_uses == 0 and unique_users == 0: 
                    logger.info(f"Для фичи '{feature_key}' не найдено использований через UserActionTimestamp с ключом '{action_key_feature}'.")
                
                return {"total_uses": total_uses, "unique_users": unique_users}
        
        return await self._execute_with_monitoring(_query, f"get_usage_feature_{feature_key}") # type: ignore


    @handle_errors(reraise_as=DatabaseError)
    async def get_subscription_analytics_for_period(self, start_date: datetime, end_date: datetime) -> Dict[str, Any]:
        """Аналитика подписок за период, используя таблицу Subscription."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                now_utc = datetime.now(timezone.utc)
                
                active_subs_stmt = select(func.count(func.distinct(Subscription.user_id))).where(
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.TRIAL]),
                    or_(Subscription.expires_at == None, Subscription.expires_at > end_date) # type: ignore
                )
                active_subs_res = await session.execute(active_subs_stmt)
                active_subscribers = active_subs_res.scalar_one_or_none() or 0

                new_paid_subs_stmt = select(func.count(Subscription.id)).where(
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.is_trial == False,
                    Subscription.activated_at >= start_date,
                    Subscription.activated_at < end_date
                )
                new_paid_subs_res = await session.execute(new_paid_subs_stmt)
                new_subscribers_in_period = new_paid_subs_res.scalar_one_or_none() or 0
                
                total_revenue_stmt = select(func.sum(Subscription.payment_amount_stars)).where(
                    Subscription.payment_amount_stars != None, # type: ignore
                    Subscription.payment_amount_stars > 0,
                    Subscription.activated_at >= start_date, 
                    Subscription.activated_at < end_date,
                    Subscription.is_trial == False 
                )
                total_revenue_res = await session.execute(total_revenue_stmt)
                total_revenue_in_period_stars = total_revenue_res.scalar_one_or_none() or 0.0
                
                num_months_in_period = max(1, round((end_date - start_date).days / 30.0))
                mrr_stars_approx = total_revenue_in_period_stars / num_months_in_period

                new_users_in_period = await self.get_new_users_count_for_period(start_date, end_date)
                conversion_rate_from_new_to_paid_percent = 0.0
                if new_users_in_period > 0:
                    conversion_rate_from_new_to_paid_percent = (new_subscribers_in_period / new_users_in_period) * 100
                
                tier_dist_stmt = select(Subscription.tier, func.count(func.distinct(Subscription.user_id))).where(
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.TRIAL]),
                    or_(Subscription.expires_at == None, Subscription.expires_at > end_date) # type: ignore
                ).group_by(Subscription.tier)
                tier_dist_res = await session.execute(tier_dist_stmt)
                tier_distribution = {row[0].value: row[1] for row in tier_dist_res.all()}

                return {
                    "active_subscribers": active_subscribers,
                    "new_subscribers_in_period": new_subscribers_in_period,
                    "mrr_stars": round(mrr_stars_approx, 2),
                    "total_revenue_in_period_stars": round(float(total_revenue_in_period_stars), 2),
                    "conversion_rate_from_new_to_paid_percent": round(conversion_rate_from_new_to_paid_percent, 2),
                    "tier_distribution": tier_distribution
                }
        return await self._execute_with_monitoring(_query, f"get_subscription_analytics_{start_date.date()}_{end_date.date()}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_monthly_churn_rate_percent(self, target_month_start_date: datetime) -> float: # pragma: no cover
        """Рассчитывает Churn Rate за указанный месяц."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                month_start = target_month_start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                next_month_start = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
                month_end = next_month_start - timedelta(microseconds=1)
                prev_month_end = month_start - timedelta(microseconds=1)

                s_start_stmt = select(func.count(func.distinct(Subscription.user_id))).where(
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.TRIAL]),
                    or_(Subscription.expires_at == None, Subscription.expires_at > prev_month_end) # type: ignore
                )
                s_start_res = await session.execute(s_start_stmt)
                s_start = s_start_res.scalar_one_or_none() or 0
                if s_start == 0: return 0.0 

                s_end_stmt = select(func.count(func.distinct(Subscription.user_id))).where(
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.TRIAL]),
                    or_(Subscription.expires_at == None, Subscription.expires_at > month_end) # type: ignore
                )
                s_end_res = await session.execute(s_end_stmt)
                s_end = s_end_res.scalar_one_or_none() or 0
                
                n_new_stmt = select(func.count(Subscription.id)).where(
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.is_trial == False,
                    Subscription.activated_at >= month_start,
                    Subscription.activated_at <= month_end 
                )
                n_new_res = await session.execute(n_new_stmt)
                n_new = n_new_res.scalar_one_or_none() or 0
                
                churn_count = s_start + n_new - s_end
                churn_rate = (churn_count / s_start) * 100 if s_start > 0 else 0.0
                
                logger.info(f"Churn calculation for {month_start.strftime('%B %Y')}: S_start={s_start}, S_end={s_end}, N_new={n_new}, Churn_count={churn_count}, Churn_rate={churn_rate:.2f}%")
                return round(max(0.0, churn_rate), 2) 
        return await self._execute_with_monitoring(_query, f"get_monthly_churn_rate_{target_month_start_date.strftime('%Y-%m')}") or 0.0 # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_average_ltv_stars(self) -> float: # pragma: no cover
        """Рассчитывает LTV в звездах."""
        async def _query():
            now = datetime.now(timezone.utc)
            start_30d = now - timedelta(days=30)
            sub_analytics_30d = await self.get_subscription_analytics_for_period(start_30d, now)
            
            total_revenue_30d = sub_analytics_30d.get("total_revenue_in_period_stars", 0.0)
            active_paying_users_30d = sub_analytics_30d.get("active_subscribers", 0)
            
            if active_paying_users_30d == 0 and total_revenue_30d > 0: 
                logger.warning("LTV: active_subscribers is 0 but revenue exists. ARPPU might be inaccurate.")
                stmt_paying_users_in_period = select(func.count(func.distinct(Subscription.user_id))).where(
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.is_trial == False,
                    Subscription.payment_amount_stars > 0, # type: ignore
                    Subscription.activated_at >= start_30d,
                    Subscription.activated_at < now
                )
                async with self.connection_manager.get_session() as session: # type: ignore
                    active_paying_users_30d = (await session.execute(stmt_paying_users_in_period)).scalar_one_or_none() or 1 

            arppu_30d = (total_revenue_30d / active_paying_users_30d) if active_paying_users_30d > 0 else 0.0
            
            last_month_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
            churn_rate_monthly_percent = await self.get_monthly_churn_rate_percent(last_month_start)
            churn_rate_monthly_decimal = churn_rate_monthly_percent / 100.0
            
            if churn_rate_monthly_decimal <= 0.001: 
                logger.warning(f"LTV: Churn rate is very low ({churn_rate_monthly_decimal}). LTV calculation might be very high or skewed.")
                if arppu_30d > 0 : return arppu_30d * 36 
                return 0.0

            ltv = arppu_30d / churn_rate_monthly_decimal
            logger.info(f"LTV Calculation: ARPPU_30d={arppu_30d:.2f}, Churn_Monthly={churn_rate_monthly_percent:.2f}%, LTV={ltv:.2f}")
            return round(max(0.0, ltv), 2)
        return await self._execute_with_monitoring(_query, "get_average_ltv_stars") or 0.0 # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_revenue_by_tier_for_period(self, start_date: datetime, end_date: datetime) -> Dict[str, float]:
        """Суммирует доход по тарифам за период из таблицы Subscription."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(
                    Subscription.tier,
                    func.sum(Subscription.payment_amount_stars).label("total_revenue_for_tier")
                ).where(
                    Subscription.payment_amount_stars != None, # type: ignore
                    Subscription.payment_amount_stars > 0,
                    Subscription.activated_at >= start_date, 
                    Subscription.activated_at < end_date,
                    Subscription.is_trial == False, 
                    Subscription.tier != SubscriptionTier.FREE 
                ).group_by(Subscription.tier)
                
                result = await session.execute(stmt)
                revenue_dict: Dict[str, float] = {tier.value: 0.0 for tier in SubscriptionTier if tier != SubscriptionTier.FREE}
                for row_tier_enum, row_revenue in result.all():
                    if row_tier_enum and row_revenue is not None:
                        revenue_dict[row_tier_enum.value] = float(row_revenue)
                return revenue_dict
        return await self._execute_with_monitoring(_query, f"get_revenue_by_tier_{start_date.date()}_{end_date.date()}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_promocodes_used_in_period(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """Получает информацию о промокодах, использованных при активации платных подписок в периоде."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                # Используем поле applied_promocode_code из таблицы Subscription
                stmt = select(
                    Subscription.applied_promocode_code,
                    func.count(Subscription.id).label("applications_in_period")
                ).where(
                    Subscription.activated_at >= start_date,
                    Subscription.activated_at < end_date,
                    Subscription.is_trial == False, # Только для платных активаций
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.applied_promocode_code != None # type: ignore
                ).group_by(Subscription.applied_promocode_code).order_by(desc(literal_column("applications_in_period")))
                
                result = await session.execute(stmt)
                used_promos_data = [{"code": row.applied_promocode_code, "applications_in_period": row.applications_in_period} for row in result.all()]
                return used_promos_data
        return await self._execute_with_monitoring(_query, f"get_promocodes_used_period_v2_{start_date.date()}_{end_date.date()}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_stats_for_promocode(self, promocode_code: str, start_date: datetime, end_date: datetime) -> Optional[Dict[str, Any]]:
        """Получает статистику для конкретного промокода за период, используя данные из Subscription."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                db_promo = await self.get_promocode_by_code(promocode_code) 
                if not db_promo or db_promo.id is None: 
                    logger.warning(f"Промокод '{promocode_code}' не найден в БД для get_stats_for_promocode.")
                    return None
                
                # Количество применений за период (из таблицы Subscription)
                stmt_apps_period = select(func.count(Subscription.id)).where(
                    Subscription.applied_promocode_id == db_promo.id,
                    Subscription.activated_at >= start_date,
                    Subscription.activated_at < end_date,
                    Subscription.is_trial == False, # Учитываем только платные активации
                    Subscription.tier != SubscriptionTier.FREE
                )
                applications_count_period = (await session.execute(stmt_apps_period)).scalar_one_or_none() or 0

                # Доход, сгенерированный с этим промокодом за период
                stmt_revenue = select(func.sum(Subscription.payment_amount_stars)).where(
                   Subscription.applied_promocode_id == db_promo.id, 
                   Subscription.activated_at >= start_date, Subscription.activated_at < end_date,
                   Subscription.is_trial == False,
                   Subscription.payment_amount_stars > 0 # type: ignore
                )
                revenue_generated_stars_period = (await session.execute(stmt_revenue)).scalar_one_or_none() or 0.0
                
                # Общая сумма скидки, предоставленной этим промокодом за период
                # Используем поле discount_applied_stars из таблицы Subscription
                stmt_discount = select(func.sum(Subscription.discount_applied_stars)).where(
                   Subscription.applied_promocode_id == db_promo.id, 
                   Subscription.activated_at >= start_date, Subscription.activated_at < end_date,
                   Subscription.is_trial == False
                )
                total_discount_stars_period = (await session.execute(stmt_discount)).scalar_one_or_none() or 0.0
                
                return {
                    "code": promocode_code,
                    "id": db_promo.id,
                    "applications_count_period": applications_count_period, 
                    "applications_count_total": db_promo.uses_count, # Общее из таблицы PromoCode
                    "revenue_generated_stars_period": round(float(revenue_generated_stars_period), 2), 
                    "total_discount_stars_period": round(float(total_discount_stars_period), 2) 
                }
        return await self._execute_with_monitoring(_query, f"get_stats_for_promocode_v2_{promocode_code}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_daily_new_users_stats(self, days_lookback: int) -> List[Dict[str, Any]]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                end_date = datetime.now(timezone.utc).date()
                start_date = end_date - timedelta(days=days_lookback -1) 
                all_dates_in_period = [start_date + timedelta(days=i) for i in range(days_lookback)]
                stmt = select(
                    cast(User.created_at, Date).label("registration_date"),
                    func.count(User.id).label("new_users_count")
                ).where(
                    cast(User.created_at, Date) >= start_date,
                    cast(User.created_at, Date) <= end_date 
                ).group_by(
                    cast(User.created_at, Date)
                ).order_by(
                    asc(cast(User.created_at, Date)) # type: ignore
                )
                result = await session.execute(stmt)
                result_map = {row.registration_date: row.new_users_count for row in result.all()}
                final_stats = []
                for dt_obj in all_dates_in_period:
                    final_stats.append({"date": dt_obj.isoformat(), "new_users_count": result_map.get(dt_obj, 0)})
                return final_stats
        return await self._execute_with_monitoring(_query, f"get_daily_new_users_stats_lookback_{days_lookback}") # type: ignore

    @handle_errors(reraise_as=DatabaseError)
    async def get_active_subscribers_with_activity(self, days_for_activity_lookback: int = 30) -> List[Dict[str, Any]]: # pragma: no cover
        """Получает активных платных подписчиков с их активностью."""
        logger.info(f"Fetching active subscribers with activity for the last {days_for_activity_lookback} days.")
        results: List[Dict[str, Any]] = []
        now_utc = datetime.now(timezone.utc)
        
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt_active_subs = select(
                    User.id.label("user_id_db"), User.telegram_id, User.first_name, User.created_at.label("user_created_at"),
                    Subscription.tier, Subscription.activated_at.label("sub_activated_at"), Subscription.expires_at.label("sub_expires_at"),
                    Subscription.is_trial
                ).join(
                    Subscription, User.id == Subscription.user_id
                ).where(
                    User.is_active == True,
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.TRIAL]),
                    Subscription.tier != SubscriptionTier.FREE,
                    or_(Subscription.expires_at == None, Subscription.expires_at > now_utc) # type: ignore
                )
                active_sub_rows = (await session.execute(stmt_active_subs)).all()

                for row in active_sub_rows:
                    user_id_db = row.user_id_db
                    days_until_expiry_val = (row.sub_expires_at.replace(tzinfo=timezone.utc) - now_utc).days if row.sub_expires_at else 9999
                    
                    activity_stats_for_user = await self.get_user_activity_stats(user_id_db, days=days_for_activity_lookback)
                    messages_last_n_days = activity_stats_for_user.get("message_count", 0)
                    active_days_last_n = activity_stats_for_user.get("active_days", 0)
                    
                    activity_prev_7_days_lookback = await self.get_user_activity_stats(user_id_db, days=7)
                    messages_previous_7_days_for_comparison = activity_prev_7_days_lookback.get("message_count", 0) if days_for_activity_lookback >=7 else 0


                    avg_session_duration_real = await self.get_avg_session_duration_for_period(now_utc - timedelta(days=days_for_activity_lookback), now_utc) 
                    feature_usage_story = await self.get_usage_count_for_feature("story_creation", now_utc - timedelta(days=days_for_activity_lookback), now_utc) 
                    feature_usage_memory_save = await self.get_usage_count_for_feature("memory_save", now_utc - timedelta(days=days_for_activity_lookback), now_utc)


                    results.append({
                        "user_id_db": user_id_db, "telegram_id": row.telegram_id, "user_first_name": row.first_name,
                        "user_created_at": row.user_created_at.isoformat() if row.user_created_at else None,
                        "current_tier": row.tier.value, "is_trial": row.is_trial,
                        "subscription_start_date": row.sub_activated_at.isoformat() if row.sub_activated_at else None,
                        "days_until_expiry": days_until_expiry_val,
                        f"messages_last_{days_for_activity_lookback}d": messages_last_n_days, 
                        "messages_previous_7_days_for_trend": messages_previous_7_days_for_comparison, 
                        f"active_days_last_{days_for_activity_lookback}d": active_days_last_n, 
                        "avg_session_duration_minutes_last_30d": round(avg_session_duration_real / 60, 1) if avg_session_duration_real else 0.0, 
                        "feature_usage_last_30d": {
                            "story_creation_total_uses": feature_usage_story.get("total_uses",0),
                            "story_creation_unique_users": feature_usage_story.get("unique_users",0),
                            "memory_save_total_uses": feature_usage_memory_save.get("total_uses",0),
                            "memory_save_unique_users": feature_usage_memory_save.get("unique_users",0),
                            }, 
                        "support_tickets_last_30_days": 0, # Заглушка
                        "failed_payments_last_30_days": 0  # Заглушка
                    })
                return results
        return await self._execute_with_monitoring(_query, f"get_active_subscribers_with_activity_lookback_{days_for_activity_lookback}") # type: ignore
    
    async def get_all_users_with_extended_metrics(self, days_lookback: int = 30) -> List[Dict[str, Any]]:
        """Собирает расширенные метрики для ВСЕХ пользователей."""
        logger.info(f"Fetching extended metrics for all users (lookback: {days_lookback} days). This can be resource-intensive.")
        all_users_data: List[Dict[str, Any]] = []
        now_utc = datetime.now(timezone.utc)
        
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                all_users_stmt = select(User)
                all_users_result = await session.execute(all_users_stmt)
                users_list = list(all_users_result.scalars().all())

                for user_db_obj in users_list:
                    user_data_entry: Dict[str, Any] = {
                        "telegram_id": user_db_obj.telegram_id,
                        "user_id_db": user_db_obj.id,
                        "user_info": {
                            "created_at": user_db_obj.created_at.isoformat() if user_db_obj.created_at else None,
                            "last_activity": user_db_obj.last_activity.isoformat() if user_db_obj.last_activity else None,
                            "is_active": user_db_obj.is_active,
                            "language_code": user_db_obj.language_code,
                            "first_name": user_db_obj.first_name,
                            "country_code": user_db_obj.country_code # Добавлено
                        },
                        "subscription": {}, "activity": {}, "monetization": {}
                    }
                    active_sub = await self.get_active_subscription_for_user(user_db_obj.id) 
                    if active_sub:
                        user_data_entry["subscription"] = {
                            "tier": active_sub.tier.value, "status": active_sub.status.value,
                            "is_trial": active_sub.is_trial,
                            "expires_at": active_sub.expires_at.isoformat() if active_sub.expires_at else None,
                            "days_until_expiry": (active_sub.expires_at.replace(tzinfo=timezone.utc) - now_utc).days if active_sub.expires_at else 9999,
                            "activated_at": active_sub.activated_at.isoformat() if active_sub.activated_at else None,
                            "applied_promocode_code": active_sub.applied_promocode_code # Добавлено
                        }
                    else: 
                         user_data_entry["subscription"] = {"tier": SubscriptionTier.FREE.value, "status": SubscriptionStatus.ACTIVE.value, "is_trial": False}
                    
                    activity_stats = await self.get_user_activity_stats(user_db_obj.id, days_lookback)
                    avg_session_real = await self.get_avg_session_duration_for_period(now_utc - timedelta(days=days_lookback), now_utc)
                    feature_usage_story = await self.get_usage_count_for_feature("story_creation", now_utc - timedelta(days=days_lookback), now_utc)
                    feature_usage_memory_save = await self.get_usage_count_for_feature("memory_save", now_utc - timedelta(days=days_lookback), now_utc)

                    user_data_entry["activity"] = {
                        f"messages_last_{days_lookback}d": activity_stats.get("message_count", 0),
                        f"active_days_last_{days_lookback}d": activity_stats.get("active_days", 0),
                        "feature_usage_last_30d": { 
                            "story_creation_total_uses": feature_usage_story.get("total_uses",0),
                            "memory_save_total_uses": feature_usage_memory_save.get("total_uses",0),
                        },
                        "avg_session_duration_minutes_last_30d": round((avg_session_real or 0.0) / 60, 1) ,
                    }
                    
                    total_paid_months = await self.get_total_paid_subscription_months(user_db_obj.id) 
                    promocodes_used_overall = await self.get_user_promocode_usage_count_overall(user_db_obj.id) 
                    total_paid_subs_count = await self.get_count_of_paid_subscriptions_for_user(user_db_obj.id)
                    
                    # Суммарные траты пользователя (из истории подписок)
                    user_subscriptions_history = await self.get_all_user_subscriptions_history(user_db_obj.id)
                    total_spent_stars_user = sum(
                        sub.payment_amount_stars for sub in user_subscriptions_history 
                        if sub.payment_amount_stars and sub.payment_amount_stars > 0 and not sub.is_trial
                    )
                    # LTV (очень грубая оценка, если нет более точных данных churn)
                    ltv_approx = total_spent_stars_user * random.uniform(1.2, 2.5) if total_paid_subs_count > 0 else 0.0


                    user_data_entry["monetization"] = {
                        "ltv_stars": round(ltv_approx,0), 
                        "total_paid_subscriptions_count": total_paid_subs_count, 
                        "total_spent_stars": total_spent_stars_user, 
                        "promocodes_used_count": promocodes_used_overall, 
                        "total_subscribed_months_paid": total_paid_months 
                    }
                    all_users_data.append(user_data_entry)
                return all_users_data
        
        logger.info(f"Начало выполнения get_all_users_with_extended_metrics (lookback {days_lookback} дней)...")
        result_data = await _query()
        logger.info(f"Завершено выполнение get_all_users_with_extended_metrics. Получено данных для {len(result_data)} пользователей.")
        return result_data

    async def get_total_paid_subscription_months(self, user_id_db: int) -> int:
        """Рассчитывает общее количество оплаченных месяцев подписки для пользователя."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(Subscription.activated_at, Subscription.expires_at).where(
                    Subscription.user_id == user_id_db,
                    Subscription.is_trial == False,
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRED, SubscriptionStatus.GRACE_PERIOD]),
                    Subscription.payment_amount_stars > 0 # type: ignore
                ).order_by(Subscription.activated_at)
                
                subscriptions_history = (await session.execute(stmt)).all()
                total_paid_days = 0
                # TODO: Улучшить логику для корректного суммирования пересекающихся или апгрейднутых подписок
                # Простая сумма длительностей может быть неточной.
                for sub_start, sub_end in subscriptions_history:
                    if sub_start and sub_end and sub_end > sub_start:
                        duration_days = (sub_end - sub_start).days
                        total_paid_days += duration_days
                return round(total_paid_days / 30.44) 
        return await self._execute_with_monitoring(_query, f"get_total_paid_sub_months_user_{user_id_db}") or 0 # type: ignore

    async def get_last_paid_subscription_ended_in_period(self, user_id_db: int, days_period_lookback: int) -> Optional[Subscription]:
        """Находит последнюю платную (не триальную) подписку, которая ЗАКОНЧИЛАСЬ в указанном периоде."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                now_utc = datetime.now(timezone.utc)
                period_start_date = now_utc - timedelta(days=days_period_lookback)
                
                stmt = select(Subscription).where(
                    Subscription.user_id == user_id_db,
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.is_trial == False,
                    Subscription.payment_amount_stars > 0, # type: ignore
                    Subscription.expires_at != None, 
                    Subscription.expires_at >= period_start_date, 
                    Subscription.expires_at < now_utc 
                ).order_by(desc(Subscription.expires_at)) 
                
                result = await session.execute(stmt)
                return result.scalar_one_or_none()
        return await self._execute_with_monitoring(_query, f"get_last_paid_sub_ended_user_{user_id_db}_period_{days_period_lookback}") # type: ignore

    async def get_user_promocode_usage_count_overall(self, user_id_db: int) -> int:
        """Получает общее количество уникальных промокодов, использованных пользователем, на основе записей в Subscription."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(func.count(func.distinct(Subscription.applied_promocode_id))).where(
                    Subscription.user_id == user_id_db,
                    Subscription.applied_promocode_id != None # type: ignore
                )
                result = await session.execute(stmt)
                return result.scalar_one_or_none() or 0
        return await self._execute_with_monitoring(_query, f"get_user_unique_promo_usage_overall_user_{user_id_db}") or 0 # type: ignore

    async def get_count_of_paid_subscriptions_for_user(self, user_id_db: int) -> int:
        """Получает общее количество платных (не триальных) подписок у пользователя за всю историю."""
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(func.count(Subscription.id)).where(
                    Subscription.user_id == user_id_db,
                    Subscription.is_trial == False,
                    Subscription.tier != SubscriptionTier.FREE,
                    Subscription.payment_amount_stars > 0 # type: ignore
                )
                result = await session.execute(stmt)
                return result.scalar_one_or_none() or 0
        return await self._execute_with_monitoring(_query, f"get_count_paid_subs_user_{user_id_db}") or 0 # type: ignore


    async def get_user_activity_stats(self, user_id_db: int, days: int) -> Dict[str, Any]: # pragma: no cover
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                now = datetime.now(timezone.utc)
                start_date = now - timedelta(days=days)
                
                stmt_msg_count = select(func.count(Message.id)).join(Conversation).where(
                    Conversation.user_id == user_id_db,
                    Message.role == 'user',
                    Message.created_at >= start_date
                )
                messages_count = (await session.execute(stmt_msg_count)).scalar_one_or_none() or 0
                
                stmt_active_days = select(func.count(func.distinct(cast(Message.created_at, Date)))).join(Conversation).where(
                     Conversation.user_id == user_id_db,
                     Message.role == 'user',
                     Message.created_at >= start_date
                )
                active_days_count = (await session.execute(stmt_active_days)).scalar_one_or_none() or 0
                
                return {"message_count": messages_count, "active_days": active_days_count}
        return await self._execute_with_monitoring(_query, f"get_user_activity_stats_user_{user_id_db}_days_{days}") # type: ignore

    async def get_active_user_db_ids(self, days_inactive_threshold: int = 30) -> List[int]: # pragma: no cover
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_inactive_threshold)
                stmt = select(User.id).where(User.is_active == True, User.last_activity >= cutoff_date)
                result = await session.execute(stmt); return [uid for uid, in result.all()] # type: ignore
        return await self._execute_with_monitoring(_query, "get_active_user_db_ids") # type: ignore

    async def get_all_user_preferences_by_key(self, key: str, persona: Optional[str] = None) -> List[Tuple[int, Any]]:
        async def _query():
            async with self.connection_manager.get_session() as session: # type: ignore
                stmt = select(UserPreference.user_id, UserPreference.preference_value, UserPreference.preference_type).where(
                    UserPreference.preference_key == key
                )
                if persona:
                    stmt = stmt.where(UserPreference.persona == persona)
                
                result = await session.execute(stmt)
                parsed_results = []
                for user_id_db, value_str, pref_type in result.all():
                    parsed_value = value_str
                    try:
                        if pref_type == 'json' and value_str: parsed_value = json.loads(value_str)
                        elif pref_type == 'bool' and value_str: parsed_value = value_str.lower() == 'true'
                        elif pref_type == 'int' and value_str: parsed_value = int(value_str)
                        elif pref_type == 'float' and value_str: parsed_value = float(value_str)
                    except (json.JSONDecodeError, ValueError, TypeError) as e:
                        logger.warning(f"Ошибка парсинга UserPreference для user_id_db {user_id_db}, key {key}: {e}")
                        continue 
                    parsed_results.append((user_id_db, parsed_value))
                return parsed_results
        return await self._execute_with_monitoring(_query, f"get_all_user_prefs_by_key_{key}_{persona or 'all'}") # type: ignore

    async def delete_user_preferences_older_than_by_datetime_value_and_key_prefix(
        self, persona: str, key_prefix: str, cutoff_date: datetime, preference_type_filter: Optional[str] = 'string'
    ) -> int: # pragma: no cover
        async def _query():
            async with self.connection_manager.get_transaction() as session: # type: ignore
                stmt_select_ids = select(UserPreference.id, UserPreference.preference_value).where(
                    UserPreference.persona == persona,
                    UserPreference.preference_key.like(f"{key_prefix}%") # type: ignore
                )
                if preference_type_filter:
                    stmt_select_ids = stmt_select_ids.where(UserPreference.preference_type == preference_type_filter)
                
                result_select = await session.execute(stmt_select_ids)
                ids_to_delete = []
                for pref_id, value_str in result_select.all():
                    if not value_str: continue
                    try:
                        date_value_to_check = value_str 
                        if preference_type_filter == 'json':
                            json_data = json.loads(value_str)
                            date_value_to_check = json_data.get('timestamp', json_data.get('date', json_data.get('used_at'))) 

                        if not date_value_to_check or not isinstance(date_value_to_check, str): continue

                        dt_value = datetime.fromisoformat(date_value_to_check.replace('Z', '+00:00'))
                        if dt_value.tzinfo is None: dt_value = dt_value.replace(tzinfo=timezone.utc)
                        
                        if dt_value < cutoff_date:
                            ids_to_delete.append(pref_id)
                    except (json.JSONDecodeError, ValueError, TypeError) as e:
                        logger.debug(f"Не удалось распарсить дату из UserPreference ID {pref_id} для удаления: {e}. Value: {value_str[:50]}")
                        continue
                
                if not ids_to_delete: return 0
                
                stmt_delete = delete(UserPreference).where(UserPreference.id.in_(ids_to_delete)) # type: ignore
                result_delete = await session.execute(stmt_delete)
                deleted_count = result_delete.rowcount
                if deleted_count > 0:
                    logger.info(f"Удалено {deleted_count} UserPreference записей (persona: {persona}, prefix: {key_prefix}, older than: {cutoff_date.isoformat()}).")
                return deleted_count
        return await self._execute_with_monitoring(_query, f"delete_old_prefs_by_date_prefix_{persona}_{key_prefix}") # type: ignore


    async def close(self): # pragma: no cover
        await self.connection_manager.close()

    def get_service_stats(self) -> Dict[str, Any]: # pragma: no cover
        return {"performance_stats": self.performance_stats.copy()}
