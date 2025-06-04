import logging
import time
import sys
import json
import asyncio
import os
import aiohttp
import openpyxl
import hashlib
from typing import List
from fastapi import FastAPI, File, UploadFile, HTTPException, WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from logging.handlers import RotatingFileHandler
from ai_data_extraction import ChatGPTClient, extract_metadata_and_scores, extract_criteria
from manual_data_extraction import extract_text_from_file, adjust_scores_with_rules, extract_metadata_fallback
from starlette.websockets import WebSocket, WebSocketState
import matplotlib.pyplot as plt

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

file_handler = RotatingFileHandler('app.log', maxBytes=10*1024*1024, backupCount=5)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

logger.info("Starting FastAPI server")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

chatgpt_client = ChatGPTClient()

active_websockets = []

# Функция для склонения слова "проект"
def decline_projects(count: int) -> str:
    if count % 100 in [11, 12, 13, 14]:
        return f"{count} проектов"
    if count % 10 == 1:
        return f"{count} проект"
    if count % 10 in [2, 3, 4]:
        return f"{count} проекта"
    return f"{count} проектов"

# Функция для склонения слова "балл"
def decline_points(score: int) -> str:
    if score % 100 in [11, 12, 13, 14]:
        return f"{score} баллов"
    if score % 10 == 1:
        return f"{score} балл"
    if score % 10 in [2, 3, 4]:
        return f"{score} балла"
    return f"{score} баллов"

@app.websocket("/ws/progress")
async def websocket_progress(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if websocket.client_state == WebSocketState.CONNECTED:
            active_websockets.remove(websocket)
            await websocket.close()

async def broadcast_progress(current_step: float, total_steps: float):
    percent = min(round((current_step / total_steps) * 100), 99)
    message = {
        "type": "progress",
        "current_step": current_step,
        "total_steps": total_steps,
        "percent": percent
    }
    for ws in active_websockets:
        try:
            await ws.send_json(message)
        except Exception as e:
            logger.error(f"Ошибка отправки прогресса: {e}")

async def broadcast_complete():
    message = {
        "type": "progress",
        "percent": 100
    }
    for ws in active_websockets:
        try:
            await ws.send_json(message)
        except Exception as e:
            logger.error(f"Ошибка отправки 100% прогресса: {e}")
    
    message = {"type": "complete"}
    for ws in active_websockets:
        try:
            await ws.send_json(message)
        except Exception as e:
            logger.error(f"Ошибка отправки завершения: {e}")

async def log_evaluations(results: List[dict], criteria: List[dict]) -> str:
    logger.info("Добавление в текстовый файл лога оценок")
    os.makedirs("static", exist_ok=True)
    log_path = "static/evaluations_log.txt"
    try:
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"\n---\nЛог оценок (добавлен: {time.strftime('%Y-%m-%d %H:%M:%S')})\n\n")
            for result in results:
                f.write(f"Файл: {result['filename']}\n")
                f.write("Оценки:\n")
                total_score = 0
                for crit in criteria:
                    score = result['scores'].get(crit['name'], 0)
                    f.write(f"  {crit['name']}: {score}\n")
                    total_score += score
                f.write(f"  Итоговый балл: {total_score}\n")
                f.write("Рекомендации:\n")
                for rec in result['recommendations']:
                    f.write(f"  - {rec}\n")
                f.write("\n")
        logger.info(f"Текстовый лог оценок добавлен по пути: {log_path}")
        return log_path
    except Exception as e:
        logger.error(f"Ошибка сохранения лога оценок: {e}")
        raise HTTPException(status_code=500, detail="Ошибка сохранения лога оценок")

async def create_excel_file(results: List[dict], criteria: List[dict]) -> str:
    logger.info("Создание Excel-файла")
    os.makedirs("static", exist_ok=True)
    excel_path = "static/results.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Results"
    headers = ["№", "ФИО", "Класс", "Школа", "Название работы"] + [f"{c['name']} (макс. {c['max_score']})" for c in criteria] + [f"ИТОГО (макс. {sum(c['max_score'] for c in criteria)})"]
    ws.append(headers)
    for idx, result in enumerate(results, 1):
        scores = result['scores']
        total = sum(scores.get(c['name'], 0) for c in criteria)
        row = [
            idx,
            result['metadata']['author'],
            result['metadata']['grade'],
            result['metadata']['school'],
            result['metadata']['title']
        ] + [scores.get(c['name'], 0) for c in criteria] + [total]
        ws.append(row)
    wb.save(excel_path)
    logger.info(f"Excel-файл сохранен по пути: {excel_path}")
    return excel_path

async def create_recommendations_file(results: List[dict]) -> str:
    logger.info("Создание файла рекомендаций")
    os.makedirs("static", exist_ok=True)
    path = "static/recommendations.txt"
    with open(path, 'w', encoding='utf-8') as f:
        for result in results:
            f.write(f"{result['filename']}:\n")
            for rec in result['recommendations']:
                f.write(f"- {rec}\n")
            f.write("\n")
    logger.info(f"Файл рекомендаций сохранен по пути: {path}")
    return path

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    logger.info("Отображение главной страницы")
    try:
        with open("index.html", encoding='utf-8') as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        logger.error("index.html не найден")
        raise HTTPException(status_code=500, detail="Ошибка сервера: файл index.html отсутствует")

@app.get("/api/test-connection")
async def test_api_connection():
    logger.info("Проверка подключения к API")
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}", "Content-Type": "application/json"}
            payload = {
                "model": "gpt-4o-2024-08-06",
                "messages": [{"role": "user", "content": "Проверьте доступность API."}],
                "max_tokens": 10
            }
            async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=10) as response:
                response.raise_for_status()
                logger.info("API доступен")
                return {"status": "success"}
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error(f"Ошибка подключения к API: {e}")
        raise HTTPException(status_code=503, detail="Не удалось подключиться к API. Пожалуйста, проверьте настройки API или попробуйте снова.")
    except Exception as e:
        logger.error(f"Неизвестная ошибка при проверке API: {e}")
        raise HTTPException(status_code=500, detail="Ошибка сервера")

@app.get("/test")
async def test():
    logger.info("Доступ к тестовому эндпоинту")
    return {"message": "Сервер работает"}

@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_json():
    return {"status": "не поддерживается"}

@app.post("/process/", response_class=HTMLResponse)
async def process_files(
    criteria_file: UploadFile = File(...),
    work_files: List[UploadFile] = File(...)
):
    logger.info("Обработка файлов")
    start_time = time.time()
    logger.info(f"Получен файл критериев: {criteria_file.filename}")
    logger.info(f"Получено {len(work_files)} рабочих файлов")

    # Проверка подключения к API
    try:
        await test_api_connection()
    except HTTPException as e:
        logger.error(f"API недоступен перед обработкой: {e.detail}")
        raise

    if not criteria_file:
        raise HTTPException(status_code=400, detail="Требуется файл с критериями")
    if not work_files or len(work_files) > 30:
        raise HTTPException(status_code=400, detail="Загрузите от 1 до 30 рабочих файлов")

    # Проверка размера и формата
    for file in [criteria_file] + work_files:
        if file.size > 50 * 1024 * 1024:
            logger.error(f"Файл {file.filename} превышает 50 МБ")
            raise HTTPException(status_code=400, detail=f"Файл {file.filename} превышает 50 МБ. Пожалуйста, загрузите файл меньшего размера.")
        if file.size == 0:
            logger.error(f"Файл {file.filename} пустой")
            raise HTTPException(status_code=400, detail=f"Файл {file.filename} пустой или не содержит текста. Пожалуйста, загрузите файл с содержимым.")
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ['.docx', '.pdf', '.txt']:
            logger.error(f"Файл {file.filename} имеет неподдерживаемый формат: {ext}")
            raise HTTPException(status_code=400, detail=f"Файл {file.filename} имеет неподдерживаемый формат. Только .docx, .pdf или .txt файлы.")

    # Проверка содержимого и дубликатов
    file_hashes = set()
    try:
        content = await criteria_file.read()
        if not content.strip():
            logger.error(f"Файл критериев {criteria_file.filename} пустой (нет содержимого)")
            raise HTTPException(status_code=400, detail=f"Файл критериев {criteria_file.filename} пустой или не содержит текста. Пожалуйста, загрузите файл с содержимым.")
        text = extract_text_from_file(criteria_file)
        if not text.strip():
            logger.error(f"Файл критериев {criteria_file.filename} не содержит текста")
            raise HTTPException(status_code=400, detail=f"Файл критериев {criteria_file.filename} пустой или не содержит текста. Пожалуйста, загрузите файл с содержимым.")
        file_hash = hashlib.md5(content).hexdigest()
        if file_hash in file_hashes:
            logger.error(f"Обнаружен дубликат файла критериев: {criteria_file.filename}")
            raise HTTPException(status_code=400, detail=f"Обнаружен дубликат файла: {criteria_file.filename}. Пожалуйста, загрузите уникальный файл.")
        file_hashes.add(file_hash)
        await criteria_file.seek(0)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка проверки файла критериев {criteria_file.filename}: {e}")
        raise HTTPException(status_code=400, detail=f"Файл критериев {criteria_file.filename} пустой или не содержит текста. Пожалуйста, загрузите файл с содержимым.")

    for work_file in work_files:
        try:
            content = await work_file.read()
            if not content.strip():
                logger.error(f"Файл работы {work_file.filename} пустой (нет содержимого)")
                raise HTTPException(status_code=400, detail=f"Файл {work_file.filename} пустой или не содержит текста. Пожалуйста, загрузите файл с содержимым.")
            text = extract_text_from_file(work_file)
            if not text.strip():
                logger.error(f"Файл работы {work_file.filename} не содержит текста")
                raise HTTPException(status_code=400, detail=f"Файл {work_file.filename} пустой или не содержит текста. Пожалуйста, загрузите файл с содержимым.")
            file_hash = hashlib.md5(content).hexdigest()
            if file_hash in file_hashes:
                logger.error(f"Обнаружен дубликат файла работы: {work_file.filename}")
                raise HTTPException(status_code=400, detail=f"Обнаружен дубликат файла: {work_file.filename}. Пожалуйста, загрузите уникальный файл.")
            file_hashes.add(file_hash)
            await work_file.seek(0)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Ошибка проверки файла работы {work_file.filename}: {e}")
            raise HTTPException(status_code=400, detail=f"Файл {work_file.filename} пустой или не содержит текста. Пожалуйста, загрузите файл с содержимым.")

    criteria_data = await extract_criteria(criteria_file, chatgpt_client)
    criteria = criteria_data['criteria']
    if not criteria:
        logger.error("Критерии не найдены в файле")
        raise HTTPException(status_code=400, detail="Критерии не найдены")

    results = []
    total_files = len(work_files)
    steps_per_file = 10
    total_steps = total_files * steps_per_file

    for idx, work_file in enumerate(work_files, 1):
        try:
            file_start_time = time.time()
            logger.info(f"Начало обработки файла: {work_file.filename}")

            for step in range(steps_per_file):
                current_step = (idx - 1) * steps_per_file + step + 1
                await broadcast_progress(current_step, total_steps)
                await asyncio.sleep(0.1)

            text = extract_text_from_file(work_file)
            metadata_scores = await extract_metadata_and_scores(text, work_file.filename, criteria, chatgpt_client)

            adjusted_scores = adjust_scores_with_rules(text, metadata_scores["scores"], criteria)
            metadata_scores["scores"] = adjusted_scores

            results.append({
                "filename": work_file.filename,
                "metadata": metadata_scores["metadata"],
                "scores": metadata_scores["scores"],
                "recommendations": metadata_scores["recommendations"]
            })
            file_end_time = time.time()
            logger.info(f"Файл {work_file.filename} обработан за {file_end_time - file_start_time:.2f} секунд")
        except Exception as e:
            logger.error(f"Не удалось обработать {work_file.filename}: {e}")
            continue

    if not results:
        logger.error("Нет обработанных рабочих файлов")
        raise HTTPException(status_code=400, detail="Нет обработанных рабочих файлов")

    await log_evaluations(results, criteria)
    await broadcast_complete()

    excel_path = await create_excel_file(results, criteria)
    rec_path = await create_recommendations_file(results)

    # Подсчет распределения итоговых баллов
    score_distribution = {}
    for r in results:
        total = sum(r['scores'].get(c['name'], 0) for c in criteria)
        score_distribution[total] = score_distribution.get(total, 0) + 1

    logger.info(f"Score distribution: {score_distribution}")

    # Подготовка данных для графика
    total_projects = len(results)
    chart_labels = []
    chart_data = []
    chart_colors = []
    score_counts = []
    tooltip_texts = []
    color_classes = []
    color_map = {
        27: 'color-1',  # #8faafc
        28: 'color-2',  # #b5f57a
        12: 'color-3',  # #f09dc5
    }
    default_colors = ['color-4', 'color-5', 'color-6', 'color-7', 'color-8', 'color-9', 'color-10', 'color-11', 'color-12']
    color_index = 0

    # Синхронизируем цвета с styles.css
    color_hex_map = {
        'color-1': '#8faafc',
        'color-2': '#b5f57a',
        'color-3': '#f09dc5',
        'color-4': '#fddb7c',
        'color-5': '#8cfddf',
        'color-6': '#d9aee9',
        'color-7': '#fd89de',
        'color-8': '#aeebae',
        'color-9': '#fdc074',
        'color-10': '#abe1f4',
        'color-11': '#f6a9a9',
        'color-12': '#b4b4ed'
    }

    for score, count in sorted(score_distribution.items()):
        chart_labels.append(str(score))
        percentage = (count / total_projects) * 100
        chart_data.append(count)
        score_counts.append(count)
        project_text = decline_projects(count).split()[-1]  # Берем только слово "проектов"
        points_text = decline_points(score).split()[-1]    # Берем только слово "баллов"
        tooltip_texts.append(f"{score} {points_text} - {count} {project_text}, {percentage:.1f}%")
        if score in color_map:
            color_classes.append(color_map[score])
            chart_colors.append(color_hex_map[color_map[score]])
        else:
            color_classes.append(default_colors[color_index % len(default_colors)])
            chart_colors.append(color_hex_map[default_colors[color_index % len(default_colors)]])
            color_index += 1

    logger.info(f"Chart labels: {chart_labels}")
    logger.info(f"Chart data: {chart_data}")
    logger.info(f"Chart colors: {chart_colors}")
    logger.info(f"Score counts: {score_counts}")
    logger.info(f"Legend data: labels={chart_labels}, counts={score_counts}, tooltip_texts={tooltip_texts}")

    # Генерация круговой диаграммы с Matplotlib
    try:
        plt.figure(figsize=(6, 6), facecolor='none')
        plt.pie(chart_data, labels=None, colors=chart_colors, autopct='%1.1f%%', startangle=90, textprops={'color': 'white', 'weight': 'bold'})
        plt.axis('equal')
        plt.tight_layout(pad=0)
        chart_path = "static/chart.png"
        plt.savefig(chart_path, bbox_inches='tight', dpi=150, transparent=True)
        plt.close()
        logger.info(f"График сохранён по пути: {chart_path}")
    except Exception as e:
        logger.error(f"Ошибка при создании графика с Matplotlib: {e}")
        raise HTTPException(status_code=500, detail="Ошибка при создании графика")

    html = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Результаты оценки</title>
        <link rel="stylesheet" href="/static/styles.css">
    </head>
    <body>
        <div class="container">
            <h1>Результаты оценки</h1>
            <div class="chart-container">
                <div class="chart-wrapper">
                    <h2>Распределение итоговых баллов</h2>
                    <img id="scoreChart" src="/static/chart.png" alt="Распределение баллов" title="Распределение итоговых баллов">
                </div>
                <div class="chart-legend" id="chartLegend">
    """
    # Генерация легенды
    html += """
            <script>
                document.getElementById('chartLegend').innerHTML = ''; // Очистка контейнера
            </script>
    """
    seen_labels = set()  # Защита от дублирования
    for idx, label in enumerate(chart_labels):
        if label not in seen_labels:
            seen_labels.add(label)
            project_text = decline_projects(score_counts[idx]).split()[-1]  # Берем только "проектов"
            points_text = decline_points(int(label)).split()[-1]           # Берем только "баллов"
            legend_text = f"{label} {points_text} - {score_counts[idx]} {project_text}"
            logger.info(f"Legend item {idx}: {legend_text}")
            html += f"""
                        <div class="legend-item" data-legend-id="legend-{idx}">
                            <span class="legend-color {color_classes[idx]}" title="{tooltip_texts[idx]}"></span>
                            <span class="legend-text" data-legend-text="{legend_text}">{legend_text}</span>
                        </div>
        """
    html += """
                </div>
            </div>
            <div class="table-container">
                <table>
                    <tr>
                        <th>№</th>
                        <th>ФИО</th>
                        <th>Класс</th>
                        <th>Школа</th>
                        <th>Название работы</th>
    """
    for c in criteria:
        html += f'                    <th>{c["name"]} (макс. {c["max_score"]})</th>\n'

    max_total_score = sum(c['max_score'] for c in criteria)
    html += f'                    <th>ИТОГО (макс. {max_total_score})</th>\n'
    html += '                    <th>Рекомендации</th>\n'
    html += '                </tr>\n'

    for idx, r in enumerate(results, 1):
        total = sum(r['scores'].get(c['name'], 0) for c in criteria)
        html += f"""
                    <tr>
                        <td>{idx}</td>
                        <td>{r['metadata']['author']}</td>
                        <td>{r['metadata']['grade']}</td>
                        <td>{r['metadata']['school']}</td>
                        <td>{r['metadata']['title']}</td>
        """
        for c in criteria:
            html += f"                    <td>{r['scores'].get(c['name'], 0)}</td>\n"
        html += f"""
                        <td>{total}</td>
                        <td>{'; '.join(r['recommendations'])}</td>
                    </tr>
        """

    html += """
                </table>
            </div>
            <div class="links">
                <a href="/static/results.xlsx" class="button" download>Скачать Excel</a>
                <a href="/static/recommendations.txt" class="button" download>Скачать рекомендации</a>
            </div>
            <div class="back-link-container">
                <a href="/" class="back-link">Назад</a>
            </div>
            <script>
                // Защита от дублирования текста в легенде
                document.addEventListener('DOMContentLoaded', function() {
                    const legendItems = document.querySelectorAll('.legend-item');
                    legendItems.forEach(item => {
                        const textSpan = item.querySelector('.legend-text');
                        const originalText = textSpan.getAttribute('data-legend-text');
                        if (textSpan.innerText !== originalText) {
                            textSpan.innerText = originalText; // Восстанавливаем оригинальный текст
                        }
                    });
                });
            </script>
        </div>
    </body>
    </html>
    """
    end_time = time.time()
    logger.info(f"Обработка заняла {end_time - start_time:.2f} сек")
    return HTMLResponse(content=html)