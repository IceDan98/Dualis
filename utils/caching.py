# utils/caching.py
import asyncio
from functools import wraps
from typing import Callable, Any, Tuple, Dict, Optional
from cachetools import TTLCache, LFUCache, RRCache # Добавим разные варианты
import logging

logger = logging.getLogger(__name__)

# Разные типы кэшей для разных нужд
# Кэш для данных подписки пользователя (часто запрашивается, не очень много ключей)
# user_subscription_cache = TTLCache(maxsize=1000, ttl=300) # 1000 пользователей, TTL 5 минут

# Кэш для настроек пользователя (аналогично)
# user_settings_cache = TTLCache(maxsize=2000, ttl=300) # (user_id, persona)

# Вместо глобальных кэшей, лучше инкапсулировать их в сервисах или передавать как зависимость

def async_ttl_cache(maxsize: int = 128, ttl: int = 300, cache_instance: Optional[TTLCache] = None):
    """
    Асинхронный декоратор для кэширования с TTL (Time-To-Live).
    Если cache_instance предоставлен, использует его, иначе создает новый TTLCache.
    Ключи кэша генерируются на основе аргументов декорируемой функции.

    Args:
        maxsize: Максимальный размер кэша.
        ttl: Время жизни записи в кэше в секундах.
        cache_instance: Опциональный существующий экземпляр TTLCache.
    """
    _cache = cache_instance if cache_instance is not None else TTLCache(maxsize=maxsize, ttl=ttl)
    _lock = asyncio.Lock() # Для предотвращения гонки состояний при доступе к кэшу

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Создаем ключ кэша на основе аргументов функции
            # Важно: аргументы должны быть хешируемыми и их порядок важен.
            # Для простоты пока будем использовать кортеж из args и отсортированных kwargs.
            # В реальных условиях может потребоваться более сложная генерация ключа.
            key_parts = list(args)
            if kwargs:
                for k, v in sorted(kwargs.items()): # Сортируем kwargs для консистентности ключа
                    key_parts.append(f"{k}={v}")
            cache_key = tuple(key_parts)
            
            async with _lock:
                if cache_key in _cache:
                    logger.debug(f"CACHE HIT: func='{func.__name__}', key='{str(cache_key)[:100]}...'")
                    return _cache[cache_key]

            logger.debug(f"CACHE MISS: func='{func.__name__}', key='{str(cache_key)[:100]}...'")
            result = await func(*args, **kwargs)
            
            async with _lock:
                _cache[cache_key] = result
            return result

        # Добавляем методы для управления кэшем к обертке
        async def clear_cache():
            async with _lock:
                _cache.clear()
            logger.info(f"Cache cleared for function '{func.__name__}' (instance: {id(_cache)})")

        async def invalidate_key(*args_key, **kwargs_key):
            key_parts_inv = list(args_key)
            if kwargs_key:
                for k, v in sorted(kwargs_key.items()):
                    key_parts_inv.append(f"{k}={v}")
            cache_key_inv = tuple(key_parts_inv)
            async with _lock:
                if cache_key_inv in _cache:
                    del _cache[cache_key_inv]
                    logger.info(f"Cache key invalidated for func='{func.__name__}', key='{str(cache_key_inv)[:100]}...'")
                    return True
            logger.debug(f"Cache key for invalidation not found: func='{func.__name__}', key='{str(cache_key_inv)[:100]}...'")
            return False
        
        async def get_cache_instance() -> TTLCache:
            return _cache


        wrapper.clear_cache = clear_cache # type: ignore
        wrapper.invalidate_key = invalidate_key # type: ignore
        wrapper.get_cache_instance = get_cache_instance # type: ignore
        wrapper._original_func = func # Сохраняем ссылку на оригинальную функцию
        
        return wrapper
    return decorator
