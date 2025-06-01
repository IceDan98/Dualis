import logging
import asyncio
from typing import Optional, Dict, Any
import aiohttp
# import json # json не используется напрямую в этом файле, убрал
from io import BytesIO
from datetime import datetime, timezone

# Корректные импорты из вашего проекта
from config.settings import BotConfig # Для доступа к API ключам и ID голоса
from utils.error_handler import handle_errors, APIError, minimax_circuit_breaker # safe_api_call здесь не используется

logger = logging.getLogger(__name__)

class TTSService:
    """Сервис для работы с синтезом речи Minimax"""
    
    def __init__(self, config: BotConfig): # Явно указываем тип для config
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None # Явная инициализация None и типизация
        
        # Настройки Minimax TTS
        self.base_url = "https://api.minimax.chat/v1/text_to_speech" # URL API Minimax
        
        # Голосовые модели для персон. Используем ID из config для Diana.
        # Для Madina можно будет указать другой ID, если он есть, или использовать тот же.
        self.voice_models: Dict[str, Dict[str, Any]] = {
            'diana': {
                'voice_id': self.config.minimax_voice_id, # Используем ID голоса из BotConfig
                'speed': 1.0,
                'pitch': 0.0,
                'emotion': 'friendly' # Пример эмоции, Minimax API может не поддерживать это поле явно
            },
            'madina': {
                # Если для Madina есть свой ID, его нужно добавить в BotConfig и использовать здесь
                # Пока что можно использовать тот же или другой стандартный, если известен
                'voice_id': self.config.minimax_voice_id, # Пример: используем тот же голос, можно изменить
                # 'voice_id': 'female_seductive_mature', # Старый вариант, если minimax_voice_id только для Diana
                'speed': 0.95, # Немного медленнее для соблазнительности
                'pitch': -0.05, # Немного ниже
                'emotion': 'passionate' # Пример эмоции
            }
        }
        
        # Статистика использования TTS
        self.usage_stats: Dict[str, Any] = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_characters_synthesized': 0, # Переименовано для ясности
            # 'total_audio_duration_ms': 0.0 # Длительность аудио сложнее получить без анализа файла
            'last_reset': datetime.now(timezone.utc) # Добавлено для отслеживания сброса
        }
    
    async def initialize(self):
        """Инициализация HTTP сессии для TTS сервиса."""
        if not self.session or self.session.closed:
            connect_timeout = getattr(self.config, 'tts_connect_timeout', 10)
            total_timeout = getattr(self.config, 'tts_request_timeout', 120) # Увеличенный таймаут для TTS
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=total_timeout, connect=connect_timeout)
            )
        logger.info(f"TTS Service (Minimax) инициализирован. Голос Diana: {self.voice_models['diana']['voice_id']}")

    @handle_errors() # Декоратор для обработки ошибок
    async def synthesize_speech(self, 
                               text: str, 
                               persona: str = 'diana',
                               custom_voice_settings: Optional[Dict[str, Any]] = None) -> Optional[BytesIO]:
        """
        Синтезирует речь из текста с использованием Minimax API.
        
        Args:
            text: Текст для озвучивания.
            persona: Персона ('diana' или 'madina'), для выбора настроек голоса.
            custom_voice_settings: Словарь для переопределения стандартных настроек голоса.
        
        Returns:
            BytesIO объект с аудиоданными в формате MP3 или None в случае ошибки.
        """
        
        if not self.session or self.session.closed:
            await self.initialize()
            if not self.session: # Если сессия все еще не создана
                 logger.error("TTS Service: HTTP сессия не инициализирована для синтеза речи.")
                 raise APIError("TTS Service: HTTP сессия не доступна.")


        self.usage_stats['total_requests'] += 1
        
        try:
            # Подготовка настроек голоса: берем стандартные для персоны и обновляем кастомными, если есть
            voice_settings_for_request = self.voice_models.get(persona, self.voice_models['diana']).copy() # Фолбэк на Диану
            if custom_voice_settings:
                voice_settings_for_request.update(custom_voice_settings)
            
            # Проверяем, есть ли voice_id в настройках
            if 'voice_id' not in voice_settings_for_request or not voice_settings_for_request['voice_id']:
                logger.error(f"TTS Service: voice_id не определен для персоны {persona} или в кастомных настройках.")
                raise APIError(f"voice_id не настроен для синтеза речи персоны {persona}.")

            # Вызов Minimax API через Circuit Breaker
            audio_data_bytes = await minimax_circuit_breaker.call(
                self._call_minimax_tts, # Метод для вызова
                text_to_synthesize=text, # Переименованный аргумент для ясности
                voice_params=voice_settings_for_request # Переименованный аргумент
            )
            
            if audio_data_bytes:
                self.usage_stats['successful_requests'] += 1
                self.usage_stats['total_characters_synthesized'] += len(text)
                logger.info(f"Синтезирована речь для персоны '{persona}': {len(text)} символов.")
                return BytesIO(audio_data_bytes)
            else:
                # _call_minimax_tts должен был бы поднять исключение, если что-то пошло не так,
                # но на всякий случай обрабатываем None.
                self.usage_stats['failed_requests'] += 1
                logger.error(f"TTS Service: _call_minimax_tts вернул None для персоны {persona}.")
                return None # Или поднять APIError("Не удалось получить аудиоданные от Minimax")
            
        except Exception as e: # Ловим все исключения, включая APIError из _call_minimax_tts
            self.usage_stats['failed_requests'] += 1
            # Ошибка уже должна быть залогирована в @handle_errors или в _call_minimax_tts
            # Просто переподнимаем ее, если это APIError, или оборачиваем в APIError
            if not isinstance(e, APIError):
                logger.error(f"Неожиданная ошибка синтеза речи для персоны {persona}: {e}", exc_info=True)
                raise APIError(f"Неожиданная ошибка синтеза речи: {e}") from e
            raise
    
    async def _call_minimax_tts(self, text_to_synthesize: str, voice_params: Dict[str, Any]) -> bytes:
        """
        Непосредственный вызов Minimax TTS API.
        Этот метод предназначен для вызова через CircuitBreaker.
        """
        if not self.session: # Дополнительная проверка на случай прямого вызова (хотя не предполагается)
            raise APIError("TTS Service: HTTP сессия не активна для вызова Minimax API.")

        # Подготовка payload для Minimax API
        # Убедимся, что все параметры соответствуют документации Minimax
        # https://api.minimax.chat/document/ vezet?doc_id=speech01_tts
        # "timber_weights" и " پاکستانی" - это примеры из их док-и, нужно адаптировать
        # "voice_id" должен быть из voice_params
        # "text" - текст для синтеза
        # "model": "speech-01" (или другая актуальная модель)
        # "speed", "pitch" - опционально
        # "audio_sample_rate", "bitrate", "format" - можно задать по умолчанию или из конфига
        
        payload = {
            "voice_id": voice_params['voice_id'],
            "text": text_to_synthesize,
            "model": "speech-01", # Уточнить актуальную модель, если нужно
            "speed": voice_params.get('speed', 1.0),
            "pitch": voice_params.get('pitch', 0.0),
            # "vol": voice_params.get('volume', 1.0), # Если API поддерживает громкость
            # "emotion": voice_params.get('emotion'), # Если API поддерживает эмоции и они есть в voice_params
            "audio_setting": { # Пример структуры из документации, если нужна такая
                 "sample_rate": voice_params.get('audio_sample_rate', 32000), # 24000 или 32000 или 48000
                 "bitrate": voice_params.get('bitrate_kbps', 128), # в kbps
                 "format": voice_params.get('format', "mp3"), # mp3, wav, pcm, aac
                 "channel": 1 # моно
            }
            # "timber_weights": [{"voice_id": "male-qn-qingse", "weight": 1}] # Пример смешивания голосов, если нужно
        }
        
        # Удаляем параметры, которые не нужны или имеют значение None, если API их не принимает
        if 'emotion' not in voice_params or voice_params['emotion'] is None:
            # Minimax может не иметь прямого параметра "emotion" в таком виде
            pass # Не добавляем, если API не поддерживает

        headers = {
            "Authorization": f"Bearer {self.config.minimax_api_key}",
            "Content-Type": "application/json",
            "GroupId": self.config.minimax_group_id # GroupId передается в заголовке
        }
        
        request_url = self.base_url # Используем базовый URL
        
        logger.debug(f"TTS запрос к Minimax: URL={request_url}, Payload={payload}")

        try:
            async with self.session.post(request_url, json=payload, headers=headers) as response:
                response_status = response.status
                
                if response_status == 200:
                    # Проверяем Content-Type, чтобы убедиться, что это аудио
                    if 'audio' not in response.headers.get('Content-Type', '').lower():
                        error_text_on_200 = await response.text()
                        logger.error(f"Minimax TTS API вернул статус 200, но Content-Type не аудио: {response.headers.get('Content-Type')}. Ответ: {error_text_on_200[:200]}")
                        raise APIError(f"Minimax TTS API вернул некорректный Content-Type: {response.headers.get('Content-Type')}", error_code='INVALID_TTS_RESPONSE_CONTENT_TYPE')
                    
                    audio_data = await response.read() # Читаем бинарные данные
                    if not audio_data:
                        logger.error("Minimax TTS API вернул статус 200, но пустые аудиоданные.")
                        raise APIError("Minimax TTS API вернул пустые аудиоданные.", error_code='EMPTY_AUDIO_DATA')
                    return audio_data
                else:
                    # Обработка ошибок API
                    error_text = await response.text()
                    logger.error(f"Ошибка Minimax TTS API (статус {response_status}): {error_text[:500]}") # Логируем часть ответа
                    if response_status == 400:
                         raise APIError(f"Ошибка запроса к Minimax TTS (400): {error_text}", error_code='MINIMAX_BAD_REQUEST')
                    elif response_status == 401:
                         raise APIError("Ошибка аутентификации с Minimax TTS API. Проверьте API ключ и Group ID.", error_code='MINIMAX_AUTH_ERROR')
                    elif response_status == 429:
                         raise APIError("Превышен лимит запросов к Minimax TTS API.", error_code='MINIMAX_RATE_LIMIT')
                    else:
                         raise APIError(f"Ошибка Minimax TTS API (статус {response_status}): {error_text}", error_code=f'MINIMAX_API_ERROR_{response_status}')
                    
        except aiohttp.ClientError as e: # Ошибки сети, таймауты и т.д.
            logger.error(f"Сетевая ошибка при вызове Minimax TTS API: {e}", exc_info=True)
            raise APIError(f"Сетевая ошибка при обращении к Minimax TTS: {e}", error_code='TTS_NETWORK_ERROR')
        except asyncio.TimeoutError:
            logger.error("Таймаут запроса к Minimax TTS API.", exc_info=True)
            raise APIError("Таймаут запроса к Minimax TTS.", error_code='TTS_TIMEOUT')
    
    def should_use_voice(self, text: str, persona: str) -> bool:
        """Определяет, стоит ли озвучивать данное сообщение для указанной персоны."""
        
        # Не озвучиваем слишком длинные тексты (лимит можно вынести в конфиг)
        max_tts_length = getattr(self.config, 'tts_max_text_length', 450) # Minimax имеет лимит около 500 символов
        if len(text) > max_tts_length:
            logger.info(f"TTS пропущено: текст слишком длинный ({len(text)} > {max_tts_length}).")
            return False
        
        # Не озвучиваем служебные сообщения или команды
        if text.startswith(('[', '/', '!', '#', 'ID ошибки:')) or '```' in text:
            logger.info(f"TTS пропущено: текст содержит служебные маркеры.")
            return False
        
        # Можно добавить другие правила, например, не озвучивать очень короткие ответы ("Ок", "Да")
        if len(text.split()) < 2 and len(text) < 10 : # Меньше 2 слов и 10 символов
            logger.info(f"TTS пропущено: текст слишком короткий ('{text}').")
            return False

        # TODO: Можно добавить настройку в UserPreference, хочет ли пользователь озвучку для этой персоны
        
        return True # По умолчанию озвучиваем, если нет причин не делать этого
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Возвращает статистику использования TTS сервиса."""
        stats = self.usage_stats.copy()
        total_successful = stats['successful_requests']
        
        stats['success_rate_percentage'] = (total_successful / stats['total_requests'] * 100) if stats['total_requests'] > 0 else 0.0
        stats['avg_chars_per_successful_request'] = (stats['total_characters_synthesized'] / total_successful) if total_successful > 0 else 0.0
        return stats
    
    def reset_usage_stats(self):
        """Сбрасывает статистику использования TTS."""
        self.usage_stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_characters_synthesized': 0,
            'last_reset': datetime.now(timezone.utc)
        }
        logger.info("Статистика использования TTS сервиса сброшена.")
    
    async def close(self):
        """Закрывает HTTP сессию, если она была открыта."""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None # Явно обнуляем сессию
            logger.info("HTTP сессия TTS сервиса закрыта.")

# Пример использования (для отладки этого файла)
async def _test_tts_service():
    # Загрузка конфигурации (упрощенная, для теста)
    # В реальном приложении BotConfig будет приходить извне
    class MockBotConfig: # Определяем класс здесь для теста
        minimax_api_key = os.getenv("MINIMAX_API_KEY")
        minimax_group_id = os.getenv("MINIMAX_GROUP_ID")
        minimax_voice_id = os.getenv("MINIMAX_VOICE_ID", "moss_audio_d62febbe-3598-11f0-9505-4e9b7ef777f4") # Используем переменную окружения или дефолт
        tts_max_text_length = 450
        # Добавьте другие поля, если они используются в TTSService

    if not MockBotConfig.minimax_api_key or not MockBotConfig.minimax_group_id:
        print("Переменные окружения MINIMAX_API_KEY и MINIMAX_GROUP_ID не установлены. Тест TTSService не может быть выполнен.")
        return

    config = MockBotConfig()
    tts_service = TTSService(config)
    
    try:
        await tts_service.initialize()
        
        print("\n--- Тест синтеза речи (Diana) ---")
        text_to_say_diana = "Привет, Даурен! Это тестовое сообщение от Дианы. Как твои дела сегодня?"
        if tts_service.should_use_voice(text_to_say_diana, "diana"):
            audio_stream_diana = await tts_service.synthesize_speech(text_to_say_diana, persona="diana")
            if audio_stream_diana:
                with open("test_diana_speech.mp3", "wb") as f:
                    f.write(audio_stream_diana.getvalue())
                print("Речь Diana сохранена в test_diana_speech.mp3")
            else:
                print("Не удалось синтезировать речь Diana.")
        else:
            print("Текст для Diana не подходит для озвучивания.")

        print("\n--- Тест синтеза речи (Madina) ---")
        text_to_say_madina = "Ммм, здравствуй, мой дорогой Даурен... Мадина здесь, готова шептать тебе на ушко."
        if tts_service.should_use_voice(text_to_say_madina, "madina"):
            # Можно передать кастомные настройки, если нужно переопределить стандартные для Madina
            # custom_madina_voice = {'pitch': -0.1, 'speed': 0.9}
            audio_stream_madina = await tts_service.synthesize_speech(text_to_say_madina, persona="madina") #, custom_voice_settings=custom_madina_voice)
            if audio_stream_madina:
                with open("test_madina_speech.mp3", "wb") as f:
                    f.write(audio_stream_madina.getvalue())
                print("Речь Madina сохранена в test_madina_speech.mp3")
            else:
                print("Не удалось синтезировать речь Madina.")
        else:
            print("Текст для Madina не подходит для озвучивания.")
            
        print("\n--- Тест слишком длинного текста ---")
        long_text = "Это очень длинный текст, который предназначен для проверки ограничения на максимальную длину синтезируемой речи. " * 10
        print(f"Длина текста: {len(long_text)}")
        if tts_service.should_use_voice(long_text, "diana"):
            print("Ошибка: should_use_voice пропустил слишком длинный текст.")
        else:
            print("Корректно: слишком длинный текст не будет озвучен.")


    except APIError as e:
        print(f"ОШИБКА API TTS: {e.message} (Код: {e.error_code})")
    except Exception as e:
        print(f"НЕПРЕДВИДЕННАЯ ОШИБКА ТЕСТА TTS: {e}")
    finally:
        await tts_service.close()
        print(f"\nСтатистика использования TTS: {tts_service.get_usage_stats()}")

if __name__ == "__main__":
    # Для запуска теста этого файла:
    # Убедитесь, что у вас есть .env файл с MINIMAX_API_KEY, MINIMAX_GROUP_ID, MINIMAX_VOICE_ID
    # python -m services.tts_service (если запускать как модуль из корня проекта)
    # или python path/to/services/tts_service.py
    
    # Настройка простого логирования для теста
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    # Загрузка .env если он не был загружен глобально
    from dotenv import load_dotenv
    from datetime import timezone # Добавлен импорт для datetime.now(timezone.utc)
    load_dotenv()

    asyncio.run(_test_tts_service())
