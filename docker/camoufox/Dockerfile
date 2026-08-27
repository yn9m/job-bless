FROM python:3.12-slim

# Установка системных зависимостей для Firefox, X11, VNC и noVNC
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    fluxbox \
    curl \
    ca-certificates \
    # Зависимости для Firefox / Camoufox
    libgtk-3-0 \
    libasound2 \
    libdbus-1-3 \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libxtst6 \
    libpango-1.0-0 \
    libcairo2 \
    && rm -rf /var/lib/apt-get/lists/*

WORKDIR /app

# Установка python-пакетов
RUN pip install --no-cache-dir camoufox playwright

COPY entrypoint.sh ./
COPY server.py ./

RUN chmod +x entrypoint.sh

# Порт 6080 для noVNC (интерфейс в браузере)
# Порт 3000 для gRPC/Playwright WebSocket
EXPOSE 6080 3000

ENTRYPOINT ["./entrypoint.sh"]
