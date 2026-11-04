# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM python:3.13-slim-bookworm@sha256:281d39ff02e3b389401586134ff9e6572954687822a58055ec07a60ded4e0b7f

# App version, stamped from the release tag by CI (docker-publish.yml build-args).
ARG VERSION=0.0.0-dev
ENV APP_VERSION=${VERSION} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CARDHOUND_DATA=/data \
    HOME=/tmp

LABEL org.opencontainers.image.source="https://github.com/LukeEvansTech/cardhound" \
      org.opencontainers.image.title="cardhound" \
      org.opencontainers.image.description="Trading-card deal finder with a deal page per alert" \
      org.opencontainers.image.version="${VERSION}"

WORKDIR /app
COPY requirements.txt ./
# Chromium and its system libraries come from Playwright's own installer, which pins the browser
# build to the playwright package version in requirements.txt.
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R a+rX /ms-playwright

COPY cardhound ./cardhound

RUN mkdir -p /data && chown -R 65532:65532 /data
VOLUME ["/data"]

USER 65532:65532
# Kubernetes probes own liveness (/health); an image healthcheck would double-probe.
HEALTHCHECK NONE
EXPOSE 8080
ENTRYPOINT ["python", "-m", "cardhound"]
CMD ["serve"]
