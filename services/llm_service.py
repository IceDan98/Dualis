# services/llm_service.py
import logging
import asyncio
from typing import List, Dict, Optional, Any
import aiohttp
import json
import re
from datetime import datetime, timezone

from config.settings import BotConfig
from config.prompts import prompt_manager
from utils.error_handler import handle_errors, safe_api_call, APIError, gemini_circuit_breaker
from utils.token_counter import TokenCounter # Assuming TokenCounter is now a class

logger = logging.getLogger(__name__)

class LLMService:
    """Сервис для работы с языковыми моделями (Gemini)"""

    def __init__(self, config: BotConfig):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self.gemini_base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        self.gemini_model = getattr(config, 'gemini_model_name', "gemini-1.5-flash-latest")
        self._system_prompts_cache: Dict[str, str] = {}
        self.token_counter_instance = TokenCounter( # Initialize TokenCounter here
            gemini_api_key=config.gemini_api_key,
            gemini_model_name_for_counting=getattr(config, 'gemini_model_name', self.gemini_model) # Use the same model for counting if not specified otherwise
        )

        self.usage_stats: Dict[str, Any] = {
            'total_requests': 0, 'successful_requests': 0, 'failed_requests': 0,
            'total_input_tokens': 0, 'total_output_tokens': 0,
            'last_reset': datetime.now(timezone.utc)
        }

    async def __aenter__(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=getattr(self.config, 'llm_request_timeout', 60),
                    connect=getattr(self.config, 'llm_connect_timeout', 10)
                )
            )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session and not self.session.closed:
            await self.session.close()

    async def initialize(self):
        """Инициализация сервиса, включая HTTP сессию и проверку API."""
        if not self.session or self.session.closed:
            connect_timeout = getattr(self.config, 'llm_connect_timeout', 10)
            total_timeout = getattr(self.config, 'llm_request_timeout', 60)
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=total_timeout, connect=connect_timeout)
            )
        # Health check can be part of initialization if critical
        # await self._health_check() 
        logger.info(f"LLM Service (модель: {self.gemini_model}) инициализирован.")

    async def _health_check(self):
        """Проверка доступности Gemini API (опционально, может замедлить старт)."""
        logger.info(f"Проверка доступности Gemini API (модель: {self.gemini_model})...")
        try:
            test_messages = [{"role": "user", "parts": [{"text": "Привет"}]}]
            await self._call_gemini_api_raw(
                contents=test_messages,
                generation_config={"maxOutputTokens": 5, "temperature": 0.1}
            )
            logger.info("Gemini API успешно прошел проверку работоспособности.")
        except Exception as e:
            logger.error(f"Проверка Gemini API не удалась: {e}", exc_info=True)
            raise APIError(f"Gemini API недоступен при инициализации: {e}")

    @handle_errors(reraise_as=APIError) # Changed reraise to reraise_as
    async def generate_response(self,
                              user_message: str,
                              persona: str,
                              context_messages: Optional[List[Dict]] = None,
                              dynamic_context_info: Optional[Dict[str, Any]] = None,
                              max_output_tokens: Optional[int] = None,
                              temperature: Optional[float] = None,
                              skip_stats: bool = False) -> str:
        """Генерирует ответ от LLM с учетом персоны, контекста и динамической информации."""

        max_output_tokens_to_use = max_output_tokens or getattr(self.config, 'llm_max_output_tokens', 1000)
        temperature_to_use = temperature if temperature is not None else getattr(self.config, 'llm_temperature', 0.7)

        if not skip_stats:
            self.usage_stats['total_requests'] += 1

        # 1. Подготовка сообщений
        full_messages_for_llm = await self._prepare_messages_for_llm(
            user_message=user_message,
            persona=persona,
            context_messages=context_messages,
            dynamic_context_info=dynamic_context_info
        )
        gemini_formatted_contents = self._convert_to_gemini_format(full_messages_for_llm)
        
        # Подсчет входных токенов с использованием экземпляра TokenCounter
        current_input_tokens = 0
        for content_item in gemini_formatted_contents:
            for part in content_item.get('parts', []):
                if 'text' in part:
                    current_input_tokens += await self.token_counter_instance.count_tokens(part['text'], model='gemini')
        
        if not skip_stats:
             self.usage_stats['total_input_tokens'] += current_input_tokens

        # 2. Вызов API через Circuit Breaker
        generation_config_params = {
            "maxOutputTokens": max_output_tokens_to_use,
            "temperature": temperature_to_use,
            "topP": getattr(self.config, 'llm_top_p', 0.95),
            "topK": getattr(self.config, 'llm_top_k', 64)
        }
        generation_config = {k: v for k, v in generation_config_params.items() if v is not None}

        response_data = await gemini_circuit_breaker.call(
            self._call_gemini_api_raw,
            contents=gemini_formatted_contents,
            generation_config=generation_config
        )

        # 3. Извлечение текста ответа
        response_text = self._extract_response_text(response_data)

        if not skip_stats:
            self.usage_stats['successful_requests'] += 1
            output_tokens = await self.token_counter_instance.count_tokens(response_text, model='gemini')
            self.usage_stats['total_output_tokens'] += output_tokens

        logger.info(f"Ответ ({len(response_text)} симв., {output_tokens if not skip_stats else 'N/A'} токенов) сгенерирован для {persona}. Входных токенов: {current_input_tokens}.")
        return response_text
        # No explicit try/except here as @handle_errors takes care of it.

    async def _prepare_messages_for_llm(self,
                               user_message: str,
                               persona: str,
                               context_messages: Optional[List[Dict]] = None,
                               dynamic_context_info: Optional[Dict[str, Any]] = None
                               ) -> List[Dict]:
        """Подготавливает полный список сообщений (системный + динамический + контекст + текущее)."""
        system_prompt_template = await self._get_system_prompt(persona)
        user_name_to_inject = (dynamic_context_info or {}).get('user_name', 'мой собеседник')
        system_prompt_content = system_prompt_template.replace("{user_name}", user_name_to_inject)
        
        dynamic_instructions_block = ""
        if dynamic_context_info:
            instructions_parts = []
            if persona == "aeris" and dynamic_context_info.get('current_vibe'):
                instructions_parts.append(f"Текущий вайб для Аэрис: {dynamic_context_info['current_vibe']}.")
            if dynamic_context_info.get('passion_level') is not None:
                instructions_parts.append(f"Текущий уровень страсти: {dynamic_context_info['passion_level']}/10.")
            if dynamic_context_info.get('sexting_mode'):
                 instructions_parts.append(f"Текущий режим секстинга: {dynamic_context_info['sexting_mode']} (Придерживайся инструкций для этого режима из основного промпта).")
            if instructions_parts:
                dynamic_instructions_block = "[Текущие установки для диалога (учитывай в первую очередь)]:\n" + "\n".join(instructions_parts) + "\n\n"
        
        final_system_content = dynamic_instructions_block + system_prompt_content
        prepared_messages: List[Dict] = [{"role": "system", "content": final_system_content}]
        if context_messages:
            prepared_messages.extend(context_messages)
        prepared_messages.append({"role": "user", "content": user_message})
        return prepared_messages

    async def _get_system_prompt(self, persona: str) -> str:
        """Получает системный промпт для персоны с кэшированием."""
        if persona not in self._system_prompts_cache:
            try:
                self._system_prompts_cache[persona] = prompt_manager.get_prompt(persona)
                logger.info(f"Системный промпт для персоны '{persona}' загружен и кэширован.")
            except Exception as e:
                logger.error(f"Ошибка загрузки системного промпта для персоны '{persona}': {e}", exc_info=True)
                raise APIError(f"Не удалось загрузить системный промпт для '{persona}'.") # Propagate as APIError
        return self._system_prompts_cache[persona]

    def _convert_to_gemini_format(self, messages: List[Dict]) -> List[Dict]:
        """Преобразует сообщения из стандартного формата [{role: str, content: str}] в формат Gemini API."""
        gemini_contents: List[Dict] = []
        current_parts: List[Dict] = []
        current_gemini_role: Optional[str] = None
        system_content_buffer = ""

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if role == "system":
                system_content_buffer += content + "\n\n"; continue
            
            gemini_role_for_msg = "user" if role == "user" else "model"
            if gemini_role_for_msg != current_gemini_role and current_parts:
                if current_gemini_role:
                     gemini_contents.append({"role": current_gemini_role, "parts": current_parts})
                current_parts = []
            current_gemini_role = gemini_role_for_msg
            
            text_to_add = content
            if system_content_buffer:
                if current_gemini_role == 'user':
                    text_to_add = system_content_buffer + content
                    system_content_buffer = ""
                else: # Should not happen if system prompt is followed by user prompt
                    logger.warning(f"System prompt buffer not empty, but current role is {current_gemini_role}. Attaching to current message.")
                    text_to_add = system_content_buffer + content
                    system_content_buffer = ""
            current_parts.append({"text": text_to_add})

        if current_gemini_role and current_parts:
            gemini_contents.append({"role": current_gemini_role, "parts": current_parts})
        
        if system_content_buffer: # If system prompt was the only thing or wasn't attached
            if not gemini_contents or gemini_contents[-1]["role"] == "model":
                gemini_contents.append({"role": "user", "parts": [{"text": system_content_buffer.strip()}]})
            else: 
                gemini_contents[-1]["parts"].append({"text": "\n\n" + system_content_buffer.strip()})

        if gemini_contents and gemini_contents[0]["role"] == "model":
            logger.warning("Диалог для Gemini начинается с роли 'model'. Добавляется user-сообщение 'Продолжай'.")
            gemini_contents.insert(0, {"role": "user", "parts": [{"text": "Продолжай."}]})
        return gemini_contents

    async def _call_gemini_api_raw(self, contents: List[Dict], generation_config: Dict, safety_settings: Optional[List[Dict]] = None) -> Dict:
        """Непосредственный вызов Gemini API."""
        if not self.session or self.session.closed: # Ensure session is initialized
            await self.initialize()
            if not self.session: # Still not initialized after attempt
                 raise APIError("HTTP сессия не инициализирована для вызова Gemini API.")

        url = f"{self.gemini_base_url}/{self.gemini_model}:generateContent?key={self.config.gemini_api_key}"
        default_safety_settings = getattr(self.config, 'gemini_safety_settings', [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"}
        ])
        payload = {
            "contents": contents,
            "generationConfig": generation_config,
            "safetySettings": safety_settings if safety_settings is not None else default_safety_settings
        }
        headers = {"Content-Type": "application/json"}

        try:
            async with self.session.post(url, json=payload, headers=headers) as response:
                response_status = response.status
                response_text_data = await response.text()
                if response_status == 200:
                    try: return json.loads(response_text_data)
                    except json.JSONDecodeError as je:
                        logger.error(f"Ошибка декодирования JSON от Gemini (статус 200): {je}. Ответ: {response_text_data[:500]}")
                        raise APIError("Ошибка декодирования ответа Gemini.", error_code='JSON_DECODE_ERROR')

                logger.error(f"Ошибка API Gemini: статус {response_status}. Ответ: {response_text_data[:1000]}")
                parsed_error_details = {}
                try: parsed_error_details = json.loads(response_text_data)
                except json.JSONDecodeError: pass
                
                # Handle specific error codes and safety blocks
                if response_status == 400:
                    error_message = parsed_error_details.get('error', {}).get('message', response_text_data)
                    if 'candidates' in parsed_error_details and isinstance(parsed_error_details['candidates'], list) and parsed_error_details['candidates']:
                        first_candidate = parsed_error_details['candidates'][0]
                        if first_candidate.get('finishReason') == 'SAFETY':
                            safety_ratings = first_candidate.get('safetyRatings', [])
                            logger.warning(f"Ответ заблокирован по соображениям безопасности Gemini: {safety_ratings}")
                            raise APIError(f"Ответ заблокирован Gemini по соображениям безопасности: {first_candidate.get('finishReason')}. Детали: {safety_ratings}", error_code='SAFETY_BLOCK')
                    raise APIError(f"Ошибка запроса к Gemini (400): {error_message}", error_code='BAD_REQUEST')
                elif response_status == 429:
                    raise APIError("Превышен лимит запросов к Gemini API.", error_code='RATE_LIMIT')
                elif response_status in [401, 403]:
                    raise APIError("Ошибка аутентификации/авторизации с Gemini API.", error_code='AUTH_ERROR')
                else:
                    raise APIError(f"Неизвестная ошибка Gemini API (статус {response_status}): {response_text_data}", error_code=f'API_ERROR_{response_status}')
        except aiohttp.ClientError as e:
            logger.error(f"Сетевая ошибка при вызове Gemini API: {e}", exc_info=True)
            raise APIError(f"Сетевая ошибка при обращении к Gemini: {e}", error_code='NETWORK_ERROR')
        except asyncio.TimeoutError: # Explicitly catch asyncio.TimeoutError
            logger.error("Таймаут запроса к Gemini API.", exc_info=True)
            raise APIError("Таймаут запроса к Gemini.", error_code='TIMEOUT')


    def _extract_response_text(self, response_data: Dict) -> str:
        """Извлекает текст ответа из данных ответа Gemini API."""
        try:
            # Check for prompt feedback blocks
            if "promptFeedback" in response_data and "blockReason" in response_data["promptFeedback"]:
                block_reason = response_data["promptFeedback"]["blockReason"]
                safety_ratings_feedback = response_data["promptFeedback"].get("safetyRatings", [])
                logger.warning(f"Промпт был заблокирован Gemini по причине: {block_reason}. Safety Ratings: {safety_ratings_feedback}")
                user_message = f"Мой ответ был заблокирован системой безопасности контента (причина: {block_reason}). Пожалуйста, попробуйте переформулировать ваш запрос."
                if block_reason == "OTHER": user_message = "К сожалению, я не могу ответить на этот запрос из-за ограничений безопасности контента."
                return f"[{user_message}]" # Return system message

            candidates = response_data.get("candidates")
            if not candidates:
                logger.warning(f"Ответ Gemini не содержит кандидатов. Данные ответа: {response_data}")
                raise APIError("Ответ Gemini не содержит кандидатов.", error_code='NO_CANDIDATES')

            candidate = candidates[0]
            finish_reason = candidate.get("finishReason")

            # Handle non-STOP/MAX_TOKENS finish reasons
            if finish_reason not in [None, "STOP", "MAX_TOKENS"]: # None is a valid initial state for streaming, but here we expect a final response
                safety_ratings_candidate = candidate.get("safetyRatings", [])
                user_message_finish_reason = f"Генерация ответа была прервана (причина: {finish_reason})."
                if finish_reason == "SAFETY":
                     user_message_finish_reason = f"Мой ответ был отфильтрован системой безопасности контента (причина: {finish_reason}). Детали: {safety_ratings_candidate}"
                logger.warning(f"Генерация ответа Gemini остановлена по причине: {finish_reason}. Safety Ratings: {safety_ratings_candidate}")
                return f"[{user_message_finish_reason}]"

            content = candidate.get("content")
            if not content or not content.get("parts"):
                if finish_reason == "SAFETY": return "[Мой ответ был полностью отфильтрован системой безопасности контента.]"
                logger.warning(f"Ответ кандидата Gemini не содержит 'content' или 'parts'. Кандидат: {candidate}")
                raise APIError("Ответ кандидата Gemini не содержит текстовых частей.", error_code='NO_CONTENT_PARTS')

            response_text_parts = [part.get("text", "") for part in content.get("parts") if "text" in part]
            response_text = "".join(response_text_parts)
            if not response_text.strip() and finish_reason == "STOP": # Empty but valid
                logger.info("Gemini вернул пустой ответ с finishReason=STOP.")
                return ""
            return response_text.strip()

        except APIError: raise # Re-raise APIErrors as they are already specific
        except Exception as e:
            logger.error(f"Неожиданная ошибка парсинга ответа Gemini: {e}. Данные: {response_data}", exc_info=True)
            raise APIError(f"Некорректный формат ответа от Gemini: {e}", error_code='INVALID_RESPONSE_FORMAT')

    async def create_summary(self,
                           messages_text: str,
                           persona: str = "diana",
                           max_tokens_summary: Optional[int] = None,
                           dynamic_context_info: Optional[Dict[str, Any]] = None,
                           ) -> str:
        """Создает краткое резюме диалога."""
        max_tokens_summary_to_use = max_tokens_summary or getattr(self.config, 'llm_summary_max_tokens', 200)
        summary_prompt_template = prompt_manager.get_prompt(f"summary_instruction_{persona}", default_fallback=None)
        user_name_to_inject = (dynamic_context_info or {}).get('user_name', 'собеседник')

        if not summary_prompt_template:
            summary_prompt_template = (
                "Ты — {persona_name}. Создай краткое и информативное резюме следующего диалога c {user_name_placeholder} (2-4 предложения), "
                "сохраняя ключевые темы, эмоциональный оттенок и важные детали. Говори от своего лица ({persona_name}).\n\nДиалог:\n{dialogue_text}\n\nРезюме:"
            )
        summary_prompt = summary_prompt_template.format(
            persona_name=persona.title(), user_name_placeholder=user_name_to_inject, dialogue_text=messages_text
        )
        try:
            summary_temperature = getattr(self.config, 'llm_summary_temperature', 0.3)
            return await self.generate_response(
                user_message=summary_prompt, persona=persona, context_messages=[],
                max_output_tokens=max_tokens_summary_to_use, temperature=summary_temperature,
                skip_stats=True 
            )
        except Exception as e:
            logger.error(f"Ошибка создания резюме для персоны {persona}: {e}", exc_info=True)
            return f"[Ошибка создания резюме: {str(e)}]"

    async def analyze_emotional_context(self,
                                      text_to_analyze: str,
                                      persona: str = "diana",
                                      dynamic_context_info: Optional[Dict[str, Any]] = None,
                                      ) -> Dict[str, float]:
        """Анализирует эмоциональный контекст текста, возвращая JSON."""
        analysis_prompt_template = prompt_manager.get_prompt(f"emotion_analysis_instruction_{persona}", default_fallback=None)
        user_name_to_inject = (dynamic_context_info or {}).get('user_name', 'собеседник')
        if not analysis_prompt_template:
            analysis_prompt_template = (
                "Ты — {persona_name}. Проанализируй эмоциональный контекст следующего текста от {user_name_placeholder}. "
                "Оцени каждую из следующих эмоций по шкале от 0.0 (отсутствует) до 1.0 (максимально выражена): "
                "positive, negative, romantic, passionate, neutral. "
                "Верни результат ТОЛЬКО в формате JSON.\n\nТекст для анализа:\n\"{text}\"\n\nJSON:"
            )
        analysis_prompt = analysis_prompt_template.format(
            persona_name=persona.title(), user_name_placeholder=user_name_to_inject, text=text_to_analyze
        )
        default_emotions = {"positive": 0.5, "negative": 0.0, "romantic": 0.0, "passionate": 0.0, "neutral": 0.5}
        try:
            response_str = await self.generate_response(
                user_message=analysis_prompt, persona=persona, context_messages=[],
                max_output_tokens=getattr(self.config, 'llm_emotion_max_tokens', 150),
                temperature=getattr(self.config, 'llm_emotion_temperature', 0.1),
                skip_stats=True
            )
            json_match = re.search(r'\{.*\}', response_str, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
                try:
                    emotions_data = json.loads(json_str)
                    validated_emotions = {}
                    for key in default_emotions:
                        value = emotions_data.get(key)
                        if isinstance(value, (int, float)) and 0.0 <= value <= 1.0:
                            validated_emotions[key] = float(value)
                        else:
                            logger.warning(f"Некорректное значение для эмоции '{key}': {value}. Используется дефолт ({default_emotions[key]}).")
                            validated_emotions[key] = default_emotions[key]
                    return validated_emotions
                except json.JSONDecodeError:
                    logger.warning(f"Не удалось декодировать JSON из анализа эмоций: {json_str}. Ответ LLM: {response_str}")
            else:
                logger.warning(f"Не удалось извлечь JSON из ответа анализа эмоций LLM: {response_str}")
            return default_emotions
        except Exception as e:
            logger.error(f"Ошибка анализа эмоционального контекста: {e}", exc_info=True)
            return default_emotions

    def clear_system_prompts_cache(self):
        self._system_prompts_cache.clear()
        logger.info("Кэш системных промптов очищен.")

    def get_usage_stats(self) -> Dict[str, Any]:
        stats = self.usage_stats.copy()
        total_successful = stats['successful_requests']
        stats['success_rate'] = (total_successful / stats['total_requests'] * 100) if stats['total_requests'] > 0 else 0.0
        stats['avg_input_tokens_per_request'] = (stats['total_input_tokens'] / total_successful) if total_successful > 0 else 0.0
        stats['avg_output_tokens_per_request'] = (stats['total_output_tokens'] / total_successful) if total_successful > 0 else 0.0
        return stats

    def reset_usage_stats(self):
        self.usage_stats = {
            'total_requests': 0, 'successful_requests': 0, 'failed_requests': 0,
            'total_input_tokens': 0, 'total_output_tokens': 0,
            'last_reset': datetime.now(timezone.utc)
        }
        logger.info("Статистика использования LLM сервиса сброшена.")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
            logger.info("HTTP сессия LLM сервиса закрыта.")
