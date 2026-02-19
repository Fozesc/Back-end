from flask import Blueprint, request, jsonify
from app.services.operation_service import OperationService
from flask_jwt_extended import jwt_required
from app.schemas.operation_schema import operation_schema, operations_schema

bp = Blueprint('operations', __name__, url_prefix='/api/operations')
service = OperationService()

@bp.route('', methods=['POST'])
@jwt_required()
def store():
    try:
        new_operation = service.create_operation(request.json)
        return jsonify(operation_schema.dump(new_operation)), 201

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        print(f"Erro no Controller: {e}")
        return jsonify({'error': 'Erro interno ao processar borderô'}), 500


@bp.route('', methods=['GET'])
@jwt_required()
def index():
    operations = service.get_all()
    return jsonify(operations_schema.dump(operations))


@bp.route('/client/<int:client_id>', methods=['GET'])
@jwt_required()
def get_by_client(client_id):

    try:
        data = service.get_by_client(client_id)
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
