import os
import subprocess
import sys
import time
import json
import gzip
import calendar
import pandas as pd
import schedule
from datetime import datetime, timedelta
from sqlalchemy import create_engine

DB_USER = os.getenv("POSTGRES_USER")
DB_PASS = os.getenv("POSTGRES_PASSWORD")
DB_NAME = os.getenv("POSTGRES_DB")
DB_HOST = os.getenv("POSTGRES_HOST", "db_fozesc")
DB_PORT = os.getenv("POSTGRES_PORT", "5432")

if not DB_USER or not DB_PASS or not DB_NAME:
    raise ValueError("ERRO: Credenciais do banco não encontradas no ambiente. Backup abortado.")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIR_DIARIO = os.path.join(BASE_DIR, "backups/diario")
DIR_SEMANAL = os.path.join(BASE_DIR, "backups/semanal")
DIR_MENSAL = os.path.join(BASE_DIR, "backups/mensal")

ARQUIVO_STATUS = os.path.join(BASE_DIR, "backups", "STATUS.json")
MIN_TABELAS = 5

def garantir_pastas():
    for pasta in [DIR_DIARIO, DIR_SEMANAL, DIR_MENSAL]:
        if not os.path.exists(pasta):
            os.makedirs(pasta)

def conferir_dump(caminho):
    """Abre o .gz e confirma que o pg_dump chegou ao fim. Sem isso, um dump
    truncado (disco cheio, container morto no meio) passa como valido."""
    tabelas = 0
    completo = False
    with gzip.open(caminho, "rt", errors="replace") as f:
        for linha in f:
            if linha.startswith("COPY "):
                tabelas += 1
            elif "PostgreSQL database dump complete" in linha:
                completo = True
    if not completo:
        return False, f"dump truncado: pg_dump nao chegou ao fim ({tabelas} tabelas)"
    if tabelas < MIN_TABELAS:
        return False, f"dump com apenas {tabelas} tabelas (esperado >= {MIN_TABELAS})"
    return True, f"{tabelas} tabelas, dump completo"

def gravar_status(ok, mensagem, arquivo=None):
    """Deixa o resultado do ultimo backup em disco. Foi a falta disso que
    escondeu 4 meses de .sql.gz vazios (mai-set/2026)."""
    garantir_pastas()
    with open(ARQUIVO_STATUS, "w") as f:
        json.dump({
            "ok": ok,
            "quando": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mensagem": mensagem,
            "arquivo": os.path.basename(arquivo) if arquivo else None,
        }, f, ensure_ascii=False, indent=2)

def exportar_para_xlsx(caminho_pasta, prefixo):
    """ Conecta no banco e gera uma planilha com as tabelas principais """
    try:
        engine = create_engine(f'postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}')
        xlsx_file = os.path.join(caminho_pasta, f"{prefixo}.xlsx")
        
        with pd.ExcelWriter(xlsx_file, engine='xlsxwriter') as writer:
            tabelas = ['transactions', 'checks', 'clients'] 
            for tabela in tabelas:
                try:
                    df = pd.read_sql_table(tabela, engine)
                    # CORREÇÃO 1: O comando correto do Pandas é to_excel, não to_sheet
                    df.to_excel(writer, sheet_name=tabela[:31], index=False)
                except Exception as e:
                    print(f"Aviso ao ler tabela {tabela}: {e}")
                    continue
        print(f"📊 Planilha gerada com sucesso: {os.path.basename(xlsx_file)}")
    except Exception as e:
        print(f"❌ Erro ao gerar XLSX: {e}")

def limpar_antigos(pasta, dias_limite):
    """ Apaga arquivos físicos modificados há mais de X dias """
    agora = datetime.now()
    limite_tempo = agora - timedelta(days=dias_limite)
    
    if not os.path.exists(pasta): return

    for filename in os.listdir(pasta):
        if filename.endswith(".gz") or filename.endswith(".xlsx"):
            filepath = os.path.join(pasta, filename)
            tempo_modificacao = datetime.fromtimestamp(os.path.getmtime(filepath))
            
            if tempo_modificacao < limite_tempo:
                os.remove(filepath)
                print(f"🧹 Limpeza executada: {filename} removido (mais de {dias_limite} dias de idade).")

def executar_backup(tipo="DIARIO"):
    garantir_pastas()
    agora = datetime.now()
    timestamp = agora.strftime("%Y-%m-%d_%H-%M-%S")
    
    if tipo == "MENSAL":
        pasta_destino = DIR_MENSAL
        prefixo = f"FOZESC_MENSAL_{timestamp}"
    elif tipo == "SEMANAL":
        pasta_destino = DIR_SEMANAL
        prefixo = f"FOZESC_SEMANAL_{timestamp}"
    else:
        pasta_destino = DIR_DIARIO
        prefixo = f"FOZESC_DIARIO_{timestamp}"
    
    sql_file = os.path.join(pasta_destino, f"{prefixo}.sql.gz")

    print(f"🐘 Iniciando extração de dados Postgres ({tipo})...")

    # CORREÇÃO 2: Removido o 'docker exec'. Agora usamos o cliente de rede interno apontando para o DB_HOST
    # CORREÇÃO 4: 'set -o pipefail' é obrigatório aqui. Sem ele, o código de saída
    # de "pg_dump | gzip" é o do GZIP (sucesso) mesmo quando o pg_dump falha ou nem
    # existe na imagem — o backup gravava um .sql.gz vazio de 20 bytes e mesmo assim
    # imprimia "salvo com sucesso". Agora a falha estoura de verdade.
    cmd = f"set -o pipefail; pg_dump -h {DB_HOST} -p {DB_PORT} -U {DB_USER} {DB_NAME} | gzip > {sql_file}"

    try:
        # CORREÇÃO 3: O pg_dump exige a senha injetada no ambiente para não travar aguardando digitação
        ambiente = os.environ.copy()
        ambiente["PGPASSWORD"] = DB_PASS

        subprocess.run(cmd, shell=True, check=True, env=ambiente,
                       executable="/bin/bash")  # pipefail é do bash, não do sh

        # CORREÇÃO 5: confere o resultado em vez de confiar no código de saída.
        tamanho = os.path.getsize(sql_file) if os.path.exists(sql_file) else 0
        if tamanho < 1024:
            raise RuntimeError(
                f"backup gerado com apenas {tamanho} bytes — arquivo vazio/inválido. "
                f"Verifique se o 'pg_dump' existe na imagem (pacote postgresql-client)."
            )
        # CORREÇÃO 6: tamanho não basta. Um dump truncado no meio tem tamanho
        # de sobra e continua inútil — só o rodapé do pg_dump prova que terminou.
        ok, detalhe = conferir_dump(sql_file)
        if not ok:
            raise RuntimeError(detalhe)

        print(f"✅ Arquivo SQL salvo em: {os.path.basename(sql_file)} ({tamanho/1024:.0f} KB, {detalhe})")

        exportar_para_xlsx(pasta_destino, prefixo)

        if tipo == "SEMANAL":
            print("Verificando se há backups DIÁRIOS com mais de 7 dias...")
            limpar_antigos(DIR_DIARIO, 7)
        elif tipo == "MENSAL":
            print("Verificando se há backups SEMANAIS com mais de 30 dias...")
            limpar_antigos(DIR_SEMANAL, 30)

        gravar_status(True, f"{tipo}: {detalhe}", sql_file)

    except Exception as e:
        print(f"❌ Falha crítica durante o backup: {e}")
        gravar_status(False, f"{tipo} FALHOU: {e}", sql_file)

def testar_restauracao(caminho=None):
    """Restaura o backup mais recente num banco descartável e confere os dados.
    Backup que nunca foi restaurado não é backup comprovado, é esperança."""
    if caminho is None:
        candidatos = []
        for pasta in [DIR_DIARIO, DIR_SEMANAL, DIR_MENSAL]:
            if os.path.isdir(pasta):
                candidatos += [os.path.join(pasta, f) for f in os.listdir(pasta)
                               if f.endswith(".sql.gz")]
        if not candidatos:
            print("❌ Nenhum backup .sql.gz encontrado para testar.")
            return False
        caminho = max(candidatos, key=os.path.getmtime)

    print(f"🧪 Testando restauração de: {os.path.basename(caminho)}")

    ok, detalhe = conferir_dump(caminho)
    if not ok:
        print(f"❌ Arquivo reprovado antes mesmo de restaurar: {detalhe}")
        return False
    print(f"   arquivo íntegro ({detalhe})")

    temp_db = "fozesc_teste_restauracao"
    if temp_db == DB_NAME:
        print("❌ Abortado: o banco de teste tem o mesmo nome do banco real.")
        return False

    ambiente = os.environ.copy()
    ambiente["PGPASSWORD"] = DB_PASS
    base = f"psql -h {DB_HOST} -p {DB_PORT} -U {DB_USER}"

    def rodar(cmd):
        return subprocess.run(cmd, shell=True, env=ambiente, executable="/bin/bash",
                              capture_output=True, text=True)

    rodar(f'{base} -d postgres -c "DROP DATABASE IF EXISTS {temp_db}"')
    r = rodar(f'{base} -d postgres -c "CREATE DATABASE {temp_db}"')
    if r.returncode != 0:
        print(f"❌ Não consegui criar o banco de teste: {r.stderr.strip()}")
        return False

    try:
        r = rodar(f"set -o pipefail; gunzip -c '{caminho}' | {base} -d {temp_db} -v ON_ERROR_STOP=1 -q")
        if r.returncode != 0:
            print(f"❌ A restauração falhou: {r.stderr.strip()[:400]}")
            return False

        consulta = ("select (select count(*) from clients) || ' clientes, ' || "
                    "(select count(*) from checks) || ' cheques, ' || "
                    "(select count(*) from operations) || ' borderos, ' || "
                    "(select count(*) from transactions) || ' transacoes'")
        r = rodar(f'{base} -d {temp_db} -tAc "{consulta}"')
        print(f"✅ Restauração comprovada — {r.stdout.strip()}")
        return True
    finally:
        rodar(f'{base} -d postgres -c "DROP DATABASE IF EXISTS {temp_db}"')

def rotina_12h():
    print("⏰[12:00] Disparando backup diário da tarde...")
    executar_backup(tipo="DIARIO")

def rotina_15h():
    print("⏰[15:00] Disparando rotina de fechamento...")
    agora = datetime.now()
    ultimo_dia_do_mes = calendar.monthrange(agora.year, agora.month)[1]
    
    if agora.day == ultimo_dia_do_mes:
        executar_backup(tipo="MENSAL")
    elif agora.weekday() == 4: # Sexta-feira
        executar_backup(tipo="SEMANAL")
    else:
        executar_backup(tipo="DIARIO")

if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else None

    if comando == "--testar":
        sys.exit(0 if testar_restauracao() else 1)

    if comando == "--agora":
        executar_backup(tipo="DIARIO")
        sys.exit(0)

    print("🕒 Módulo de Backups FOZESC iniciado com sucesso.")
    print("🕒 Aguardando os horários agendados (12h e 15h)...")

    schedule.every().day.at("12:00").do(rotina_12h)
    schedule.every().day.at("15:00").do(rotina_15h)

    while True:
        schedule.run_pending()
        time.sleep(60)