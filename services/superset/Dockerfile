FROM apache/superset:6.1.0 AS builder

USER root
RUN mkdir -p /tmp/superset-extra && \
    uv pip install --python /app/.venv/bin/python --target /tmp/superset-extra \
      fastmcp psycopg2-binary gevent

FROM apache/superset:6.1.0 AS runtime

USER root
COPY --from=builder /tmp/superset-extra/ /tmp/superset-extra/
ENV PYTHONPATH=/tmp/superset-extra
USER superset
