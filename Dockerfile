FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 aie \
    && mkdir -p /app/data/raw /app/data/processed /app/data/database \
    && chown -R aie:aie /app

USER aie

CMD ["python", "run_dashboard.py", "--host", "0.0.0.0", "--port", "8000"]
