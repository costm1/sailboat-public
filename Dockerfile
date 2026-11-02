FROM python:3.12-slim
RUN useradd -m -u 1000 app
WORKDIR /app
COPY pyproject.toml README.md LICENSE constraints-tested.txt ./
COPY sailboat ./sailboat
COPY sailboat_core ./sailboat_core
COPY sailboat_assembly ./sailboat_assembly
RUN pip install --no-cache-dir -c constraints-tested.txt .
USER app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/tmp/hf-cache HF_HUB_DISABLE_IMPLICIT_TOKEN=1 SAILBOAT_BIND_HOST=0.0.0.0
EXPOSE 7860
CMD ["python", "-m", "sailboat.cloud_server"]
