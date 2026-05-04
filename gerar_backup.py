import os
import subprocess
import pandas as pd
from datetime import datetime, timedelta
from sqlalchemy import create_engine


CONTAINER_NAME = "db_fozesc"
DB_USER = "postgres"
DB_NAME = "fozesc"
DB_PASS = "postgres" # Ajuste se mudou a senha 
DB_HOST = "localhost"
DB_PORT = "5432"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DIR_DIARIO = os.path.join(BASE_DIR, "backups/diario_modificacoes")
DIR_SEMANAL = os.path.join(BASE_DIR, "backups/semanal_arquivamento")


MAX_DIARIOS = 30    # Guarda os últimos 30 backups de modificação
MAX_SEMANAIS = 8    # Guarda os últimos 2 meses (8 semanas)

def garantir_pastas():
    for pasta in [DIR_DIARIO, DIR_SEMANAL]:
        if not os.path.exists(pasta):
            os.makedirs(pasta)

def exportar_para_xlsx(caminho_pasta, prefixo):
    """ Conecta no banco e gera uma planilha com todas as tabelas """
    try:
        engine = create_engine(f'postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}')
        xlsx_file = os.path.join(caminho_pasta, f"{prefixo}.xlsx")
        
        with pd.ExcelWriter(xlsx_file, engine='xlsxwriter') as writer:
            # Exporta as tabelas principais
            tabelas = ['transactions', 'checks', 'clients'] # Ajuste se os nomes mudarem
            for tabela in tabelas:
                try:
                    df = pd.read_sql_table(tabela, engine)
                    df.to_sheet(writer, sheet_name=tabela[:31], index=False)
                except:
                    continue
        print(f"📊 Planilha gerada: {os.path.basename(xlsx_file)}")
    except Exception as e:
        print(f"❌ Erro ao gerar XLSX: {e}")

def limpar_antigos(pasta, limite):
    files = [os.path.join(pasta, f) for f in os.listdir(pasta) if f.endswith(".gz") or f.endswith(".xlsx")]

    files.sort(key=os.path.getmtime)
    

    while len(files) > (limite * 2):
        removido = files.pop(0)
        os.remove(removido)
        print(f"🧹 Limpeza: {os.path.basename(removido)} removido.")

def executar_backup():
    garantir_pastas()
    agora = datetime.now()
    timestamp = agora.strftime("%Y-%m-%d_%H-%M-%S")
    

    e_semanal = agora.weekday() == 6 or not os.listdir(DIR_SEMANAL)
    pasta_destino = DIR_SEMANAL if e_semanal else DIR_DIARIO
    prefixo = f"backup_SEMANAL_{timestamp}" if e_semanal else f"backup_MOD_{timestamp}"
    
    sql_file = os.path.join(pasta_destino, f"{prefixo}.sql.gz")

    print(f"🐘 Dump Postgres em andamento...")
    cmd = f"docker exec {CONTAINER_NAME} pg_dump -U {DB_USER} {DB_NAME} | gzip > {sql_file}"
    
    try:
        subprocess.run(cmd, shell=True, check=True)
        print(f"✅ SQL salvo em: {os.path.basename(sql_file)}")
        

        exportar_para_xlsx(pasta_destino, prefixo)
        
  
        limpar_antigos(DIR_DIARIO, MAX_DIARIOS)
        limpar_antigos(DIR_SEMANAL, MAX_SEMANAIS)
        
    except Exception as e:
        print(f"❌ Falha crítica no backup: {e}")

if __name__ == "__main__":
    executar_backup()