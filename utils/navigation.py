# utils/navigation.py
import logging
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from typing import Dict, List, Optional, Any, Union

logger = logging.getLogger(__name__)

class NavigationNode:
    """
    Узел навигационной системы.
    ... (остальной код класса NavigationNode без изменений) ...
    """
    def __init__(self,
                 id: str,
                 text: str,
                 handler: Optional[str] = None,
                 children: Optional[List['NavigationNode']] = None,
                 action: Optional[str] = None,
                 persona_specific: Optional[List[str]] = None,
                 condition: Optional[str] = None,
                 web_app_url: Optional[str] = None,
                 row: Optional[int] = None,
                 emoji: Optional[str] = "",
                 dynamic_text_key: Optional[str] = None,
                 parent_id_override: Optional[str] = None):
        self.id = id
        self.text_template = f"{emoji} {text}".strip() if emoji else text
        self.handler = handler
        self.children = children if children else []
        self.action = action
        self.persona_specific = persona_specific if persona_specific else []
        self.condition = condition
        self.web_app_url = web_app_url
        self.row = row
        self.dynamic_text_key = dynamic_text_key
        self.parent_id_override = parent_id_override
        self.parent_id: Optional[str] = None

    def add_child(self, node: 'NavigationNode'):
        self.children.append(node)

    def get_text(self, user_conditions: Optional[Dict[str, Any]] = None) -> str:
        text = self.text_template
        user_conditions = user_conditions or {}

        if self.dynamic_text_key:
            current_persona = user_conditions.get('current_persona')
            # Логика для current_persona
            if self.dynamic_text_key == 'current_persona' and self.action and self.action.startswith("switch_persona_"):
                persona_name_from_action = self.action.split("switch_persona_")[-1]
                if current_persona == persona_name_from_action:
                    text += " (текущая)"
            # Логика для current_vibe_aeris
            elif self.dynamic_text_key == 'current_vibe_aeris' and self.action and self.action.startswith("set_vibe_aeris_"):
                vibe_name_from_action = self.action.split("set_vibe_aeris_")[-1]
                if user_conditions.get('current_vibe_aeris') == vibe_name_from_action:
                    text += " ✓"
            # Логика для current_sexting_level_luneth
            elif self.dynamic_text_key == 'current_sexting_level_luneth' and self.action and self.action.startswith("set_sexting_level_"):
                try:
                    level_from_action = int(self.action.split("set_sexting_level_")[-1])
                    if user_conditions.get('current_sexting_level_luneth') == level_from_action:
                        text += " ✓"
                except ValueError:
                    logger.warning(f"Ошибка парсинга уровня из action '{self.action}' для dynamic_text_key")
        return text


class NavigationManager:
    """
    ... (остальной код класса NavigationManager без изменений) ...
    """
    def __init__(self, root_nodes: List[NavigationNode]):
        self.root_nodes_map: Dict[str, NavigationNode] = {node.id: node for node in root_nodes}
        self.node_map: Dict[str, NavigationNode] = {}
        self._build_node_map(root_nodes)
        logger.info(f"NavigationManager инициализирован. Загружено узлов: {len(self.node_map)}")

    def _build_node_map(self, nodes: List[NavigationNode], parent_id: Optional[str] = None):
        for node in nodes:
            if node.id in self.node_map:
                logger.warning(f"Обнаружен дублирующийся ID узла: {node.id}. Узел будет перезаписан в карте.")
            node.parent_id = parent_id
            self.node_map[node.id] = node
            if node.children:
                self._build_node_map(node.children, node.id)

    def get_node(self, node_id: str) -> Optional[NavigationNode]:
        return self.node_map.get(node_id)

    def create_markup(self,
                      current_node_id: str,
                      current_persona: Optional[str] = None,
                      user_conditions: Optional[Dict[str, Any]] = None,
                      pagination_buttons: Optional[List[List[InlineKeyboardButton]]] = None
                     ) -> InlineKeyboardMarkup:
        node = self.get_node(current_node_id)
        if not node:
            logger.warning(f"Узел навигации с ID '{current_node_id}' не найден. Попытка возврата к 'main'.")
            node = self.get_node("main") 
            if not node: 
                 logger.error("КРИТИЧЕСКАЯ ОШИБКА: Узел 'main' не определен в структуре навигации!")
                 return InlineKeyboardMarkup(inline_keyboard=[
                     [InlineKeyboardButton(text="Ошибка: Меню не найдено", callback_data="error_menu_critical")]
                 ])

        buttons_rows: List[List[InlineKeyboardButton]] = []
        user_conditions_checked = user_conditions or {}
        if current_persona:
            user_conditions_checked['current_persona'] = current_persona

        row_buckets: Dict[int, List[InlineKeyboardButton]] = {}
        default_row_start_index = 1000 # Для кнопок без указания row, чтобы они шли после явно указанных

        # Сначала обрабатываем дочерние узлы
        children_to_render = node.children
        
        # Если текущий узел сам является "листовым" и не имеет детей,
        # но имеет action или handler, он может сам быть кнопкой в меню родителя.
        # Однако, create_markup вызывается для УЗЛА, чьих ДЕТЕЙ мы рендерим.
        # Поэтому эта логика здесь избыточна.

        for child_node in children_to_render:
            if child_node.persona_specific and "all" not in child_node.persona_specific and current_persona not in child_node.persona_specific:
                continue
            if child_node.condition and not user_conditions_checked.get(child_node.condition, False):
                continue

            callback_data = f"nav_{child_node.id}" # По умолчанию навигация к дочернему узлу
            if child_node.action:
                callback_data = f"action_{child_node.action}"
            elif child_node.handler: # Если есть handler, он имеет приоритет над простой навигацией по ID
                # Проверяем, является ли handler ID другого узла или прямым callback_data
                if self.get_node(child_node.handler): # Если handler - это ID существующего узла
                    callback_data = f"nav_{child_node.handler}"
                else: # Иначе handler - это просто строка для callback_data
                    callback_data = child_node.handler # Может быть action_ или что-то еще
            
            button_text = child_node.get_text(user_conditions_checked)
            button_params: Dict[str, Any] = {"text": button_text}

            if child_node.web_app_url:
                button_params["web_app"] = WebAppInfo(url=child_node.web_app_url)
            else:
                button_params["callback_data"] = callback_data
            
            button = InlineKeyboardButton(**button_params)
            
            # Группировка по рядам
            row_index = child_node.row if child_node.row is not None else (default_row_start_index + len(buttons_rows) + len(row_buckets))
            if row_index not in row_buckets:
                row_buckets[row_index] = []
            row_buckets[row_index].append(button)

        # Собираем ряды из бакетов в отсортированном порядке
        for row_idx in sorted(row_buckets.keys()):
            buttons_rows.append(row_buckets[row_idx])
            
        # Добавляем кнопку "Назад" или "В главное меню"
        # parent_id_to_use учитывает parent_id_override
        parent_id_to_use = node.parent_id_override if node.parent_id_override is not None else node.parent_id
        
        if parent_id_to_use: # Если есть родитель (переопределенный или обычный)
            if self.get_node(parent_id_to_use): # Убедимся, что такой узел существует
                 buttons_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"nav_{parent_id_to_use}")])
            else:
                 logger.warning(f"Родительский узел '{parent_id_to_use}' для кнопки 'Назад' не найден (для узла '{current_node_id}'). Будет добавлена кнопка 'В главное меню'.")
                 if current_node_id != "main": # Не добавляем "В главное меню" если мы уже в нем
                     buttons_rows.append([InlineKeyboardButton(text="🏡 В главное меню", callback_data="nav_main")])
        elif current_node_id != "main": # Если родителя нет, но мы не в главном меню
             buttons_rows.append([InlineKeyboardButton(text="🏡 В главное меню", callback_data="nav_main")])
        
        # Добавляем кнопки пагинации, если они переданы
        if pagination_buttons:
            buttons_rows.extend(pagination_buttons)
            
        return InlineKeyboardMarkup(inline_keyboard=buttons_rows)

    def create_quick_actions_menu(self, current_persona: str, user_conditions: Optional[Dict[str, Any]] = None) -> InlineKeyboardMarkup:
        # ... (код метода без изменений) ...
        user_conditions_checked = user_conditions or {}
        buttons_rows: List[List[InlineKeyboardButton]] = []

        if current_persona == 'aeris':
            vibes_aeris_quick = {'friend': '😊', 'romantic': '🥰', 'passionate': '🔥', 'philosophical': '🤔'}
            current_vibe_aeris = user_conditions_checked.get('current_vibe_aeris')
            row1_aeris = [
                InlineKeyboardButton(
                    text=f"{emoji}{' ✓' if vibe_key == current_vibe_aeris else ''}", 
                    callback_data=f"action_set_vibe_aeris_{vibe_key}"
                ) for vibe_key, emoji in vibes_aeris_quick.items()
            ]
            buttons_rows.append(row1_aeris)
            buttons_rows.append([
                InlineKeyboardButton(text="📚 История", callback_data="action_create_story"),
                InlineKeyboardButton(text="💫 Фантазия", callback_data="action_romantic_fantasy"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="nav_stats") # Изменен на nav_stats
            ])
        elif current_persona == 'luneth':
            levels_luneth_quick = [1, 3, 5, 7, 10] # Добавлен уровень 1 для начала
            current_level_luneth = user_conditions_checked.get('current_sexting_level_luneth')
            row1_luneth = [
                InlineKeyboardButton(
                    text=f"🔥{level}{' ✓' if level == current_level_luneth else ''}", 
                    callback_data=f"action_set_sexting_level_{level}"
                ) for level in levels_luneth_quick
            ]
            buttons_rows.append(row1_luneth)
            buttons_rows.append([
                InlineKeyboardButton(text="💋 Хочу тебя!", callback_data="action_i_want_you"),
                InlineKeyboardButton(text="🎭 Ролевые игры", callback_data="nav_roleplay_madina"),
                InlineKeyboardButton(text="😌 Остыть", callback_data="action_stop_sexting")
            ])
        
        return InlineKeyboardMarkup(inline_keyboard=buttons_rows)


# --- Утилита для пагинации ---
def create_pagination_buttons(current_page: int,
                             total_pages: int,
                             callback_prefix: str,
                             items_per_row: int = 5) -> List[List[InlineKeyboardButton]]:
    # ... (код функции без изменений) ...
    if total_pages <= 1:
        return []

    rows: List[List[InlineKeyboardButton]] = []
    page_buttons_flat: List[InlineKeyboardButton] = []
    
    pages_to_render = set()
    if total_pages <= items_per_row + 2: 
        for i in range(1, total_pages + 1):
            pages_to_render.add(i)
    else:
        pages_to_render.add(1)
        pages_to_render.add(total_pages)
        num_around_current = (items_per_row - 3) // 2 
        if num_around_current < 0 : num_around_current = 0

        for i in range(max(1, current_page - num_around_current), min(total_pages, current_page + num_around_current) + 1):
            pages_to_render.add(i)
        
        if current_page - num_around_current > 2: pages_to_render.add(2)
        if current_page + num_around_current < total_pages - 1: pages_to_render.add(total_pages - 1)
        
    sorted_page_numbers = sorted(list(pages_to_render))
    
    last_rendered_page = 0
    for page_num in sorted_page_numbers:
        if page_num > last_rendered_page + 1 and last_rendered_page != 0:
            if len(page_buttons_flat) < items_per_row: 
                page_buttons_flat.append(InlineKeyboardButton(text="…", callback_data=f"{callback_prefix}_noop"))
        
        text = f"• {page_num} •" if page_num == current_page else str(page_num)
        page_buttons_flat.append(InlineKeyboardButton(text=text, callback_data=f"{callback_prefix}_page_{page_num}"))
        last_rendered_page = page_num

    # Разделяем page_buttons_flat на ряды по items_per_row
    for i in range(0, len(page_buttons_flat), items_per_row):
        rows.append(page_buttons_flat[i:i + items_per_row])

    nav_row = []
    if current_page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ Пред.", callback_data=f"{callback_prefix}_page_{current_page - 1}"))
    if current_page < total_pages:
        nav_row.append(InlineKeyboardButton(text="След. ➡️", callback_data=f"{callback_prefix}_page_{current_page + 1}"))
    
    if nav_row:
        rows.append(nav_row)
        
    return rows

# --- Определение структуры навигации (узлы) ---
# Главный узел
main_node = NavigationNode(
    id="main", text="Главное меню", emoji="🎮",
    children=[
        NavigationNode(id="personas_nav", text="Переключить персону", emoji="🎭", handler="nav_personas", row=0),
        NavigationNode(id="current_persona_settings_nav", text="Настройки текущей персоны", emoji="⚙️", handler="nav_current_persona_settings", row=1),
        NavigationNode(id="activities_nav", text="Творчество и Активности", emoji="🎨", handler="nav_activities", row=2),
        NavigationNode(id="memory_journal_main_nav", text="Память и Журнал", emoji="🧠", handler="nav_memory_journal_main", row=3),
        NavigationNode(id="stats_nav", text="Статистика", emoji="📊", handler="nav_stats", row=4), # Ссылка на узел статистики
        NavigationNode(id="profile_premium_main_nav", text="Профиль и Подписки", emoji="👤", handler="nav_profile_premium_main", row=5),
        NavigationNode(id="admin_panel_entry_nav", text="Админ-панель", emoji="🛠️", condition="is_admin", handler="nav_admin_main", row=6),
        NavigationNode(id="help_feedback_main_nav", text="Помощь и Связь", emoji="❓", handler="nav_help_feedback_main", row=7),
        NavigationNode(id="close_menu_action", text="Закрыть меню", emoji="❌", action="close_menu", row=8)
    ]
)

# Узел выбора персон
personas_node = NavigationNode(
    id="personas", text="Выбор AI-компаньона", emoji="🎭",
    children=[
        NavigationNode(id="switch_to_diana", text="Diana", emoji="🌟", action="switch_persona_diana", dynamic_text_key="current_persona", row=0),
        NavigationNode(id="switch_to_madina", text="Madina", emoji="😈", action="switch_persona_madina", dynamic_text_key="current_persona", row=0, condition="can_access_madina"),
    ]
)

# Узел-заглушка для динамического выбора настроек персоны
current_persona_settings_node = NavigationNode(id="current_persona_settings", text="Настройки персоны")

# Настройки Diana
diana_vibes_nodes_children = [
    NavigationNode(id=f"vibe_diana_{key}", text=name, action=f"set_vibe_diana_{key}", dynamic_text_key="current_vibe_diana", row=i//2)
    for i, (key, name) in enumerate(
        {'friend': '😊 Дружеский', 'romantic': '🥰 Романтичный', 'passionate': '🔥 Страстный', 'philosophical': '🤔 Философский'}.items()
    )
]
settings_diana_node = NavigationNode(
    id="settings_diana", text="Настройки Diana", emoji="⚙️", persona_specific=['diana'],
    children=diana_vibes_nodes_children + [
        NavigationNode(id="diana_romantic_fantasy_act", text="Романтическая фантазия", emoji="💫", action="romantic_fantasy", row=2), # Обновлен row
        NavigationNode(id="diana_mood_check_act", text="Проверка настроения", emoji="🎯", action="mood_check", row=2), # Обновлен row
        # NavigationNode(id="diana_ispark_act", text="Искра мысли", emoji="💡", action="ispark_diana", row=3), # Пример новой команды
        # NavigationNode(id="diana_selfpath_act", text="Путь к себе", emoji="🧭", action="selfpath_diana", row=3), # Пример новой команды
    ]
)

# Настройки Madina
madina_sexting_level_nodes_children = [
    NavigationNode(
        id=f"sexting_level_madina_{i}", 
        text=f"{'🔥' * min(i // 2 + 1, 5) if i > 0 else '❄️'} Уровень {i}", 
        action=f"set_sexting_level_{i}", 
        dynamic_text_key="current_sexting_level_madina",
        row=(i-1)//2 
    ) for i in range(1, 11) 
]
settings_madina_node = NavigationNode(
    id="settings_madina", text="Настройки Madina", emoji="⚙️", persona_specific=['madina'],
    children=madina_sexting_level_nodes_children + [
        NavigationNode(id="madina_i_want_you_act", text="Я хочу тебя", emoji="💋", action="i_want_you", row=5), # Обновлен row
        NavigationNode(id="madina_stop_sexting_act", text="Остыть", emoji="😌", action="stop_sexting", row=5), # Обновлен row
        NavigationNode(id="madina_roleplay_nav_link", text="Ролевые игры", emoji="🎭", handler="nav_roleplay_madina", row=6) # Обновлен row
    ]
)
roleplay_madina_node = NavigationNode(
    id="roleplay_madina", text="Ролевые игры с Madina", emoji="🎭",
    children=[
        NavigationNode(id="rp_madina_submissive_act", text="Покорная Мадина", action="start_roleplay_madina_sub", row=0),
        NavigationNode(id="rp_madina_dominant_act", text="Властная Мадина", action="start_roleplay_madina_dom", row=0),
        NavigationNode(id="rp_madina_custom_act", text="Свой сценарий...", action="start_roleplay_madina_custom", row=1),
    ]
)

# Творчество и Активности
activities_node = NavigationNode(
    id="activities", text="Творчество и Активности", emoji="🎨",
    children=[
        NavigationNode(id="act_create_story_link", text="Создать историю", emoji="📚", action="action_create_story_fsm", row=0), # Изменен action для запуска FSM
        # Добавляем новый узел для /start_quest
        NavigationNode(id="act_start_quest_link", text="Начать квест самопознания", emoji="🗺️", action="action_start_quest", persona_specific=['diana','all'], row=0), # Указываем action
        NavigationNode(id="act_write_poem", text="Написать стих", emoji="🎵", action="write_poem", row=1),
        NavigationNode(id="act_describe_scene", text="Описать сцену", emoji="🖼️", action="describe_scene", row=1),
        NavigationNode(id="act_improvisation", text="Импровизация", emoji="🎭", action="improvisation", row=2),
        NavigationNode(id="act_daydream", text="Сон наяву", emoji="🌟", action="daydream", row=2),
        NavigationNode(id="act_future_letter", text="Письмо в будущее", emoji="📝", action="future_letter", row=3),
        NavigationNode(id="act_romantic_fantasy_link", text="Романтическая фантазия", emoji="💫", action="romantic_fantasy", persona_specific=['diana', 'all'], row=4, condition="can_create_fantasy"),
        NavigationNode(id="act_mood_check_link", text="Проверка настроения", emoji="🎯", action="mood_check", persona_specific=['diana', 'all'], row=4),
    ]
)

# Память и Журнал
memory_journal_main_node = NavigationNode(
    id="memory_journal_main", text="Память и Журнал", emoji="🧠",
    # ... (остальные дети без изменений) ...
    children=[
        NavigationNode(id="mem_save_insight_act", text="Сохранить мысль", emoji="💡", action="save_insight", row=0),
        NavigationNode(id="mem_my_insights_act", text="Мои инсайты", emoji="🔍", action="my_insights", row=0),
        NavigationNode(id="mem_new_journal_entry_act", text="Новая запись в журнал", emoji="✍️", action="new_journal_entry", row=1),
        NavigationNode(id="mem_my_journal_act", text="Мой журнал", emoji="📔", action="my_journal", row=1),
        NavigationNode(id="mem_our_memories_act", text="Наши воспоминания", emoji="🧠", action="our_memories", row=2),
        NavigationNode(id="mem_daily_reflection_act", text="Размышление дня", emoji="🎯", action="daily_reflection", row=2),
        NavigationNode(id="mem_overview_nav", text="Обзор памяти", emoji="🗂️", handler="nav_memory_overview", row=3),
        NavigationNode(id="mem_ai_insights_nav", text="AI-Инсайты", emoji="💡", handler="nav_ai_insights", condition="is_premium_or_higher", row=3)
    ]
)
memory_overview_node = NavigationNode(id="memory_overview", text="Обзор памяти", emoji="🗂️")
ai_insights_node = NavigationNode(id="ai_insights", text="AI-Инсайты", emoji="💡", condition="is_premium_or_higher")

# Статистика
stats_node = NavigationNode(
    id="stats", text="Статистика", emoji="📊",
    # ... (остальные дети без изменений) ...
    children=[
        NavigationNode(id="stat_general_act", text="Общая статистика", emoji="📊", action="general_stats", row=0),
        NavigationNode(id="stat_diana_act", text="Статистика Diana", emoji="🌟", action="diana_stats", persona_specific=['diana', 'all'], row=1),
        NavigationNode(id="stat_madina_act", text="Статистика Madina", emoji="😈", action="madina_stats", persona_specific=['madina', 'all'], row=1),
        NavigationNode(id="stat_conversation_analysis_act", text="Анализ разговоров", emoji="💬", action="conversation_analysis", row=2, condition="is_premium_or_higher"),
        NavigationNode(id="stat_emotion_map_act", text="Эмоциональная карта", emoji="🧠", action="emotion_map", row=2, condition="is_premium_or_higher"),
    ]
)

# Профиль и Подписки
profile_premium_main_node = NavigationNode(
    id="profile_premium_main", text="Профиль и Подписки", emoji="👤",
    children=[
        NavigationNode(id="profile_view_nav", text="Мой профиль", emoji="🧑", handler="nav_user_profile_view", row=0),
        NavigationNode(id="profile_my_subscription_nav", text="Моя подписка", emoji="💎", handler="nav_my_subscription_view", row=1), # Изменен handler, чтобы вести на просмотр текущей подписки
        NavigationNode(id="profile_subscription_plans_nav", text="Тарифы Premium", emoji="⭐", handler="nav_subscription_plans_view", row=1),
        NavigationNode(id="profile_referral_nav", text="Пригласить друзей", emoji="🎁", handler="nav_referral_dashboard", row=2),
        NavigationNode(id="profile_enter_promocode_act", text="Ввести промокод", emoji="🎟️", action="action_enter_promocode_start", row=2),
    ]
)
user_profile_view_node = NavigationNode(id="user_profile_view", text="Мой профиль", emoji="🧑")
# Узел "Тарифы Premium" теперь будет показывать разные планы
subscription_plans_view_node = NavigationNode(
    id="subscription_plans_view", text="Тарифы Premium", emoji="⭐",
    children=[
        NavigationNode(id="sub_buy_basic_monthly", text="💎 Basic (ежемесячно)", action="subscribe_basic_monthly", row=0),
        NavigationNode(id="sub_buy_basic_yearly", text="💎 Basic (ежегодно) - Скидка!", action="subscribe_basic_yearly", row=0),
        NavigationNode(id="sub_buy_premium_monthly", text="🔥 Premium (ежемесячно)", action="subscribe_premium_monthly", row=1),
        NavigationNode(id="sub_buy_premium_yearly", text="🔥 Premium (ежегодно) - Скидка!", action="subscribe_premium_yearly", row=1),
        NavigationNode(id="sub_buy_vip_monthly", text="👑 VIP (ежемесячно)", action="subscribe_vip_monthly", row=2),
        NavigationNode(id="sub_buy_vip_yearly", text="👑 VIP (ежегодно) - Скидка!", action="subscribe_vip_yearly", row=2),
        NavigationNode(id="sub_compare_plans", text="📊 Сравнить все тарифы", action="action_compare_plans", row=3),
    ]
)
my_subscription_view_node = NavigationNode(id="my_subscription_view", text="Моя подписка", emoji="💎") # Этот узел будет отображать инфо о текущей подписке
referral_dashboard_node = NavigationNode(id="referral_dashboard", text="Реферальная программа", emoji="🎁")

# Помощь и Связь
help_feedback_main_node = NavigationNode(
    id="help_feedback_main", text="Помощь и Связь", emoji="❓",
    # ... (остальные дети без изменений) ...
    children=[
        NavigationNode(id="help_info_act", text="Справка по боту", emoji="📜", action="show_help_info", row=0),
        NavigationNode(id="help_leave_feedback_act", text="Оставить отзыв", emoji="💬", action="leave_feedback_start", row=0), # Предполагает FSM для отзыва
    ]
)

# --- Админ-панель ---
admin_main_node = NavigationNode(
    id="admin_main", text="Административная панель", emoji="🛠️", condition="is_admin",
    # ... (остальные дети без изменений) ...
    children=[
        NavigationNode(id="admin_nav_analytics_link", text="📈 Аналитика", handler="nav_admin_analytics_menu", row=0),
        NavigationNode(id="admin_nav_users_link", text="👥 Пользователи", handler="nav_admin_users_menu", row=0),
        NavigationNode(id="admin_nav_promocodes_link", text="🎟️ Промокоды", handler="nav_admin_promocodes_menu", row=1),
        NavigationNode(id="admin_nav_maintenance_link", text="🧹 Обслуживание", handler="nav_admin_maintenance_menu", row=1),
        NavigationNode(id="admin_act_reload_prompts", text="🔄 Перезагрузить промпты", action="admin_reload_all_prompts", row=2),
        NavigationNode(id="admin_act_sys_logs", text="📋 Системные логи", action="admin_view_system_logs", row=3),
        NavigationNode(id="admin_act_err_report", text="🚨 Отчет об ошибках", action="admin_view_error_report", row=3),
        NavigationNode(id="admin_act_adv_settings", text="⚙️ Расширенные настройки", action="admin_view_advanced_settings", row=4)
    ],
    parent_id_override="main"
)
admin_analytics_menu_node = NavigationNode(id="admin_analytics_menu", text="Аналитика (Админ)", emoji="📈", children=[
    NavigationNode(id="admin_act_view_stats_7d", text="Статистика за 7 дней", action="admin_view_stats_7", row=0),
    NavigationNode(id="admin_act_view_stats_30d", text="Статистика за 30 дней", action="admin_view_stats_30", row=0),
    NavigationNode(id="admin_act_export_csv_30d", text="Экспорт CSV (30д)", action="admin_export_analytics_csv_30", row=1),
], parent_id_override="admin_main")
admin_users_menu_node = NavigationNode(id="admin_users_menu", text="Управление Пользователями (Админ)", emoji="👥", children=[
    NavigationNode(id="admin_act_find_user", text="Найти по ID", action="admin_find_user_start", row=0),
    NavigationNode(id="admin_act_export_users", text="Экспорт пользователей", action="admin_export_users_data", row=1),
], parent_id_override="admin_main")
admin_promocodes_menu_node = NavigationNode(id="admin_promocodes_menu", text="Управление Промокодами (Админ)", emoji="🎟️", children=[
    NavigationNode(id="admin_act_promo_create", text="Создать промокод", action="admin_promocode_create_start", row=0),
    NavigationNode(id="admin_act_promo_list", text="Список промокодов", action="admin_promocode_list_view", row=0),
], parent_id_override="admin_main")
admin_maintenance_menu_node = NavigationNode(id="admin_maintenance_menu", text="Обслуживание (Админ)", emoji="🧹", children=[
    NavigationNode(id="admin_act_cleanup_logs", text="Очистить старые логи", action="admin_cleanup_old_logs", row=0),
    NavigationNode(id="admin_act_clear_cache", text="Очистить кэш системы", action="admin_clear_system_cache", row=0),
], parent_id_override="admin_main")

# Собираем все узлы для NavigationManager
all_navigation_nodes = [
    main_node, personas_node, current_persona_settings_node, settings_diana_node, settings_madina_node, roleplay_madina_node,
    activities_node, # activities_node уже включает act_start_quest_link
    memory_journal_main_node, memory_overview_node, ai_insights_node, stats_node,
    profile_premium_main_node, user_profile_view_node, subscription_plans_view_node, my_subscription_view_node, referral_dashboard_node,
    help_feedback_main_node,
    admin_main_node, admin_analytics_menu_node, admin_users_menu_node, admin_promocodes_menu_node, admin_maintenance_menu_node
]

# Создаем экземпляр NavigationManager
navigation = NavigationManager(all_navigation_nodes)
