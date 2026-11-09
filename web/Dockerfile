FROM nginx:1.28-alpine

RUN apk add --no-cache python3
COPY exporter.py /opt/monitoring-lab/exporter.py
COPY start.sh /usr/local/bin/start-monitoring-lab
RUN chmod 755 /usr/local/bin/start-monitoring-lab

ENTRYPOINT ["/usr/local/bin/start-monitoring-lab"]
