# Sandbox image for agent `run` commands. Dependencies are installed at build
# time (with network); containers are then started with --network none.
FROM python:3.12-slim
RUN pip install --no-cache-dir pytest==8.3.3 pytest-cov==5.0.0 mypy==1.13.0
