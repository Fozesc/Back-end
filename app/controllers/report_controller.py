from flask import Blueprint, request, send_file, jsonify
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

@bp.route('/resumo', methods=['GET'])
@jwt_required()
def resumo():
    """Numeros do relatorio gerencial que a tela imprime. Antes a tela nao chamava
    nada: imprimia valores fixos escritos no Vue."""
    try:
        dados = service.gerar_resumo(
            request.args.get('tipo'),
            request.args.get('inicio'),
            request.args.get('fim'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(dados)
