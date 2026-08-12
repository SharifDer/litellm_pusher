FROM python:3.12-slim

RUN pip install --no-cache-dir requests

WORKDIR /app
COPY push_logs.py .

ENTRYPOINT ["python", "push_logs.py"]
