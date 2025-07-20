# Base Python image
FROM python:3.11-slim

# Install system dependencies including ffmpeg (which includes ffprobe), TeXLive and font support
RUN apt-get update && apt-get install -y \
    ffmpeg \
    texlive-latex-base \
    texlive-fonts-recommended \
    texlive-latex-extra \
    fontconfig \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy all project files (including ./bin/ffmpeg)
COPY . /app

# Make local ffmpeg executable and move to global path if you still want to override system ffmpeg
RUN chmod +x ./bin/ffmpeg && mv ./bin/ffmpeg /usr/local/bin/ffmpeg || echo "No local ffmpeg binary to move"

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Optional: unbuffer logs for Docker
ENV PYTHONUNBUFFERED=1

# Expose the port your Flask app uses
EXPOSE 5000

# Run the Flask app
CMD ["python", "app.py"]