FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    CB_BIND=0.0.0.0:8765 CB_DATA_DIR=/var/lib/cred-broker
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 broker && useradd --uid 10001 --gid broker --no-create-home broker \
    && install -d -o broker -g broker -m 0700 /var/lib/cred-broker
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip==26.3 \
    && python -m pip install --no-cache-dir -r requirements.txt
COPY tools/install_instabox.py /tmp/install_instabox.py
RUN python /tmp/install_instabox.py && rm /tmp/install_instabox.py
COPY cred_broker ./cred_broker
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8765/healthz',timeout=3)"]
CMD ["python", "-m", "cred_broker"]
