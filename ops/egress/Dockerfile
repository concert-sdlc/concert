# The egress proxy for agent containers (concert proxy): HTTPS CONNECT to named hosts only.
# It sits on two networks: concert-egress (internal, where agents live) and the default bridge
# (the way out), so an agent can reach nothing except through it.
FROM python:3.12-slim
COPY *.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl \
 && useradd --uid 10002 --no-create-home --shell /usr/sbin/nologin egress
USER 10002
EXPOSE 3128
ENTRYPOINT ["concert", "proxy", "--listen", "0.0.0.0:3128"]
CMD ["--allow", "api.anthropic.com"]
