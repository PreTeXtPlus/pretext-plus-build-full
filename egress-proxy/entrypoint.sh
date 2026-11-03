#!/bin/sh
# Turn EGRESS_ALLOW_HOSTS (comma-separated host names) into tinyproxy's filter
# file, one anchored regex per host, then run tinyproxy in the foreground.
# An empty list leaves the filter empty, which (with FilterDefaultDeny) refuses
# every request.
set -eu

filter=/tmp/egress-filter
: > "$filter"

for host in $(printf '%s' "${EGRESS_ALLOW_HOSTS:-}" | tr ',' ' '); do
  # Host names only: no schemes, ports, paths, or wildcards, so nothing in
  # the list can widen the regex beyond one exact name.
  if ! printf '%s' "$host" | grep -Eq '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$'; then
    echo "egress-proxy: invalid host in EGRESS_ALLOW_HOSTS: '$host'" >&2
    exit 1
  fi
  printf '^%s$\n' "$(printf '%s' "$host" | sed 's/\./\\./g')" >> "$filter"
  echo "egress-proxy: allowing $host"
done

[ -s "$filter" ] || echo "egress-proxy: EGRESS_ALLOW_HOSTS is empty; refusing all requests" >&2

exec tinyproxy -d -c /etc/tinyproxy/tinyproxy.conf
