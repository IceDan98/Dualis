# analytics/user_segmentation.py
import logging
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta, timezone

from database.operations import DatabaseService
from database.enums import SubscriptionTier, SubscriptionStatus 

logger = logging.getLogger(__name__)

class UserSegmentationEngine:
    """
    Advanced user segmentation for personalized experiences and targeted actions.
    Relies on DatabaseService.get_all_users_with_extended_metrics() to provide comprehensive user data.
    """

    def __init__(self, db_service: DatabaseService):
        self.db_service = db_service
        # Пороговые значения для сегментации (можно вынести в конфиг)
        self.segmentation_thresholds = {
            "newbie_max_days": 7,
            "newbie_active_min_messages_7d": 5,
            "free_engaged_min_active_days_30d": 10,
            "free_disengaged_max_active_days_30d": 3,
            "free_disengaged_min_account_age_days": 14,
            "free_churn_risk_min_inactive_days": 60,
            "paid_churn_risk_max_active_days_30d": 5,
            "paid_churn_risk_max_days_to_expiry": 7,
            "loyal_paid_min_months": 6,
            "loyal_paid_min_active_days_30d": 10,
            "potential_upgrade_basic_active_days_30d": 20, # Примерный порог для активного Basic
            "potential_upgrade_premium_active_days_30d": 25, # Примерный порог для активного Premium
            "power_user_story_min_count_30d": 5,
            "power_user_memory_min_count_30d": 10,
            "promocode_lover_min_count": 2,
            "recently_churned_paid_lookback_days": 60,
            "high_value_ltv_threshold_multiplier": 3, # Множитель для ARPU из конфига
            "high_value_vip_active_days_30d": 15,
        }


    async def segment_all_users(self, days_lookback_activity: int = 30) -> Dict[str, List[int]]:
        """
        Segments all relevant users based on various criteria.
        Returns a dictionary where keys are segment names and values are lists of user_telegram_id.
        """
        logger.info(f"Starting comprehensive user segmentation (activity lookback: {days_lookback_activity} days).")
        # Инициализация словаря сегментов
        segments: Dict[str, List[int]] = {
            f"newbies_active_last_{self.segmentation_thresholds['newbie_max_days']}d": [],
            f"newbies_inactive_last_{self.segmentation_thresholds['newbie_max_days']}d": [],
            "engaged_free_users": [], "disengaged_free_users": [],
            "paying_basic": [], "paying_premium": [], "paying_vip": [],
            "high_value_customers": [], "at_risk_churn_paid": [], "at_risk_churn_free": [],
            "potential_upgrade_to_premium": [], "potential_upgrade_to_vip": [],
            "feature_power_users_story": [], "feature_power_users_memory": [],
            "promocode_lovers": [], "long_term_loyal_paid": [], "recently_churned_paid": [],
        }

        all_users_data = await self.db_service.get_all_users_with_extended_metrics(days_lookback=days_lookback_activity)
        if not all_users_data:
            logger.warning("No user data retrieved. Segmentation cannot proceed.")
            return segments

        logger.info(f"Retrieved data for {len(all_users_data)} users for segmentation.")
        now_utc = datetime.now(timezone.utc)

        for user_data in all_users_data:
            user_id_tg = user_data.get("telegram_id")
            user_id_db = user_data.get("user_id_db")
            if not user_id_tg or not user_id_db: continue

            user_info = user_data.get("user_info", {})
            subscription_info = user_data.get("subscription", {})
            activity_info = user_data.get("activity", {})
            monetization_info = user_data.get("monetization", {})
            
            created_at_str = user_info.get("created_at")
            account_age_days = 9999
            if created_at_str:
                try: 
                    created_at_dt = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                    if created_at_dt.tzinfo is None: created_at_dt = created_at_dt.replace(tzinfo=timezone.utc)
                    account_age_days = (now_utc - created_at_dt).days
                except ValueError: logger.warning(f"Invalid created_at format for user {user_id_tg}: {created_at_str}")

            current_tier_str = subscription_info.get("tier", SubscriptionTier.FREE.value)
            try: current_tier_enum = SubscriptionTier(current_tier_str)
            except ValueError: current_tier_enum = SubscriptionTier.FREE
            
            current_status_str = subscription_info.get("status", SubscriptionStatus.ACTIVE.value)
            try: current_status_enum = SubscriptionStatus(current_status_str)
            except ValueError: current_status_enum = SubscriptionStatus.ACTIVE

            is_currently_paid_and_active = (current_tier_enum != SubscriptionTier.FREE and \
                                 current_status_enum in [SubscriptionStatus.ACTIVE, SubscriptionStatus.GRACE_PERIOD, SubscriptionStatus.TRIAL])
            
            days_until_expiry = subscription_info.get("days_until_expiry") # Может быть None
            
            # Активность
            # messages_last_7d - используем данные за меньший из периодов: 7 дней или days_lookback_activity
            lookback_for_newbie_activity = min(7, days_lookback_activity)
            activity_newbie_period = await self.db_service.get_user_activity_stats(user_id_db, days=lookback_for_newbie_activity)
            messages_newbie_period = activity_newbie_period.get("message_count", 0)

            active_days_current_lookback = activity_info.get(f"active_days_last_{days_lookback_activity}d", 0)
            
            # Монетизация
            ltv_stars = monetization_info.get("ltv_stars", 0.0)
            promocodes_used_count = monetization_info.get("promocodes_used_count", 0) # Общее число использований
            total_paid_months = monetization_info.get("total_subscribed_months_paid", 0) # Должен быть из get_all_users_with_extended_metrics

            # --- Логика сегментации ---
            newbie_max_d = self.segmentation_thresholds['newbie_max_days']
            if account_age_days <= newbie_max_d:
                if messages_newbie_period >= self.segmentation_thresholds['newbie_active_min_messages_7d']:
                    segments[f"newbies_active_last_{newbie_max_d}d"].append(user_id_tg)
                else:
                    segments[f"newbies_inactive_last_{newbie_max_d}d"].append(user_id_tg)
            
            if current_tier_enum == SubscriptionTier.FREE:
                if active_days_current_lookback >= self.segmentation_thresholds['free_engaged_min_active_days_30d']:
                    segments["engaged_free_users"].append(user_id_tg)
                elif active_days_current_lookback <= self.segmentation_thresholds['free_disengaged_max_active_days_30d'] and \
                     account_age_days > self.segmentation_thresholds['free_disengaged_min_account_age_days']:
                    segments["disengaged_free_users"].append(user_id_tg)
                    if account_age_days > self.segmentation_thresholds['free_churn_risk_min_inactive_days']:
                        segments["at_risk_churn_free"].append(user_id_tg)
            
            if is_currently_paid_and_active:
                if current_tier_enum == SubscriptionTier.BASIC: segments["paying_basic"].append(user_id_tg)
                elif current_tier_enum == SubscriptionTier.PREMIUM: segments["paying_premium"].append(user_id_tg)
                elif current_tier_enum == SubscriptionTier.VIP: segments["paying_vip"].append(user_id_tg)

                target_arppu = self.db_service.bot_config.target_arppu_stars or 150.0 # Из конфига
                if ltv_stars > target_arppu * self.segmentation_thresholds['high_value_ltv_threshold_multiplier'] or \
                   (current_tier_enum == SubscriptionTier.VIP and active_days_current_lookback >= self.segmentation_thresholds['high_value_vip_active_days_30d']):
                    segments["high_value_customers"].append(user_id_tg)
                
                is_near_expiry = (days_until_expiry is not None and 0 <= days_until_expiry < self.segmentation_thresholds['paid_churn_risk_max_days_to_expiry'])
                low_activity_paid_for_churn = active_days_current_lookback < self.segmentation_thresholds['paid_churn_risk_max_active_days_30d']
                if (low_activity_paid_for_churn and account_age_days > 30) or is_near_expiry: # Не новый и малоактивный ИЛИ скоро истекает
                    segments["at_risk_churn_paid"].append(user_id_tg)
                
                if total_paid_months >= self.segmentation_thresholds['loyal_paid_min_months'] and \
                   active_days_current_lookback >= self.segmentation_thresholds['loyal_paid_min_active_days_30d']:
                    segments["long_term_loyal_paid"].append(user_id_tg)

                if current_tier_enum == SubscriptionTier.BASIC and active_days_current_lookback > self.segmentation_thresholds['potential_upgrade_basic_active_days_30d']:
                    segments["potential_upgrade_to_premium"].append(user_id_tg)
                if current_tier_enum == SubscriptionTier.PREMIUM and active_days_current_lookback > self.segmentation_thresholds['potential_upgrade_premium_active_days_30d']:
                    segments["potential_upgrade_to_vip"].append(user_id_tg)
            else: # Не является активным платным подписчиком СЕЙЧАС
                # Проверяем, был ли он недавно платным
                last_paid_sub_ended = await self.db_service.get_last_paid_subscription_ended_in_period(user_id_db, self.segmentation_thresholds['recently_churned_paid_lookback_days'])
                if last_paid_sub_ended:
                    segments["recently_churned_paid"].append(user_id_tg)
            
            feature_usage_data = activity_info.get("feature_usage_last_30d", {})
            if isinstance(feature_usage_data, dict):
                if feature_usage_data.get("story_creation_count", 0) > self.segmentation_thresholds['power_user_story_min_count_30d']:
                    segments["feature_power_users_story"].append(user_id_tg)
                # Пример для памяти, если бы get_all_users_with_extended_metrics возвращал это
                # if feature_usage_data.get("memory_saves_count", 0) > self.segmentation_thresholds['power_user_memory_min_count_30d']:
                #     segments["feature_power_users_memory"].append(user_id_tg)

            if promocodes_used_count >= self.segmentation_thresholds['promocode_lover_min_count']: 
                segments["promocode_lovers"].append(user_id_tg)

        for seg_name, user_ids_list in segments.items():
            if len(user_ids_list) > 0: 
                logger.info(f"Segment '{seg_name}': {len(user_ids_list)} users.")
                if len(user_ids_list) < 5: 
                    logger.debug(f"Segment '{seg_name}' example user_ids: {user_ids_list[:5]}") # Показываем до 5 ID
        return segments

    async def apply_personalized_action_for_segment(self, user_id_tg: int, segment_name: str,
                                                  bot_instance: Optional[Any] = None # AICompanionBot instance
                                                  ) -> Dict[str, Any]:
        """
        Applies a personalized action for a user based on their segment.
        Requires bot_instance to access NotificationService, PromoCodeService.
        """
        logger.info(f"Attempting personalized action for user {user_id_tg} in segment '{segment_name}'.")
        action_taken_description = "No specific action defined or executed for this segment yet."
        success = False 
        
        if not bot_instance:
            logger.warning("bot_instance not provided to apply_personalized_action_for_segment. Cannot execute actions.")
            return {"user_id_tg": user_id_tg, "segment": segment_name, "action_taken": "Error: Bot services unavailable.", "success": False}

        notification_service = getattr(bot_instance, 'notification_service', None)
        promocode_service = getattr(bot_instance, 'promocode_service', None)
        db_user = await self.db_service.get_user_by_telegram_id(user_id_tg)

        if not db_user:
            logger.error(f"User TG ID {user_id_tg} not found in DB for personalized action.")
            return {"user_id_tg": user_id_tg, "segment": segment_name, "action_taken": "Error: User not found.", "success": False}

        user_first_name = db_user.first_name or "дорогой пользователь"

        try:
            if segment_name == "at_risk_churn_paid" and notification_service and promocode_service:
                # Генерируем персональный промокод на скидку для удержания
                promo = await promocode_service.create_promocode(
                    discount_type=promocode_service.PromoCodeDiscountType.PERCENTAGE, # Используем Enum из сервиса
                    discount_value=25.0, # 25% скидка
                    description=f"Персональная скидка 25% для удержания пользователя {user_id_tg}",
                    user_specific_id=db_user.id, 
                    expires_in_days=7, max_uses=1,
                    code_type=promocode_service.PromoCodeType.USER_SPECIFIC,
                    user_facing_description=f"Мы ценим вас! Вот ваша персональная скидка 25% на продление подписки."
                )
                if promo:
                    # Отправляем уведомление с промокодом
                    # Предполагается, что в NotificationService есть шаблон "retention_offer_paid"
                    await notification_service.send_notification(
                        user_id_tg, "retention_offer_paid_promo", # Пример ID шаблона
                        variables={"user_first_name": user_first_name, "promocode": promo.code, "discount_percent": 25}
                    )
                    action_taken_description = f"Отправлено предложение по удержанию со скидкой 25% (промокод: {promo.code})."
                    success = True
                else:
                    action_taken_description = "Не удалось создать промокод для удержания."

            elif segment_name == "newbies_active_last_7d" and notification_service:
                await notification_service.send_notification(
                    user_id_tg, "newbie_active_engagement_tip", # Пример ID шаблона
                    variables={"user_first_name": user_first_name}
                )
                action_taken_description = "Отправлен совет по вовлечению для активного новичка."
                success = True
            
            elif segment_name == "potential_upgrade_to_premium" and notification_service:
                # TODO: Проверить, что пользователь еще не Premium/VIP
                current_sub_data = await self.db_service.get_active_subscription_for_user(db_user.id)
                if current_sub_data and current_sub_data.tier == SubscriptionTier.BASIC:
                    await notification_service.send_notification(
                        user_id_tg, "upgrade_to_premium_offer", # Пример ID шаблона
                        variables={"user_first_name": user_first_name}
                    )
                    action_taken_description = "Отправлено предложение об апгрейде до Premium для активного Basic пользователя."
                    success = True
                else:
                    action_taken_description = "Пользователь уже не Basic или нет активной подписки для апгрейда."


            # Добавить другие сегменты и действия...

            if success:
                logger.info(f"Personalized action '{action_taken_description}' for user {user_id_tg} in segment '{segment_name}' succeeded.")
            elif action_taken_description.startswith("No specific action"):
                 logger.info(f"No specific action implemented for user {user_id_tg} in segment '{segment_name}'.")


        except Exception as e:
            logger.error(f"Error applying personalized action for user {user_id_tg}, segment '{segment_name}': {e}", exc_info=True)
            action_taken_description = f"Error: {str(e)}"
            success = False

        return {
            "user_id_tg": user_id_tg,
            "segment": segment_name,
            "action_taken": action_taken_description,
            "success": success
        }
