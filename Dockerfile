# Docker skeleton (P0-08): the server wheel on a pinned Python slim base, run as a non-root user.
# Built in CI on pull requests that change it (job `docker-build`); not published (P9-08).

# python:3.15.2-slim-trixie (digest from docker-library/repo-info, 2026-10-27)
FROM python:3.15.2-slim-trixie@sha256:197f6622a70b727a4e61bdac13a73b4e2b914b896aa5a729ceb1f45a09bde5fa AS build
WORKDIR /source
COPY packages/ packages/
RUN for package in packages/*/; do \
        python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels "$package"; \
    done

FROM python:3.15.2-slim-trixie@sha256:197f6622a70b727a4e61bdac13a73b4e2b914b896aa5a729ceb1f45a09bde5fa
COPY --from=build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels \
    && useradd --create-home --uid 10001 app
USER 10001
WORKDIR /home/app
# Local mode binds to the loopback interface only, so until server mode (P9-08) the image's
# default command reports the installed versions.
ENTRYPOINT ["python", "-m", "sextant.server"]
CMD ["--version"]
