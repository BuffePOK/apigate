# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md ./
COPY apigate ./apigate
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    APIGATE_DATA_DIR=/data APIGATE_HOST=0.0.0.0 APIGATE_PORT=18080
COPY --from=build /install /usr/local
COPY presets /opt/presets
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin apigate \
 && mkdir /data && chown apigate /data
USER apigate
VOLUME /data
EXPOSE 18080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:18080/healthz',timeout=4).status==200 else 1)"
ENTRYPOINT ["apigate"]
CMD ["serve"]
