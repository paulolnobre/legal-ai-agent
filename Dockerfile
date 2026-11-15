FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG DEBIAN_FRONTEND=noninteractive

# ffmpeg/ffprobe decode attachments from the panel's chat (core/services/media.py);
# prlimit, which bounds them, comes with util-linux, part of every Debian image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       poppler-utils \
       tesseract-ocr \
       tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY core/ ./core/
COPY clients/ ./clients/

# Deploy metadata, written by CD before it synchronizes the release. The glob
# makes it optional: a local build has no such file and must still succeed,
# and the application reports an unknown commit rather than failing.
#
# requirements.txt is named again only to keep the glob company: a COPY whose
# every source is a glob that matches nothing fails the build outright, which
# is exactly what a checkout without the file would be.
COPY requirements.txt .deployed_sha* ./

RUN addgroup --system app && adduser --system --ingroup app app \
    && mkdir -p /data/documents \
    && chown -R app:app /data
USER app

EXPOSE 8000

CMD ["uvicorn", "core.web.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
