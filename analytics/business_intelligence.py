# analytics/business_intelligence.py
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Union # Добавил Union

from database.operations import DatabaseService 
from .ml_predictor import MLPredictor 
# from .user_segmentation import UserSegmentationEngine # Если будет использоваться для рекомендаций

logger = logging.getLogger(__name__)

class BusinessIntelligenceEngine:
    """
    Advanced analytics for business decision making.
    Provides data for dashboards and reports.
    Uses implemented methods from DatabaseService.
    """

    def __init__(self, db_service: DatabaseService, ml_predictor: Optional[MLPredictor] = None):
        self.db_service = db_service
        self.ml_predictor = ml_predictor if ml_predictor is not None else MLPredictor(self.db_service)
        # self.report_generator = ReportGenerator() # Если будет отдельный класс для генерации отчетов

    async def generate_executive_dashboard(self, period_days: int = 30) -> Dict[str, Any]:
        """
        Generates a comprehensive executive dashboard with key metrics and insights.
        Args:
            period_days (int): The number of days to look back for trend calculations (e.g., 7, 30).
        """
        logger.info(f"Generating executive dashboard for the last {period_days} days.")
        dashboard: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "report_period_days": period_days,
            "key_metrics": {},
            "revenue_analysis": {},
            "user_insights": {"message": "Анализ поведения пользователей в разработке."}, 
            "predictive_analytics": {},
            "recommendations": [{"title": "Рекомендация (заглушка)", "details": "Требуется дальнейший анализ."}] 
        }

        try:
            dashboard["key_metrics"] = await self._calculate_key_metrics(period_days)
            dashboard["revenue_analysis"] = await self._analyze_revenue_performance(period_days)
            dashboard["predictive_analytics"] = await self._generate_predictions() # period_days не используется здесь напрямую
            # dashboard["user_insights"] = await self._generate_user_insights(period_days) 
            # dashboard["recommendations"] = await self._generate_strategic_recommendations(dashboard) 

            logger.info("Executive dashboard generated successfully.")
        except Exception as e:
            logger.error(f"Error generating executive dashboard: {e}", exc_info=True)
            dashboard["error"] = str(e)
        return dashboard

    async def _calculate_trend(self, current_value: Optional[Union[int, float]], previous_value: Optional[Union[int, float]]) -> Optional[float]: # type: ignore
        """Рассчитывает процентное изменение. Допускает None."""
        if current_value is None or previous_value is None:
            return None
        current_value_float = float(current_value)
        previous_value_float = float(previous_value)

        if previous_value_float == 0:
            if current_value_float > 0: return float('inf') 
            if current_value_float == 0: return 0.0 
            return float('-inf') 
        return round(((current_value_float - previous_value_float) / previous_value_float) * 100, 2)


    async def _calculate_key_metrics(self, period_days: int) -> Dict[str, Any]:
        """Calculates core business KPIs using db_service."""
        logger.debug(f"Calculating key metrics for the last {period_days} days.")
        metrics: Dict[str, Any] = {
            "users": {}, "engagement": {}, "subscriptions": {}
        }

        now_utc = datetime.now(timezone.utc)
        end_date_current_period = now_utc
        start_date_current_period = end_date_current_period - timedelta(days=period_days)
        
        end_date_previous_period = start_date_current_period
        start_date_previous_period = end_date_previous_period - timedelta(days=period_days)

        # --- User Metrics ---
        total_active_users = await self.db_service.get_total_active_users_count()
        new_users_current = await self.db_service.get_new_users_count_for_period(start_date_current_period, end_date_current_period)
        new_users_previous = await self.db_service.get_new_users_count_for_period(start_date_previous_period, end_date_previous_period)
        
        dau_lookback = min(period_days, 7) 
        avg_dau_current = await self.db_service.get_average_dau_for_period(end_date_current_period - timedelta(days=dau_lookback-1), end_date_current_period) # -1 т.к. get_average_dau_for_period ожидает start и end
        avg_dau_previous = await self.db_service.get_average_dau_for_period(end_date_previous_period - timedelta(days=dau_lookback-1), end_date_previous_period)

        mau_current = await self.db_service.get_mau_for_period(end_date_current_period, period_days=30)
        mau_previous = await self.db_service.get_mau_for_period(end_date_previous_period, period_days=30)

        metrics["users"] = {
            "total_active_users": total_active_users,
            f"new_users_last_{period_days}d": new_users_current,
            f"new_users_last_{period_days}d_trend_vs_prev_period": await self._calculate_trend(new_users_current, new_users_previous),
            f"daily_active_users_avg_last_{dau_lookback}d": round(avg_dau_current, 2) if avg_dau_current is not None else None,
            f"daily_active_users_avg_last_{dau_lookback}d_trend_vs_prev_period": await self._calculate_trend(avg_dau_current, avg_dau_previous),
            "monthly_active_users_last_30d": mau_current,
            "monthly_active_users_last_30d_trend_vs_prev_30d": await self._calculate_trend(mau_current, mau_previous),
        }

        # --- Engagement Metrics ---
        avg_session_duration_current = await self.db_service.get_avg_session_duration_for_period(start_date_current_period, end_date_current_period)
        avg_session_duration_previous = await self.db_service.get_avg_session_duration_for_period(start_date_previous_period, end_date_previous_period)
        
        avg_msg_per_user_current = await self.db_service.get_avg_messages_per_active_user_for_period(start_date_current_period, end_date_current_period)
        avg_msg_per_user_previous = await self.db_service.get_avg_messages_per_active_user_for_period(start_date_previous_period, end_date_previous_period)
        
        feature_story_stats = await self.db_service.get_usage_count_for_feature("story_creation", start_date_current_period, end_date_current_period)
        dau_for_feature_calc = metrics["users"].get(f"daily_active_users_avg_last_{dau_lookback}d", 0) 

        metrics["engagement"] = {
            f"avg_session_duration_sec_last_{period_days}d": round(avg_session_duration_current, 1) if avg_session_duration_current is not None else None,
            f"avg_session_duration_sec_last_{period_days}d_trend_vs_prev_period": await self._calculate_trend(avg_session_duration_current, avg_session_duration_previous),
            f"messages_per_active_user_avg_last_{period_days}d": round(avg_msg_per_user_current, 1) if avg_msg_per_user_current is not None else None,
            f"messages_per_active_user_avg_last_{period_days}d_trend_vs_prev_period": await self._calculate_trend(avg_msg_per_user_current, avg_msg_per_user_previous),
            "feature_usage_story_creation_unique_users_percent_of_dau": round((feature_story_stats.get("unique_users", 0) / dau_for_feature_calc * 100) if dau_for_feature_calc and dau_for_feature_calc > 0 else 0, 1) if feature_story_stats and dau_for_feature_calc is not None else None,
            "feature_usage_story_creation_total_uses": feature_story_stats.get("total_uses", 0) if feature_story_stats else None
        }

        # --- Subscription Metrics ---
        sub_stats_current = await self.db_service.get_subscription_analytics_for_period(start_date_current_period, end_date_current_period)
        sub_stats_previous = await self.db_service.get_subscription_analytics_for_period(start_date_previous_period, end_date_previous_period)
        
        current_month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        churn_rate = await self.db_service.get_monthly_churn_rate_percent(current_month_start)
        ltv = await self.db_service.get_average_ltv_stars()

        metrics["subscriptions"] = {
            "total_active_subscribers": sub_stats_current.get("active_subscribers"),
            "total_active_subscribers_trend_vs_prev_period": await self._calculate_trend(sub_stats_current.get("active_subscribers"), sub_stats_previous.get("active_subscribers")),
            "conversion_rate_new_to_paid_percent_current_period": sub_stats_current.get("conversion_rate_from_new_to_paid_percent"),
            "churn_rate_monthly_percent_current": churn_rate, 
            "mrr_stars_current": sub_stats_current.get("mrr_stars"),
            "mrr_stars_trend_vs_prev_period": await self._calculate_trend(sub_stats_current.get("mrr_stars"), sub_stats_previous.get("mrr_stars")),
            "ltv_stars_avg": ltv, 
            "tier_distribution_active_paid": sub_stats_current.get("tier_distribution") # Уже словарь {tier_value: count}
        }
        logger.debug(f"Key metrics calculated: {metrics}")
        return metrics

    async def _analyze_revenue_performance(self, period_days: int) -> Dict[str, Any]:
        logger.debug(f"Analyzing revenue performance for the last {period_days} days.")
        end_date_current_period = datetime.now(timezone.utc)
        start_date_current_period = end_date_current_period - timedelta(days=period_days)
        end_date_previous_period = start_date_current_period
        start_date_previous_period = end_date_previous_period - timedelta(days=period_days)

        sub_analytics_current = await self.db_service.get_subscription_analytics_for_period(start_date_current_period, end_date_current_period)
        sub_analytics_previous = await self.db_service.get_subscription_analytics_for_period(start_date_previous_period, end_date_previous_period)

        total_revenue_current = sub_analytics_current.get("total_revenue_in_period_stars")
        total_revenue_previous = sub_analytics_previous.get("total_revenue_in_period_stars")
        
        mau_current = await self.db_service.get_mau_for_period(end_date_current_period, period_days=30)
        arpu_current = (total_revenue_current / mau_current) if mau_current and mau_current > 0 and total_revenue_current is not None else None
        
        active_subscribers_current = sub_analytics_current.get("active_subscribers")
        arppu_current = (total_revenue_current / active_subscribers_current) if active_subscribers_current and active_subscribers_current > 0 and total_revenue_current is not None else None

        revenue_by_tier_current = await self.db_service.get_revenue_by_tier_for_period(start_date_current_period, end_date_current_period)

        revenue_analysis = {
            f"total_revenue_stars_last_{period_days}d": total_revenue_current,
            f"total_revenue_stars_last_{period_days}d_trend_vs_prev_period": await self._calculate_trend(total_revenue_current, total_revenue_previous),
            f"arpu_stars_monthly_avg_last_30d": round(arpu_current, 2) if arpu_current is not None else None,
            f"arppu_stars_monthly_avg_last_30d": round(arppu_current, 2) if arppu_current is not None else None,
            f"revenue_by_tier_distrib_last_{period_days}d": revenue_by_tier_current, 
            "promotional_impact": await self._calculate_promocode_roi(period_days) # ROI за период
        }
        logger.debug(f"Revenue analysis complete: {revenue_analysis}")
        return revenue_analysis

    async def _calculate_promocode_roi(self, period_days: int) -> Dict[str, Any]:
        logger.debug(f"Calculating promocode ROI for the last {period_days} days.")
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=period_days)

        promocodes_used_list = await self.db_service.get_promocodes_used_in_period(start_date, end_date)
        all_promo_stats_list = []
        total_revenue_from_promo_all = 0.0
        total_discount_from_promo_all = 0.0
        total_applications_all = 0

        for code_str in promocodes_used_list:
            # get_stats_for_promocode теперь возвращает статистику за период
            stats = await self.db_service.get_stats_for_promocode(code_str, start_date, end_date)
            if stats:
                revenue_generated = float(stats.get("revenue_generated_stars_period", 0)) # Используем _period
                discount_given = float(stats.get("total_discount_stars_period", 0)) # Используем _period
                applications = int(stats.get("applications_count_period", 0)) # Используем _period
                
                total_revenue_from_promo_all += revenue_generated
                total_discount_from_promo_all += discount_given
                total_applications_all += applications

                roi = ((revenue_generated - discount_given) / discount_given * 100) if discount_given > 0 else \
                      (float('inf') if revenue_generated > 0 else 0.0) 
                all_promo_stats_list.append({
                    "code": code_str,
                    "roi_percent": round(roi, 2),
                    "applications_count_period": applications, 
                    "revenue_generated_stars_period": revenue_generated,
                    "total_discount_stars_period": discount_given
                })
        
        sorted_by_roi = sorted(all_promo_stats_list, key=lambda x: x["roi_percent"] if x["roi_percent"] != float('inf') else -1, reverse=True) 
        
        overall_roi_all_campaigns = ((total_revenue_from_promo_all - total_discount_from_promo_all) / total_discount_from_promo_all * 100) if total_discount_from_promo_all > 0 else \
                                    (float('inf') if total_revenue_from_promo_all > 0 else 0.0)

        promo_roi_data = {
            "top_performers_by_roi_period": sorted_by_roi[:3],
            "overall_roi_percent_all_campaigns_period": round(overall_roi_all_campaigns, 2),
            "total_revenue_from_promo_purchases_stars_period": round(total_revenue_from_promo_all, 2),
            "total_discounts_given_stars_period": round(total_discount_from_promo_all, 2),
            "total_applications_all_promos_period": total_applications_all,
            "total_unique_promos_used_in_period": len(promocodes_used_list)
        }
        logger.debug(f"Promocode ROI calculation complete: {promo_roi_data}")
        return promo_roi_data

    async def _generate_predictions(self) -> Dict[str, Any]: # Убрал period_days, т.к. predict_user_growth имеет свой days_ahead
        logger.debug("Generating ML predictions.")
        predictions = {
            "user_growth_next_30d": {},
            "revenue_prediction_next_30d": {"message": "Предсказание дохода в разработке."},
            "churn_risk_high_users_count": 0,
            "top_churn_risk_users_sample": []
        }
        try:
            predictions["user_growth_next_30d"] = await self.ml_predictor.predict_user_growth(days_ahead=30)
            
            high_risk_users = await self.ml_predictor.identify_churn_risk_users()
            predictions["churn_risk_high_users_count"] = len(high_risk_users)
            predictions["top_churn_risk_users_sample"] = high_risk_users[:3] # Пример топ-3
            
            logger.debug(f"ML predictions generated: {predictions}")
        except Exception as e:
            logger.error(f"Error generating ML predictions: {e}", exc_info=True)
            predictions["error_ml_predictions"] = str(e)
        return predictions
    
    # --- Заглушки для методов, которые будут реализованы позже или требуют UserSegmentationEngine ---
    async def _generate_user_insights(self, period_days: int) -> Dict[str, Any]:
        logger.warning("_generate_user_insights is a STUB and needs implementation with UserSegmentationEngine.")
        return {
            "most_active_segment": "engaged_free_users (stub)",
            "segment_engagement_trends": {"engaged_free_users": "+5% WoW (stub)"},
            "popular_features_by_segment": {"engaged_free_users": ["chatting", "story_creation_basic (stub)"]},
            "conversion_hotspots": ["newbies_active_day_3 (stub)"]
        }

    async def _generate_strategic_recommendations(self, dashboard_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        logger.warning("_generate_strategic_recommendations is a STUB.")
        recommendations = []
        # Пример:
        # if dashboard_data.get("key_metrics",{}).get("subscriptions",{}).get("churn_rate_monthly_percent_current", 100) > 10:
        #     recommendations.append({
        #         "title": "Снизить отток пользователей",
        #         "details": "Текущий уровень оттока превышает 10%. Рекомендуется запустить кампании по удержанию.",
        #         "priority": "High"
        #     })
        return recommendations


    # --- Вспомогательные методы для подсчета метрик (примеры, если они не в db_service) ---
    # Эти методы должны быть заменены вызовами к self.db_service, когда они там будут реализованы.
    # async def _count_total_users(self) -> int: return await self.db_service.get_total_active_users_count()
    # async def _count_dau(self, date: datetime.date) -> int: return await self.db_service.get_dau_for_date(date) # Пример
    # ... и так далее для других метрик, если они не напрямую из db_service.get_..._for_period
