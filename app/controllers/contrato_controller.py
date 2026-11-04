import json
import re
from datetime import date, datetime

from flask import Blueprint, jsonify, request
from flask_jwt_extended import get_jwt, get_jwt_identity, jwt_required
from werkzeug.security import check_password_hash
from sqlalchemy import desc, exists, func, or_

from app import db
from app.controllers import limite_senha
from app.models.domain import AuditLog, Check, Client, CompanySettings, Contrato, Operation, User
from app.services.audit_service import AuditService
from app.services.operation_service import EMPRESA_PADRAO, OperationService, reais

bp = Blueprint('contratos', __name__)
audit = AuditService()
MAX_POR_PAGINA = 100
MAX_JSON = 100_000
REP_VAZIO = {'nome': '', 'cpf': '', 'rg': '', 'nacionalidade': '', 'estadoCivil': '', 'endereco': ''}
# numeros que estao no texto do modelo (clausulas 7 e 8 e o fecho); contrato salvo antes da aba Contratos usa estes
CONDICOES_MODELO = {'jurosMora': 1.0, 'multa': 2.0, 'honorarios': 10.0, 'prazoReembolso': 30, 'vias': 2}


def _condicoes_padrao(s):
    """Os padroes de Ajustes > Contratos (vazio = o do modelo)."""
    return {'jurosMora': s.contrato_juros_mora if s.contrato_juros_mora is not None else CONDICOES_MODELO['jurosMora'],
            'multa': s.contrato_multa if s.contrato_multa is not None else CONDICOES_MODELO['multa'],
            'honorarios': s.contrato_honorarios if s.contrato_honorarios is not None else CONDICOES_MODELO['honorarios'],
            'prazoReembolso': s.contrato_prazo_reembolso or CONDICOES_MODELO['prazoReembolso'],
            'vias': s.contrato_vias or CONDICOES_MODELO['vias']}


def _quem():
    return get_jwt().get('name', 'Sistema')


def _num(valor, minimo=2, maximo=5):
    """4 -> '4,00'; 0.0041 -> '0,0041'; 5.6784 -> '5,6784'."""
    inteiro, dec = f"{float(valor or 0):.{maximo}f}".split('.')
    return f"{int(inteiro):,}".replace(',', '.') + ',' + dec.rstrip('0').ljust(minimo, '0')


def _endereco(c):
    linha1 = ', '.join(filter(None, [c.address, c.neighborhood]))
    linha2 = ' - '.join(filter(None, [c.city, c.state])) + (f' · CEP {c.zip_code}' if c.zip_code else '')
    return '\n'.join(filter(None, [linha1, linha2]))


def _anterior(filtro, op_id):
    return (Contrato.query.join(Operation, Contrato.operation_id == Operation.id)
            .filter(filtro, Contrato.operation_id != op_id).order_by(desc(Contrato.atualizado_em)).first())


def _sugestao(op):
    """O contrato montado com o que o sistema sabe: empresa (Ajustes), cliente, titulos e encargos do borderô.
    Representantes e e-mail da contratada vem do ultimo contrato salvo do mesmo cliente / da mesma empresa."""
    d = OperationService().documento(op.id)
    c, s = op.client, CompanySettings.query.first() or CompanySettings()
    empresa = op.empresa or EMPRESA_PADRAO
    ant_cliente = _anterior(Operation.client_id == op.client_id, op.id)
    ant_empresa = _anterior(func.coalesce(Operation.empresa, EMPRESA_PADRAO) == empresa, op.id)
    contratada_ant = (ant_empresa.dados.get('contratada') or {}) if ant_empresa else {}
    contratante_ant = (ant_cliente.dados.get('contratante') or {}) if ant_cliente else {}

    # representante: 1o o cadastro (Ajustes / cliente), o que estiver vazio vem do ultimo contrato salvo
    junta = lambda cadastro, anterior: {k: (cadastro.get(k) or '').strip() or (anterior or {}).get(k, '') for k in REP_VAZIO}
    pessoa_fisica = len(re.sub(r'\D', '', c.document or '')) == 11
    if c.rep_nome:
        rep_cad = {'nome': c.rep_nome, 'cpf': c.rep_cpf, 'rg': c.rep_rg, 'nacionalidade': c.rep_nacionalidade,
                   'estadoCivil': c.rep_estado_civil, 'endereco': c.rep_endereco}
    elif pessoa_fisica:
        rep_cad = {'nome': c.name, 'cpf': c.document, 'rg': c.rg, 'nacionalidade': c.nacionalidade,
                   'estadoCivil': c.estado_civil, 'endereco': _endereco(c)}
    else:
        rep_cad = {}
    rep_cliente = junta(rep_cad, contratante_ant.get('rep'))
    p = '' if empresa == 'Fozesc' else 'pq_'
    rep_empresa = junta({'nome': getattr(s, p + 'rep_nome'), 'cpf': getattr(s, p + 'rep_cpf'), 'rg': getattr(s, p + 'rep_rg'),
                         'nacionalidade': getattr(s, p + 'rep_nacionalidade'), 'estadoCivil': getattr(s, p + 'rep_estado_civil'),
                         'endereco': getattr(s, p + 'rep_endereco')}, contratada_ant.get('rep'))

    titulos = []
    for t in d['titulos']:
        promissoria = t['tipo'] == 'PROMISSORIA'
        do_cliente = promissoria or (t['emitente'] or '').strip().lower() == c.name.strip().lower()
        titulos.append({'tipo': 'Nota promissória' if promissoria else 'Cheque', 'numero': t['numero'],
                        'emitente': c.name if promissoria else t['emitente'],
                        'documento': (c.document or '') if do_cliente else '',
                        'vencimento': '/'.join(reversed(t['vencimento'].split('-'))) if t['vencimento'] else '',
                        'valor': reais(t['valor'])})

    face = sum(t['valor'] for t in d['titulos'])
    iof_total = round(d['iof_total'], 2)
    base = op.iof_base if op.iof_base is not None else (s.iof_rate or 0)
    diario_pct = op.iof_diario if op.iof_diario is not None else (s.iof_daily_rate or 0)
    adicional = min(round(face * base / 100, 2), iof_total) if iof_total > 0 else 0.0
    condicoes = _condicoes_padrao(s)
    return {
        'numero': str(op.id),
        'valorAntecipado': reais(d['valor_total']),
        'local': s.contrato_cidade or 'Foz do Iguaçu',
        'condicoes': condicoes,
        'data': (op.operation_date or date.today()).isoformat(),
        'contratada': {
            'nome': d['empresa']['nome'], 'cnpj': d['empresa']['cnpj'] or '', 'endereco': d['empresa']['endereco'] or '',
            'telefone': ((s.phone if empresa == 'Fozesc' else s.pq_telefone) or contratada_ant.get('telefone', '')),
            'email': getattr(s, p + 'email') or contratada_ant.get('email', ''),
            'esc': bool(s.esc if empresa == 'Fozesc' else s.pq_esc),
            'rep': rep_empresa,
        },
        'contratante': {
            'nome': c.name, 'documento': c.document or '', 'endereco': _endereco(c),
            'telefone': c.phone or contratante_ant.get('telefone', ''), 'email': c.email or contratante_ant.get('email', ''),
            'rep': rep_cliente,
        },
        'titulos': titulos,
        'encargos': {
            'valorAntecipado': reais(d['liquido_total']), 'taxaJuros': _num(d['taxa_mensal'], 2, 4),
            'jurosMora': _num(condicoes['jurosMora'], 2, 4) + '%',
            # sem IOF cobrado no borderô, as taxas do Quadro 03 tambem saem zeradas
            'iofAdicionalPct': _num(base if iof_total > 0 else 0, 2, 5) + '%',
            'iofDiarioPct': _num(diario_pct if iof_total > 0 else 0, 2, 5) + '%',
            'iofAdicional': reais(adicional), 'iofDiario': reais(round(iof_total - adicional, 2)), 'iofTotal': reais(iof_total),
        },
        'testemunhas': [{'nome': '', 'cpf': '', 'rg': ''}, {'nome': '', 'cpf': '', 'rg': ''}],
    }


def _taxa_mensal(fluxos, presente):
    """Taxa ao mes que faz os titulos [(valor, dias)] valerem `presente` hoje (juros compostos, mes de 30 dias)."""
    if presente <= 0 or not fluxos or all(d <= 0 for _, d in fluxos):
        return None
    falta = lambda r: sum(v / (1 + r) ** (max(d, 0) / 30) for v, d in fluxos) - presente
    if falta(0) <= 0:
        return 0.0
    baixo, alto = 0.0, 1.0
    if falta(alto) > 0:
        return None
    for _ in range(100):
        meio = (baixo + alto) / 2
        baixo, alto = (meio, alto) if falta(meio) > 0 else (baixo, meio)
    return (baixo + alto) / 2


def _resumo_operacao(op):
    """Os numeros do "Borderô da operação de desconto" (modelo teste-contratos/Borderô 398)."""
    d = OperationService().documento(op.id)
    fluxos = [(t.original_amount if t.original_amount is not None else t.amount, t.days or 0) for t in op.checks]
    valor, juros, iof, liquido = d['valor_total'], d['juros_total'], d['iof_total'], d['liquido_total']
    pagos = [t.status == 'Pago' for t in op.checks]
    primeiro = (db.session.query(func.min(Operation.operation_date)).filter(Operation.client_id == op.client_id).scalar())
    criado = (AuditLog.query.filter(AuditLog.target == 'Borderô', AuditLog.action == 'CREATE',
                                    AuditLog.description.like(f'Novo Borderô #{op.id} criado.%')).first())
    efetiva = _taxa_mensal(fluxos, round(valor - juros, 2))
    cet = _taxa_mensal(fluxos, liquido)
    prazo = sum(v * dias for v, dias in fluxos) / valor if valor else 0
    return {
        'situacao': 'Paga' if pagos and all(pagos) else 'Em aberto',
        'cliente_desde': primeiro.isoformat() if primeiro else None,
        'operador': criado.user_name if criado else '',
        'qtd_titulos': len(fluxos), 'valor_total': valor, 'juros': juros, 'iof_total': iof, 'liquido': liquido,
        'taxa_mensal': d['taxa_mensal'], 'prazo_medio': round(prazo, 2),
        'taxa_periodo': round(juros / valor * 100, 6) if valor else 0,
        'taxa_efetiva_mes': round(efetiva * 100, 6) if efetiva is not None else None,
        'cet_mes': round(cet * 100, 2) if cet is not None else None,
        'cet_ano': round(((1 + cet) ** 12 - 1) * 100, 2) if cet is not None else None,
    }


def _limpar(valor, nivel=0):
    """So texto, numero, lista e objeto, com tamanho limitado: o contrato e' guardado como veio da tela."""
    if nivel > 4:
        raise ValueError('Contrato inválido')
    if isinstance(valor, dict):
        if len(valor) > 60:
            raise ValueError('Contrato inválido')
        return {str(k)[:40]: _limpar(v, nivel + 1) for k, v in valor.items()}
    if isinstance(valor, list):
        if len(valor) > 200:
            raise ValueError('Contrato com itens demais')
        return [_limpar(v, nivel + 1) for v in valor]
    if valor is None or isinstance(valor, bool):
        return ''
    if isinstance(valor, (int, float)):
        return valor
    return str(valor)[:1000]


def _bordero(op):
    return {'id': op.id, 'cliente': op.client.name if op.client else '', 'cliente_id': op.client_id,
            'data': op.operation_date.isoformat() if op.operation_date else None,
            'valor': float(op.total_face_value or 0), 'empresa': op.empresa or EMPRESA_PADRAO}


def _assinatura(k):
    return {'assinado': bool(k.assinado), 'assinado_por': k.assinado_por,
            'assinado_em': k.assinado_em.strftime('%d/%m/%Y %H:%M') if k.assinado_em else None}


def gerar_contrato_se_faltar(op, quem):
    """Borderô da Fozesc (empresa dos contratos) ja nasce com o contrato montado com os dados do sistema.
    Nao mexe em contrato que ja existe; falha aqui nunca derruba o borderô."""
    try:
        if (op.empresa or EMPRESA_PADRAO) != 'Fozesc' or Contrato.query.filter_by(operation_id=op.id).first():
            return False
        db.session.add(Contrato(operation_id=op.id, dados=_sugestao(op), atualizado_por=str(quem)[:100],
                                atualizado_em=datetime.now()))
        db.session.commit()
        audit.log_action(quem, 'CREATE', 'Contrato', f"Gerou automaticamente o contrato do borderô #{op.id} "
                                                      f"({op.client.name if op.client else ''}) - empresa Fozesc")
        return True
    except Exception:
        db.session.rollback()
        return False


def previa_contrato(op, quem):
    """Contrato e borderô da operação de um borderô montado com simular=True (antes de efetivar). Quem chama desfaz."""
    operacao = _resumo_operacao(op)
    operacao['operador'] = operacao['operador'] or str(quem)
    return {'dados': _sugestao(op), 'operacao': operacao, 'bordero': _bordero(op)}


def _tem_atraso():
    """Borderô com titulo para a notificacao de cobranca (a mesma regra da /cobranca): sem pagar e vencido,
    devolvido ou no juridico."""
    return exists().where(Check.operation_id == Operation.id, Check.status != 'Pago',
                          or_(Check.due_date < date.today(), Check.status.in_(('Devolvido', 'Juridico'))))


@bp.route('/pendencias', methods=['GET'])
@jwt_required()
def pendencias():
    """Aviso da tela inicial: contratos sem assinatura e com titulo para cobrar (conta tambem o borderô
    da Fozesc cujo contrato a lista ainda vai criar)."""
    base = (db.session.query(func.count(Operation.id)).outerjoin(Contrato, Contrato.operation_id == Operation.id)
            .filter(or_(Operation.empresa == 'Fozesc', Contrato.id.isnot(None))))
    return jsonify({'nao_assinados': base.filter(Contrato.assinado.isnot(True)).scalar() or 0,
                    'com_atraso': base.filter(_tem_atraso()).scalar() or 0})


@bp.route('', methods=['GET'])
@jwt_required()
def listar():
    # borderô da Fozesc feito antes do contrato automatico (ou que perdeu o contrato) ganha o dele aqui: a lista
    # mostra todos os contratos, nao so os abertos/salvos na tela
    faltando = (Operation.query.outerjoin(Contrato, Contrato.operation_id == Operation.id)
                .filter(Operation.empresa == 'Fozesc', Contrato.id.is_(None)).order_by(Operation.id).limit(200).all())
    for op in faltando:
        gerar_contrato_se_faltar(op, _quem())

    consulta = Contrato.query.join(Operation, Contrato.operation_id == Operation.id).join(Client, Operation.client_id == Client.id)
    assinado = request.args.get('assinado')
    if assinado in ('sim', 'nao'):
        consulta = consulta.filter(Contrato.assinado.is_(assinado == 'sim'))
    if request.args.get('atraso') == 'sim':
        consulta = consulta.filter(_tem_atraso())
    q = ' '.join(request.args.get('q', '', type=str).split())[:100]
    if q:
        numero = q.lstrip('#')
        consulta = consulta.filter(or_(Client.name.icontains(q, autoescape=True),
                                       *([Operation.id == int(numero)] if numero.isdigit() else [])))
    pagina = (consulta.order_by(desc(Contrato.atualizado_em), desc(Contrato.id))
              .paginate(page=request.args.get('page', 1, type=int), per_page=request.args.get('per_page', 20, type=int),
                        max_per_page=MAX_POR_PAGINA, error_out=False))
    return jsonify({'items': [{**_bordero(k.operation), 'atualizado_por': k.atualizado_por,
                               'atualizado_em': k.atualizado_em.strftime('%d/%m/%Y %H:%M'), **_assinatura(k)} for k in pagina.items],
                    'total': pagina.total, 'pages': pagina.pages, 'page': pagina.page})


@bp.route('/<int:op_id>', methods=['GET'])
@jwt_required()
def abrir(op_id):
    op = db.get_or_404(Operation, op_id)
    salvo = Contrato.query.filter_by(operation_id=op_id).first()
    sugestao = _sugestao(op)
    return jsonify({'bordero': _bordero(op), 'operacao': _resumo_operacao(op), **(_assinatura(salvo) if salvo else {'assinado': False}),
                    'salvo': bool(salvo), 'dados': salvo.dados if salvo else sugestao,
                    'sugestao': sugestao, 'atualizado_por': salvo.atualizado_por if salvo else None,
                    'atualizado_em': salvo.atualizado_em.strftime('%d/%m/%Y %H:%M') if salvo else None})


@bp.route('/<int:op_id>', methods=['PUT'])
@jwt_required()
def salvar(op_id):
    op = db.get_or_404(Operation, op_id)
    corpo = request.get_json(silent=True) or {}
    try:
        dados = _limpar(corpo.get('dados'))
        if not isinstance(dados, dict) or not isinstance(dados.get('titulos'), list) or not isinstance(dados.get('contratante'), dict):
            raise ValueError('Contrato incompleto: faltam o contratante ou os títulos')
        if len(json.dumps(dados, ensure_ascii=False)) > MAX_JSON:
            raise ValueError('Contrato grande demais')
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    contrato = Contrato.query.filter_by(operation_id=op_id).first()
    novo = contrato is None
    if novo:
        contrato = Contrato(operation_id=op_id)
        db.session.add(contrato)
    contrato.dados, contrato.atualizado_por, contrato.atualizado_em = dados, str(_quem())[:100], datetime.now()
    db.session.commit()
    audit.log_action(_quem(), 'CREATE' if novo else 'UPDATE', 'Contrato',
                     f"{'Gerou' if novo else 'Alterou'} o contrato do borderô #{op_id} ({op.client.name if op.client else ''})")
    return jsonify({'salvo': True, 'dados': contrato.dados, 'atualizado_por': contrato.atualizado_por,
                    'atualizado_em': contrato.atualizado_em.strftime('%d/%m/%Y %H:%M')})


@bp.route('/<int:op_id>/assinado', methods=['PUT'])
@limite_senha
@jwt_required()
def marcar_assinado(op_id):
    """Marca/desmarca o contrato como assinado. Pede a senha de quem esta logado (fica na auditoria)."""
    contrato = Contrato.query.filter_by(operation_id=op_id).first()
    if not contrato:
        return jsonify({'error': 'Este borderô ainda não tem contrato salvo'}), 404
    corpo = request.get_json(silent=True) or {}
    if not isinstance(corpo.get('assinado'), bool):
        return jsonify({'error': 'Informe se o contrato está assinado (sim ou não)'}), 400
    usuario = db.session.get(User, int(get_jwt_identity()))
    if not usuario or not check_password_hash(usuario.password_hash, str(corpo.get('senha') or '')):
        audit.log_action(_quem(), 'NEGADO', 'Contrato', f'Senha incorreta ao marcar o contrato do borderô #{op_id} como '
                                                        f"{'assinado' if corpo['assinado'] else 'não assinado'}")
        return jsonify({'error': 'Senha incorreta - nada foi alterado'}), 403
    contrato.assinado = corpo['assinado']
    contrato.assinado_em = datetime.now() if contrato.assinado else None
    contrato.assinado_por = str(_quem())[:100] if contrato.assinado else None
    db.session.commit()
    audit.log_action(_quem(), 'UPDATE', 'Contrato', f"Marcou o contrato do borderô #{op_id} como "
                                                    f"{'ASSINADO' if contrato.assinado else 'NÃO assinado'} [confirmado com senha]")
    return jsonify(_assinatura(contrato))


@bp.route('/<int:op_id>/cobranca', methods=['GET'])
@jwt_required()
def cobranca(op_id):
    """Notificacao de cobranca (clausula 7): titulos vencidos sem pagar, com juros de mora pro rata die capitalizados
    ao mes desde o vencimento e a multa sobre o devido apos os juros (clausula 8), calculados ate hoje."""
    op = db.get_or_404(Operation, op_id)
    contrato = Contrato.query.filter_by(operation_id=op_id).first()
    dados = contrato.dados if contrato else _sugestao(op)
    cond = {**CONDICOES_MODELO, **(dados.get('condicoes') or {})}
    mora, multa_pct = float(cond['jurosMora'] or 0), float(cond['multa'] or 0)
    hoje = date.today()
    titulos = []
    for t in sorted(op.checks, key=lambda t: (t.due_date, t.id)):
        problema = t.status in ('Devolvido', 'Juridico')
        if t.status == 'Pago' or (t.due_date >= hoje and not problema):
            continue
        dias = max((hoje - t.due_date).days, 0)
        valor = round(float(t.amount or 0), 2)
        juros = round(valor * ((1 + mora / 100) ** (dias / 30) - 1), 2)
        multa = round((valor + juros) * multa_pct / 100, 2)
        titulos.append({'tipo': 'Nota promissória' if t.type == 'PROMISSORIA' else 'Cheque', 'numero': t.number or '',
                        'vencimento': t.due_date.isoformat(), 'situacao': 'Atrasado' if t.status == 'Aguardando' else t.status,
                        'dias': dias, 'valor': valor, 'juros': juros, 'multa': multa, 'total': round(valor + juros + multa, 2)})
    soma = lambda k: round(sum(x[k] for x in titulos), 2)
    return jsonify({'numero': dados.get('numero') or str(op.id), 'data_contrato': dados.get('data'), 'local': dados.get('local'),
                    'contratada': dados.get('contratada') or {}, 'contratante': dados.get('contratante') or {},
                    'condicoes': cond, 'hoje': hoje.isoformat(), 'titulos': titulos,
                    'totais': {k: soma(k) for k in ('valor', 'juros', 'multa', 'total')}})
