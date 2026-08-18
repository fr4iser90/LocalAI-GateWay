FROM python:3.13-alpine

RUN apk add --no-cache curl \
    && addgroup -S -g 10002 sidecar \
    && adduser -S -u 10002 -G sidecar -h /app -s /sbin/nologin sidecar

WORKDIR /app
COPY *.py .
RUN chown sidecar:sidecar /app/*.py

ENV PORT=8080 \
    SYS_ROOT=/host/sys \
    TEMP_THERMAL_ROOT=/host/sys/class/thermal

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

USER sidecar

CMD ["python", "main.py"]
