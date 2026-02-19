from app import create_app
from app.services.report_service import ReportService
import os

app = create_app()

def run_backup():
    with app.app_context():
        print("="*50)
        print("🚀 INICIANDO BACKUP DE CHEQUES (BACKEND)")
        print("="*50)
        
        service = ReportService()
        
        # Gera na pasta 'backups' dentro do backend
        filepath, info = service.gerar_relatorio_geral_cheques(output_folder="backups")
        
        if filepath:
            print(f"\n📂 Arquivo salvo em: {os.path.abspath(filepath)}")
            print(f"🔢 Total de registros: {info}")
        else:
            print(f"⚠️ Erro/Vazio: {info}")
            
        print("="*50)

if __name__ == "__main__":
    run_backup()