from datetime import date, datetime
from sqlalchemy import func, or_
from app import db
from werkzeug.security import check_password_hash
from app.models.domain import Vale, Transaction, User
from app.services.audit_service import AuditService
from app.services.check_service import CONTAS_CAIXA, CheckService
from app.services.operation_service import recalcular_total

MAX_POR_PAGINA = 100
VALOR_MAXIMO = 10_000_000
MAX_PAGAMENTOS = 200


def _usuario():
    try:
        from flask_jwt_extended import get_jwt
        return get_jwt().get('name', 'Sistema')
    except Exception:
        return 'Sistema'


def _data(texto):
    if not texto:
        return date.today()
    try:
        return datetime.strptime(str(texto)[:10], '%Y-%m-%d').date()
    except ValueError:
        raise ValueError('Data inválida')


def _conta(texto):
    if texto not in CONTAS_CAIXA:
        raise ValueError(f"Conta inválida (use {', '.join(CONTAS_CAIXA)})")
    return texto


def _conferir_senha(senha, acao):
    from flask_jwt_extended import get_jwt_identity
    ident = get_jwt_identity()
    usuario = db.session.get(User, int(ident)) if str(ident).isdigit() else None
    if not usuario or not check_password_hash(usuario.password_hash, str(senha or '')):
        AuditService().log_action(_usuario(), 'NEGADO', 'Vale', f"Senha incorreta ao tentar {acao}")
        raise PermissionError('Senha incorreta - nada foi alterado')


def _descricao(dados):
    descricao = ' '.join(str(dados.get('descricao') or '').split())[:200]
    if not descricao:
        raise ValueError('Informe a descrição do vale')
    return descricao


def _valor(dados):
    try:
        valor = round(float(dados.get('valor')), 2)
    except (TypeError, ValueError):
        raise ValueError('Valor inválido')
    if not 0 < valor <= VALOR_MAXIMO:
        raise ValueError('Valor tem que ser maior que zero')
    return valor


def _uma(q):
    achadas = q.limit(2).all()
    return achadas[0] if len(achadas) == 1 else None


def _saida(v):
    t = Transaction.query.filter_by(vale_id=v.id, type='saida').first()
    if t:
        return t
    # vale de antes da saida ser ligada ao vale: acha pelo texto/data/valor/conta, so se for uma linha so
    textos = [f"Vale #{v.id} - {v.descricao}"[:200]] + ([f"Vale - {v.pessoa}"[:200]] if v.pessoa else [])
    t = _uma(Transaction.query.filter(
        Transaction.vale_id.is_(None), Transaction.category == 'Vale', Transaction.type == 'saida',
        Transaction.date == v.data, Transaction.origin == v.conta,
        func.abs(func.abs(Transaction.amount) - v.valor) < 0.005, Transaction.description.in_(textos)))
    if t:
        t.vale_id = v.id
    return t


def _ligar_baixa_antiga(v):
    # vale quitado pela baixa antiga (antes do pagamento parcial): a entrada nao tinha vale_id
    if v.status != 'Pago' or not v.pessoa or not v.data_pagamento:
        return
    if Transaction.query.filter_by(vale_id=v.id, type='entrada').first():
        return
    t = _uma(Transaction.query.filter(
        Transaction.vale_id.is_(None), Transaction.category == 'Vale', Transaction.type == 'entrada',
        Transaction.date == v.data_pagamento, Transaction.origin == v.conta_pagamento,
        func.abs(Transaction.amount - v.valor) < 0.005,
        Transaction.description == f"Pagamento de vale - {v.pessoa}"[:200]))
    if t:
        t.vale_id = v.id


def _pagos(ids):
    if not ids:
        return {}
    return dict(db.session.query(Transaction.vale_id, func.sum(Transaction.amount))
                .filter(Transaction.vale_id.in_(ids), Transaction.type == 'entrada')
                .group_by(Transaction.vale_id).all())


def _texto(v):
    # vale antigo tinha "para quem" + observacao; o novo so tem a descricao
    return ' - '.join(x for x in (v.pessoa, v.descricao) if x)


def _serialize(v, pago=0.0):
    # vale quitado antes do pagamento parcial existir nao tem linhas ligadas: saldo 0 pelo status
    saldo = 0.0 if v.status == 'Pago' else round(max(v.valor - float(pago or 0), 0.0), 2)
    return {
        'id': v.id,
        'descricao': _texto(v),
        'valor': v.valor,
        'pago': round(v.valor - saldo, 2),
        'saldo': saldo,
        'data': v.data.isoformat(),
        'conta': v.conta,
        'status': v.status,
        'data_pagamento': v.data_pagamento.isoformat() if v.data_pagamento else None,
        'conta_pagamento': v.conta_pagamento,
    }


def travar_para_abater(id):
    """Vale em aberto que vai receber um abatimento, travado ate o fim da transacao (dois
    abatimentos ao mesmo tempo nao passam do que falta), e quanto falta pagar nele."""
    try:
        id = int(id)
    except (TypeError, ValueError):
        raise ValueError('Vale inválido')
    v = db.session.query(Vale).filter_by(id=id).with_for_update().first()
    if not v:
        raise ValueError('Vale não encontrado')
    saldo = _serialize(v, _pagos([v.id]).get(v.id))['saldo']
    if saldo <= 0:
        raise ValueError(f'O vale #{v.id} já está quitado')
    return v, saldo


def abatimento(v, saldo, valor, data, conta, origem, **vinculo):
    """Desconta `valor` (ate o `saldo`) da comissao no vale: entra como pagamento do vale na mesma
    conta de onde a comissao sai inteira, entao do caixa so sai de verdade o que passa do vale.
    Quita se cobrir o que falta. O chamador grava a linha junto com o resto da operacao."""
    quitou = valor >= saldo - 0.005
    if quitou:
        v.status, v.data_pagamento, v.conta_pagamento = 'Pago', data, conta[:20]
    return Transaction(date=data, amount=valor, type='entrada', origin=conta, category='Vale', vale_id=v.id,
                       **vinculo, description=(f"{'Desconto' if quitou else 'Desconto parcial'} no vale #{v.id} - "
                                               f"{_texto(v)} · {origem}")[:200])


class ValeService:
    def __init__(self):
        self.audit = AuditService()

    def listar(self, page, per_page, status=None, busca=None):
        per_page = max(1, min(per_page, MAX_POR_PAGINA))
        q = Vale.query
        if status in ('Aberto', 'Pago'):
            q = q.filter(Vale.status == status)
        if busca:
            q = q.filter(or_(Vale.descricao.ilike(f"%{busca}%"), Vale.pessoa.ilike(f"%{busca}%")))
        pag = q.order_by(Vale.data.desc(), Vale.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
        em_aberto, qtd_aberto = db.session.query(
            func.coalesce(func.sum(Vale.valor), 0.0), func.count(Vale.id)
        ).filter(Vale.status == 'Aberto').one()
        ja_pago = db.session.query(func.coalesce(func.sum(Transaction.amount), 0.0)).join(
            Vale, Transaction.vale_id == Vale.id
        ).filter(Vale.status == 'Aberto', Transaction.type == 'entrada').scalar()
        pagos = _pagos([v.id for v in pag.items])
        return {
            'items': [_serialize(v, pagos.get(v.id)) for v in pag.items],
            'total': pag.total,
            'pages': pag.pages,
            'current_page': page,
            'em_aberto': round(float(em_aberto) - float(ja_pago), 2),
            'qtd_aberto': qtd_aberto,
        }

    def criar(self, dados):
        descricao = _descricao(dados)
        valor = _valor(dados)
        v = Vale(descricao=descricao, valor=valor,
                 data=_data(dados.get('data')), conta=_conta(dados.get('conta') or 'Dinheiro'))
        db.session.add(v)
        db.session.flush()
        db.session.add(Transaction(date=v.data, description=f"Vale #{v.id} - {descricao}"[:200], amount=valor,
                                   type='saida', origin=v.conta, category='Vale', vale_id=v.id))
        db.session.commit()
        self.audit.log_action(_usuario(), 'CREATE', 'Vale',
                              f"Vale #{v.id}: {descricao} | R$ {valor} | conta: {v.conta}")
        return _serialize(v)

    def pagar(self, id, dados):
        v = db.session.query(Vale).filter_by(id=id).with_for_update().first()
        if not v:
            return None
        if v.status == 'Pago':
            raise ValueError('Este vale já foi pago')
        saldo = _serialize(v, _pagos([v.id]).get(v.id))['saldo']
        try:
            valor = round(float(dados.get('valor', saldo)), 2)
        except (TypeError, ValueError):
            raise ValueError('Valor inválido')
        if not valor > 0:
            raise ValueError('Valor tem que ser maior que zero')
        if valor > saldo + 0.005:
            raise ValueError(f"Valor maior que o saldo do vale (R$ {saldo:.2f})")
        if dados.get('partes'):
            partes = CheckService()._validar_partes(dados['partes'], valor, 'valor do pagamento')
        else:
            partes = [(_conta(dados.get('conta') or v.conta), '', valor)]
        data = _data(dados.get('data'))
        obs = ' '.join(str(dados.get('observacao') or '').split())[:100]

        quitou = valor >= saldo - 0.005
        rotulo = 'Pagamento' if quitou else 'Pagamento parcial'
        for i, (conta, forma, valor_parte) in enumerate(partes, 1):
            extra = ([f"Parte {i}/{len(partes)}"] if len(partes) > 1 else []) \
                + ([forma] if forma and forma != conta else [])
            sufixo = f" ({' · '.join(extra)})" if extra else ''
            db.session.add(Transaction(date=data, amount=valor_parte, type='entrada', origin=conta,
                                       description=(f"{rotulo} do vale #{v.id} - {_texto(v)}{sufixo}"
                                                    + (f" · {obs}" if obs else ''))[:200],
                                       category='Vale', vale_id=v.id))
        contas = '+'.join(dict.fromkeys(c for c, _, _ in partes))
        if quitou:
            v.status = 'Pago'
            v.data_pagamento = data
            v.conta_pagamento = contas[:20]
        db.session.commit()
        self.audit.log_action(_usuario(), 'BAIXA' if quitou else 'PAGAMENTO', 'Vale',
                              f"{rotulo} do vale #{v.id} ({_texto(v)}) | R$ {valor:.2f} de R$ {saldo:.2f} | "
                              + ' + '.join(f"{c} R$ {p:.2f}" + (f" ({f})" if f else '') for c, f, p in partes)
                              + (f" | obs: {obs}" if obs else ''))
        return _serialize(v, _pagos([v.id]).get(v.id))

    def detalhe(self, id):
        v = db.session.get(Vale, id)
        if not v:
            return None
        linhas = Transaction.query.with_entities(
            Transaction.id, Transaction.date, Transaction.amount, Transaction.origin, Transaction.description,
            Transaction.operation_id, Transaction.check_id
        ).filter(Transaction.vale_id == id, Transaction.type == 'entrada').order_by(
            Transaction.date, Transaction.id).limit(MAX_PAGAMENTOS).all()
        return {
            **_serialize(v, sum(l.amount for l in linhas)),
            # desconto com comissao: o unico pagamento de vale ligado a um borderô/cheque
            'pagamentos': [{'id': l.id, 'data': l.date.isoformat(), 'valor': l.amount, 'conta': l.origin,
                            'descricao': l.description, 'abatimento': bool(l.operation_id or l.check_id)}
                           for l in linhas],
        }

    def editar(self, id, dados):
        if not db.session.get(Vale, id):
            return None
        _conferir_senha(dados.get('senha'), f"editar o vale #{id}")
        v = db.session.query(Vale).filter_by(id=id).with_for_update().first()
        descricao = _descricao(dados)
        valor = _valor(dados)
        data = _data(dados.get('data') or v.data.isoformat())
        conta = _conta(dados.get('conta') or v.conta)

        _ligar_baixa_antiga(v)
        saida = _saida(v)
        db.session.flush()
        pago = round(float(_pagos([v.id]).get(v.id) or 0), 2)
        quitado_antigo = v.status == 'Pago' and pago == 0
        if quitado_antigo and abs(valor - v.valor) > 0.005:
            raise ValueError('Este vale foi quitado antes do registro de pagamentos: '
                             'dá para mudar descrição, data e conta, mas não o valor')
        if valor < pago - 0.005:
            raise ValueError(f"Este vale já tem R$ {pago:.2f} pago - o valor não pode ficar menor que isso")

        mudancas = []
        if descricao != _texto(v):
            mudancas.append(f"descrição '{_texto(v)}' -> '{descricao}'")
            v.pessoa, v.descricao = None, descricao
        if abs(valor - v.valor) > 0.005:
            mudancas.append(f"valor R$ {v.valor:.2f} -> R$ {valor:.2f}")
            v.valor = valor
        if data != v.data:
            mudancas.append(f"data {v.data:%d/%m/%Y} -> {data:%d/%m/%Y}")
            v.data = data
        if conta != v.conta:
            mudancas.append(f"conta {v.conta} -> {conta}")
            v.conta = conta

        if not quitado_antigo:
            if pago >= valor - 0.005:
                if v.status != 'Pago':
                    ultimo = Transaction.query.filter_by(vale_id=v.id, type='entrada').order_by(
                        Transaction.date.desc(), Transaction.id.desc()).first()
                    v.status, v.data_pagamento, v.conta_pagamento = 'Pago', ultimo.date, ultimo.origin[:20]
                    mudancas.append('ficou quitado')
            elif v.status == 'Pago':
                v.status, v.data_pagamento, v.conta_pagamento = 'Aberto', None, None
                mudancas.append('voltou para Aberto')

        if saida:
            saida.description = f"Vale #{v.id} - {_texto(v)}"[:200]
            saida.amount = -v.valor if saida.amount < 0 else v.valor
            saida.date, saida.origin = v.data, v.conta
        elif mudancas:
            mudancas.append('saída do vale não encontrada no caixa - o caixa NÃO foi alterado')
        db.session.commit()
        if mudancas:
            self.audit.log_action(_usuario(), 'UPDATE', 'Vale',
                                  f"Editou o vale #{v.id}: " + ' | '.join(mudancas) + ' [confirmado com senha]')
        return {**_serialize(v, pago), 'saida_no_caixa': saida is not None}

    def apagar(self, id, dados):
        if not db.session.get(Vale, id):
            return None
        _conferir_senha(dados.get('senha'), f"apagar o vale #{id}")
        v = db.session.query(Vale).filter_by(id=id).with_for_update().first()
        _ligar_baixa_antiga(v)
        saida = _saida(v)
        db.session.flush()
        linhas = Transaction.query.filter_by(vale_id=v.id).order_by(Transaction.id).all()
        retrato = (f"Apagou o vale #{v.id}: {_texto(v)} | R$ {v.valor:.2f} | {v.data:%d/%m/%Y} | "
                   f"conta: {v.conta} | {v.status}. Saiu do caixa: "
                   + ('; '.join(f"#{t.id} {t.type} R$ {abs(t.amount):.2f} {t.origin} {t.date:%d/%m/%Y} '{t.description}'"
                                for t in linhas) or 'nada')
                   + ('' if saida else ' | saída do vale não encontrada no caixa'))
        for t in linhas:
            db.session.delete(t)
        db.session.delete(v)
        db.session.flush()
        for grupo in {t.grupo_id for t in linhas if t.grupo_id}:
            recalcular_total(grupo)  # desconto de comissao que saiu: o total do borderô acompanha
        db.session.commit()
        self.audit.log_action(_usuario(), 'DELETE', 'Vale', retrato + ' [confirmado com senha]')
        return {'linhas_apagadas': len(linhas), 'saida_no_caixa': saida is not None}
