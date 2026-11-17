# Docker skeleton (P0-08): the server wheel on a pinned Python slim base, run as a non-root user.
# Built in CI on pull requests that change it (job `docker-build`); not published (P9-08).

# python:3.15.2-slim-trixie (digest from docker-library/repo-info, 2026-10-27)
FROM python:3.16.0a1-slim-trixie@sha256:df293ba3b89f458e1aad6051b62ff1c4fad7b3b52b8147ae9cc4b667f9cf2741 AS build
WORKDIR /source
COPY packages/ packages/
RUN for package in packages/*/; do \
        python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels "$package"; \
    done

FROM python:3.16.0a1-slim-trixie@sha256:df293ba3b89f458e1aad6051b62ff1c4fad7b3b52b8147ae9cc4b667f9cf2741
COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels \
    && useradd --create-home --uid 10001 app
USER 10001
WORKDIR /home/app
# Local mode binds to the loopback interface only, so until server mode (P9-08) the image's
# default command reports the installed versions.
ENTRYPOINT ["python", "-m", "orvex.server"]
CMD ["--version"]
