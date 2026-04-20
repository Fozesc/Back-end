import os
import shutil
import hashlib
import pandas as pd
from datetime import datetime
from flask import current_app
from app import db
from sqlalchemy import inspect

class BackupService:
    def __init__(self):
        self.backup_dir = os.path.join(os.getcwd(), 'backups')
        self.last_hash_file = os.path.join(self.backup_dir, '.last_backup_hash')
        if not os.path.exists(self.backup_dir):
            os.makedirs(self.backup_dir)

    def _get_db_hash(self, filepath):
        """Gera um hash MD5 para comparar se o arquivo mudou."""
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def generate_smart_backup(self):
        try:
            # Localiza o banco SQLite
            db_path = current_app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
            if not os.path.exists(db_path):
                return {"status": "error", "message": "Banco de dados não encontrado."}

            # Verifica se houve alteração desde o último backup
            current_hash = self._get_db_hash(db_path)
            if os.path.exists(self.last_hash_file):
                with open(self.last_hash_file, 'r') as f:
                    last_hash = f.read().strip()
                if current_hash == last_hash:
                    return {"status": "skipped", "message": "Nenhuma alteração detectada desde o último backup."}

            # Se mudou, cria a pasta de backup
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            path_completo = os.path.join(self.backup_dir, f"backup_{timestamp}")
            os.makedirs(path_completo)

            # 1. Copia o DB Bruto
            shutil.copy2(db_path, os.path.join(path_completo, 'database.db'))

            # 2. Gera o Excel (Leitura fácil)
            excel_path = os.path.join(path_completo, 'dados_exportados.xlsx')
            inspector = inspect(db.engine)
            with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
                for table_name in inspector.get_table_names():
                    df = pd.read_sql_table(table_name, db.engine)
                    df.to_excel(writer, sheet_name=table_name, index=False)

            # Atualiza o hash para o próximo controle
            with open(self.last_hash_file, 'w') as f:
                f.write(current_hash)

            return {
                "status": "success",
                "message": f"Backup realizado: {timestamp}",
                "folder": path_completo
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}