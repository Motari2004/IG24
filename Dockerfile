# Use an official Python 3.11 slim image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# 1. Install system tools needed for the setup
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy and install Python requirements first
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Use Playwright's built-in dependency installer
# This command detects your OS version and installs exactly what's needed
RUN playwright install-deps chromium
RUN playwright install chromium

# 4. Copy the rest of your app
COPY . .

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV RENDER=True

EXPOSE 10000

CMD ["gunicorn", "-k", "eventlet", "-w", "1", "--bind", "0.0.0.0:10000", "app:app"]