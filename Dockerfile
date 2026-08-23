FROM python:3.12-slim

WORKDIR /app
ENV PYTHONPATH=/app

# weasyprint (HTML->PDF rendering for the brochure_v1 template) needs these
# system libs for Pango/Cairo text and graphics rendering — pure pip install
# is not enough.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY config/ config/
COPY backoffice/ backoffice/

EXPOSE 8000
EXPOSE 8501

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
