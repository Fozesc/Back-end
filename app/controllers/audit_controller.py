from flask import Blueprint, jsonify, request
from app.services.audit_service import AuditService
from flask_jwt_extended import jwt_required
bp = Blueprint('audit', __name__, url_prefix='/api/audit')
service = AuditService()

@bp.route('', methods=['GET'])
@jwt_required()
def index():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '', type=str)
    action = request.args.get('action', '', type=str)
    date_start = request.args.get('date_start', '', type=str)
    date_end = request.args.get('date_end', '', type=str)

    data = service.get_paginated(page, per_page, search, action, date_start, date_end)
    return jsonify(data)