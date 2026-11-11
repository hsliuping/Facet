# PlanRouter test image. The app is pure stdlib; only setuptools is needed at build time.
# Build:  docker compose build        (or: docker build -t facet-planrouter:test .)
# Offline build against a locally cached Python image:
#   docker build --build-arg BASE_IMAGE=<local-python-image> -t facet-planrouter:test .
# Run:    docker compose up -d

ARG BASE_IMAGE=python:3.12-slim
# DSH (DeepSeek Harness) is a Node CLI the console spawns; it ships inside the
# image so the browser entry works without a host-side npm install. Offline
# builds can point NODE_IMAGE at a locally cached node:22-slim.
ARG NODE_IMAGE=docker.m.daocloud.io/library/node:22-slim
ARG DSH_VERSION=0.2.2-rc.1
# Bailian CLI (`bl`) that the free-align sweep shells out to for
# `bl usage free`. Its console endpoints accept a token exchanged from the
# RAM AccessKey pair (BAILIAN_ACCESS_KEY_ID/SECRET), so no browser login is
# needed; an optional `bl auth login` state lives in ~/.bailian
# (= /data/.bailian, inside the persisted data volume).
ARG BAILIAN_CLI_VERSION=2.8.3

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

# Install the DSH CLI once, then copy only its runtime pieces into the final
# image: the node binary, the C++ runtime node links against (the python base
# image has libgcc but not libstdc++), and the resolved package tree. npm's
# own tooling is not needed at run time.
FROM ${NODE_IMAGE} AS dsh
ARG DSH_VERSION
ARG BAILIAN_CLI_VERSION
USER root
RUN npm install -g "@deepseek-ai/dsh@${DSH_VERSION}" \
    && npm install --ignore-scripts --prefix /opt/bailian \
       "bailian-cli@${BAILIAN_CLI_VERSION}" \
    && npm cache clean --force

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
# Console usage manual (docs/api-key-guide.md + its screenshots). ui.py looks
# here when the package is installed without a repository checkout.
COPY docs/ /opt/planrouter/docs/
COPY docker/smoke_test.py /opt/planrouter/smoke_test.py
# Shipped DSH profile: entrypoint copies it into $HOME/.dsh on first run.
COPY docker/dsh-profile/ /opt/planrouter/dsh-profile/

COPY --from=dsh /usr/local/bin/node /usr/local/bin/node
COPY --from=dsh /usr/lib/x86_64-linux-gnu/libstdc++.so.6* /usr/lib/x86_64-linux-gnu/
COPY --from=dsh /usr/local/lib/node_modules/@deepseek-ai /usr/local/lib/node_modules/@deepseek-ai
# The console resolves the npm shim to its package entry, so the link has to
# land on lib/bin.js rather than a wrapper script.
RUN ln -s ../lib/node_modules/@deepseek-ai/dsh/lib/bin.js /usr/local/bin/dsh \
    && chmod +x /usr/local/bin/dsh \
    && node --version

# Bailian CLI for the free-quota sweep. Installed under /opt so npm's bundled
# tree stays out of the image; it only needs node (copied above) and keeps its
# console-login state in BAILIAN_CONFIG_DIR (/data/.bailian by compose).
COPY --from=dsh /opt/bailian /opt/bailian
RUN ln -s /opt/bailian/node_modules/bailian-cli/dist/bailian.mjs /usr/local/bin/bl \
    && chmod +x /opt/bailian/node_modules/bailian-cli/dist/bailian.mjs \
    && bl --version

WORKDIR /data
USER planrouter
# 8787 is the gateway/console; 3080 is the DSH web UI the console launches.
EXPOSE 8787 3080

# "/" is the unauthenticated console page, so this is a pure liveness probe.
# "/health" needs the gateway key AND returns 503 while every channel is cooling,
# which would make Docker restart a perfectly healthy process.
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/', timeout=3)"]

ENTRYPOINT ["python", "/opt/planrouter/entrypoint.py"]
