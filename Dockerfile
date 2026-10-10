FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
COPY app ./app
COPY prompts ./prompts
COPY static ./static
COPY templates ./templates
COPY run.py .
EXPOSE 8000
CMD ["python", "run.py"]
