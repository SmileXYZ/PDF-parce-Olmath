# Парсер олимпиадных PDF → JSON + картинки (без нейросетей)

Детерминированный парсер цифровых (не сканы) олимпиадных PDF, собранных LaTeX.
Всё на PyMuPDF: текстовый слой + геометрия глифов и векторных путей. Никаких
LLM в пути парсинга. На выходе — JSON с задачами (условие/ответ/решения/
комментарии) в Markdown+KaTeX и картинки (чертежи) отдельными PNG.

## Что внутри

- `olymp_parse.py` — сам парсер (одна функция `parse(pdf, outdir)`), stdlib + PyMuPDF.
- `service.py` — FastAPI-обёртка (`POST /parse`). По ней «стучим» из n8n/фронта.
- `Dockerfile`, `docker-compose.yml`, `requirements.txt` — деплой (Coolify/любой Docker).
- `olympiad-pdf-pipeline.n8n.json` — воркфлоу n8n: вебхук → микросервис →
  заливка картинок в Supabase Storage → подстановка URL → INSERT задач.
- `schema.sql` — таблица Supabase + bucket.

## Как работает парсер (коротко)

1. **Классификация глифов по шрифту.** CM*/MSAM/… = математика, SF* = кириллица.
   Верхние/нижние индексы — по размеру шрифта (меньше базового) и знаку смещения
   baseline. Юникод-символы CM (·, ∠, √, ⩾, …) → KaTeX-команды.
2. **Выключные формулы — 2D-реконструкция.** Прогон подряд идущих чисто-
   математических строк = один выключной блок. Внутри него горизонтальные
   векторные сегменты = дробные черты: то, что над чертой в её x-диапазоне —
   числитель, под — знаменатель, рекурсивно (вложенные дроби). Результат
   собирается в порядке чтения (по строкам сверху-вниз, внутри строки слева-
   направо) → `\dfrac{…}{…}`. Большие скобки CMEX → `\left( \right)`.
3. **Чертежи.** Кластеризация векторных путей + картинок (мелочь — QED-квадраты,
   дробные черты, overline — отсеивается). Сетка «только ортогональные линии +
   текст внутри» → markdown-таблица. Остальное → рендер страницы (200 dpi) и
   кроп по bbox, с захватом буквенных меток вершин вокруг чертежа.
4. **Сегментация.** Жирные `Задача N.` → задачи. Курсивные `Ответ:/Решение/
   Способ/Комментарий/Оценка/Пример/Замечание` → секции. Автор `(И. Фамилия)` →
   поле `author`. Плейсхолдеры `{{FIG:tN_fK}}` встают в поток по y-координате.

На тестовом ММО-2025 (9 класс): 6 задач, 16 чертежей, **273/273 формулы**
компилируются реальным KaTeX без ошибок.

## API микросервиса

```
GET  /health                         -> {"ok": true, ...}

POST /parse?images=base64|none
  Три способа отдать PDF:
    1) multipart/form-data, поле file=<pdf>        # форма с фронта
    2) Content-Type: application/pdf, тело = байты  # бинарь прямо в тело
    3) Content-Type: application/json {"url":"...", "headers":{...}}  # скачать (Supabase Storage/S3)
  images=base64 (по умолчанию): картинки приходят в figures[].b64
  images=none: только JSON + bbox (картинки не рендерятся — быстрее)
```

Пример ответа:
```json
{
  "source": "9-sol.pdf",
  "base_size": 10,
  "tasks": [{
    "number": 3,
    "author": "М. Евдокимов",
    "statement_md": "...$\\angle CHR = 90^{\\circ}$...\n{{FIG:t3_f4}}\n...",
    "answer_md": "они равны.",
    "solutions": [{"title": "Решение 1", "body_md": "$$\nS_{CQH}+S_{HPR}=...\n$$"}],
    "comments": [],
    "figures": [{"id":"t3_f4","page":2,"bbox":[112.1,400.3,306.9,523.0],
                 "mime":"image/png","b64":"iVBORw0..."}],
    "meta": {}, "verified": false
  }]
}
```

`{{FIG:id}}` остаются в тексте — подстановку реального URL делает n8n после
заливки картинки в Storage (или фронт, если рендерит из base64 напрямую).

### curl-проверка

```bash
# форма
curl -X POST "http://HOST:8080/parse?images=none" -F "file=@9-sol.pdf"
# бинарь
curl -X POST "http://HOST:8080/parse" -H "Content-Type: application/pdf" --data-binary @9-sol.pdf
# по ссылке из Supabase Storage
curl -X POST "http://HOST:8080/parse" -H "Content-Type: application/json" \
     -d '{"url":"https://<proj>.supabase.co/storage/v1/object/authenticated/pdfs/9-sol.pdf",
          "headers":{"Authorization":"Bearer <SERVICE_KEY>"}}'
```

## Деплой (Coolify / Docker)

```bash
docker compose up -d --build     # или через Coolify: Docker Compose, порт 8080
```
Единственная переменная: `MAX_PDF_BYTES` (по умолчанию 40 MB). Внешних
зависимостей у сервиса нет — Supabase-креды живут в n8n, не в парсере.

## n8n

Импортируй `olympiad-pdf-pipeline.n8n.json`. Переменные окружения n8n:
`PARSER_URL` (напр. `http://olymp-parser:8080` в общей docker-сети),
`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.

Вебхук `POST /webhook/olympiad-pdf` принимает:
- **бинарь PDF** (форма с фронта `multipart`, поле `file`, или сырое тело) — ветка «Parser: binary PDF»;
- **JSON `{"url": "..."}`** (файл лежит в Supabase Storage / где угодно) — ветка «Parser: URL».

Дальше: микросервис возвращает JSON+base64 → каждая картинка заливается в
bucket `olympiad-figures/<batch>/<fig_id>.png` → `{{FIG:id}}` меняется на
`![fig](public-url)` → задачи вставляются в `olympiad_tasks` с `verified=false`.

## Что осознанно НЕ делает парсер

- **Темы и метрики сложности** (logical_steps, idea_novelty, …) — их без LLM
  честно не проставить по смыслу задачи. В схеме поля есть, заполняются на
  фронте выверки человеком (или отдельным опциональным LLM-шагом, если захочешь
  — но это уже вне детерминированного ядра). `verified=false` — дефолт под ручную выверку.
- Многострочные `align`-выкладки с ручными разрывами внутри одной дроби —
  редкий пограничный случай; собираются в порядке чтения, глазами проверить стоит.
