from flask import Blueprint, request, jsonify
from app.services.settings_service import SettingsService
from flask_jwt_extended import jwt_required
from app.controllers import so_admin

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


@bp.route('/backup', methods=['GET'])
@so_admin
def get_backup():
    return jsonify(service.get_backup())


@bp.route('/backup/status', methods=['GET'])
@so_admin
def status_backup():
    """Aviso da tela inicial quando o backup falha ou para de rodar."""
    return jsonify(service.status_backup())


@bp.route('/backup', methods=['PUT'])
@so_admin
def update_backup():
    try:
        return jsonify(service.update_backup(request.get_json(silent=True) or {}))
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@bp.route('/backup/enviar', methods=['POST'])
@so_admin
def enviar_backup():
    try:
        return jsonify(service.pedir_envio_backup())
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
