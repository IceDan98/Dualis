# handlers/navigation_handlers.py
import logging
import asyncio
from datetime import datetime, timezone
from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.utils.markdown import hbold, hitalic
from typing import TYPE_CHECKING, Optional, Union, Dict, Any, List, Callable, Awaitable # Added Callable, Awaitable

# Сервисы и утилиты из вашего проекта
from services.subscription_system import SubscriptionService, SubscriptionTier
from services.memory_service import MemoryService, MemoryType
from services.referral_ab_testing import ReferralService
from utils.error_handler import handle_errors # Removed ErrorHandler as it's not directly used here
from utils.navigation import navigation, create_pagination_buttons

from handlers.story_creation_fsm import start_story_creation # Removed StoryCreationFSM as it's not directly used here
# Import PromoCodeFSM for state management if needed, or handle through bot_instance
from handlers.payment_handlers import PromoCodeFSM


if TYPE_CHECKING:
    from main import AICompanionBot
    from database.operations import DatabaseService
    from database.models import User as DBUser
    from services.llm_service import LLMService
    # AdminPanel might be needed if admin actions are directly called from here
    from handlers.admin_panel import AdminPanel


logger = logging.getLogger(__name__)
enhanced_nav_router = Router()

# Constants for callback prefixes/data
SEXTING_MODE_CONVERSATIONAL_CALLBACK = "action_set_sexting_mode_conversational"
SEXTING_MODE_NARRATIVE_CALLBACK = "action_set_sexting_mode_narrative"
SEXTING_MODE_CANCEL_CALLBACK = "action_cancel_sexting_mode_selection"
QUEST_CHOOSE_THEME_CALLBACK_PREFIX = "action_quest_choose_theme_"

# Quest themes remain the same
QUEST_THEMES = {
    "mindful_sensations": {"name": "Неделя осознанных ощущений ✨", "first_task": "Сегодня обратите внимание на три обыденных ощущения (например, текстура ткани, вкус утреннего кофе, звук дождя) и коротко опишите их в журнале (/new_entry). Что нового вы заметили?"},
    "little_joys": {"name": "Поиск маленьких радостей 😊", "first_task": "Ваша задача на сегодня – найти и записать в журнал (/new_entry) три маленькие вещи, которые вызвали у вас улыбку или теплое чувство. Это может быть что угодно!"},
    "one_word_diary": {"name": "Дневник одного слова 📝", "first_task": "В конце дня выберите одно слово, которое лучше всего описывает ваши сегодняшние чувства или события. Запишите его в журнал (/new_entry) и, если хотите, добавьте короткий комментарий, почему именно это слово."},
    "shadow_exploration": {"name": "Исследование своей тени 🌗", "first_task": "Подумайте о качестве или черте характера, которую вы обычно стараетесь не замечать в себе или которая вам не нравится. Без осуждения, просто опишите ее в журнале (/new_entry) и подумайте, когда она проявляется."},
}

class NavigationHandler:
    """
    Handles navigation logic for the bot, including menu display and action processing.
    Refactored to use a dispatcher for callback actions.
    """
    def __init__(self, bot_instance: 'AICompanionBot'):
        self.bot_instance = bot_instance
        self.subscription_service: SubscriptionService = bot_instance.subscription_service
        self.memory_service: MemoryService = bot_instance.memory_service
        self.referral_service: ReferralService = bot_instance.referral_service
        self.db_service: 'DatabaseService' = bot_instance.db_service
        self.llm_service: 'LLMService' = bot_instance.llm_service

        # Dispatcher for navigation and action callbacks
        self.callback_dispatcher: Dict[str, Callable[[types.CallbackQuery, FSMContext, Dict[str, Any]], Awaitable[None]]] = {
            # Navigation actions (nav_)
            "nav_main": self._handle_nav_main,
            "nav_current_persona_settings": self._handle_nav_current_persona_settings,
            "nav_referral_dashboard": self._handle_nav_referral_dashboard,
            "nav_memory_overview": self._handle_nav_memory_overview,
            "nav_ai_insights": self._handle_nav_ai_insights,
            "nav_user_profile_view": self._handle_nav_user_profile_view,
            "nav_subscription_plans_view": self._handle_nav_subscription_plans_view,
            "nav_my_subscription_view": self._handle_nav_my_subscription_view,
            "nav_admin_main": self._handle_nav_admin_main,
            # Generic nav handler should be checked after specific ones
            # "nav_generic": self._handle_nav_generic, # Will be handled by a prefix check

            # Specific actions (action_)
            "action_close_menu": self._handle_action_close_menu,
            "action_enter_promocode_start": self._handle_action_enter_promocode_start,
            "action_cancel_promocode_entry": self._handle_action_cancel_promocode_entry,
            "action_compare_plans": self._handle_action_compare_plans,
            "action_i_want_you": self._handle_action_i_want_you,
            SEXTING_MODE_CONVERSATIONAL_CALLBACK: self._handle_action_set_sexting_mode_conversational, # Use constant
            SEXTING_MODE_NARRATIVE_CALLBACK: self._handle_action_set_sexting_mode_narrative,       # Use constant
            SEXTING_MODE_CANCEL_CALLBACK: self._handle_action_cancel_sexting_mode_selection, # Use constant
            "action_stop_sexting": self._handle_action_stop_sexting,
            "action_create_story_fsm": self._handle_action_create_story_fsm,
            "action_start_quest": self._handle_action_start_quest,
            # Prefix-based actions will need special handling in the main dispatcher logic
            # "action_switch_persona_": self._handle_action_switch_persona,
            # "action_set_vibe_aeris_": self._handle_action_set_vibe_aeris,
            # "action_set_sexting_level_": self._handle_action_set_sexting_level,
            # QUEST_CHOOSE_THEME_CALLBACK_PREFIX: self._handle_action_quest_choose_theme,
            # "admin_": self._handle_admin_actions,
        }

    # --- Helper methods (some might be similar to existing ones) ---
    async def _get_common_callback_data(self, callback: types.CallbackQuery) -> Dict[str, Any]:
        """Extracts common data needed by most callback handlers."""
        user_id_tg = callback.from_user.id
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: # Attempt to create if doesn't exist, critical for new users in callbacks
            aiogram_user = await self.bot_instance.bot.get_chat(user_id_tg)
            if isinstance(aiogram_user, types.User):
                db_user = await self.db_service.get_or_create_user(
                    telegram_id=user_id_tg, username=aiogram_user.username,
                    first_name=aiogram_user.first_name, last_name=aiogram_user.last_name
                )
            if not db_user:
                await callback.answer("Ошибка профиля пользователя.", show_alert=True)
                raise ValueError("User not found and could not be created in callback.")
        
        current_persona = await self.bot_instance._get_current_persona(user_id_tg)
        return {
            "user_id_tg": user_id_tg,
            "db_user": db_user,
            "current_persona": current_persona,
            "target_message_event": callback # Pass the callback itself for context
        }

    # --- Individual Handler Methods for Dispatcher ---

    async def _handle_nav_main(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self.show_main_menu(callback, state)

    async def _handle_nav_current_persona_settings(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        settings_node_id = "settings_aeris" if common_data["current_persona"] == "aeris" else "settings_luneth"
        await self._show_menu_node(common_data["target_message_event"], common_data["user_id_tg"], settings_node_id, common_data["current_persona"])
        await callback.answer()

    async def _handle_nav_referral_dashboard(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self.show_referral_dashboard(callback)

    async def _handle_nav_memory_overview(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self.show_memory_overview(callback)

    async def _handle_nav_ai_insights(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self.show_ai_insights(callback)
    
    async def _handle_nav_user_profile_view(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self.show_user_profile_view(callback)

    async def _handle_nav_subscription_plans_view(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self.show_subscription_plans_view(callback)

    async def _handle_nav_my_subscription_view(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self.show_my_subscription_view(callback)
    
    async def _handle_nav_admin_main(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        if not (common_data["user_id_tg"] in self.bot_instance.config.admin_user_ids):
            await callback.answer("Доступ запрещен.", show_alert=True); return
        admin_title = "🛠️ " + hbold("Административная панель") + "\n\nДобро пожаловать!"
        await self._show_menu_node(common_data["target_message_event"], common_data["user_id_tg"], "admin_main", common_data["current_persona"], title_override=admin_title)
        await callback.answer()

    async def _handle_nav_generic(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any], target_node_id: str):
        """Handles generic nav_NODE_ID callbacks."""
        await self._show_menu_node(common_data["target_message_event"], common_data["user_id_tg"], target_node_id, common_data["current_persona"])
        await callback.answer()

    async def _handle_action_switch_persona(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any], new_persona: str):
        access_key = "can_access_luneth" if new_persona == "luneth" else None
        if access_key:
            user_conditions_for_check = await self._get_current_user_conditions(common_data["user_id_tg"], common_data["current_persona"])
            if not user_conditions_for_check.get(access_key, True):
                await self.show_subscription_upgrade_prompt(common_data["target_message_event"], common_data["user_id_tg"], access_key)
                await callback.answer(); return
        await self.db_service.update_user_preference(common_data["db_user"].id, 'current_persona', new_persona, persona='system', preference_type='string')
        await callback.answer(f"Персона изменена на {new_persona.title()}!", show_alert=False)
        await self.show_main_menu(callback, state)

    async def _handle_action_set_vibe_aeris(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any], new_vibe: str):
        if common_data["current_persona"] != "aeris":
            await callback.answer("Эта настройка только для Aeris.", show_alert=True); return
        await self.db_service.update_conversation_settings(common_data["db_user"].id, "aeris", {"current_vibe": new_vibe})
        await callback.answer(f"Вайб Aeris установлен на '{new_vibe}'.", show_alert=False)
        await self._show_menu_node(common_data["target_message_event"], common_data["user_id_tg"], "settings_aeris", common_data["current_persona"])

    async def _handle_action_set_sexting_level(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any], level_str: str):
        if common_data["current_persona"] != "luneth":
            await callback.answer("Эта настройка только для Luneth.", show_alert=True); return
        try:
            level = int(level_str)
            level_access = await self.subscription_service.check_feature_access(common_data["user_id_tg"], "sexting_level", level=level)
            if not level_access.get("allowed"):
                await self.show_subscription_upgrade_prompt(common_data["target_message_event"], common_data["user_id_tg"], "sexting_level_too_high", required_tier_override=SubscriptionTier.PREMIUM)
                await callback.answer(); return
            await self.db_service.update_conversation_settings(common_data["db_user"].id, "luneth", {"sexting_level": level})
            await callback.answer(f"Уровень страсти Luneth: {level}.", show_alert=False)
            await self._show_menu_node(common_data["target_message_event"], common_data["user_id_tg"], "settings_luneth", common_data["current_persona"])
        except ValueError: await callback.answer("Ошибка установки уровня.", show_alert=True)

    async def _handle_action_i_want_you(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        if common_data["current_persona"] != "luneth": 
             current_persona_settings_node = "settings_aeris" if common_data["current_persona"] == "aeris" else "main"
             await callback.answer(f"Сейчас активна {common_data['current_persona'].title()}. Для этого действия переключитесь на Luneth или выберите другую опцию.", show_alert=True)
             await self._show_menu_node(common_data["target_message_event"], common_data["user_id_tg"], current_persona_settings_node, common_data["current_persona"])
             return
        await self.db_service.update_conversation_settings(common_data["db_user"].id, "luneth", {"sexting_level": 10})
        await self._propose_sexting_mode(common_data["target_message_event"], common_data["user_id_tg"], common_data["current_persona"])
        await callback.answer()

    async def _handle_action_set_sexting_mode_conversational(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self._start_sexting_interaction(callback, common_data["user_id_tg"], common_data["current_persona"], "conversational", state)

    async def _handle_action_set_sexting_mode_narrative(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self._start_sexting_interaction(callback, common_data["user_id_tg"], common_data["current_persona"], "narrative", state)
    
    async def _handle_action_cancel_sexting_mode_selection(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await callback.answer("Выбор режима отменен.", show_alert=False)
        await self.show_main_menu(callback, state)

    async def _handle_action_stop_sexting(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        if common_data["current_persona"] != "luneth": await callback.answer("Только для Luneth!", show_alert=True); return
        await self.db_service.update_conversation_settings(common_data["db_user"].id, "luneth", {"sexting_level": 0})
        user_conditions = await self._get_current_user_conditions(common_data["user_id_tg"], common_data["current_persona"])
        quick_actions_markup = navigation.create_quick_actions_menu(common_data["current_persona"], user_conditions)
        if callback.message: # Check if message exists
            await callback.message.edit_text("😌 Хорошо, мой дорогой. Немного остынем... но я всегда готова, если ты захочешь.", reply_markup=quick_actions_markup)
        await callback.answer("Режим страсти деактивирован.", show_alert=False)

    async def _handle_action_create_story_fsm(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await start_story_creation(common_data["target_message_event"], state, self.bot_instance)
        await callback.answer()

    async def _handle_action_start_quest(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await self.handle_start_quest_action(callback, state) # Assuming this method exists in NavigationHandler

    async def _handle_action_quest_choose_theme(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any], theme_key: str):
         # The original logic was in handle_quest_theme_chosen, adapting it here
        if common_data["current_persona"] != "aeris":
            await callback.answer("Эта функция доступна только для персоны Aeris.", show_alert=True); return

        if theme_key in QUEST_THEMES:
            selected_theme = QUEST_THEMES[theme_key]
            await self.db_service.update_user_preference(
                user_id_db=common_data["db_user"].id, 
                key="active_quest_theme", value=theme_key, persona=common_data["current_persona"], preference_type="string"
            )
            await self.db_service.update_user_preference(
                user_id_db=common_data["db_user"].id, 
                key=f"quest_{theme_key}_start_date", value=datetime.now(timezone.utc).isoformat(),
                persona=common_data["current_persona"], preference_type="string"
            )
            response_text = f"🚀 Отлично! Мы начинаем квест: {hbold(selected_theme['name'])}\n\n"
            response_text += f"✨ {hbold('Твое первое задание:')}\n{selected_theme['first_task']}\n\n"
            response_text += "Удачи! Не забывай делиться своими мыслями в журнале."
            await self._show_menu_node(common_data["target_message_event"], common_data["user_id_tg"], "activities", common_data["current_persona"], title_override=response_text)
            await callback.answer(f"Квест '{selected_theme['name']}' начат!")
        else:
            await callback.answer("Выбрана неизвестная тема квеста.", show_alert=True)

    async def _handle_action_close_menu(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        try:
            if callback.message: await callback.message.delete()
        except Exception as e: logger.warning(f"Не удалось удалить сообщение при закрытии меню: {e}")
        await callback.answer("Меню закрыто.")

    async def _handle_action_enter_promocode_start(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        await state.set_state(PromoCodeFSM.waiting_for_code_entry)
        if callback.message: # Check if message exists
            await state.update_data(last_menu_message_id=callback.message.message_id, last_menu_chat_id=callback.message.chat.id)
            await callback.message.edit_text(
                "🎁 Введите ваш промокод или нажмите 'Отмена':",
                reply_markup=types.InlineKeyboardMarkup(inline_keyboard=[
                    [types.InlineKeyboardButton(text="⬅️ Отмена", callback_data="action_cancel_promocode_entry")]
                ])
            )
        await callback.answer()

    async def _handle_action_cancel_promocode_entry(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
         await state.clear()
         await self._show_menu_node(common_data["target_message_event"], common_data["user_id_tg"], "profile_premium_main", common_data["current_persona"])
         await callback.answer("Ввод промокода отменен.")
    
    async def _handle_action_compare_plans(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any]):
        # Placeholder for actual implementation or call to payment_handlers
        await callback.answer("Функция сравнения тарифов будет здесь.", show_alert=True)

    async def _handle_admin_actions(self, callback: types.CallbackQuery, state: FSMContext, common_data: Dict[str, Any], admin_action_name: str):
        if not (common_data["user_id_tg"] in self.bot_instance.config.admin_user_ids):
            await callback.answer("Доступ запрещен.", show_alert=True); return
        
        # Example:
        if admin_action_name == "reload_all_prompts":
            self.bot_instance.dp.workflow_data['prompt_manager'].reload_prompts()
            if self.bot_instance.llm_service: self.bot_instance.llm_service.clear_system_prompts_cache()
            await callback.answer("Промпты персон перезагружены.", show_alert=True)
        else:
            await callback.answer(f"Админ-действие '{admin_action_name}' в разработке.", show_alert=True)


    # --- Main Callback Handler using Dispatcher ---
    @handle_errors() # Apply error handling to the main dispatcher
    async def main_callback_dispatcher(self, callback: types.CallbackQuery, state: FSMContext):
        if not callback.message or not callback.from_user: # Basic check
            await callback.answer("Ошибка: не удалось обработать запрос.")
            return

        action_full = callback.data
        logger.debug(f"Callback received: {action_full} from user {callback.from_user.id}")
        
        common_data = await self._get_common_callback_data(callback) # Fetch common data once

        # 1. Direct match in dispatcher
        handler_method = self.callback_dispatcher.get(action_full)
        if handler_method:
            await handler_method(callback, state, common_data)
            return

        # 2. Prefix-based matching for dynamic actions
        if action_full.startswith("nav_"):
            target_node_id = action_full.split("_", 1)[1]
            await self._handle_nav_generic(callback, state, common_data, target_node_id)
            return
        elif action_full.startswith("action_switch_persona_"):
            new_persona = action_full.split("action_switch_persona_")[-1]
            await self._handle_action_switch_persona(callback, state, common_data, new_persona)
            return
        elif action_full.startswith("action_set_vibe_aeris_"):
            new_vibe = action_full.split("action_set_vibe_aeris_")[-1]
            await self._handle_action_set_vibe_aeris(callback, state, common_data, new_vibe)
            return
        elif action_full.startswith("action_set_sexting_level_"):
            level_str = action_full.split("action_set_sexting_level_")[-1]
            await self._handle_action_set_sexting_level(callback, state, common_data, level_str)
            return
        elif action_full.startswith(QUEST_CHOOSE_THEME_CALLBACK_PREFIX.split("action_")[-1]): # remove "action_"
            theme_key = action_full.split(QUEST_CHOOSE_THEME_CALLBACK_PREFIX.split("action_")[-1])[-1]
            await self._handle_action_quest_choose_theme(callback, state, common_data, theme_key)
            return
        elif action_full.startswith("admin_"): # Generic admin action prefix
            admin_action_name = action_full.split("admin_")[-1]
            await self._handle_admin_actions(callback, state, common_data, admin_action_name)
            return
            
        # Fallback for unknown actions
        logger.warning(f"Неизвестный callback: {action_full} для user {common_data['user_id_tg']}")
        await callback.answer(f"Действие '{action_full}' пока не реализовано или неизвестно.", show_alert=True)

    # --- Methods from the original NavigationHandler (show_main_menu, etc.) ---
    # These methods are now part of this refactored NavigationHandler class.
    # Ensure they use self.bot_instance, self.db_service etc. correctly.
    # (The content of these methods is largely the same as provided in your original file,
    # I'm omitting them here for brevity but they should be part of this class)

    async def _get_current_user_conditions(self, user_id_tg: int, current_persona: str) -> Dict[str, Any]:
        # (Implementation as provided in the original file)
        user_db: Optional[DBUser] = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not user_db: 
            aiogram_user = await self.bot_instance.bot.get_chat(user_id_tg)
            if isinstance(aiogram_user, types.User):
                user_db = await self.db_service.get_or_create_user(
                    telegram_id=user_id_tg, username=aiogram_user.username,
                    first_name=aiogram_user.first_name, last_name=aiogram_user.last_name
                )
            else: 
                logger.warning(f"Не удалось получить AiogramUser для user_id_tg {user_id_tg} в _get_current_user_conditions")
                return {"is_admin": user_id_tg in self.bot_instance.config.admin_user_ids}
            if not user_db: # Still no user_db after creation attempt
                 logger.error(f"Не удалось создать пользователя для TG ID {user_id_tg} в _get_current_user_conditions")
                 return {"is_admin": user_id_tg in self.bot_instance.config.admin_user_ids}


        user_sub_data = await self.subscription_service.get_user_subscription(user_id_tg)
        conv_settings = await self.db_service.get_conversation_settings(user_db.id, current_persona)

        conditions = {
            "is_admin": user_id_tg in self.bot_instance.config.admin_user_ids,
            "is_subscribed": user_sub_data.get("tier", "free") != SubscriptionTier.FREE.value,
            "is_premium_or_higher": self.subscription_service.plans.TIER_HIERARCHY.get(SubscriptionTier(user_sub_data.get("tier", "free")), 0) >= self.subscription_service.plans.TIER_HIERARCHY[SubscriptionTier.PREMIUM],
            "is_basic_or_higher": self.subscription_service.plans.TIER_HIERARCHY.get(SubscriptionTier(user_sub_data.get("tier", "free")), 0) >= self.subscription_service.plans.TIER_HIERARCHY[SubscriptionTier.BASIC],
            "can_access_luneth": self.subscription_service.plans.TIER_HIERARCHY.get(SubscriptionTier(user_sub_data.get("tier", "free")), 0) >= self.subscription_service.plans.TIER_HIERARCHY[SubscriptionTier.BASIC],
            "can_create_fantasy": user_sub_data.get("limits", {}).get("custom_fantasies_allowed", False), # Corrected key
            "current_persona": current_persona,
            "current_vibe_aeris": conv_settings.get("current_vibe") if current_persona == "aeris" else None,
            "current_sexting_level_luneth": conv_settings.get("sexting_level") if current_persona == "luneth" else None,
        }
        return conditions

    async def _show_menu_node(self,
                              target_message_event: Union[types.Message, types.CallbackQuery],
                              user_id_tg: int,
                              node_id: str,
                              current_persona: str,
                              title_override: Optional[str] = None,
                              pagination_cb_prefix: Optional[str] = None,
                              current_page: int = 1,
                              total_pages: int = 1,
                              custom_reply_markup: Optional[types.InlineKeyboardMarkup] = None):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        user_conditions = await self._get_current_user_conditions(user_id_tg, current_persona)
        pagination_markup_buttons = None
        if pagination_cb_prefix and total_pages > 1:
            pagination_markup_buttons = create_pagination_buttons(current_page, total_pages, pagination_cb_prefix)
        
        menu_markup_to_use = custom_reply_markup
        if not menu_markup_to_use:
            menu_markup_to_use = navigation.create_markup(node_id, current_persona, user_conditions, pagination_markup_buttons)

        node_obj = navigation.get_node(node_id)
        menu_text = title_override
        if not menu_text:
            if node_obj:
                base_text = node_obj.get_text(user_conditions)
                if node_obj.children or node_id == "main": menu_text = f"{base_text}\n\nВыберите опцию:"
                else: menu_text = base_text # For leaf nodes, just text
            else: menu_text = "Меню не найдено."
        
        target_message_to_edit_or_answer: Optional[types.Message] = None
        if isinstance(target_message_event, types.CallbackQuery): 
            target_message_to_edit_or_answer = target_message_event.message
        elif isinstance(target_message_event, types.Message): 
            target_message_to_edit_or_answer = target_message_event # Answer this message

        if not target_message_to_edit_or_answer:
             logger.error(f"Не удалось определить target_message для _show_menu_node (node_id: {node_id})")
             if isinstance(target_message_event, types.CallbackQuery):
                 await target_message_event.answer("Ошибка отображения меню.", show_alert=True)
             return
        try:
            # Edit if it's a callback query and the message content or markup needs changing
            if isinstance(target_message_event, types.CallbackQuery) and \
               target_message_to_edit_or_answer and \
               (target_message_to_edit_or_answer.text != menu_text or target_message_to_edit_or_answer.reply_markup != menu_markup_to_use):
                await target_message_to_edit_or_answer.edit_text(menu_text, reply_markup=menu_markup_to_use, parse_mode="Markdown")
            # Answer if it's a message command
            elif isinstance(target_message_event, types.Message):
                 await target_message_event.answer(menu_text, reply_markup=menu_markup_to_use, parse_mode="Markdown")
            # If it's a callback but no edit is needed (e.g. just answering the callback)
            # elif isinstance(target_message_event, types.CallbackQuery):
            #    pass # Answer will be called by the dispatcher if not already
        except Exception as e: # Broad exception for Telegram API errors like message not modified, etc.
            logger.error(f"Ошибка обновления/отправки меню '{node_id}' для user {user_id_tg}: {e}", exc_info=True)
            # Fallback to sending a new message if editing failed for a callback
            if isinstance(target_message_event, types.CallbackQuery) and target_message_to_edit_or_answer:
                await self.bot_instance.bot.send_message(target_message_to_edit_or_answer.chat.id, menu_text, reply_markup=menu_markup_to_use, parse_mode="Markdown")


    async def show_main_menu(self, message_or_callback: Union[types.Message, types.CallbackQuery], state: Optional[FSMContext] = None):
        # (Implementation as provided in the original file)
        user = message_or_callback.from_user
        target_event = message_or_callback
        if not user:
            logger.warning("show_main_menu: Не удалось получить пользователя.")
            if isinstance(message_or_callback, types.CallbackQuery): await message_or_callback.answer("Ошибка отображения меню.")
            return
        if state: await state.clear()
        current_persona = await self.bot_instance._get_current_persona(user.id)
        user_sub_data = await self.subscription_service.get_user_subscription(user.id)
        menu_title = await self._get_personalized_menu_title(user.id, user_sub_data, current_persona)
        await self._show_menu_node(target_event, user.id, "main", current_persona, title_override=menu_title)
        if isinstance(message_or_callback, types.CallbackQuery):
            await message_or_callback.answer()


    async def _get_personalized_menu_title(self, user_id_tg: int, subscription: Dict, current_persona: str) -> str:
        # (Implementation as provided in the original file)
        tier_name = subscription.get("tier_name", self.subscription_service._get_tier_name(SubscriptionTier.FREE.value))
        persona_emoji = "🌟" if current_persona == "aeris" else "😈"
        user_info = await self.bot_instance.bot.get_chat(user_id_tg)
        user_display_name = user_info.first_name or user_info.username or "Гость"
        
        text = f"Привет, {hbold(user_display_name)}! 👋\n"
        text += f"🎮 **Главное меню**\n\n"
        text += f"{persona_emoji} Персона: **{current_persona.title()}**\n"
        text += f"💎 Тариф: **{tier_name}**\n"
        
        limit_check = await self.subscription_service.check_message_limit(user_id_tg)
        if limit_check.get("unlimited", False):
            text += f"💬 Сообщения: Безлимит (сегодня: {limit_check.get('used',0)})\n"
        else:
            text += f"💬 Сообщения: {limit_check.get('used',0)}/{limit_check.get('effective_limit', 0)} (осталось: {limit_check.get('remaining',0)})\n"
        
        if subscription.get("tier", "free") == SubscriptionTier.FREE.value and not limit_check.get("unlimited", False):
             text += "\n🚀 Хотите больше общения и функций? Рассмотрите Premium!"
        text += "\n\nВыберите действие:"
        return text

    async def show_subscription_upgrade_prompt(self, target_message_event: Union[types.Message, types.CallbackQuery], user_id_tg: int, feature_name_key: str, required_tier_override: Optional[SubscriptionTier] = None):
        # (Implementation as provided in the original file)
        required_tier_map = {
            "voice_messages": SubscriptionTier.BASIC, "ai_insights": SubscriptionTier.PREMIUM,
            "luneth_advanced": SubscriptionTier.PREMIUM, "permanent_memory": SubscriptionTier.VIP,
            "custom_fantasies": SubscriptionTier.BASIC, "can_access_luneth": SubscriptionTier.BASIC,
             "sexting_level_too_high": SubscriptionTier.PREMIUM 
        }
        feature_display_names = {
            "voice_messages": "голосовые сообщения", "ai_insights": "AI-инсайты",
            "luneth_advanced": "расширенные возможности Luneth", "permanent_memory": "постоянную память",
            "custom_fantasies": "создание персональных фантазий", "can_access_luneth": "персону Luneth",
            "sexting_level_too_high": "более высокий уровень страсти"
        }
        required_tier = required_tier_override or required_tier_map.get(feature_name_key, SubscriptionTier.BASIC)
        feature_display = feature_display_names.get(feature_name_key, f"функцию '{feature_name_key}'")
        required_tier_name_display = self.subscription_service._get_tier_name(required_tier.value)
        
        text = (f"🔒 Для доступа к функции **'{feature_display}'** необходима подписка "
                f"**'{required_tier_name_display}'** или выше.\n\nУлучшите ваш опыт общения!")
        buttons_rows = [
            [types.InlineKeyboardButton(text=f"⭐ Перейти к тарифу '{required_tier_name_display}'", callback_data=f"nav_subscription_plans_view")],
            [types.InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="nav_main")]
        ]
        markup = types.InlineKeyboardMarkup(inline_keyboard=buttons_rows)
        
        target_message_to_handle: Optional[types.Message] = None
        if isinstance(target_message_event, types.CallbackQuery): 
            target_message_to_handle = target_message_event.message
        elif isinstance(target_message_event, types.Message): 
            target_message_to_handle = target_message_event
        
        if target_message_to_handle:
            try: 
                if isinstance(target_message_event, types.CallbackQuery): # Edit if it's a callback
                    await target_message_to_handle.edit_text(text, reply_markup=markup, parse_mode="Markdown")
                else: # Answer if it's a message
                    await target_message_to_handle.answer(text, reply_markup=markup, parse_mode="Markdown")
            except Exception: # Fallback to sending a new message
                 await self.bot_instance.bot.send_message(user_id_tg, text, reply_markup=markup, parse_mode="Markdown")
        else: # Should not happen if target_message_event is valid
            await self.bot_instance.bot.send_message(user_id_tg, text, reply_markup=markup, parse_mode="Markdown")


    async def show_referral_dashboard(self, callback: types.CallbackQuery):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        if not callback.from_user or not callback.message: return await callback.answer("Ошибка")
        user_id_tg = callback.from_user.id
        referral_info = await self.referral_service.get_user_referral_dashboard_info(user_id_tg)

        if referral_info.get("error"):
            await callback.message.edit_text(f"Ошибка: {referral_info['error']}"); await callback.answer(); return
        
        text = f"👥 {hbold('Ваша Реферальная Программа')}\n\n"
        text += f"🔗 Ваш реферальный код: `{referral_info.get('referral_code', 'N/A')}`\n"
        referral_link = referral_info.get('referral_link', '#')
        text += f"🔗 Ваша реферальная ссылка для приглашения:\n`{referral_link}`\n\n"
        text += f"🙋‍♂️ Приглашено друзей (перешли по ссылке): **{referral_info.get('initiated_referrals', 0)}**\n"
        text += f"✅ Успешных рефералов (совершили целевое действие): **{referral_info.get('completed_referrals', 0)}**\n\n"
        
        next_milestone = referral_info.get("next_milestone")
        if next_milestone:
            text += f"🎯 {hbold('Следующая цель:')} {next_milestone['reward_description']}\n"
            if next_milestone.get('needed', 0) > 0 : text += f"   Осталось пригласить: **{next_milestone['needed']}** (из {next_milestone['total_for_milestone']})\n\n"
            else: text += f"   Цель достигнута! Ожидайте начисления награды.\n\n"
        else: text += "🎉 Поздравляем! Все основные реферальные цели достигнуты!\n\n"
        
        applied_rewards: List[Dict] = referral_info.get("applied_rewards", [])
        if applied_rewards:
            text += f"🎁 {hbold('Ваши полученные награды:')}\n"
            for reward in applied_rewards[:3]: # Show first 3
                reward_desc = reward.get('description', 'Награда')
                granted_at_str = reward.get('granted_at')
                granted_date_display = datetime.fromisoformat(granted_at_str.replace('Z','+00:00')).strftime('%d.%m.%Y') if granted_at_str else 'N/A'
                text += f"  - {reward_desc} (от {granted_date_display})\n"
            if len(applied_rewards) > 3: text += "  ... и другие.\n"
            text += "\n"
        
        text += f"💡 {hbold('Как это работает?')}\n"
        text += f"1. Поделитесь вашим реферальным кодом или ссылкой с друзьями.\n"
        text += f"2. Ваш друг вводит код при старте бота (`/start ВАШ_КОД`) или переходит по ссылке.\n"
        text += f"3. Вы получаете: **{self.referral_service.DEFAULT_REFERRER_REWARD.description}**.\n"
        text += f"4. Ваш друг получает: **{self.referral_service.DEFAULT_REFEREE_REWARD.description}**.\n"
        text += f"5. Когда ваш друг совершает первую платную покупку, вы получаете: **{self.referral_service.SUCCESSFUL_REFERRAL_BONUS_FOR_REFERRER.description}**.\n"
        text += "Достигайте целей по количеству успешных рефералов и получайте еще больше бонусов!\n"
        
        buttons = [
            [types.InlineKeyboardButton(text="🔗 Скопировать ссылку", switch_inline_query=referral_link)],
            [types.InlineKeyboardButton(text="⬅️ Назад", callback_data="nav_profile_premium_main")]
        ]
        await callback.message.edit_text(text, reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown", disable_web_page_preview=True)
        await callback.answer()


    async def show_memory_overview(self, callback: types.CallbackQuery):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        user_id_tg = callback.from_user.id; current_persona = await self.bot_instance._get_current_persona(user_id_tg)
        stats = await self.memory_service.get_memory_stats(user_id_tg)

        if "error" in stats and stats.get("upgrade_required"):
            await self.show_subscription_upgrade_prompt(callback, user_id_tg, "permanent_memory"); await callback.answer(); return
        elif "error" in stats:
             await callback.message.edit_text(f"Ошибка получения статистики памяти: {stats['error']}"); await callback.answer(); return
        
        text = f"{hbold('🧠 Статистика вашей памяти')}\n\n"
        text += f"Тип хранения: **{stats.get('storage_type_description', 'N/A')}**\n"
        text += f"Активных воспоминаний: **{stats.get('total_active_memories', 0)}**\n"
        text += f"Лимит записей: **{stats.get('max_entries_limit', 'N/A')}** ({stats.get('usage_percentage', 0):.0f}% использовано)\n"
        text += f"Срок хранения: **{stats.get('retention_days_display', 'N/A')}**\n\n"
        
        type_breakdown = stats.get('content_type_breakdown', {})
        if type_breakdown:
            text += f"{hbold('Распределение по типам контента:')}\n"
            for type_name, count in type_breakdown.items(): text += f"  - {type_name.title()}: {count}\n"
            text += "\n"
        
        priority_breakdown = stats.get('priority_breakdown', {})
        if priority_breakdown:
            text += f"{hbold('Распределение по приоритетам:')}\n"
            for prio_name, count in priority_breakdown.items(): text += f"  - {prio_name.replace('_', ' ').title()}: {count}\n" # Use replace for enum names
            text += "\n"
            
        text += f"Средний эмоциональный вес: **{stats.get('avg_emotional_weight', 0.0):.2f}**\n"
        text += f"Всего обращений к памяти: **{stats.get('total_accesses', 0)}**\n"
        
        await self._show_menu_node(callback, user_id_tg, "memory_overview", current_persona, title_override=text)
        await callback.answer()


    async def show_ai_insights(self, callback: types.CallbackQuery):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        user_id_tg = callback.from_user.id; current_persona = await self.bot_instance._get_current_persona(user_id_tg)
        access_check = await self.subscription_service.check_feature_access(user_id_tg, "ai_insights")
        if not access_check.get("allowed", False):
            await self.show_subscription_upgrade_prompt(callback, user_id_tg, "ai_insights"); await callback.answer(); return

        insights = await self.memory_service.get_memory_insights(user_id_tg)
        text = f"{hbold('💡 Ваши AI-Инсайты')}\n\n"
        if insights.get("error") and insights.get("upgrade_required"):
            await self.show_subscription_upgrade_prompt(callback, user_id_tg, "ai_insights"); await callback.answer(); return
        elif insights.get("error"): text += insights["error"]
        elif insights.get("message"): text += insights["message"]
        else:
            text += f"Проанализировано воспоминаний: **{insights.get('total_memories_analyzed', 0)}**\n\n"
            dist = insights.get('memory_content_types_distribution', {}); 
            if dist:
                text += f"{hbold('Распределение типов воспоминаний:')}\n"; 
                for type_name, count in dist.items(): text += f"  - {type_name.title()}: {count}\n"
                text += "\n"
            emo_profile = insights.get('emotional_tags_profile', {})
            if emo_profile and any(emo_profile.values()): # Check if any emotion has count > 0
                text += f"{hbold('Эмоциональный профиль (по тегам в памяти):')}\n"; 
                for emo_tag, count in emo_profile.items():
                    if count > 0: text += f"  - {emo_tag.title()}: {count}\n"
                text += "\n"
            topics = insights.get('top_recurring_topics', [])
            if topics:
                text += f"{hbold('Часто встречающиеся темы:')}\n"; 
                for topic_info in topics: text += f"  - {topic_info.get('topic','N/A').capitalize()}: {topic_info.get('count','N/A')} раз\n"
                text += "\n"
            patterns = insights.get('behavioral_patterns', [])
            if patterns:
                text += f"{hbold('Возможные паттерны поведения:')}\n"; 
                for pattern_desc in patterns: text += f"  - {pattern_desc}\n"
                text += "\n"
            recommendations = insights.get('personalized_recommendations', [])
            if recommendations:
                text += f"{hbold('Персональные рекомендации:')}\n"; 
                for rec_text in recommendations: text += f"  - {rec_text}\n"
        
        await self._show_menu_node(callback, user_id_tg, "ai_insights", current_persona, title_override=text)
        await callback.answer()


    async def show_user_profile_view(self, callback: types.CallbackQuery):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        user_id_tg = callback.from_user.id; user_tg_obj = callback.from_user; current_persona = await self.bot_instance._get_current_persona(user_id_tg)
        subscription = await self.subscription_service.get_user_subscription(user_id_tg)
        
        text = f"👤 **Ваш профиль, {hbold(user_tg_obj.first_name or 'Гость')}**\n\n"
        text += f"Telegram ID: `{user_tg_obj.id}`\n"
        if user_tg_obj.username: text += f"Username: @{user_tg_obj.username}\n"
        text += f"\n💎 **Подписка**\n"; text += f"Тариф: **{subscription.get('tier_name', 'N/A')}**\n"
        text += f"Статус: **{subscription.get('status', 'N/A').replace('_',' ').title()}**\n"
        
        expires_at_str = subscription.get("expires_at")
        if expires_at_str and subscription.get("tier", "free") != "free": # Check if tier is not free
            try:
                expires_dt = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                if expires_dt.tzinfo is None: expires_dt = expires_dt.replace(tzinfo=timezone.utc) # Ensure tz aware
                days_left = (expires_dt - datetime.now(timezone.utc)).days
                if days_left >= 0: 
                    text += f"Действует до: {expires_dt.strftime('%d.%m.%Y')} ({days_left + 1} дн. осталось)\n" # +1 for more natural "days left"
                else: 
                    text += f"Истекла: {expires_dt.strftime('%d.%m.%Y')}\n"
            except ValueError: 
                text += f"Дата истечения: {expires_at_str} (не удалось распознать)\n"
        
        await self._show_menu_node(callback, user_id_tg, "user_profile_view", current_persona, title_override=text)
        await callback.answer()


    async def show_subscription_plans_view(self, callback: types.CallbackQuery):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        user_id_tg = callback.from_user.id; current_persona = await self.bot_instance._get_current_persona(user_id_tg)
        title = "⭐ " + hbold("Выберите тариф Premium") + "\n\nОзнакомьтесь с преимуществами и выберите подходящий план:"
        await self._show_menu_node(callback, user_id_tg, "subscription_plans_view", current_persona, title_override=title)
        await callback.answer()


    async def show_my_subscription_view(self, callback: types.CallbackQuery):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        user_id_tg = callback.from_user.id; current_persona = await self.bot_instance._get_current_persona(user_id_tg)
        sub_menu_data = await self.subscription_service.get_subscription_menu(user_id_tg)
        title = sub_menu_data["text"]; custom_markup = sub_menu_data["reply_markup"]
        await self._show_menu_node(callback, user_id_tg, "my_subscription_view", current_persona, title_override=title, custom_reply_markup=custom_markup)
        # await callback.answer() # Answer is called in _show_menu_node if message is edited


    async def _propose_sexting_mode(self, target_message_event: Union[types.Message, types.CallbackQuery], user_id_tg: int, current_persona: str):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        user_info = await self.bot_instance.bot.get_chat(user_id_tg)
        user_display_name = user_info.first_name or "мой хороший"
        text = (
            f"🔥 Похоже, мы переходим к самому интересному, {hbold(user_display_name)}!\n\n"
            "Как ты хочешь продолжить?\n\n"
            f"💋 {hbold('Интерактивный чат')}: Быстрый обмен горячими сообщениями, живое общение и действия в реальном времени.\n"
            f"📖 {hbold('Эротическая история')}: Я создам для нас подробный и чувственный рассказ, где мы будем главными героями.\n\n"
            "Выбирай!"
        )
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
            [types.InlineKeyboardButton(text="💋 Интерактивный чат", callback_data=SEXTING_MODE_CONVERSATIONAL_CALLBACK)],
            [types.InlineKeyboardButton(text="📖 Рассказать историю", callback_data=SEXTING_MODE_NARRATIVE_CALLBACK)],
            [types.InlineKeyboardButton(text="🚫 Отмена", callback_data=SEXTING_MODE_CANCEL_CALLBACK)]
        ])
        # Using a placeholder node_id for _show_menu_node as this is a specific interaction, not a standard menu node
        await self._show_menu_node(target_message_event, user_id_tg, "propose_sexting_mode_node", current_persona, title_override=text, custom_reply_markup=keyboard)


    async def _start_sexting_interaction(self, callback: types.CallbackQuery, user_id_tg: int, current_persona: str, sexting_mode: str, state: FSMContext):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        if not callback.message: return await callback.answer("Ошибка")
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)
        if not db_user: await callback.answer("Ошибка пользователя.", show_alert=True); return
        
        await self.db_service.update_user_preference(db_user.id, 'current_sexting_mode', sexting_mode, persona='system', preference_type='string')
        await self.db_service.update_conversation_settings(db_user.id, current_persona, {"sexting_level": 10}) # Max level for sexting
        
        mode_display = "интерактивного чата" if sexting_mode == "conversational" else "создания эротической истории"
        initial_prompt_text = f"Хорошо, мой желанный! Начинаем режим {mode_display}. Что у тебя на уме? 😏"
        if sexting_mode == "narrative":
            initial_prompt_text = (
                f"Отлично! Я готова соткать для нас горячую историю. 📝\n"
                f"Расскажи, о чем она будет? Какие у тебя есть идеи, желания, ключевые моменты или фетиши, которые ты хотел бы увидеть? "
                f"Ты можешь описать всё подробно или просто сказать '{hitalic('удиви меня!')}'"
            )
        
        user_tg_info = await self.bot_instance.bot.get_chat(user_id_tg)
        dynamic_context = {
            "user_name": user_tg_info.first_name or "мой желанный", "sexting_mode": sexting_mode, "passion_level": 10
        }
        if current_persona == "aeris":
            conv_settings = await self.db_service.get_conversation_settings(db_user.id, "aeris")
            dynamic_context["current_vibe"] = conv_settings.get("current_vibe", "passionate") # Default to passionate for Aeris in sexting
        
        llm_seed_message = f"Мы начинаем взаимодействие в режиме '{sexting_mode}'. Пользователь '{dynamic_context['user_name']}' готов и ожидает твоего первого хода."
        if sexting_mode == 'narrative':
             llm_seed_message = f"Пользователь '{dynamic_context['user_name']}' хочет, чтобы ты начала рассказывать эротическую историю в режиме '{sexting_mode}'. Он может дать детали или попросить тебя удивить. Начни историю."
        
        try:
            await callback.message.edit_text(initial_prompt_text, reply_markup=None, parse_mode="Markdown")
            await self.bot_instance.bot.send_chat_action(callback.message.chat.id, "typing")
            ai_first_response = await self.llm_service.generate_response(
                user_message=llm_seed_message, persona=current_persona, context_messages=[],
                dynamic_context_info=dynamic_context, max_output_tokens=getattr(self.bot_instance.config, 'llm_sexting_initial_response_tokens', 250)
            )
            if ai_first_response:
                user_conditions_quick = await self._get_current_user_conditions(user_id_tg, current_persona)
                quick_actions_markup = navigation.create_quick_actions_menu(current_persona, user_conditions_quick)
                await callback.message.answer(ai_first_response, reply_markup=quick_actions_markup, parse_mode="Markdown")
            await callback.answer(f"Режим '{mode_display}' активирован!")
        except Exception as e:
            logger.error(f"Ошибка при старте секстинг-взаимодействия для user {user_id_tg}: {e}", exc_info=True)
            error_id = self.bot_instance.error_handler_instance.log_error(e, context={'user_id_tg': user_id_tg, 'sexting_mode': sexting_mode}, user_id=user_id_tg)
            await callback.message.answer(f"😔 Произошла ошибка при активации режима (Код: `{error_id}`). Пожалуйста, попробуйте позже.", parse_mode="Markdown")
            await callback.answer("Ошибка активации режима", show_alert=True)


    async def handle_start_quest_action(self, callback_or_message: Union[types.Message, types.CallbackQuery], state: FSMContext):
        # (Implementation as provided in the original file, ensure it uses self. for service access)
        target_event = callback_or_message
        user_id_tg = target_event.from_user.id # type: ignore
        current_persona = await self.bot_instance._get_current_persona(user_id_tg)

        if current_persona != "aeris": 
            msg_to_answer = target_event.message if isinstance(target_event, types.CallbackQuery) else target_event
            if msg_to_answer: await msg_to_answer.answer("Эта функция доступна только для персоны Aeris.")
            if isinstance(target_event, types.CallbackQuery): await target_event.answer()
            return

        text = f"🗺️ {hbold('Квесты Самопознания с Дианой')}\n\n"
        text += "Выбери тему для нашего небольшого совместного приключения. Каждый квест поможет тебе лучше понять себя и мир вокруг. "
        text += "Ты сможешь делать заметки в своем журнале (/new_entry) по ходу выполнения."

        buttons = []
        for key, theme_data in QUEST_THEMES.items():
            buttons.append([types.InlineKeyboardButton(text=theme_data["name"], callback_data=f"{QUEST_CHOOSE_THEME_CALLBACK_PREFIX}{key}")])
        buttons.append([types.InlineKeyboardButton(text="⬅️ Назад в Активности", callback_data="nav_activities")])
        
        # Use _show_menu_node to handle message editing or sending
        await self._show_menu_node(target_event, user_id_tg, "start_quest_node", current_persona, # Using a placeholder node_id
                                   title_override=text, 
                                   custom_reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons))
        if isinstance(target_event, types.CallbackQuery): await target_event.answer()


# Register the main dispatcher callback
# This should be the primary entry point for all callbacks handled by NavigationHandler
@enhanced_nav_router.callback_query(F.data.startswith("nav_") | F.data.startswith("action_"))
async def route_all_navigation_callbacks(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    # The instance of NavigationHandler is already available in bot_instance
    nav_handler_instance: NavigationHandler = bot_instance.navigation_handlers_instance
    await nav_handler_instance.main_callback_dispatcher(callback, state)
