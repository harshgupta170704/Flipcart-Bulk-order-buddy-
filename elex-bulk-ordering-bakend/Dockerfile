# Playwright's official Python image already contains Chromium and every system
# library the browser needs — far more reliable than installing them by hand.
# The tag MUST stay in step with the playwright version in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# The schema migrations (docs/DEPLOY.md §5). Compose's one-shot "migrate"
# service runs "alembic upgrade head" from this image before the API starts.
COPY alembic.ini ./
COPY alembic ./alembic

# Browsers are happier not running as root.
RUN chown -R pwuser:pwuser /app
USER pwuser

EXPOSE 8000

# The API always sits behind a reverse proxy — nginx in docker-compose.yml, a
# load balancer in the cloud — on a private network, and its port is never
# published to the outside. So uvicorn may trust X-Forwarded-For from private
# addresses: request.client then names the browser, which the sign-in
# throttle keys on, and not the proxy (one address for everyone, which five
# wrong passwords would lock out). A browser cannot name its own address this
# way because nginx (frontend/nginx.conf) REPLACES X-Forwarded-For with the
# one address it saw — it never appends to the browser's header, which would
# let a browser on a private address pick the entry uvicorn trusts. Never set
# this to "*" where a browser could reach this port directly — "*" takes the
# left-most entry, which is whatever the browser sent.
ENV FORWARDED_ALLOW_IPS="127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7"

# Overridden by docker-compose for the worker service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
