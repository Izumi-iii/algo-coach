FROM python:3.12-slim
WORKDIR /app
COPY src/algo_coach/sandbox_worker.py /app/worker.py
USER 65534:65534
ENTRYPOINT ["python", "-I", "-B", "/app/worker.py"]
