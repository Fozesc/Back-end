from flask import Blueprint, request, jsonify
from app.services.user_service import UserService
from flask_jwt_extended import jwt_required


bp = Blueprint('user_bp', __name__)
service = UserService()


@bp.route('/', methods=['GET'], strict_slashes=False)
@jwt_required()
def list_users():
    return jsonify(service.get_all())

@bp.route('/', methods=['POST'], strict_slashes=False)
@jwt_required()
def create_user():
    data = request.get_json()
    try:
        user = service.create(data)
        return jsonify(user), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def update_user(id):
    data = request.get_json()
    try:
        user = service.update(id, data)
        return jsonify(user)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@bp.route('/<int:id>', methods=['DELETE'])
@jwt_required()
def delete_user(id):
    if service.delete(id):
        return jsonify({'message': 'Usuário removido'})
    return jsonify({'error': 'Erro ao remover'}), 400