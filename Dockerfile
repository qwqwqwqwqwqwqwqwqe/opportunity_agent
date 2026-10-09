FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml README.md ./
# Setuptools needs a package to build the local distribution, but copying the
# whole source tree here would invalidate the expensive dependency layer for
# every Python edit. The complete package is copied after dependencies install.
COPY opportunity_agent/__init__.py ./opportunity_agent/__init__.py
# python:3.12-slim currently ships pip 25.0.1, which crashes while evaluating
# this project's optional-dependency markers (InvalidVersion: 'dev'). Upgrade
# the installer before resolving the V2 extras.
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --upgrade pip \
    && python -m pip install --no-cache-dir --index-url "$PIP_INDEX_URL" ".[v2,resume,observability]"
# Runtime only needs the Python package. Do not copy the entire repository:
# tests, local data, build artifacts, and temporary files needlessly enlarge
# the build context and do not belong in the API/worker image.
COPY opportunity_agent ./opportunity_agent
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY data/university_domains.json ./data/university_domains.json
CMD ["sh", "-c", "python -m alembic upgrade head && exec uvicorn opportunity_agent.v2.api.app:app --host 0.0.0.0 --port 8000"]
