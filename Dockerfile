# Render deployment only -- Vercel's serverless functions can't run a
# Dockerfile with a system package install, which is the entire reason
# this file exists: it's what actually gets Tesseract onto the machine
# running document_qa.py's photo-OCR path, instead of that feature
# permanently degrading to its honest "OCR isn't available" fallback.
FROM python:3.12-slim

# tesseract-ocr is the one thing Vercel's deployment structurally can't
# install -- see render_main.py's own docstring for why this deployment
# exists at all. --no-install-recommends keeps the image lean; the apt
# list cleanup keeps it from bloating the final layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets $PORT itself and routes external traffic to whatever this
# container actually listens on -- render_main.py reads it, this is just
# a sane local default for `docker run` without -e PORT=... set.
ENV PORT=8000
EXPOSE 8000

CMD ["python", "render_main.py"]
