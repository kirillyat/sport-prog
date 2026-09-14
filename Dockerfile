FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    PYTHONPATH=/app

WORKDIR /app

# Зависимости ставим отдельным слоем, чтобы правки кода не пересобирали их.
# Список берём из pyproject, чтобы не заводить второй источник правды.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
 && python -c "import tomllib;print(chr(10).join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt

# Пакет не устанавливаем: WORKDIR=/app, поэтому `import app` работает как есть.
COPY app ./app
COPY course ./course
COPY migrations ./migrations
COPY alembic.ini entrypoint.sh ./
RUN chmod +x entrypoint.sh

# База лежит в томе, значит писать в неё должен непривилегированный пользователь.
RUN useradd --create-home --uid 10001 sport && mkdir -p /data && chown -R sport:sport /data /app
USER sport

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
