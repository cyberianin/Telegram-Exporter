FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[server,bot]'
VOLUME ["/data"]
ENV TGX_DATA_DIR=/data
ENTRYPOINT ["tgx"]
CMD ["serve", "--host", "0.0.0.0"]
