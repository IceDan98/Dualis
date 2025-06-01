# services/notification_marketing_system.py
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Set, Union # Added Union
from enum import Enum
from dataclasses import dataclass
import json

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, User as AiogramUser
from aiogram.utils.markdown import hbold

from database.operations import DatabaseService
from services.subscription_system import SubscriptionService, SubscriptionTier, SubscriptionStatus # Corrected import
from utils.error_handler import handle_errors

logger = logging.getLogger(__name__)

class NotificationType(Enum):
    """Defines the types of notifications the system can send."""
    SUBSCRIPTION_EXPIRY = "subscription_expiry"
    ENGAGEMENT_RETENTION = "engagement_retention"
    FEATURE_ANNOUNCEMENT = "feature_announcement"
    PROMOTIONAL = "promotional"
    REMINDER = "reminder"
    WELCOME = "welcome"
    SUBSCRIPTION_DOWNGRADED = "subscription_downgraded"

class NotificationPriority(Enum):
    """Defines the priority levels for notifications."""
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

@dataclass
class NotificationTemplate:
    """Structure for a notification template."""
    id: str # Unique identifier for the template
    type: NotificationType
    priority: NotificationPriority
    title: str # Title of the notification (can include placeholders)
    message: str # Main message content (can include placeholders)
    buttons: List[Dict[str, str]] # List of button data: [{"text": "Button", "callback_data": "action"}]
    target_segments: List[str] # User segments this template is for (e.g., 'new_user', 'inactive_7_days')
    conditions: Dict[str, Any] # Specific conditions for sending (e.g., {'days_until_expiry': {'exact': 3}})
    cooldown_hours: int # Minimum hours before this notification can be re-sent to the same user

class EngagementTracker:
    """Tracks user engagement based on activity."""
    def __init__(self, db_service: DatabaseService):
        self.db_service = db_service

    async def get_user_engagement_score(self, user_id_db: int, days_back: int = 7) -> float:
        """Calculates an engagement score for a user based on recent activity."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_back)
        # Local import for SQLAlchemy models to avoid circular dependencies at module level if models import services
        from database.models import Message, User, Conversation 
        from sqlalchemy import select, func, and_

        async with self.db_service.connection_manager.get_session() as session: # type: ignore
            # Count user messages within the period
            messages_result = await session.execute(
                select(func.count(Message.id)).join(Conversation).where(
                    and_(
                        Conversation.user_id == user_id_db,
                        Message.created_at >= cutoff_date,
                        Message.role == 'user' # Count only user messages for engagement
                    )
                )
            )
            message_count = messages_result.scalar_one_or_none() or 0

            # Count active days within the period
            active_days_result = await session.execute(
                select(func.count(func.distinct(func.date(Message.created_at)))).join(Conversation).where(
                    and_(
                        Conversation.user_id == user_id_db,
                        Message.created_at >= cutoff_date,
                        Message.role == 'user'
                    )
                )
            )
            active_days = active_days_result.scalar_one_or_none() or 0

            # Get last activity timestamp
            last_activity_result = await session.execute(
                select(User.last_activity).where(User.id == user_id_db)
            )
            last_activity_dt = last_activity_result.scalar_one_or_none()
            if last_activity_dt and last_activity_dt.tzinfo is None: # Ensure timezone aware
                last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)

            # Scoring logic (example, can be tuned)
            messages_score = min(message_count * 2, 40) 
            consistency_score = (active_days / days_back) * 30 if days_back > 0 else 0
            recency_score = 0
            if last_activity_dt:
                hours_since = (datetime.now(timezone.utc) - last_activity_dt).total_seconds() / 3600
                if hours_since <= 24: recency_score = 30
                elif hours_since <= 72: recency_score = 20
                elif hours_since <= 168: recency_score = 10 # 7 days
                elif hours_since <= 168 * 2: recency_score = 5 # 14 days
            total_score = messages_score + consistency_score + recency_score
            return min(total_score, 100.0) # Cap score at 100

    async def segment_users_by_engagement(self, days_for_inactive_check: int = 7) -> Dict[str, List[int]]:
        """Segments users based on their engagement levels and inactivity."""
        segments: Dict[str, List[int]] = {
            'high_engagement': [], 'medium_engagement': [], 'low_engagement': [], 'at_risk': [],
            f'inactive_{days_for_inactive_check}_days': [],
            f'inactive_{days_for_inactive_check*2}_days': [],
            f'inactive_{days_for_inactive_check*4}_days': []
        }
        from database.models import User # Local import
        from sqlalchemy import select

        async with self.db_service.connection_manager.get_session() as session: # type: ignore
            result = await session.execute(select(User.id, User.last_activity, User.created_at).where(User.is_active == True)) # type: ignore
            all_users_data = result.all()
            now_utc = datetime.now(timezone.utc)

            for user_id_db, last_activity_dt_raw, created_at_dt_raw in all_users_data:
                last_activity_dt = last_activity_dt_raw
                if last_activity_dt and last_activity_dt.tzinfo is None:
                    last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
                created_at_dt = created_at_dt_raw # Used as fallback if last_activity is None
                if created_at_dt and created_at_dt.tzinfo is None:
                    created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
                
                effective_last_activity = last_activity_dt or created_at_dt or now_utc # Fallback chain

                score = await self.get_user_engagement_score(user_id_db)
                days_inactive = (now_utc - effective_last_activity).days

                if days_inactive >= days_for_inactive_check * 4:
                    segments[f'inactive_{days_for_inactive_check*4}_days'].append(user_id_db)
                elif days_inactive >= days_for_inactive_check * 2:
                    segments[f'inactive_{days_for_inactive_check*2}_days'].append(user_id_db)
                elif days_inactive >= days_for_inactive_check:
                    segments[f'inactive_{days_for_inactive_check}_days'].append(user_id_db)
                elif score >= 70: segments['high_engagement'].append(user_id_db)
                elif score >= 40: segments['medium_engagement'].append(user_id_db)
                elif score >= 15: segments['low_engagement'].append(user_id_db)
                else: segments['at_risk'].append(user_id_db)
        logger.info(f"User engagement segmentation complete: { {k: len(v) for k, v in segments.items()} }")
        return segments

class NotificationService:
    """Service for sending scheduled and event-driven notifications to users."""
    def __init__(self, bot: Bot, db_service: DatabaseService,
                 subscription_service: SubscriptionService):
        self.bot = bot
        self.db_service = db_service
        self.subscription_service = subscription_service
        self.engagement_tracker = EngagementTracker(db_service)
        self.templates = self._init_default_templates()
        self.is_running = False
        self.last_cleanup_notifications_log = datetime.now(timezone.utc)
        self._background_tasks: List[asyncio.Task] = []

    def _init_default_templates(self) -> Dict[str, NotificationTemplate]:
        """Initializes default notification templates. Consider moving to a config file."""
        # (Template definitions as before, comments can be reduced if structure is clear)
        return {
            'welcome_new_user': NotificationTemplate(
                id='welcome_new_user', type=NotificationType.WELCOME, priority=NotificationPriority.HIGH,
                title='🎉 Добро пожаловать в AI Companion!',
                message=("Привет, {user_first_name}! Я рада, что ты здесь! 🌟\n\n"
                         "Сейчас ты можешь:\n"
                         "• Общаться с Aeris в дружеском режиме\n"
                         "• Создавать истории и получать советы\n"
                         "• Сохранять важные моменты в памяти\n\n"
                         "Хочешь разблокировать больше возможностей? "
                         "Загляни в раздел /premium и выбери свой путь к безграничному общению!"),
                buttons=[{"text": "🎮 Главное меню", "callback_data": "nav_main"},
                         {"text": "⭐ Тарифы Premium", "callback_data": "nav_subscription_plans_view"}],
                target_segments=['new_user'], conditions={'registration_hours_ago': {'max': 2}}, cooldown_hours=0 
            ),
            'subscription_expiry_warning_3_days': NotificationTemplate(
                id='subscription_expiry_warning_3_days', type=NotificationType.SUBSCRIPTION_EXPIRY, priority=NotificationPriority.HIGH,
                title='⚠️ Подписка «{tier_name}» скоро истекает!',
                message=("Твоя подписка «{tier_name}» истекает {expiry_date} (через {days_until_expiry} дн.)!\n\n"
                         "Не теряй доступ к эксклюзивным функциям. Продли сейчас!"),
                buttons=[{"text": "🔄 Продлить подписку", "callback_data": "nav_my_subscription_view"},
                         {"text": "🎁 Узнать о бонусах", "callback_data": "action_show_renewal_bonuses"}],
                target_segments=['basic_expiring', 'premium_expiring', 'vip_expiring'], 
                conditions={'days_until_expiry': {'exact': 3}}, cooldown_hours=24 * 3
            ),
            'subscription_expiry_warning_1_day': NotificationTemplate(
                id='subscription_expiry_warning_1_day', type=NotificationType.SUBSCRIPTION_EXPIRY, priority=NotificationPriority.CRITICAL,
                title='‼️ Остался 1 день подписки «{tier_name}»!',
                message=("Срок твоей подписки «{tier_name}» заканчивается завтра, {expiry_date}!\n\n"
                         "Продли сейчас, чтобы сохранить доступ."),
                buttons=[{"text": "🚀 Продлить НЕМЕДЛЕННО", "callback_data": "nav_my_subscription_view"}],
                target_segments=['basic_expiring', 'premium_expiring', 'vip_expiring'],
                conditions={'days_until_expiry': {'exact': 1}}, cooldown_hours=24
            ),
            'subscription_actually_expired': NotificationTemplate(
                id='subscription_actually_expired', type=NotificationType.SUBSCRIPTION_DOWNGRADED, priority=NotificationPriority.HIGH,
                title='😔 Ваша подписка «{original_tier_name}» истекла',
                message=("Срок действия вашей подписки «{original_tier_name}» истек {expiry_date_formatted}.\n"
                         "Вы были переведены на тариф Free.\n\n"
                         "Чтобы восстановить доступ, оформите новую подписку."),
                buttons=[{"text": "💳 Оформить новую подписку", "callback_data": "nav_subscription_plans_view"},
                         {"text": "☑️ Понятно", "callback_data": "action_acknowledge_downgrade"}],
                target_segments=[], conditions={}, cooldown_hours=24 * 30 
            ),
            'engagement_boost_inactive_7_days': NotificationTemplate(
                id='engagement_boost_inactive_7_days', type=NotificationType.ENGAGEMENT_RETENTION, priority=NotificationPriority.MEDIUM,
                title='😢 Мы скучаем по тебе, {user_first_name}!',
                message=("Ты не заходил(а) уже {days_inactive} дней... 💔\n\n"
                         "Возвращайся! Твои AI-компаньоны приготовили кое-что интересное."),
                buttons=[{"text": "❤️ Вернуться к общению", "callback_data": "nav_main"},
                         {"text": "🎁 Бонус за возвращение", "callback_data": "action_claim_return_bonus_7d"}],
                target_segments=['inactive_7_days'], conditions={'days_inactive': {'min': 7, 'max': 13}}, cooldown_hours=24 * 7
            ),
        }

    async def start(self):
        """Starts the notification service and its background tasks."""
        if self.is_running: logger.warning("NotificationService is already running."); return
        self.is_running = True
        logger.info("🔔 NotificationService started.")
        self._background_tasks.append(asyncio.create_task(self._notification_scheduler()))
        self._background_tasks.append(asyncio.create_task(self._cleanup_notifications_log_scheduler()))

    async def stop(self):
        """Stops the notification service and cancels background tasks."""
        if not self.is_running: logger.warning("NotificationService was not running."); return
        self.is_running = False
        for task in self._background_tasks:
            if task and not task.done(): task.cancel()
        try: await asyncio.gather(*self._background_tasks, return_exceptions=True)
        except asyncio.CancelledError: logger.info("NotificationService background tasks cancelled.")
        self._background_tasks.clear()
        logger.info("🔔 NotificationService stopped.")

    async def _notification_scheduler(self):
        """Periodically checks and sends relevant notifications."""
        while self.is_running:
            try:
                now_utc = datetime.now(timezone.utc)
                logger.info(f"NotificationScheduler: Running check at {now_utc.strftime('%Y-%m-%d %H:%M:%S UTC')}")
                
                await self._check_and_send_expiring_subscriptions()
                
                # Example: run engagement check every 6 hours
                if now_utc.hour % 6 == 0 and now_utc.minute < 15: 
                    engagement_segments = await self.engagement_tracker.segment_users_by_engagement(days_for_inactive_check=7)
                    await self._check_and_send_engagement_notifications(engagement_segments)
                
                await asyncio.sleep(3600) # Check hourly
            except asyncio.CancelledError: 
                logger.info("NotificationScheduler stopped."); break
            except Exception as e: 
                logger.error(f"Error in NotificationScheduler: {e}", exc_info=True)
                await asyncio.sleep(600) # Wait longer after an error

    async def _check_and_send_expiring_subscriptions(self):
        """Checks for subscriptions nearing expiry and sends warning notifications."""
        logger.debug("Checking for expiring subscriptions...")
        try:
            all_prefs = await self.db_service.get_all_user_preferences_by_key(
                self.subscription_service.SUBSCRIPTION_DATA_KEY, 
                persona=self.subscription_service.USER_PREFERENCE_PERSONA_SYSTEM
            )
            now_utc = datetime.now(timezone.utc)
            expiring_templates = [
                self.templates.get('subscription_expiry_warning_3_days'),
                self.templates.get('subscription_expiry_warning_1_day')
            ]
            expiring_templates = [t for t in expiring_templates if t is not None]

            for user_id_db, pref_value in all_prefs:
                try:
                    sub_data: Dict[str, Any] = pref_value # Assumes get_all_user_preferences_by_key returns parsed dict
                    if not isinstance(sub_data, dict): # Fallback if it's JSON string
                        sub_data = json.loads(str(pref_value))

                    tier_str = sub_data.get('tier')
                    status_str = sub_data.get('status')
                    expires_at_str = sub_data.get('expires_at')

                    if not tier_str or tier_str == SubscriptionTier.FREE.value or \
                       status_str != SubscriptionStatus.ACTIVE.value or not expires_at_str:
                        continue

                    expires_at_dt = datetime.fromisoformat(expires_at_str.replace('Z', '+00:00'))
                    if expires_at_dt.tzinfo is None: expires_at_dt = expires_at_dt.replace(tzinfo=timezone.utc)
                    if expires_at_dt < now_utc: continue

                    days_until_expiry = (expires_at_dt - now_utc).days
                    user_obj = await self.db_service.get_user_by_db_id(user_id_db)
                    if not (user_obj and user_obj.telegram_id): continue

                    for template in expiring_templates:
                        condition_days = template.conditions.get('days_until_expiry', {}).get('exact')
                        if condition_days is not None and days_until_expiry == condition_days and \
                           not await self._was_notification_sent(user_obj.telegram_id, template.id, template.cooldown_hours):
                            variables = {
                                'tier_name': sub_data.get('tier_name', tier_str.title()),
                                'expiry_date': expires_at_dt.strftime('%d.%m.%Y'),
                                'days_until_expiry': days_until_expiry + 1,
                                'user_first_name': user_obj.first_name or "Пользователь"
                            }
                            await self.send_notification(user_obj.telegram_id, template.id, variables)
                            break 
                except (json.JSONDecodeError, ValueError, TypeError) as e_parse:
                    logger.warning(f"Error processing subscription data for user_id_db {user_id_db}: {e_parse}. Data: {pref_value}")
                except Exception as e_inner:
                    logger.error(f"Inner error checking subscription for user_id_db {user_id_db}: {e_inner}", exc_info=True)
        except Exception as e:
            logger.error(f"General error in _check_and_send_expiring_subscriptions: {e}", exc_info=True)

    async def _check_and_send_engagement_notifications(self, engagement_segments: Dict[str, List[int]]):
        """Sends notifications to users based on their engagement segment."""
        logger.debug(f"Checking engagement notifications. Segments: { {k: len(v) for k, v in engagement_segments.items()} }")
        template_inactive_7d = self.templates.get('engagement_boost_inactive_7_days')
        
        if template_inactive_7d:
            target_segment_key = f'inactive_{template_inactive_7d.conditions.get("days_inactive", {}).get("min", 7)}_days'
            users_to_notify_db_ids = engagement_segments.get(target_segment_key, [])
            max_engagement_notifications_per_run = 50 # Limit batch size
            
            for user_id_db in users_to_notify_db_ids[:max_engagement_notifications_per_run]:
                try:
                    user_obj = await self.db_service.get_user_by_db_id(user_id_db)
                    if not (user_obj and user_obj.telegram_id and user_obj.is_active): continue
                    
                    last_activity_dt = user_obj.last_activity
                    if last_activity_dt and last_activity_dt.tzinfo is None:
                        last_activity_dt = last_activity_dt.replace(tzinfo=timezone.utc)
                    days_inactive = (datetime.now(timezone.utc) - (last_activity_dt or user_obj.created_at)).days

                    condition_days = template_inactive_7d.conditions.get('days_inactive', {})
                    if not (condition_days.get('min', 0) <= days_inactive <= condition_days.get('max', float('inf'))):
                        continue

                    if not await self._was_notification_sent(user_obj.telegram_id, template_inactive_7d.id, template_inactive_7d.cooldown_hours):
                        variables = {'user_first_name': user_obj.first_name or "дорогой друг", 'days_inactive': days_inactive}
                        await self.send_notification(user_obj.telegram_id, template_inactive_7d.id, variables)
                except Exception as e:
                    logger.error(f"Error sending engagement notification (segment: {target_segment_key}) for user_id_db {user_id_db}: {e}", exc_info=True)

    async def send_welcome_notification_if_needed(self, user_id_tg: int, user_first_name: Optional[str]):
        """Sends a welcome notification to a new user if not already sent."""
        template_id = 'welcome_new_user'
        template = self.templates.get(template_id)
        if not template: logger.error(f"Welcome template '{template_id}' not found."); return
        
        if not await self._was_notification_sent(user_id_tg, template_id, cooldown_hours=0): # Cooldown 0 for one-time
            variables = {'user_first_name': hbold(user_first_name or "пользователь")}
            await self.send_notification(user_id_tg, template_id, variables)
            logger.info(f"Welcome notification sent to user {user_id_tg}.")
        else:
            logger.info(f"Welcome notification already sent to user {user_id_tg}.")

    async def send_notification_on_downgrade(self, user_id_tg: int, original_tier_name: str, expiry_date_iso: Optional[str]):
        """Sends a notification when a user's subscription expires and is downgraded."""
        template_id = 'subscription_actually_expired'
        template = self.templates.get(template_id)
        if not template: logger.error(f"Downgrade template '{template_id}' not found."); return

        if await self._was_notification_sent(user_id_tg, template_id, template.cooldown_hours):
            logger.info(f"Downgrade notification '{template_id}' already sent to user {user_id_tg} within cooldown."); return
            
        user_first_name = "Пользователь"
        try:
            user_obj_tg: Union[AiogramUser, types.Chat, None] = await self.bot.get_chat(user_id_tg) # get_chat can return Chat for bots/channels
            if isinstance(user_obj_tg, AiogramUser) and user_obj_tg.first_name:
                user_first_name = user_obj_tg.first_name
        except Exception as e: logger.warning(f"Could not get user's first name for downgrade notification {user_id_tg}: {e}")

        expiry_date_formatted = "недавно"
        if expiry_date_iso:
            try:
                expiry_dt = datetime.fromisoformat(expiry_date_iso.replace('Z', '+00:00'))
                if expiry_dt.tzinfo is None: expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                expiry_date_formatted = expiry_dt.strftime('%d.%m.%Y')
            except ValueError: logger.warning(f"Invalid expiry date format '{expiry_date_iso}' for downgrade notification.")

        variables = {'original_tier_name': original_tier_name, 'expiry_date_formatted': expiry_date_formatted, 'user_first_name': user_first_name}
        await self.send_notification(user_id_tg, template_id, variables)
        logger.info(f"Downgrade notification sent to user {user_id_tg}.")


    @handle_errors(log_level="ERROR") # Decorator for robust error handling
    async def send_notification(self, telegram_id: int, template_id: str, variables: Optional[Dict[str, Any]] = None):
        """Sends a notification to a user based on a template and variables."""
        template = self.templates.get(template_id)
        if not template: 
            logger.error(f"Notification template '{template_id}' not found for user {telegram_id}.")
            return

        try:
            final_title = template.title.format(**(variables or {}))
            final_message_text = template.message.format(**(variables or {}))
        except KeyError as ke: 
            logger.error(f"Formatting error for template '{template_id}': missing key {ke}. Vars: {variables}")
            # Fallback to unformatted text or skip sending
            final_title = template.title 
            final_message_text = template.message # Or decide to not send

        keyboard = None
        if template.buttons:
            buttons_rows: List[List[InlineKeyboardButton]] = []
            # Handles both List[Dict] (single button per row) and List[List[Dict]] (multiple buttons per row)
            button_list_for_rows: List[List[Dict[str,str]]] = []
            if template.buttons and isinstance(template.buttons[0], dict):
                button_list_for_rows = [[btn_data] for btn_data in template.buttons]
            elif template.buttons and isinstance(template.buttons[0], list):
                button_list_for_rows = template.buttons # type: ignore
            
            for button_data_row in button_list_for_rows:
                row = []
                for button_data in button_data_row:
                    if isinstance(button_data, dict) and "text" in button_data and "callback_data" in button_data:
                        try:
                            button_text = button_data["text"].format(**(variables or {}))
                            button_cb = button_data["callback_data"].format(**(variables or {}))
                        except KeyError: # Fallback if formatting fails for button text/cb
                            button_text = button_data["text"]
                            button_cb = button_data["callback_data"]
                        row.append(InlineKeyboardButton(text=button_text, callback_data=button_cb))
                if row: buttons_rows.append(row)
            if buttons_rows: keyboard = InlineKeyboardMarkup(inline_keyboard=buttons_rows)
            
        try:
            await self.bot.send_message(
                chat_id=telegram_id, text=f"{hbold(final_title)}\n\n{final_message_text}",
                reply_markup=keyboard, parse_mode="Markdown", disable_web_page_preview=True
            )
            await self._record_notification_sent(telegram_id, template_id)
            logger.info(f"Notification '{template_id}' sent to user {telegram_id}.")
        except Exception as e: # Catch specific Telegram API errors if needed
            logger.error(f"Error sending notification '{template_id}' to user {telegram_id}: {e}", exc_info=True)
            if "bot was blocked by the user" in str(e).lower() or \
               "user is deactivated" in str(e).lower() or \
               "chat not found" in str(e).lower():
                user_db = await self.db_service.get_user_by_telegram_id(telegram_id)
                if user_db: await self.db_service.update_user_activity_status(user_db.id, is_active=False, reason_inactive=f"notification_send_fail_{type(e).__name__}")

    async def _was_notification_sent(self, telegram_id: int, template_id: str, cooldown_hours: int) -> bool:
        """Checks if a notification was recently sent to the user within the cooldown period."""
        user_db = await self.db_service.get_user_by_telegram_id(telegram_id)
        if not user_db: 
            logger.warning(f"_was_notification_sent: User TG ID {telegram_id} not found. Assuming not sent to avoid spam on error.")
            return False 
        
        # For welcome notification (cooldown_hours == 0), check one-time flag
        if template_id == 'welcome_new_user' and cooldown_hours == 0:
            preferences = await self.db_service.get_user_preferences(user_db.id, persona='notifications_log')
            return preferences.get(f'sent_once_{template_id}') is True 
        
        if cooldown_hours <= 0: return False # No cooldown, always allow sending (except for welcome)
            
        preferences = await self.db_service.get_user_preferences(user_db.id, persona='notifications_log')
        last_sent_iso_str = preferences.get(f'last_sent_{template_id}')
        if last_sent_iso_str and isinstance(last_sent_iso_str, str):
            try:
                last_sent_dt = datetime.fromisoformat(last_sent_iso_str.replace('Z', '+00:00'))
                if last_sent_dt.tzinfo is None: last_sent_dt = last_sent_dt.replace(tzinfo=timezone.utc)
                return last_sent_dt > (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours))
            except ValueError:
                logger.warning(f"Invalid date format '{last_sent_iso_str}' for last_sent_{template_id} of user {telegram_id}.")
        return False

    async def _record_notification_sent(self, telegram_id: int, template_id: str):
        """Records that a notification has been sent to the user."""
        user_db = await self.db_service.get_user_by_telegram_id(telegram_id)
        if not user_db: 
            logger.error(f"_record_notification_sent: User TG ID {telegram_id} not found. Cannot record.")
            return
        
        key_to_set, value_to_set_str, pref_type = "", "", ""
        if template_id == 'welcome_new_user': # Special handling for one-time welcome
            key_to_set = f'sent_once_{template_id}'; value_to_set_str = 'true'; pref_type = 'bool_str'
        else:
            key_to_set = f'last_sent_{template_id}'; value_to_set_str = datetime.now(timezone.utc).isoformat(); pref_type = 'string'
        
        await self.db_service.update_user_preference(
            user_id_db=user_db.id, key=key_to_set, value=value_to_set_str,
            persona='notifications_log', preference_type=pref_type
        )

    async def _cleanup_notifications_log_scheduler(self):
        """Periodically cleans up old notification log entries from UserPreference."""
        while self.is_running:
            await asyncio.sleep(24 * 60 * 60) # Run daily
            try: await self._cleanup_old_notifications_log_entries()
            except asyncio.CancelledError: logger.info("Notification log cleanup scheduler stopped."); break
            except Exception as e: logger.error(f"Error in notification log cleanup scheduler: {e}", exc_info=True)

    async def _cleanup_old_notifications_log_entries(self):
        """Deletes old 'last_sent_*' entries from UserPreference."""
        # Keep logs for a reasonable period, e.g., 90 days
        days_to_keep_logs = getattr(self.db_service.connection_manager, '_pool_recycle', 90) 
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_to_keep_logs)
        logger.info(f"Running cleanup of notification logs (last_sent_*) older than {cutoff_date.strftime('%Y-%m-%d')}.")
        
        if hasattr(self.db_service, 'delete_user_preferences_older_than_by_datetime_value_and_key_prefix'):
            deleted_count = await self.db_service.delete_user_preferences_older_than_by_datetime_value_and_key_prefix(
                persona='notifications_log', key_prefix='last_sent_', cutoff_date=cutoff_date, preference_type_filter='string'
            )
            logger.info(f"Cleaned up {deleted_count} old 'last_sent_*' notification log entries.")
            self.last_cleanup_notifications_log = datetime.now(timezone.utc)
        else:
            logger.warning("Required DB method for log cleanup not found. Skipping cleanup of 'last_sent_'.")
        # 'sent_once_*' flags are typically not deleted by date.

    async def send_broadcast_message(self, message_text: str, target_segments_keys: List[str],
                                     buttons_list: Optional[List[List[Dict[str, str]]]] = None,
                                     title: Optional[str] = "📢 Важное сообщение") -> Dict[str, int]:
        """Sends a broadcast message to users in specified segments."""
        sent_count, failed_count = 0, 0
        all_target_user_db_ids: Set[int] = set()

        if "all_active_users" in target_segments_keys:
            if hasattr(self.db_service, 'get_active_user_db_ids'):
                active_ids = await self.db_service.get_active_user_db_ids(days_inactive_threshold=30)
                all_target_user_db_ids.update(active_ids)
            else: # Fallback if specific method is not available
                from database.models import User; from sqlalchemy import select # Local import
                async with self.db_service.connection_manager.get_session() as s: # type: ignore
                    res = await s.execute(select(User.id).where(User.is_active == True)); all_target_user_db_ids.update([uid for uid, in res.all()]) # type: ignore
        else:
            engagement_data = await self.engagement_tracker.segment_users_by_engagement()
            for seg_key in target_segments_keys:
                all_target_user_db_ids.update(engagement_data.get(seg_key, []))
        
        if not all_target_user_db_ids:
            logger.info(f"Broadcast: No users in segments {target_segments_keys}."); return {'sent': 0, 'failed': 0, 'total_targets': 0}

        logger.info(f"Starting broadcast to {len(all_target_user_db_ids)} users. Segments: {target_segments_keys}")
        reply_markup = None
        if buttons_list:
            inline_keyboard_rows = []
            for row_data in buttons_list:
                current_row = [InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"]) for btn in row_data if isinstance(btn,dict) and "text" in btn and "callback_data" in btn]
                if current_row: inline_keyboard_rows.append(current_row)
            if inline_keyboard_rows: reply_markup = InlineKeyboardMarkup(inline_keyboard=inline_keyboard_rows)

        final_msg_text = f"{hbold(title)}\n\n{message_text}" if title else message_text
        
        for user_id_db in list(all_target_user_db_ids):
            user_obj = await self.db_service.get_user_by_db_id(user_id_db)
            if not (user_obj and user_obj.telegram_id and user_obj.is_active): 
                failed_count +=1; continue
            try:
                await self.bot.send_message(
                    chat_id=user_obj.telegram_id, text=final_msg_text, reply_markup=reply_markup, 
                    parse_mode="Markdown", disable_web_page_preview=True
                )
                sent_count += 1
                if sent_count % 25 == 0: await asyncio.sleep(1.1) # Rate limit protection
            except Exception as e:
                failed_count += 1
                logger.warning(f"Error broadcasting to user TG_ID {user_obj.telegram_id} (DB_ID {user_id_db}): {e}")
                if "bot was blocked" in str(e).lower() or "user is deactivated" in str(e).lower() or "chat not found" in str(e).lower():
                    await self.db_service.update_user_activity_status(user_id_db, is_active=False, reason_inactive=f"broadcast_fail_{type(e).__name__}")
        
        logger.info(f"Broadcast finished. Sent: {sent_count}, Failed: {failed_count}, Total targets: {len(all_target_user_db_ids)}")
        return {'sent': sent_count, 'failed': failed_count, 'total_targets': len(all_target_user_db_ids)}

    async def get_notification_stats(self) -> Dict[str, Any]:
        """Returns statistics about the notification service."""
        # This is a basic implementation. More detailed stats can be added.
        stats = {
            'templates_count': len(self.templates),
            'active_background_tasks': len([task for task in self._background_tasks if task and not task.done()]),
            'last_log_cleanup_at': self.last_cleanup_notifications_log.isoformat()
        }
        # Example: Count total 'last_sent_*' records (might be slow on large DBs without specific query)
        # total_sent_records = await self.db_service.count_user_preferences_by_persona_and_prefix('notifications_log', 'last_sent_')
        # stats['total_recorded_notifications_in_db'] = total_sent_records
        logger.warning("get_notification_stats: Detailed DB stats for notifications not fully implemented.")
        return stats
