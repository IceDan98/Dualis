# handlers/story_creation_fsm.py
import logging
from typing import Dict, Any, TYPE_CHECKING, Union, Optional, List # Added List
from datetime import datetime, timezone

from aiogram import Router, F, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command, StateFilter
from aiogram.utils.markdown import hbold

from utils.navigation import navigation # For "Back to Main Menu" button on cancel
from services.memory_service import MemoryType # For saving the generated story

if TYPE_CHECKING:
    from main import AICompanionBot
    from services.llm_service import LLMService
    # from services.memory_service import MemoryService # Already imported

logger = logging.getLogger(__name__)
story_fsm_router = Router()

class StoryCreationFSM(StatesGroup):
    """Defines FSM states for the story creation process."""
    waiting_for_genre = State()
    waiting_for_hero_description = State()
    waiting_for_setting_description = State()
    waiting_for_plot_problem = State()
    waiting_for_key_elements = State()
    waiting_for_story_tone = State()
    waiting_for_story_style = State() # For selecting narrative or conversational style
    confirm_story_details = State()
    story_generated_waiting_feedback = State()

# --- Keyboards ---
def get_fsm_cancel_keyboard(back_to_confirm_callback: Optional[str] = None) -> types.InlineKeyboardMarkup:
    """Creates a keyboard with a 'Cancel' button and optionally a 'Back to Confirmation' button."""
    buttons = [[types.InlineKeyboardButton(text="🚫 Отмена", callback_data="fsm_story_cancel")]]
    if back_to_confirm_callback: # Add "Back" button if callback is provided
        buttons.insert(0, [types.InlineKeyboardButton(text="⬅️ К подтверждению", callback_data=back_to_confirm_callback)])
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

def get_story_style_keyboard() -> types.InlineKeyboardMarkup:
    """Returns a keyboard for selecting the story's narrative style."""
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="📖 Подробное повествование", callback_data="fsm_story_style_narrative")],
        [types.InlineKeyboardButton(text="💬 Динамичный диалог/чат", callback_data="fsm_story_style_conversational")],
        [types.InlineKeyboardButton(text="✨ На усмотрение AI", callback_data="fsm_story_style_auto")],
        [types.InlineKeyboardButton(text="🚫 Отмена", callback_data="fsm_story_cancel")]
    ])

def get_story_confirmation_keyboard() -> types.InlineKeyboardMarkup:
    """Creates a keyboard for the story details confirmation step."""
    return types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="✅ Создать историю", callback_data="fsm_story_generate")],
        # Edit buttons for each parameter
        [types.InlineKeyboardButton(text="✏️ Жанр", callback_data="fsm_story_edit_genre"),
         types.InlineKeyboardButton(text="✏️ Герой", callback_data="fsm_story_edit_hero")],
        [types.InlineKeyboardButton(text="✏️ Сеттинг", callback_data="fsm_story_edit_setting"),
         types.InlineKeyboardButton(text="✏️ Проблема", callback_data="fsm_story_edit_problem")],
        [types.InlineKeyboardButton(text="✏️ Элементы", callback_data="fsm_story_edit_elements"),
         types.InlineKeyboardButton(text="✏️ Тон", callback_data="fsm_story_edit_tone")],
        [types.InlineKeyboardButton(text="✏️ Стиль", callback_data="fsm_story_edit_style")],
        [types.InlineKeyboardButton(text="🚫 Отмена", callback_data="fsm_story_cancel")]
    ])

def get_story_feedback_keyboard() -> types.InlineKeyboardMarkup:
    """Creates a keyboard for providing feedback after story generation."""
    buttons = [
        [types.InlineKeyboardButton(text="🎉 Отлично!", callback_data="fsm_story_feedback_good"),
         types.InlineKeyboardButton(text="🤔 Можно лучше", callback_data="fsm_story_feedback_improve")],
        [types.InlineKeyboardButton(text="💾 Сохранить эту историю", callback_data="fsm_story_save_generated")],
        [types.InlineKeyboardButton(text="🏡 В главное меню", callback_data="nav_main")]
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=buttons)

# --- FSM Cancel Handlers ---
@story_fsm_router.message(Command("cancel_story"), StateFilter(StoryCreationFSM))
@story_fsm_router.callback_query(F.data == "fsm_story_cancel", StateFilter(StoryCreationFSM))
async def cancel_story_creation_handler(event: Union[types.Message, types.CallbackQuery], state: FSMContext, bot_instance: 'AICompanionBot'):
    """Handles cancellation of the story creation FSM."""
    current_state_str = await state.get_state()
    user_id = event.from_user.id
    logger.info(f"User {user_id} cancelled story creation at step {current_state_str}")
    await state.clear()
    cancel_message_text = "Создание истории отменено."
    # Provide a button to go back to the main menu
    main_menu_button = types.InlineKeyboardMarkup(inline_keyboard=[
        [types.InlineKeyboardButton(text="🏡 В главное меню", callback_data="nav_main")]
    ])
    if isinstance(event, types.CallbackQuery) and event.message:
        try:
            await event.message.edit_text(cancel_message_text, reply_markup=main_menu_button)
        except Exception as e: # Fallback if edit fails
            logger.warning(f"Could not edit message on FSM cancel: {e}")
            await event.message.answer(cancel_message_text, reply_markup=main_menu_button)
        await event.answer()
    elif isinstance(event, types.Message):
        await event.answer(cancel_message_text, reply_markup=main_menu_button)

# --- Helper function to request next FSM step ---
async def request_next_step(
    target_message_event: Union[types.Message, types.CallbackQuery],
    state: FSMContext,
    next_fsm_state: State,
    prompt_text: str,
    bot_instance: 'AICompanionBot', # bot_instance is needed for sending messages
    reply_markup_override: Optional[types.InlineKeyboardMarkup] = None,
    edit_mode: bool = False # To show "Back to confirmation" button
):
    """Sets the next FSM state and sends/edits the prompt message to the user."""
    await state.set_state(next_fsm_state)
    final_reply_markup = reply_markup_override if reply_markup_override is not None \
                         else get_fsm_cancel_keyboard("fsm_story_back_to_confirm" if edit_mode else None)

    message_to_handle: Optional[types.Message] = None
    if isinstance(target_message_event, types.CallbackQuery):
        message_to_handle = target_message_event.message
    elif isinstance(target_message_event, types.Message):
        message_to_handle = target_message_event

    if not message_to_handle:
        logger.error("request_next_step: Could not determine message to reply/edit.")
        if isinstance(target_message_event, types.CallbackQuery):
            await target_message_event.answer("Ошибка отображения следующего шага.", show_alert=True)
        return

    try:
        # Edit if it's a callback query or if the target message is from the bot itself. Otherwise, send a new message.
        can_edit = isinstance(target_message_event, types.CallbackQuery) or \
                   (hasattr(message_to_handle, 'from_user') and message_to_handle.from_user and message_to_handle.from_user.is_bot)
        
        if can_edit:
            await message_to_handle.edit_text(prompt_text, reply_markup=final_reply_markup, parse_mode="Markdown")
        else:
            await message_to_handle.answer(prompt_text, reply_markup=final_reply_markup, parse_mode="Markdown")
    except Exception as e: # Fallback if edit/answer fails
        logger.warning(f"Error in request_next_step during edit/answer: {e}. Sending new message as fallback.")
        chat_id_to_send = message_to_handle.chat.id
        await bot_instance.bot.send_message(chat_id_to_send, prompt_text, reply_markup=final_reply_markup, parse_mode="Markdown")

    if isinstance(target_message_event, types.CallbackQuery):
        await target_message_event.answer()


# --- FSM Start ---
async def start_story_creation(target_message_event: Union[types.Message, types.CallbackQuery], state: FSMContext, bot_instance: 'AICompanionBot'):
    """Initiates the story creation FSM."""
    await state.clear() # Clear any previous FSM data for this user
    await request_next_step(
        target_message_event, state, StoryCreationFSM.waiting_for_genre,
        hbold("📚 Давайте создадим увлекательную историю вместе!") + "\n\n"
        "Сначала, пожалуйста, **опишите жанр** вашей будущей истории (например: фэнтези, научная фантастика, детектив, романтика, хоррор, юмор, эротика).",
        bot_instance=bot_instance
    )

# --- State Handlers ---

@story_fsm_router.message(StoryCreationFSM.waiting_for_genre, F.text)
async def process_genre_handler(message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Processes the genre input from the user."""
    if not message.text or not message.text.strip():
        await message.reply("Пожалуйста, введите текст для жанра или используйте кнопку 'Отмена'.")
        return
    genre = message.text.strip()
    await state.update_data(genre=genre)
    logger.info(f"User {message.from_user.id} (Story FSM): Genre - '{genre}'")
    await request_next_step(
        message, state, StoryCreationFSM.waiting_for_hero_description,
        f"👍 Жанр: {hbold(genre)}\n\nТеперь **опишите вашего главного героя**: его имя, характер, внешность, ключевые особенности или предысторию.",
        bot_instance=bot_instance
    )

# ... (process_hero_handler, process_setting_handler, process_problem_handler, process_elements_handler - similar structure, comments refined) ...
@story_fsm_router.message(StoryCreationFSM.waiting_for_hero_description, F.text)
async def process_hero_handler(message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not message.text or not message.text.strip():
        await message.reply("Пожалуйста, введите описание героя или используйте кнопку 'Отмена'.")
        return
    hero = message.text.strip()
    await state.update_data(hero=hero)
    logger.info(f"User {message.from_user.id} (Story FSM): Hero - '{hero[:50]}...'")
    await request_next_step(
        message, state, StoryCreationFSM.waiting_for_setting_description,
        "👤 Герой описан.\n\n**Где и когда будут происходить события?** Опишите мир, локацию, эпоху или атмосферу сеттинга.",
        bot_instance=bot_instance
    )

@story_fsm_router.message(StoryCreationFSM.waiting_for_setting_description, F.text)
async def process_setting_handler(message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not message.text or not message.text.strip():
        await message.reply("Пожалуйста, введите описание сеттинга или используйте кнопку 'Отмена'.")
        return
    setting = message.text.strip()
    await state.update_data(setting=setting)
    logger.info(f"User {message.from_user.id} (Story FSM): Setting - '{setting[:50]}...'")
    await request_next_step(
        message, state, StoryCreationFSM.waiting_for_plot_problem,
        "🌍 Мир создан.\n\n**Какая основная проблема, конфликт, цель или загадка** будет двигать сюжет вашей истории?",
        bot_instance=bot_instance
    )

@story_fsm_router.message(StoryCreationFSM.waiting_for_plot_problem, F.text)
async def process_problem_handler(message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not message.text or not message.text.strip():
        await message.reply("Пожалуйста, введите описание проблемы/конфликта или используйте кнопку 'Отмена'.")
        return
    problem = message.text.strip()
    await state.update_data(problem=problem)
    logger.info(f"User {message.from_user.id} (Story FSM): Problem - '{problem[:50]}...'")
    await request_next_step(
        message, state, StoryCreationFSM.waiting_for_key_elements,
        "🔥 Интрига заложена.\n\nПеречислите **несколько ключевых элементов, персонажей или событий**, которые обязательно должны присутствовать в истории (например: древний артефакт, верный спутник, неожиданное предательство, встреча с драконом).",
        bot_instance=bot_instance
    )

@story_fsm_router.message(StoryCreationFSM.waiting_for_key_elements, F.text)
async def process_elements_handler(message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
    if not message.text or not message.text.strip():
        await message.reply("Пожалуйста, введите ключевые элементы или используйте кнопку 'Отмена'.")
        return
    elements = message.text.strip()
    await state.update_data(elements=elements)
    logger.info(f"User {message.from_user.id} (Story FSM): Elements - '{elements[:50]}...'")
    await request_next_step(
        message, state, StoryCreationFSM.waiting_for_story_tone,
        "✨ Детали добавлены.\n\nЗадайте **тон или настроение** вашей истории (например: юмористический, мрачный, эпический, трогательный, напряженный, загадочный, эротический).",
        bot_instance=bot_instance
    )

@story_fsm_router.message(StoryCreationFSM.waiting_for_story_tone, F.text)
async def process_tone_handler(message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Processes the story tone input and moves to style selection."""
    if not message.text or not message.text.strip():
        await message.reply("Пожалуйста, введите желаемый тон истории или используйте кнопку 'Отмена'.")
        return
    tone = message.text.strip()
    await state.update_data(tone=tone)
    logger.info(f"User {message.from_user.id} (Story FSM): Tone - '{tone}'.")
    await request_next_step(
        message, state, StoryCreationFSM.waiting_for_story_style,
        f"🎨 Тон: {hbold(tone)}.\n\nТеперь выберите **стиль изложения истории**:",
        reply_markup_override=get_story_style_keyboard(), # Provide style selection keyboard
        bot_instance=bot_instance
    )

@story_fsm_router.callback_query(StateFilter(StoryCreationFSM.waiting_for_story_style), F.data.startswith("fsm_story_style_"))
async def process_style_callback_handler(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Processes the story style selection from callback query."""
    if not callback.message: return await callback.answer("Ошибка отображения.")
    
    style_choice = callback.data.split("fsm_story_style_")[-1]
    style_display_names = { # For user-facing display
        "narrative": "Подробное повествование", "conversational": "Динамичный диалог/чат", "auto": "На усмотрение AI"
    }
    selected_style_display = style_display_names.get(style_choice, style_choice.capitalize())

    await state.update_data(story_style=style_choice) # Store technical name
    logger.info(f"User {callback.from_user.id} (Story FSM): Style - '{style_choice}' ({selected_style_display}). Moving to confirmation.")
    await show_confirmation_details(callback, state, bot_instance)
    # callback.answer() is called within show_confirmation_details if it's a callback

# --- Confirmation and Editing Step ---
async def show_confirmation_details(
    message_or_callback_event: Union[types.Message, types.CallbackQuery],
    state: FSMContext,
    bot_instance: 'AICompanionBot'
):
    """Displays all collected story details for user confirmation."""
    story_data = await state.get_data()
    style_tech_name = story_data.get('story_style', 'auto')
    style_display_names = {
        "narrative": "Подробное повествование", "conversational": "Динамичный диалог/чат", "auto": "На усмотрение AI"
    }
    style_display = style_display_names.get(style_tech_name, style_tech_name.capitalize())

    confirmation_text = hbold("📝 Пожалуйста, проверьте детали вашей будущей истории:") + "\n\n"
    confirmation_text += f"**Жанр:** {story_data.get('genre', hbold('не указан'))}\n"
    confirmation_text += f"**Главный герой:** {story_data.get('hero', hbold('не описан'))[:200]}...\n" # Preview long text
    confirmation_text += f"**Сеттинг:** {story_data.get('setting', hbold('не описан'))[:200]}...\n"
    confirmation_text += f"**Проблема/Конфликт:** {story_data.get('problem', hbold('не указана'))[:200]}...\n"
    confirmation_text += f"**Ключевые элементы:** {story_data.get('elements', hbold('не указаны'))[:200]}...\n"
    confirmation_text += f"**Тон истории:** {story_data.get('tone', hbold('нейтральный'))}\n"
    confirmation_text += f"**Стиль изложения:** {hbold(style_display)}\n\n"
    confirmation_text += "Всё верно? Или хотите что-то изменить?"

    target_message = message_or_callback_event.message if isinstance(message_or_callback_event, types.CallbackQuery) else message_or_callback_event
    
    await state.set_state(StoryCreationFSM.confirm_story_details)
    try:
        await target_message.edit_text(confirmation_text, reply_markup=get_story_confirmation_keyboard(), parse_mode="Markdown")
    except Exception: # Fallback if edit fails
        await target_message.answer(confirmation_text, reply_markup=get_story_confirmation_keyboard(), parse_mode="Markdown")
    
    if isinstance(message_or_callback_event, types.CallbackQuery):
        await message_or_callback_event.answer()

@story_fsm_router.callback_query(F.data.startswith("fsm_story_edit_"), StateFilter(StoryCreationFSM.confirm_story_details))
async def edit_story_detail_handler(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Handles requests to edit a specific detail of the story."""
    if not callback.message: return await callback.answer("Ошибка")
    
    edit_action = callback.data.split("fsm_story_edit_")[-1]
    # Prompts and target states for editing each detail
    edit_prompts = {
        "genre": "✏️ Введите новый **жанр**:", "hero": "✏️ Опишите **главного героя** по-новому:",
        "setting": "✏️ Введите новое описание **сеттинга**:", "problem": "✏️ Сформулируйте **проблему/конфликт** иначе:",
        "elements": "✏️ Укажите новые **ключевые элементы**:", "tone": "✏️ Задайте новый **тон** для истории:",
        "style": "✏️ Выберите новый **стиль изложения**:"
    }
    target_states = {
        "genre": StoryCreationFSM.waiting_for_genre, "hero": StoryCreationFSM.waiting_for_hero_description,
        "setting": StoryCreationFSM.waiting_for_setting_description, "problem": StoryCreationFSM.waiting_for_plot_problem,
        "elements": StoryCreationFSM.waiting_for_key_elements, "tone": StoryCreationFSM.waiting_for_story_tone,
        "style": StoryCreationFSM.waiting_for_story_style
    }

    if edit_action in edit_prompts and edit_action in target_states:
        await state.update_data(_is_editing_from_confirm=True) # Flag to return to confirmation
        reply_markup_for_edit = get_story_style_keyboard() if edit_action == "style" else None
        await request_next_step(
            callback, state, target_states[edit_action],
            edit_prompts[edit_action], bot_instance=bot_instance,
            reply_markup_override=reply_markup_for_edit,
            edit_mode=True # To show "Back to Confirmation" button
        )
    else:
        await callback.answer("Неизвестное действие редактирования.", show_alert=True)

@story_fsm_router.callback_query(F.data == "fsm_story_back_to_confirm", StateFilter(StoryCreationFSM))
async def back_to_confirmation_handler(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Handles returning to the confirmation step after editing a detail."""
    if not callback.message: return await callback.answer("Ошибка")
    await state.update_data(_is_editing_from_confirm=False) # Reset edit flag
    await show_confirmation_details(callback, state, bot_instance)

# --- Story Generation ---
@story_fsm_router.callback_query(F.data == "fsm_story_generate", StateFilter(StoryCreationFSM.confirm_story_details))
async def generate_story_handler(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Generates the story based on collected details."""
    if not callback.message or not callback.from_user : return await callback.answer("Ошибка")
    
    user_id_tg = callback.from_user.id
    logger.info(f"User {user_id_tg} (Story FSM): Confirmed details. Starting story generation.")
    story_data = await state.get_data()
    
    try: # Attempt to edit message to show "generating" status
        await callback.message.edit_text(
            "⏳ Отлично! Ваша история уже пишется... Это может занять некоторое время. Пожалуйста, подождите. 🧙‍♂️✨",
            reply_markup=None # Remove buttons during generation
        )
    except Exception as e:
        logger.warning(f"Could not edit message before story generation: {e}")
        await callback.message.answer("⏳ Отлично! Ваша история уже пишется... Это может занять некоторое время. Пожалуйста, подождите. 🧙‍♂️✨")

    current_persona = await bot_instance._get_current_persona(user_id_tg)
    llm_service: 'LLMService' = bot_instance.llm_service

    story_style = story_data.get('story_style', 'auto')
    style_display_names = { # For prompt construction
        "narrative": "Подробное повествование", "conversational": "Динамичный диалог/чат", "auto": "На усмотрение AI"
    }
    dynamic_llm_context = {"user_name": callback.from_user.first_name or "читатель"}
    if story_style != 'auto': # Pass sexting_mode for narrative/conversational styles
        dynamic_llm_context["sexting_mode"] = story_style 
    
    # Construct the prompt for LLM
    prompt_parts = [
        f"Ты — {current_persona}, талантливый рассказчик. Напиши историю по параметрам от пользователя ({dynamic_llm_context.get('user_name', 'пользователь')}):",
        f"1. Жанр: {story_data.get('genre', 'не указан')}.",
        f"2. Герой: {story_data.get('hero', 'не описан')}. Раскрой его характер.",
        f"3. Сеттинг: {story_data.get('setting', 'не описано')}. Создай атмосферу.",
        f"4. Проблема/Цель: {story_data.get('problem', 'не указана')}. Это ядро сюжета.",
        f"5. Ключевые элементы: {story_data.get('elements', 'не указаны')}.",
        f"6. Тон: {story_data.get('tone', 'нейтральный')}.",
        f"7. Стиль изложения: {style_display_names.get(story_style, style_style.capitalize())} (Инструкция для LLM: {'narrative' if story_style == 'narrative' else 'conversational' if story_style == 'conversational' else 'любой подходящий'}).",
        "\n**Требования:** Логичность, последовательность, яркий язык, оригинальность (если возможно). Учти стиль персоны '{current_persona}' и инструкции из ее основного промпта. Объем: 500-1500 слов (несколько абзацев)."
    ]
    prompt_for_llm = "\n".join(prompt_parts)
    
    try:
        generated_story_text = await llm_service.generate_response(
            user_message=prompt_for_llm, persona=current_persona, context_messages=[],
            dynamic_context_info=dynamic_llm_context,
            max_output_tokens=bot_instance.config.llm_max_output_tokens, temperature=0.75 
        )
        
        await state.update_data(generated_story_text=generated_story_text, story_params=story_data) 
        await state.set_state(StoryCreationFSM.story_generated_waiting_feedback)

        if generated_story_text:
            # Handle long messages by splitting
            max_len = 4000 # Telegram message length limit (approx)
            story_header = hbold("📖 Ваша история готова:") + "\n\n"
            full_display_text = story_header + generated_story_text
            
            if len(full_display_text) > max_len :
                await callback.message.answer(hbold("📖 Ваша история готова! Она получилась довольно объемной, поэтому я разделю ее на части:"))
                parts: List[str] = []
                remaining_text = generated_story_text
                while len(remaining_text) > 0:
                    # Determine chunk size, considering header for the first part
                    current_header_len = len(story_header) if not parts else 0
                    chunk_max_len = max_len - current_header_len - 20 # -20 for "..." and safety margin
                    if len(remaining_text) <= chunk_max_len:
                        parts.append(remaining_text); break
                    
                    chunk = remaining_text[:chunk_max_len]
                    # Try to split at a good point (newline or space)
                    split_pos = max(chunk.rfind('\n'), chunk.rfind(' '))
                    if split_pos == -1 or len(chunk) - split_pos > 150: # If no good split point or it's too far back
                        split_pos = len(chunk) # Force split
                    
                    parts.append(chunk[:split_pos] + ("..." if len(remaining_text[split_pos:]) > 0 else ""))
                    remaining_text = remaining_text[split_pos:].lstrip()

                for i, part_text in enumerate(parts):
                    current_header_to_send = story_header if i == 0 else ""
                    is_last_part = (i == len(parts) - 1)
                    await callback.message.answer(
                        current_header_to_send + part_text, 
                        reply_markup=get_story_feedback_keyboard() if is_last_part else None,
                        parse_mode="Markdown"
                    )
                    if not is_last_part: await asyncio.sleep(0.5) # Small delay between parts
            else: # Story fits in one message
                await callback.message.answer(full_display_text, reply_markup=get_story_feedback_keyboard(), parse_mode="Markdown")
        else: # No story generated
            await callback.message.answer(
                "😔 К сожалению, не удалось создать историю по вашему запросу. Попробуйте изменить параметры.",
                reply_markup=get_story_feedback_keyboard()
            )
    except Exception as e:
        logger.error(f"Ошибка генерации истории для user {user_id_tg}: {e}", exc_info=True)
        error_id = bot_instance.error_handler_instance.log_error(e, context={'user_id_tg': user_id_tg, 'fsm_step': 'generation'})
        await callback.message.answer(f"😔 Произошла ошибка при создании истории (Код: `{error_id}`). Пожалуйста, попробуйте позже.", parse_mode="Markdown")
        await state.clear()
    await callback.answer()


# --- Feedback and Save Handlers ---
@story_fsm_router.callback_query(F.data.in_({"fsm_story_feedback_good", "fsm_story_feedback_improve"}), StateFilter(StoryCreationFSM.story_generated_waiting_feedback))
async def story_feedback_handler(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Handles user feedback on the generated story."""
    if not callback.message or not callback.from_user: return await callback.answer()
    
    feedback_type = callback.data.split('_')[-1] # "good" or "improve"
    user_id_tg = callback.from_user.id
    story_data_from_state = await state.get_data()
    story_params = story_data_from_state.get('story_params', {})

    logger.info(f"User {user_id_tg} provided story feedback: {feedback_type}. Params: {story_params.get('genre')}, Tone: {story_params.get('tone')}, Style: {story_params.get('story_style')}")

    db_user = await bot_instance.db_service.get_user_by_telegram_id(user_id_tg)
    if db_user: # Log statistic if user exists
        await bot_instance.db_service.save_statistic(
            metric_name=f"story_feedback", metric_value=1.0 if feedback_type == "good" else 0.0,
            user_id=db_user.id, 
            additional_data={
                'feedback_type': feedback_type, 'genre': story_params.get('genre'),
                'tone': story_params.get('tone'), 'style': story_params.get('story_style')
            }
        )
    response_text = "Спасибо за ваш отзыв! ❤️ Рад, что вам понравилось!" if feedback_type == "good" else "Спасибо! Я учту ваши пожелания для будущих творений. 🧐"
    try: # Attempt to edit the message containing the story or feedback buttons
        current_message_text = callback.message.text or "" 
        # Append feedback response, ensuring not to exceed message limits
        if len(current_message_text) + len(response_text) + 20 < 4096: # +20 for formatting and newline
             await callback.message.edit_text(f"{current_message_text.split('Как вам результат?')[0].strip()}\n\n*{response_text}*", reply_markup=get_story_feedback_keyboard(), parse_mode="Markdown")
        else: 
            await callback.message.reply(f"*{response_text}*", parse_mode="Markdown")
    except Exception as e: # Fallback if edit fails
        logger.warning(f"Could not edit message with feedback: {e}")
        await callback.message.reply(f"*{response_text}*", parse_mode="Markdown")
    await callback.answer()

@story_fsm_router.callback_query(F.data == "fsm_story_save_generated", StateFilter(StoryCreationFSM.story_generated_waiting_feedback))
async def save_generated_story_handler(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Saves the generated story to the user's memory."""
    if not callback.message or not callback.from_user: return await callback.answer("Ошибка")

    user_id_tg = callback.from_user.id
    story_data_from_state = await state.get_data()
    generated_story_text = story_data_from_state.get("generated_story_text")
    story_params = story_data_from_state.get("story_params", {})
    
    if not generated_story_text:
        await callback.answer("Не удалось найти текст истории для сохранения.", show_alert=True); return

    memory_service: 'MemoryService' = bot_instance.memory_service
    current_persona = await bot_instance._get_current_persona(user_id_tg)

    story_title = f"История: {story_params.get('genre', 'Рассказ')} ({datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')})"
    # Prepare content for saving, including parameters
    story_content_for_saving = f"**{story_title}**\n\n{generated_story_text}\n\n"
    story_content_for_saving += "*Параметры создания:*\n" + \
                                f"- Жанр: {story_params.get('genre', '-')}\n" + \
                                f"- Стиль: {story_params.get('story_style', 'auto').capitalize()}\n" + \
                                f"- Тон: {story_params.get('tone', '-')}"
    
    try:
        saved_item = await memory_service.save_memory(
            user_id_tg=user_id_tg, persona=current_persona, 
            content=story_content_for_saving,
            memory_content_type=MemoryType.GENERATED_STORY.value, # Specific type for generated stories
            tags=["сгенерированная_история", story_params.get('genre', 'unknown_genre').lower().replace(" ", "_")]
        )
        if saved_item:
            await callback.answer("История успешно сохранена в вашу память (раздел 'События' или 'Инсайты')!", show_alert=True)
            logger.info(f"User {user_id_tg} saved generated story (Memory ID {saved_item.id})")
            await state.clear() 
            
            main_menu_button = types.InlineKeyboardMarkup(inline_keyboard=[
                [types.InlineKeyboardButton(text="🏡 В главное меню", callback_data="nav_main")]
            ])
            try: # Edit the original story message to indicate it's saved
                 original_story_display_text = callback.message.text or (hbold("📖 Ваша история готова:") + "\n\n" + generated_story_text[:100]+"...")
                 final_text = f"{original_story_display_text.split('Как вам результат?')[0].strip()}\n\n*История сохранена в вашу память.*"
                 await callback.message.edit_text(final_text, reply_markup=main_menu_button, parse_mode="Markdown")
            except Exception as e_edit:
                 logger.warning(f"Could not edit message after saving story: {e_edit}")
                 await callback.message.answer("История сохранена.", reply_markup=main_menu_button) # Send new message as fallback
        else:
            await callback.answer("Не удалось сохранить историю. Попробуйте позже.", show_alert=True)
    except Exception as e:
        logger.error(f"Error saving generated story for user {user_id_tg}: {e}", exc_info=True)
        await callback.answer("Произошла ошибка при сохранении истории.", show_alert=True)

# --- Fallback Handler for incorrect input within FSM ---
@story_fsm_router.message(StateFilter(StoryCreationFSM)) # Catches any message in any StoryCreationFSM state
async def incorrect_input_in_story_fsm_handler(message: types.Message, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Handles unexpected text messages during the FSM progression."""
    data = await state.get_data()
    is_editing_from_confirm = data.get('_is_editing_from_confirm', False)
    current_fsm_state_name = await state.get_state()
    
    reply_markup_to_use = get_fsm_cancel_keyboard("fsm_story_back_to_confirm" if is_editing_from_confirm else None)
    prompt_message = "Пожалуйста, следуйте инструкциям или используйте кнопку 'Отмена', чтобы прервать создание истории."
    
    # If waiting for a button press (like style selection), remind the user
    if current_fsm_state_name == StoryCreationFSM.waiting_for_story_style.state:
        prompt_message = "Пожалуйста, выберите стиль изложения с помощью кнопок выше или нажмите 'Отмена'."
        reply_markup_to_use = get_story_style_keyboard()

    await message.reply(
        f"Хм, кажется, я ожидал немного другой информации на этом шаге. {prompt_message}",
        reply_markup=reply_markup_to_use
    )
