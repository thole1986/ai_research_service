FROM postgres:latest
COPY scripts/start-postgres.sh /usr/local/bin/start-service.sh
RUN chmod +x /usr/local/bin/start-service.sh
ENTRYPOINT ["/bin/sh", "/usr/local/bin/start-service.sh"]
