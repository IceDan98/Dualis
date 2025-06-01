# analytics/ml_predictor.py
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta, timezone
import random # Для заглушек и вариативности

from database.operations import DatabaseService 

logger = logging.getLogger(__name__)

class MLPredictor:
    """
    Machine learning predictions for business intelligence.
    Initial version with simple heuristics and stubs for ML models.
    Uses implemented methods from DatabaseService.
    """
    def __init__(self, db_service: DatabaseService):
        self.db_service = db_service
        # Модели пока не используются, но оставлены для будущей интеграции
        # self.user_growth_model = None 
        # self.churn_model = None       
        # self.revenue_model = None     

    async def predict_user_growth(self, days_ahead: int) -> Dict[str, Any]:
        """
        Predicts user growth for the next N days.
        Uses simple linear trend based on historical data from DatabaseService.
        """
        logger.debug(f"Predicting user growth for {days_ahead} days ahead.")
        try:
            # Получаем исторические данные о ежедневных регистрациях за последние 90 дней
            historical_data_raw = await self.db_service.get_daily_new_users_stats(days_lookback=90)

            if not historical_data_raw or len(historical_data_raw) < 14: 
                logger.warning(f"Not enough historical data for user growth prediction (got {len(historical_data_raw)} days). Using fallback.")
                # Улучшенный fallback: используем среднее за последние доступные дни, если есть хоть какие-то данные
                if historical_data_raw:
                    avg_recent_daily = sum(d.get("new_users_count",0) for d in historical_data_raw[-7:]) / min(7, len(historical_data_raw))
                    fallback_avg_daily = max(1, round(avg_recent_daily)) if avg_recent_daily > 0 else 5 # Минимум 5, если нет данных
                else:
                    fallback_avg_daily = 5 
                
                return {
                    "predicted_new_users_total": fallback_avg_daily * days_ahead,
                    "confidence_interval_total": {
                        "lower": int(fallback_avg_daily * days_ahead * 0.3), # Более широкий интервал для fallback
                        "upper": int(fallback_avg_daily * days_ahead * 2.0)
                    },
                    "method": "fallback_average_or_constant",
                    "confidence": "very_low",
                    "comment": "Insufficient historical data for robust trend analysis."
                }

            # Используем логику из roadmap_qa_document.md для простого прогноза
            # Данные уже должны быть отсортированы по дате из get_daily_new_users_stats
            days_hist_indices = list(range(len(historical_data_raw))) # Оси X - просто порядковые номера дней
            registrations_hist_values = [data.get("new_users_count", 0) for data in historical_data_raw]

            n_hist = len(days_hist_indices)
            sum_x = sum(days_hist_indices)
            sum_y = sum(registrations_hist_values)
            sum_xy = sum(x * y for x, y in zip(days_hist_indices, registrations_hist_values))
            sum_x2 = sum(x * x for x in days_hist_indices)

            # Коэффициенты линейной регрессии y = a * x + b
            a_trend = 0.0 # Коэффициент наклона (среднедневной прирост/убыль)
            b_intercept = float(sum_y / n_hist) if n_hist > 0 else 5.0 # Среднее, если нет тренда

            if n_hist > 1 and (n_hist * sum_x2 - sum_x * sum_x) != 0: 
                a_trend = (n_hist * sum_xy - sum_x * sum_y) / (n_hist * sum_x2 - sum_x * sum_x)
                b_intercept = (sum_y - a_trend * sum_x) / n_hist
            elif n_hist == 1: 
                a_trend = 0 
                b_intercept = float(registrations_hist_values[0])

            # Прогноз на будущее по тренду
            future_day_indices_for_pred = [len(days_hist_indices) + i for i in range(days_ahead)]
            predicted_values_by_trend = [max(0, a_trend * day_idx + b_intercept) for day_idx in future_day_indices_for_pred]

            # Учет простой сезонности (недельной)
            seasonal_factor_applied = self._calculate_simple_weekly_seasonality(registrations_hist_values)

            final_daily_predictions = [val * seasonal_factor_applied for val in predicted_values_by_trend]
            total_predicted_new_users = sum(final_daily_predictions)

            # Расчет доверительного интервала (упрощенный)
            std_error_of_estimate = 0.0
            if n_hist > 2 and (sum_x2 - (sum_x**2)/n_hist if n_hist > 0 else 0) != 0 :
                residuals_squared_sum = sum([(reg - (a_trend * day_idx + b_intercept))**2 for day_idx, reg in zip(days_hist_indices, registrations_hist_values)])
                mse = residuals_squared_sum / (n_hist - 2) 
                std_error_of_estimate = mse**0.5
            elif n_hist > 0: # Если только среднее, без тренда
                std_error_of_estimate = (sum( (reg - b_intercept)**2 for reg in registrations_hist_values ) / n_hist)**0.5
            
            # Приблизительный CI для суммы (очень грубо, т.к. ошибки прогнозов коррелируют)
            # Увеличиваем неопределенность для суммы
            std_error_sum_approx = std_error_of_estimate * (days_ahead**0.5) * 1.5 # Множитель 1.5 для доп. неопределенности

            lower_bound_total = max(0, int(total_predicted_new_users - 1.96 * std_error_sum_approx ))
            upper_bound_total = int(total_predicted_new_users + 1.96 * std_error_sum_approx )

            return {
                "predicted_new_users_total": max(0, int(round(total_predicted_new_users))), 
                "predicted_daily_avg": round(total_predicted_new_users / days_ahead, 2) if days_ahead > 0 else 0,
                "confidence_interval_total": {"lower": lower_bound_total, "upper": upper_bound_total},
                "trend_coeff_a_per_day": round(a_trend, 3), 
                "trend_intercept_b": round(b_intercept,2), 
                "seasonal_factor_applied": round(seasonal_factor_applied, 3),
                "method": "simple_linear_regression_with_weekly_seasonality",
                "confidence": "medium" if std_error_sum_approx < (total_predicted_new_users * 0.35 if total_predicted_new_users > 0 else 50) else "low"
            }
        except Exception as e:
            logger.error(f"Error in predict_user_growth: {e}", exc_info=True)
            return {"error": str(e), "predicted_new_users_total": 0, "confidence": "error"}


    def _calculate_simple_weekly_seasonality(self, daily_values: List[float]) -> float:
        """Расчет простого недельного сезонного фактора."""
        if len(daily_values) < 14: return 1.0 # Нужно хотя бы 2 полные недели

        # Сравниваем среднее за последние 7 дней со средним за предыдущие 7 дней
        recent_week_sum = sum(daily_values[-7:])
        previous_week_sum = sum(daily_values[-14:-7])

        if recent_week_sum == 0 and previous_week_sum == 0: return 1.0
        if previous_week_sum == 0: return 1.2 # Небольшой рост, если раньше не было данных (или 1.0)

        recent_avg = recent_week_sum / 7.0
        previous_avg = previous_week_sum / 7.0

        seasonal_factor = recent_avg / previous_avg if previous_avg > 0 else 1.0
        return max(0.7, min(1.5, seasonal_factor)) # Ограничиваем фактор (0.7 - 1.5)

    async def identify_churn_risk_users(self, risk_threshold: float = 0.6) -> List[Dict[str, Any]]:
        """
        Identifies users at high risk of churning using data from DatabaseService.
        Uses logic from roadmap_qa_document.md and available data.
        """
        logger.debug(f"Identifying churn risk users (threshold: {risk_threshold}).")
        try:
            # Получаем данные по активным подписчикам и их активности
            # get_active_subscribers_with_activity теперь возвращает более полные данные
            active_subscribers_data = await self.db_service.get_active_subscribers_with_activity(days_for_activity_lookback=30)

            if not active_subscribers_data:
                logger.info("No active subscribers data found to analyze for churn risk.")
                return []

            high_risk_users_list = []
            for user_data_dict in active_subscribers_data:
                # user_data_dict должен содержать поля, как описано в DatabaseService,
                # например: "user_id_db", "telegram_id", "messages_last_7_days", "messages_previous_7_days",
                # "avg_session_duration_minutes_last_30d", "days_until_expiry", "current_tier", etc.
                
                risk_score = await self._calculate_simple_churn_risk_from_db_data(user_data_dict)
                if risk_score > risk_threshold: 
                     risk_factors_identified = await self._identify_risk_factors_from_db_data(user_data_dict)
                     high_risk_users_list.append({
                        "user_id_tg": user_data_dict.get("telegram_id"), 
                        "db_user_id": user_data_dict.get("user_id_db"),
                        "risk_score": round(risk_score, 3),
                        "risk_factors": risk_factors_identified,
                        "recommended_action": self._get_retention_recommendation_stub(risk_score),
                        "days_until_expiry": user_data_dict.get("days_until_expiry"), # Может быть None
                        "current_tier": user_data_dict.get("current_tier")
                    })
            
            high_risk_users_list.sort(key=lambda x: x["risk_score"], reverse=True)
            logger.info(f"Identified {len(high_risk_users_list)} high churn risk users (threshold > {risk_threshold}).")
            return high_risk_users_list # Возвращаем всех, кто выше порога

        except Exception as e:
            logger.error(f"Error in identify_churn_risk_users: {e}", exc_info=True)
            return [{"error": str(e), "user_id_tg": 0, "risk_score": 0.0, "risk_factors": ["error_in_processing"]}]

    async def _calculate_simple_churn_risk_from_db_data(self, user_data: Dict[str, Any]) -> float:
        """Расчет риска оттока на основе данных из БД, согласно roadmap_qa_document.md."""
        risk_factors_values: Dict[str, float] = {
            "usage_decline": 0.0, "low_engagement_sessions": 0.0, "low_engagement_activity_days": 0.0,
            "subscription_expiry_soon": 0.0, "support_issues_high": 0.0, "payment_issues_exist": 0.0,
            "low_feature_interaction": 0.0
        }
        
        # Фактор 1: Снижение использования (сообщения)
        current_activity_msg_7d = user_data.get("messages_last_7_days", 0)
        previous_activity_msg_7d = user_data.get("messages_previous_7_days", 0) 
        
        if previous_activity_msg_7d > 5: # Если была хоть какая-то значимая активность
            activity_change_ratio = (previous_activity_msg_7d - current_activity_msg_7d) / previous_activity_msg_7d
            risk_factors_values["usage_decline"] = max(0, min(1, activity_change_ratio * 1.2)) # Усиленный вес для спада
        elif current_activity_msg_7d < 2 and previous_activity_msg_7d <=5 : # Если и раньше было мало, и сейчас мало
             risk_factors_values["usage_decline"] = 0.4 # Небольшой риск из-за общей низкой активности
        
        # Фактор 2: Низкая вовлеченность (длительность сессий и активные дни)
        avg_session_duration_min = user_data.get("avg_session_duration_minutes_last_30d", 10.0) 
        if avg_session_duration_min < 2.0: risk_factors_values["low_engagement_sessions"] = 0.8
        elif avg_session_duration_min < 5.0: risk_factors_values["low_engagement_sessions"] = 0.4
        
        active_days_30d = user_data.get("active_days_last_N_days", 15) # N = 30 в get_active_subscribers_with_activity
        if active_days_30d < 5: risk_factors_values["low_engagement_activity_days"] = 0.7
        elif active_days_30d < 10: risk_factors_values["low_engagement_activity_days"] = 0.35
        
        # Фактор 3: Близкий срок истечения подписки
        days_until_expiry = user_data.get("days_until_expiry") # Может быть None для Free/истекших
        if days_until_expiry is not None:
            if user_data.get("current_tier") != SubscriptionTier.FREE.value and not user_data.get("is_trial", False): # Только для платных нетриальных
                if days_until_expiry <= 3: risk_factors_values["subscription_expiry_soon"] = 0.9
                elif days_until_expiry <= 7: risk_factors_values["subscription_expiry_soon"] = 0.6
                elif days_until_expiry <= 14: risk_factors_values["subscription_expiry_soon"] = 0.3
        
        # Фактор 4: Проблемы с поддержкой (заглушка)
        support_tickets = user_data.get("support_tickets_last_30_days", 0)
        if support_tickets > 1: risk_factors_values["support_issues_high"] = min(0.7, support_tickets * 0.3)
        
        # Фактор 5: Проблемы с платежами (заглушка)
        failed_payments = user_data.get("failed_payments_last_30_days", 0)
        if failed_payments > 0: risk_factors_values["payment_issues_exist"] = min(0.8, failed_payments * 0.5)

        # Фактор 6: Низкое взаимодействие с ключевыми функциями (заглушка)
        feature_usage = user_data.get("feature_usage_last_30d", {})
        # Пример: если есть фича 'story_creation_count' и 'memory_saves'
        key_features_used_count = feature_usage.get("story_creation_count",0) + feature_usage.get("memory_saves",0)
        if key_features_used_count < 3 and active_days_30d > 5 : # Если был активен, но мало использовал фичи
            risk_factors_values["low_feature_interaction"] = 0.4


        # Взвешенный расчет общего риска
        weights = {
            "usage_decline": 0.30, "low_engagement_sessions": 0.15, "low_engagement_activity_days": 0.15,
            "subscription_expiry_soon": 0.20, "support_issues_high": 0.05, "payment_issues_exist": 0.10,
            "low_feature_interaction": 0.05
        }
        total_risk = sum(risk_factors_values[factor] * weight for factor, weight in weights.items())
        return min(1.0, total_risk + 0.05) # +0.05 базовый небольшой риск для всех платных

    async def _identify_risk_factors_from_db_data(self, user_data: Dict[str, Any]) -> List[str]:
        """Определение текстовых факторов риска на основе данных из БД."""
        factors = []
        if user_data.get("messages_last_7_days", 100) < user_data.get("messages_previous_7_days", 0) * 0.6 : factors.append("Снижение кол-ва сообщений")
        if user_data.get("avg_session_duration_minutes_last_30d", 10) < 3.0: factors.append("Короткие сессии")
        if user_data.get("active_days_last_N_days", 15) < 7: factors.append("Мало активных дней")
        
        days_to_exp = user_data.get("days_until_expiry")
        if days_to_exp is not None and user_data.get("current_tier") != SubscriptionTier.FREE.value and not user_data.get("is_trial", False):
            if days_to_exp <= 7: factors.append(f"Подписка истекает через {days_to_exp} дн.")
        
        if user_data.get("support_tickets_last_30_days", 0) > 1 : factors.append("Частые обращения в поддержку")
        if user_data.get("failed_payments_last_30_days", 0) > 0 : factors.append("Были проблемы с оплатой")
        
        feature_usage = user_data.get("feature_usage_last_30d", {})
        key_features_used_count = feature_usage.get("story_creation_count",0) + feature_usage.get("memory_saves",0)
        if key_features_used_count < 2 and user_data.get("active_days_last_N_days", 0) > 3 :
            factors.append("Низкое использование ключевых функций")

        return factors if factors else ["Общее снижение вовлеченности или неясные факторы"]


    def _get_retention_recommendation_stub(self, risk_score: float) -> str:
        """Заглушка для рекомендаций по удержанию из roadmap_qa_document.md."""
        if risk_score >= 0.8: return "Срочное вмешательство (персональное предложение, скидка на продление)"
        elif risk_score >= 0.7: return "Проактивный контакт (опрос о причинах, предложение помощи)"
        elif risk_score >= 0.6: return "Кампания по вовлечению (новые фичи, бонусы за активность)"
        else: return "Продолжать мониторинг"

    # async def predict_revenue(self, days_ahead: int) -> Dict[str, Any]: ... (Заглушка)
    # async def segment_users(self) -> Dict[str, List[int]]: ... (Заглушка) - это теперь в UserSegmentationEngine
