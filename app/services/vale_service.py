from datetime import date, datetime
from sqlalchemy import func
from app import db
from app.models.domain import Vale, Transaction
from app.services.audit_service import AuditService
from app.services.check_service import CONTAS_CAIXA

MAX_POR_PAGINA = 100
VALOR_MAXIMO = 10_000_000


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


def _serialize(v):
    return {
        'id': v.id,
        'pessoa': v.pessoa,
        'descricao': v.descricao,
        'valor': v.valor,
        'data': v.data.isoformat(),
        'conta': v.conta,
        'status': v.status,
        'data_pagamento': v.data_pagamento.isoformat() if v.data_pagamento else None,
        'conta_pagamento': v.conta_pagamento,
    }


class ValeService:
    def __init__(self):
        self.audit = AuditService()

    def listar(self, page, per_page, status=None, busca=None):
        per_page = max(1, min(per_page, MAX_POR_PAGINA))
        q = Vale.query
        if status in ('Aberto', 'Pago'):
            q = q.filter(Vale.status == status)
        if busca:
            q = q.filter(Vale.pessoa.ilike(f"%{busca}%"))
        pag = q.order_by(Vale.data.desc(), Vale.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
        em_aberto, qtd_aberto = db.session.query(
            func.coalesce(func.sum(Vale.valor), 0.0), func.count(Vale.id)
        ).filter(Vale.status == 'Aberto').one()
        return {
            'items': [_serialize(v) for v in pag.items],
            'total': pag.total,
            'pages': pag.pages,
            'current_page': page,
            'em_aberto': round(float(em_aberto), 2),
            'qtd_aberto': qtd_aberto,
        }

    def criar(self, dados):
        pessoa = str(dados.get('pessoa') or '').strip()[:100]
        if not pessoa:
            raise ValueError('Informe para quem é o vale')
        try:
            valor = round(float(dados.get('valor')), 2)
        except (TypeError, ValueError):
            raise ValueError('Valor inválido')
        if not 0 < valor <= VALOR_MAXIMO:
            raise ValueError('Valor tem que ser maior que zero')
        descricao = str(dados.get('descricao') or '').strip()[:200] or None
        v = Vale(pessoa=pessoa, descricao=descricao, valor=valor,
                 data=_data(dados.get('data')), conta=_conta(dados.get('conta') or 'Dinheiro'))
        db.session.add(v)
        db.session.add(Transaction(date=v.data, description=f"Vale - {pessoa}"[:200], amount=valor,
                                   type='saida', origin=v.conta, category='Vale'))
        db.session.commit()
        self.audit.log_action(_usuario(), 'CREATE', 'Vale',
                              f"Vale #{v.id} para {pessoa} | R$ {valor} | conta: {v.conta}")
        return _serialize(v)

    def baixar(self, id, dados):
        v = db.session.query(Vale).filter_by(id=id).with_for_update().first()
        if not v:
            return None
        if v.status == 'Pago':
            db.session.rollback()
            raise ValueError('Este vale já foi pago')
        v.status = 'Pago'
        v.data_pagamento = _data(dados.get('data'))
        v.conta_pagamento = _conta(dados.get('conta') or v.conta)
        db.session.add(Transaction(date=v.data_pagamento, description=f"Pagamento de vale - {v.pessoa}"[:200],
                                   amount=v.valor, type='entrada', origin=v.conta_pagamento, category='Vale'))
        db.session.commit()
        self.audit.log_action(_usuario(), 'BAIXA', 'Vale',
                              f"Baixa do vale #{v.id} de {v.pessoa} | R$ {v.valor} | conta: {v.conta_pagamento}")
        return _serialize(v)
