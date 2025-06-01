# optimization/business_optimizer.py
import logging
from typing import Dict, Any, List, Optional

from database.operations import DatabaseService
from analytics.business_intelligence import BusinessIntelligenceEngine
from analytics.ml_predictor import MLPredictor # Может понадобиться для рекомендаций на основе прогнозов
from analytics.user_segmentation import UserSegmentationEngine # Для рекомендаций по сегментам
from config.settings import BotConfig

logger = logging.getLogger(__name__)

class BusinessOptimizationEngine:
    """
    Generates actionable business optimization recommendations based on analytics data,
    predictions, and user segments.
    """

    def __init__(self,
                 db_service: DatabaseService,
                 bi_engine: BusinessIntelligenceEngine,
                 ml_predictor: MLPredictor,
                 user_segmentation_engine: UserSegmentationEngine,
                 config: BotConfig):
        self.db_service = db_service
        self.bi_engine = bi_engine
        self.ml_predictor = ml_predictor
        self.user_segmentation_engine = user_segmentation_engine
        self.config = config

    async def generate_all_strategic_recommendations(self,
                                                     period_days_for_dashboard: int = 30
                                                     ) -> List[Dict[str, Any]]:
        """
        Generates a comprehensive list of strategic recommendations by fetching
        all necessary analytical data.
        """
        logger.info("Generating all strategic business optimization recommendations...")

        # 1. Получаем данные из BI дашборда
        analytics_dashboard_data = await self.bi_engine.generate_executive_dashboard(
            period_days=period_days_for_dashboard
        )
        if analytics_dashboard_data.get("error"):
            logger.error(f"Failed to fetch analytics dashboard data for recommendations: {analytics_dashboard_data.get('error')}")
            return [{
                "category": "System Error",
                "title": "Ошибка получения данных для генерации рекомендаций",
                "description": "Не удалось загрузить аналитические данные из BI Engine.",
                "priority_score": 10, "expected_impact_score": 0,
                "action_items": ["Проверить работоспособность BusinessIntelligenceEngine и DatabaseService."],
                "success_metrics": ["N/A"]
            }]

        # 2. Получаем прогнозы (если они еще не в дашборде или нужны самые свежие)
        # predictive_analytics = await self.bi_engine._generate_predictions() # Используем метод из BI Engine
        predictive_analytics = analytics_dashboard_data.get("predictive_analytics", {})


        # 3. Получаем сегменты пользователей
        user_segments = await self.user_segmentation_engine.segment_all_users()

        recommendations: List[Dict[str, Any]] = []

        key_metrics = analytics_dashboard_data.get("key_metrics", {})
        revenue_analysis = analytics_dashboard_data.get("revenue_analysis", {})

        # Генерация рекомендаций по разным направлениям
        recommendations.extend(self._analyze_revenue_opportunities(key_metrics, revenue_analysis))
        recommendations.extend(self._analyze_acquisition_opportunities(key_metrics, predictive_analytics))
        recommendations.extend(self._analyze_retention_opportunities(key_metrics, predictive_analytics, user_segments))
        recommendations.extend(self._analyze_product_opportunities(key_metrics, user_segments))
        recommendations.extend(self._analyze_promotional_campaign_effectiveness(revenue_analysis))

        # Сортировка
        recommendations.sort(
            key=lambda x: (x.get("priority_score", 0), x.get("expected_impact_score", 0)),
            reverse=True
        )

        logger.info(f"Generated {len(recommendations)} strategic recommendations in total.")
        return recommendations[:10] # Возвращаем топ-N рекомендаций

    def _analyze_revenue_opportunities(self, key_metrics: Dict[str, Any], revenue_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []
        subscriptions_metrics = key_metrics.get("subscriptions", {})
        
        conversion_rate_key = "conversion_rate_new_to_paid_percent_current_period" # Ключ из BI Engine
        conversion_rate = subscriptions_metrics.get(conversion_rate_key)
        target_conversion_rate = getattr(self.config, 'target_conversion_rate_new_users_percent', 8.0)
        
        if conversion_rate is not None and conversion_rate < target_conversion_rate * 0.75:
            recs.append({
                "category": "Оптимизация Дохода",
                "title": "Улучшить конверсию новых пользователей в платных",
                "description": f"Текущий коэффициент конверсии ({conversion_rate}%) значительно ниже целевого ({target_conversion_rate}%).",
                "priority_score": 9, "expected_impact_score": 8,
                "action_items": [
                    "Провести A/B тестирование онбординга и экранов с предложением подписки.",
                    "Проанализировать воронку от регистрации до первой покупки для выявления узких мест.",
                    "Внедрить персонализированные предложения для новых пользователей на основе их первоначальной активности.",
                    "Усилить демонстрацию ценности платных функций в первые дни использования."
                ],
                "success_metrics": [conversion_rate_key, "new_paid_subscribers_count", "first_payment_conversion_rate"]
            })

        arppu_key = next((k for k in revenue_analysis if "arppu_stars" in k and "trend" not in k), None)
        arppu = revenue_analysis.get(arppu_key) if arppu_key else None
        target_arppu = getattr(self.config, 'target_arppu_stars', 150.0)
        if arppu is not None and arppu < target_arppu * 0.8:
            recs.append({
                "category": "Оптимизация Дохода / Ценообразование",
                "title": "Увеличить средний доход с платящего пользователя (ARPPU)",
                "description": f"Текущий ARPPU ({arppu:.2f}⭐) ниже целевого ({target_arppu:.2f}⭐).",
                "priority_score": 8, "expected_impact_score": 7,
                "action_items": [
                    "Проанализировать ценовую эластичность и рассмотреть корректировку цен.",
                    "Разработать и протестировать бандлы или доп. платные функции (add-ons).",
                    "Стимулировать переход на более дорогие тарифы (upselling) через персонализированные предложения для активных пользователей более дешевых тарифов."
                ],
                "success_metrics": ["arppu_stars", "mrr_stars", "tier_upgrade_rate", "avg_items_per_purchase"]
            })
        return recs

    def _analyze_acquisition_opportunities(self, key_metrics: Dict[str, Any], predictive_analytics: Dict[str, Any]) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []
        user_metrics = key_metrics.get("users", {})
        
        new_users_trend_key = next((k for k in user_metrics if "new_users_last_" in k and "_trend_vs_prev_period" in k), None)
        new_users_trend = user_metrics.get(new_users_trend_key) if new_users_trend_key else None

        if new_users_trend is not None and new_users_trend < 5.0:
            recs.append({
                "category": "Привлечение Пользователей",
                "title": "Стимулировать рост новых пользователей",
                "description": f"Темп прироста новых пользователей ({new_users_trend}%) ниже желаемого. Необходимо активизировать каналы привлечения.",
                "priority_score": 9, "expected_impact_score": 7,
                "action_items": [
                    "Запустить/оптимизировать рекламные кампании в Telegram Ads или других каналах.",
                    "Улучшить виральность: внедрить/улучшить реферальную программу с привлекательными бонусами.",
                    "Провести анализ источников трафика для выявления наиболее эффективных каналов.",
                    "Создать уникальный и вовлекающий контент для социальных сетей и блогов."
                ],
                "success_metrics": ["new_users_count", "cost_per_acquisition_cpa", "viral_coefficient_k_factor"]
            })
        
        user_growth_prediction = predictive_analytics.get("user_growth_next_30d", {})
        predicted_total = user_growth_prediction.get("predicted_new_users_total")
        # target_new_users_next_30d = getattr(self.config, 'target_new_users_next_30d', 500)
        # if predicted_total is not None and predicted_total < target_new_users_next_30d * 0.8:
        #     recs.append(...) # Рекомендация, если прогноз роста ниже цели
        return recs

    def _analyze_retention_opportunities(self, key_metrics: Dict[str, Any], predictive_analytics: Dict[str, Any], user_segments: Dict[str, List[int]]) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []
        subscriptions_metrics = key_metrics.get("subscriptions", {})
        
        churn_rate = subscriptions_metrics.get("churn_rate_monthly_percent_current")
        target_churn_rate = getattr(self.config, 'target_monthly_churn_rate_percent', 5.0)

        if churn_rate is not None and churn_rate > target_churn_rate * 1.1: # Если отток на 10% выше цели
            recs.append({
                "category": "Удержание Пользователей",
                "title": "Снизить ежемесячный отток платных подписчиков",
                "description": f"Текущий уровень оттока ({churn_rate}%) превышает целевой ({target_churn_rate}%).",
                "priority_score": 10, "expected_impact_score": 9,
                "action_items": [
                    "Проанализировать причины оттока (опросы ушедших, анализ поведения перед оттоком).",
                    "Разработать и запустить кампании по удержанию для сегмента 'at_risk_churn_paid'.",
                    "Улучшить онбординг для новых платных пользователей, демонстрируя ключевые ценности.",
                    "Внедрить систему сбора обратной связи для раннего выявления проблем."
                ],
                "success_metrics": ["churn_rate_monthly_percent", "customer_lifetime_value_ltv_stars", "active_subscribers_count"]
            })

        at_risk_users_count = predictive_analytics.get("churn_risk_high_users_count", 0)
        total_active_subs = subscriptions_metrics.get("total_active_subscribers", 0)
        if total_active_subs > 0 and (at_risk_users_count / total_active_subs) > 0.1: # Если более 10% подписчиков в зоне риска
            recs.append({
                "category": "Удержание Пользователей (Проактивное)",
                "title": "Провести проактивную работу с пользователями из группы риска оттока",
                "description": f"{at_risk_users_count} пользователей ({ (at_risk_users_count / total_active_subs * 100) if total_active_subs else 0:.1f}%) имеют высокий риск оттока.",
                "priority_score": 9, "expected_impact_score": 8,
                "action_items": [
                    "Сегментировать пользователей из группы риска по причинам (если MLPredictor их дает).",
                    "Запустить персонализированные email/push кампании с предложениями помощи или бонусами.",
                    "Предложить временные скидки на продление или апгрейд.",
                    "Собрать обратную связь от этой группы для понимания их болевых точек."
                ],
                "success_metrics": ["churn_rate_among_at_risk_segment", "retention_rate_of_targeted_users"]
            })
        return recs

    def _analyze_product_opportunities(self, key_metrics: Dict[str, Any], user_segments: Dict[str, List[int]]) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []
        engagement_metrics = key_metrics.get("engagement", {})

        story_creation_usage_key = "feature_usage_story_creation_unique_users_percent_of_dau"
        story_creation_usage = engagement_metrics.get(story_creation_usage_key)
        if story_creation_usage is not None and story_creation_usage < 15.0:
            recs.append({
                "category": "Оптимизация Продукта",
                "title": "Повысить использование функции создания историй",
                "description": f"Только {story_creation_usage}% DAU используют создание историй. Необходимо повысить вовлеченность.",
                "priority_score": 7, "expected_impact_score": 6,
                "action_items": [
                    "Провести опрос пользователей о причинах низкого использования.",
                    "Улучшить UX/UI функции, сделать ее более заметной и интуитивно понятной.",
                    "Добавить обучающие подсказки или примеры использования.",
                    "Рассмотреть возможность геймификации или вознаграждений за создание историй."
                ],
                "success_metrics": [story_creation_usage_key, "stories_created_per_active_user"]
            })
        
        avg_session_key = next((k for k in engagement_metrics if "avg_session_duration_sec" in k and "trend" not in k), None)
        avg_session_duration_sec = engagement_metrics.get(avg_session_key) if avg_session_key else None
        if avg_session_duration_sec is not None and avg_session_duration_sec < 300 : # Менее 5 минут
             recs.append({
                "category": "Вовлеченность в Продукт",
                "title": "Увеличить среднюю продолжительность сессии",
                "description": f"Средняя сессия ({avg_session_duration_sec/60:.1f} мин) короче желаемой. Это может указывать на проблемы с удержанием внимания.",
                "priority_score": 7, "expected_impact_score": 7,
                "action_items": [
                    "Проанализировать точки выхода пользователей из бота.",
                    "Добавить более интерактивный или персонализированный контент в начале сессии.",
                    "Улучшить скорость ответа и навигацию по основным функциям.",
                    "Внедрить элементы геймификации или цепочки задач для удержания."
                ],
                "success_metrics": ["avg_session_duration_sec", "user_retention_rate_day_N", "session_depth"]
            })
        return recs

    def _analyze_promotional_campaign_effectiveness(self, revenue_analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
        recs: List[Dict[str, Any]] = []
        promo_impact = revenue_analysis.get("promotional_impact", {})
        overall_roi = promo_impact.get("overall_roi_percent_all_campaigns")

        if overall_roi is not None and overall_roi < 100.0 and overall_roi != float('inf'): # Если ROI меньше 100% (т.е. не окупаются)
            recs.append({
                "category": "Маркетинг / Промоакции",
                "title": "Повысить эффективность промо-кампаний (ROI)",
                "description": f"Общий ROI промо-кампаний ({overall_roi}%) ниже точки окупаемости. Необходимо пересмотреть условия или таргетинг.",
                "priority_score": 8, "expected_impact_score": 7,
                "action_items": [
                    "Проанализировать ROI по каждой отдельной промо-кампании/промокоду.",
                    "Отключить или изменить условия для низкоэффективных промокодов.",
                    "Усилить таргетинг промокодов на более конверсионные сегменты пользователей.",
                    "Протестировать различные типы скидок и бонусов."
                ],
                "success_metrics": ["overall_roi_percent_all_campaigns", "revenue_per_promo_user", "cost_per_promo_acquisition"]
            })
        return recs
