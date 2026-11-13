FROM python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app

COPY api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared ./shared
COPY api ./api

CMD ["uvicorn", "api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "9870"]
