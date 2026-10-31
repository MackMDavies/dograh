ARG BASE_IMAGE
FROM ${BASE_IMAGE}
COPY payload/api/routes/organization.py /app/api/routes/organization.py
COPY payload/api/services/subscription_managed_numbers.py /app/api/services/subscription_managed_numbers.py
COPY payload/api/tests/test_subscription_imported_numbers.py /app/api/tests/test_subscription_imported_numbers.py
LABEL io.sysevo.number-policy-revision="53cc51efdcd8aea47b3af49c33dd8a93edb94b90"
