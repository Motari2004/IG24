# Use an official Python 3.11 slim image
FROM python:3.11-slim

# Install system dependencies for Playwright and Chromium
# These are required to run a headless browser in a Linux container
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    libgbm-dev \
    libnss3 \
    libnspr4 \
    libasound2 \
    libatk-1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libfontconfig1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libpango-1.0-0 \
    libx11-6 \
    libxcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxshmfence1 \
    libxtst6 \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install the Chromium browser via Playwright
# This puts the browser in the container's internal cache
RUN playwright install chromium

# Copy the rest of your application code
COPY . .

# Set environment variables
# PYTHONUNBUFFERED ensures logs are sent to the console immediately
ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV RENDER=True

# Expose the port your app runs on
EXPOSE 10000

# Start the application using Gunicorn with the eventlet worker
CMD ["gunicorn", "-k", "eventlet", "-w", "1", "--bind", "0.0.0.0:10000", "app:app"]