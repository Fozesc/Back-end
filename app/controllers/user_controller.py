from flask import Blueprint, request, jsonify
from app.controllers import so_admin
from app.services.user_service import UserService


bp = Blueprint('user_bp', __name__)
service = UserService()


@bp.route('/', methods=['GET'], strict_slashes=False)
@so_admin
def list_users():
    return jsonify(service.get_all())

@bp.route('/', methods=['POST'], strict_slashes=False)
@so_admin
def create_user():
    data = request.get_json(silent=True) or {}
    try:
        user = service.create(data)
        return jsonify(user), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@bp.route('/<int:id>', methods=['PUT'])
@so_admin
def update_user(id):
    data = request.get_json(silent=True) or {}
    try:
        user = service.update(id, data)
        return jsonify(user)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@bp.route('/<int:id>', methods=['DELETE'])
@so_admin
def delete_user(id):
    try:
        if service.delete(id):
            return jsonify({'message': 'Usuário removido'})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'error': 'Usuário não encontrado'}), 404
