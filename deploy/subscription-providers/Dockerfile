# Supply each service's own inspected running image. API and worker differ.
ARG BASE_IMAGE=scratch
FROM ${BASE_IMAGE}
ARG SOURCE_REVISION
LABEL io.sysevo.subscription-providers.revision=${SOURCE_REVISION}
USER root
COPY api/routes/organization.py /app/api/routes/organization.py
COPY api/schemas/telephony_config.py /app/api/schemas/telephony_config.py
COPY api/services/telephony/providers/__init__.py /app/api/services/telephony/providers/__init__.py
COPY api/services/telephony/providers/signalwire /app/api/services/telephony/providers/signalwire
COPY api/services/telephony/providers/sip /app/api/services/telephony/providers/sip
COPY api/services/telephony/providers/ari/provider.py api/services/telephony/providers/ari/strategies.py /app/api/services/telephony/providers/ari/
COPY api/services/telephony/asterisk_media.py api/services/telephony/asterisk_strategies.py api/services/telephony/ari_manager.py api/services/telephony/sip_gateway_config.py api/services/telephony/factory.py /app/api/services/telephony/
RUN python -m compileall -q /app/api/routes/organization.py /app/api/schemas/telephony_config.py /app/api/services/telephony
USER dograh
