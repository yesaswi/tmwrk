FROM python:3.12.0-slim

WORKDIR /app

RUN apt update && apt upgrade -y

COPY . .

RUN python -m pip install -r requirements.txt

RUN playwright install chromium

RUN playwright install-deps

COPY main.py .

EXPOSE 8080

CMD uvicorn 'main:app' --host=0.0.0.0 --port=8080 --log-level trace --use-colors
