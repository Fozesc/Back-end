from flask import Blueprint, jsonify, request
from app.services.dashboard_service import DashboardService
from flask_jwt_extended import jwt_required

bp = Blueprint('dashboard', __name__, url_prefix='/api/dashboard')
service = DashboardService()

@bp.route('', methods=['GET'])
@jwt_required()
def index():
    period = request.args.get('period', 'meses')
    try:
        data = service.get_dashboard_data(period)
        return jsonify(data)
    except Exception as e:
        print(f"Erro Dashboard: {e}")
        return jsonify({'error': 'Erro ao carregar dashboard'}), 500