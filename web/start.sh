#!/bin/sh
set -eu

# Новая жизнь контейнера — новые накопительные счётчики и новый журнал метрик.
: > /var/log/nginx/monitoring-metrics.log
python3 /opt/monitoring-lab/exporter.py &
exec /docker-entrypoint.sh nginx -g 'daemon off;'
