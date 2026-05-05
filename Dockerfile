# Use a slim Python image for a smaller footprint
FROM python:3.13-slim-bookworm

# Set the working directory inside the container
WORKDIR /app

# Prevent Python from writing .pyc files and enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies if needed (none strictly for these libs, but good practice)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bridge script into the container
COPY bridge.py test.py ./

# Run the script
CMD ["python", "bridge.py"]
