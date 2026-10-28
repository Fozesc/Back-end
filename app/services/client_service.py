from app.models.domain import Client, ClientNote, Evento, Operation, Check, CheckExtension, User
from app import db
from app.services.audit_service import AuditService
from sqlalchemy import or_, and_, func, case, cast, exists, Integer
from werkzeug.security import check_password_hash
from datetime import datetime, date, timedelta

MAX_TEXTO_NOTA = 4000
MAX_NOTAS_POR_PAGINA = 50
MAX_POR_PAGINA = 100

# Reputacao: cada titulo que ja venceu ganha pontos pelo que aconteceu com ele e a nota e' a
# media pesada pelo valor de face (um cheque grande pesa mais que um pequeno). Titulo a vencer
# nao conta. Pago ate TOLERANCIA_DIAS depois do vencimento combinado (o original, antes de
# prorrogar) ainda e' "em dia", e so vira "vencido" depois disso: e' o prazo de compensar.
TOLERANCIA_DIAS = 3
PONTOS = {'em_dia': 100, 'prorrogado': 70, 'atrasado': 50, 'devolvido_pago': 40,
          'vencido': 20, 'devolvido': 0, 'juridico': 0}
MIN_AVALIADOS = 3

# endereco do cadastro: campo -> tamanho da coluna (UF vai em maiuscula). Antes so `address`
# era gravado na criacao e nada na edicao: bairro, cidade, UF e CEP digitados se perdiam.
ENDERECO = {'email': 100, 'address': 200, 'neighborhood': 100, 'city': 100, 'state': 2, 'zip_code': 20}


def _texto(valor, tamanho):
    texto = ' '.join(str(valor or '').split())[:tamanho]
    return texto or None

class ClientService:
    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
            from flask_jwt_extended import get_jwt
            return get_jwt().get('name', 'Sistema')
        except:
            return 'Sistema'

    def _usuario_logado(self):
        """O User de verdade - precisa dele para conferir a senha na mesclagem."""
        try:
            from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
            verify_jwt_in_request(optional=True)
            ident = get_jwt_identity()
            return db.session.get(User, int(ident)) if str(ident).isdigit() else None
        except Exception:
            return None

    def get_by_id(self, id):
        return Client.query.get(id)

    def create(self, data):
        client = Client(
            name=data.get('name'),
            document=data.get('document'),
            phone=data.get('phone'),
            **{campo: _texto(data.get(campo), tam) for campo, tam in ENDERECO.items()},
            credit_limit=data.get('credit_limit', 0.0),
            standard_rate=data.get('standard_rate', 4.0),
            notes=data.get('notes')
        )
        if client.state:
            client.state = client.state.upper()
        db.session.add(client)
        db.session.commit()

        self.audit.log_action(
            self._get_current_user(), 
            'CREATE', 
            'Cliente', 
            f"Novo cliente cadastrado: {client.name} | CPF/CNPJ: {client.document}"
        )
        return client

    def update(self, id, data):
        client = self.get_by_id(id)
        if not client: return None
        
        old_name = client.name
        changes = []

        if 'name' in data and data['name'] != client.name:
            changes.append(f"Nome: {client.name} -> {data['name']}")
            client.name = data['name']
        if 'document' in data and data['document'] != client.document:
            changes.append(f"Doc: {client.document} -> {data['document']}")
            client.document = data['document']
        if 'phone' in data: client.phone = data['phone']
        if 'credit_limit' in data: 
            changes.append(f"Limite: {client.credit_limit} -> {data['credit_limit']}")
            client.credit_limit = data['credit_limit']
        if 'standard_rate' in data: client.standard_rate = data['standard_rate']
        if 'notes' in data: client.notes = data['notes']
        for campo, tam in ENDERECO.items():
            if campo in data:
                setattr(client, campo, _texto(data[campo], tam))
        if client.state:
            client.state = client.state.upper()

        db.session.commit()

        if changes:
            self.audit.log_action(
                self._get_current_user(),
                'UPDATE',
                'Cliente',
                f"Cliente {old_name} alterado. Detalhes: {', '.join(changes)}"
            )
        return client

    def delete(self, id):
        client = self.get_by_id(id)
        if not client: return False
        
        if client.operations: 
            raise ValueError(f"Não é possível excluir. O cliente possui {len(client.operations)} operações registradas.")

        name = client.name
        db.session.delete(client)
        db.session.commit()

        self.audit.log_action(
            self._get_current_user(),
            'DELETE',
            'Cliente',
            f"Cliente excluído permanentemente: {name}"
        )
        return True

    def merge(self, origem_id, destino_id, senha):
        """
        Junta dois cadastros que sao a MESMA pessoa: todos os borderos (e com eles os
        cheques) do ORIGEM passam para o DESTINO, e o ORIGEM e apagado.

        Existe por causa da importacao da planilha: o mesmo cliente aparece como
        'Joao dos S. Ribeiro' na planilha e 'Joao dos Santos Ribeiro' no cadastro.
        Em vez de adivinhar antes de importar, voce junta depois, olhando os dados.

        Exige a SENHA de quem esta logado (mesmo 2o fator da edicao de cheque): apagar
        cadastro e mover historico nao pode acontecer por clique errado.
        """
        origem = self.get_by_id(origem_id)
        destino = self.get_by_id(destino_id)
        if not origem or not destino:
            raise ValueError("Cliente não encontrado")
        if origem.id == destino.id:
            raise ValueError("Escolha dois clientes diferentes")

        usuario = self._usuario_logado()
        if not usuario or not check_password_hash(usuario.password_hash, str(senha or '')):
            self.audit.log_action(self._get_current_user(), 'NEGADO', 'Cliente',
                                  f"Senha incorreta ao tentar juntar '{origem.name}' (#{origem.id}) "
                                  f"em '{destino.name}' (#{destino.id})")
            raise PermissionError("Senha incorreta - nada foi alterado")

        # Contagem ANTES de mover, para o registro da auditoria.
        qtd_borderos = db.session.query(func.count(Operation.id)) \
                                 .filter(Operation.client_id == origem.id).scalar() or 0
        qtd_cheques = db.session.query(func.count(Check.id)) \
                                .join(Operation, Check.operation_id == Operation.id) \
                                .filter(Operation.client_id == origem.id).scalar() or 0

        # Guarda o cadastro apagado por inteiro: e o que permite refazer a mao se voce
        # juntar as pessoas erradas (o merge em si nao tem "desfazer").
        nome_origem, nome_destino = origem.name, destino.name
        retrato = (f"nome='{origem.name}' doc='{origem.document or ''}' "
                   f"tel='{origem.phone or ''}' limite={origem.credit_limit or 0} "
                   f"taxa={origem.standard_rate or 0} obs='{(origem.notes or '')[:200]}'")

        try:
            # UPDATE unico no banco. Depois da importacao um cliente pode ter milhares
            # de borderos - carregar tudo na memoria para mudar um campo nao serve.
            Operation.query.filter_by(client_id=origem.id).update(
                {'client_id': destino.id, 'client_name_snapshot': destino.name},
                synchronize_session=False)
            qtd_notas = ClientNote.query.filter_by(client_id=origem.id).update(
                {'client_id': destino.id}, synchronize_session=False)
            Evento.query.filter_by(client_id=origem.id).update({'client_id': destino.id}, synchronize_session=False)
            db.session.delete(origem)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        self.audit.log_action(
            self._get_current_user(), 'MERGE', 'Cliente',
            f"Juntou o cliente '{nome_origem}' (#{origem_id}) em '{nome_destino}' (#{destino_id}): "
            f"{qtd_borderos} borderô(s), {qtd_cheques} cheque(s) e {qtd_notas} nota(s) foram transferidos. "
            f"Cadastro apagado: [{retrato}] [confirmado com senha]")

        return {'borderos': qtd_borderos, 'cheques': qtd_cheques,
                'apagado': nome_origem, 'mantido': nome_destino, 'destino_id': destino_id}

    @staticmethod
    def _texto_nota(dados):
        texto = (dados or {}).get('texto')
        texto = texto.strip() if isinstance(texto, str) else ''
        if not texto:
            raise ValueError('A nota não pode ficar vazia')
        if len(texto) > MAX_TEXTO_NOTA:
            raise ValueError(f'A nota pode ter no máximo {MAX_TEXTO_NOTA} caracteres')
        return texto

    @staticmethod
    def _serialize_nota(n):
        return {'id': n.id, 'autor': n.autor, 'texto': n.texto,
                'criado_em': n.criado_em.isoformat(timespec='minutes'),
                'editado_em': n.editado_em.isoformat(timespec='minutes') if n.editado_em else None}

    def _nota(self, client_id, nota_id):
        return ClientNote.query.filter_by(id=nota_id, client_id=client_id).first()

    def listar_notas(self, client_id, page, per_page):
        if not db.session.get(Client, client_id):
            return None
        per_page = max(1, min(per_page, MAX_NOTAS_POR_PAGINA))
        pag = ClientNote.query.filter_by(client_id=client_id).order_by(
            ClientNote.criado_em.desc(), ClientNote.id.desc()
        ).paginate(page=page, per_page=per_page, error_out=False)
        return {'items': [self._serialize_nota(n) for n in pag.items],
                'total': pag.total, 'pages': pag.pages, 'current_page': page}

    def criar_nota(self, client_id, dados):
        cliente = db.session.get(Client, client_id)
        if not cliente:
            return None
        nota = ClientNote(client_id=client_id, autor=self._get_current_user()[:100], texto=self._texto_nota(dados))
        db.session.add(nota)
        db.session.commit()
        self.audit.log_action(nota.autor, 'CREATE', 'Nota', f"Nota #{nota.id} no cliente '{cliente.name}' (#{client_id})")
        return self._serialize_nota(nota)

    def editar_nota(self, client_id, nota_id, dados):
        nota = self._nota(client_id, nota_id)
        if not nota:
            return None
        texto = self._texto_nota(dados)
        if texto != nota.texto:
            antigo = nota.texto
            nota.texto, nota.editado_em = texto, datetime.now()
            db.session.commit()
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Nota',
                                  f"Editou a nota #{nota.id} do cliente #{client_id} (autor: {nota.autor}). "
                                  f"Antes: '{antigo[:500]}'")
        return self._serialize_nota(nota)

    def apagar_nota(self, client_id, nota_id):
        nota = self._nota(client_id, nota_id)
        if not nota:
            return False
        retrato = (f"Apagou a nota #{nota.id} do cliente #{client_id} (autor: {nota.autor}, "
                   f"criada em {nota.criado_em:%d/%m/%Y %H:%M}): '{nota.texto[:1000]}'")
        db.session.delete(nota)
        db.session.commit()
        self.audit.log_action(self._get_current_user(), 'DELETE', 'Nota', retrato)
        return True

    def get_paginated(self, page, per_page, search=None, status_filter=None):
        query = Client.query

        if search:
            term = f"%{search}%"
            query = query.filter(
                or_(
                    Client.name.ilike(term),
                    Client.document.ilike(term),
                    Client.phone.ilike(term)
                )
            )

        query = query.order_by(Client.name.asc())
        pagination = query.paginate(page=page, per_page=per_page, max_per_page=MAX_POR_PAGINA, error_out=False)

        estatisticas = self.estatisticas([c.id for c in pagination.items])
        items = []
        for client in pagination.items:
            stats = estatisticas[client.id]
            is_pending = stats['total_debt'] > 0 and (client.credit_limit > 0 and stats['total_debt'] > client.credit_limit)

            items.append({
                'id': client.id,
                'name': client.name,
                'document': client.document,
                'phone': client.phone,
                'credit_limit': client.credit_limit,
                'standard_rate': client.standard_rate,
                'notes': client.notes,
                **{campo: getattr(client, campo) for campo in ENDERECO},
                'valor_em_aberto': stats['total_debt'],
                'cheques_ativos': stats['active_count'],
                'cheques_totais': stats['total_count'],
                'pendencia': is_pending,
                'reputacao': stats['reputacao'],
            })

        return {
            'items': items,
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page
        }

    def estatisticas(self, ids):
        """Em aberto e reputacao de varios clientes de uma vez (2 consultas para a pagina toda,
        antes era 1 por cliente)."""
        saida = {i: {'total_count': 0, 'active_count': 0, 'total_debt': 0.0, 'devolvido': 0.0,
                     'reputacao': self._reputacao({})} for i in ids}
        if not ids:
            return saida
        aberto = and_(Check.status != 'Pago', Check.fora_do_calculo.is_(False))
        # total_count conta TUDO (e' o historico do cliente, so informativo).
        # Em aberto / divida contam so cheque que esta no calculo: assim o limite
        # de credito nao dispara por causa de titulo antigo da planilha.
        for cid, total, ativos, divida, devolvido in db.session.query(
                Operation.client_id, func.count(Check.id),
                func.sum(case((aberto, 1), else_=0)), func.sum(case((aberto, Check.amount), else_=0)),
                func.sum(case((Check.status == 'Devolvido', Check.amount), else_=0))
        ).join(Operation, Check.operation_id == Operation.id)\
         .filter(Operation.client_id.in_(ids)).group_by(Operation.client_id):
            saida[cid].update(total_count=total or 0, active_count=int(ativos or 0), total_debt=divida or 0.0,
                              devolvido=float(devolvido or 0))

        # a reputacao usa o historico inteiro, inclusive o que esta fora do calculo
        hoje = date.today()
        vencimento = func.coalesce(Check.original_due_date, Check.due_date)
        pago = Check.status == 'Pago'
        situacao = case(
            (Check.status == 'Juridico', 'juridico'),
            (Check.status == 'Devolvido', 'devolvido'),
            (or_(Check.status == 'Atrasado',
                 and_(Check.status == 'Aguardando', Check.due_date < hoje - timedelta(days=TOLERANCIA_DIAS))), 'vencido'),
            (and_(pago, Check.fine_amount > 0), 'devolvido_pago'),
            (and_(pago, exists().where(CheckExtension.check_id == Check.id)), 'prorrogado'),
            (and_(pago, Check.payment_date > vencimento + TOLERANCIA_DIAS), 'atrasado'),
            (pago, 'em_dia'),
            else_='a_vencer')
        por_cliente = {}
        for cid, sit, qtd, valor, atraso in db.session.query(
                Operation.client_id, situacao, func.count(Check.id),
                func.sum(func.coalesce(Check.original_amount, Check.amount)),
                func.avg(cast(Check.payment_date - vencimento, Integer))
        ).join(Operation, Check.operation_id == Operation.id)\
         .filter(Operation.client_id.in_(ids)).group_by(Operation.client_id, situacao):
            por_cliente.setdefault(cid, {})[sit] = (qtd, float(valor or 0), atraso)
        for cid, grupos in por_cliente.items():
            saida[cid]['reputacao'] = self._reputacao(grupos)
        return saida

    def _reputacao(self, grupos):
        contagem = {s: grupos.get(s, (0,))[0] for s in (*PONTOS, 'a_vencer')}
        avaliados = sum(contagem[s] for s in PONTOS)
        peso = sum(grupos[s][1] for s in PONTOS if s in grupos)
        nota = None
        if avaliados >= MIN_AVALIADOS and peso > 0:
            nota = round(sum(PONTOS[s] * grupos[s][1] for s in PONTOS if s in grupos) / peso)
        if nota is None:
            nivel = 'Sem histórico'
        elif nota >= 90:
            nivel = 'Excelente'
        elif nota >= 75:
            nivel = 'Bom'
        elif nota >= 50:
            nivel = 'Regular'
        else:
            nivel = 'Ruim'
        # titulo devolvido ou no juridico em aberto: nao passa de Regular, por maior que seja o resto
        if nivel in ('Excelente', 'Bom') and (contagem['devolvido'] or contagem['juridico']):
            nivel = 'Regular'
        atraso = grupos.get('atrasado', (0, 0, None))[2]
        return {'nota': nota, 'nivel': nivel, 'avaliados': avaliados, 'contagem': contagem,
                'atraso_medio_dias': round(float(atraso)) if atraso is not None else None}