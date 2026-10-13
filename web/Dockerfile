# The web server image: Caddy with this folder's Caddyfile (see docker-compose.yml).
FROM caddy:2-alpine

COPY Caddyfile /etc/caddy/Caddyfile

# A mistake in the Caddyfile stops the build here, while the old container keeps
# serving the site, instead of leaving the site without a web server.
RUN caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
