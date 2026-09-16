from app.models.domain import Check, Operation, Client, Transaction, CheckExtension, User
from app import db
from app.services.audit_service import AuditService
from app.utils.sanitizer import sanitize_input
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import or_, and_, desc, asc, func
from datetime import datetime, date
from flask import request
from werkzeug.security import check_password_hash

# Marca que o import_planilha.py grava em Operation.notes. E' assim que o sistema
# sabe que um cheque veio da planilha antiga, sem precisar de coluna nova.
TAG_IMPORT = 'IMPORT-PLANILHA'

# Campos que a edicao de cheque aceita mexer. Valor bruto, juros e liquido NAO estao
# aqui de proposito: a regra do projeto e nunca recalcular/reescrever juros.
CAMPOS_EDITAVEIS = ('emitente', 'vencimento', 'data_pagamento', 'data_operacao')

class CheckService:
    def __init__(self):
        self.audit = AuditService()

    def _get_current_user(self):
        try:
            verify_jwt_in_request(optional=True)
            return get_jwt_identity() or 'Sistema'
        except:
            return 'Sistema'

    def _usuario_logado(self):
        """O User de verdade (precisa dele para conferir a senha na edicao)."""
        ident = self._get_current_user()
        if str(ident).isdigit():
            return db.session.get(User, int(ident))
        return None

    def _data(self, valor, campo):
        """Le AAAA-MM-DD e recusa ano fora da faixa - foi digito errado de ano
        (2005 no lugar de 2025, 1902 no lugar de 2022) que sujou a planilha antiga."""
        try:
            d = datetime.strptime(str(valor), '%Y-%m-%d').date()
        except (ValueError, TypeError):
            raise ValueError(f"{campo}: data invalida (use AAAA-MM-DD)")
        if not (2000 <= d.year <= 2100):
            raise ValueError(f"{campo}: ano {d.year} fora da faixa permitida (2000-2100)")
        return d

    def create(self, data):
        try:
            amount = float(data.get('valor', 0))
            due_date_str = data.get('vencimento')
            due_date = datetime.strptime(due_date_str, '%Y-%m-%d').date() if due_date_str else None
            conta_saida = data.get('contaSaida', 'Dinheiro')
            
            # 1. Cria a Operação
            nova_operacao = Operation(
                client_id=data.get('client_id'),
                operation_date=datetime.now().date(),
                total_face_value=amount,
                total_net_value=amount,
                total_interest=0.0,
                status='Aprovada',
                account_source=conta_saida,
                notes=data.get('observacao')
            )
            db.session.add(nova_operacao)
            db.session.flush()
            
          
            novo_cheque = Check(
                operation_id=nova_operacao.id,
                bank=data.get('banco'),
                number=data.get('num_doc'),
                issuer_name=data.get('emitente'),
                amount=amount,
                net_amount=amount,
                interest_amount=0.0,
                due_date=due_date,
                original_due_date=due_date,
                status='Aguardando'
            )
            db.session.add(novo_cheque)
            
           
            if conta_saida and amount > 0:
                desc_tx = f"Empréstimo Cheque #{novo_cheque.number or 'S/N'} - {novo_cheque.issuer_name}"
                transacao = Transaction(
                    date=datetime.now().date(),
                    description=desc_tx[:200],
                    amount=amount,
                    type='saida',
                    origin=conta_saida, 
                    category='Empréstimo Manual',
                    operation_id=nova_operacao.id
                )
                db.session.add(transacao)

            db.session.commit()
            self.audit.log_action(self._get_current_user(), 'CREATE', 'Cheque', f"Criou cheque manual: R$ {amount}")
            return True, self._serialize_check(novo_cheque)
            
        except Exception as e:
            db.session.rollback()
            print(f"Erro ao criar cheque: {e}")
            return False, str(e)


    def update(self, id, data):
        """
        Edicao de cheque com confirmacao por SENHA (o 2o fator): quem edita digita a
        propria senha de novo, senao nada muda. Serve para corrigir nome do emitente e
        data digitada errada (o caso dos anos 2005/1902 que vieram da planilha).

        NAO mexe em valor bruto, juros nem liquido - nem por senha. O codigo antigo
        desta funcao fazia `net_amount = valor`, ou seja: editar o valor apagava o
        juros do cheque sem avisar. Foi tirado.
        """
        check = Check.query.get(id)
        if not check:
            return False, "Cheque não encontrado"

        usuario = self._usuario_logado()
        if not usuario or not check_password_hash(usuario.password_hash, str(data.get('senha') or '')):
            self.audit.log_action(self._get_current_user(), 'NEGADO', 'Cheque',
                                  f"Senha incorreta ao tentar editar cheque #{check.number or 'S/N'} "
                                  f"({check.issuer_name})")
            raise PermissionError("Senha incorreta - o cheque não foi alterado")

        if not any(c in data for c in CAMPOS_EDITAVEIS):
            return False, "Nada para alterar"

        try:
            mudancas = []

            if 'emitente' in data:
                novo = sanitize_input(str(data['emitente'] or '')).strip()[:100]
                if not novo:
                    return False, "Emitente não pode ficar vazio"
                if novo != (check.issuer_name or ''):
                    mudancas.append(f"emitente '{check.issuer_name}' -> '{novo}'")
                    check.issuer_name = novo

            if 'vencimento' in data:
                nova = self._data(data['vencimento'], 'Vencimento')
                if nova != check.due_date:
                    mudancas.append(f"vencimento {check.due_date} -> {nova}")
                    check.due_date = nova
                    # de proposito NAO recalcula juros, liquido nem dias.

            if 'data_pagamento' in data:
                nova = self._data(data['data_pagamento'], 'Data de pagamento') if data['data_pagamento'] else None
                if nova != check.payment_date:
                    mudancas.append(f"data de pagamento {check.payment_date} -> {nova}")
                    check.payment_date = nova

            if 'data_operacao' in data and check.operation:
                nova = self._data(data['data_operacao'], 'Data da operação')
                if nova != check.operation.operation_date:
                    mudancas.append(f"data da operacao (borderô #{check.operation_id}) "
                                    f"{check.operation.operation_date} -> {nova}")
                    check.operation.operation_date = nova

            if not mudancas:
                return True, self._serialize_check(check)

            db.session.commit()
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Cheque',
                                  f"Editou cheque #{check.number or 'S/N'} ({check.issuer_name}): "
                                  + " | ".join(mudancas) + " [confirmado com senha]")
            return True, self._serialize_check(check)
        except ValueError as e:
            db.session.rollback()
            return False, str(e)
        except Exception as e:
            db.session.rollback()
            print(f"Erro ao atualizar cheque: {e}")
            return False, "Erro ao atualizar o cheque"

    def _montar_query(self, search=None, status=None, date_start=None, date_end=None, calculo=None):
        """Filtros da tela de Cheques num lugar so: a lista e a acao em lote usam os
        MESMOS filtros. Sem isso, 'aplicar aos filtrados' podia pegar cheque que voce
        nem esta vendo na tela."""
        query = Check.query.join(Operation).join(Client)

        if calculo == 'dentro':
            query = query.filter(Check.fora_do_calculo.is_(False))
        elif calculo == 'fora':
            query = query.filter(Check.fora_do_calculo.is_(True))

        if search:
            term = f"%{search}%"
            filtros_busca = [
                Check.issuer_name.ilike(term),
                Client.name.ilike(term),
                Check.number.ilike(term),
                Check.bank.ilike(term)
            ]
            search_limpo = search.replace('#', '').strip()
            if search_limpo.isdigit():
                filtros_busca.append(Operation.id == int(search_limpo))
            query = query.filter(or_(*filtros_busca))

        if status:
            status_list = status.split(',')
            status_list = [s for s in status_list if s]
            if status_list and 'Todos' not in status_list:
            
                if 'Atrasado' in status_list:
                    hoje = date.today()
                   
                    condicao_atrasado = or_(
                        Check.status == 'Atrasado',
                        and_(Check.status == 'Aguardando', Check.due_date < hoje)
                    )
                    
                    outros_status = [s for s in status_list if s != 'Atrasado']
                    if outros_status:
                        query = query.filter(or_(Check.status.in_(outros_status), condicao_atrasado))
                    else:
                        query = query.filter(condicao_atrasado)
                else:
                    query = query.filter(Check.status.in_(status_list))

        if date_start:
            hoje = date.today()
           
            query = query.filter(
                or_(
                    Check.due_date >= date_start,
                    and_(Check.status == 'Aguardando', Check.due_date < hoje),
                    Check.status.in_(['Atrasado', 'Devolvido'])
                )
            )
            
        if date_end:
            query = query.filter(Check.due_date <= date_end)

        return query

    def get_paginated(self, page, per_page, search=None, status=None, date_start=None,
                      date_end=None, sort_by='due_date', sort_order='asc', calculo=None):
        query = self._montar_query(search, status, date_start, date_end, calculo)

        sort_column = Check.due_date 
        if sort_by == 'amount': sort_column = Check.amount
        elif sort_by == 'issuer_name': sort_column = Check.issuer_name
        elif sort_by == 'due_date': sort_column = Check.due_date
            
        if sort_order == 'desc':
            query = query.order_by(desc(sort_column))
        else:
            query = query.order_by(asc(sort_column))

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        items_serializados = []
        hoje = date.today()
        for c in pagination.items:

            if c.status == 'Aguardando' and c.due_date < hoje:
                c.status = 'Atrasado'
            items_serializados.append(self._serialize_check(c))

        return {
            'items': items_serializados,
            'total': pagination.total,
            'pages': pagination.pages,
            'current_page': page
        }

    def definir_calculo(self, fora, ids=None, filtros=None):
        """
        Liga/desliga 'fora do calculo' em LOTE. Dois modos:
          - ids=[1,2,3]        -> so os cheques marcados na tela
          - filtros={...}      -> todos os que batem com o filtro atual da tela
                                  (ex: status=Juridico), sem precisar marcar um por um.

        E' um UPDATE unico no banco (nao carrega 8 mil cheques na memoria) e deixa
        UMA linha na auditoria com a quantidade, nao uma por cheque.
        """
        fora = bool(fora)

        if ids:
            try:
                ids = [int(i) for i in ids][:5000]
            except (TypeError, ValueError):
                return False, "Lista de cheques inválida"
            alvo = Check.query.filter(Check.id.in_(ids))
            descricao = f"{len(ids)} cheque(s) selecionado(s)"
        elif filtros:
            sub = self._montar_query(**filtros).with_entities(Check.id).scalar_subquery()
            alvo = Check.query.filter(Check.id.in_(sub))
            usados = {k: v for k, v in filtros.items() if v}
            descricao = f"filtro {usados or 'nenhum (todos)'}"
        else:
            return False, "Informe os cheques (selecionados ou por filtro)"

        try:
            quantos = alvo.update({Check.fora_do_calculo: fora}, synchronize_session=False)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Erro ao mudar fora_do_calculo: {e}")
            return False, "Erro ao aplicar a alteração"

        acao = 'TIROU DO CALCULO' if fora else 'VOLTOU AO CALCULO'
        self.audit.log_action(self._get_current_user(), 'UPDATE', 'Cheque',
                              f"{acao}: {quantos} cheque(s) | {descricao}")
        return True, {'alterados': quantos, 'fora_do_calculo': fora}

    def get_portfolio_total(self):
        # Inclui 'Prorrogado' (título renegociado ainda a receber) no total da carteira.
        # Cheque marcado como 'fora do calculo' (historico da planilha) nao entra.
        total = db.session.query(func.sum(Check.amount)).filter(
            Check.status.in_(['Aguardando', 'Atrasado', 'Juridico', 'Prorrogado']),
            Check.fora_do_calculo.is_(False)
        ).scalar()
        return {'total_portfolio': total or 0.0}

    def update_status(self, id, new_status, payment_data=None):
        check = Check.query.get(id)
        if not check: return False
        
        old_status = check.status
        check.status = new_status
        
        
        if old_status == 'Pago' and new_status != 'Pago':
            if hasattr(check, 'payment_date'): check.payment_date = None
            if hasattr(check, 'paid_amount'): check.paid_amount = 0.0
            
            
            prefixo_pgto = f"Recebimento Cheque #{getattr(check, 'number', 'S/N')} - {getattr(check, 'issuer_name', '')}"
            transacao_pgto = Transaction.query.filter(
                Transaction.operation_id == check.operation_id,
                Transaction.category == 'Recebimento de Cheque',
                Transaction.description.like(f"{prefixo_pgto}%")
            ).first()
            
            if transacao_pgto:
                db.session.delete(transacao_pgto)

        if old_status == 'Devolvido' and new_status != 'Devolvido':
            if hasattr(check, 'fine_amount'): check.fine_amount = 0.0
            
    
            prefixo_multa = f"Multa Devolução Cheque #{getattr(check, 'number', 'S/N')} - {getattr(check, 'issuer_name', '')}"
            transacao_multa = Transaction.query.filter(
                Transaction.operation_id == check.operation_id,
                Transaction.category == 'Multas e Juros',
                Transaction.description.like(f"{prefixo_multa}%")
            ).first()
            
            if transacao_multa:
                db.session.delete(transacao_multa)

        # --- LÓGICA DE NOVO PAGAMENTO ---
        if new_status == 'Pago' and old_status != 'Pago':
        
            method = 'Dinheiro'
            amount_paid = float(check.amount) if hasattr(check, 'amount') else 0.0
            
            try:
                req_data = request.get_json(silent=True)
                if req_data and 'payment_data' in req_data:
                    if 'method' in req_data['payment_data']:
                        method = req_data['payment_data']['method']
                    if 'amount' in req_data['payment_data']:
                        amount_paid = float(req_data['payment_data']['amount'])
            except Exception as e:
                print(f"Erro ao ler método de pagamento: {e}")
                
            if hasattr(check, 'payment_date'): check.payment_date = datetime.now().date()
            if hasattr(check, 'paid_amount'): check.paid_amount = amount_paid
            if hasattr(check, 'payment_method'): check.payment_method = method 

            desc_tx = f"Recebimento Cheque #{getattr(check, 'number', 'S/N')} - {getattr(check, 'issuer_name', '')}"
            transacao = Transaction(
                date=datetime.now().date(),
                description=desc_tx[:200],
                amount=amount_paid, 
                type='entrada',
                origin=method, 
                category='Recebimento de Cheque',
                operation_id=check.operation_id
            )
            db.session.add(transacao)
        
        # --- LÓGICA DE NOVO CHEQUE DEVOLVIDO ---
        elif new_status == 'Devolvido' and old_status != 'Devolvido':
            taxa_multa = 2.0
            method = 'Dinheiro' 
            
            try:
                req_data = request.get_json(silent=True)
                if req_data and 'payment_data' in req_data:
                    if 'taxa_multa' in req_data['payment_data']:
                        taxa_multa = float(req_data['payment_data']['taxa_multa'])
                    if 'method' in req_data['payment_data']:
                        method = req_data['payment_data']['method'] 
            except Exception as e:
                print(f"Erro ao ler taxa/método: {e}")

            multa_calculada = float(getattr(check, 'amount', 0.0)) * (taxa_multa / 100.0)
            if hasattr(check, 'fine_amount'): check.fine_amount = multa_calculada

            desc_tx = f"Multa Devolução Cheque #{getattr(check, 'number', 'S/N')} - {getattr(check, 'issuer_name', '')} ({taxa_multa}%)"
            transacao = Transaction(
                date=datetime.now().date(),
                description=desc_tx[:200],
                amount=multa_calculada, 
                type='entrada',
                origin=method, 
                category='Multas e Juros',
                operation_id=check.operation_id
            )
            db.session.add(transacao)

        db.session.commit()

        acao = 'BAIXA' if new_status == 'Pago' else 'UPDATE'
        detalhes = f"Cheque #{check.number} ({check.issuer_name}): {old_status} -> {new_status}"
        if new_status == 'Pago': detalhes += f" | Recebido: R$ {check.paid_amount}"
        if new_status == 'Devolvido': detalhes += f" | Multa: R$ {check.fine_amount}"
        
        self.audit.log_action(self._get_current_user(), acao, 'Cheque', detalhes)

        
        return check

    def prorrogate_check(self, check_id, new_date_str, fee_amount, notes):
        check = Check.query.get(check_id)
        if not check: return False, "Título não encontrado"

        try:
            old_date = check.due_date
            new_date_obj = datetime.strptime(new_date_str, '%Y-%m-%d').date()

            if hasattr(check, 'original_due_date') and not check.original_due_date:
                check.original_due_date = old_date

            fee_amount_float = float(fee_amount) if fee_amount else 0.0

            method = 'Dinheiro'
            try:
                req_data = request.get_json(silent=True)
                if req_data and 'method' in req_data:
                    method = req_data['method']
            except:
                pass

            extension = CheckExtension(
                check_id=check.id,
                old_due_date=old_date,
                new_due_date=new_date_obj,
                days_added=(new_date_obj - old_date).days,
                fee_amount=fee_amount_float,
                notes=notes,
                status='PAGO' 
            )
            db.session.add(extension)

 
            if fee_amount_float > 0:
                desc_tx = f"Taxa Prorrogação Cheque #{getattr(check, 'number', 'S/N')} - {getattr(check, 'issuer_name', '')}"
                transacao = Transaction(
                    date=datetime.now().date(),
                    description=desc_tx[:200],
                    amount=fee_amount_float,
                    type='entrada',
                    origin=method,
                    category='Multas e Juros',
                    operation_id=check.operation_id
                )
                db.session.add(transacao)

            check.due_date = new_date_obj
            check.status = 'Prorrogado' 
            
            db.session.commit()
            
            self.audit.log_action(self._get_current_user(), 'UPDATE', 'Cheque', f"Prorrogação Cheque #{getattr(check, 'number', 'S/N')}: {old_date} -> {new_date_str}")
            return True, "Prorrogação realizada com sucesso"
        except Exception as e:
            db.session.rollback()
            return False, str(e)

    def delete(self, id):
        cheque = Check.query.get(id)
        if not cheque:
            return False

        # O modelo Check não possui campo `document` (o correto é `number`). O código
        # antigo referenciava `cheque.document`, sempre estourava AttributeError e o log
        # caía no fallback "ID: X", perdendo a informação do cheque. Corrigido.
        try:
            info = f"Cheque #{cheque.number or 'S/N'} - {cheque.issuer_name} (R$ {cheque.amount})"
        except Exception:
            info = f"ID: {cheque.id}"

        db.session.delete(cheque)
        db.session.commit()

        self.audit.log_action(
            self._get_current_user(), 
            'DELETE', 
            'Cheque', 
            f"Excluiu permanentemente: {info}"
        )
        return True

    def _serialize_check(self, c):

        obs_da_operacao = ""
        if hasattr(c, 'operation') and c.operation:
            obs_da_operacao = getattr(c.operation, 'notes', '')

        # 2. Descobre forma de devolução (se houver)
        forma_devolucao = None
        if getattr(c, 'status', '') == 'Devolvido':
            tx_dev = Transaction.query.filter(
                Transaction.operation_id == c.operation_id,
                Transaction.category == 'Multas e Juros',
                Transaction.description.like("Multa Devolução%")
            ).order_by(Transaction.id.desc()).first()
            if tx_dev:
                forma_devolucao = tx_dev.origin


        op_notes = (getattr(c.operation, 'notes', '') or '') if getattr(c, 'operation', None) else ''

        return {
            'id': c.id,
            'operation_id': c.operation_id,
            'fora_do_calculo': bool(getattr(c, 'fora_do_calculo', False)),
            'importado': TAG_IMPORT in op_notes,
            'data_operacao': (c.operation.operation_date.strftime('%Y-%m-%d')
                              if getattr(c, 'operation', None) and c.operation.operation_date else None),
            'numero': getattr(c, 'number', ''),
            'banco': getattr(c, 'bank', ''),
            'num_doc': getattr(c, 'number', ''),
            'valor_bruto': float(getattr(c, 'amount', 0.0)),
            'valor_liquido': float(getattr(c, 'net_amount', 0.0)),
            'juros': float(getattr(c, 'interest_amount', 0.0)),
            
            'emissao': (getattr(c, 'issue_date', None) or date.today()).strftime('%Y-%m-%d'),
            'vencimento': (getattr(c, 'due_date', None) or date.today()).strftime('%Y-%m-%d'),
            'vencimento_original': (getattr(c, 'original_due_date', None) or getattr(c, 'due_date', None) or date.today()).strftime('%Y-%m-%d'),
            'data_entrada': (getattr(c, 'created_at', None) or datetime.now()).strftime('%Y-%m-%d'),
            
            'data_pagamento': c.payment_date.strftime('%Y-%m-%d') if getattr(c, 'payment_date', None) else None,
            'valor_pago': float(getattr(c, 'paid_amount', 0.0)),
            'forma_pagamento': getattr(c, 'payment_method', None),
            'forma_devolucao': forma_devolucao,
            'cliente': c.operation.client.name if c.operation and c.operation.client else 'Sem Cliente',
            'emitente': getattr(c, 'issuer_name', ''),
            'status': getattr(c, 'status', ''),
            'observacao': obs_da_operacao, # Agora a variável está definida!
            'historico_prorrogacao': [self._serialize_extension(e) for e in getattr(c, 'extensions', [])]
        }

    def _serialize_extension(self, e):
        tx_prorrog = Transaction.query.filter(
            Transaction.operation_id == getattr(e, 'check', type('obj', (object,), {'operation_id': None})).operation_id,
            Transaction.category == 'Multas e Juros',
            Transaction.amount == getattr(e, 'fee_amount', 0.0),
            Transaction.description.like("Taxa Prorrogação%")
        ).order_by(Transaction.id.desc()).first()
        
        metodo = tx_prorrog.origin if tx_prorrog else 'Dinheiro'
        data_simulacao = getattr(e, 'created_at', getattr(e, 'old_due_date', datetime.now()))

        return {
            'id': e.id,
            'data_simulacao': data_simulacao.strftime('%Y-%m-%d') if data_simulacao else None,
            'de': getattr(e, 'old_due_date', datetime.now()).strftime('%Y-%m-%d'),
            'para': getattr(e, 'new_due_date', datetime.now()).strftime('%Y-%m-%d'),
            'dias': getattr(e, 'days_added', 0),
            'taxa': float(getattr(e, 'fee_amount', 0.0)),
            'forma_pagamento': metodo 
        }