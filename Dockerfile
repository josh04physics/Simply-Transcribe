# Base Python image
FROM python:3.11-slim

# Install system dependencies needed for TeXLive and font support
RUN apt-get update && apt-get install -y \
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

# Make ffmpeg executable and move it to a global path
RUN chmod +x ./bin/ffmpeg && mv ./bin/ffmpeg /usr/local/bin/ffmpeg

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Optional: unbuffer logs for Docker
ENV PYTHONUNBUFFERED=1

# Expose the port your Flask app uses
EXPOSE 5000

# Run the Flask app
CMD ["python", "app.py"]
