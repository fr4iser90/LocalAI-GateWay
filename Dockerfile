FROM python:3.13-alpine

WORKDIR /app

RUN apk add --no-cache curl tzdata

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/data
ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
