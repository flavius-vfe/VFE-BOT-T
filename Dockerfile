FROM python:3.12-slim

LABEL org.opencontainers.image.title="VFE-BOT-T" \
      org.opencontainers.image.description="Telegram Docker controller for Unraid" \
      org.opencontainers.image.source="https://github.com/flavius-vfe/VFE-BOT-T" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
COPY tools/collect_licenses.py /tmp/collect_licenses.py
RUN pip install --no-cache-dir --report /tmp/pip-install-report.json -r requirements.txt \
    && python /tmp/collect_licenses.py \
         --report /tmp/pip-install-report.json \
         --output /usr/share/licenses/vfe-bot-t/python-packages \
    && rm -f /tmp/collect_licenses.py /tmp/pip-install-report.json

COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/licenses/vfe-bot-t/
COPY licenses/third-party /usr/share/licenses/vfe-bot-t/direct-dependencies/
COPY VERSION ./VERSION
COPY vfe_bot ./vfe_bot

CMD ["python", "-m", "vfe_bot.main"]
