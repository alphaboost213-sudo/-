import os
import io
import uuid
import time
import random
import sqlite3
import urllib.request
import subprocess
import tempfile
import secrets
import re
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, render_template_string, after_this_request, session, redirect, url_for
from flask_cors import CORS
from PIL import Image, ImageFilter, ImageEnhance
from werkzeug.security import generate_password_hash, check_password_hash
import numpy as np

app = Flask(__name__)
CORS(app)

# Persistent storage.
# На Railway лучше подключить Volume и задать переменную DATA_DIR=/data.
# Без Volume файл тоже создастся, но после пересборки/рестарта окружения может пропасть.
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
if not DATA_DIR.exists():
    DATA_DIR = Path('.')

DB_PATH = DATA_DIR / 'imguniq.sqlite3'


def get_or_create_secret_key() -> str:
    env_secret = os.environ.get('SECRET_KEY', '').strip()
    if env_secret:
        return env_secret

    secret_file = DATA_DIR / 'secret_key.txt'
    if secret_file.exists():
        secret = secret_file.read_text(encoding='utf-8').strip()
        if secret:
            return secret

    secret = secrets.token_hex(32)
    secret_file.write_text(secret, encoding='utf-8')
    try:
        secret_file.chmod(0o600)
    except OSError:
        pass
    return secret


app.secret_key = get_or_create_secret_key()

# ─── Auth / access control ───────────────────────────────────────────────────
# AUTH_ENABLED=0 можно поставить только для локальной отладки.
AUTH_ENABLED = os.environ.get('AUTH_ENABLED', '1').lower() not in ('0', 'false', 'no', 'off')
ADMIN_LOGIN = os.environ.get('ADMIN_LOGIN', 'admin').strip().lower()
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '').strip()
# По умолчанию готовые /img и /video ссылки публичные, чтобы их могли открыть внешние рендереры.
# Если поставить PUBLIC_MEDIA_LINKS=0, сами готовые ссылки тоже будут требовать вход.
PUBLIC_MEDIA_LINKS = os.environ.get('PUBLIC_MEDIA_LINKS', '1').lower() not in ('0', 'false', 'no', 'off')

# ─── Video defaults ───────────────────────────────────────────────────────────
VIDEO_VARIANTS_DEFAULT = int(os.environ.get('VIDEO_VARIANTS_DEFAULT', 5))
VIDEO_VARIANTS_MAX = int(os.environ.get('VIDEO_VARIANTS_MAX', 5))
VIDEO_MAX_SECONDS = int(os.environ.get('VIDEO_MAX_SECONDS', 90))



def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """Добавляет колонку в существующую SQLite-таблицу, если её ещё нет."""
    cols = [row['name'] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()]
    if column not in cols:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def get_or_create_bootstrap_admin_password() -> str:
    """Берёт пароль админа из ENV или генерирует один раз и хранит в DATA_DIR."""
    if ADMIN_PASSWORD:
        return ADMIN_PASSWORD

    password_file = DATA_DIR / 'admin_password.txt'
    if password_file.exists():
        password = password_file.read_text(encoding='utf-8').strip()
        if password:
            return password

    password = secrets.token_urlsafe(12)
    password_file.write_text(password, encoding='utf-8')
    try:
        password_file.chmod(0o600)
    except OSError:
        pass
    return password


def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                created REAL NOT NULL,
                created_by TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                created REAL NOT NULL,
                created_by TEXT,
                variant INTEGER NOT NULL DEFAULT 1
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                login TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                password_plain TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                is_active INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS auth_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                success INTEGER NOT NULL,
                message TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT,
                created REAL NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS action_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                login TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                ip TEXT,
                created REAL NOT NULL
            )
        ''')

        ensure_column(conn, 'images', 'created_by', 'TEXT')
        ensure_column(conn, 'videos', 'created_by', 'TEXT')
        ensure_column(conn, 'videos', 'variant', 'INTEGER NOT NULL DEFAULT 1')

        admin_password = get_or_create_bootstrap_admin_password()
        existing = conn.execute('SELECT id FROM users WHERE login = ?', (ADMIN_LOGIN,)).fetchone()
        if not existing:
            conn.execute(
                '''INSERT INTO users (id, login, password_hash, password_plain, role, is_active, created)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (
                    uuid.uuid4().hex,
                    ADMIN_LOGIN,
                    generate_password_hash(admin_password),
                    admin_password,
                    'admin',
                    1,
                    time.time(),
                )
            )
            print(f'ImgUniq admin login: {ADMIN_LOGIN}')
            print(f'ImgUniq admin password: {admin_password}')
        elif ADMIN_PASSWORD:
            # Если админ уже был создан раньше с другим/случайным паролем,
            # синхронизируем его с переменной ADMIN_PASSWORD на каждом старте.
            conn.execute(
                '''UPDATE users
                   SET password_hash = ?, password_plain = ?, role = 'admin', is_active = 1
                   WHERE login = ?''',
                (generate_password_hash(ADMIN_PASSWORD), ADMIN_PASSWORD, ADMIN_LOGIN)
            )
            print(f'ImgUniq admin login: {ADMIN_LOGIN}')
            print('ImgUniq admin password updated from ADMIN_PASSWORD')


def is_valid_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


def save_image_url(url: str, created_by: str | None = None) -> str:
    img_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            'INSERT INTO images (id, url, created, created_by) VALUES (?, ?, ?, ?)',
            (img_id, url, time.time(), created_by)
        )
    return img_id


def save_video_url(url: str, created_by: str | None = None, variant: int = 1) -> str:
    video_id = uuid.uuid4().hex[:12]
    with get_db() as conn:
        conn.execute(
            'INSERT INTO videos (id, url, created, created_by, variant) VALUES (?, ?, ?, ?, ?)',
            (video_id, url, time.time(), created_by, variant)
        )
    return video_id


def get_image_url(img_id: str) -> str | None:
    with get_db() as conn:
        row = conn.execute('SELECT url FROM images WHERE id = ?', (img_id,)).fetchone()
    return row['url'] if row else None


def get_video_record(video_id: str) -> sqlite3.Row | None:
    with get_db() as conn:
        row = conn.execute('SELECT url, variant FROM videos WHERE id = ?', (video_id,)).fetchone()
    return row


def get_video_url(video_id: str) -> str | None:
    row = get_video_record(video_id)
    return row['url'] if row else None


def count_registered() -> int:
    with get_db() as conn:
        images = conn.execute('SELECT COUNT(*) AS total FROM images').fetchone()
        videos = conn.execute('SELECT COUNT(*) AS total FROM videos').fetchone()
    return int(images['total']) + int(videos['total'])


init_db()

def normalize_login(login: str) -> str:
    return (login or '').strip().lower()


def get_user_by_login(login: str) -> sqlite3.Row | None:
    login = normalize_login(login)
    if not login:
        return None
    with get_db() as conn:
        return conn.execute('SELECT * FROM users WHERE login = ?', (login,)).fetchone()


def get_current_user() -> dict | None:
    if not AUTH_ENABLED:
        return {'login': 'local', 'role': 'admin'}
    user = session.get('user')
    if not user:
        return None
    db_user = get_user_by_login(user.get('login', ''))
    if not db_user or not db_user['is_active']:
        session.clear()
        return None
    return {'login': db_user['login'], 'role': db_user['role']}


def log_auth(login: str, success: bool, message: str) -> None:
    with get_db() as conn:
        conn.execute(
            '''INSERT INTO auth_logs (login, success, message, ip, user_agent, created)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (
                normalize_login(login) or '-',
                1 if success else 0,
                message,
                request.headers.get('X-Forwarded-For', request.remote_addr or ''),
                (request.headers.get('User-Agent') or '')[:300],
                time.time(),
            )
        )


def log_action(action: str, details: str = '') -> None:
    user = get_current_user() or {'login': '-'}
    with get_db() as conn:
        conn.execute(
            '''INSERT INTO action_logs (login, action, details, ip, created)
               VALUES (?, ?, ?, ?, ?)''',
            (
                user['login'],
                action,
                details[:500],
                request.headers.get('X-Forwarded-For', request.remote_addr or ''),
                time.time(),
            )
        )


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not AUTH_ENABLED:
            return fn(*args, **kwargs)
        if get_current_user():
            return fn(*args, **kwargs)
        if request.path.startswith('/api/'):
            return jsonify({'success': False, 'error': 'Требуется авторизация'}), 401
        return redirect(url_for('login', next=request.path))
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = get_current_user()
        if not user:
            if request.path.startswith('/api/'):
                return jsonify({'success': False, 'error': 'Требуется авторизация'}), 401
            return redirect(url_for('login', next=request.path))
        if user['role'] != 'admin':
            return 'Доступ запрещён', 403
        return fn(*args, **kwargs)
    return wrapper


def clamp_video_variants(value) -> int:
    try:
        variants = int(value)
    except (TypeError, ValueError):
        variants = VIDEO_VARIANTS_DEFAULT
    return max(1, min(VIDEO_VARIANTS_MAX, variants))


def uniqualize_image(img: Image.Image) -> Image.Image:
    """Применяет набор лёгких случайных трансформаций к изображению."""
    img = img.copy().convert('RGB')
    w, h = img.size

    # 1. Субпиксельный affine-сдвиг: почти незаметно, но меняет сетку пикселей.
    shift_x = random.uniform(-0.35, 0.35)
    shift_y = random.uniform(-0.35, 0.35)
    img = img.transform(
        img.size,
        Image.Transform.AFFINE,
        (1, 0, shift_x, 0, 1, shift_y),
        resample=Image.Resampling.BICUBIC,
        fillcolor=(255, 255, 255),
    )

    # 2. Случайный ресемплинг через промежуточный размер.
    scale = random.uniform(0.985, 1.015)
    tmp_w = max(1, int(w * scale))
    tmp_h = max(1, int(h * scale))
    resample_filter = random.choice([
        Image.Resampling.LANCZOS,
        Image.Resampling.BICUBIC,
        Image.Resampling.BILINEAR,
    ])
    img = img.resize((tmp_w, tmp_h), resample_filter)
    img = img.resize((w, h), resample_filter)

    # 3. Микрокроп на 0-2 пикселя с возвратом к исходному размеру.
    crop_px = random.randint(0, 2)
    if crop_px > 0 and w > crop_px * 2 and h > crop_px * 2:
        left = random.randint(0, crop_px)
        top = random.randint(0, crop_px)
        right = w - random.randint(0, crop_px)
        bottom = h - random.randint(0, crop_px)
        if right > left and bottom > top:
            img = img.crop((left, top, right, bottom))
            img = img.resize((w, h), Image.Resampling.LANCZOS)

    # 4. Низкоамплитудная текстура + случайный шум.
    img_array = np.array(img, dtype=np.float32)
    noise_level = random.uniform(0.25, 1.15)
    noise = np.random.normal(0, noise_level, img_array.shape)

    texture_strength = random.uniform(0.15, 0.45)
    small_w = max(2, min(96, w // 12 or 2))
    small_h = max(2, min(96, h // 12 or 2))
    texture = np.random.normal(0, texture_strength, (small_h, small_w, 1)).astype(np.float32)
    texture_img = Image.fromarray(
        np.clip((texture[:, :, 0] + 128), 0, 255).astype(np.uint8),
        mode='L',
    ).resize((w, h), Image.Resampling.BICUBIC)
    texture = (np.array(texture_img, dtype=np.float32) - 128)[:, :, None]

    img_array = np.clip(img_array + noise + texture, 0, 255).astype(np.uint8)
    img = Image.fromarray(img_array, mode='RGB')

    # 5. Микросдвиги яркости, контраста и насыщенности.
    img = ImageEnhance.Brightness(img).enhance(random.uniform(0.992, 1.008))
    img = ImageEnhance.Contrast(img).enhance(random.uniform(0.995, 1.006))
    img = ImageEnhance.Color(img).enhance(random.uniform(0.994, 1.007))

    # 6. Лёгкий blur или sharpening.
    if random.random() > 0.5:
        img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.04, 0.16)))
    else:
        img = ImageEnhance.Sharpness(img).enhance(random.uniform(1.01, 1.05))

    return img


def build_random_exif() -> bytes:
    """Создаёт небольшой случайный EXIF-блок для JPEG."""
    exif = Image.Exif()
    now = time.strftime('%Y:%m:%d %H:%M:%S', time.gmtime())
    exif[305] = f"ImgUniq/{uuid.uuid4().hex[:8]}"  # Software
    exif[306] = now                              # DateTime
    exif[270] = f"render-{uuid.uuid4().hex[:12]}"  # ImageDescription
    return exif.tobytes()


def save_randomized_jpeg(img: Image.Image, buf: io.BytesIO) -> int:
    """Сохраняет JPEG с плавающими параметрами компрессии."""
    quality = random.randint(88, 95)
    img.save(
        buf,
        format='JPEG',
        quality=quality,
        optimize=True,
        progressive=random.choice([True, False]),
        subsampling=random.choice([0, 1, 2]),
        exif=build_random_exif(),
    )
    return quality


def load_image_from_url(url: str) -> Image.Image:
    """Загружает изображение по URL"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        data = response.read()
    return Image.open(io.BytesIO(data)).convert('RGB')


MAX_VIDEO_BYTES = int(os.environ.get('MAX_VIDEO_BYTES', 80 * 1024 * 1024))
VIDEO_TIMEOUT = int(os.environ.get('VIDEO_TIMEOUT', 180))


def get_ffmpeg_exe() -> str:
    """Возвращает путь к ffmpeg. На Railway его принесёт imageio-ffmpeg."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return os.environ.get('FFMPEG_BINARY', 'ffmpeg')


def download_url_to_temp(url: str, suffix: str = '.bin') -> str:
    """Скачивает URL во временный файл с лимитом размера, без хранения в RAM."""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    req = urllib.request.Request(url, headers=headers)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp_path = tmp.name
    total = 0

    try:
        with urllib.request.urlopen(req, timeout=25) as response, tmp:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise ValueError(f'Видео слишком большое. Лимит: {MAX_VIDEO_BYTES // 1024 // 1024} MB')
                tmp.write(chunk)
        return tmp_path
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def parse_ffmpeg_duration(stderr: str) -> float | None:
    match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', stderr or '')
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def get_video_duration_seconds(input_path: str) -> float | None:
    ffmpeg = get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg, '-hide_banner', '-i', input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=20,
    )
    return parse_ffmpeg_duration(result.stderr)


def random_video_filter(variant: int = 1) -> str:
    """Лёгкие случайные изменения кадров без заметной порчи качества."""
    # У каждого варианта немного свой диапазон, плюс каждый рендер всё равно плавает.
    strength = 1 + (max(1, variant) - 1) * 0.08
    brightness = random.uniform(-0.006, 0.006) * strength
    contrast = random.uniform(0.992, 1.008)
    saturation = random.uniform(0.992, 1.008)
    hue = random.uniform(-0.7, 0.7) * strength
    noise = random.uniform(0.35, 1.15) * strength

    filters = [
        f'eq=brightness={brightness:.5f}:contrast={contrast:.5f}:saturation={saturation:.5f}',
        f'hue=h={hue:.4f}',
        f'noise=alls={noise:.3f}:allf=t+u',
        'scale=trunc(iw/2)*2:trunc(ih/2)*2',
        'format=yuv420p',
    ]

    if random.random() > 0.45:
        filters.insert(2, 'unsharp=3:3:0.12:3:3:0.0')

    return ','.join(filters)


def transcode_randomized_video(input_path: str, output_path: str, variant: int = 1) -> None:
    """Перекодирует видео в MP4/H.264 с плавающими параметрами."""
    ffmpeg = get_ffmpeg_exe()
    crf = random.randint(21, 26)
    audio_bitrate = random.choice(['96k', '112k', '128k', '160k'])
    comment = f'variant-{variant}-render-{uuid.uuid4().hex[:12]}'

    cmd = [
        ffmpeg,
        '-y',
        '-hide_banner',
        '-loglevel', 'error',
        '-i', input_path,
        '-map', '0:v:0',
        '-map', '0:a?',
        '-t', str(VIDEO_MAX_SECONDS),
        '-vf', random_video_filter(variant),
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-crf', str(crf),
        '-pix_fmt', 'yuv420p',
        '-c:a', 'aac',
        '-b:a', audio_bitrate,
        '-map_metadata', '-1',
        '-metadata', f'comment={comment}',
        '-metadata', f'encoder=ImgUniq/{uuid.uuid4().hex[:8]}',
        '-movflags', '+faststart',
        output_path,
    ]

    subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=VIDEO_TIMEOUT,
    )


# ─── HTML интерфейс ───────────────────────────────────────────────────────────

HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ImgUniq — Уникализатор фото и видео</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0a0a0f;
    --surface: #111118;
    --surface2: #1a1a24;
    --border: rgba(255,255,255,0.07);
    --border-hover: rgba(139,92,246,0.4);
    --accent: #8b5cf6;
    --accent2: #06b6d4;
    --accent-glow: rgba(139,92,246,0.15);
    --text: #f1f1f5;
    --text-muted: #6b6b80;
    --text-dim: #3a3a4a;
    --success: #10b981;
    --error: #ef4444;
    --mono: 'JetBrains Mono', monospace;
    --sans: 'Inter', sans-serif;
  }

  body {
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
  }

  /* ── Ambient glow bg ── */
  body::before {
    content: '';
    position: fixed;
    top: -200px; left: 50%;
    transform: translateX(-50%);
    width: 800px; height: 500px;
    background: radial-gradient(ellipse at center, rgba(139,92,246,0.08) 0%, transparent 70%);
    pointer-events: none;
    z-index: 0;
  }

  .container {
    width: 100%;
    max-width: 720px;
    padding: 0 24px;
    position: relative;
    z-index: 1;
  }

  .topbar {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    align-items: center;
    padding-top: 18px;
    color: var(--text-muted);
    font-size: 12px;
  }

  .topbar a {
    color: var(--text-muted);
    text-decoration: none;
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 7px 10px;
    background: var(--surface);
  }

  .topbar a:hover { color: var(--text); border-color: var(--border-hover); }

  .variants-wrap {
    display: none;
    margin-top: 12px;
    color: var(--text-muted);
    font-size: 12px;
    align-items: center;
    gap: 10px;
  }

  .variants-wrap.show { display: flex; }

  .variants-input {
    width: 74px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 10px 12px;
    font-family: var(--mono);
    color: var(--text);
    outline: none;
  }

  /* ── Header ── */
  header {
    padding: 56px 0 48px;
    text-align: center;
  }

  .logo-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 100px;
    padding: 6px 14px 6px 10px;
    margin-bottom: 28px;
  }

  .logo-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--accent);
    box-shadow: 0 0 8px var(--accent);
    animation: pulse 2s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 1; box-shadow: 0 0 8px var(--accent); }
    50% { opacity: 0.6; box-shadow: 0 0 16px var(--accent); }
  }

  .logo-text {
    font-size: 11px;
    font-weight: 500;
    color: var(--text-muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  h1 {
    font-size: clamp(28px, 5vw, 42px);
    font-weight: 300;
    letter-spacing: -0.02em;
    line-height: 1.2;
    margin-bottom: 14px;
  }

  h1 span {
    background: linear-gradient(135deg, #8b5cf6, #06b6d4);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    font-weight: 600;
  }

  .subtitle {
    font-size: 15px;
    color: var(--text-muted);
    font-weight: 300;
    line-height: 1.6;
  }

  /* ── Card ── */
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 32px;
    margin-bottom: 16px;
    transition: border-color 0.3s;
  }

  .card:hover {
    border-color: rgba(139,92,246,0.2);
  }

  .card-label {
    font-size: 11px;
    font-weight: 500;
    color: var(--text-muted);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 12px;
  }

  /* ── Input ── */
  .input-wrap {
    position: relative;
    display: flex;
    gap: 10px;
    align-items: stretch;
  }

  .url-input {
    flex: 1;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text);
    outline: none;
    transition: border-color 0.2s, box-shadow 0.2s;
    width: 100%;
  }

  .url-input::placeholder { color: var(--text-dim); }

  .url-input:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
  }

  .type-select {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 0 12px;
    font-family: var(--sans);
    font-size: 13px;
    color: var(--text);
    outline: none;
  }

  .type-select:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
  }

  /* ── Кнопки ── */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 14px 22px;
    border-radius: 12px;
    font-family: var(--sans);
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    border: none;
    transition: all 0.2s;
    white-space: nowrap;
  }

  .btn-primary {
    background: linear-gradient(135deg, #7c3aed, #0891b2);
    color: #fff;
    box-shadow: 0 4px 20px rgba(139,92,246,0.25);
  }

  .btn-primary:hover {
    transform: translateY(-1px);
    box-shadow: 0 8px 28px rgba(139,92,246,0.35);
  }

  .btn-primary:active { transform: translateY(0); }

  .btn-primary:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    transform: none;
  }

  .btn-ghost {
    background: var(--surface2);
    color: var(--text-muted);
    border: 1px solid var(--border);
  }

  .btn-ghost:hover {
    border-color: var(--border-hover);
    color: var(--text);
  }

  .btn-sm {
    padding: 8px 14px;
    font-size: 12px;
    border-radius: 8px;
  }

  /* ── URLs List ── */
  .urls-section { margin-top: 20px; }

  .urls-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
  }

  .url-count {
    font-size: 11px;
    color: var(--text-muted);
    font-family: var(--mono);
  }

  .urls-textarea {
    width: 100%;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 14px 16px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-muted);
    outline: none;
    resize: vertical;
    min-height: 100px;
    transition: border-color 0.2s;
    line-height: 1.8;
  }

  .urls-textarea:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-glow);
    color: var(--text);
  }

  /* ── Result ── */
  .result-section {
    display: none;
    margin-top: 24px;
  }

  .result-section.show { display: block; }

  .result-divider {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 20px;
    color: var(--text-dim);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .result-divider::before,
  .result-divider::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  .result-grid {
    display: grid;
    gap: 12px;
  }

  .result-item {
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    display: flex;
    align-items: center;
    gap: 14px;
    transition: border-color 0.2s;
  }

  .result-item:hover { border-color: var(--border-hover); }

  .result-num {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--accent);
    background: var(--accent-glow);
    border-radius: 6px;
    padding: 3px 7px;
    flex-shrink: 0;
  }

  .result-url {
    flex: 1;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .result-actions {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
  }

  /* ── Toast ── */
  .toast {
    position: fixed;
    bottom: 28px;
    right: 28px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 18px;
    font-size: 13px;
    color: var(--text);
    box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    transform: translateY(80px);
    opacity: 0;
    transition: all 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
    z-index: 100;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .toast.show {
    transform: translateY(0);
    opacity: 1;
  }

  .toast-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: var(--success);
    flex-shrink: 0;
  }

  /* ── Stats strip ── */
  .stats-strip {
    display: flex;
    gap: 1px;
    background: var(--border);
    border-radius: 14px;
    overflow: hidden;
    margin-bottom: 16px;
  }

  .stat-item {
    flex: 1;
    background: var(--surface);
    padding: 16px;
    text-align: center;
  }

  .stat-num {
    font-family: var(--mono);
    font-size: 20px;
    font-weight: 500;
    color: var(--accent);
    display: block;
  }

  .stat-label {
    font-size: 10px;
    color: var(--text-muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 2px;
  }

  /* ── Loader ── */
  .spinner {
    width: 16px; height: 16px;
    border: 2px solid rgba(255,255,255,0.2);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  /* ── Copy all button ── */
  .copy-all-bar {
    display: none;
    gap: 10px;
    margin-top: 12px;
    justify-content: flex-end;
  }

  .copy-all-bar.show { display: flex; }

  /* ── Footer ── */
  footer {
    padding: 40px 0 32px;
    text-align: center;
    color: var(--text-dim);
    font-size: 12px;
  }
</style>
</head>
<body>
<div class="container">
  <div class="topbar">
    <span>Вход: {{ current_user.login }}</span>
    {% if current_user.role == 'admin' %}<a href="/admin">Пользователи</a>{% endif %}
    <a href="/logout">Выйти</a>
  </div>
  <header>
    <div class="logo-badge">
      <div class="logo-dot"></div>
      <span class="logo-text">ImgUniq v1.2</span>
    </div>
    <h1>Уникализация<br><span>фото и видео</span></h1>
    <p class="subtitle">Вставь ссылку на картинку или видео — получи статическую ссылку,<br>по которой каждый раз будет новая версия</p>
  </header>

  <!-- Stats -->
  <div class="stats-strip" id="statsStrip" style="display:none">
    <div class="stat-item">
      <span class="stat-num" id="statTotal">0</span>
      <div class="stat-label">Ссылок создано</div>
    </div>
    <div class="stat-item">
      <span class="stat-num" id="statSession">0</span>
      <div class="stat-label">В этой сессии</div>
    </div>
    <div class="stat-item">
      <span class="stat-num" id="statRendered">∞</span>
      <div class="stat-label">Рендеров на ссылку</div>
    </div>
  </div>

  <!-- Input card -->
  <div class="card">
    <div class="card-label">Исходник</div>
    <div class="input-wrap">
      <input 
        type="text" 
        class="url-input" 
        id="urlInput"
        placeholder="https://example.com/image.jpg или video.mp4"
        autocomplete="off"
        spellcheck="false"
      >
      <select class="type-select" id="mediaType" title="Тип файла">
        <option value="image">Фото</option>
        <option value="video">Видео</option>
      </select>
      <button class="btn btn-primary" id="addBtn" onclick="addSingleUrl()">
        <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14M5 12h14"/></svg>
        Добавить
      </button>
    </div>
    <div class="variants-wrap" id="variantsWrap">
      <span>Вариантов видео</span>
      <input class="variants-input" id="videoVariants" type="number" min="1" max="5" value="5">
      <span>максимум 5, длина до 90 секунд</span>
    </div>

    <div class="urls-section">
      <div class="urls-header">
        <div class="card-label" style="margin:0">Или вставь сразу несколько ссылок</div>
        <span class="url-count" id="urlCount">0 ссылок</span>
      </div>
      <textarea 
        class="urls-textarea" 
        id="urlsTextarea"
        placeholder="Вставь несколько ссылок одного типа — каждую с новой строки..."
        oninput="countUrls()"
      ></textarea>
      <div style="display:flex; justify-content:flex-end; margin-top:10px; gap:8px">
        <button class="btn btn-ghost btn-sm" onclick="clearAll()">Очистить</button>
        <button class="btn btn-primary btn-sm" id="processBtn" onclick="processBatch()">
          Создать ссылки
        </button>
      </div>
    </div>
  </div>

  <!-- Results -->
  <div class="result-section" id="resultSection">
    <div class="result-divider">Готовые ссылки</div>
    <div class="result-grid" id="resultGrid"></div>
    <div class="copy-all-bar" id="copyAllBar">
      <button class="btn btn-ghost btn-sm" onclick="copyAll()">
        <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
        Скопировать все
      </button>
    </div>
  </div>

</div>

<footer>ImgUniq — каждый рендер создаётся заново</footer>

<div class="toast" id="toast">
  <div class="toast-dot"></div>
  <span id="toastMsg">Скопировано</span>
</div>

<script>
const BASE = window.location.origin;
let sessionCount = 0;
let totalCount = 0;
let allResultUrls = [];

function getVideoVariants() {
  const input = document.getElementById('videoVariants');
  const n = parseInt(input.value || '5', 10);
  if (Number.isNaN(n)) return 5;
  return Math.max(1, Math.min(5, n));
}

function updateVariantsVisibility() {
  const mediaType = document.getElementById('mediaType').value;
  document.getElementById('variantsWrap').classList.toggle('show', mediaType === 'video');
}

function countUrls() {
  const lines = document.getElementById('urlsTextarea').value
    .split('\\n')
    .map(l => l.trim())
    .filter(l => l.startsWith('http'));
  document.getElementById('urlCount').textContent = lines.length + ' ссылок';
}

function clearAll() {
  document.getElementById('urlInput').value = '';
  document.getElementById('urlsTextarea').value = '';
  document.getElementById('urlCount').textContent = '0 ссылок';
}

async function addSingleUrl() {
  const url = document.getElementById('urlInput').value.trim();
  const mediaType = document.getElementById('mediaType').value;
  if (!url || !url.startsWith('http')) {
    showToast('Вставь корректную ссылку', true);
    return;
  }

  const btn = document.getElementById('addBtn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Создаю...';

  try {
    const res = await fetch('/api/register', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, type: mediaType, variants: getVideoVariants()})
    });
    const data = await res.json();
    
    if (data.success) {
      const results = data.results && data.results.length ? data.results : [data];
      results.forEach(r => addResultItem(r.unique_url, r.id));
      document.getElementById('urlInput').value = '';
      showToast(results.length > 1 ? `Создано ${results.length} вариантов` : 'Ссылка создана');
      updateStats();
    } else {
      showToast(data.error || 'Ошибка', true);
    }
  } catch(e) {
    showToast('Ошибка соединения', true);
  }

  btn.disabled = false;
  btn.innerHTML = '<svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14M5 12h14"/></svg> Добавить';
}

async function processBatch() {
  const lines = document.getElementById('urlsTextarea').value
    .split('\\n')
    .map(l => l.trim())
    .filter(l => l.startsWith('http'));
  
  const mediaType = document.getElementById('mediaType').value;

  if (!lines.length) {
    showToast('Нет ссылок для обработки', true);
    return;
  }

  const btn = document.getElementById('processBtn');
  btn.disabled = true;
  btn.innerHTML = '<div class="spinner"></div> Создаю...';

  try {
    const res = await fetch('/api/register-batch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({urls: lines, type: mediaType, variants: getVideoVariants()})
    });
    const data = await res.json();
    
    if (data.success) {
      data.results.forEach(r => addResultItem(r.unique_url, r.id));
      showToast(`Создано ${data.results.length} ссылок`);
      updateStats();
      document.getElementById('urlsTextarea').value = '';
      document.getElementById('urlCount').textContent = '0 ссылок';
    } else {
      showToast(data.error || 'Ошибка', true);
    }
  } catch(e) {
    showToast('Ошибка соединения', true);
  }

  btn.disabled = false;
  btn.innerHTML = 'Создать ссылки';
}

function addResultItem(uniqueUrl, id) {
  const section = document.getElementById('resultSection');
  const grid = document.getElementById('resultGrid');
  
  section.classList.add('show');
  document.getElementById('copyAllBar').classList.add('show');
  
  allResultUrls.push(uniqueUrl);
  sessionCount++;
  totalCount++;

  const idx = allResultUrls.length;
  
  const item = document.createElement('div');
  item.className = 'result-item';
  item.innerHTML = `
    <span class="result-num">${String(idx).padStart(2,'0')}</span>
    <span class="result-url">${uniqueUrl}</span>
    <div class="result-actions">
      <button class="btn btn-ghost btn-sm" onclick="copyUrl('${uniqueUrl}')">
        <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>
        Копировать
      </button>
      <a class="btn btn-ghost btn-sm" href="${uniqueUrl}" target="_blank">
        <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
        Открыть
      </a>
    </div>
  `;
  grid.appendChild(item);
  
  updateStats();
}

function copyUrl(url) {
  navigator.clipboard.writeText(url).then(() => showToast('Скопировано'));
}

function copyAll() {
  navigator.clipboard.writeText(allResultUrls.join('\\n')).then(() => 
    showToast(`Скопировано ${allResultUrls.length} ссылок`)
  );
}

function updateStats() {
  const strip = document.getElementById('statsStrip');
  strip.style.display = 'flex';
  document.getElementById('statTotal').textContent = totalCount;
  document.getElementById('statSession').textContent = sessionCount;
}

function showToast(msg, isError) {
  const t = document.getElementById('toast');
  const dot = t.querySelector('.toast-dot');
  document.getElementById('toastMsg').textContent = msg;
  dot.style.background = isError ? 'var(--error)' : 'var(--success)';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2500);
}

document.getElementById('mediaType').addEventListener('change', updateVariantsVisibility);
updateVariantsVisibility();

// Enter to submit
document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') addSingleUrl();
});
</script>
</body>
</html>'''



LOGIN_HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Вход — ImgUniq</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0a0a0f;color:#f1f1f5;font-family:Inter,Arial,sans-serif}
  .card{width:min(420px,calc(100vw - 32px));background:#111118;border:1px solid rgba(255,255,255,.08);border-radius:20px;padding:28px;box-shadow:0 20px 70px rgba(0,0,0,.35)}
  h1{margin:0 0 8px;font-size:28px;font-weight:600}p{margin:0 0 22px;color:#74748a;font-size:14px;line-height:1.5}
  label{display:block;margin:14px 0 8px;color:#74748a;font-size:12px;text-transform:uppercase;letter-spacing:.08em}
  input{width:100%;box-sizing:border-box;background:#1a1a24;border:1px solid rgba(255,255,255,.08);border-radius:12px;color:#fff;padding:14px 15px;outline:none;font-size:15px}
  input:focus{border-color:#8b5cf6;box-shadow:0 0 0 3px rgba(139,92,246,.14)}
  button{width:100%;margin-top:18px;border:0;border-radius:12px;padding:14px 18px;background:linear-gradient(135deg,#7c3aed,#0891b2);color:#fff;font-size:15px;font-weight:600;cursor:pointer}
  .err{background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.28);color:#fecaca;border-radius:12px;padding:12px 14px;margin-bottom:16px;font-size:14px}
</style>
</head>
<body>
  <form class="card" method="post">
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <label>Логин</label>
    <input name="login" autocomplete="username" autofocus>
    <label>Пароль</label>
    <input name="password" type="password" autocomplete="current-password">
    <button type="submit">Войти</button>
  </form>
</body>
</html>'''

ADMIN_HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Пользователи — ImgUniq</title>
<style>
  body{margin:0;min-height:100vh;background:#0a0a0f;color:#f1f1f5;font-family:Inter,Arial,sans-serif;padding:32px 18px}
  .wrap{max-width:980px;margin:0 auto}.top{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-bottom:20px}a{color:#a78bfa;text-decoration:none}
  .card{background:#111118;border:1px solid rgba(255,255,255,.08);border-radius:18px;padding:22px;margin-bottom:18px}.muted{color:#74748a;font-size:13px;line-height:1.5}
  h1{margin:0;font-size:30px}h2{margin:0 0 14px;font-size:18px}label{display:block;margin:12px 0 7px;color:#74748a;font-size:12px;text-transform:uppercase;letter-spacing:.08em}
  input,select{width:100%;box-sizing:border-box;background:#1a1a24;border:1px solid rgba(255,255,255,.08);border-radius:12px;color:#fff;padding:12px 14px;outline:none}
  button{margin-top:14px;border:0;border-radius:12px;padding:12px 16px;background:linear-gradient(135deg,#7c3aed,#0891b2);color:#fff;font-weight:600;cursor:pointer}.danger{background:linear-gradient(135deg,#dc2626,#7f1d1d)}.danger-row{display:flex;gap:12px;flex-wrap:wrap}.danger-row form{display:inline}.grid{display:grid;grid-template-columns:1fr 1fr 140px;gap:12px}
  table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:11px 10px;border-bottom:1px solid rgba(255,255,255,.07);text-align:left;vertical-align:top}th{color:#74748a;font-size:11px;text-transform:uppercase;letter-spacing:.08em}.pass{font-family:monospace;color:#d8b4fe}.ok{color:#86efac}.bad{color:#fecaca}.msg{margin-bottom:14px;background:rgba(16,185,129,.1);border:1px solid rgba(16,185,129,.25);border-radius:12px;padding:12px 14px;color:#bbf7d0}.err{margin-bottom:14px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);border-radius:12px;padding:12px 14px;color:#fecaca}
  @media(max-width:760px){.grid{grid-template-columns:1fr}.table-wrap{overflow:auto}}
</style>
</head>
<body><div class="wrap">
  <div class="top"><h1>Пользователи</h1><div><a href="/">Главная</a> · <a href="/logout">Выйти</a></div></div>
  <div class="card">
    <h2>Добавить известного пользователя</h2>
    <p class="muted">Самостоятельной регистрации нет. Пароль задаёшь ты; смены пароля пользователем нет.</p>
    {% if message %}<div class="msg">{{ message }}</div>{% endif %}
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="post" class="grid">
      <input type="hidden" name="action" value="add_user">
      <div><label>Логин</label><input name="login" required></div>
      <div><label>Пароль</label><input name="password" required></div>
      <div><label>Роль</label><select name="role"><option value="user">user</option><option value="admin">admin</option></select></div>
      <div style="grid-column:1/-1"><button type="submit">Добавить</button></div>
    </form>
  </div>

  <div class="card">
    <h2>Очистка базы</h2>
    <p class="muted">Можно очистить только логи или удалить всех добавленных пользователей. Основной admin останется, пароль возьмётся из Railway-переменной ADMIN_PASSWORD.</p>
    <div class="danger-row">
      <form method="post" onsubmit="return confirm('Точно очистить логи входов и действий?')">
        <input type="hidden" name="action" value="clear_logs">
        <button class="danger" type="submit">Очистить логи</button>
      </form>
      <form method="post" onsubmit="return confirm('Точно удалить всех пользователей кроме admin и очистить логи?')">
        <input type="hidden" name="action" value="clear_users">
        <button class="danger" type="submit">Оставить только admin</button>
      </form>
    </div>
  </div>

  <div class="card table-wrap">
    <h2>Список пользователей и пароли</h2>
    <table><thead><tr><th>Логин</th><th>Пароль</th><th>Роль</th><th>Активен</th><th>Создан</th></tr></thead><tbody>
      {% for u in users %}<tr><td>{{ u.login }}</td><td class="pass">{{ u.password_plain }}</td><td>{{ u.role }}</td><td>{{ 'да' if u.is_active else 'нет' }}</td><td>{{ fmt_time(u.created) }}</td></tr>{% endfor %}
    </tbody></table>
  </div>

  <div class="card table-wrap">
    <h2>Последние входы</h2>
    <table><thead><tr><th>Время</th><th>Логин</th><th>Статус</th><th>Сообщение</th><th>IP</th></tr></thead><tbody>
      {% for l in auth_logs %}<tr><td>{{ fmt_time(l.created) }}</td><td>{{ l.login }}</td><td class="{{ 'ok' if l.success else 'bad' }}">{{ 'успешно' if l.success else 'ошибка' }}</td><td>{{ l.message }}</td><td>{{ l.ip }}</td></tr>{% endfor %}
    </tbody></table>
  </div>

  <div class="card table-wrap">
    <h2>Последние действия</h2>
    <table><thead><tr><th>Время</th><th>Логин</th><th>Действие</th><th>Детали</th><th>IP</th></tr></thead><tbody>
      {% for l in action_logs %}<tr><td>{{ fmt_time(l.created) }}</td><td>{{ l.login }}</td><td>{{ l.action }}</td><td>{{ l.details }}</td><td>{{ l.ip }}</td></tr>{% endfor %}
    </tbody></table>
  </div>
</div></body></html>'''


def fmt_time(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return '-'

# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    if get_current_user():
        return redirect(url_for('index'))

    error = ''
    if request.method == 'POST':
        login_value = normalize_login(request.form.get('login', ''))
        password = request.form.get('password', '')
        user = get_user_by_login(login_value)

        if user and user['is_active'] and check_password_hash(user['password_hash'], password):
            session['user'] = {'login': user['login'], 'role': user['role']}
            log_auth(login_value, True, 'Успешный вход')
            return redirect(request.args.get('next') or url_for('index'))

        error = 'Неправильный пароль'
        log_auth(login_value, False, error)

    return render_template_string(LOGIN_HTML, error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/admin', methods=['GET', 'POST'])
@admin_required
def admin_users():
    message = ''
    error = ''

    if request.method == 'POST':
        action = (request.form.get('action') or 'add_user').strip()

        if action == 'clear_logs':
            with get_db() as conn:
                conn.execute('DELETE FROM auth_logs')
                conn.execute('DELETE FROM action_logs')
            message = 'Логи входов и действий очищены'

        elif action == 'clear_users':
            admin_password = get_or_create_bootstrap_admin_password()
            with get_db() as conn:
                conn.execute('DELETE FROM users WHERE login <> ?', (ADMIN_LOGIN,))
                existing = conn.execute('SELECT id FROM users WHERE login = ?', (ADMIN_LOGIN,)).fetchone()
                if existing:
                    conn.execute(
                        '''UPDATE users
                           SET password_hash = ?, password_plain = ?, role = 'admin', is_active = 1
                           WHERE login = ?''',
                        (generate_password_hash(admin_password), admin_password, ADMIN_LOGIN)
                    )
                else:
                    conn.execute(
                        '''INSERT INTO users (id, login, password_hash, password_plain, role, is_active, created)
                           VALUES (?, ?, ?, ?, ?, ?, ?)''',
                        (uuid.uuid4().hex, ADMIN_LOGIN, generate_password_hash(admin_password), admin_password, 'admin', 1, time.time())
                    )
                conn.execute('DELETE FROM auth_logs')
                conn.execute('DELETE FROM action_logs')
            session['user'] = {'login': ADMIN_LOGIN, 'role': 'admin'}
            message = 'База пользователей очищена: оставлен только admin'

        else:
            login_value = normalize_login(request.form.get('login', ''))
            password = (request.form.get('password', '') or '').strip()
            role = (request.form.get('role', 'user') or 'user').strip().lower()

            if role not in ('user', 'admin'):
                role = 'user'

            if not re.fullmatch(r'[a-z0-9_.@+-]{2,64}', login_value or ''):
                error = 'Логин: 2–64 символа, латиница/цифры/._@+-'
            elif len(password) < 4:
                error = 'Пароль должен быть минимум 4 символа'
            elif get_user_by_login(login_value):
                error = 'Такой логин уже есть'
            else:
                with get_db() as conn:
                    conn.execute(
                        '''INSERT INTO users (id, login, password_hash, password_plain, role, is_active, created)
                           VALUES (?, ?, ?, ?, ?, ?, ?)''',
                        (uuid.uuid4().hex, login_value, generate_password_hash(password), password, role, 1, time.time())
                    )
                log_action('user_created', f'{login_value} / role={role}')
                message = f'Пользователь {login_value} добавлен'

    with get_db() as conn:
        users = conn.execute('SELECT login, password_plain, role, is_active, created FROM users ORDER BY created DESC').fetchall()
        auth_logs = conn.execute('SELECT login, success, message, ip, created FROM auth_logs ORDER BY created DESC LIMIT 100').fetchall()
        action_logs = conn.execute('SELECT login, action, details, ip, created FROM action_logs ORDER BY created DESC LIMIT 100').fetchall()

    return render_template_string(
        ADMIN_HTML,
        users=users,
        auth_logs=auth_logs,
        action_logs=action_logs,
        message=message,
        error=error,
        fmt_time=fmt_time,
    )


@app.route('/')
@login_required
def index():
    return render_template_string(HTML, current_user=get_current_user())


@app.route('/api/register', methods=['POST'])
@login_required
def register():
    data = request.json or {}
    url = data.get('url', '').strip()
    media_type = data.get('type', 'image').strip().lower()
    variants = clamp_video_variants(data.get('variants')) if media_type == 'video' else 1
    user = get_current_user() or {'login': '-'}

    if media_type not in ('image', 'video'):
        return jsonify({'success': False, 'error': 'Некорректный тип файла'})

    if not is_valid_url(url):
        return jsonify({'success': False, 'error': 'Некорректная ссылка'})

    base_url = request.host_url.rstrip('/')
    results = []

    if media_type == 'video':
        for variant in range(1, variants + 1):
            media_id = save_video_url(url, user['login'], variant=variant)
            results.append({
                'id': media_id,
                'type': media_type,
                'variant': variant,
                'unique_url': f"{base_url}/video/{media_id}",
                'source_url': url
            })
        log_action('video_registered', f'{variants} variant(s): {url}')
    else:
        media_id = save_image_url(url, user['login'])
        results.append({
            'id': media_id,
            'type': media_type,
            'variant': 1,
            'unique_url': f"{base_url}/img/{media_id}",
            'source_url': url
        })
        log_action('image_registered', url)

    first = results[0]
    return jsonify({
        'success': True,
        'id': first['id'],
        'type': media_type,
        'variant': first['variant'],
        'unique_url': first['unique_url'],
        'source_url': url,
        'results': results,
    })


@app.route('/api/register-batch', methods=['POST'])
@login_required
def register_batch():
    data = request.json or {}
    urls = data.get('urls', [])
    media_type = data.get('type', 'image').strip().lower()
    variants = clamp_video_variants(data.get('variants')) if media_type == 'video' else 1
    user = get_current_user() or {'login': '-'}

    if media_type not in ('image', 'video'):
        return jsonify({'success': False, 'error': 'Некорректный тип файла'})

    if not urls:
        return jsonify({'success': False, 'error': 'Нет ссылок'})

    results = []
    base_url = request.host_url.rstrip('/')
    source_limit = 5 if media_type == 'video' else 50

    for url in urls[:source_limit]:
        url = url.strip()
        if not is_valid_url(url):
            continue

        if media_type == 'video':
            for variant in range(1, variants + 1):
                media_id = save_video_url(url, user['login'], variant=variant)
                results.append({
                    'id': media_id,
                    'type': media_type,
                    'variant': variant,
                    'unique_url': f"{base_url}/video/{media_id}",
                    'source_url': url
                })
        else:
            media_id = save_image_url(url, user['login'])
            results.append({
                'id': media_id,
                'type': media_type,
                'variant': 1,
                'unique_url': f"{base_url}/img/{media_id}",
                'source_url': url
            })

    log_action(f'{media_type}_batch_registered', f'{len(results)} link(s) from {min(len(urls), source_limit)} source(s)')
    return jsonify({'success': True, 'results': results})


@app.route('/img/<img_id>')
def serve_image(img_id):
    if not PUBLIC_MEDIA_LINKS and not get_current_user():
        return redirect(url_for('login', next=request.path))
    source_url = get_image_url(img_id)
    if not source_url:
        return 'Not found', 404
    
    try:
        img = load_image_from_url(source_url)
        img = uniqualize_image(img)
        
        buf = io.BytesIO()
        save_randomized_jpeg(img, buf)
        buf.seek(0)
        
        response = send_file(buf, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
        
    except Exception as e:
        return f'Error loading image: {str(e)}', 500


@app.route('/video/<video_id>')
def serve_video(video_id):
    if not PUBLIC_MEDIA_LINKS and not get_current_user():
        return redirect(url_for('login', next=request.path))
    record = get_video_record(video_id)
    if not record:
        return 'Not found', 404
    source_url = record['url']
    variant = int(record['variant'] or 1)

    input_path = None
    output_path = None

    try:
        input_path = download_url_to_temp(source_url, suffix='.video')
        duration = get_video_duration_seconds(input_path)
        if duration is not None and duration > VIDEO_MAX_SECONDS:
            raise ValueError(f'Видео длиннее лимита: максимум {VIDEO_MAX_SECONDS} секунд')

        output_file = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4')
        output_path = output_file.name
        output_file.close()

        transcode_randomized_video(input_path, output_path, variant=variant)

        @after_this_request
        def cleanup(response):
            for path in (input_path, output_path):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            return response

        response = send_file(
            output_path,
            mimetype='video/mp4',
            as_attachment=False,
            download_name=f'{video_id}.mp4',
            conditional=False,
        )
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    except subprocess.TimeoutExpired:
        for path in (input_path, output_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return 'Error processing video: timeout', 504
    except Exception as e:
        for path in (input_path, output_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return f'Error processing video: {str(e)}', 500


@app.route('/api/stats')
@login_required
def stats():
    return jsonify({
        'total_registered': count_registered(),
        'storage': str(DB_PATH),
        'max_video_mb': MAX_VIDEO_BYTES // 1024 // 1024,
        'video_timeout_seconds': VIDEO_TIMEOUT,
        'video_max_seconds': VIDEO_MAX_SECONDS,
        'video_variants_default': VIDEO_VARIANTS_DEFAULT,
        'video_variants_max': VIDEO_VARIANTS_MAX,
        'auth_enabled': AUTH_ENABLED,
        'public_media_links': PUBLIC_MEDIA_LINKS,
        'uptime': 'ok'
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
