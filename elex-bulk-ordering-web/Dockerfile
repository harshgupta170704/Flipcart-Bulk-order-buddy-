# --- build the SPA ----------------------------------------------------------
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* ./
# Use a lockfile install when one exists, otherwise fall back to a plain install.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .
RUN npm run build

# --- serve it ---------------------------------------------------------------
FROM nginx:alpine

# nginx.conf is the site config, installed where nginx reads it, as before. It
# is also kept as the template that config is rendered from at every start:
# the image's entrypoint first runs each executable script in
# /docker-entrypoint.d/, and 40-trusted-proxies.sh adds the realip lines for
# ORDERING_TRUSTED_PROXIES (the TLS proxy or load balancer in front, audit
# F-30), or stops the start on a value that is not an address. Unset, the
# config is nginx.conf exactly. Not the image's own /etc/nginx/templates: its
# envsubst cannot turn one list into a line per address, and it would rewrite
# any "$name" in the config that an environment variable happened to share.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/ordering/nginx.conf.template
COPY docker-entrypoint.d/40-trusted-proxies.sh /docker-entrypoint.d/
# The entrypoint passes over a script without its executable bit (with one
# easily missed log line), and the setting would then quietly not apply.
RUN chmod 755 /docker-entrypoint.d/40-trusted-proxies.sh
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
