FROM python:3.12-slim

# OpenCV needs these even in headless builds.
RUN apt-get update && apt-get install -y --no-install-recommends \
      libglib2.0-0 libgl1 libsm6 libxext6 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Headless build: ~200MB smaller and there's no display in a container anyway.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y opencv-python \
    && pip install --no-cache-dir opencv-python-headless

COPY . .

# SQLite lives here. On Render this MUST be a mounted disk or every deploy and
# every restart wipes your labelled events - which are the only thing in this
# project that can't be regenerated.
VOLUME ["/app/data"]

ENV PORT=8000
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}"]
