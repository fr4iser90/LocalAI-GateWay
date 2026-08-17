FROM python:3.13-alpine

RUN apk add --no-cache curl \
    && addgroup -S -g 10002 gpu \
    && adduser -S -u 10002 -G gpu -h /app -s /sbin/nologin gpu

WORKDIR /app
COPY main.py .
RUN chown gpu:gpu /app/main.py

ENV PORT=8080 \
    SYS_ROOT=/host/sys

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

USER gpu

CMD ["python", "main.py"]
