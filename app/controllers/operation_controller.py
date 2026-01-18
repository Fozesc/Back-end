from flask import Blueprint, request, jsonify
from app.services.operation_service import OperationService
from app.schemas.operation_schema import create_operation_schema, operation_schema

bp = Blueprint('operations', __name__, url_prefix='/api/operations')
service = OperationService()

@bp.route('', methods=['POST'])
def create():
    try:

        data = create_operation_schema.load(request.json)
        

        operation = service.create_operation(data)
        
      
        return jsonify(operation_schema.dump(operation)), 201
        
    except Exception as e:

        print(f"Erro ao criar operação: {e}")
        return jsonify({'error': str(e)}), 400