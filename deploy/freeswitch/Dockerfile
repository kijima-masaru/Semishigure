# Semishigure development PBX: FreeSWITCH 1.10.12 built from source with a
# minimal module set (no SignalWire package token needed).
# Build:  docker compose build     (or: docker build -t semishigure-fs deploy/freeswitch)
# Run:    docker compose up -d     (host networking; see README.md)

FROM debian:bookworm-slim AS build
ARG FS_VERSION=v1.10.12
ARG SOFIA_VERSION=v1.13.17
ARG SPANDSP_COMMIT=8f1e1646bdec99eac5fd2cd92c35563f736b9b89
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates build-essential autoconf automake libtool libtool-bin pkg-config cmake yasm nasm \
    libpcre3-dev libedit-dev libsqlite3-dev libcurl4-openssl-dev libspeexdsp-dev libspeex-dev libldns-dev \
    libsndfile1-dev uuid-dev libtiff-dev libssl-dev zlib1g-dev libjpeg-dev libopus-dev libncurses5-dev \
    libexpat1-dev libgnutls28-dev libsrtp2-dev libpq-dev unixodbc-dev libdb-dev libgdbm-dev \
 && rm -rf /var/lib/apt/lists/*
# Optional: drop a corporate proxy CA (PEM, *.crt) into deploy/freeswitch/ca/ so
# git/apt inside the build trust it. The directory is empty by default.
COPY ca/ /usr/local/share/ca-certificates/semishigure/
RUN update-ca-certificates
WORKDIR /usr/src
RUN git clone --depth 1 --branch ${SOFIA_VERSION} https://github.com/freeswitch/sofia-sip.git \
 && cd sofia-sip && ./bootstrap.sh -i && ./configure --with-pic --with-glib=no --without-doxygen --disable-stun --prefix=/usr/local \
 && make -j"$(nproc)" && make install && ldconfig
RUN git clone https://github.com/freeswitch/spandsp.git && cd spandsp && git checkout ${SPANDSP_COMMIT} \
 && ./bootstrap.sh -i && ./configure --with-pic --prefix=/usr/local && make -j"$(nproc)" && make install && ldconfig
COPY modules.conf /tmp/modules.conf
RUN git clone --depth 1 --branch ${FS_VERSION} https://github.com/signalwire/freeswitch.git \
 && cd freeswitch && cp /tmp/modules.conf modules.conf && ./bootstrap.sh -j \
 && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure --prefix=/usr/local/freeswitch \
      --disable-libvpx --disable-libyuv --without-python --without-python3 --without-erlang --without-java \
 && make -j"$(nproc)" && make install

FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpcre3 libedit2 libsqlite3-0 libcurl4 libspeexdsp1 libspeex1 libldns3 libsndfile1 libuuid1 libtiff6 \
    libssl3 zlib1g libjpeg62-turbo libopus0 libncurses6 libexpat1 libgnutls30 libsrtp2-1 libpq5 libodbc2 libdb5.3 libgdbm6 \
    procps openssh-server \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /usr/local/freeswitch /usr/local/freeswitch
COPY --from=build /usr/local/lib/libsofia-sip-ua.so* /usr/local/lib/libspandsp.so* /usr/local/lib/
RUN ldconfig && ln -s /usr/local/freeswitch/bin/fs_cli /usr/local/bin/fs_cli \
 && mkdir -p /etc/semishigure-fs /var/log/freeswitch /var/lib/freeswitch/db /var/run/freeswitch
COPY conf /etc/semishigure-fs
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 5060/udp 8021/tcp
ENTRYPOINT ["/entrypoint.sh"]
