FROM python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app

COPY bot/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY shared ./shared
COPY bot ./bot

CMD ["python", "-m", "bot.main"]
