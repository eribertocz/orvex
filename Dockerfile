# Docker skeleton (P0-08): the server wheel on a pinned Python slim base, run as a non-root user.
# Built in CI on pull requests that change it (job `docker-build`); not published (P9-08).

# python:3.15.2-slim-trixie (digest from docker-library/repo-info, 2026-10-27)
FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
WORKDIR /source
COPY packages/ packages/
RUN for package in packages/*/; do \
        python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels "$package"; \
    done

FROM python:3.15.2-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
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
