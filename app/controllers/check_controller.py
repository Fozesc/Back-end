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
    calculo = request.args.get('calculo')  # 'dentro' | 'fora' | vazio (todos)

    return jsonify(service.get_paginated(page, per_page, search, status, date_start,
                                         date_end, sort_by, sort_order, calculo))


# Filtros que a acao em lote aceita - os MESMOS da listagem. Whitelist de proposito:
# sem ela, um campo qualquer no JSON entraria direto como parametro da query.
FILTROS_LOTE = ('search', 'status', 'date_start', 'date_end', 'calculo')


@bp.route('/calculo', methods=['PATCH'])
@jwt_required()
def definir_calculo():
    """Tira/devolve cheques do calculo em lote (selecionados ou pelo filtro da tela)."""
    data = request.get_json(silent=True) or {}

    if 'fora' not in data:
        return jsonify({'error': "Informe 'fora': true (tirar do cálculo) ou false (voltar)"}), 400

    ids = data.get('ids')
    filtros = {k: v for k, v in (data.get('filtros') or {}).items() if k in FILTROS_LOTE}

    success, result = service.definir_calculo(data.get('fora'), ids=ids, filtros=filtros)
    if success:
        return jsonify(result), 200
    return jsonify({'error': result}), 400

@bp.route('/emitentes', methods=['GET'])
@jwt_required()
def emitentes():
    """Sugestoes para o campo Emitente (os mais usados primeiro)."""
    return jsonify(service.listar_emitentes(request.args.get('q'),
                                            request.args.get('limit', 10, type=int)))


@bp.route('/portfolio-total', methods=['GET'])
@jwt_required()
def portfolio_total():
    return jsonify(service.get_portfolio_total())

# Chaves de `payment_data` que o servico entende. Whitelist para nada mais do JSON
# entrar por engano na baixa (ex.: mandar 'status' ou campo do cheque por dentro).
CAMPOS_PAGAMENTO = ('method', 'forma', 'amount', 'taxa_multa', 'partes')


@bp.route('/<int:id>/status', methods=['PATCH'])
@jwt_required()
def update_status(id):
    data = request.get_json(silent=True) or {}
    new_status = data.get('status')

    if not new_status:
        return jsonify({'error': 'Status é obrigatório'}), 400

    payment_data = {k: v for k, v in (data.get('payment_data') or {}).items()
                    if k in CAMPOS_PAGAMENTO}
    # formato antigo (campos no topo do JSON) continua aceito
    for topo, dentro in (('paid_amount', 'amount'), ('payment_method', 'method'),
                         ('taxa_multa', 'taxa_multa')):
        if data.get(topo) is not None:
            payment_data.setdefault(dentro, data[topo])

    try:
        result = service.update_status(id, new_status, payment_data)
    except ValueError as e:
        # recebimento dividido invalido (soma nao fecha, conta desconhecida, etc.)
        return jsonify({'error': str(e)}), 400

    if not result:
        return jsonify({'error': 'Cheque não encontrado'}), 404

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

@bp.route('', methods=['POST'], strict_slashes=False)
@bp.route('/', methods=['POST'], strict_slashes=False)
@jwt_required()
def create_check():
    data = request.get_json()
    success, result = service.create(data)
    if success:
        return jsonify(result), 201
    return jsonify({'error': result}), 400

@bp.route('/<int:id>', methods=['PUT'], strict_slashes=False)
@jwt_required()
def update_check(id):
    """Edita nome/datas do cheque. Exige a senha de quem esta logado no corpo do PUT."""
    data = request.get_json(silent=True) or {}
    try:
        success, result = service.update(id, data)
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    if success:
        return jsonify(result), 200
    return jsonify({'error': result}), 400