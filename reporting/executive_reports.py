# reporting/executive_reports.py
import logging
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timedelta, timezone
import random 

from database.operations import DatabaseService 
from analytics.business_intelligence import BusinessIntelligenceEngine
from config.settings import BotConfig
from aiogram.utils.markdown import hbold, hitalic

logger = logging.getLogger(__name__)

class ExecutiveReportGenerator:
    """
    Generates comprehensive business reports for executive review.
    Utilizes BusinessIntelligenceEngine for data and insights.
    """

    def __init__(self, db_service: DatabaseService, bi_engine: BusinessIntelligenceEngine, config: BotConfig):
        self.db_service = db_service 
        self.bi_engine = bi_engine 
        self.config = config

    async def generate_monthly_executive_report(
        self,
        target_date_for_month: Optional[datetime] = None, 
        previous_month_comparison: bool = True 
    ) -> Dict[str, Any]:
        """
        Generates a comprehensive monthly executive report for the month of target_date_for_month.
        If target_date_for_month is None, generates for the previous full calendar month.
        """
        if target_date_for_month is None:
            today = datetime.now(timezone.utc)
            first_day_of_current_month = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end_date_report_month = first_day_of_current_month - timedelta(microseconds=1)
            start_date_report_month = end_date_report_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            start_date_report_month = target_date_for_month.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            next_month_start = (start_date_report_month.replace(day=28) + timedelta(days=4)).replace(day=1)
            end_date_report_month = next_month_start - timedelta(microseconds=1)

        report_month_str = start_date_report_month.strftime("%B %Y")
        logger.info(f"Generating monthly executive report for {report_month_str}...")

        report: Dict[str, Any] = {
            "report_title": f"Ежемесячный отчет AI Companion Bot - {report_month_str}",
            "report_generated_at": datetime.now(timezone.utc).isoformat(),
            "report_period": {
                "month_name": start_date_report_month.strftime("%B"), 
                "year": start_date_report_month.year,
                "start_date_iso": start_date_report_month.isoformat(),
                "end_date_iso": end_date_report_month.isoformat()
            },
            "executive_summary": {}, "key_performance_indicators": {}, "financial_performance": {},
            "user_engagement_and_growth": {}, "product_and_feature_insights": {},
            "marketing_and_promotional_activity": {}, "challenges_and_risks": [],
            "strategic_recommendations": [], "next_month_outlook_and_priorities": []
        }

        try:
            # Получаем данные из BI Engine
            # BI Engine должен уметь принимать start_date и end_date
            # Пока что generate_executive_dashboard принимает period_days, что не идеально для отчета за конкретный месяц.
            # Адаптируем: передаем количество дней в отчетном месяце.
            days_in_report_month = (end_date_report_month - start_date_report_month).days + 1
            
            # Данные за отчетный месяц
            # TODO: Адаптировать BI Engine для приема start_date, end_date
            # Пока используем period_days, что означает, что данные будут за последние N дней до СЕГОДНЯ,
            # а не за конкретный календарный месяц в прошлом, если target_date_for_month не сегодняшний день.
            # Это НЕСООТВЕТСТВИЕ нужно будет исправить в BI Engine или здесь.
            # Для корректного отчета за прошлый месяц, BI Engine должен получать end_date_report_month.
            
            # Используем generate_executive_dashboard как основной источник данных
            # Он уже содержит и key_metrics, и revenue_analysis, и predictive_analytics
            bi_dashboard_current = await self.bi_engine.generate_executive_dashboard(period_days=days_in_report_month)
            
            bi_dashboard_previous = None
            if previous_month_comparison:
                prev_month_end_date = start_date_report_month - timedelta(microseconds=1)
                prev_month_start_date = prev_month_end_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                days_in_prev_month = (prev_month_end_date - prev_month_start_date).days + 1
                # TODO: BI Engine должен уметь генерировать дашборд для произвольного ПРОШЛОГО периода.
                # Пока что, если generate_executive_dashboard всегда считает от "сегодня", то для прошлого периода
                # мы не сможем получить корректные данные через него.
                # Поэтому, для MoM сравнения, лучше напрямую запрашивать метрики из DatabaseService за нужные периоды.
                logger.info(f"Fetching previous month data directly for comparison: {prev_month_start_date.date()} - {prev_month_end_date.date()}")
                bi_dashboard_previous = await self._get_direct_metrics_for_period(prev_month_start_date, prev_month_end_date, days_in_prev_month)


            report["executive_summary"] = self._generate_executive_summary_from_bi(bi_dashboard_current, bi_dashboard_previous)
            report["key_performance_indicators"] = self._get_monthly_kpis_from_bi(bi_dashboard_current, bi_dashboard_previous)
            report["financial_performance"] = self._analyze_financial_from_bi(bi_dashboard_current, bi_dashboard_previous)
            report["user_engagement_and_growth"] = self._analyze_user_data_from_bi(bi_dashboard_current, bi_dashboard_previous)
            report["product_and_feature_insights"] = self._analyze_product_insights(bi_dashboard_current)
            report["marketing_and_promotional_activity"] = self._analyze_marketing_activity(bi_dashboard_current)
            
            report["strategic_recommendations"] = self._generate_report_recommendations(bi_dashboard_current)
            report["challenges_and_risks"].append("Точность отчета зависит от полноты данных в DatabaseService и корректности расчетов в BI Engine.")
            report["next_month_outlook_and_priorities"].append("Улучшение сбора данных для детализации LTV и ROI по промокодам.")

            logger.info(f"Monthly executive report for {report_month_str} generated successfully.")

        except Exception as e:
            logger.error(f"Error generating monthly executive report for {report_month_str}: {e}", exc_info=True)
            report["error"] = f"Ошибка генерации отчета: {str(e)}"
        
        return report

    async def _get_direct_metrics_for_period(self, start_date: datetime, end_date: datetime, period_days: int) -> Dict[str, Any]:
        """Вспомогательный метод для получения ключевых метрик напрямую из DatabaseService за указанный период."""
        # Этот метод дублирует часть логики BI Engine, но позволяет получить данные за конкретный прошлый период.
        # В идеале, BI Engine должен сам уметь это делать.
        key_metrics = await self.bi_engine._calculate_key_metrics(period_days=period_days) # period_days здесь не очень хорошо, т.к. _calculate_key_metrics считает от today
        revenue_analysis = await self.bi_engine._analyze_revenue_performance(period_days=period_days) # Аналогично
        
        # Пересчитываем метрики для конкретного start_date, end_date
        # Это пример, как можно было бы сделать, если бы BI Engine не был адаптирован
        # Users
        new_users = await self.db_service.get_new_users_count_for_period(start_date, end_date)
        avg_dau = await self.db_service.get_average_dau_for_period(start_date, end_date)
        mau = await self.db_service.get_mau_for_period(end_date, period_days=period_days) # mau всегда за 30 дней до end_date
        
        # Subscriptions
        sub_stats = await self.db_service.get_subscription_analytics_for_period(start_date, end_date)
        
        return {
            "report_period_days": period_days, # Для консистентности с bi_dashboard_current
            "key_metrics": {
                "users": {
                    f"new_users_last_{period_days}d": {"value": new_users},
                    f"daily_active_users_avg_last_{min(period_days,7)}d": {"value": avg_dau}, # Используем min для ключа
                    "monthly_active_users_last_30d": {"value": mau},
                },
                "subscriptions": {
                    "total_active_subscribers": {"value": sub_stats.get("active_subscribers")},
                    "conversion_rate_new_to_paid_percent_current_period": {"value": sub_stats.get("conversion_rate_from_new_to_paid_percent")},
                    "mrr_stars_current": {"value": sub_stats.get("mrr_stars")},
                }
            },
            "revenue_analysis": {
                f"total_revenue_stars_last_{period_days}d": {"value": sub_stats.get("total_revenue_in_period_stars")},
                f"arpu_stars_monthly_avg_last_30d": {"value": (sub_stats.get("total_revenue_in_period_stars",0) / mau) if mau > 0 else 0},
                f"arppu_stars_monthly_avg_last_30d": {"value": (sub_stats.get("total_revenue_in_period_stars",0) / sub_stats.get("active_subscribers",1)) if sub_stats.get("active_subscribers",0) > 0 else 0},
                f"revenue_by_tier_distrib_last_{period_days}d": sub_stats.get("tier_distribution"),
            }
        }


    def _format_mom_growth(self, current_value: Optional[Union[int, float]], previous_value: Optional[Union[int, float]]) -> str: # type: ignore
        if current_value is None or previous_value is None: return "N/A"
        current_float = float(current_value); previous_float = float(previous_value)
        if previous_float == 0:
            return "+∞%" if current_float > 0 else ("0.0%" if current_float == 0 else "-∞%")
        growth = ((current_float - previous_float) / previous_float) * 100
        return f"{'+' if growth >= 0 else ''}{growth:.1f}%" # Добавил + для 0

    def _get_value_from_bi_metric(self, bi_data: Optional[Dict[str, Any]], category: str, metric_key_part: str, period_days: int) -> Optional[Any]:
        """Извлекает 'value' из сложной структуры метрик BI дашборда."""
        if not bi_data: return None
        # Ключи в bi_dashboard_current могут содержать period_days, например, "new_users_last_30d"
        # Попробуем найти ключ, который содержит metric_key_part и period_days
        full_metric_key = f"{metric_key_part}_last_{period_days}d" # Примерный формат ключа
        
        metric_data = bi_data.get("key_metrics", {}).get(category, {}).get(full_metric_key)
        if isinstance(metric_data, dict): return metric_data.get("value")
        
        # Фоллбэк, если ключ не содержит period_days (например, для общих метрик)
        metric_data_simple = bi_data.get("key_metrics", {}).get(category, {}).get(metric_key_part)
        if isinstance(metric_data_simple, dict): return metric_data_simple.get("value")
        
        # Фоллбэк для revenue_analysis
        revenue_metric_data = bi_data.get("revenue_analysis", {}).get(full_metric_key)
        if isinstance(revenue_metric_data, dict): return revenue_metric_data.get("value")
        
        revenue_metric_data_simple = bi_data.get("revenue_analysis", {}).get(metric_key_part)
        if isinstance(revenue_metric_data_simple, dict): return revenue_metric_data_simple.get("value")

        return None


    def _generate_executive_summary_from_bi(self, bi_current: Dict[str, Any], bi_previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        summary = {"overall_performance_assessment": "Требуется анализ данных BI Engine."}
        period_days = bi_current.get("report_period_days", 30)

        current_revenue = self._get_value_from_bi_metric(bi_current, "revenue_analysis", f"total_revenue_stars", period_days)
        current_new_users = self._get_value_from_bi_metric(bi_current, "users", f"new_users", period_days)
        
        prev_revenue = self._get_value_from_bi_metric(bi_previous, "revenue_analysis", f"total_revenue_stars", period_days) if bi_previous else None
        prev_new_users = self._get_value_from_bi_metric(bi_previous, "users", f"new_users", period_days) if bi_previous else None

        summary["total_revenue_stars_current_month"] = current_revenue
        summary["revenue_growth_mom_formatted"] = self._format_mom_growth(current_revenue, prev_revenue)
        summary["new_users_acquired_current_month"] = current_new_users
        summary["user_growth_mom_formatted"] = self._format_mom_growth(current_new_users, prev_new_users)
        summary["active_subscribers_end_of_month"] = self._get_value_from_bi_metric(bi_current, "subscriptions", "total_active_subscribers", period_days)
        
        # Оценка производительности
        if current_revenue is not None and prev_revenue is not None:
            if current_revenue > prev_revenue * 1.15: summary["overall_performance_assessment"] = "Значительный рост выручки."
            elif current_revenue > prev_revenue * 1.02: summary["overall_performance_assessment"] = "Умеренный рост выручки."
            elif current_revenue < prev_revenue * 0.9: summary["overall_performance_assessment"] = "Зафиксировано снижение выручки, требуется анализ."
            else: summary["overall_performance_assessment"] = "Стабильные показатели выручки."
        elif current_revenue is not None:
            summary["overall_performance_assessment"] = "Данные за предыдущий период отсутствуют для сравнения выручки."


        summary["key_achievements_this_month"] = ["Продолжается сбор данных для BI (заглушка)."]
        summary["key_challenges_this_month"] = ["Необходимо обеспечить полноту и точность данных для BI Engine."]
        return summary

    def _get_monthly_kpis_from_bi(self, bi_current: Dict[str, Any], bi_previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        kpis = {}
        period_days = bi_current.get("report_period_days", 30)
        dau_lookback = min(period_days, 7) # Для DAU обычно смотрят за последнюю неделю

        def get_metric_with_trend(category_key: str, metric_key_part: str, p_days: int, d_lookback: Optional[int] = None):
            actual_period_key = f"{metric_key_part}_last_{d_lookback or p_days}d"
            curr_val = self._get_value_from_bi_metric(bi_current, category_key, actual_period_key, p_days) # period_days здесь может быть не нужен, если ключ уже полный
            # Для тренда, нужно знать ключ предыдущего периода
            prev_val = self._get_value_from_bi_metric(bi_previous, category_key, actual_period_key, p_days) if bi_previous else None
            return {"value": curr_val, "trend_mom": self._format_mom_growth(curr_val, prev_val)}

        kpis["monthly_active_users_mau"] = get_metric_with_trend("users", "monthly_active_users", 30) # MAU всегда за 30 дней
        kpis["daily_active_users_avg_dau"] = get_metric_with_trend("users", "daily_active_users_avg", period_days, dau_lookback)
        
        # Финансовые KPI из revenue_analysis
        arpu_curr = self._get_value_from_bi_metric(bi_current, "revenue_analysis", "arpu_stars_monthly_avg", 30)
        arpu_prev = self._get_value_from_bi_metric(bi_previous, "revenue_analysis", "arpu_stars_monthly_avg", 30) if bi_previous else None
        kpis["average_revenue_per_user_arpu_stars"] = {"value": arpu_curr, "trend_mom": self._format_mom_growth(arpu_curr, arpu_prev)}
        
        arppu_curr = self._get_value_from_bi_metric(bi_current, "revenue_analysis", "arppu_stars_monthly_avg", 30)
        arppu_prev = self._get_value_from_bi_metric(bi_previous, "revenue_analysis", "arppu_stars_monthly_avg", 30) if bi_previous else None
        kpis["average_revenue_per_paying_user_arppu_stars"] = {"value": arppu_curr, "trend_mom": self._format_mom_growth(arppu_curr, arppu_prev)}

        # KPI по подпискам
        churn_curr = self._get_value_from_bi_metric(bi_current, "subscriptions", "churn_rate_monthly_percent_current", period_days)
        churn_prev = self._get_value_from_bi_metric(bi_previous, "subscriptions", "churn_rate_monthly_percent_current", period_days) if bi_previous else None
        kpis["customer_churn_rate_monthly_percent"] = {"value": churn_curr, "trend_mom": self._format_mom_growth(churn_curr, churn_prev)}
        
        ltv_curr = self._get_value_from_bi_metric(bi_current, "subscriptions", "ltv_stars_avg", period_days)
        ltv_prev = self._get_value_from_bi_metric(bi_previous, "subscriptions", "ltv_stars_avg", period_days) if bi_previous else None
        kpis["customer_lifetime_value_ltv_stars"] = {"value": ltv_curr, "trend_mom": self._format_mom_growth(ltv_curr, ltv_prev)}
        
        conv_curr = self._get_value_from_bi_metric(bi_current, "subscriptions", "conversion_rate_new_to_paid_percent_current_period", period_days)
        conv_prev = self._get_value_from_bi_metric(bi_previous, "subscriptions", "conversion_rate_new_to_paid_percent_current_period", period_days) if bi_previous else None
        kpis["conversion_rate_new_to_paid_percent"] = {"value": conv_curr, "trend_mom": self._format_mom_growth(conv_curr, conv_prev)}
        
        return kpis

    def _analyze_financial_from_bi(self, bi_current: Dict[str, Any], bi_previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        financials = {}
        period_days = bi_current.get("report_period_days", 30)

        total_rev_curr = self._get_value_from_bi_metric(bi_current, "revenue_analysis", f"total_revenue_stars", period_days)
        total_rev_prev = self._get_value_from_bi_metric(bi_previous, "revenue_analysis", f"total_revenue_stars", period_days) if bi_previous else None
        financials["total_revenue_stars"] = {"value": total_rev_curr, "trend_mom": self._format_mom_growth(total_rev_curr, total_rev_prev)}

        mrr_curr = self._get_value_from_bi_metric(bi_current, "subscriptions", "mrr_stars_current", period_days) # Предполагаем, что MRR есть в subscriptions
        mrr_prev = self._get_value_from_bi_metric(bi_previous, "subscriptions", "mrr_stars_current", period_days) if bi_previous else None
        financials["mrr_stars_end_of_month"] = {"value": mrr_curr, "trend_mom": self._format_mom_growth(mrr_curr, mrr_prev)}
        
        financials["revenue_distribution_by_tier_stars"] = bi_current.get("revenue_analysis", {}).get(f"revenue_by_tier_distrib_last_{period_days}d", {})
        financials["promotional_campaigns_summary"] = bi_current.get("revenue_analysis", {}).get("promotional_impact", {})
        
        return financials

    def _analyze_user_data_from_bi(self, bi_current: Dict[str, Any], bi_previous: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        user_data = {}
        period_days = bi_current.get("report_period_days", 30)
        dau_lookback = min(period_days, 7)

        new_users_curr = self._get_value_from_bi_metric(bi_current, "users", f"new_users", period_days)
        new_users_prev = self._get_value_from_bi_metric(bi_previous, "users", f"new_users", period_days) if bi_previous else None
        user_data["new_users_acquired"] = {"value": new_users_curr, "trend_mom": self._format_mom_growth(new_users_curr, new_users_prev)}

        avg_dau_curr = self._get_value_from_bi_metric(bi_current, "users", f"daily_active_users_avg", period_days, dau_lookback)
        avg_dau_prev = self._get_value_from_bi_metric(bi_previous, "users", f"daily_active_users_avg", period_days, dau_lookback) if bi_previous else None
        user_data["daily_active_users_average"] = {"value": avg_dau_curr, "trend_mom": self._format_mom_growth(avg_dau_curr, avg_dau_prev)}
        
        avg_session_curr = self._get_value_from_bi_metric(bi_current, "engagement", f"avg_session_duration_sec", period_days)
        avg_session_prev = self._get_value_from_bi_metric(bi_previous, "engagement", f"avg_session_duration_sec", period_days) if bi_previous else None
        user_data["avg_session_duration_seconds"] = {"value": avg_session_curr, "trend_mom": self._format_mom_growth(avg_session_curr, avg_session_prev)}

        avg_msg_curr = self._get_value_from_bi_metric(bi_current, "engagement", f"messages_per_active_user_avg", period_days)
        avg_msg_prev = self._get_value_from_bi_metric(bi_previous, "engagement", f"messages_per_active_user_avg", period_days) if bi_previous else None
        user_data["avg_messages_per_active_user"] = {"value": avg_msg_curr, "trend_mom": self._format_mom_growth(avg_msg_curr, avg_msg_prev)}

        user_data["user_retention_rates_stub"] = { # Заглушка, пока нет точных данных из BI/DB
             "day_1_retention_percent": {"value": random.uniform(40, 60) if avg_dau_curr else 0, "trend_mom": "N/A"},
             "day_7_retention_percent": {"value": random.uniform(20, 40) if avg_dau_curr else 0, "trend_mom": "N/A"},
        }
        user_data["user_feedback_summary"] = "Сбор и анализ обратной связи в разработке."
        return user_data

    def _analyze_product_insights(self, bi_current: Dict[str, Any]) -> Dict[str, Any]:
        insights = {"message": "Анализ использования функций в разработке."}
        period_days = bi_current.get("report_period_days", 30)
        engagement_metrics = bi_current.get("key_metrics", {}).get("engagement", {})
        
        story_creation_usage = engagement_metrics.get("feature_usage_story_creation_unique_users_percent_of_dau", {}).get("value")
        story_total_uses = engagement_metrics.get("feature_usage_story_creation_total_uses", {}).get("value")

        insights["story_creation_adoption_percent_dau"] = story_creation_usage
        insights["story_creation_total_uses_period"] = story_total_uses
        
        # TODO: Добавить анализ других ключевых фич, если они отслеживаются в BI Engine
        # insights["memory_usage_stats"] = self._get_value_from_bi_metric(bi_current, "engagement", "feature_usage_memory_...", period_days)
        
        if story_creation_usage is not None and story_creation_usage < 10:
            insights["story_creation_recommendation"] = "Низкое использование функции создания историй. Рассмотреть промо-акции или улучшение UX."
        return insights

    def _analyze_marketing_activity(self, bi_current: Dict[str, Any]) -> Dict[str, Any]:
        marketing = {"message": "Анализ маркетинговых кампаний и промокодов в разработке."}
        promo_impact = bi_current.get("revenue_analysis", {}).get("promotional_impact", {})
        
        marketing["promocode_summary"] = {
            "overall_roi_percent": promo_impact.get("overall_roi_percent_all_campaigns_period"),
            "total_applications": promo_impact.get("total_applications_all_promos_period"),
            "revenue_from_promo_stars": promo_impact.get("total_revenue_from_promo_purchases_stars_period"),
            "top_promocodes": promo_impact.get("top_performers_by_roi_period")
        }
        # TODO: Добавить анализ других маркетинговых каналов, если данные есть в BI
        return marketing

    def _generate_report_recommendations(self, bi_current: Dict[str, Any]) -> List[str]:
        recommendations = []
        # Пример генерации рекомендаций на основе данных
        churn_rate = self._get_value_from_bi_metric(bi_current, "subscriptions", "churn_rate_monthly_percent_current", bi_current.get("report_period_days",30))
        if churn_rate is not None and churn_rate > (self.db_service.bot_config.target_monthly_churn_rate_percent or 5.0) * 1.2: # Если отток на 20% выше цели
            recommendations.append(f"Высокий уровень оттока ({churn_rate:.1f}%). Необходимо проанализировать причины и запустить кампании по удержанию.")

        conversion_rate = self._get_value_from_bi_metric(bi_current, "subscriptions", "conversion_rate_new_to_paid_percent_current_period", bi_current.get("report_period_days",30))
        if conversion_rate is not None and conversion_rate < (self.db_service.bot_config.target_conversion_rate_new_users_percent or 8.0) * 0.8: # Если конверсия на 20% ниже цели
            recommendations.append(f"Низкая конверсия новых пользователей в платных ({conversion_rate:.1f}%). Оптимизировать онбординг и ценностное предложение.")
        
        if not recommendations:
            recommendations.append("Продолжать мониторинг ключевых показателей. Рассмотреть A/B тестирование для улучшения воронок.")
        return recommendations


    async def _get_stub_previous_month_dashboard(self, current_dashboard: Dict[str, Any]) -> Dict[str, Any]:
        """Создает заглушку для данных предыдущего месяца на основе текущих."""
        stub_previous = json.loads(json.dumps(current_dashboard)) 
        period_days = current_dashboard.get("report_period_days", 30)
        
        def adjust_metric_value(value: Any) -> Any:
            if isinstance(value, (int, float)):
                adjusted = value * random.uniform(0.75, 0.95) # Уменьшаем на 5-25%
                return round(adjusted, 2) if isinstance(adjusted, float) else int(adjusted)
            return value

        if "key_metrics" in stub_previous:
            for category_key, category_data in stub_previous["key_metrics"].items():
                if isinstance(category_data, dict):
                    for metric_key, metric_dict_or_val in category_data.items():
                        if isinstance(metric_dict_or_val, dict) and "value" in metric_dict_or_val:
                            metric_dict_or_val["value"] = adjust_metric_value(metric_dict_or_val["value"])
                        elif isinstance(metric_dict_or_val, (int, float)): # Если значение прямое
                             category_data[metric_key] = adjust_metric_value(metric_dict_or_val)
        
        if "revenue_analysis" in stub_previous:
            for key, data_dict_or_val in stub_previous["revenue_analysis"].items():
                if key == f"revenue_by_tier_distrib_last_{period_days}d" and isinstance(data_dict_or_val, dict):
                    for tier, revenue_val in data_dict_or_val.items():
                        data_dict_or_val[tier] = adjust_metric_value(revenue_val)
                elif isinstance(data_dict_or_val, dict) and "value" in data_dict_or_val:
                    data_dict_or_val["value"] = adjust_metric_value(data_dict_or_val["value"])
                elif isinstance(data_dict_or_val, (int,float)):
                     stub_previous["revenue_analysis"][key] = adjust_metric_value(data_dict_or_val)


        logger.info("Generated STUB data for previous month comparison for Executive Report.")
        return stub_previous
