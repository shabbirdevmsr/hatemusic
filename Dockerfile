FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p audio images videos uploads state/sessions

EXPOSE 5000

# Run Flask (for upload + files) AND the bot in the same container
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:5000 --workers 1 --timeout 600 app:app & python bot.py"]
