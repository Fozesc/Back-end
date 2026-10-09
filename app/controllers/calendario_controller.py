from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app import db
from app.services.calendario_service import CalendarioService

bp = Blueprint('calendario', __name__)
service = CalendarioService()


@bp.route('', methods=['GET'])
@jwt_required()
def periodo():
    try:
        return jsonify(service.periodo(request.args.get('inicio'), request.args.get('fim'),
                                       request.args.get('cliente_id', type=int)))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@bp.route('/previsao', methods=['GET'])
@jwt_required()
def previsao():
    return jsonify(service.previsao())


@bp.route('/eventos', methods=['POST'])
@jwt_required()
def criar():
    try:
        return jsonify(service.criar(request.get_json(silent=True) or {})), 201
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400


@bp.route('/eventos/<int:id>', methods=['PUT'])
@jwt_required()
def editar(id):
    try:
        e = service.editar(id, request.get_json(silent=True) or {})
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    if e is None:
        return jsonify({'error': 'Evento não encontrado'}), 404
    return jsonify(e)


@bp.route('/eventos/<int:id>', methods=['DELETE'])
@jwt_required()
def apagar(id):
    if not service.apagar(id):
        return jsonify({'error': 'Evento não encontrado'}), 404
    return '', 204
