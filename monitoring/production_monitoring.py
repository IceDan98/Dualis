# monitoring/production_monitoring.py
import asyncio
import logging
from typing import Dict, Any, List, Optional, Callable, Awaitable, Union
from datetime import datetime, timezone, timedelta
import random # Для заглушек

from database.operations import DatabaseService
from config.settings import BotConfig
from .alert_system import AlertManager # Импортируем AlertManager

logger = logging.getLogger(__name__)

class MetricsCollector:
    """
    Собирает и агрегирует метрики для мониторинга.
    Использует DatabaseService для получения сырых данных.
    Может использовать кэш для часто запрашиваемых метрик.
    """
    def __init__(self, db_service: DatabaseService, config: BotConfig, alert_manager: AlertManager): # alert_manager теперь обязателен
        self.db_service = db_service
        self.config = config
        self.alert_manager = alert_manager
        self.registered_metrics: Dict[str, Dict[str, Any]] = {}
        self.collection_tasks: Dict[str, asyncio.Task] = {}
        # self.cache_service = cache_service # Если будет отдельный сервис кэширования

    async def register_metric(self, metric_name: str, collection_config: Dict[str, Any]):
        """
        Регистрирует метрику для периодического сбора.
        collection_config может содержать:
            - 'source_type': 'db_method', 'calc_method'
            - 'query_or_method': Имя метода в db_service/self
            - 'params': параметры для метода
            - 'check_interval_sec': как часто собирать (default: 300)
            - 'alert_thresholds': {'min': X, 'max': Y, 'critical_min': Z, ...} - пороги для AlertManager
            - 'alert_rule_config': Полная конфигурация правила для AlertManager (имеет приоритет над alert_thresholds)
        """
        if metric_name in self.registered_metrics:
            logger.warning(f"Metric '{metric_name}' is already registered. Re-registering.")
            if metric_name in self.collection_tasks and not self.collection_tasks[metric_name].done():
                self.collection_tasks[metric_name].cancel()

        self.registered_metrics[metric_name] = {
            **collection_config,
            "last_collected_at": None,
            "last_value": None,
            "consecutive_collection_errors": 0
        }

        # Если есть alert_rule_config, регистрируем правило в AlertManager
        if "alert_rule_config" in collection_config:
            rule_conf = collection_config["alert_rule_config"]
            # Убедимся, что имя метрики в правиле совпадает
            if rule_conf.get("metric_name") != metric_name:
                logger.warning(f"Mismatch in metric_name for alert rule '{rule_conf.get('rule_name', metric_name)}'. Overriding with '{metric_name}'.")
            rule_conf["metric_name"] = metric_name # Гарантируем правильное имя метрики
            rule_name_for_alert = rule_conf.pop("rule_name", f"alert_for_{metric_name}") # Извлекаем имя правила или генерируем
            await self.alert_manager.add_alert_rule(rule_name_for_alert, rule_conf)

        self.collection_tasks[metric_name] = asyncio.create_task(self._collect_metric_loop(metric_name))
        logger.info(f"Metric '{metric_name}' registered for collection every {collection_config.get('check_interval_sec', 300)}s.")

    async def _collect_metric_loop(self, metric_name: str):
        metric_config = self.registered_metrics.get(metric_name)
        if not metric_config:
            logger.error(f"Configuration for metric '{metric_name}' not found in _collect_metric_loop.")
            return

        check_interval = metric_config.get("check_interval_sec", 300)

        while True:
            try:
                current_value = await self._collect_single_metric_value(metric_name, metric_config)
                
                metric_config["last_collected_at"] = datetime.now(timezone.utc)
                metric_config["last_value"] = current_value
                metric_config["consecutive_collection_errors"] = 0
                
                # TODO: Сохранить значение метрики в БД (например, в BotStatistics) - функционал сохранения статистики будет реализован в рамках отдельной задачи по развитию системы аналитики.
                # await self.db_service.save_statistic(metric_name=f"monitoring_{metric_name}", metric_value=float(current_value))
                logger.debug(f"Collected metric '{metric_name}': {current_value}")

                # Проверка порогов и отправка алертов через AlertManager
                # AlertManager теперь сам ищет правила по metric_name
                await self.alert_manager.check_and_trigger_alert(
                    metric_name, 
                    current_value, 
                    metric_config.get("alert_thresholds", {}) # Передаем пороги из конфига метрики, если нет alert_rule_config
                )

            except asyncio.CancelledError:
                logger.info(f"Metric collection loop for '{metric_name}' cancelled.")
                break
            except Exception as e:
                metric_config["consecutive_collection_errors"] += 1
                logger.error(f"Error collecting metric '{metric_name}': {e}", exc_info=True)
                if metric_config["consecutive_collection_errors"] >= 3:
                    await self.alert_manager.trigger_alert( # Используем trigger_alert для прямых событий
                        alert_key=f"metric_collection_failure_{metric_name}",
                        alert_data={
                            "type": "metric_collection_system_error",
                            "rule_name": f"failure_rule_for_{metric_name}", # Генерируем имя правила
                            "metric_name": metric_name,
                            "severity": "HIGH",
                            "description": f"Failed to collect metric '{metric_name}' {metric_config['consecutive_collection_errors']} times consecutively.",
                            "title": f"Metric Collection Failure: {metric_name}",
                            "details": str(e),
                            "cooldown_minutes": 60 # Cooldown для этого типа системного алерта
                        }
                    )
            
            await asyncio.sleep(check_interval)

    async def _collect_single_metric_value(self, metric_name: str, config: Dict[str, Any]) -> Any:
        source_type = config.get("source_type")
        query_or_method_name = config.get("query_or_method")
        params = config.get("params", {})

        if source_type == "db_method":
            if not hasattr(self.db_service, query_or_method_name): # type: ignore
                raise ValueError(f"DatabaseService has no method '{query_or_method_name}' for metric '{metric_name}'.")
            db_method_to_call: Callable[..., Awaitable[Any]] = getattr(self.db_service, query_or_method_name) # type: ignore
            return await db_method_to_call(**params)
        elif source_type == "calc_method":
            if not hasattr(self, query_or_method_name): # type: ignore
                raise ValueError(f"MetricsCollector has no calculation method '{query_or_method_name}' for metric '{metric_name}'.")
            calc_method_to_call: Callable[..., Awaitable[Any]] = getattr(self, query_or_method_name) # type: ignore
            return await calc_method_to_call(**params)
        else:
            raise ValueError(f"Unsupported source_type '{source_type}' for metric '{metric_name}'.")

    async def get_metric_value(self, metric_name: str) -> Optional[Any]:
        metric_config = self.registered_metrics.get(metric_name)
        if metric_config:
            return metric_config.get("last_value")
        logger.warning(f"Attempted to get value for unregistered metric '{metric_name}'.")
        return None

    async def stop_all_collections(self): # pragma: no cover
        logger.info("Stopping all metric collection tasks...")
        for metric_name, task in self.collection_tasks.items():
            if task and not task.done():
                task.cancel()
                logger.info(f"Cancelled collection task for metric '{metric_name}'.")
        await asyncio.gather(*[task for task in self.collection_tasks.values() if task and not task.done()], return_exceptions=True)
        self.collection_tasks.clear()
        logger.info("All metric collection tasks stopped.")

    # --- Пример методов для расчета метрик (calc_method) ---
    async def calculate_dau_metric(self, period_days: int = 1) -> Optional[float]: # pragma: no cover
        try:
            # Предполагаем, что get_average_dau_for_period возвращает float или None
            return await self.db_service.get_average_dau_for_period(
                datetime.now(timezone.utc) - timedelta(days=period_days),
                datetime.now(timezone.utc)
            )
        except Exception as e:
            logger.error(f"Error in calculate_dau_metric: {e}", exc_info=True)
            return None


    async def calculate_conversion_rate_metric(self, period_days: int = 7) -> Optional[float]: # pragma: no cover
        try:
            # Предполагаем, что get_subscription_analytics_for_period возвращает словарь или None
            sub_analytics = await self.db_service.get_subscription_analytics_for_period(
                datetime.now(timezone.utc) - timedelta(days=period_days),
                datetime.now(timezone.utc)
            )
            return sub_analytics.get("conversion_rate_from_new_to_paid_percent") if sub_analytics else None
        except Exception as e:
            logger.error(f"Error in calculate_conversion_rate_metric: {e}", exc_info=True)
            return None

class ProductionMonitoringSystem:
    """
    Comprehensive monitoring for production deployment.
    Uses MetricsCollector and AlertManager.
    """
    def __init__(self, bot_instance: Any): # 'AICompanionBot'
        self.bot_instance = bot_instance
        self.db_service: DatabaseService = getattr(bot_instance, 'db_service')
        self.config: BotConfig = getattr(bot_instance, 'config')
        self.alert_manager: AlertManager = AlertManager(bot_instance=bot_instance) # Инициализируем AlertManager здесь
        self.metrics_collector: MetricsCollector = MetricsCollector(self.db_service, self.config, self.alert_manager)
        self.is_initialized = False

    async def initialize_monitoring(self):
        if self.is_initialized:
            logger.info("Production Monitoring System already initialized.")
            return

        logger.info("Initializing Production Monitoring System...")
        await self._setup_business_metrics_monitoring()
        await self._setup_technical_performance_monitoring() # Пока заглушка
        await self._setup_error_monitoring_alerts() # Пока заглушка
        self.is_initialized = True
        logger.info("Production Monitoring System initialized successfully.")

    async def _setup_business_metrics_monitoring(self):
        logger.info("Setting up business metrics monitoring rules...")
        
        # Правила из Roadmap (Часть 4, Comprehensive Alert Framework -> setup_business_alerts)
        # Адаптируем их для регистрации в MetricsCollector и AlertManager
        
        # Daily Active Users
        await self.metrics_collector.register_metric(
            metric_name="daily_active_users",
            collection_config={
                "source_type": "calc_method",
                "query_or_method": "calculate_dau_metric", # Метод в MetricsCollector
                "params": {"period_days": 1},
                "check_interval_sec": 3600, # Каждый час
                "alert_rule_config": { # Конфигурация для AlertManager.add_alert_rule
                    "rule_name": "dau_critical_low", # Уникальное имя правила
                    # metric_name будет добавлен автоматически = "daily_active_users"
                    "threshold_value": self.config.monitoring_dau_critical_min or 10, # Из BotConfig
                    "comparison": "<",
                    "severity": "CRITICAL",
                    "description": "Daily Active Users critically low.",
                    "cooldown_minutes": 60,
                    "notification_channels": ["telegram_admin", "email_admin"] # Пример
                }
            }
        )

        # Subscription Conversion Rate (Weekly)
        await self.metrics_collector.register_metric(
            metric_name="subscription_conversion_rate_weekly",
            collection_config={
                "source_type": "calc_method",
                "query_or_method": "calculate_conversion_rate_metric",
                "params": {"period_days": 7},
                "check_interval_sec": 6 * 3600, # Каждые 6 часов
                "alert_rule_config": {
                    "rule_name": "conversion_rate_low",
                    "threshold_value": self.config.monitoring_conversion_min_percent or 5.0,
                    "comparison": "<",
                    "severity": "HIGH",
                    "description": "Weekly subscription conversion rate is below target.",
                    "cooldown_minutes": 24 * 60 # раз в сутки
                }
            }
        )

        # Daily Revenue (пример, если есть метод в db_service)
        if hasattr(self.db_service, 'get_daily_revenue_stars_sum'): # Проверяем наличие метода
            await self.metrics_collector.register_metric(
                metric_name="daily_revenue_stars",
                collection_config={
                    "source_type": "db_method",
                    "query_or_method": "get_daily_revenue_stars_sum", # Метод должен быть в DatabaseService
                    "params": {"target_date": datetime.now(timezone.utc).date()}, # Передаем текущую дату
                    "check_interval_sec": 1800, # Каждые 30 минут
                    "alert_rule_config": {
                        "rule_name": "daily_revenue_critically_low",
                        "threshold_value": getattr(self.config, 'monitoring_revenue_critical_min_stars', 100),
                        "comparison": "<",
                        "severity": "CRITICAL",
                        "description": "Daily revenue in Stars is critically low.",
                        "cooldown_minutes": 60
                    }
                }
            )
        else:
            logger.warning("Method 'get_daily_revenue_stars_sum' not found in DatabaseService. Metric 'daily_revenue_stars' not registered.")

        # Churn Spike (пример, потребует более сложной метрики)
        # Для этого нужно, чтобы MetricsCollector мог собирать "daily_churn_rate"
        # await self.alert_manager.add_alert_rule(
        #     rule_name="churn_spike_daily",
        #     config={
        #         "metric_name": "daily_churn_rate_percent", # Эта метрика должна собираться MetricsCollector
        #         "threshold_value": 10.0, # Например, если дневной отток > 10% от среднего за 30 дней
        #         "comparison": ">", # ( (current_daily_churn) / (avg_30d_churn) * 100 ) > threshold_value
        #         "severity": "CRITICAL",
        #         "description": "Unusual spike in daily customer churn.",
        #         "cooldown_minutes": 1440 # Раз в день
        #     }
        # )
        logger.info("Business metrics monitoring rules set up.")


    async def _setup_technical_performance_monitoring(self): # pragma: no cover
        logger.info("Setting up technical performance monitoring (STUB).")
        # Примеры метрик:
        # - Среднее время ответа API LLM
        # - Процент ошибок API LLM
        # - Среднее время выполнения запросов к БД
        # - Загрузка CPU/Memory сервера (если доступно)
        #
        # Пример регистрации метрики и алерта:
        # await self.metrics_collector.register_metric(
        #     metric_name="avg_llm_response_time_ms",
        #     collection_config={
        #         "source_type": "calc_method", # или из логов, или из LLMService.get_stats()
        #         "query_or_method": "get_avg_llm_response_time", # Метод в MetricsCollector или LLMService
        #         "check_interval_sec": 60,
        #         "alert_rule_config": {
        #             "rule_name": "llm_response_slow",
        #             "threshold_value": 5000, # 5 секунд
        #             "comparison": ">",
        #             "severity": "HIGH",
        #             "description": "Average LLM API response time is too high.",
        #             "cooldown_minutes": 30
        #         }
        #     }
        # )
        pass

    async def _setup_error_monitoring_alerts(self): # pragma: no cover
        logger.info("Setting up error monitoring alerts (STUB).")
        # Алерт на высокий процент ошибок в боте (например, из ErrorHandler.get_error_stats())
        # Алерт на частые ошибки конкретного типа
        #
        # Пример:
        # await self.alert_manager.add_alert_rule(
        #     rule_name="high_payment_errors_rate",
        #     config={
        #         "metric_name": "payment_error_rate_last_1h_percent", # Метрика из MetricsCollector
        #         "threshold_value": 10.0, # 10% ошибок платежей за час
        #         "comparison": ">",
        #         "severity": "CRITICAL",
        #         "description": "High rate of payment processing errors detected.",
        #         "cooldown_minutes": 30
        #     }
        # )
        pass

    async def get_current_system_status(self) -> Dict[str, Any]: # pragma: no cover
        status_summary: Dict[str, Any] = {"timestamp": datetime.now(timezone.utc).isoformat(), "metrics": {}, "active_alerts": []}
        for metric_name, config in self.metrics_collector.registered_metrics.items():
            last_value = config.get("last_value")
            # Определяем статус на основе порогов (упрощенно)
            metric_status = "ok"
            alert_thresholds = config.get("alert_thresholds", config.get("alert_rule_config", {})) # Ищем пороги
            
            if alert_thresholds and last_value is not None:
                crit_min = alert_thresholds.get("critical_min")
                norm_min = alert_thresholds.get("min")
                crit_max = alert_thresholds.get("critical_max")
                norm_max = alert_thresholds.get("max")
                comparison = alert_thresholds.get("comparison") # Из alert_rule_config

                if comparison == "<":
                    if crit_min is not None and last_value < crit_min: metric_status = "critical"
                    elif norm_min is not None and last_value < norm_min: metric_status = "warning"
                elif comparison == ">":
                    if crit_max is not None and last_value > crit_max: metric_status = "critical"
                    elif norm_max is not None and last_value > norm_max: metric_status = "warning"
            
            status_summary["metrics"][metric_name] = {
                "value": last_value,
                "last_collected_at": config.get("last_collected_at").isoformat() if config.get("last_collected_at") else None,
                "status": metric_status
            }
        
        status_summary["active_alerts"] = await self.alert_manager.get_active_alerts_summary()
        return status_summary

    async def stop_monitoring_system(self): # pragma: no cover
        logger.info("Stopping Production Monitoring System...")
        await self.metrics_collector.stop_all_collections()
        # if self.alert_manager and hasattr(self.alert_manager, 'stop_alert_routines'): # Если есть фоновые задачи в AlertManager
        #     await self.alert_manager.stop_alert_routines()
        self.is_initialized = False
        logger.info("Production Monitoring System stopped.")
