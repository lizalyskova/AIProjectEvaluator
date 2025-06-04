import os
import asyncio
import aiohttp
import re
import json
import logging
import hashlib
from typing import List, Dict
from dotenv import load_dotenv
from fastapi import HTTPException
from cachetools import LRUCache
import aiofiles
from manual_data_extraction import extract_metadata_fallback, extract_criteria_fallback, evaluate_work_fallback, generate_recommendations_fallback

# Загрузка переменных окружения
load_dotenv()

# Получение API-ключа
API_KEY = os.getenv("OPENAI_API_KEY")
if not API_KEY:
    logging.error("OPENAI_API_KEY не найден в переменных окружения")
    raise ValueError("OPENAI_API_KEY не настроен")
API_URL = "https://api.openai.com/v1/chat/completions"

class ChatGPTClient:
    def __init__(self):
        self.logger = logging.getLogger(__name__)  # Определяем logger для класса
        self.headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
        self.semaphore = asyncio.Semaphore(2)  # Ограничение на параллельные запросы
        self.cache = LRUCache(maxsize=1000)  # LRU-кэш на 1000 записей
        self.cache_file = "cache.json"
        self.max_cache_size_bytes = 10 * 1024 * 1024  # 10 МБ
        self.current_cache_size_bytes = 0  # Текущий размер кэша в байтах
        self.load_cache()  # Загружаем кэш при инициализации

    def load_cache(self):
        """Загружаем кэш из файла при старте."""
        try:
            with open(self.cache_file, 'r') as f:
                data = json.load(f)
                for key, value in data.items():
                    value_size = len(json.dumps(value).encode('utf-8'))
                    if self.current_cache_size_bytes + value_size <= self.max_cache_size_bytes:
                        self.cache[key] = value
                        self.current_cache_size_bytes += value_size
                    else:
                        self.logger.warning(f"Кэш заполнен, пропускаем запись {key}")
                        break
            self.logger.info(f"Кэш загружен, текущий размер: {self.current_cache_size_bytes} байт")
        except FileNotFoundError:
            self.logger.info("Файл кэша не найден, создаём новый")
        except Exception as e:
            self.logger.error(f"Ошибка загрузки кэша: {e}")

    async def save_cache(self):
        """Сохраняем кэш в файл асинхронно."""
        try:
            async with aiofiles.open(self.cache_file, 'w') as f:
                await f.write(json.dumps(dict(self.cache)))
            self.logger.info(f"Кэш сохранён, размер: {self.current_cache_size_bytes} байт")
        except Exception as e:
            self.logger.error(f"Ошибка сохранения кэша: {e}")

    async def query(self, prompt: str, text: str, max_tokens: int = 2000, retries: int = 3) -> dict:
        """Выполняем запрос к API с кэшированием."""
        # Улучшенное хэширование
        # Формируем ключ только на основе текста, чтобы он не зависел от изменений в промпте
        cache_key = hashlib.sha256(text[:2000].encode('utf-8')).hexdigest()
        if cache_key in self.cache:
            self.logger.info(f"Использован кэшированный результат для ключа {cache_key}")
            return self.cache[cache_key]

        async with self.semaphore:
            async with aiohttp.ClientSession() as session:
                for attempt in range(retries):
                    try:
                        payload = {
                            "model": "gpt-4o-2024-08-06",
                            "messages": [
                                {"role": "system", "content": "Вы эксперт по извлечению данных и анализу."},
                                {"role": "user", "content": f"{prompt}\n\nТекст:\n{text}"}
                            ],
                            "max_tokens": max_tokens
                        }
                        async with session.post(API_URL, headers=self.headers, json=payload, timeout=30) as response:
                            response.raise_for_status()
                            response_json = await response.json()
                            self.logger.info(f"ChatGPT API ответ: {response_json}")

                            # Подсчитываем размер ответа
                            response_size = len(json.dumps(response_json).encode('utf-8'))
                            if self.current_cache_size_bytes + response_size <= self.max_cache_size_bytes:
                                self.cache[cache_key] = response_json
                                self.current_cache_size_bytes += response_size
                                await self.save_cache()  # Сохраняем кэш на диск
                            else:
                                self.logger.warning(f"Кэш заполнен, размер: {self.current_cache_size_bytes} байт, ответ не сохранён")

                            await asyncio.sleep(1)  # Задержка для соблюдения лимитов API
                            return response_json
                    except aiohttp.ClientResponseError as e:
                        if e.status == 429:
                            self.logger.warning(f"Ошибка 429: слишком много запросов, попытка {attempt+1}")
                            await asyncio.sleep(2 ** attempt * 10)
                        else:
                            self.logger.warning(f"ChatGPT API попытка {attempt+1} не удалась: {e}")
                            if attempt == retries - 1:
                                self.logger.error("ChatGPT API не удался после всех попыток")
                                return None
                            await asyncio.sleep(2 ** attempt)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        self.logger.warning(f"ChatGPT API попытка {attempt+1} не удалась: {e}")
                        if attempt == retries - 1:
                            self.logger.error("ChatGPT API не удался после всех попыток")
                            return None
                        await asyncio.sleep(2 ** attempt)
                return None

async def extract_metadata_and_scores(text: str, filename: str, criteria: List[dict], chatgpt_client: ChatGPTClient) -> dict:
    logger = logging.getLogger(__name__)
    logger.info(f"Извлечение метаданных, оценок и рекомендаций для {filename}")
    criteria_str = json.dumps(criteria, ensure_ascii=False)
    prompt = f"""
Вы — эксперт по оценке школьных проектов. Извлеките метаданные, оцените проект и предложите рекомендации на русском языке. Проекты создаются учащимися 8–11 классов и могут быть техническими (модели, устройства), гуманитарными (опросы, анализ) или исследовательскими (эксперименты, обзоры). Верните JSON:
{{
    "metadata": {{
        "author": "имя_или_имена",
        "grade": "класс",
        "school": "школа",
        "title": "название"
    }},
    "scores": {{ "критерий1": {{"score": балл, "reason": "обоснование"}}, ... }},
    "recommendations": ["предложение 1", "предложение 2"]
}}
Если метаданные не найдены, используйте "Unknown".

### Извлечение метаданных:
1. Ищите:
   - Автор: "Выполнил", "Автор", "Ученик", "Разработчики".
   - Класс: "класс", "Ученик", "группа" (нормализуйте: "10 класс", "10а").
   - Школа: "МБОУ", "Лицей", "Школа", "СОШ".
   - Название: "Тема", "Проект", заголовок.
2. Авторов объединяйте через ", " или "и". Если данных нет, используйте "Unknown".
3. Автор может быть указан без ключевых слов, например, Карпуков Г.

### Оценка критериев:
Оцените проект по критериям: {criteria_str}. Выставляйте целые баллы от 1 до max_score. Обоснуйте оценку кратко, опираясь на текст. Без доказательств ставьте 1. Типичный проект: 70–85% max_score. Баллы ≥ 90% max_score редки, требуют выдающихся результатов. Для max_score = 1–3: 1 балл за слабое выполнение. Максимальный общий балл: {sum(c['max_score'] for c in criteria)}.

#### Уровни выполнения:
- **Минимально** (1 балл): Критерий упомянут без результатов.
- **Хорошо** (75–85% max_score): Полное выполнение, чёткие результаты.
- **Отлично** (≥ 90% max_score): Выдающиеся результаты.

#### Правила:
- max_score = 1: 1 балл.
- max_score = 2: 1 (минимально), 2 (хорошо/отлично).
- max_score = 3: 1 (минимально), 2 (хорошо), 3 (отлично).
- max_score ≥ 4: 1 (минимально), пропорционально уровням.

### Рекомендации:
Предложите 2 конкретных, достижимых улучшения для школьников, связанных с проектом (методология, оформление, применение). Каждое — до 30 слов. Учитывайте тип проекта и уровень учащихся. Формат: ["предложение 1", "предложение 2"].

### Примеры:
- Технический: ["Добавьте датчик для автоматизации модели.", "Увеличьте ячейки для точности эксперимента."]
- Гуманитарный: ["Проведите опрос в другой школе.", "Добавьте диаграммы для наглядности."]

### Инструкции:
- Учитывайте школьный контекст: ограниченные ресурсы, образовательные цели.
- Ссылайтесь на текст в обоснованиях.
- Обязательно оцените каждый критерий
- Учитывайте типичный балл
- Не ставьте нули
- Сохраните JSON-формат.
"""
    response = await chatgpt_client.query(prompt, text, max_tokens=2000)  # Раскомментировать для теста
    result = {
        "metadata": {"author": "Unknown", "grade": "Unknown", "school": "Unknown", "title": "Unknown"},  # Без extract_metadata_fallback
        "scores": evaluate_work_fallback(criteria),
        "recommendations": generate_recommendations_fallback(filename)
    }
   
    if response and 'choices' in response and response['choices']:
        try:
            content = response['choices'][0]['message']['content']
            content = re.sub(r'^```json\s*|\s*```$', '', content, flags=re.MULTILINE).strip()
            parsed_result = json.loads(content)

           
            if "metadata" in parsed_result and isinstance(parsed_result["metadata"], dict):
                result["metadata"] = parsed_result["metadata"]

           
            if "scores" in parsed_result and isinstance(parsed_result["scores"], dict):
                scores = parsed_result["scores"]
                for crit in criteria:
                    crit_name = crit['name']
                    max_score = crit['max_score']
                    if crit_name in scores and isinstance(scores[crit_name], dict):
                        score_entry = scores[crit_name]
                        score = score_entry.get("score", 0)
                        reason = score_entry.get("reason", "Причина не указана")
                        if not isinstance(score, int) or score < 0 or score > max_score:
                            logger.warning(f"Недопустимый балл для {crit_name} в {filename}: {score} (максимум {max_score})")
                            score = min(max(0, score), max_score)
                            reason = f"Исправлено из-за недопустимого значения. {reason}"
                        result["scores"][crit_name] = score if score > 0 else result["scores"][crit_name]
                        logger.info(f"Оценка для '{crit_name}' в {filename}: {result['scores'][crit_name]}, причина: {reason}")
                    else:
                        logger.warning(f"Критерий '{crit_name}' отсутствует в ответе GPT-4o для {filename}")

          
            if "recommendations" in parsed_result and isinstance(parsed_result["recommendations"], list):
                recommendations = parsed_result["recommendations"]
                if (len(recommendations) == 2 and
                    all(isinstance(rec, str) and rec.strip() for rec in recommendations)):
                    total_words = sum(len(rec.split()) for rec in recommendations)
                    valid_sentences = all(len(re.split(r'[.!?]', rec)) >= 2 for rec in recommendations)
                    if total_words <= 60 and valid_sentences:
                        result["recommendations"] = recommendations
                    else:
                        logger.warning(f"Недопустимый формат рекомендаций для {filename}: {total_words} слов или неверное число предложений")
                else:
                    logger.warning(f"Недопустимый формат рекомендаций для {filename}")

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Ошибка извлечения данных через ChatGPT для {filename}: {e}")
    else:
        logger.warning(f"ChatGPT API не вернул корректный ответ для {filename}")

 
    missing_criteria = sum(1 for crit in criteria if crit['name'] not in result["scores"])
    if missing_criteria > 0:
        logger.error(f"Не оценено {missing_criteria} критериев для {filename}")
   
    logger.info(f"Оценено {len(result['scores'])}/{len(criteria)} критериев для {filename}")
    return result

async def extract_criteria(file, chatgpt_client: ChatGPTClient) -> dict:
    logger = logging.getLogger(__name__)
    logger.info("Извлечение критериев")
    from manual_data_extraction import extract_text_from_file
    text = extract_text_from_file(file)
    if not text.strip():
        raise HTTPException(status_code=400, detail="Файл с критериями пустой")
    prompt = """
    Извлеките критерии оценки и их максимальные баллы из текста. Верните JSON в формате:
    {
        "criteria": [
            {"name": "критерий1", "max_score": число},
            {"name": "критерий2", "max_score": число},
            ...
        ],
        "max_total_score": общее_число
    }
    Ищите критерии в тексте файла, обращая внимание на ключевые слова, такие как "Критерии", "Оценка", "Максимальный балл", "Баллы". 
    Если критерии не найдены, верните пустой список критериев и max_total_score равный 0.
    Убедитесь, что названия критериев и максимальные баллы извлечены точно из текста, а не из заранее заданного шаблона.
    """
    response = await chatgpt_client.query(prompt, text)  
   
    if response and 'choices' in response and response['choices']:
        try:
            content = response['choices'][0]['message']['content']
            content = re.sub(r'^```json\s*|\s*```$', '', content, flags=re.MULTILINE).strip()
            criteria_data = json.loads(content)
            logger.info(f"Извлечены критерии: {criteria_data}")
           
            total_score = sum(c['max_score'] for c in criteria_data.get('criteria', []))
            if criteria_data.get('max_total_score', 0) != total_score:
                logger.warning(f"Расхождение в max_total_score: указано {criteria_data.get('max_total_score')}, рассчитано {total_score}")
                criteria_data['max_total_score'] = total_score
            return criteria_data
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Ошибка извлечения критериев через ChatGPT: {e}")
  
    return extract_criteria_fallback(text)