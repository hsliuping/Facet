# Browser cache layer for the combined image.
# Building the Chromium download inside Dockerfile.combined is slow behind
# flaky CDNs, so the browsers are extracted once from an already-provisioned
# image into their own tiny layer (see PLAYWRIGHT_BROWSERS_IMAGE below).
#
# Build:  docker build -f docker/playwright-browsers.Dockerfile \
#           --build-arg BROWSERS_SOURCE_IMAGE=<image-with-/ms-playwright> \
#           -t facet-playwright-browsers:v1161 .

ARG BROWSERS_SOURCE_IMAGE=facet-combined:latest
FROM ${BROWSERS_SOURCE_IMAGE} AS browsers

FROM scratch
COPY --from=browsers /ms-playwright/ /ms-playwright/
