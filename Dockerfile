# Family Table in one container: Python, FastAPI/Uvicorn, the app code, templates, and static files.
# The family's data (database and photos) is NOT in the image; compose.yaml mounts it at /data.

# Same Python version as the development .venv (3.14.4), on slim Debian 13.
FROM python:3.14.4-slim-trixie

# No .pyc files (the code folder is read-only), logs appear immediately, quieter pip.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Run as an ordinary user. Its id matches the WSL user that owns data/ (1000 by default),
# so the container can read and write the existing database and photos.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "$APP_GID" familytable \
    && useradd --uid "$APP_UID" --gid familytable --no-create-home --shell /usr/sbin/nologin familytable

WORKDIR /app

# Dependencies first, so code changes rebuild in seconds.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY scripts/container-start.sh /usr/local/bin/family-table-start

# Inside the container the data folder is always /data, and phones arrive over HTTPS.
ENV FAMILY_TABLE_DATA_DIR=/data \
    COOKIE_SECURE=true
RUN mkdir /data && chown familytable:familytable /data

USER familytable
EXPOSE 8000

# Checks the data folder, then runs Uvicorn in production mode (see the script for details).
CMD ["family-table-start"]
