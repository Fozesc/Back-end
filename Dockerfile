
FROM python:3.12-slim-bookworm


RUN apt-get update && apt-get install -y \
    libpq-dev \
    postgresql-client \
    gcc \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*
# postgresql-client traz o binario pg_dump. Sem ele o gerar_backup.py rodava
# "pg_dump | gzip", o pg_dump nao existia, mas o codigo de saida do pipe e o do
# gzip (sucesso) -> o backup gravava um .sql.gz VAZIO de 20 bytes e imprimia
# "backup salvo com sucesso". libpq-dev sozinho NAO instala o pg_dump.

# "hoje" do sistema (recebimento, comissao, vencido) e' o de Brasilia: em UTC, das 21h
# a meia-noite o servidor ja estava no dia seguinte e o caixa gravava a data de amanha.
ENV TZ=America/Sao_Paulo

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/backups

EXPOSE 5001

# Servidor de producao (gunicorn), nao o de desenvolvimento do Flask (`python run.py`, que
# so serve para rodar na maquina). 8 threads atendem o escritorio todo ao mesmo tempo.
# ponytail: 1 processo so - o limite de pedidos (Flask-Limiter) guarda a contagem na memoria
# e as rotinas do boot (create_app) rodam uma vez. Mais processos pede Redis no limiter.
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "1", "--threads", "8", "--timeout", "120", "--access-logfile", "-", "run:app"]