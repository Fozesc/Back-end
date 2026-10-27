from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app import db
from app.services.recorrente_service import RecorrenteService

bp = Blueprint('recorrentes', __name__)
service = RecorrenteService()


def _responder(funcao, *args, status=200):
    try:
        r = funcao(*args)
    except ValueError as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 400
    if r is None:
        return jsonify({'error': 'Despesa recorrente não encontrada'}), 404
    return jsonify(r), status


@bp.route('', methods=['GET'])
@jwt_required()
def listar():
    return jsonify(service.listar())


@bp.route('', methods=['POST'])
@jwt_required()
def criar():
    return _responder(service.criar, request.get_json(silent=True) or {}, status=201)


@bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def editar(id):
    return _responder(service.editar, id, request.get_json(silent=True) or {})


@bp.route('/<int:id>', methods=['DELETE'])
@jwt_required()
def apagar(id):
    if not service.apagar(id):
        return jsonify({'error': 'Despesa recorrente não encontrada'}), 404
    return '', 204


@bp.route('/gerar', methods=['POST'])
@jwt_required()
def gerar():
    """Debito automatico do que venceu ate hoje. A tela chama ao abrir o sistema."""
    return jsonify({'lancados': service.gerar()})


@bp.route('/<int:id>/lancar', methods=['POST'])
@jwt_required()
def lancar(id):
    return _responder(service.lancar, id, request.get_json(silent=True) or {}, status=201)


@bp.route('/<int:id>/adiar', methods=['POST'])
@jwt_required()
def adiar(id):
    return _responder(service.adiar, id, (request.get_json(silent=True) or {}).get('ate'))
