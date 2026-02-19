from flask import Blueprint, request, jsonify
from app.services.settings_service import SettingsService
from flask_jwt_extended import jwt_required

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