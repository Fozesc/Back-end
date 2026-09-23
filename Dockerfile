
FROM python:3.12-slim


RUN apt-get update && apt-get install -y \
    libpq-dev \
    postgresql-client \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*
# postgresql-client traz o binario pg_dump. Sem ele o gerar_backup.py rodava
# "pg_dump | gzip", o pg_dump nao existia, mas o codigo de saida do pipe e o do
# gzip (sucesso) -> o backup gravava um .sql.gz VAZIO de 20 bytes e imprimia
# "backup salvo com sucesso". libpq-dev sozinho NAO instala o pg_dump.

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/backups

EXPOSE 5000

CMD ["python", "run.py"]