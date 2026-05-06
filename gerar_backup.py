import os
import subprocess
import time
import calendar
import pandas as pd
import schedule
from datetime import datetime, timedelta
from sqlalchemy import create_engine

CONTAINER_NAME = "db_fozesc"

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

def garantir_pastas():
    for pasta in [DIR_DIARIO, DIR_SEMANAL, DIR_MENSAL]:
        if not os.path.exists(pasta):
            os.makedirs(pasta)

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
    cmd = f"pg_dump -h {DB_HOST} -p {DB_PORT} -U {DB_USER} {DB_NAME} | gzip > {sql_file}"
    
    try:
        # CORREÇÃO 3: O pg_dump exige a senha injetada no ambiente para não travar aguardando digitação
        ambiente = os.environ.copy()
        ambiente["PGPASSWORD"] = DB_PASS

        subprocess.run(cmd, shell=True, check=True, env=ambiente)
        print(f"✅ Arquivo SQL salvo em: {os.path.basename(sql_file)}")
        
        exportar_para_xlsx(pasta_destino, prefixo)
        
        if tipo == "SEMANAL":
            print("Verificando se há backups DIÁRIOS com mais de 7 dias...")
            limpar_antigos(DIR_DIARIO, 7)
        elif tipo == "MENSAL":
            print("Verificando se há backups SEMANAIS com mais de 30 dias...")
            limpar_antigos(DIR_SEMANAL, 30)
            
    except Exception as e:
        print(f"❌ Falha crítica durante o backup: {e}")

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
    print("🕒 Módulo de Backups FOZESC iniciado com sucesso.")
    print("🕒 Aguardando os horários agendados (12h e 15h)...")
    
    schedule.every().day.at("12:00").do(rotina_12h)
    schedule.every().day.at("15:00").do(rotina_15h)
    
    while True:
        schedule.run_pending()
        time.sleep(60)