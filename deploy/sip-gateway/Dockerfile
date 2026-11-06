# Build without touching the live API/worker. Supply the inspected immutable
# running API image, tagged locally for BuildKit, as BASE_IMAGE.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE} AS asterisk-builder
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config libedit-dev libjansson-dev libsqlite3-dev \
    libssl-dev libxml2-dev uuid-dev libcurl4-openssl-dev libncurses-dev \
    curl bzip2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /tmp/asterisk-source
RUN curl --fail --show-error --location --proto '=https' --tlsv1.2 \
    https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-22.12.0.tar.gz -o asterisk.tar.gz \
    && echo 'f8b2ba98aac6c4ff3376bc0a2868bea6ed577ca684efb7ec739ae6e8ce6e35e2  asterisk.tar.gz' | sha256sum -c - \
    && tar -xzf asterisk.tar.gz --strip-components=1 \
    && ./configure --with-pjproject-bundled --without-dahdi --without-pri \
    && make menuselect.makeopts \
    && menuselect/menuselect --disable BUILD_NATIVE --disable-category MENUSELECT_CORE_SOUNDS --disable-category MENUSELECT_MOH --enable chan_websocket --enable res_websocket_client menuselect.makeopts \
    && make -j1 \
    && make install DESTDIR=/out

FROM ${BASE_IMAGE}
ARG SOURCE_REVISION
LABEL io.sysevo.subscription-providers.revision=${SOURCE_REVISION}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    libedit2 libjansson4 libsqlite3-0 libssl3t64 libxml2 libuuid1 libcurl4t64 libncurses6 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=asterisk-builder /out/usr/sbin/ /usr/sbin/
COPY --from=asterisk-builder /out/usr/lib/ /usr/lib/
COPY --from=asterisk-builder /out/var/lib/asterisk/ /var/lib/asterisk/
COPY api/services/telephony/providers/sip /app/api/services/telephony/providers/sip
COPY api/services/telephony/providers/signalwire /app/api/services/telephony/providers/signalwire
COPY api/services/telephony/providers/__init__.py /app/api/services/telephony/providers/__init__.py
COPY api/services/telephony/asterisk_media.py api/services/telephony/asterisk_strategies.py api/services/telephony/ari_manager.py api/services/telephony/call_concurrency.py api/services/telephony/sip_gateway.py api/services/telephony/sip_gateway_config.py api/services/telephony/factory.py /app/api/services/telephony/
COPY api/schemas/telephony_config.py /app/api/schemas/telephony_config.py
WORKDIR /app
CMD ["python", "-m", "uvicorn", "api.services.telephony.sip_gateway:app", "--host", "0.0.0.0", "--port", "8089", "--no-access-log"]
