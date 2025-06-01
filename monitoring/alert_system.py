# monitoring/alert_system.py
import asyncio
import logging
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timezone, timedelta

from aiogram import Bot # Для отправки сообщений напрямую, если NotificationService не используется
from aiogram.utils.markdown import hbold # Для форматирования

# Предполагаем, что NotificationService будет импортирован, если используется для отправки
# from services.notification_marketing_system import NotificationService
# MetricsCollector может быть нужен для получения текущих значений метрик, если проверка происходит здесь,
# но в Roadmap предполагается, что MetricsCollector вызывает AlertManager.
# from .production_monitoring import MetricsCollector # Циклическая зависимость, если AlertManager передается в MetricsCollector

from config.settings import BotConfig # Для доступа к ADMIN_USER_IDS

logger = logging.getLogger(__name__)

class AlertManager:
    """
    Управляет правилами алертов, проверяет их срабатывание и уведомляет администраторов.
    """

    def __init__(self, bot_instance: Optional[Any] = None, # 'AICompanionBot' для доступа к bot, config, notification_service
                 config: Optional[BotConfig] = None,
                 notification_service: Optional[Any] = None): # Замените Any на NotificationService, когда он будет готов
        
        # Получаем зависимости из bot_instance, если он передан
        if bot_instance:
            self.bot: Optional[Bot] = getattr(bot_instance, 'bot', None)
            self.config: BotConfig = getattr(bot_instance, 'config', config or BotConfig()) # BotConfig обязателен
            self.notification_service = getattr(bot_instance, 'notification_service', notification_service)
            # self.metrics_collector = getattr(bot_instance, 'metrics_collector', None) # Если нужен доступ к метрикам
        else: # Если bot_instance не передан, зависимости должны быть переданы явно
            self.bot = None # Потребуется установить позже или передать NotificationService
            if not config:
                logger.critical("AlertManager: BotConfig не предоставлен. Используется дефолтный BotConfig, что может быть некорректно.")
                self.config = BotConfig()
            else:
                self.config = config
            self.notification_service = notification_service
            # self.metrics_collector = None


        self.active_alerts: Dict[str, Dict[str, Any]] = {} # Хранит активные (сработавшие) алерты
        self.alert_rules: Dict[str, Dict[str, Any]] = {}   # Хранит зарегистрированные правила алертов
        self.alert_history: List[Dict[str, Any]] = []      # История всех сработавших алертов (можно ограничить по размеру)
        self.cooldown_periods: Dict[str, datetime] = {}    # Для отслеживания периода "затишья" для каждого типа алерта

        logger.info("AlertManager инициализирован.")

    async def add_alert_rule(self, rule_name: str, config: Dict[str, Any]):
        """
        Регистрирует новое правило для алерта.
        Config должен содержать:
            - 'metric_name': Имя метрики для отслеживания (из MetricsCollector).
            - 'threshold_value': Пороговое значение.
            - 'comparison': Оператор сравнения ('<', '>', '<=', '>=', '==', '!=').
            - 'severity': Уровень важности ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL').
            - 'description': Описание алерта.
            - 'cooldown_minutes': Период затишья в минутах (default: 15).
            - 'notification_channels': Список каналов для уведомления (default: ['telegram_admin']).
            - 'business_impact': (опционально) Влияние на бизнес.
            - 'technical_impact': (опционально) Техническое влияние.
        """
        if not all(k in config for k in ['metric_name', 'threshold_value', 'comparison', 'severity', 'description']):
            logger.error(f"Неполная конфигурация для правила алерта '{rule_name}'. Пропущены обязательные поля.")
            return

        self.alert_rules[rule_name] = {
            **config,
            "last_checked_at": None,
            "last_triggered_at": None,
            "trigger_count": 0,
            "is_active_alert_state": False # Флаг, что алерт сейчас активен (сработал и не разрешен)
        }
        logger.info(f"Правило алерта '{rule_name}' для метрики '{config['metric_name']}' зарегистрировано.")

    async def check_and_trigger_alert(self, metric_name: str, current_value: Any, thresholds: Dict[str, Any]):
        """
        Проверяет текущее значение метрики по отношению к заданным порогам и инициирует алерт.
        Этот метод вызывается из MetricsCollector.
        thresholds: {'min': X, 'max': Y, 'critical_min': Z, 'critical_max': W, 'cooldown_minutes': M}
        """
        alert_triggered_this_check = False
        triggered_rule_details: Optional[Dict[str, Any]] = None

        # Ищем правила, связанные с этой метрикой
        for rule_name, rule_config in self.alert_rules.items():
            if rule_config.get("metric_name") == metric_name:
                # Используем пороги из конфигурации правила, если они там есть,
                # иначе используем пороги, переданные из MetricsCollector (если они есть в thresholds)
                rule_thresholds = rule_config.get("alert_thresholds", thresholds)
                
                # Проверяем каждый тип порога (min, max, critical_min, critical_max)
                # Это упрощенная проверка, в Roadmap была более сложная структура 'condition'
                # Адаптируем под структуру 'alert_thresholds': {'min': X, 'critical_min': Y}
                
                severity_to_check = None
                threshold_violated = None
                comparison_operator = ""

                if "critical_min" in rule_thresholds and current_value < rule_thresholds["critical_min"]:
                    severity_to_check = "CRITICAL"
                    threshold_violated = rule_thresholds["critical_min"]
                    comparison_operator = "<"
                elif "min" in rule_thresholds and current_value < rule_thresholds["min"]:
                    severity_to_check = rule_config.get("severity", "HIGH") # Используем severity из правила
                    threshold_violated = rule_thresholds["min"]
                    comparison_operator = "<"
                elif "critical_max" in rule_thresholds and current_value > rule_thresholds["critical_max"]:
                    severity_to_check = "CRITICAL"
                    threshold_violated = rule_thresholds["critical_max"]
                    comparison_operator = ">"
                elif "max" in rule_thresholds and current_value > rule_thresholds["max"]:
                    severity_to_check = rule_config.get("severity", "HIGH")
                    threshold_violated = rule_thresholds["max"]
                    comparison_operator = ">"
                
                if severity_to_check and threshold_violated is not None:
                    alert_key = f"{rule_name}_{metric_name}_{severity_to_check.lower()}"
                    if not await self._is_in_cooldown(alert_key, rule_config):
                        alert_data = {
                            "type": "metric_threshold_violation",
                            "rule_name": rule_name,
                            "metric_name": metric_name,
                            "current_value": current_value,
                            "threshold_value": threshold_violated,
                            "comparison": comparison_operator,
                            "severity": severity_to_check,
                            "description": rule_config.get("description", f"Metric {metric_name} violated threshold."),
                            "title": f"{severity_to_check} Alert: {metric_name} {comparison_operator} {threshold_violated}",
                            "cooldown_minutes": rule_thresholds.get("cooldown_minutes", rule_config.get("cooldown_minutes", 15))
                        }
                        await self.trigger_alert(alert_key, alert_data)
                        alert_triggered_this_check = True
                        triggered_rule_details = rule_config
                        rule_config["is_active_alert_state"] = True # Помечаем, что алерт активен
                    else:
                        logger.debug(f"Alert for rule '{rule_name}' (metric: {metric_name}) is in cooldown.")
                elif rule_config["is_active_alert_state"]: # Если алерт был активен, но условие больше не выполняется
                    logger.info(f"Alert condition for rule '{rule_name}' (metric: {metric_name}) is now resolved. Current value: {current_value}")
                    rule_config["is_active_alert_state"] = False
                    # Можно отправить уведомление о разрешении проблемы
                    # await self.send_alert_resolved_notification(rule_name, metric_name, current_value)


    async def trigger_alert(self, alert_key: str, alert_data: Dict[str, Any]):
        """Инициирует обработку алерта: логирование, уведомление, установка cooldown."""
        now = datetime.now(timezone.utc)
        alert_data["timestamp"] = now.isoformat()

        self.active_alerts[alert_key] = alert_data
        self.alert_history.append(alert_data)
        if len(self.alert_history) > 1000: # Ограничиваем историю
            self.alert_history.pop(0)

        rule_name = alert_data.get("rule_name")
        if rule_name and rule_name in self.alert_rules:
            self.alert_rules[rule_name]["last_triggered_at"] = now
            self.alert_rules[rule_name]["trigger_count"] = self.alert_rules[rule_name].get("trigger_count", 0) + 1
            self.alert_rules[rule_name]["is_active_alert_state"] = True


        logger.warning(f"ALERT TRIGGERED: Key='{alert_key}', Data={alert_data}")
        await self._send_formatted_alert(alert_key, alert_data)
        self._set_cooldown(alert_key, alert_data.get("cooldown_minutes", 15))


    async def _send_formatted_alert(self, alert_key: str, alert_data: Dict[str, Any]):
        """Форматирует и отправляет уведомление об алерте."""
        formatted_message = self._format_alert_message_from_data(alert_data)
        severity = alert_data.get("severity", "MEDIUM")
        channels = self._get_notification_channels_for_severity(severity)
        rule_config = self.alert_rules.get(alert_data.get("rule_name", ""))
        if rule_config:
            channels = rule_config.get("notification_channels", channels)


        # Отправка через NotificationService или напрямую через self.bot
        if self.notification_service and hasattr(self.notification_service, 'send_admin_alert_message'): # Пример метода
            try:
                # Предполагаем, что send_admin_alert_message принимает текст и каналы
                await self.notification_service.send_admin_alert_message(
                    message_text=formatted_message,
                    channels=channels, # NotificationService должен знать, как работать с каналами
                    priority=severity
                )
                logger.info(f"Alert '{alert_key}' sent via NotificationService to channels: {channels}.")
            except Exception as e:
                logger.error(f"Failed to send alert '{alert_key}' via NotificationService: {e}. Falling back to direct bot message.")
                await self._send_direct_bot_alert(formatted_message)
        else:
            await self._send_direct_bot_alert(formatted_message)

    async def _send_direct_bot_alert(self, formatted_message: str):
        """Отправляет алерт напрямую через self.bot администраторам."""
        if not self.bot:
            logger.error("Cannot send direct bot alert: Bot instance is not available in AlertManager.")
            return
        if not self.config.admin_user_ids:
            logger.warning("Cannot send direct bot alert: ADMIN_USER_IDS not configured.")
            return

        for admin_id in self.config.admin_user_ids:
            try:
                await self.bot.send_message(admin_id, formatted_message, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Failed to send direct alert to admin {admin_id}: {e}")


    def _format_alert_message_from_data(self, alert_data: Dict[str, Any]) -> str:
        """Форматирует сообщение алерта на основе данных из alert_data."""
        severity = alert_data.get("severity", "INFO")
        title = alert_data.get("title", alert_data.get("type", "Alert"))
        description = alert_data.get("description", "No description provided.")
        timestamp_iso = alert_data.get("timestamp", datetime.now(timezone.utc).isoformat())
        
        try:
            timestamp_dt = datetime.fromisoformat(timestamp_iso.replace('Z', '+00:00'))
            time_str = timestamp_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except ValueError:
            time_str = timestamp_iso

        severity_emojis = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "⚡", "LOW": "ℹ️", "INFO": "ℹ️"}
        emoji = severity_emojis.get(severity, "📊")

        message = f"{emoji} {hbold(f'{severity} ALERT: {title}')}\n\n"
        message += f"**Time**: {time_str}\n"
        message += f"**Description**: {description}\n"

        # Добавляем детали из alert_data, исключая уже использованные
        extra_context: Dict[str, Any] = {
            k: v for k, v in alert_data.items()
            if k not in ["type", "rule_name", "severity", "description", "title", "timestamp", "cooldown_minutes"] and v is not None
        }
        if extra_context:
            message += "\n**Details**:\n"
            for key, value in extra_context.items():
                message += f"  - {key.replace('_', ' ').title()}: {value}\n"
        
        # Можно добавить рекомендации, если они есть в alert_data или rule_config
        # if "recommended_actions" in alert_data: ...

        return message.strip()


    def _get_notification_channels_for_severity(self, severity: str) -> List[str]:
        """Определяет каналы уведомлений по уровню важности (из Roadmap)."""
        # Roadmap: CRITICAL: ["admin_telegram", "sms", "email"], HIGH: ["admin_telegram", "email"], MEDIUM/LOW: ["admin_telegram"]
        # Пока реализуем только "admin_telegram" как общий канал.
        # Реальная отправка по разным каналам потребует интеграции с NotificationService.
        if severity == "CRITICAL":
            return ["telegram_admin", "email_admin", "sms_admin"] # Примерные названия каналов
        elif severity == "HIGH":
            return ["telegram_admin", "email_admin"]
        else: # MEDIUM, LOW, INFO
            return ["telegram_admin"]

    async def _is_in_cooldown(self, alert_key: str, rule_config: Optional[Dict[str, Any]] = None) -> bool:
        """Проверяет, находится ли алерт в периоде "затишья"."""
        cooldown_until = self.cooldown_periods.get(alert_key)
        if cooldown_until and datetime.now(timezone.utc) < cooldown_until:
            return True
        
        # Если cooldown прошел или не был установлен, сбрасываем его
        if alert_key in self.cooldown_periods:
            del self.cooldown_periods[alert_key]
        return False

    def _set_cooldown(self, alert_key: str, minutes: Optional[Union[int, float]]):
        """Устанавливает период "затишья" для алерта."""
        if minutes is None or minutes <= 0:
            minutes = 15 # Дефолтный cooldown
        
        cooldown_until = datetime.now(timezone.utc) + timedelta(minutes=float(minutes))
        self.cooldown_periods[alert_key] = cooldown_until
        logger.debug(f"Cooldown for alert '{alert_key}' set until {cooldown_until.isoformat()}.")

    async def get_active_alerts_summary(self) -> List[Dict[str, Any]]:
        """Возвращает сводку по текущим активным (неразрешенным) алертам."""
        summary = []
        for key, data in self.active_alerts.items():
            # Проверяем, действительно ли условие алерта все еще выполняется (если есть доступ к метрикам)
            # Это сложнее, т.к. AlertManager сам не хранит метрики.
            # Пока просто возвращаем то, что было зафиксировано как активное.
            # В ProductionMonitoringSystem можно добавить логику разрешения алертов.
            if self.alert_rules.get(data.get("rule_name"), {}).get("is_active_alert_state"):
                 summary.append({
                    "alert_key": key,
                    "title": data.get("title", "N/A"),
                    "severity": data.get("severity", "N/A"),
                    "triggered_at": data.get("timestamp", "N/A"),
                    "description": data.get("description", "N/A")
                })
        return summary

    # async def send_alert_resolved_notification(self, rule_name: str, metric_name: str, current_value: Any):
    #     """Отправляет уведомление о том, что условие алерта больше не выполняется."""
    #     alert_key = f"{rule_name}_{metric_name}_resolved" # Уникальный ключ для resolved-уведомления
    #     if not await self._is_in_cooldown(alert_key, {"cooldown_minutes": 60}): # Cooldown для resolved-уведомлений
    #         alert_data = {
    #             "type": "metric_threshold_resolved",
    #             "rule_name": rule_name,
    #             "metric_name": metric_name,
    #             "current_value": current_value,
    #             "severity": "INFO", # Уведомление о разрешении обычно INFO
    #             "description": f"Condition for alert rule '{rule_name}' (metric: {metric_name}) is now resolved.",
    #             "title": f"RESOLVED: Alert for {metric_name}",
    #             "cooldown_minutes": 60
    #         }
    #         await self.trigger_alert(alert_key, alert_data) # Используем тот же механизм trigger_alert
    #         logger.info(f"Sent RESOLVED notification for rule '{rule_name}', metric '{metric_name}'.")
