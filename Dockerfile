# iStealClips Web Dashboard — Docker Image
FROM python:3.11-slim

# Install FFmpeg, system graphics libraries & Linux TrueType fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    fonts-dejavu-core \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Create working directories
RUN mkdir -p uploads/raw uploads/edited uploads/temp data logos backgrounds temp data/fonts

# Expose port
EXPOSE 8000

# Start web server using uvicorn
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
