ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SOURCE_REVISION
LABEL io.sysevo.agent-test-attestation.revision=${SOURCE_REVISION}
COPY api/services/workflow/test_sandbox_binding.py /app/api/services/workflow/test_sandbox_binding.py
COPY deploy/agent-test-attestation/patch_runtime.py /tmp/agent-test-attestation-patch.py
RUN python3 /tmp/agent-test-attestation-patch.py --root /app \
    && python3 -m compileall -q /app/api/services/workflow/test_sandbox_binding.py /app/api/routes/workflow_text_chat.py /app/api/services/workflow/text_chat_runner.py /app/api/routes/workflow.py
