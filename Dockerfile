FROM python:3.12-alpine

WORKDIR /app
COPY db.py engine.py app.py notify.py /app/
COPY static /app/static

ENV DATA_DIR=/data \
    PORT=8080 \
    LISTEN=0.0.0.0 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=8s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"

CMD ["python", "-u", "/app/app.py"]
