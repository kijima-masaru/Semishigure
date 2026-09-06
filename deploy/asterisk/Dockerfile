# Semishigure development PBX: Asterisk 20 from the Ubuntu 24.04 packages.
# Build:  docker compose build   (in deploy/asterisk)
# Run:    docker compose up -d   (host networking; see README.md)
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    asterisk asterisk-modules asterisk-config procps openssh-server python3 \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /etc/semishigure-ast /var/log/asterisk /run/sshd
COPY conf /etc/semishigure-ast/templates
COPY render-conf.sh entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/render-conf.sh /usr/local/bin/entrypoint.sh
EXPOSE 5060/udp 5038/tcp
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
