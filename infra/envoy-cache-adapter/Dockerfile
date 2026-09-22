FROM envoyproxy/envoy:v1.38.0@sha256:8146b97ee61a42cd216514709e4e3198af75f014974e3d9f310aef9c901fcbdf

USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY entrypoint.py shipper.py envoy.yaml.template /app/
COPY turnstile_core /app/turnstile_core/
RUN mkdir -p /tmp/turnstile-envoy \
    && chown -R envoy:envoy /app /tmp/turnstile-envoy

USER envoy
EXPOSE 8080
ENTRYPOINT ["python3", "/app/entrypoint.py"]