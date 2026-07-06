FROM python:3.12-slim

# PyMuPDF рендерит фигуры сам, системный poppler не нужен.
# libgl/шрифты — на случай экзотических PDF с растровыми вставками.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY olymp_parse.py service.py ./

EXPOSE 8080
ENV MAX_PDF_BYTES=41943040
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
