from flask import Blueprint, request, jsonify
from app.services.settings_service import SettingsService
from flask_jwt_extended import get_jwt, jwt_required

bp = Blueprint('settings_bp', __name__)
service = SettingsService()

@bp.route('/', methods=['GET'], strict_slashes=False)
@jwt_required()
def get_settings():
    return jsonify(service.get_settings())

@bp.route('/', methods=['PUT'], strict_slashes=False)
@jwt_required()
def update_settings():
    data = request.get_json()
    return jsonify(service.update_settings(data))


def _so_admin():
    if get_jwt().get('role') != 'Admin':
        return jsonify({'error': 'Só o administrador mexe no backup'}), 403


@bp.route('/backup', methods=['GET'])
@jwt_required()
def get_backup():
    return _so_admin() or jsonify(service.get_backup())


@bp.route('/backup', methods=['PUT'])
@jwt_required()
def update_backup():
    negado = _so_admin()
    if negado:
        return negado
    try:
        return jsonify(service.update_backup(request.get_json(silent=True) or {}))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@bp.route('/backup/enviar', methods=['POST'])
@jwt_required()
def enviar_backup():
    negado = _so_admin()
    if negado:
        return negado
    try:
        return jsonify(service.pedir_envio_backup())
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
