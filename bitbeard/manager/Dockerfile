FROM python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app

COPY manager/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared ./shared
COPY manager ./manager

CMD ["uvicorn", "manager.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "9871"]
