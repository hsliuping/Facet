# PlanRouter test image. The app is pure stdlib; only setuptools is needed at build time.
# Build:  docker compose build        (or: docker build -t facet-planrouter:test .)
# Offline build against a locally cached Python image:
#   docker build --build-arg BASE_IMAGE=<local-python-image> -t facet-planrouter:test .
# Run:    docker compose up -d

ARG BASE_IMAGE=python:3.12-slim

FROM ${BASE_IMAGE} AS builder
USER root
WORKDIR /src
COPY . .
# src/facet/data/ and src/facet/_version.py are generated and gitignored, but
# pyproject.toml reads version from facet._version. Generate them before building
# the wheel, otherwise `pip wheel .` fails on a clean checkout.
RUN python tools/build_package.py --skip-build \
    && (python -c "import setuptools" || python -m pip install "setuptools>=77") \
    && python -m pip wheel --no-build-isolation --no-deps --wheel-dir /wheels .

FROM ${BASE_IMAGE}
USER root
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Let the base image assign a free uid/gid; some images already occupy 1000.
RUN if id planrouter >/dev/null 2>&1; then :; \
    elif command -v useradd >/dev/null 2>&1; then \
      groupadd planrouter \
        && useradd -g planrouter --home-dir /data --no-create-home planrouter; \
    else \
      addgroup planrouter \
        && adduser -G planrouter -h /data -D planrouter; \
    fi \
    && mkdir -p /data /opt/planrouter \
    && chown -R planrouter:planrouter /data

COPY --from=builder /wheels/ /tmp/wheels/
RUN python -m pip install --no-index --no-cache-dir /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels

COPY docker/entrypoint.py /opt/planrouter/entrypoint.py
COPY docker/mock_upstream.py /opt/planrouter/mock_upstream.py
COPY docker/router.example.json /opt/planrouter/router.example.json
COPY docker/smoke_test.py /opt/planrouter/smoke_test.py

WORKDIR /data
USER planrouter
EXPOSE 8787

# "/" is the unauthenticated console page, so this is a pure liveness probe.
# "/health" needs the gateway key AND returns 503 while every channel is cooling,
# which would make Docker restart a perfectly healthy process.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/', timeout=3)"]

ENTRYPOINT ["python", "/opt/planrouter/entrypoint.py"]
