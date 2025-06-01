import os
import logging
from typing import Dict
from pathlib import Path

logger = logging.getLogger(__name__)

class PromptManager:
    """Менеджер для загрузки и управления системными промптами"""
    
    def __init__(self, prompts_dir: str = "personas"):
        self.prompts_dir = Path(prompts_dir)
        self._prompts: Dict[str, str] = {}
        self._load_prompts()
    
    def _load_prompts(self):
        """Загружает все промпты из файлов"""
        prompt_files = {
            "aeris": "aeris.txt",
            "luneth": "luneth.txt"
        }
        
        for persona, filename in prompt_files.items():
            file_path = self.prompts_dir / filename
            try:
                if file_path.exists():
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                        if content:
                            self._prompts[persona] = content
                            logger.info(f"Загружен промпт для {persona}: {len(content)} символов")
                        else:
                            logger.warning(f"Файл {filename} пуст")
                else:
                    logger.error(f"Файл промпта не найден: {file_path}")
            except Exception as e:
                logger.error(f"Ошибка загрузки промпта {filename}: {e}")
                raise
    
    def get_prompt(self, persona: str) -> str:
        """Возвращает промпт для указанной персоны"""
        if persona not in self._prompts:
            logger.error(f"Промпт для персоны '{persona}' не найден")
            raise ValueError(f"Промпт для персоны '{persona}' не найден")
        return self._prompts[persona]
    
    def reload_prompts(self):
        """Перезагружает все промпты"""
        logger.info("Перезагрузка промптов...")
        self._prompts.clear()
        self._load_prompts()
    
    def validate_prompts(self) -> bool:
        """Проверяет, что все необходимые промпты загружены"""
        required_personas = ["aeris", "luneth"]
        missing = [p for p in required_personas if p not in self._prompts]
        
        if missing:
            logger.error(f"Отсутствуют промпты для персон: {missing}")
            return False
        
        return True
    
    @property
    def available_personas(self) -> list:
        """Возвращает список доступных персон"""
        return list(self._prompts.keys())

# Глобальный экземпляр
prompt_manager = PromptManager()

def get_system_prompt(persona: str) -> str:
    """Удобная функция для получения системного промпта"""
    return prompt_manager.get_prompt(persona)