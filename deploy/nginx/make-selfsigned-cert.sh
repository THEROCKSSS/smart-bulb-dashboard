#!/usr/bin/env bash
# Generate a self-signed certificate for LAN-only HTTPS (W2-042), for use
# with deploy/nginx/smart-bulb-dashboard.conf when there is no public
# domain and therefore no Let's Encrypt.
#
# If you use Caddy instead, don't run this -- deploy/caddy/Caddyfile.lan-selfsigned
# does the same job with `tls internal`, and Caddy renews the cert itself.
#
# Usage:
#     ./make-selfsigned-cert.sh bulbs.lan 192.168.1.20
#     ./make-selfsigned-cert.sh bulbs.lan 192.168.1.20 /etc/nginx/ssl
#
# Every argument after the first is added as a Subject Alternative Name, so
# pass every hostname AND IP you will actually type into a browser. Modern
# browsers ignore the Common Name entirely and match only against SANs, so
# a cert without the IP in its SAN list fails on https://192.168.1.20 even
# though the CN says otherwise -- the classic self-signed dead end.
set -euo pipefail

if [ "$#" -lt 1 ]; then
	echo "usage: $0 <primary-hostname> [extra-hostname-or-ip ...] [output-dir]" >&2
	exit 64
fi

PRIMARY="$1"
shift

# Last argument is the output directory if it looks like a path.
OUT_DIR="./certs"
ARGS=("$@")
if [ "${#ARGS[@]}" -gt 0 ]; then
	LAST="${ARGS[${#ARGS[@]}-1]}"
	case "$LAST" in
		*/*) OUT_DIR="$LAST"; unset 'ARGS[${#ARGS[@]}-1]' ;;
	esac
fi

DAYS="${SBD_CERT_DAYS:-825}"
KEY="$OUT_DIR/$PRIMARY.key"
CRT="$OUT_DIR/$PRIMARY.crt"

mkdir -p "$OUT_DIR"

# Build the SAN list. Anything that parses as a dotted quad or contains a
# colon goes in as IP:, everything else as DNS:.
SAN="DNS:$PRIMARY"
for entry in ${ARGS[@]+"${ARGS[@]}"}; do
	if [[ "$entry" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ || "$entry" == *:* ]]; then
		SAN="$SAN,IP:$entry"
	else
		SAN="$SAN,DNS:$entry"
	fi
done

echo "Generating a ${DAYS}-day self-signed certificate"
echo "  subject : CN=$PRIMARY"
echo "  SANs    : $SAN"
echo "  output  : $CRT"

openssl req -x509 -newkey rsa:2048 -nodes \
	-keyout "$KEY" \
	-out "$CRT" \
	-days "$DAYS" \
	-subj "/CN=$PRIMARY" \
	-addext "subjectAltName=$SAN" \
	-addext "basicConstraints=critical,CA:FALSE" \
	-addext "keyUsage=critical,digitalSignature,keyEncipherment" \
	-addext "extendedKeyUsage=serverAuth"

chmod 600 "$KEY"

echo
echo "Done. Point nginx at these:"
echo "    ssl_certificate     $(cd "$OUT_DIR" && pwd)/$PRIMARY.crt;"
echo "    ssl_certificate_key $(cd "$OUT_DIR" && pwd)/$PRIMARY.key;"
echo
echo "Inspect it with:"
echo "    openssl x509 -in $CRT -noout -text | grep -A1 'Subject Alternative Name'"
echo
echo "Every browser will warn on first visit -- that is inherent to"
echo "self-signed and is not a sign anything went wrong. ${DAYS} days is"
echo "the ceiling Apple/Chrome accept for a server cert; longer is silently"
echo "rejected on macOS and iOS, so do not raise SBD_CERT_DAYS past 825."
echo
echo "The PIN still travels over a connection whose identity nothing has"
echo "verified for you. This encrypts the wire; it does not authenticate"
echo "the server. Prefer Tailscale Serve or a real cert where you can."
