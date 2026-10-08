from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app import db
from app.services.vale_service import ValeService

bp = Blueprint('vales', __name__)
service = ValeService()


@bp.route('', methods=['GET'])
@jwt_required()
def index():
    return jsonify(service.listar(
        request.args.get('page', 1, type=int),
        request.args.get('per_page', 20, type=int),
        request.args.get('status', type=str),
        request.args.get('search', '', type=str),
    ))


@bp.route('', methods=['POST'])
@jwt_required()
def criar():
    try:
        return jsonify(service.criar(request.get_json(silent=True) or {})), 201
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def detalhe(id):
    v = service.detalhe(id)
    if v is None:
        return jsonify({'error': 'Vale não encontrado'}), 404
    return jsonify(v)


@bp.route('/<int:id>/pagamentos', methods=['POST'])
@jwt_required()
def pagar(id):
    try:
        v = service.pagar(id, request.get_json(silent=True) or {})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    if v is None:
        return jsonify({'error': 'Vale não encontrado'}), 404
    return jsonify(v)


@bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def editar(id):
    try:
        v = service.editar(id, request.get_json(silent=True) or {})
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    if v is None:
        return jsonify({'error': 'Vale não encontrado'}), 404
    return jsonify(v)


@bp.route('/<int:id>', methods=['DELETE'])
@jwt_required()
def apagar(id):
    try:
        r = service.apagar(id, request.get_json(silent=True) or {})
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    if r is None:
        return jsonify({'error': 'Vale não encontrado'}), 404
    return jsonify(r)
