import io
import ipaddress
import os
import random
import secrets
import socket
import sqlite3
import time
from contextlib import closing
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps, UnidentifiedImageError

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(APP_DIR, "data")
DATABASE_PATH = os.environ.get("DATABASE_PATH", os.path.join(DEFAULT_DATA_DIR, "imguniq.sqlite3"))
MAX_BATCH_SIZE = int(os.environ.get("MAX_BATCH_SIZE", "50"))
MAX_IMAGE_BYTES = int(os.environ.get("MAX_IMAGE_BYTES", str(12 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", str(25_000_000)))
MAX_IMAGE_SIDE = int(os.environ.get("MAX_IMAGE_SIDE", "4096"))
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "12"))
MAX_REDIRECTS = int(os.environ.get("MAX_REDIRECTS", "4"))

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

app = Flask(__name__)

@app.after_request
def add_api_headers(response):
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response


def init_db() -> None:
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    with closing(sqlite3.connect(DATABASE_PATH)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                hits INTEGER NOT NULL DEFAULT 0,
                last_hit_at INTEGER
            )
            """
        )
        conn.commit()


def db_connect() -> sqlite3.Connection:
    init_db()
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def create_image_record(source_url: str) -> str:
    img_id = secrets.token_urlsafe(9)
    now = int(time.time())
    with closing(db_connect()) as conn:
        conn.execute(
            "INSERT INTO images (id, source_url, created_at) VALUES (?, ?, ?)",
            (img_id, source_url, now),
        )
        conn.commit()
    return img_id


def get_image_record(img_id: str) -> sqlite3.Row | None:
    with closing(db_connect()) as conn:
        row = conn.execute("SELECT * FROM images WHERE id = ?", (img_id,)).fetchone()
    return row


def mark_hit(img_id: str) -> None:
    now = int(time.time())
    with closing(db_connect()) as conn:
        conn.execute(
            "UPDATE images SET hits = hits + 1, last_hit_at = ? WHERE id = ?",
            (now, img_id),
        )
        conn.commit()


def normalize_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Нужна корректная http/https ссылка")
    validate_public_host(parsed.hostname)
    return url


def validate_public_host(hostname: str | None) -> None:
    if not hostname:
        raise ValueError("В ссылке не найден домен")

    try:
        addresses = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError("Домен не удалось разрешить") from exc

    for family, _, _, _, sockaddr in addresses:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global:
            raise ValueError("Внутренние и локальные адреса запрещены")


def response_error(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def public_image_url(img_id: str) -> str:
    base_url = os.environ.get("APP_BASE_URL", request.host_url.rstrip("/"))
    return f"{base_url}/img/{img_id}"


def fetch_image_bytes(url: str) -> bytes:
    current_url = url
    headers = {"User-Agent": "ImgUniq/2.0 (+image proxy)"}

    for _ in range(MAX_REDIRECTS + 1):
        current_url = normalize_url(current_url)
        response = requests.get(
            current_url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            stream=True,
            allow_redirects=False,
        )

        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("Редирект без Location")
            current_url = urljoin(current_url, location)
            continue

        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type and not content_type.startswith("image/"):
            response.close()
            raise ValueError("Ссылка должна вести на изображение")

        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_IMAGE_BYTES:
            response.close()
            raise ValueError("Изображение слишком большое")

        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ValueError("Изображение слишком большое")
                chunks.append(chunk)
        finally:
            response.close()
        return b"".join(chunks)

    raise ValueError("Слишком много редиректов")


def load_image_from_url(url: str) -> Image.Image:
    data = fetch_image_bytes(url)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = ImageOps.exif_transpose(image)
            image = image.convert("RGB")
            image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
            return image.copy()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Не удалось прочитать изображение") from exc


def uniqualize_image(img: Image.Image) -> Image.Image:
    img = img.copy()

    noise_level = random.uniform(0.3, 1.2)
    noise = Image.effect_noise(img.size, noise_level).convert("L")
    noise_rgb = Image.merge("RGB", (noise, noise, noise))
    img = ImageChops.add(img, noise_rgb, scale=1.0, offset=-128)

    img = ImageEnhance.Brightness(img).enhance(random.uniform(0.992, 1.008))
    img = ImageEnhance.Contrast(img).enhance(random.uniform(0.995, 1.005))

    if random.random() > 0.5:
        img = img.filter(ImageFilter.GaussianBlur(radius=random.uniform(0.05, 0.15)))
    else:
        img = ImageEnhance.Sharpness(img).enhance(random.uniform(1.01, 1.04))

    w, h = img.size
    crop_px = random.randint(0, 2)
    if crop_px > 0 and w > crop_px * 2 and h > crop_px * 2:
        left = random.randint(0, crop_px)
        top = random.randint(0, crop_px)
        right = w - random.randint(0, crop_px)
        bottom = h - random.randint(0, crop_px)
        if right > left and bottom > top:
            img = img.crop((left, top, right, bottom)).resize((w, h), Image.Resampling.LANCZOS)

    img = ImageEnhance.Color(img).enhance(random.uniform(0.994, 1.006))
    return img


@app.get("/")
def index():
    return render_template("index.html", max_batch_size=MAX_BATCH_SIZE)


@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    try:
        url = normalize_url(data.get("url", ""))
    except ValueError as exc:
        return response_error(str(exc))

    img_id = create_image_record(url)
    return jsonify(
        {
            "success": True,
            "id": img_id,
            "unique_url": public_image_url(img_id),
            "source_url": url,
        }
    )


@app.post("/api/register-batch")
def register_batch():
    data = request.get_json(silent=True) or {}
    urls = data.get("urls", [])
    if not isinstance(urls, list) or not urls:
        return response_error("Нет ссылок для обработки")

    results = []
    errors = []
    for index, raw_url in enumerate(urls[:MAX_BATCH_SIZE], start=1):
        try:
            url = normalize_url(str(raw_url))
            img_id = create_image_record(url)
            results.append(
                {
                    "id": img_id,
                    "unique_url": public_image_url(img_id),
                    "source_url": url,
                }
            )
        except ValueError as exc:
            errors.append({"index": index, "url": raw_url, "error": str(exc)})

    return jsonify(
        {
            "success": bool(results),
            "results": results,
            "errors": errors,
            "limit": MAX_BATCH_SIZE,
        }
    )


@app.get("/img/<img_id>")
def serve_image(img_id: str):
    row = get_image_record(img_id)
    if row is None:
        return "Not found", 404

    try:
        image = uniqualize_image(load_image_from_url(row["source_url"]))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92, optimize=True)
        buffer.seek(0)
        mark_hit(img_id)

        response = send_file(buffer, mimetype="image/jpeg", download_name=f"imguniq-{img_id}.jpg")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        response.headers["X-ImgUniq-ID"] = img_id
        return response
    except requests.RequestException:
        return "Source image is unavailable", 502
    except ValueError as exc:
        return str(exc), 422


@app.get("/api/stats")
def stats():
    with closing(db_connect()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total_registered, COALESCE(SUM(hits), 0) AS total_hits FROM images"
        ).fetchone()
    return jsonify(
        {
            "success": True,
            "total_registered": row["total_registered"],
            "total_hits": row["total_hits"],
            "max_batch_size": MAX_BATCH_SIZE,
            "storage": DATABASE_PATH,
        }
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
