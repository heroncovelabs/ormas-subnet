# MIT. public_subnet/protected/worker/ is the source, not a deployment mirror.
# Build from this directory:
#   docker build -t reference-worker .
# Run the local fixtures (operator supplies the artifact byte cap):
#   python3 conformance/conformance.py reference-worker --max-patch-bytes "$PATCH_CAP_BYTES"
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
COPY reference_worker.py /opt/worker/reference_worker.py
USER 65534:65534
ENV ORMAS_WORK=/work
ENTRYPOINT ["python3","-I","/opt/worker/reference_worker.py"]
