import re
import logging
from typing import List, Dict
from fastapi import UploadFile, HTTPException
from docx import Document
from PyPDF2 import PdfReader
from pathlib import Path

def extract_text_from_file(file: UploadFile) -> str:
    logger = logging.getLogger(__name__)
    logger.info(f"Извлечение текста из {file.filename}")
    try:
        content = file.file.read()
        ext = Path(file.filename).suffix.lower()
        logger.info(f"Расширение файла: {ext}")
        if ext == '.txt':
            return content.decode('utf-8')
        elif ext == '.docx':
            doc = Document(file.file)
            text = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
            logger.info(f"Длина извлеченного текста из .docx: {len(text)}")
            return text
        elif ext == '.pdf':
            reader = PdfReader(file.file)
            text = "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
            logger.info(f"Длина извлеченного текста из .pdf: {len(text)}")
            return text if text.strip() else ""
        else:
            raise ValueError(f"Неподдерживаемый формат файла: {ext}")
    except Exception as e:
        logger.error(f"Ошибка при извлечении текста из {file.filename}: {e}")
        raise HTTPException(status_code=400, detail=f"Не удалось обработать {file.filename}: {str(e)}")

def extract_metadata_fallback(text: str, filename: str) -> dict:
    logger = logging.getLogger(__name__)
    logger.info(f"Извлечение метаданных из {filename}")

    logger.debug(f"Первые 500 символов текста: {text[:500]}")

    metadata = {"author": "Unknown", "grade": "Unknown", "school": "Unknown", "title": "Unknown"}

    search_text = text[:2000] if len(text) > 2000 else text
    search_text = re.sub(r'\s+', ' ', search_text.strip()).replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
    text_lower = search_text.lower()

    author_patterns = [
        r'(?:Выполнил[аи]?:?\s*)([А-Я][а-я]+(?:\s+[А-Я]\.\s*[А-Я]\.?|\s+[А-Я][а-я]+(?:\s+[А-Я][а-я]+)?))',  
        r'(?:Автор[ы]?\s*проекта:?\s*)([А-Я][а-я]+(?:\s+[А-Я][а-я]+)?(?:,\s*[А-Я][а-я]+(?:\s+[А-Я][а-я]+)?)*)',  
        r'(?:Автор\s*работы:?\s*)([А-Я][а-я]+(?:\s+[А-Я][а-я]+(?:\s+[А-Я][а-я]+)?)?)',  
        r'(?:обучающийся|ученик|ученица)\s*[^:]*?\s*([А-Я][а-я]+(?:\s+[А-Я][а-я]+(?:\s+[А-Я][а-я]+)?)?)'  
    ]
    for pattern in author_patterns:
        match = re.search(pattern, search_text, re.IGNORECASE)
        if match:
            author = match.group(1).strip()
            if not any(s in author.lower() for s in ['мбоу', 'лицей', 'сош', 'гимназия', 'школа', 'центр', 'оош', 'проект', 'ученик', 'ученица', 'класс']):
                metadata['author'] = author
                logger.info(f"Извлечен автор: {metadata['author']} из текста")
                break

    grade_patterns = [
        r'(?:Ученик|Учащийся|Обучающийся|ученица)\s*(\b(5|6|7|8|9|10|11)\b\s*(?:[а-дА-Г]?\s*(?:класса|класс|«[А-Г]»\s*класс)?))',  
        r'(\b(5|6|7|8|9|10|11)\b\s*(?:[а-дА-Г]?\s*(?:класс|кл\.|класса|«[А-Г]»\s*класс)?))',  
        r'(\b(5|6|7|8|9|10|11)\b\s*технологический\s*класс)',  
        r'(\b(5|6|7|8|9|10|11)\b\s*класса)'  
    ]
    for pattern in grade_patterns:
        match = re.search(pattern, search_text[:500], re.IGNORECASE)  
        if match:
            grade = match.group(1).strip()
            if not re.search(r'(?:глава|раздел|страница|пункт|оглавление)\s*' + re.escape(match.group(0)), search_text, re.IGNORECASE):
                metadata['grade'] = grade
                logger.info(f"Извлечен класс: {metadata['grade']} из текста")
                break
            else:
                logger.debug(f"Пропущен класс '{match.group(0)}' — вероятно, из оглавления")

   
    school_patterns = [
        r'(?:МБОУ|МБУ\s*DO)\s*«[^»]{1,300}»',
        r'(?:МБОУ|Школа|СОШ|Лицей|Гимназия|Центр)\s*(?:№\s*\d+)?\s*[^\n"]{1,300}', 
        r'[А-Я][а-я]+\s*(?:лицей|гимназия|школа|оош)\s*(?:г\.\s*[А-Я][а-я]+)?', 
        r'(?:Школа|Лицей)\s*им(?:ени)?\.\s*[А-Я][а-я]+(?:\s+[А-Я][а-я]+)?',  
        r'(?:Школа|Лицей|ООШ)\s*(?:поселка|села)\s*[А-Я][а-я]+(?:\s+[А-Я][а-я]+)?' 
    ]
    for pattern in school_patterns:
        match = re.search(pattern, search_text, re.IGNORECASE)
        if match:
            school = match.group(0).strip()
            school = re.sub(r'Городского округа Шатура|Московской области|»\s*[^\n"]+|г\.о\.\s*Шатура[^\n"]*', '', school, flags=re.IGNORECASE).strip()
            school = re.sub(r'Муниципальное бюджетное общеобразовательное учреждение\s*', 'МБОУ ', school, flags=re.IGNORECASE).strip()
            metadata['school'] = school
            logger.info(f"Извлечена школа: {metadata['school']} из текста")
            break

   
    title_patterns = [
        r'(?:Тема|Название|Проект)[:\s]*[""]([^""]{1,100})[""]', 
        r'(?:Исследовательский\s*проект|Технический\s*проект|Научная\s*работа|Практико-ориентированный\s*проект)[:\s]*["«]([^»"]{1,100})["»]',  
        r'(?:Тема\s*проекта|Тема\s*исследования|Тема\s*проектно-исследовательской\s*работы|Проект\s*по\s*[а-я]+?\s*на\s*тему|Учебный\s*проект\s*по\s*теме)[:\s]*["«]([^»"]{1,100})["»]',  
        r'^(?:Проект\s*на\s*тему\s*:?\s*)([^\n]{1,100})(?:\n|$)'  
    ]
    for pattern in title_patterns:
        match = re.search(pattern, search_text, re.IGNORECASE)
        if match:
            metadata['title'] = match.group(1).strip()
            logger.info(f"Извлечено название: {metadata['title']} из текста")
            break

   
    if metadata['author'] == "Unknown" or metadata['grade'] == "Unknown" or metadata['school'] == "Unknown":
        filename_parts = Path(filename).stem.split('_')
        if len(filename_parts) >= 2:
            if metadata['author'] == "Unknown" and not any(s in filename_parts[0].lower() for s in ['проект', 'сош', 'лицей', 'мбоу', 'оош', 'центр']):
                metadata['author'] = filename_parts[0].strip()
            if metadata['grade'] == "Unknown" and re.match(r'\b(5|6|7|8|9|10|11)\b\s*(?:кл|класс|[а-дА-Г])?', filename_parts[1], re.IGNORECASE):
                metadata['grade'] = filename_parts[1].strip()
            if metadata['school'] == "Unknown" and len(filename_parts) >= 3 and any(s in filename_parts[2].lower() for s in ['лицей', 'сош', 'гимназия', 'школа', 'мбоу', 'центр', 'оош']):
                metadata['school'] = filename_parts[2].strip()
        elif len(filename_parts) == 1 and metadata['author'] == "Unknown":
            if not any(s in filename_parts[0].lower() for s in ['проект', 'сош', 'лицей', 'мбоу', 'оош', 'центр']):
                metadata['author'] = filename_parts[0].strip()
        logger.info(f"Извлечены метаданные из имени файла {filename}: {metadata}")


    if len(metadata['author']) > 100 or not re.match(r'^[А-Яа-я\s,\.\-–&]+$', metadata['author'], re.UNICODE):
        logger.warning(f"Недопустимый формат автора для {filename}: {metadata['author']}")
        metadata['author'] = "Unknown"
    if len(metadata['grade']) > 20 or not re.match(r'^\b(5|6|7|8|9|10|11)\b\s*(?:[а-дА-Г]?\s*(?:класса|класс|кл\.|«[А-Г]»\s*класс)?|технологический\s*класс)?$', metadata['grade'], re.IGNORECASE):
        logger.warning(f"Недопустимый формат класса для {filename}: {metadata['grade']}")
        metadata['grade'] = "Unknown"
    if len(metadata['school']) > 300 or not re.match(r'^[А-Яа-я0-9\s№«»,\.\-–"]+$', metadata['school'], re.UNICODE):
        logger.warning(f"Недопустимый формат школы для {filename}: {metadata['school']}")
        metadata['school'] = "Unknown"
    if len(metadata['title']) > 100:
        logger.warning(f"Слишком длинное название для {filename}: {metadata['title']}")
        metadata['title'] = metadata['title'][:100]

    for key, value in metadata.items():
        if value == "Unknown":
            logger.warning(f"Поле метаданных '{key}' не найдено для {filename}")

    return metadata

def extract_criteria_fallback(text: str) -> dict:
    logger = logging.getLogger(__name__)
    logger.info("Извлечение критериев резервным методом")

    criteria = []
    max_total_score = 0


    text = re.sub(r'\s+', ' ', text.strip()).replace('–', '-').replace('—', '-')

    
    pattern1 = r'(?:\d+\.\s*|[-•]\s*)([А-Яа-я\s,\(\):]+?)\s*(?:0-(\d+)\s*балл[аов]{0,2}|\(0-(\d+)\s*балл[аов]{0,2}\))'
    
    pattern2 = r'([-•]?\s*[А-Яа-я\s,\(\):]+?)\s*\(0-(\d+)\s*балл[аов]{0,2}\)'
   
    pattern3 = r'\|\s*([А-Яа-я\s,\(\):]+?)\s*\|\s*(?:0-(\d+)\s*балл[аов]{0,2})'

    patterns = [pattern1, pattern2, pattern3]

    for pattern in patterns:
        matches = re.finditer(pattern, text, re.IGNORECASE)
        for match in matches:
            name = match.group(1).strip()
            score = int(match.group(2) or match.group(3))
           
            if len(name) > 5 and not any(keyword in name.lower() for keyword in ['итого', 'максимальный', 'комментарий']):
                criteria.append({"name": name, "max_score": score})
                max_total_score += score
                logger.info(f"Извлечен критерий: {name}, max_score: {score}")

   
    if not criteria:
        logger.warning("Критерии не найдены, использование стандартного набора")
        criteria = [
            {"name": "актуальность изобретения, новизна решения", "max_score": 4},
            {"name": "исследовательская составляющая работы", "max_score": 5},
            {"name": "степень самостоятельности в проведении исследования", "max_score": 3},
            {"name": "сложность проекта", "max_score": 7},
            {"name": "практическое применение и социальная значимость", "max_score": 5},
            {"name": "авторский вклад в проект", "max_score": 5},
            {"name": "грамотность и качество оформления работы", "max_score": 3}
        ]
        max_total_score = sum(c["max_score"] for c in criteria)


    total_score_match = re.search(r'(?:Максимальный\s*балл\s*[-–]\s*|\(макс\.\s*балл\s*[-–]\s*)(\d+)', text, re.IGNORECASE)
    if total_score_match:
        parsed_total = int(total_score_match.group(1))
        if parsed_total != max_total_score:
            logger.warning(f"Расхождение в max_total_score: указано {parsed_total}, рассчитано {max_total_score}")
            max_total_score = parsed_total

    return {"criteria": criteria, "max_total_score": max_total_score}

def evaluate_work_fallback(criteria: List[dict]) -> dict:
    logger = logging.getLogger(__name__)
    logger.info("Использование резервной оценки")
    scores = {}
    for c in criteria:
        max_score = c['max_score']
        score = max(1, max_score - 1)
        scores[c['name']] = score
        logger.info(f"Резервный балл для '{c['name']}': {score} из {max_score}")
    return scores

def adjust_scores_with_rules(text: str, scores: Dict[str, int], criteria: List[dict]) -> Dict[str, int]:
    logger = logging.getLogger(__name__)
    text_lower = text.lower()
    adjusted_scores = scores.copy()

    for crit in criteria:
        crit_name = crit['name']
        max_score = crit['max_score']
        current_score = adjusted_scores.get(crit_name, 0)

        if crit_name == "актуальность изобретения, новизна решения":
            keywords = ["новый", "инновационный", "уникальный", "впервые", "актуально", "современно", "проблема", "решение"]
            if any(keyword in text_lower for keyword in keywords):
                adjusted_scores[crit_name] = min(max_score, current_score + 1)
                logger.info(f"Увеличен балл для '{crit_name}' на основе ключевых слов: {adjusted_scores[crit_name]}")
            else:
                adjusted_scores[crit_name] = max(0, current_score - 1)
                logger.info(f"Уменьшен балл для '{crit_name}' из-за отсутствия ключевых слов: {adjusted_scores[crit_name]}")

        elif crit_name == "грамотность и качество оформления работы":
            structure_keywords = ["введение", "заключение", "цель", "задачи", "оглавление"]
            if any(keyword in text_lower for keyword in structure_keywords):
                adjusted_scores[crit_name] = min(max_score, current_score + 1)
                logger.info(f"Увеличен балл для '{crit_name}' на основе структуры: {adjusted_scores[crit_name]}")
            else:
                adjusted_scores[crit_name] = max(0, current_score - 1)
                logger.info(f"Уменьшен балл для '{crit_name}' из-за отсутствия структуры: {adjusted_scores[crit_name]}")

    return adjusted_scores

def generate_recommendations_fallback(filename: str) -> List[str]:
    logger = logging.getLogger(__name__)
    logger.info(f"Резервные рекомендации для {filename}")
    return [
        "Углубите анализ результатов исследования. Это повысит их научную ценность.",
        "Добавьте визуальные элементы для наглядности. Это улучшит восприятие проекта."
    ]