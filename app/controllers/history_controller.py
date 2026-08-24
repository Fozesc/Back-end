from flask import Blueprint, jsonify
from app.services.history_service import HistoryService
from flask_jwt_extended import jwt_required

bp = Blueprint('history', __name__)
service = HistoryService()


@bp.route('/months', methods=['GET'])
@jwt_required()
def months():
    """Lista todos os meses com atividade + resumo (para o seletor)."""
    return jsonify(service.get_available_months())


@bp.route('/month/<int:year>/<int:month>', methods=['GET'])
@jwt_required()
def month_detail(year, month):
    """Detalhe de um mês: saldo por banco, crescimento/perda e lançamentos."""
    if month < 1 or month > 12:
        return jsonify({'error': 'Mês inválido'}), 400
    return jsonify(service.compute_month_detail(year, month))
