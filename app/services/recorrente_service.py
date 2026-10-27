import calendar
import math
from datetime import date, datetime, timedelta

from sqlalchemy import func

from app import db
from app.models.domain import Recorrente, Transaction
from app.services.audit_service import AuditService
from app.services.check_service import CheckService

FREQUENCIAS = ('mensal', 'semanal', 'anual')
TIPOS = ('saida', 'entrada')
CONTAS = ('Dinheiro', 'BB', 'Caixa')


def _usuario():
    try:
        from flask_jwt_extended import get_jwt
        return get_jwt().get('name', 'Sistema')
    except Exception:
        return 'Sistema'


def proximo_depois(atual, frequencia, dia):
    """Proximo vencimento. `dia` e' o do cadastro: o do dia 31 cai no 28 em fevereiro e
    volta ao 31 em marco, em vez de ficar no 28 para sempre."""
    if frequencia == 'semanal':
        return atual + timedelta(days=7)
    i = atual.month - 1 + (12 if frequencia == 'anual' else 1)
    ano, mes = atual.year + i // 12, i % 12 + 1
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


def _avancar(r):
    """Passa para a proxima ocorrencia. Adiamento e pagamento parcial valiam so para a atual."""
    r.proximo = proximo_depois(r.proximo, r.frequencia, r.dia)
    r.adiado_ate, r.valor_atual, r.pago_atual = None, None, 0.0


def _lancamento(r, valor, data, conta, como):
    return Transaction(date=data, description=f"{r.descricao} ({como})"[:200], type=r.tipo,
                       amount=valor if r.tipo == 'entrada' else -valor, origin=conta,
                       category=r.categoria, recorrente_id=r.id)


def _base(r):
    """O valor da conta deste periodo (o do cadastro, ou o informado no 1o pagamento parcial)."""
    return r.valor_atual if r.valor_atual is not None else r.valor


def _valor(bruto):
    try:
        v = round(abs(float(bruto)), 2)
    except (TypeError, ValueError):
        raise ValueError('Valor inválido')
    if not math.isfinite(v) or v <= 0:
        raise ValueError('O valor tem que ser maior que zero')
    return v


def _data(bruto, campo):
    try:
        return datetime.strptime(str(bruto)[:10], '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise ValueError(f'{campo} inválida')


def _conta(bruto):
    if bruto not in CONTAS:
        raise ValueError('Conta inválida (use Dinheiro, BB ou Caixa)')
    return bruto


def _serialize(r, ultimo=None):
    return {
        'id': r.id, 'descricao': r.descricao, 'valor': r.valor, 'tipo': r.tipo, 'conta': r.conta,
        'valor_atual': round(_base(r), 2), 'pago_atual': round(r.pago_atual or 0, 2),
        'falta': round(_base(r) - (r.pago_atual or 0), 2),
        'categoria': r.categoria, 'frequencia': r.frequencia, 'proximo': r.proximo.isoformat(),
        'adiado_ate': r.adiado_ate.isoformat() if r.adiado_ate else None,
        'vencimento': r.vencimento.isoformat(), 'automatico': r.automatico, 'ativo': r.ativo,
        'obs': r.obs or '',
        'ultimo': {'data': ultimo[0].isoformat(), 'valor': round(abs(ultimo[1]), 2)} if ultimo else None,
    }


class RecorrenteService:
    def __init__(self):
        self.audit = AuditService()

    def listar(self):
        itens = Recorrente.query.order_by(func.coalesce(Recorrente.adiado_ate, Recorrente.proximo),
                                          Recorrente.id).all()
        # o ultimo dia em que cada uma lancou, somando as partes daquele dia (DISTINCT ON do
        # Postgres: uma linha por recorrente)
        por_dia = (db.session.query(Transaction.recorrente_id.label('rid'), Transaction.date.label('dia'),
                                    func.sum(Transaction.amount).label('valor'))
                   .filter(Transaction.recorrente_id.isnot(None))
                   .group_by(Transaction.recorrente_id, Transaction.date).subquery())
        ultimos = {rid: (d, v) for rid, d, v in db.session.query(por_dia.c.rid, por_dia.c.dia, por_dia.c.valor)
                   .distinct(por_dia.c.rid).order_by(por_dia.c.rid, por_dia.c.dia.desc())}
        return [_serialize(r, ultimos.get(r.id)) for r in itens]

    def _aplicar(self, r, dados):
        antes = (r.proximo, r.automatico, r.ativo)
        if 'descricao' in dados:
            r.descricao = ' '.join(str(dados.get('descricao') or '').split())[:200]
            if not r.descricao:
                raise ValueError('Informe a descrição')
        if 'valor' in dados:
            r.valor = _valor(dados.get('valor'))
        if 'tipo' in dados:
            if dados['tipo'] not in TIPOS:
                raise ValueError('Tipo inválido (use saida ou entrada)')
            r.tipo = dados['tipo']
        if 'conta' in dados:
            r.conta = _conta(dados['conta']) if dados.get('conta') else None
        if 'categoria' in dados:
            r.categoria = ' '.join(str(dados.get('categoria') or '').split())[:50] or 'Despesas fixas'
        if 'frequencia' in dados:
            if dados['frequencia'] not in FREQUENCIAS:
                raise ValueError('Frequência inválida (use mensal, semanal ou anual)')
            r.frequencia = dados['frequencia']
        if 'proximo' in dados:
            proximo = _data(dados.get('proximo'), 'Data do vencimento')
            # o formulario sempre manda a data: so conta se mudou de verdade
            if proximo != r.proximo:
                r.proximo, r.dia, r.adiado_ate = proximo, proximo.day, None
        if 'automatico' in dados:
            r.automatico = bool(dados['automatico'])
        if 'ativo' in dados:
            r.ativo = bool(dados['ativo'])
        if 'obs' in dados:
            r.obs = str(dados.get('obs') or '').strip()[:1000] or None
        if r.automatico and not r.conta:
            raise ValueError('Escolha a conta de onde sai o débito automático')
        # debito automatico com vencimento no passado lancaria todos os periodos atrasados
        # no caixa de uma vez (ex.: cadastrar em outubro um aluguel "desde janeiro")
        if (r.automatico and r.ativo and r.vencimento < date.today()
                and (r.proximo, r.automatico, r.ativo) != antes):
            raise ValueError('No débito automático o próximo vencimento precisa ser hoje ou depois: '
                             'o que já venceu entraria no caixa de uma vez. Lance os atrasados à mão '
                             'ou mude a data.')

    def criar(self, dados):
        if not dados.get('proximo'):
            raise ValueError('Informe o próximo vencimento')
        r = Recorrente()
        # descricao e valor sempre passam pelo _aplicar, para a validacao valer tambem quando faltam
        self._aplicar(r, {'tipo': 'saida', 'categoria': 'Despesas fixas', 'frequencia': 'mensal',
                          'automatico': True, 'ativo': True, **dados,
                          'descricao': dados.get('descricao'), 'valor': dados.get('valor')})
        db.session.add(r)
        db.session.commit()
        self.audit.log_action(_usuario(), 'CREATE', 'Recorrente',
                              f"Recorrente #{r.id}: {r.descricao} | R$ {r.valor:.2f} {r.frequencia} "
                              f"({f'débito automático, {r.conta}' if r.automatico else 'manual'})")
        return _serialize(r)

    def editar(self, id, dados):
        r = db.session.get(Recorrente, id)
        if not r:
            return None
        self._aplicar(r, dados)
        db.session.commit()
        self.audit.log_action(_usuario(), 'UPDATE', 'Recorrente',
                              f"Recorrente #{r.id}: {r.descricao} | R$ {r.valor:.2f} {r.frequencia} "
                              f"({'débito automático' if r.automatico else 'manual'}, "
                              f"{'ativa' if r.ativo else 'pausada'}, próximo {r.vencimento:%d/%m/%Y})")
        return _serialize(r)

    def apagar(self, id):
        r = db.session.get(Recorrente, id)
        if not r:
            return False
        descricao = r.descricao
        db.session.delete(r)
        db.session.commit()
        self.audit.log_action(_usuario(), 'DELETE', 'Recorrente',
                              f"Recorrente #{id}: {descricao} (os lançamentos que ela fez continuam no caixa)")
        return True

    def gerar(self):
        """Debito automatico: lanca no caixa as ocorrencias vencidas ate hoje, cada uma na
        data do vencimento (sistema fechado no fim de semana lanca o sabado na segunda)."""
        hoje = date.today()
        # trava as linhas: duas telas abrindo juntas lancariam a mesma conta duas vezes;
        # a segunda espera a primeira e ja encontra a data avancada
        itens = (Recorrente.query
                 .filter(Recorrente.ativo.is_(True), Recorrente.automatico.is_(True),
                         func.coalesce(Recorrente.adiado_ate, Recorrente.proximo) <= hoje)
                 .with_for_update().all())
        novos = []
        for r in itens:
            while r.vencimento <= hoje:
                novos.append(_lancamento(r, r.valor, r.vencimento, r.conta, 'débito automático'))
                _avancar(r)
        if not novos:
            db.session.rollback()  # solta a trava
            return []
        db.session.add_all(novos)
        db.session.commit()
        self.audit.log_action('Sistema', 'CREATE', 'Recorrente',
                              'Débito automático: ' + '; '.join(
                                  f"{t.description} R$ {abs(t.amount):.2f} em {t.date:%d/%m/%Y} ({t.origin})"
                                  for t in novos))
        return [{'descricao': t.description, 'valor': abs(t.amount), 'data': t.date.isoformat()} for t in novos]

    def lancar(self, id, dados):
        """Lanca a ocorrencia atual a mao, como o Receber dos titulos: uma conta ou varias
        partes (conta, forma, valor). `valor_conta` e' o valor da conta deste periodo (a luz
        nunca vem igual). Pagando menos que falta, fica o resto para depois e o vencimento so
        anda quando quitar. Aceita tambem so {valor, conta}."""
        r = Recorrente.query.filter_by(id=id).with_for_update().first()
        if not r:
            return None
        if not r.ativo:
            raise ValueError('Esta despesa está pausada: retome antes de lançar')
        base = _valor(dados['valor_conta']) if dados.get('valor_conta') else _base(r)
        falta = round(base - (r.pago_atual or 0), 2)
        if falta <= 0:
            raise ValueError(f'O valor da conta (R$ {base:.2f}) não pode ser menor que o já pago (R$ {r.pago_atual:.2f})')
        partes = dados.get('partes')
        if not partes:
            partes = [{'conta': dados.get('conta') or r.conta, 'forma': dados.get('forma') or '',
                       'valor': dados.get('valor', falta)}]
        try:
            soma = round(sum(abs(float(p.get('valor') or 0)) for p in partes), 2)
        except (TypeError, ValueError, AttributeError):
            raise ValueError('Valor inválido')
        partes = [(conta, forma, abs(v)) for conta, forma, v in CheckService()._validar_partes(
            [{**p, 'valor': abs(float(p.get('valor') or 0))} for p in partes], soma, rotulo='pagamento')]
        if soma > falta + 0.005:
            raise ValueError(f'O pagamento (R$ {soma:.2f}) passa do que falta desta conta (R$ {falta:.2f})')
        data = _data(dados.get('data') or date.today().isoformat(), 'Data')
        quita = soma >= falta - 0.005
        vencia = r.vencimento
        como = 'recorrente' if quita and not r.pago_atual else ('quitação' if quita else 'parcial')
        novas = []
        for n, (conta, forma, valor) in enumerate(partes, 1):
            rotulo = ([f"Parte {n}/{len(partes)}"] if len(partes) > 1 else []) + ([forma] if forma and forma != conta else [])
            novas.append(_lancamento(r, valor, data, conta, ' · '.join([como, *rotulo])))
        db.session.add_all(novas)
        db.session.flush()
        if len(novas) > 1:
            for t in novas:
                t.grupo_id = novas[0].id   # o caixa mostra as partes juntas
        r.valor_atual, r.pago_atual = base, round((r.pago_atual or 0) + soma, 2)
        if quita:
            _avancar(r)
        db.session.commit()
        resto = '' if quita else f" | falta R$ {base - r.pago_atual:.2f}"
        self.audit.log_action(_usuario(), 'CREATE', 'Recorrente',
                              f"Lançou {r.descricao} (vencimento {vencia:%d/%m/%Y}, conta de R$ {base:.2f}) | "
                              f"R$ {soma:.2f} em {data:%d/%m/%Y} ("
                              + ', '.join(f"{c} R$ {v:.2f}" for c, _, v in partes) + f"){resto}"
                              + (f" | próximo {r.proximo:%d/%m/%Y}" if quita else ''))
        return _serialize(r, (novas[-1].date, soma))

    def adiar(self, id, ate):
        """Adia so a ocorrencia atual. `ate` vazio desfaz o adiamento."""
        r = db.session.get(Recorrente, id)
        if not r:
            return None
        if ate:
            ate = _data(ate, 'Nova data')
            if ate <= r.proximo:
                raise ValueError('A nova data precisa ser depois do vencimento original')
            r.adiado_ate = ate
        else:
            r.adiado_ate = None
        db.session.commit()
        self.audit.log_action(_usuario(), 'UPDATE', 'Recorrente',
                              f"{r.descricao}: " + (f"adiada de {r.proximo:%d/%m/%Y} para {r.adiado_ate:%d/%m/%Y}"
                                                    if r.adiado_ate else f"adiamento desfeito (vence {r.proximo:%d/%m/%Y})"))
        return _serialize(r)
