from flask import Blueprint, jsonify, request
from app.services.transaction_service import TransactionService
from flask_jwt_extended import jwt_required
from app.controllers import so_admin

from app.controllers import limite_senha

bp = Blueprint('transactions', __name__, url_prefix='/api/transactions')
service = TransactionService()

@bp.route('', methods=['GET'])
@jwt_required()
def index():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '', type=str)
    date = request.args.get('date', '', type=str) or None
    tipo = request.args.get('type', 'todos', type=str)
    conta = request.args.get('conta', '', type=str) or None

    data = service.get_paginated(page, per_page, search, date, tipo, conta)
    return jsonify(data)

@bp.route('/balances', methods=['GET'])
@jwt_required()
def get_balances():
    return jsonify(service.get_balances())

@bp.route('', methods=['POST'])
@jwt_required()
def create():
    try:
        return jsonify(service.create(request.get_json(silent=True) or {})), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@bp.route('/troca', methods=['POST'])
@jwt_required()
def create_troca():
    try:
        return jsonify(service.create_troca(request.get_json(silent=True) or {})), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

# --- ROTA QUE ESTAVA FALTANDO PARA EDITAR LANÇAMENTO ---
@bp.route('/<int:id>', methods=['PUT'])
@limite_senha
@jwt_required()
def update_transaction(id):
    try:
        result = service.update(id, request.get_json(silent=True) or {})
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if result:
        return jsonify(result), 200
    return jsonify({"error": "Lançamento não encontrado"}), 404

# --- ROTA DE SALDOS INICIAIS (MANTIDA) ---
@bp.route('/initial-balances', methods=['PUT'])
@so_admin
def update_initial_balances():
    """Capital social e saldos iniciais: mudam todos os saldos, so o Admin."""
    try:
        ok = service.update_initial_balances(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    if ok:
        return jsonify({"message": "Saldos iniciais salvos com sucesso"}), 200
    return jsonify({"error": "Falha ao salvar saldos"}), 400

@bp.route('/<int:id>', methods=['DELETE'])
@limite_senha
@so_admin
def delete(id):
    """Apaga lancamento do caixa. Exige a senha de quem esta logado no corpo."""
    data = request.get_json(silent=True) or {}
    try:
        ok, erro = service.delete(id, data.get('senha'))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    if ok:
        return jsonify({'message': 'Deletado com sucesso'})
    return jsonify({'error': erro or 'Erro ao deletar'}), 404