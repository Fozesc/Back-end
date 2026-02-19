from flask import Blueprint, request, send_file
from flask_jwt_extended import jwt_required
from app.services.report_service import ReportService
import os

bp = Blueprint('reports', __name__, url_prefix='/api/reports')
service = ReportService()

@bp.route('/export', methods=['POST'])
@jwt_required()
def export_custom():
    data = request.json

    start_date = data.get('start_date')
    end_date = data.get('end_date')
    tipo = data.get('type') # 'geral', 'cheques', 'fluxo', 'clientes'

    filepath = service.gerar_relatorio_customizado(tipo, start_date, end_date)
    
    if not filepath:
        return {'error': 'Nenhum dado encontrado para os filtros selecionados'}, 404

    return send_file(
        filepath, 
        as_attachment=True, 
        download_name=os.path.basename(filepath)
    )