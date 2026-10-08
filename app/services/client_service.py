from app.models.domain import Client, ClientNote, Operation, Check, User
from app import db
from app.services.audit_service import AuditService
from sqlalchemy import or_, and_, func, case
from werkzeug.security import check_password_hash
from datetime import datetime

MAX_TEXTO_NOTA = 4000
MAX_NOTAS_POR_PAGINA = 50

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
            email=data.get('email'),
            address=data.get('address'),
            credit_limit=data.get('credit_limit', 0.0),
            standard_rate=data.get('standard_rate', 4.0),
            notes=data.get('notes')
        )
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
        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        items = []
        for client in pagination.items:
            stats = self._calculate_client_stats(client.id)
            is_pending = stats['total_debt'] > 0 and (client.credit_limit > 0 and stats['total_debt'] > client.credit_limit)

            items.append({
                'id': client.id,
                'name': client.name,
                'document': client.document,
                'phone': client.phone,
                'credit_limit': client.credit_limit,
                'standard_rate': client.standard_rate,
                'notes': client.notes,
                'valor_em_aberto': stats['total_debt'],
                'cheques_ativos': stats['active_count'],
                'cheques_totais': stats['total_count'],
                'pendencia': is_pending
            })

        return {
            'items': items,
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page
        }

    def _calculate_client_stats(self, client_id):
        totals = db.session.query(
            # total_count conta TUDO (e' o historico do cliente, so informativo).
            # Em aberto / divida contam so cheque que esta no calculo: assim o limite
            # de credito nao dispara por causa de titulo antigo da planilha.
            func.count(Check.id).label('total_count'),
            func.sum(case((and_(Check.status != 'Pago', Check.fora_do_calculo.is_(False)), 1),
                          else_=0)).label('active_count'),
            func.sum(case((and_(Check.status != 'Pago', Check.fora_do_calculo.is_(False)), Check.amount),
                          else_=0)).label('total_debt')
        ).join(Operation, Check.operation_id == Operation.id)\
         .filter(Operation.client_id == client_id)\
         .first()

        return {
            'total_count': totals.total_count or 0,
            'active_count': totals.active_count or 0,
            'total_debt': totals.total_debt or 0.0
        }