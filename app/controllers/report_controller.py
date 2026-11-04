from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from app.services.report_service import ReportService

bp = Blueprint('reports', __name__, url_prefix='/api/reports')
service = ReportService()

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
            request.args.get('empresa'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(dados)


@bp.route('/caixa', methods=['GET'])
@jwt_required()
def caixa():
    """Relatorio do caixa: geral ou de uma conta, com o extrato paginado."""
    try:
        return jsonify(service.relatorio_caixa(
            request.args.get('inicio'), request.args.get('fim'),
            request.args.get('conta', 'todas'), request.args.get('tipo', 'todos'),
            request.args.get('page', 1, type=int), request.args.get('per_page', 100, type=int)))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@bp.route('/extrato', methods=['GET'])
@jwt_required()
def extrato():
    """Extrato geral do periodo (PDF para os socios): caixa, borderos, recebimentos, vales..."""
    try:
        return jsonify(service.extrato_periodo(request.args.get('inicio'), request.args.get('fim')))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
