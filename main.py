import os
import io
import uuid
import json
import time
import random
import hashlib
import urllib.request
from flask import Flask, request, jsonify, send_file, render_template_string
from flask_cors import CORS
from PIL import Image, ImageFilter, ImageEnhance
import numpy as np

app = Flask(__name__)
CORS(app)

# In-memory storage (для Railway - можно потом подключить Redis)
images_db = {}

def uniqualize_image(img: Image.Image) -> Image.Image:
    """Применяет случайные уникализирующие трансформации к изображению"""
    img = img.copy()
    
    # 1. Случайный шум (очень лёгкий, незаметный)
    img_array = np.array(img, dtype=np.float32)
    noise_level = random.uniform(0.3, 1.2)
    noise = np.random.normal(0, noise_level, img_array.shape)
    img_array = np.clip(img_array + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(img_array)
    
    # 2. Микросдвиг яркости (±0.5-1.5%)
    brightness_factor = random.uniform(0.992, 1.008)
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(brightness_factor)
    
    # 3. Микросдвиг контраста (±0.3-0.8%)
    contrast_factor = random.uniform(0.995, 1.005)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(contrast_factor)
    
    # 4. Случайный лёгкий blur или sharpen (50/50)
    if random.random() > 0.5:
        radius = random.uniform(0.05, 0.15)
        img = img.filter(ImageFilter.GaussianBlur(radius=radius))
    else:
        factor = random.uniform(1.01, 1.04)
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(factor)
    
    # 5. Случайный кроп на 1-3 пикселя с ресайзом обратно
    w, h = img.size
    crop_px = random.randint(0, 2)
    if crop_px > 0:
        left = random.randint(0, crop_px)
        top = random.randint(0, crop_px)
        right = w - random.randint(0, crop_px)
        bottom = h - random.randint(0, crop_px)
        if right > left and bottom > top:
            img = img.crop((left, top, right, bottom))
            img = img.resize((w, h), Image.LANCZOS)
    
    # 6. Случайное изменение насыщенности (очень лёгкое)
    saturation_factor = random.uniform(0.994, 1.006)
    enhancer = ImageEnhance.Color(img)
    img = enhancer.enhance(saturation_factor)
    
    return img


def load_image_from_url(url: str) -> Image.Image:
    """Загружает изображение по URL"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as response:
        data = response.read()
    return Image.open(io.BytesIO(data)).convert('RGB')


# ─── HTML интерфейс ───────────────────────────────────────────────────────────

HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ImgUniq — Уникализатор изображений</title>
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
  <header>
    <div class="logo-badge">
      <div class="logo-dot"></div>
      <span class="logo-text">ImgUniq v1.0</span>
    </div>
    <h1>Уникализация<br><span>без следов</span></h1>
    <p class="subtitle">Вставь ссылку на скриншот — получи статическую ссылку,<br>по которой каждый раз будет новая уникальная версия</p>
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
        placeholder="https://screenshoter-renderer.kapps.pro/device/..."
        autocomplete="off"
        spellcheck="false"
      >
      <button class="btn btn-primary" id="addBtn" onclick="addSingleUrl()">
        <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14M5 12h14"/></svg>
        Добавить
      </button>
    </div>

    <div class="urls-section">
      <div class="urls-header">
        <div class="card-label" style="margin:0">Или вставь сразу несколько ссылок</div>
        <span class="url-count" id="urlCount">0 ссылок</span>
      </div>
      <textarea 
        class="urls-textarea" 
        id="urlsTextarea"
        placeholder="Вставь несколько ссылок — каждую с новой строки..."
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

<footer>ImgUniq — каждый рендер уникален</footer>

<div class="toast" id="toast">
  <div class="toast-dot"></div>
  <span id="toastMsg">Скопировано</span>
</div>

<script>
const BASE = window.location.origin;
let sessionCount = 0;
let totalCount = 0;
let allResultUrls = [];

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
      body: JSON.stringify({url})
    });
    const data = await res.json();
    
    if (data.success) {
      addResultItem(data.unique_url, data.id);
      document.getElementById('urlInput').value = '';
      showToast('Ссылка создана');
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
      body: JSON.stringify({urls: lines})
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

// Enter to submit
document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') addSingleUrl();
});
</script>
</body>
</html>'''


# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template_string(HTML)


@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    url = data.get('url', '').strip()
    
    if not url or not url.startswith('http'):
        return jsonify({'success': False, 'error': 'Некорректная ссылка'})
    
    img_id = str(uuid.uuid4())[:12]
    images_db[img_id] = {
        'url': url,
        'created': time.time()
    }
    
    base_url = request.host_url.rstrip('/')
    unique_url = f"{base_url}/img/{img_id}"
    
    return jsonify({
        'success': True,
        'id': img_id,
        'unique_url': unique_url,
        'source_url': url
    })


@app.route('/api/register-batch', methods=['POST'])
def register_batch():
    data = request.json
    urls = data.get('urls', [])
    
    if not urls:
        return jsonify({'success': False, 'error': 'Нет ссылок'})
    
    results = []
    base_url = request.host_url.rstrip('/')
    
    for url in urls[:50]:  # лимит 50 за раз
        url = url.strip()
        if not url.startswith('http'):
            continue
        
        img_id = str(uuid.uuid4())[:12]
        images_db[img_id] = {
            'url': url,
            'created': time.time()
        }
        
        unique_url = f"{base_url}/img/{img_id}"
        results.append({
            'id': img_id,
            'unique_url': unique_url,
            'source_url': url
        })
    
    return jsonify({'success': True, 'results': results})


@app.route('/img/<img_id>')
def serve_image(img_id):
    if img_id not in images_db:
        return 'Not found', 404
    
    source_url = images_db[img_id]['url']
    
    try:
        img = load_image_from_url(source_url)
        img = uniqualize_image(img)
        
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=92)
        buf.seek(0)
        
        response = send_file(buf, mimetype='image/jpeg')
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
        
    except Exception as e:
        return f'Error loading image: {str(e)}', 500


@app.route('/api/stats')
def stats():
    return jsonify({
        'total_registered': len(images_db),
        'uptime': 'ok'
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
