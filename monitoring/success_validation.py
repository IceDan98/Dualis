# monitoring/success_validation.py
import logging
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timedelta, timezone
import random # Для заглушек

from database.operations import DatabaseService # Для получения значений метрик
from config.settings import BotConfig # Может понадобиться для некоторых порогов по умолчанию
from aiogram.utils.markdown import hbold # Для форматирования рекомендаций

# Для type hinting, если bot_instance передается целиком
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from main import AICompanionBot # pragma: no cover

logger = logging.getLogger(__name__)

class LaunchSuccessValidator:
    """
    Validates launch success against defined criteria for different time periods.
    Uses DatabaseService to fetch current metric values.
    """

    def __init__(self, db_service: DatabaseService, config: BotConfig):
        self.db_service = db_service
        self.config = config
        # Критерии успеха, как определено в Roadmap (Часть 4, Launch Success Validation)
        # Эти значения можно также вынести в конфигурацию, если они часто меняются
        # Добавлены 'human_name' для отображения и 'metric_getter_params' для _get_current_metric_value
        self.success_criteria: Dict[str, Dict[str, Dict[str, Any]]] = {
            "first_24_hours": {
                "system_uptime_percentage": {
                    "human_name": "Аптайм системы (%)",
                    "target": 99.9, "critical_min": 99.0, "weight": 1.5,
                    "metric_getter_params": {"period_days": 1}
                },
                "avg_response_time_ms": {
                    "human_name": "Среднее время ответа (мс)",
                    "target": 1500, "critical_max": 5000, "weight": 1.0,
                    "metric_getter_params": {"period_days": 1}
                },
                "error_rate_percentage": {
                    "human_name": "Уровень ошибок (%)",
                    "target": 0.5, "critical_max": 2.0, "weight": 1.5,
                    "metric_getter_params": {"period_days": 1}
                },
                "successful_user_registrations_count": {
                    "human_name": "Количество регистраций",
                    "target": 50, "critical_min": 10, "weight": 1.0,
                    "metric_getter_params": {"period_days": 1}
                },
                "successful_payments_count": {
                    "human_name": "Количество успешных платежей",
                    "target": 5, "critical_min": 1, "weight": 1.2,
                    "metric_getter_params": {"period_days": 1}
                }
            },
            "first_week": { # (7 дней)
                "system_uptime_percentage": {
                    "human_name": "Аптайм системы (%)",
                    "target": 99.95, "critical_min": 99.5, "weight": 1.5,
                    "metric_getter_params": {"period_days": 7}
                },
                "avg_response_time_ms": {
                    "human_name": "Среднее время ответа (мс)",
                    "target": 1000, "critical_max": 3000, "weight": 1.0,
                    "metric_getter_params": {"period_days": 7}
                },
                "error_rate_percentage": {
                    "human_name": "Уровень ошибок (%)",
                    "target": 0.2, "critical_max": 1.0, "weight": 1.5,
                    "metric_getter_params": {"period_days": 7}
                },
                "daily_active_users_avg": {
                    "human_name": "Среднее DAU",
                    "target": 100, "critical_min": 30, "weight": 1.2,
                    "metric_getter_params": {"period_days": 7}
                },
                "conversion_to_paid_percentage": {
                    "human_name": "Конверсия в платного (%)",
                    "target": 5.0, "critical_min": 1.0, "weight": 1.3,
                    "metric_getter_params": {"period_days": 7}
                },
                "user_retention_day_1_percentage": {
                    "human_name": "Удержание на 1-й день (%)",
                    "target": 60.0, "critical_min": 40.0, "weight": 1.0,
                    "metric_getter_params": {"period_days": 7} # Для расчета ретеншена может потребоваться другой период
                },
                "customer_satisfaction_score_avg": {
                    "human_name": "Средний CSAT (1-5)",
                    "target": 4.0, "critical_min": 3.5, "weight": 0.8,
                    "metric_getter_params": {"period_days": 7}
                }
            }
        }

    async def validate_launch_success_period(self, days_since_launch: int) -> Dict[str, Any]:
        """
        Comprehensive launch success validation for a specific period
        (e.g., after 1 day, after 7 days).
        """
        logger.info(f"Validating launch success for {days_since_launch} days since launch.")
        validation_result: Dict[str, Any] = {
            "days_since_launch": days_since_launch,
            "evaluation_period_key": "",
            "overall_success_score_percent": 0.0,
            "launch_status": "unknown",
            "metric_evaluations": [],
            "critical_issues_found_count": 0,
            "warnings_found_count": 0,
            "critical_issue_details": [],
            "warning_details": [],
            "recommendations": []
        }

        period_key = ""
        if days_since_launch <= 1: period_key = "first_24_hours"
        elif days_since_launch <= 7: period_key = "first_week"
        else:
            logger.warning(f"No predefined success criteria for {days_since_launch} days since launch. Using latest available (first_week).")
            period_key = "first_week" # По умолчанию используем критерии последней недели
            if period_key not in self.success_criteria and "first_24_hours" in self.success_criteria:
                 period_key = "first_24_hours" # Если нет недели, берем 24 часа
            elif period_key not in self.success_criteria:
                validation_result["launch_status"] = "no_criteria_defined"
                validation_result["error"] = f"No success criteria defined for {days_since_launch} days or fallback periods."
                return validation_result

        validation_result["evaluation_period_key"] = period_key
        criteria_for_period = self.success_criteria.get(period_key, {})
        if not criteria_for_period: # Дополнительная проверка
            validation_result["launch_status"] = "no_criteria_for_period"
            validation_result["error"] = f"Success criteria for period '{period_key}' not found (empty)."
            return validation_result

        total_weighted_score = 0.0
        total_possible_weight = 0.0

        for metric_name, criteria in criteria_for_period.items():
            metric_getter_params = criteria.get("metric_getter_params", {"period_days": days_since_launch})
            current_value = await self._get_current_metric_value(metric_name, **metric_getter_params)
            evaluation = self._evaluate_metric_against_criteria(metric_name, current_value, criteria)
            
            validation_result["metric_evaluations"].append(evaluation)
            metric_weight = criteria.get("weight", 1.0)

            if evaluation["status"] == "critical_failure":
                validation_result["critical_issues_found_count"] += 1
                validation_result["critical_issue_details"].append(f"{criteria.get('human_name', metric_name)}: {evaluation['message']}")
            elif evaluation["status"] == "warning":
                validation_result["warnings_found_count"] += 1
                validation_result["warning_details"].append(f"{criteria.get('human_name', metric_name)}: {evaluation['message']}")
                total_weighted_score += metric_weight * 0.5
            elif evaluation["status"] == "success":
                total_weighted_score += metric_weight * 1.0
            
            total_possible_weight += metric_weight

        if total_possible_weight > 0:
            validation_result["overall_success_score_percent"] = round((total_weighted_score / total_possible_weight) * 100, 2)
        
        score = validation_result["overall_success_score_percent"]
        if validation_result["critical_issues_found_count"] > 0: validation_result["launch_status"] = "critical_issues"
        elif score >= 90: validation_result["launch_status"] = "excellent"
        elif score >= 75: validation_result["launch_status"] = "successful"
        elif score >= 60: validation_result["launch_status"] = "acceptable"
        else: validation_result["launch_status"] = "needs_improvement"
        
        validation_result["recommendations"] = self._generate_success_recommendations(validation_result)
        logger.info(f"Launch success validation for period '{period_key}' complete. Status: {validation_result['launch_status']}, Score: {score}%")
        return validation_result

    def _evaluate_metric_against_criteria(self, metric_name: str, current_value: Optional[float], criteria: Dict[str, Any]) -> Dict[str, Any]:
        evaluation: Dict[str, Any] = {
            "metric_name": metric_name,
            "human_name": criteria.get("human_name", metric_name),
            "current_value": current_value,
            "target_value": criteria.get("target"),
            "critical_min_value": criteria.get("critical_min"),
            "critical_max_value": criteria.get("critical_max"),
            "status": "unknown",
            "message": ""
        }

        if current_value is None:
            evaluation["status"] = "error_fetching_value"
            evaluation["message"] = "Не удалось получить текущее значение метрики."
            return evaluation

        target = criteria.get("target")
        critical_min = criteria.get("critical_min")
        critical_max = criteria.get("critical_max")

        if critical_min is not None: # Чем больше, тем лучше
            if current_value < critical_min:
                evaluation["status"] = "critical_failure"
                evaluation["message"] = f"Критически низкое: {current_value} < {critical_min} (крит. мин)."
            elif target is not None and current_value < target:
                evaluation["status"] = "warning"
                evaluation["message"] = f"Ниже цели: {current_value} < {target} (цель)."
            else:
                evaluation["status"] = "success"
                evaluation["message"] = f"Достигнута или превышена цель/минимум: {current_value}."
        elif critical_max is not None: # Чем меньше, тем лучше
            if current_value > critical_max:
                evaluation["status"] = "critical_failure"
                evaluation["message"] = f"Критически высокое: {current_value} > {critical_max} (крит. макс)."
            elif target is not None and current_value > target:
                evaluation["status"] = "warning"
                evaluation["message"] = f"Выше цели: {current_value} > {target} (цель)."
            else:
                evaluation["status"] = "success"
                evaluation["message"] = f"Достигнута или ниже цели/максимума: {current_value}."
        elif target is not None:
             # Если есть только target, оцениваем отклонение (например, +/- 10% от цели - warning)
            if abs(current_value - target) / target > 0.20 : # Отклонение более 20% - warning
                 evaluation["status"] = "warning"
                 evaluation["message"] = f"Значение {current_value} значительно отклоняется от цели {target}."
            else:
                 evaluation["status"] = "success"
                 evaluation["message"] = f"Значение метрики: {current_value} (цель: {target})."
        else:
            evaluation["status"] = "no_clear_criteria"
            evaluation["message"] = "Не определены четкие критерии (min/max/target) для оценки."
            logger.warning(f"No clear evaluation criteria for metric '{metric_name}'.")
        return evaluation

    async def _get_current_metric_value(self, metric_name: str, period_days: int) -> Optional[float]:
        """
        Fetches the current value for a given metric from DatabaseService.
        Uses period_days to define the lookback window for the metric.
        """
        logger.debug(f"Fetching current value for metric '{metric_name}' for the last {period_days} days.")
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=period_days)
        
        value: Optional[Union[int, float]] = None # Инициализация

        # ЗАГЛУШКИ - здесь должны быть вызовы методов self.db_service
        # Эти методы должны быть реализованы в DatabaseService
        try:
            if metric_name == "system_uptime_percentage":
                # value = await self.db_service.get_system_uptime_for_period(start_date, end_date)
                value = random.uniform(99.0, 100.0) # Заглушка
            elif metric_name == "avg_response_time_ms":
                # value = await self.db_service.get_average_response_time_for_period(start_date, end_date)
                value = random.uniform(500, 4000) # Заглушка
            elif metric_name == "error_rate_percentage":
                # value = await self.db_service.get_error_rate_for_period(start_date, end_date)
                value = random.uniform(0.1, 3.0) # Заглушка
            elif metric_name == "successful_user_registrations_count":
                value = float(await self.db_service.get_new_users_count_for_period(start_date, end_date))
            elif metric_name == "successful_payments_count":
                # value = await self.db_service.get_successful_payments_count_for_period(start_date, end_date)
                value = float(random.randint(0, 10 * period_days // 7)) # Заглушка, пропорциональная периоду
            elif metric_name == "daily_active_users_avg":
                value = await self.db_service.get_average_dau_for_period(start_date, end_date)
            elif metric_name == "conversion_to_paid_percentage":
                # sub_analytics = await self.db_service.get_subscription_analytics_for_period(start_date, end_date)
                # value = sub_analytics.get("conversion_rate_from_new_to_paid_percent") if sub_analytics else None
                value = random.uniform(1.0, 10.0) # Заглушка
            elif metric_name == "user_retention_day_1_percentage":
                # retention_start = start_date - timedelta(days=1) # Базовый период для Day 1 Retention
                # retention_end = end_date - timedelta(days=1)
                # value = await self.db_service.get_retention_rate_day_n(day_n=1, cohort_period_start=retention_start, cohort_period_end=retention_end)
                value = random.uniform(30.0, 75.0) # Заглушка
            elif metric_name == "customer_satisfaction_score_avg":
                # value = await self.db_service.get_average_csat_score_for_period(start_date, end_date)
                value = random.uniform(3.0, 4.8) # Заглушка
            else:
                logger.warning(f"Metric '{metric_name}' has no defined data fetching logic in _get_current_metric_value.")
                return None
            
            return float(value) if value is not None else None

        except Exception as e:
            logger.error(f"Error fetching metric '{metric_name}': {e}", exc_info=True)
            return None


    def _generate_success_recommendations(self, validation_result: Dict[str, Any]) -> List[str]:
        """Generates actionable recommendations based on validation results."""
        recommendations = []
        status = validation_result.get("launch_status", "unknown")
        human_period = validation_result.get("evaluation_period_key", "current period").replace("_", " ")

        if status == "critical_issues":
            recommendations.append(f"🚨 {hbold(f'КРИТИЧЕСКИЕ ПРОБЛЕМЫ ({validation_result.get('critical_issues_found_count',0)}) в {human_period}!')} Требуется немедленное вмешательство.")
        elif status == "needs_improvement":
            recommendations.append(f"⚠️ {hbold(f'Запуск в {human_period} требует улучшений.')} Сфокусируйтесь на метриках с предупреждениями или критическими провалами.")
        elif status == "acceptable":
            recommendations.append(f"✅ {hbold(f'Запуск в {human_period} приемлем, но есть пространство для роста.')} Проанализируйте метрики с предупреждениями.")
        elif status == "successful":
            recommendations.append(f"👍 {hbold(f'Успешный {human_period}!')} Продолжайте мониторинг и оптимизацию.")
        elif status == "excellent":
            recommendations.append(f"🎉 {hbold(f'Отличный {human_period}!')} Поддерживайте текущий уровень и планируйте масштабирование.")

        for detail in validation_result.get("critical_issue_details", []):
            recommendations.append(f"  - 🆘 {hbold('Срочно:')} {detail}")
        for detail in validation_result.get("warning_details", []):
            recommendations.append(f"  - 📈 {hbold('Внимание:')} {detail}")
        
        if not recommendations:
            recommendations.append(f"Все ключевые метрики в норме для {human_period}.")
            
        return recommendations[:7] # Ограничиваем количество рекомендаций
