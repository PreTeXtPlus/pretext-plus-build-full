# The only way out of a build container. Builds run on the internal
# `pretext-build-egress` network (no route anywhere), and this proxy, which also
# sits on the compose default network, forwards HTTP(S) to the hosts listed in
# EGRESS_ALLOW_HOSTS and refuses everything else. See README "Network access".
FROM alpine:3.20

RUN apk add --no-cache tinyproxy

COPY tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh

# Port 8888 needs no privileges; the filter file is written to /tmp at start.
USER nobody
EXPOSE 8888
ENTRYPOINT ["/entrypoint.sh"]
