FROM python:3.9-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY gamma_scanner/ ./gamma_scanner/
COPY market_clock.py .

# Create data directory (trades, picks, logs)
RUN mkdir -p /app/gamma_scanner/static /app/data

# Environment variables (override these at deploy time)
ENV ALPACA_API_KEY=""
ENV ALPACA_SECRET_KEY=""
ENV PORT=8081

# Expose the API port
EXPOSE 8081

# Start script runs both the API server and profit monitor
COPY gamma_scanner/docker_start.sh /app/start.sh
RUN chmod +x /app/start.sh

CMD ["/app/start.sh"]
