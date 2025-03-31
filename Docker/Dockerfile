FROM python:3-alpine

# Set environment variables to prevent interactive prompts during apt-get and improve Python behavior
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set the working directory
WORKDIR /app

# Copy project files into the working directory
COPY . /app

# Install dependencies using pip
# --no-cache-dir is good practice for smaller layers
# --upgrade pip ensures we have the latest pip
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Expose the port the app runs on (matches settings.py)
EXPOSE 8699

# 设置容器启动时运行的命令，使用 uvicorn 启动 FastAPI 应用
# Point to the module and the FastAPI app instance within that module
CMD ["uvicorn", "jmcomic_api_fast.core.main:app", "--host", "0.0.0.0", "--port", "8699"]
