from flask import Blueprint, jsonify, request
from app.services.transaction_service import TransactionService
from flask_jwt_extended import jwt_required

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
    
    data = service.get_paginated(page, per_page, search, date, tipo)
    return jsonify(data)

@bp.route('/balances', methods=['GET'])
@jwt_required()
def get_balances():
    return jsonify(service.get_balances())

@bp.route('', methods=['POST'])
@jwt_required()
def create():
    data = request.json
    return jsonify(service.create(data)), 201

# --- ROTA QUE ESTAVA FALTANDO PARA EDITAR LANÇAMENTO ---
@bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def update_transaction(id):
    data = request.get_json()
    result = service.update(id, data)
    if result:
        return jsonify(result), 200
    return jsonify({"error": "Lançamento não encontrado"}), 404

# --- ROTA DE SALDOS INICIAIS (MANTIDA) ---
@bp.route('/initial-balances', methods=['PUT'])
@jwt_required()
def update_initial_balances():
    data = request.get_json()
    if service.update_initial_balances(data):
        return jsonify({"message": "Saldos iniciais salvos com sucesso"}), 200
    return jsonify({"error": "Falha ao salvar saldos"}), 400

@bp.route('/<int:id>', methods=['DELETE'])
@jwt_required()
def delete(id):
    if service.delete(id):
        return jsonify({'message': 'Deletado com sucesso'})
    return jsonify({'error': 'Erro ao deletar'}), 400