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
    fontconfig \
    fonts-liberation \
    fonts-dejavu-core \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# Guard the font fallback. With fonts-liberation alone and no generic-alias
# rules, `fc-match sans-serif` in this image resolved to Liberation Mono, so
# every rendered brochure came out in a monospace face no matter which brand
# font was detected. Fail the build rather than ship that silently again.
RUN fc-match sans-serif | grep -qi 'mono' \
    && (echo "sans-serif resolves to a monospace face: $(fc-match sans-serif)" && exit 1) \
    || echo "font fallback OK: $(fc-match sans-serif)"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/
COPY config/ config/
COPY backoffice/ backoffice/

EXPOSE 8000
EXPOSE 8501

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
