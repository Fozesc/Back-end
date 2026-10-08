from flask import Blueprint, jsonify, request
from app import db
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

@bp.route('/merge', methods=['POST'])
@jwt_required()
def merge():
    """Junta dois cadastros do mesmo cliente. Exige a senha de quem esta logado."""
    data = request.json or {}
    try:
        origem = int(data.get('origem_id'))
        destino = int(data.get('destino_id'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Informe os dois clientes'}), 400

    try:
        return jsonify(service.merge(origem, destino, data.get('senha')))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        return jsonify({'error': 'Não foi possível juntar os cadastros'}), 500


@bp.route('/<int:id>/notas', methods=['GET'])
@jwt_required()
def listar_notas(id):
    data = service.listar_notas(id, request.args.get('page', 1, type=int), request.args.get('per_page', 20, type=int))
    if data is None:
        return jsonify({'error': 'Cliente não encontrado'}), 404
    return jsonify(data)


@bp.route('/<int:id>/notas', methods=['POST'])
@jwt_required()
def criar_nota(id):
    try:
        nota = service.criar_nota(id, request.get_json(silent=True))
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    if nota is None:
        return jsonify({'error': 'Cliente não encontrado'}), 404
    return jsonify(nota), 201


@bp.route('/<int:id>/notas/<int:nota_id>', methods=['PUT'])
@jwt_required()
def editar_nota(id, nota_id):
    try:
        nota = service.editar_nota(id, nota_id, request.get_json(silent=True))
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    if nota is None:
        return jsonify({'error': 'Nota não encontrada'}), 404
    return jsonify(nota)


@bp.route('/<int:id>/notas/<int:nota_id>', methods=['DELETE'])
@jwt_required()
def apagar_nota(id, nota_id):
    if not service.apagar_nota(id, nota_id):
        return jsonify({'error': 'Nota não encontrada'}), 404
    return '', 204
