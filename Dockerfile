# Use official Playwright Python image
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set working directory
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Upgrade pip and install Python packages
RUN pip install --no-cache-dir --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers (Chromium, Firefox, WebKit)
# Install Playwright browsers (Chromium only to save space)
RUN playwright install chromium

# Copy application code
COPY . .

# Expose port
EXPOSE 5000

# Run the FastAPI app
# Run the FastAPI app using the PORT environment variable provided by Render
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-5000}
