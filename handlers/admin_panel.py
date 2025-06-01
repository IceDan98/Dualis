# handlers/admin_panel.py
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
import io # Used for CSV export
import csv # Used for CSV export

from aiogram import Router, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup # Keep for potential future FSM use in admin
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, BufferedInputFile # Added BufferedInputFile

from services.subscription_system import SubscriptionService
from database.operations import DatabaseService
from utils.error_handler import handle_errors
from config.settings import BotConfig
from config.prompts import prompt_manager

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import AICompanionBot

logger = logging.getLogger(__name__)
admin_router = Router()

class AdminStates(StatesGroup):
    """Defines FSM states for complex admin actions, if needed in the future."""
    # Example:
    # waiting_for_broadcast_message_text = State()
    # waiting_for_promocode_details = State()
    pass

class AdminPanel:
    """
    Provides functionality for the bot's administrative panel.
    Allows admins to view statistics, manage users, settings, etc.
    """
    
    def __init__(self, db_service: DatabaseService, 
                 subscription_service: SubscriptionService,
                 config: BotConfig,
                 bot_instance: Optional['AICompanionBot'] = None):
        self.db_service = db_service
        self.subscription_service = subscription_service
        self.config = config
        self.bot_instance = bot_instance # For accessing shared components like prompt_manager or LLMService if needed

    async def is_admin(self, user_id: int) -> bool:
        """Checks if the given user ID belongs to an administrator."""
        return user_id in self.config.admin_user_ids

    async def create_main_admin_menu(self) -> Dict[str, Any]:
        """Creates the main menu for the admin panel with key statistics."""
        # Fetching dashboard data
        dashboard_data = await self.db_service.get_admin_dashboard_data()
        subscription_analytics = await self.db_service.get_subscription_analytics()
        # Assuming revenue stats are for the last 30 days by default for the main dashboard
        revenue_stats_30d = await self.db_service.get_revenue_stats(days_back=30) 
        
        text = "🛠️ **Административная панель AI Companion**\n\n"
        text += "📊 **Обзор системы:**\n"
        text += f"  👥 Всего пользователей: {dashboard_data.get('total_users', 'N/A')}\n"
        text += f"   активных за 24ч: {dashboard_data.get('active_users_24h', 'N/A')}\n"
        text += f"  💬 Всего сообщений: {dashboard_data.get('total_messages', 'N/A')}\n"
        text += f"   сообщений за 24ч: {dashboard_data.get('messages_24h', 'N/A')}\n\n"
        
        text += "💰 **Монетизация (за последние 30 дней):**\n"
        text += f"  💵 Общий доход: {revenue_stats_30d.get('total_revenue', 0.0):.2f} ⭐\n"
        text += f"  🛒 Продано подписок: {revenue_stats_30d.get('total_sales', 0)}\n"
        text += f"  💳 Средний чек: {revenue_stats_30d.get('avg_revenue_per_sale', 0.0):.2f} ⭐\n"
        text += f"  📊 Конверсия в платный: {subscription_analytics.get('conversion_rate', 0.0):.1f}%\n\n"
        
        text += "🎯 **Распределение по тарифам:**\n"
        tier_dist = subscription_analytics.get('tier_distribution', {})
        text += f"  🆓 Free: {tier_dist.get('free', 0)}\n"
        text += f"  💎 Basic: {tier_dist.get('basic', 0)}\n"
        text += f"  🔥 Premium: {tier_dist.get('premium', 0)}\n"
        text += f"  👑 VIP: {tier_dist.get('vip', 0)}\n"
        
        buttons = [
            [InlineKeyboardButton(text="📈 Аналитика", callback_data="admin_analytics_main"),
             InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users_main")],
            [InlineKeyboardButton(text="💰 Финансы", callback_data="admin_finance_main"), # Placeholder
             InlineKeyboardButton(text="📡 Рассылки", callback_data="admin_broadcast_main")], # Placeholder
            [InlineKeyboardButton(text="🎟️ Промокоды", callback_data="admin_promocodes_main"), # Placeholder
             InlineKeyboardButton(text="⚙️ Настройки Бота", callback_data="admin_settings_main")], # Placeholder for bot settings
            [InlineKeyboardButton(text="🧹 Обслуживание", callback_data="admin_maintenance_main"),
             InlineKeyboardButton(text="📥 Экспорт данных", callback_data="admin_export_main")], # Placeholder
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close_panel")]
        ]
        return {"text": text, "reply_markup": InlineKeyboardMarkup(inline_keyboard=buttons)}

    async def create_analytics_submenu(self, period_days: int = 7) -> Dict[str, Any]:
        """Creates the detailed analytics submenu for a specified period."""
        engagement_stats = await self.db_service.get_user_engagement_stats(period_days)
        revenue_stats = await self.db_service.get_revenue_stats(period_days)
        
        text = f"📈 **Аналитический отчет за {period_days} дней**\n\n"
        text += "👥 **Вовлеченность пользователей:**\n"
        text += f"  • Средний DAU: {engagement_stats.get('avg_daily_active_users', 0.0):.1f}\n"
        text += f"  • Всего сообщений: {engagement_stats.get('total_messages', 0)}\n"
        text += f"  • Сообщений на активного пользователя: {engagement_stats.get('avg_messages_per_active_user', 0.0):.1f}\n\n"
        
        text += "💰 **Финансовые показатели:**\n"
        text += f"  • Общий доход: {revenue_stats.get('total_revenue', 0.0):.2f} ⭐\n"
        text += f"  • Количество продаж: {revenue_stats.get('total_sales', 0)}\n"
        text += f"  • Средний чек: {revenue_stats.get('avg_revenue_per_sale', 0.0):.2f} ⭐\n\n"
        
        # Placeholder for DAU trend graph or data
        # text += "📊 **Тренды DAU:** (график в разработке)\n"
            
        buttons = [
            [InlineKeyboardButton(text="7 дней", callback_data="admin_analytics_period_7"),
             InlineKeyboardButton(text="30 дней", callback_data="admin_analytics_period_30"),
             InlineKeyboardButton(text="90 дней", callback_data="admin_analytics_period_90")],
            [InlineKeyboardButton(text="📥 Экспорт CSV (30 дней)", callback_data="admin_export_analytics_csv_30")],
            [InlineKeyboardButton(text="⬅️ Назад в Админ-меню", callback_data="admin_main")]
        ]
        return {"text": text, "reply_markup": InlineKeyboardMarkup(inline_keyboard=buttons)}

    async def export_analytics_to_csv(self, period_days: int = 30) -> Optional[BufferedInputFile]:
        """Exports key analytical data to a CSV file."""
        try:
            # Fetching raw data for the period
            dau_data = await self.db_service.get_analytics_data('daily_active_users', period_days)
            messages_data = await self.db_service.get_analytics_data('messages_processed_daily', period_days) # Assuming daily sum
            revenue_data = await self.db_service.get_analytics_data('subscription_revenue_daily', period_days) # Assuming daily sum

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['Date', 'Daily_Active_Users', 'Messages_Count', 'Revenue_Stars'])

            # Aggregate data by date
            aggregated_data: Dict[str, Dict[str, Any]] = {}
            all_dates = set()

            for item_list, key_name in [(dau_data, 'dau'), (messages_data, 'messages'), (revenue_data, 'revenue')]:
                for item in item_list:
                    date_str = item['date'].strftime('%Y-%m-%d')
                    all_dates.add(date_str)
                    aggregated_data.setdefault(date_str, {'dau': 0, 'messages': 0, 'revenue': 0})
                    aggregated_data[date_str][key_name] += item['value'] # Sum if multiple entries for a day (e.g. hourly messages)

            for date_key in sorted(list(all_dates)): # Iterate over all unique dates found
                data_row = aggregated_data.get(date_key, {'dau': 0, 'messages': 0, 'revenue': 0}) # Use get for safety
                writer.writerow([date_key, data_row['dau'], data_row['messages'], data_row['revenue']])
            
            csv_content = output.getvalue().encode('utf-8')
            output.close()
            filename = f"ai_companion_analytics_{period_days}d_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
            return BufferedInputFile(csv_content, filename=filename)
        except Exception as e:
            logger.error(f"Ошибка экспорта аналитики в CSV: {e}", exc_info=True)
            return None
    
    async def create_user_management_submenu(self) -> Dict[str, Any]:
        """Creates the user management submenu."""
        text = "👥 **Управление пользователями**\n\nВыберите действие:"
        buttons = [
            [InlineKeyboardButton(text="🔍 Найти пользователя по ID", callback_data="admin_find_user_by_id")], # Placeholder
            [InlineKeyboardButton(text="📊 Статистика по пользователям", callback_data="admin_users_stats_overview")], # Placeholder
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_main")]
        ]
        return {"text": text, "reply_markup": InlineKeyboardMarkup(inline_keyboard=buttons)}

# --- Callback Handlers for Admin Panel ---
@admin_router.callback_query(F.data.startswith("admin_"))
@handle_errors() # Handles errors globally for these callbacks
async def handle_admin_panel_callback(callback: types.CallbackQuery, state: FSMContext, bot_instance: 'AICompanionBot'):
    """Main dispatcher for admin panel callback queries."""
    if not callback.message or not callback.from_user:
        return await callback.answer("Ошибка обработки.", show_alert=True)

    admin_panel = AdminPanel( # Create instance on demand
        db_service=bot_instance.db_service,
        subscription_service=bot_instance.subscription_service,
        config=bot_instance.config,
        bot_instance=bot_instance
    )

    if not await admin_panel.is_admin(callback.from_user.id):
        return await callback.answer("❌ Доступ запрещен.", show_alert=True)

    action = callback.data.split('admin_', 1)[1] # Remove "admin_" prefix
    
    try:
        if action == "main":
            menu_data = await admin_panel.create_main_admin_menu()
        elif action.startswith("analytics_period_"):
            period_days = int(action.split('_')[-1])
            menu_data = await admin_panel.create_analytics_submenu(period_days)
        elif action == "analytics_main":
            menu_data = await admin_panel.create_analytics_submenu(7) # Default to 7 days
        elif action.startswith("export_analytics_csv_"):
            period_days_csv = int(action.split('_')[-1])
            csv_file = await admin_panel.export_analytics_to_csv(period_days_csv)
            if csv_file:
                await callback.message.answer_document(document=csv_file, caption=f"📊 Экспорт аналитики за {period_days_csv} дней")
                await callback.answer(f"CSV файл за {period_days_csv} дней формируется...")
            else:
                await callback.answer("Не удалось создать CSV файл.", show_alert=True)
            return # No menu update needed after sending file
        elif action == "users_main":
            menu_data = await admin_panel.create_user_management_submenu()
        elif action == "maintenance_main":
            maint_text = "🧹 **Обслуживание Системы**"
            maint_buttons = [
                [InlineKeyboardButton(text="🗑️ Очистить старые логи/статистику", callback_data="admin_action_cleanup_data")],
                [InlineKeyboardButton(text="🔄 Перезагрузить промпты персон", callback_data="admin_action_reload_prompts")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_main")]
            ]
            menu_data = {"text": maint_text, "reply_markup": InlineKeyboardMarkup(inline_keyboard=maint_buttons)}
        elif action == "action_cleanup_data":
            # This should ideally be a background task or confirm before running
            # await bot_instance.db_service.cleanup_old_data(days_to_keep=90) 
            await callback.answer("Очистка старых данных (демо).", show_alert=True)
            return # No menu update
        elif action == "action_reload_prompts":
            prompt_manager.reload_prompts()
            if bot_instance.llm_service and hasattr(bot_instance.llm_service, 'clear_system_prompts_cache'):
                bot_instance.llm_service.clear_system_prompts_cache()
            await callback.answer("Промпты персон перезагружены.", show_alert=True)
            return # No menu update
        elif action == "close_panel":
            try: await callback.message.delete()
            except Exception as e_del: logger.warning(f"Не удалось удалить сообщение админ-панели: {e_del}")
            await callback.answer("Админ-панель закрыта.")
            return # No menu update
        else:
            await callback.answer(f"Действие '{action}' в разработке.", show_alert=True)
            return # No menu update for unimplemented actions

        # Update the message with the new menu
        if callback.message and menu_data.get("text") and menu_data.get("reply_markup"):
            await callback.message.edit_text(menu_data["text"], reply_markup=menu_data["reply_markup"], parse_mode="Markdown")
        await callback.answer() # Acknowledge callback

    except Exception as e: # Catch-all for safety during admin actions
        logger.error(f"Ошибка в обработчике админ-панели (action: {action}): {e}", exc_info=True)
        await callback.answer("Произошла внутренняя ошибка в админ-панели.", show_alert=True)

# Command to show the admin panel (can be registered in main.py)
@admin_router.message(Command("adminpanel")) 
@handle_errors()
async def cmd_show_admin_panel(message: types.Message, bot_instance: 'AICompanionBot'):
    """Displays the main admin panel menu via a command."""
    if not message.from_user: return

    admin_panel = AdminPanel(
        db_service=bot_instance.db_service,
        subscription_service=bot_instance.subscription_service,
        config=bot_instance.config,
        bot_instance=bot_instance
    )
    if not await admin_panel.is_admin(message.from_user.id):
        return await message.reply("❌ У вас нет доступа к этой команде.")

    menu_data = await admin_panel.create_main_admin_menu()
    await message.answer(menu_data["text"], reply_markup=menu_data["reply_markup"], parse_mode="Markdown")

