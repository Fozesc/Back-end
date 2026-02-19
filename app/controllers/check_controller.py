from flask import Blueprint, request, jsonify
from app.services.check_service import CheckService
from flask_jwt_extended import jwt_required

bp = Blueprint('check_bp', __name__) 
service = CheckService()



@bp.route('/', methods=['GET'], strict_slashes=False)
@jwt_required()
def index():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    date_start = request.args.get('date_start')
    date_end = request.args.get('date_end')
    sort_by = request.args.get('sort_by', 'due_date')
    sort_order = request.args.get('sort_order', 'asc')
    
    return jsonify(service.get_paginated(page, per_page, search, status, date_start, date_end, sort_by, sort_order))

@bp.route('/portfolio-total', methods=['GET'])
@jwt_required()
def portfolio_total():
    return jsonify(service.get_portfolio_total())

@bp.route('/<int:id>/status', methods=['PATCH'])
@jwt_required()
def update_status(id):
    data = request.get_json()
    new_status = data.get('status')
    
    payment_data = None
    if new_status == 'Pago':
        payment_data = {
            'amount': data.get('paid_amount'),
            'method': data.get('payment_method')
        }

    if not new_status:
        return jsonify({'error': 'Status é obrigatório'}), 400
        
    result = service.update_status(id, new_status, payment_data)
    
    if not result:
        return jsonify({'error': 'Erro ao atualizar status'}), 400
        
    return jsonify({'message': 'Status atualizado com sucesso'})

@bp.route('/<int:id>/prorrogate', methods=['POST'])
@jwt_required()
def prorrogate(id):
    data = request.get_json()
    
    new_date = data.get('new_date')
    fee = data.get('fee_amount', 0.0)
    notes = data.get('notes', '')

    if not new_date:
        return jsonify({'error': 'Nova data é obrigatória'}), 400

    success, message = service.prorrogate_check(id, new_date, fee, notes)
    
    if not success:
        return jsonify({'error': message}), 400
        
    return jsonify({'message': message})

@bp.route('/<int:id>', methods=['DELETE'])
@jwt_required()
def delete(id):
    if service.delete(id):
        return jsonify({'message': 'Cheque removido'})
    return jsonify({'error': 'Erro ao remover'}), 400