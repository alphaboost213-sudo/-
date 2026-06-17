# ImgUniq — уникализатор изображений

Сервис принимает ссылку на изображение и выдаёт постоянный URL вида `/img/<id>`. При каждом открытии этого URL исходное изображение скачивается заново и отдаётся как новая JPEG-версия с небольшими техническими изменениями: шум, микросдвиги яркости/контраста, лёгкий blur/sharpen, микрокроп и изменение насыщенности.

## Что изменено в v2

- Постоянное хранилище на SQLite вместо памяти процесса.
- Работает корректно с несколькими gunicorn workers.
- Поддерживает Railway Volume через `RAILWAY_VOLUME_MOUNT_PATH` или явный `DATABASE_PATH`.
- Блокирует локальные, приватные и внутренние IP-адреса, чтобы не превращаться в SSRF-прокси.
- Ограничивает размер файла, число пикселей, редиректы и batch-обработку.
- HTML, CSS и JS вынесены в `templates/` и `static/`.
- Добавлены `/health` и расширенная статистика `/api/stats`.

## Локальный запуск

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -U pip
pip install -r requirements.txt
python main.py
```

После запуска открой `http://localhost:8080`.

## Деплой на Railway

1. Загрузи папку в GitHub-репозиторий.
2. В Railway создай проект через `Deploy from GitHub repo`.
3. Добавь переменные при необходимости:
   - `APP_BASE_URL=https://твой-домен.railway.app` — фиксирует публичный домен в создаваемых ссылках.
   - `WEB_CONCURRENCY=2` — число workers.
   - `MAX_IMAGE_BYTES=12582912` — лимит входного изображения, по умолчанию 12 MB.
   - `MAX_IMAGE_PIXELS=25000000` — защита от огромных изображений.
   - `MAX_BATCH_SIZE=50` — лимит ссылок за один batch.

## Railway Volume

Если хочешь, чтобы ссылки переживали redeploy/restart, добавь Railway Volume и примонтируй его в сервис. Приложение автоматически использует путь из `RAILWAY_VOLUME_MOUNT_PATH` и создаст там `imguniq.sqlite3`.

Если нужен явный путь, задай:

```bash
DATABASE_PATH=/data/imguniq.sqlite3
```

Без volume SQLite-файл будет создан в папке `data/`, но на Railway такое хранилище может исчезать при пересоздании окружения.

## API

### `POST /api/register`

```json
{ "url": "https://example.com/photo.jpg" }
```

Ответ:

```json
{
  "success": true,
  "id": "abc123",
  "unique_url": "https://service/img/abc123",
  "source_url": "https://example.com/photo.jpg"
}
```

### `POST /api/register-batch`

```json
{ "urls": ["https://example.com/1.jpg", "https://example.com/2.jpg"] }
```

### `GET /img/<id>`

Возвращает JPEG с заголовками `no-cache`, чтобы каждый запрос рендерился заново.

### `GET /api/stats`

Возвращает количество созданных ссылок, число рендеров и текущий путь к SQLite.

## Файлы

```text
main.py                Flask API, SQLite, загрузка и уникализация изображений
templates/index.html   интерфейс
static/app.css         стили
static/app.js          клиентская логика
requirements.txt       зависимости
Procfile               запуск для Railway
railway.toml           конфиг Railway
```
