#!/usr/bin/env python3
"""
AI Companion Bot - Enhanced Production Version
"""
import asyncio
import logging
import sys
from datetime import datetime as dt, timezone, timedelta 
from typing import Dict, Set, Any, Optional, Tuple

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.ext.declarative import declarative_base

from aiogram.types import BotCommand, User as AiogramUser

# Основные компоненты
from config.settings import load_config, setup_logging, BotConfig, ConfigurationError
from config.prompts import prompt_manager
from database.models import Base as AppBase
# Используем MemoryStorage для FSM вместо SQLAlchemyStorage
from database.operations import DatabaseService
from database.models import User as DBUser

# Сервисы
from services.llm_service import LLMService
from services.memory_service import MemoryService, MemoryType 
from services.context_manager import ContextManager
from services.subscription_system import SubscriptionService, SubscriptionMiddleware, SubscriptionTier
from services.tts_service import TTSService
from services.promocode_system import PromoCodeService
from services.referral_ab_testing import ReferralService, ABTestService, ABTestIntegration
from services.notification_marketing_system import NotificationService
from services.limits_validation import AdvancedLimitsValidator, ValidationResult

# Утилиты
from utils.error_handler import ErrorHandler 
from utils.token_counter import TokenCounter 
from utils.navigation import navigation

# Обработчики
from handlers.navigation_handlers import enhanced_nav_router, NavigationHandler 
from handlers.payment_handlers import payment_router, PromoCodeFSM 
from handlers.admin_panel import admin_router as admin_panel_router
from handlers.story_creation_fsm import story_fsm_router, start_story_creation 

logger = logging.getLogger(__name__)

# --- Периодические задачи очистки ---
async def periodic_rate_limiter_cleanup(db_service: DatabaseService, interval_hours: int = 24):
    while True:
        await asyncio.sleep(interval_hours * 60 * 60)
        try:
            cutoff_for_global_cleanup = dt.now(timezone.utc) - timedelta(days=7)
            deleted_count = await db_service.delete_old_user_action_timestamps(older_than_time=cutoff_for_global_cleanup)
            if deleted_count > 0:
                logger.info(f"[Periodic Cleanup] Удалено {deleted_count} очень старых записей UserActionTimestamp (старше 7 дней).")
        except Exception as e:
            logger.error(f"[Periodic Cleanup] Ошибка при очистке UserActionTimestamp: {e}", exc_info=True)

async def periodic_temp_block_cleanup(db_service: DatabaseService, interval_hours: int = 1):
    while True:
        await asyncio.sleep(interval_hours * 60 * 60)
        try:
            deleted_count = await db_service.delete_expired_temporary_blocks() 
            if deleted_count > 0:
                logger.info(f"[Periodic Cleanup] Удалено {deleted_count} истекших временных блокировок из БД.")
        except Exception as e:
            logger.error(f"[Periodic Cleanup] Ошибка при очистке TemporaryBlock: {e}", exc_info=True)

async def periodic_summary_cleanup(db_service: DatabaseService, interval_hours: int = 24, days_to_keep: int = 30):
    while True:
        await asyncio.sleep(interval_hours * 60 * 60)
        try:
            deleted_count = await db_service.delete_old_context_summaries(older_than_days=days_to_keep)
            if deleted_count > 0:
                logger.info(f"[Periodic Cleanup] Удалено {deleted_count} старых суммаризаций контекста (старше {days_to_keep} дней).")
        except Exception as e:
            logger.error(f"[Periodic Cleanup] Ошибка при очистке ContextSummary: {e}", exc_info=True)

async def setup_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="🎭 Начать/Перезапустить общение"),
        BotCommand(command="menu", description="🎮 Главное меню навигации"),
        BotCommand(command="help", description="❓ Подробная справка по боту"),
        BotCommand(command="switch_persona", description="🔄 Переключить персону (Aeris/Luneth)"),
        BotCommand(command="set_vibe", description="🎭 Настройки Aeris (вайб)"), 
        BotCommand(command="sexting_level", description="🔥 Уровень страсти Luneth"), 
        BotCommand(command="i_want_you", description="💋 Страстный режим"), 
        BotCommand(command="stop_sexting", description="😌 Остыть (Luneth)"), 
        BotCommand(command="romantic_fantasy", description="💫 Романтическая фантазия (Aeris)"),
        BotCommand(command="mood_check", description="🎯 Проверка настроения (Aeris)"),
        BotCommand(command="create_story", description="📚 Создать историю"),
        BotCommand(command="start_quest", description="🗺️ Начать квест самопознания (Aeris)"),
        BotCommand(command="save_insight", description="💡 Сохранить мысль/инсайт"),
        BotCommand(command="my_insights", description="🧠 Мои инсайты"),
        BotCommand(command="new_entry", description="✍️ Новая запись в журнал рефлексии"),
        BotCommand(command="my_journal", description="📔 Мой журнал рефлексии"),
        BotCommand(command="stats", description="📊 Статистика использования"),
        BotCommand(command="profile", description="👤 Мой профиль и настройки"),
        BotCommand(command="premium", description="⭐️ Управление подпиской Premium"),
        BotCommand(command="feedback", description="💬 Оставить отзыв о боте"),
        BotCommand(command="referral", description="🎁 Пригласить друзей и получить бонусы"),
        BotCommand(command="cancel_story", description="🚫 Отменить создание истории (в процессе)"),
        BotCommand(command="adminpanel", description="🔒 Админ-панель (для админов)")
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("✅ Команды бота настроены")
    except Exception as e_commands:
        logger.error(f"Ошибка настройки команд бота: {e_commands}")

class AICompanionBot:
    def __init__(self, bot_config: BotConfig):
        self.config = bot_config
        self.bot = Bot(token=self.config.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2)) 

        self.db_service = DatabaseService(self.config)
        
        self.storage: Optional[MemoryStorage] = None 
        self.dp = Dispatcher(storage=self.storage) 

        self.error_handler_instance = ErrorHandler(app_config=self.config) 
        self.llm_service = LLMService(self.config)
        self.tts_service = TTSService(self.config)
        self.subscription_service = SubscriptionService(self.db_service, self.config, self) 
        self.memory_service = MemoryService(self.db_service, self.subscription_service)
        self.promocode_service = PromoCodeService(self.db_service, self.subscription_service, self.config)
        self.referral_service = ReferralService(self.db_service, self.subscription_service, self.promocode_service, self.config)
        self.ab_test_service = ABTestService(self.db_service)
        self.ab_test_integration = ABTestIntegration(self.ab_test_service, self.db_service)
        self.notification_service = NotificationService(self.bot, self.db_service, self.subscription_service)
        self.limits_validator = AdvancedLimitsValidator(self.subscription_service, self.db_service, 
                                                        validator_config=getattr(self.config, 'validator_config', None))
        
        self.token_counter_instance = TokenCounter(
            gemini_api_key=self.config.gemini_api_key,
            gemini_model_name_for_counting=getattr(self.config, 'gemini_model_name', "gemini-2.0-flash") 
        )

        self.context_manager = ContextManager(
            db_service=self.db_service, 
            max_messages_in_context=self.config.max_context_messages,
            summary_creation_threshold=self.config.context_summary_threshold,
            max_tokens_for_llm=self.config.max_tokens_per_request
        )
        self.subscription_middleware = SubscriptionMiddleware(self.subscription_service)
        
        self.dp.workflow_data.update({
            'bot_instance': self, 'config': self.config, 'db_service': self.db_service,
            'llm_service': self.llm_service, 'tts_service': self.tts_service,
            'subscription_service': self.subscription_service, 'memory_service': self.memory_service,
            'promocode_service': self.promocode_service, 'referral_service': self.referral_service,
            'error_handler': self.error_handler_instance, 'notification_service': self.notification_service,
            'limits_validator': self.limits_validator,
            'context_manager': self.context_manager,
            'token_counter_instance': self.token_counter_instance
        })
        self.navigation_handlers_instance = NavigationHandler(self)
        self.dp.workflow_data['navigation_handlers_instance'] = self.navigation_handlers_instance
        self.stats: Dict[str, Any] = {
            'messages_processed': 0, 'errors_count': 0, 'active_users': set(), 
            'start_time': dt.now(timezone.utc), 'last_activity': dt.now(timezone.utc),
            'revenue_total_stars': 0.0, 'subscriptions_sold': 0, 'daily_active_users': set(),
            'conversion_rate': 0.0 
        }
        if hasattr(prompt_manager, 'load_prompts') and callable(prompt_manager.load_prompts):
             prompt_manager.load_prompts(persona_dir=getattr(self.config, 'persona_files_dir', 'personas'))
        self.dp.workflow_data['prompt_manager'] = prompt_manager

    async def initialize(self):
        logger.info(f"🚀 Инициализация AI Companion Bot v{self.config.version}...")
        try:
            if not self.config.telegram_bot_token or "YOUR_BOT_TOKEN" in self.config.telegram_bot_token or "УКАЖИТЕ_ВАШ_ТОКЕН_В_.ENV" in self.config.telegram_bot_token : 
                raise ConfigurationError("TELEGRAM_BOT_TOKEN не установлен или имеет значение по умолчанию!")
            if not self.config.gemini_api_key or "YOUR_API_KEY" in self.config.gemini_api_key or "УКАЖИТЕ_ВАШ_GEMINI_КЛЮЧ_В_.ENV" in self.config.gemini_api_key: 
                raise ConfigurationError("GEMINI_API_KEY не установлен или имеет значение по умолчанию!")
            if not self.config.bot_username or self.config.bot_username == "YOUR_BOT_USERNAME_HERE" or self.config.bot_username == "УКАЖИТЕ_ИМЯ_ПОЛЬЗОВАТЕЛЯ_БОТА_В_.ENV":
                logger.warning("BOT_USERNAME не установлен корректно в .env! Реферальные ссылки могут не работать. Попытка получить из API при старте.")

            await self.db_service.initialize() 
            
            if not self.db_service.connection_manager.engine:
                raise ConfigurationError("Database engine не был инициализирован в DatabaseService.")

            self.storage = MemoryStorage()
            self.dp.storage = self.storage 
            logger.info("✅ MemoryStorage для FSM инициализирован.")

            await self.llm_service.initialize()
            await self.tts_service.initialize()
            await setup_bot_commands(self.bot)
            await self._setup_handlers()
            
            self.dp.update.middleware(self.subscription_middleware)
            logger.info("✅ Subscription middleware зарегистрирован.")
            
            if not prompt_manager.validate_prompts():
                logger.warning("⚠️ Ошибка валидации системных промптов. Проверьте файлы в папке 'personas'.")
            
            logger.info("✅ Все основные сервисы инициализированы успешно.")
        except ConfigurationError as ce: 
            logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА КОНФИГУРАЦИИ: {ce}")
            await self.cleanup_on_error(); sys.exit(1) 
        except Exception as e:
            logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА ИНИЦИАЛИЗАЦИИ: {e}", exc_info=True)
            await self.cleanup_on_error(); raise 

    async def _setup_handlers(self):
        self.dp.include_router(enhanced_nav_router)
        self.dp.include_router(payment_router)
        self.dp.include_router(admin_panel_router)
        self.dp.include_router(story_fsm_router)

        self.dp.message(Command("start"))(self.cmd_start)
        self.dp.message(Command("menu"))(self.cmd_menu)
        self.dp.message(Command("premium"))(self.cmd_premium)
        self.dp.message(Command("profile"))(self.cmd_profile)
        self.dp.message(Command("help"))(self.cmd_help)
        self.dp.message(Command("referral"))(self.cmd_referral)
        self.dp.message(Command("adminpanel"))(self.cmd_adminpanel_entry)
        self.dp.message(Command("quickstats"))(self._quick_stats_command)
        self.dp.message(Command("create_story"))(self.cmd_create_story_entry)
        self.dp.message(Command("start_quest"))(self.cmd_start_quest_entry)

        self.dp.message(F.text)(self.handle_text_message)
        self.dp.message(F.voice)(self.handle_voice_message_main)
        logger.info("✅ Обработчики команд и сообщений настроены.")

    async def cmd_start(self, message: types.Message, command: CommandObject, state: FSMContext, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        user_id_tg = message.from_user.id
        user_tg_obj = message.from_user
        
        db_user, is_new_user_flag = await bot_instance._get_or_create_user_with_new_flag(user_tg_obj)
        
        if not db_user:
            logger.error(f"Не удалось получить/создать пользователя для TG ID {user_id_tg} в cmd_start.")
            await message.answer("Произошла ошибка при инициализации вашего профиля. Пожалуйста, попробуйте позже.")
            return

        if is_new_user_flag:
            logger.info(f"Новый пользователь {user_id_tg} ({user_tg_obj.username}) присоединился.")
            try:
                await bot_instance.notification_service.send_welcome_notification_if_needed(user_id_tg, user_tg_obj.first_name)
            except Exception as e_notify:
                logger.error(f"Ошибка отправки welcome-уведомления пользователю {user_id_tg}: {e_notify}")
        
        referral_code_from_args = command.args
        if referral_code_from_args: 
            logger.info(f"Пользователь {user_id_tg} запустил бота с аргументом (реф. код?): {referral_code_from_args}")
            referral_result = await bot_instance.referral_service.process_referral_code_usage(
                referee_user_id_tg=user_id_tg, 
                referral_code_entered=referral_code_from_args
            )
            if referral_result.get("success"):
                await message.answer(f"🎉 {referral_result.get('message', 'Реферальный код принят!')}")
            else:
                logger.info(f"Не удалось применить реф. код '{referral_code_from_args}' для {user_id_tg}: {referral_result.get('message')}")
        
        await bot_instance.navigation_handlers_instance.show_main_menu(message, state)

    async def cmd_menu(self, message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
        await bot_instance.navigation_handlers_instance.show_main_menu(message, state)

    async def cmd_premium(self, message: types.Message, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        current_persona = await bot_instance._get_current_persona(message.from_user.id)
        await bot_instance.navigation_handlers_instance._show_menu_node(message, message.from_user.id, "my_subscription_view", current_persona)

    async def cmd_profile(self, message: types.Message, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        current_persona = await bot_instance._get_current_persona(message.from_user.id)
        await bot_instance.navigation_handlers_instance._show_menu_node(message, message.from_user.id, "user_profile_view", current_persona)

    async def cmd_help(self, message: types.Message, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        current_persona = await bot_instance._get_current_persona(message.from_user.id)
        help_text_template = bot_instance.dp.workflow_data['prompt_manager'].get_prompt("help_message", default_fallback="Это основная справка по боту...")
        user_info = await bot_instance.bot.get_chat(message.from_user.id)
        user_display_name = user_info.first_name if isinstance(user_info, types.User) else (user_info.title if isinstance(user_info, types.Chat) else "пользователь")
        help_text = help_text_template.format(user_name=user_display_name)
        await bot_instance.navigation_handlers_instance._show_menu_node(message, message.from_user.id, "help_feedback_main", current_persona, title_override=help_text)

    async def cmd_referral(self, message: types.Message, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        current_persona = await bot_instance._get_current_persona(message.from_user.id)
        await bot_instance.navigation_handlers_instance._show_menu_node(message, message.from_user.id, "referral_dashboard", current_persona)
    
    async def cmd_adminpanel_entry(self, message: types.Message, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        admin_panel_handler = bot_instance.dp.workflow_data.get('admin_panel_handler_instance')
        if not admin_panel_handler: 
             from handlers.admin_panel import AdminPanel
             admin_panel_handler = AdminPanel(bot_instance.db_service, bot_instance.subscription_service, bot_instance.config, bot_instance)
             bot_instance.dp.workflow_data['admin_panel_handler_instance'] = admin_panel_handler
        if not await admin_panel_handler.is_admin(message.from_user.id): return await message.reply("❌ У вас нет доступа к этой команде.")
        menu_data = await admin_panel_handler.create_main_admin_menu()
        await message.answer(menu_data["text"], reply_markup=menu_data["reply_markup"], parse_mode="Markdown")

    async def cmd_create_story_entry(self, message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        await start_story_creation(message, state, bot_instance)

    async def cmd_start_quest_entry(self, message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        current_persona = await bot_instance._get_current_persona(message.from_user.id)
        if current_persona != 'diana':
            await message.answer("Квесты доступны только при общении с Дианой. Используйте /switch_persona.")
            return
        nav_handler_instance: NavigationHandler = bot_instance.navigation_handlers_instance
        await nav_handler_instance.handle_start_quest_action(message, state)

    async def _quick_stats_command(self, message: types.Message, bot_instance: 'AICompanionBot'):
        if not message.from_user or message.from_user.id not in bot_instance.config.admin_user_ids: 
            return await message.reply("Команда доступна только администраторам.")
        stats_text = f"⚡ {types.bold('Быстрая статистика AI Companion Bot')}\n\n" 
        stats_text += f"⏱️ Время работы: {dt.now(timezone.utc) - bot_instance.stats['start_time']}\n"
        stats_text += f"👤 Активных пользователей (сессия): {len(bot_instance.stats['active_users'])}\n"
        stats_text += f"☀️ DAU (сегодня): {len(bot_instance.stats['daily_active_users'])}\n"
        stats_text += f"💬 Сообщений обработано: {bot_instance.stats['messages_processed']}\n"
        stats_text += f"💰 Доход (звезды): {bot_instance.stats['revenue_total_stars']:.0f} ⭐\n"
        stats_text += f"🛒 Продано подписок: {bot_instance.stats['subscriptions_sold']}\n"
        stats_text += f"⚠️ Ошибок за сессию: {bot_instance.stats['errors_count']}\n"
        db_stats = bot_instance.db_service.get_service_stats()
        llm_stats = bot_instance.llm_service.get_usage_stats()
        stats_text += f"\n{types.bold('База данных:')}\n"
        stats_text += f"  Запросов: {db_stats['performance_stats']['total_queries']}, Медленных: {db_stats['performance_stats']['slow_queries']}\n"
        stats_text += f"\n{types.bold('LLM (' + str(bot_instance.config.gemini_model_name) + '):')}\n"
        stats_text += f"  Запросов: {llm_stats['total_requests']} (Успех: {llm_stats.get('success_rate', 0.0):.1f}%)\n"
        stats_text += f"  Токены (вх/вых): {llm_stats['total_input_tokens']}/{llm_stats['total_output_tokens']}\n"
        await message.answer(stats_text, parse_mode="HTML") 

    async def _perform_pre_llm_processing(self, message: types.Message) -> Tuple[Optional[DBUser], Optional[str], Optional[ValidationResult]]:
        """Handles user creation/retrieval, stats update, and limit validation."""
        if not message.from_user or not message.text:
            return None, None, ValidationResult(allowed=False, reason="Invalid message data.")

        user_id_tg = message.from_user.id
        self.stats['active_users'].add(user_id_tg)
        self.stats['daily_active_users'].add(user_id_tg)
        self.stats['last_activity'] = dt.now(timezone.utc)

        user, _ = await self._get_or_create_user_with_new_flag(message.from_user)
        if not user:
            logger.error(f"Не удалось получить или создать пользователя для TG ID {user_id_tg} в _perform_pre_llm_processing.")
            await message.answer("Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте позже.")
            return None, None, ValidationResult(allowed=False, reason="User creation/retrieval failed.")

        current_persona = await self._get_current_persona(user.telegram_id)
        validation_result = await self.limits_validator.validate_message_send(
            user_id_tg, message.text, current_persona
        )

        if not validation_result.allowed:
            reason_text = validation_result.user_message_override or validation_result.reason
            buttons_for_limit = []
            if validation_result.data.get("upgrade_required"):
                buttons_for_limit.append([types.InlineKeyboardButton(text="⭐ Улучшить подписку", callback_data="nav_subscription_plans_view")])
            if validation_result.data.get("block_remaining_seconds"):
                buttons_for_limit.append([types.InlineKeyboardButton(text="⏳ Повторить позже", callback_data="noop_message_blocked")])
            buttons_for_limit.append([types.InlineKeyboardButton(text="🎮 Главное меню", callback_data="nav_main")])
            
            await message.answer(reason_text, 
                                 reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons_for_limit) if buttons_for_limit else None, 
                                 parse_mode="Markdown")
            logger.info(f"Сообщение от user {user_id_tg} заблокировано: {validation_result.reason}. Данные: {validation_result.data}")
            return user, current_persona, validation_result

        await self.subscription_service.increment_message_usage(user_id_tg)
        self.stats['messages_processed'] += 1
        return user, current_persona, validation_result

    async def _get_llm_response(self, user_db_id: int, user_id_tg: int, user_first_name: Optional[str],
                                current_persona: str, user_message_text: str) -> Optional[str]:
        """Prepares context and gets response from LLMService."""
        await self.bot.send_chat_action(user_id_tg, "typing")

        recent_messages_db_dicts = await self.db_service.get_recent_messages(
            user_db_id, current_persona, limit=self.config.max_context_messages
        )
        relevant_memories_data = await self.memory_service.get_relevant_memories(
            user_id_tg, current_persona, user_message_text, limit=3
        )
        relevant_memories_content = [mem['content'] for mem in relevant_memories_data]

        context_for_llm = await self.context_manager.prepare_context_for_llm(
            user_id_db=user_db_id,
            raw_db_messages=recent_messages_db_dicts,
            current_persona=current_persona,
            token_counter_func=self.token_counter_instance.count_tokens,
            relevant_memories=relevant_memories_content
        )

        user_prefs_system = await self.db_service.get_user_preferences(user_db_id, persona='system')
        conv_settings = await self.db_service.get_conversation_settings(user_db_id, current_persona)
        dynamic_llm_context = {
            "user_name": user_first_name or "собеседник",
            "current_vibe": conv_settings.get("current_vibe") if current_persona == "diana" else None,
            "sexting_mode": user_prefs_system.get("current_sexting_mode"),
            "passion_level": conv_settings.get("sexting_level") if current_persona == "madina" else None
        }
        dynamic_llm_context = {k: v for k, v in dynamic_llm_context.items() if v is not None}

        return await self.llm_service.generate_response(
            user_message=user_message_text,
            persona=current_persona,
            context_messages=context_for_llm,
            dynamic_context_info=dynamic_llm_context
        )

    async def _perform_post_llm_processing(self, user_id_tg: int, user_db_id: int, current_persona: str,
                                           user_message_text: str, message_date: dt, 
                                           assistant_response_text: str, conversation_id: int):
        """Handles saving messages, memory extraction, TTS, and summarization."""
        await self.db_service.save_message(
            conversation_id=conversation_id,
            role='assistant',
            content=assistant_response_text,
            tokens_count=await self.token_counter_instance.count_tokens(assistant_response_text, model='gemini')
        )

        messages_for_memory_extraction = [
            {"role": "user", "content": user_message_text, "persona": current_persona, "timestamp": message_date.isoformat()},
            {"role": "assistant", "content": assistant_response_text, "persona": current_persona, "timestamp": dt.now(timezone.utc).isoformat()}
        ]
        extracted_memories_candidates = await self.memory_service.extract_memories_from_conversation(messages_for_memory_extraction, user_id_tg)
        for mem_data in extracted_memories_candidates:
            await self.memory_service.save_memory(
                user_id_tg=user_id_tg,
                persona=mem_data.get("persona", current_persona),
                content=mem_data["content"],
                memory_content_type=mem_data.get("memory_content_type", MemoryType.GENERAL.value),
                tags=mem_data.get("tags", []),
                relevance_score=mem_data.get("relevance_score", 0.5),
                emotional_weight=mem_data.get("emotional_weight", 0.5),
                context=mem_data.get("context")
            )

        voice_access = await self.subscription_service.check_feature_access(user_id_tg, "voice_messages")
        if voice_access.get("allowed", False) and self.tts_service.should_use_voice(assistant_response_text, current_persona):
            try:
                text_for_tts = assistant_response_text
                max_len_tts = getattr(self.config, 'tts_max_text_length', 450)
                if len(text_for_tts) > max_len_tts:
                    last_sentence_end = text_for_tts.rfind('.', 0, max_len_tts)
                    text_for_tts = text_for_tts[:last_sentence_end + 1] if last_sentence_end != -1 else text_for_tts[:max_len_tts]
                audio_data = await self.tts_service.synthesize_speech(text=text_for_tts, persona=current_persona)
                if audio_data:
                    await self.bot.send_voice(chat_id=user_id_tg, voice=types.BufferedInputFile(audio_data.getvalue(), filename=f"response_{current_persona}.mp3"))
            except Exception as e_tts:
                logger.warning(f"Ошибка генерации/отправки голосового сообщения для user {user_id_tg}: {e_tts}")
        
        all_conv_messages = await self.db_service.get_recent_messages(
            user_db_id, current_persona, limit=self.config.context_summary_threshold + 5
        )
        await self.context_manager.try_create_and_add_summary(
            user_id_db=user_db_id,
            all_raw_db_messages=all_conv_messages,
            current_persona=current_persona,
            llm_service_instance=self.llm_service
        )

    async def handle_text_message(self, message: types.Message, state: FSMContext):
        bot_instance = self

        try:
            user_db, current_persona, validation_result = await bot_instance._perform_pre_llm_processing(message)
            
            if not user_db or not current_persona or not validation_result or not validation_result.allowed:
                return
            
            if not message.text:
                logger.warning("handle_text_message: message.text is None after pre-processing.")
                return

            conversation = await bot_instance.db_service.get_or_create_conversation(user_db.id, current_persona)
            await bot_instance.db_service.save_message(
                conversation_id=conversation.id,
                role='user',
                content=message.text,
                tokens_count=await bot_instance.token_counter_instance.count_tokens(message.text, model='gemini')
            )

            if not message.from_user:
                return
            
            assistant_response_text = await bot_instance._get_llm_response(
                user_db.id, message.from_user.id, message.from_user.first_name,
                current_persona, message.text
            )

            if assistant_response_text is None:
                logger.error(f"LLM_Service вернул None для user {message.from_user.id}")
                await message.answer("К сожалению, я не смог сгенерировать ответ. Попробуйте позже.")
                return

            user_conditions_quick = await bot_instance.navigation_handlers_instance._get_current_user_conditions(
                message.from_user.id, current_persona
            )
            quick_actions_markup = navigation.create_quick_actions_menu(current_persona, user_conditions_quick)
            await message.answer(assistant_response_text, reply_markup=quick_actions_markup, parse_mode="Markdown")

            await bot_instance._perform_post_llm_processing(
                message.from_user.id, user_db.id, current_persona,
                message.text, message.date, assistant_response_text, conversation.id
            )

        except Exception as e:
            user_id_tg_for_error = message.from_user.id if message.from_user else 0
            current_persona_for_error = await bot_instance._get_current_persona(user_id_tg_for_error)
            
            error_id = bot_instance.error_handler_instance.log_error(
                e, 
                context={'user_id_tg': user_id_tg_for_error, 'persona': current_persona_for_error, 'message_text_preview': message.text[:200] if message.text else "N/A"}, 
                user_id=user_id_tg_for_error
            )
            user_friendly_message = bot_instance.error_handler_instance.get_user_friendly_message(e)
            await message.answer(f"{user_friendly_message}\nКод ошибки для поддержки: `{error_id}`", parse_mode="Markdown")
            bot_instance.stats['errors_count'] += 1
    
    async def handle_voice_message_main(self, message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
        if not message.from_user: return
        voice_processing_access = await bot_instance.limits_validator.validate_feature_access(message.from_user.id, "voice_messages")
        if not voice_processing_access.allowed:
            buttons_for_limit_voice = []
            if voice_processing_access.data.get("upgrade_required"):
                buttons_for_limit_voice.append([types.InlineKeyboardButton(text="⭐ Улучшить подписку", callback_data="nav_subscription_plans_view")])
            buttons_for_limit_voice.append([types.InlineKeyboardButton(text="🎮 Главное меню", callback_data="nav_main")])
            await message.answer(
                voice_processing_access.user_message_override or voice_processing_access.reason, 
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons_for_limit_voice) if buttons_for_limit_voice else None, 
                parse_mode="Markdown"
            )
            return
        await message.answer("🎤 Голосовое сообщение получено! Функция распознавания речи и ответа голосом в активной разработке. Скоро я смогу полноценно общаться с тобой голосом! 😊")

    async def _get_or_create_user(self, telegram_user: AiogramUser) -> Optional[DBUser]: 
        user_data_dict = {
            'username': telegram_user.username, 
            'first_name': telegram_user.first_name, 
            'last_name': telegram_user.last_name, 
            'language_code': telegram_user.language_code
        }
        user_data_for_db = {k: v for k, v in user_data_dict.items() if v is not None}
        try:
            return await self.db_service.get_or_create_user(telegram_id=telegram_user.id, **user_data_for_db)
        except Exception as e:
            logger.error(f"Ошибка в _get_or_create_user для telegram_id {telegram_user.id}: {e}", exc_info=True)
            return None

    async def _get_or_create_user_with_new_flag(self, telegram_user: AiogramUser) -> tuple[Optional[DBUser], bool]:
        existing_user_db = await self.db_service.get_user_by_telegram_id(telegram_user.id)
        is_new = False
        user_data_dict = {
            'username': telegram_user.username,
            'first_name': telegram_user.first_name,
            'last_name': telegram_user.last_name,
            'language_code': telegram_user.language_code
        }
        user_data_for_db = {k: v for k, v in user_data_dict.items() if v is not None}
        if not existing_user_db:
            user_db = await self.db_service.get_or_create_user(telegram_id=telegram_user.id, **user_data_for_db)
            if user_db:
                is_new = True
        else:
            user_db = await self.db_service.get_or_create_user(telegram_id=telegram_user.id, **user_data_for_db)
        return user_db, is_new

    async def _get_current_persona(self, telegram_id: int) -> str:
        db_user = await self.db_service.get_user_by_telegram_id(telegram_id)
        if not db_user: 
            logger.warning(f"User with telegram_id {telegram_id} not found in _get_current_persona. Defaulting to '{self.config.default_persona}'.")
            return self.config.default_persona
        preferences = await self.db_service.get_user_preferences(db_user.id, persona='system')
        return preferences.get('current_persona', self.config.default_persona)

    async def _start_background_tasks(self):
        logger.info("🌀 Запуск фоновых задач...")
        if self.notification_service and hasattr(self.notification_service, 'start') and callable(self.notification_service.start):
            asyncio.create_task(self.notification_service.start())
            logger.info("✅ Фоновые задачи NotificationService запланированы к запуску.")
        else: 
            logger.warning("NotificationService не имеет метода start() для запуска фоновых задач.")
        
        asyncio.create_task(periodic_rate_limiter_cleanup(self.db_service, interval_hours=24))
        logger.info("✅ Фоновая задача очистки UserActionTimestamp (RateLimiter) запланирована (раз в 24ч, старше 7 дней).")
        asyncio.create_task(periodic_temp_block_cleanup(self.db_service, interval_hours=1))
        logger.info("✅ Фоновая задача очистки TemporaryBlock (AntiSpam) запланирована (раз в 1ч, истекшие).")
        asyncio.create_task(periodic_summary_cleanup(self.db_service, interval_hours=24, days_to_keep=30))
        logger.info("✅ Фоновая задача очистки ContextSummary (старые саммари) запланирована (раз в 24ч, старше 30 дней).")

    async def start(self):
        try:
            await self.initialize()
            if not self.config.bot_username or \
               self.config.bot_username == "YOUR_BOT_USERNAME_HERE" or \
               self.config.bot_username == "УКАЖИТЕ_ИМЯ_ПОЛЬЗОВАТЕЛЯ_БОТА_В_.ENV":
                try:
                    bot_info = await self.bot.get_me()
                    if bot_info.username: 
                        self.config.bot_username = bot_info.username
                        logger.info(f"Имя пользователя бота установлено из API: @{self.config.bot_username}")
                    else: 
                        logger.error("Не удалось получить имя пользователя бота из API Telegram. Реферальные ссылки могут не работать.")
                        self.config.bot_username = "default_bot_username_api_failed" 
                except Exception as e_get_me: 
                    logger.error(f"Критическая ошибка при получении информации о боте: {e_get_me}. Невозможно установить bot_username.")
                    self.config.bot_username = "critical_bot_username_fetch_failed"
            
            await self._start_background_tasks()
            logger.info(f"🚀 AI Companion Bot @{self.config.bot_username} готов к работе!")
            logger.info("Перед вызовом start_polling...")
            allowed_updates_resolved = self.dp.resolve_used_update_types()
            logger.info(f"Allowed updates for polling: {allowed_updates_resolved}")
            await self.dp.start_polling(self.bot, allowed_updates=allowed_updates_resolved)
            logger.info("После вызова start_polling.")
        except ConfigurationError as ce: 
            logger.critical(f"КРИТИЧЕСКАЯ ОШИБКА КОНФИГУРАЦИИ ПРИ ЗАПУСКЕ: {ce}")
            await self.cleanup_on_error()
            sys.exit(1)
        except Exception as e: 
            logger.critical(f"❌ КРИТИЧЕСКАЯ ОШИБКА ПРИ ЗАПУСКЕ БОТА: {e}", exc_info=True)
            await self.cleanup_on_error()
            sys.exit(1)

    async def cleanup_on_error(self):
        logger.warning("Попытка очистки ресурсов после критической ошибки...")
        if self.dp and self.dp.storage and hasattr(self.dp.storage, 'close') and callable(self.dp.storage.close): 
            try: 
                await self.dp.storage.close()
                logger.info("FSM Storage закрыт (из cleanup_on_error).")
            except Exception as e_storage_close: 
                logger.error(f"Ошибка закрытия FSM Storage при cleanup_on_error: {e_storage_close}")
        services_to_close_session = [self.llm_service, self.tts_service]
        for service in services_to_close_session:
            if service and hasattr(service, 'session') and service.session and not service.session.closed:
                try: 
                    await service.session.close() 
                except Exception as e_svc_close: 
                    logger.error(f"Ошибка закрытия сессии сервиса {type(service).__name__}: {e_svc_close}")
        if self.db_service and self.db_service.connection_manager and self.db_service.connection_manager.engine:
            try: 
                await self.db_service.connection_manager.close()
                logger.info("DB Connection Manager закрыт (из cleanup_on_error).")
            except Exception as e_db_close: 
                logger.error(f"Ошибка закрытия DB Connection Manager: {e_db_close}")
        if self.bot and self.bot.session:
            try:
                await self.bot.session.close()
                logger.info("Сессия бота закрыта (из cleanup_on_error).")
            except Exception as e_bot_close: 
                logger.error(f"Ошибка закрытия сессии бота: {e_bot_close}")
        logger.info("Завершена попытка очистки ресурсов после ошибки.")

    async def cleanup(self):
        logger.info("🧹 Завершение работы AI Companion Bot и очистка ресурсов...")
        if self.notification_service and hasattr(self.notification_service, 'stop') and callable(self.notification_service.stop):
            logger.info("Остановка NotificationService...")
            await self.notification_service.stop()
        if self.dp.storage and hasattr(self.dp.storage, 'close') and callable(self.dp.storage.close):
            logger.info("Закрытие FSM Storage (SQLAlchemy)...")
            try: 
                await self.dp.storage.close()
            except Exception as e_storage_close: 
                logger.error(f"Ошибка при закрытии FSM Storage: {e_storage_close}")
        if self.llm_service and hasattr(self.llm_service, 'close') and callable(self.llm_service.close): 
            await self.llm_service.close()
        if self.tts_service and hasattr(self.tts_service, 'close') and callable(self.tts_service.close): 
            await self.tts_service.close()
        if self.db_service and hasattr(self.db_service, 'close') and callable(self.db_service.close): 
            await self.db_service.close()
        if self.bot and self.bot.session: 
            await self.bot.session.close()
        logger.info("🧼 Ресурсы успешно очищены. Бот остановлен.")

async def main_bot_runner():
    config = None
    try: 
        config = load_config()
        setup_logging(config) 
    except ConfigurationError as e: 
        print(f"CRITICAL CONFIGURATION ERROR: {e}")
        logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
        logging.critical(f"CRITICAL CONFIGURATION ERROR: {e}", exc_info=True)
        sys.exit(1)
    except Exception as e_log: 
        print(f"CRITICAL ERROR DURING EARLY INITIALIZATION (config/logging): {e_log}")
        logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
        logging.critical(f"CRITICAL ERROR DURING EARLY INITIALIZATION: {e_log}", exc_info=True)
        sys.exit(1)

    bot_instance = AICompanionBot(bot_config=config)
    try:
        await bot_instance.start()
    except KeyboardInterrupt:
        logger.info("👋 Получен сигнал KeyboardInterrupt. Завершение работы AICompanionBot...")
    except Exception as e: 
        logger.critical(f"💥 Глобальная неперехваченная ошибка в main_bot_runner после старта бота: {e}", exc_info=True)
    finally:
        logger.info("🌀 Процесс main_bot_runner завершается, вызов финальной очистки AICompanionBot...")
        await bot_instance.cleanup()

if __name__ == "__main__":
    if sys.platform == "win32" and sys.version_info >= (3, 8):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    try:
        asyncio.run(main_bot_runner())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен пользователем (из __main__)")
    except Exception as e_global:
        import traceback
        print(f"\n💥 КРИТИЧЕСКАЯ НЕОБРАБОТАННАЯ ОШИБКА В __main__: {e_global}")
        print("\nПолный traceback:")
        traceback.print_exc()
        if logger and logger.handlers: 
            logger.critical(f"КРИТИЧЕСКАЯ НЕОБРАБОТАННАЯ ОШИБКА В __main__: {e_global}", exc_info=True)