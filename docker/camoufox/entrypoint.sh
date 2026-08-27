#!/bin/bash
set -e

# Указываем X11 дисплей
export DISPLAY=:1

# Очищаем старые X11 lock-файлы перед запуском (чтобы Xvfb не падал после перезапуска контейнера)
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1

# Скачиваем/проверяем бинарники Camoufox при старте контейнера (сохраняется в volume)
echo "Checking Camoufox browser installation..."
python -m camoufox fetch

echo "Starting Xvfb (Virtual Framebuffer) on display :1..."
Xvfb :1 -screen 0 1280x720x24 &
sleep 1

echo "Starting Fluxbox window manager..."
fluxbox &
sleep 1

echo "Starting x11vnc server on port 5900..."
x11vnc -display :1 -nopw -forever -shared -rfbport 5900 &
sleep 1

echo "Starting noVNC proxy on port 6080..."
if [ -f /usr/share/novnc/utils/novnc_proxy ]; then
    /usr/share/novnc/utils/novnc_proxy --vnc localhost:5900 --listen 6080 &
elif [ -f /usr/bin/novnc_proxy ]; then
    /usr/bin/novnc_proxy --vnc localhost:5900 --listen 6080 &
else
    echo "Warning: noVNC proxy command not found!"
fi
sleep 1

echo "Starting Camoufox websocket server..."
exec python server.py
