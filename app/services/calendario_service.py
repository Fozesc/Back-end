from datetime import date, datetime, timedelta
from sqlalchemy import func, case
from app import db
from app.models.domain import Check, Client, Evento, Operation, Transaction
from app.services.audit_service import AuditService

TIPOS = ('evento', 'nota', 'lembrete')
PREVISOES = ('entrada', 'saida')
HORIZONTES = (30, 60, 90)
MAX_DIAS = 62            # a tela pede um mes (grade de ate 6 semanas)
MAX_CHEQUES = 2000
ABERTOS = ('Aguardando', 'Atrasado', 'Juridico', 'Devolvido')
COM_SINAL = case((Transaction.type == 'entrada', func.abs(Transaction.amount)), else_=-func.abs(Transaction.amount))


def _usuario():
    try:
        from flask_jwt_extended import get_jwt
        return get_jwt().get('name', 'Sistema')
    except Exception:
        return 'Sistema'


def _dia(texto, campo='Data'):
    try:
        return datetime.strptime(str(texto)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValueError(f'{campo} inválida (use AAAA-MM-DD)')


def _serialize_evento(e):
    return {'id': e.id, 'data': e.data.isoformat(), 'titulo': e.titulo, 'descricao': e.descricao,
            'tipo': e.tipo, 'cliente_id': e.client_id, 'cliente': e.client.name if e.client else None,
            'valor': e.valor, 'previsao': e.previsao, 'concluido': e.concluido, 'autor': e.autor}


class CalendarioService:
    def __init__(self):
        self.audit = AuditService()

    # ------------------------------------------------------------------ previsao
    def _movimento_previsto(self, d1, d2):
        """Por dia, o que esta previsto para entrar/sair entre d1 e d2 (inclusive): cheques
        em dia a receber e eventos com valor nao concluidos. Lancamento do caixa com data
        futura nao entra aqui: ja esta no saldo. Tudo somado no banco."""
        por_dia = {}

        def somar(dia, entra=0.0, sai=0.0):
            d = por_dia.setdefault(dia.isoformat(), {'entra': 0.0, 'sai': 0.0})
            d['entra'] += float(entra or 0)
            d['sai'] += float(sai or 0)

        for dia, total in db.session.query(Check.due_date, func.sum(Check.amount)).filter(
                Check.status == 'Aguardando', Check.fora_do_calculo.is_(False),
                Check.due_date >= d1, Check.due_date <= d2).group_by(Check.due_date):
            somar(dia, entra=total)
        for dia, prev, total in db.session.query(Evento.data, Evento.previsao, func.sum(Evento.valor)).filter(
                Evento.concluido.is_(False), Evento.valor.isnot(None), Evento.previsao.isnot(None),
                Evento.data >= d1, Evento.data <= d2).group_by(Evento.data, Evento.previsao):
            somar(dia, **({'entra': total} if prev == 'entrada' else {'sai': total}))
        return por_dia

    def previsao(self):
        """Metodo do sistema financeiro: saldo do caixa + o que vai entrar ate a data - o
        que vai sair. O saldo e' o mesmo do Fluxo de Caixa (tudo que foi lancado, qualquer
        data). Vencido fica a parte (nao entra)."""
        hoje = date.today()
        saldo = float(db.session.query(func.coalesce(func.sum(COM_SINAL), 0.0)).scalar() or 0)
        # de HOJE em diante: cheque que vence hoje e evento de hoje ainda nao aconteceram
        por_dia = self._movimento_previsto(hoje, hoje + timedelta(days=max(HORIZONTES)))
        vencido, qtd_vencidos = db.session.query(func.coalesce(func.sum(Check.amount), 0.0), func.count(Check.id)).filter(
            Check.status.in_(('Aguardando', 'Atrasado')), Check.fora_do_calculo.is_(False), Check.due_date < hoje).one()
        horizontes = []
        for dias in HORIZONTES:
            limite = (hoje + timedelta(days=dias)).isoformat()
            entra = sum(v['entra'] for k, v in por_dia.items() if k <= limite)
            sai = sum(v['sai'] for k, v in por_dia.items() if k <= limite)
            horizontes.append({'dias': dias, 'a_receber': round(entra, 2), 'a_pagar': round(sai, 2),
                               'saldo_previsto': round(saldo + entra - sai, 2)})
        return {'saldo': round(saldo, 2), 'vencido': round(float(vencido or 0), 2),
                'qtd_vencidos': qtd_vencidos, 'horizontes': horizontes}

    # ------------------------------------------------------------------ mes
    def periodo(self, inicio, fim, cliente_id=None):
        d1, d2 = _dia(inicio, 'Início'), _dia(fim, 'Fim')
        if d1 > d2:
            raise ValueError('O início não pode ser depois do fim')
        if (d2 - d1).days > MAX_DIAS:
            raise ValueError(f'Período grande demais (máximo {MAX_DIAS} dias)')
        hoje = date.today()

        q = db.session.query(
            Check.id, Check.number, Check.issuer_name, Check.amount, Check.due_date, Check.status,
            Check.operation_id, Client.id.label('cliente_id'), Client.name.label('cliente'),
        ).join(Operation, Check.operation_id == Operation.id).join(Client, Operation.client_id == Client.id).filter(
            Check.due_date >= d1, Check.due_date <= d2, Check.status.in_(ABERTOS), Check.fora_do_calculo.is_(False))
        ev = Evento.query.filter(Evento.data >= d1, Evento.data <= d2)
        if cliente_id:
            q = q.filter(Client.id == cliente_id)
            ev = ev.filter(Evento.client_id == cliente_id)
        cheques = q.order_by(Check.due_date, Check.id).limit(MAX_CHEQUES + 1).all()

        # saldo previsto dia a dia (so do caixa inteiro, nao depende do filtro de cliente)
        prev = self.previsao()
        por_dia = self._movimento_previsto(hoje, d2) if d2 >= hoje else {}
        saldo = prev['saldo']
        saldo_dia = {}
        d = hoje
        while d <= d2:
            m = por_dia.get(d.isoformat())
            if m:
                saldo += m['entra'] - m['sai']
            if d >= d1:
                saldo_dia[d.isoformat()] = round(saldo, 2)
            d += timedelta(days=1)

        return {
            'inicio': d1.isoformat(), 'fim': d2.isoformat(), 'hoje': hoje.isoformat(),
            'cheques': [{
                'id': c.id, 'numero': c.number, 'emitente': c.issuer_name, 'valor': round(float(c.amount or 0), 2),
                'vencimento': c.due_date.isoformat(), 'operation_id': c.operation_id,
                'cliente_id': c.cliente_id, 'cliente': c.cliente,
                'status': 'Atrasado' if c.status == 'Aguardando' and c.due_date < hoje else c.status,
            } for c in cheques[:MAX_CHEQUES]],
            'cheques_cortados': len(cheques) > MAX_CHEQUES,
            'eventos': [_serialize_evento(e) for e in ev.order_by(Evento.data, Evento.id).limit(1000)],
            'saldo_previsto': saldo_dia,
            'previsao': prev,
        }

    # ------------------------------------------------------------------ eventos
    def _validar(self, dados, evento=None):
        titulo = ' '.join(str(dados.get('titulo') or '').split())[:120]
        if not titulo:
            raise ValueError('Informe o título')
        tipo = dados.get('tipo') or 'evento'
        if tipo not in TIPOS:
            raise ValueError('Tipo inválido (evento, nota ou lembrete)')
        descricao = str(dados.get('descricao') or '').strip()[:2000] or None
        cliente_id = dados.get('cliente_id') or None
        if cliente_id is not None:
            try:
                cliente_id = int(cliente_id)
            except (TypeError, ValueError):
                raise ValueError('Cliente inválido')
            if not db.session.get(Client, cliente_id):
                raise ValueError('Cliente não encontrado')
        valor, previsao = dados.get('valor'), dados.get('previsao') or None
        if valor in (None, ''):
            valor, previsao = None, None
        else:
            try:
                valor = round(float(valor), 2)
            except (TypeError, ValueError):
                raise ValueError('Valor inválido')
            if not 0 < valor <= 100_000_000:
                raise ValueError('O valor tem que ser maior que zero')
            if previsao not in PREVISOES:
                raise ValueError('Diga se o valor entra ou sai do caixa')
        return {'data': _dia(dados.get('data')), 'titulo': titulo, 'tipo': tipo, 'descricao': descricao,
                'client_id': cliente_id, 'valor': valor, 'previsao': previsao,
                'concluido': bool(dados.get('concluido'))}

    def criar(self, dados):
        campos = self._validar(dados)
        e = Evento(**campos, autor=_usuario()[:100])
        db.session.add(e)
        db.session.commit()
        self.audit.log_action(e.autor, 'CREATE', 'Calendario',
                              f"{e.tipo.capitalize()} #{e.id} em {e.data:%d/%m/%Y}: {e.titulo}"
                              + (f" | {e.previsao} R$ {e.valor:.2f}" if e.valor else ''))
        return _serialize_evento(e)

    def editar(self, id, dados):
        e = db.session.get(Evento, id)
        if not e:
            return None
        antes = f"{e.data:%d/%m/%Y} {e.titulo}" + (f" R$ {e.valor:.2f}" if e.valor else '')
        for campo, valor in self._validar(dados).items():
            setattr(e, campo, valor)
        db.session.commit()
        self.audit.log_action(_usuario(), 'UPDATE', 'Calendario', f"Editou o evento #{e.id} (antes: {antes})")
        return _serialize_evento(e)

    def apagar(self, id):
        e = db.session.get(Evento, id)
        if not e:
            return False
        retrato = f"Apagou o {e.tipo} #{e.id} de {e.data:%d/%m/%Y}: {e.titulo} | {e.descricao or ''}"[:1000]
        db.session.delete(e)
        db.session.commit()
        self.audit.log_action(_usuario(), 'DELETE', 'Calendario', retrato)
        return True
