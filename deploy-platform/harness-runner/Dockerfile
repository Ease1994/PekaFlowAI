FROM python:3.11-slim

WORKDIR /runner
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.aliyun.com/pypi/simple/
COPY runner.py .

RUN useradd --uid 65532 --no-create-home --shell /usr/sbin/nologin harness
USER 65532:65532

EXPOSE 8090
CMD ["uvicorn", "runner:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1", "--no-access-log"]
