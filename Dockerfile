FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt pyproject.toml ./
COPY src ./src
COPY tests ./tests

RUN pip install --no-cache-dir -r requirements.txt

ENV PYTHONPATH=/app/src
ENV BACKEND_HOST=0.0.0.0
ENV BACKEND_PORT=8000

EXPOSE 8000
CMD ["python", "-m", "shelfwise_backend"]
