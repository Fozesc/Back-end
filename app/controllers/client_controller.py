from flask import Blueprint, request, jsonify
from app.services.client_service import ClientService
from app.schemas.client_schema import client_schema, clients_schema

bp = Blueprint('clients', __name__, url_prefix='/api/clients')
service = ClientService()

@bp.route('', methods=['GET'])
def index():
    clients = service.get_all_clients()
    return jsonify(clients_schema.dump(clients))

@bp.route('', methods=['POST'])
def store():
    try:
        data = request.json
        client = service.create_client(data)
        return jsonify(client_schema.dump(client)), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

@bp.route('/<int:id>', methods=['GET'])
def show(id):
    client = service.get_client(id)
    if not client:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(client_schema.dump(client))