FROM redis:7.4-alpine
COPY scripts/start-redis.sh /usr/local/bin/start-service.sh
RUN chmod +x /usr/local/bin/start-service.sh
ENTRYPOINT ["/bin/sh", "/usr/local/bin/start-service.sh"]
