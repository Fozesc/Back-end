from flask import Blueprint, jsonify, request
from app.services.client_service import ClientService
from flask_jwt_extended import jwt_required
bp = Blueprint('clients', __name__, url_prefix='/api/clients')
service = ClientService()

@bp.route('', methods=['GET'])
@jwt_required()
def index():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '', type=str)
    
    data = service.get_paginated(page, per_page, search)
    return jsonify(data)

@bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def show(id):
    client = service.get_by_id(id)
    if not client: return jsonify({'error': 'Cliente não encontrado'}), 404
    return jsonify({
        'id': client.id, 'name': client.name, 'document': client.document,
        'phone': client.phone, 'credit_limit': client.credit_limit,
        'standard_rate': client.standard_rate, 'notes': client.notes,
        'address': client.address
    })

@bp.route('', methods=['POST'])
@jwt_required()
def create():
    client = service.create(request.json)
    return jsonify({'id': client.id, 'message': 'Criado com sucesso'}), 201

@bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def update(id):
    client = service.update(id, request.json)
    if client: return jsonify({'message': 'Atualizado'})
    return jsonify({'error': 'Erro ao atualizar'}), 400