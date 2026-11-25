# Build context is the repository root (the image bundles the source PDFs for the source viewer).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY services/chat/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY services/chat/ ./
COPY corpus/raw/ ./public/sources/

RUN useradd --system --uid 10002 --create-home chat && chown -R chat /app
USER chat

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/', timeout=4).status == 200 else 1)"
CMD ["sh", "-c", "python init_db.py && exec chainlit run app.py --host 0.0.0.0 --port 8080 --headless"]
