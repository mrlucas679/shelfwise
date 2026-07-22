FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt pyproject.toml ./

RUN pip install --no-cache-dir -r requirements.txt

# data/ ships seeded runtime datasets the image is contractually required to carry
# (tests/test_infra_config.py::test_backend_image_contains_seeded_runtime_datasets).
# tests/ is not: it is executed by CI directly against the repo, never inside this
# image (see .github/workflows/ci.yml), and no backend startup path reads it.
COPY src ./src
COPY data ./data

ENV PYTHONPATH=/app/src
ENV PYTHONDONTWRITEBYTECODE=1
ENV BACKEND_HOST=0.0.0.0
ENV BACKEND_PORT=8000

RUN adduser --disabled-password --gecos "" --uid 10001 appuser
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=3s --start-period=5s --retries=10 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()"
CMD ["python", "-m", "shelfwise_backend"]
