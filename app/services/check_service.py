from app.models.domain import Check, Operation, Client, Transaction, CheckExtension, User
from app import db
from app.services.audit_service import AuditService
from app.utils.sanitizer import sanitize_input
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import or_, and_, desc, asc, func, case
import math
import re
from datetime import datetime, date
from werkzeug.security import check_password_hash
from app.services.operation_service import arredondar, agrupar, linha_comissao, linha_informativa

# Marca que o import_planilha.py grava em Operation.notes. E' assim que o sistema
# sabe que um cheque veio da planilha antiga, sem precisar de coluna nova.
TAG_IMPORT = 'IMPORT-PLANILHA'
RX_MARCA_IMPORT = re.compile(r'\[' + TAG_IMPORT + r':[^\]]*\]')


def notas_limpas(op):
    # a marca do import e o texto fixo dele nao sao observacao de ninguem
    return re.sub(r'\[' + TAG_IMPORT + r':[^\]]*\]\s*(Importado da planilha historica)?\s*\|?\s*',
                  '', (op.notes if op else '') or '').strip()

# Campos que a edicao de cheque aceita mexer. Valor bruto, juros e liquido NAO estao
# aqui de proposito: a regra do projeto e nunca recalcular/reescrever juros.
CAMPOS_EDITAVEIS = ('emitente', 'vencimento', 'data_pagamento', 'data_operacao', 'observacao')

# Contas que existem no caixa. A origem da transacao TEM que ser uma destas: e' esse
# texto que o get_balances usa para decidir em qual saldo o dinheiro entrou
# (BB -> Banco do Brasil, Caixa -> Caixa Economica, resto -> Dinheiro). Aceitar texto
# livre aqui faria o dinheiro cair no saldo errado, por isso e' whitelist.
CONTAS_CAIXA = ('Dinheiro', 'BB', 'Caixa')

# Como o cliente pagou. E' so um rotulo que vai na descricao do lancamento - quem
# manda no saldo e a conta, nao a forma (um PIX cai no banco, nao no cofre).
FORMAS_PAGAMENTO = ('Dinheiro', 'PIX', 'TED/DOC', 'Depósito', 'Cheque', 'Outro')

# Teto de partes num recebimento dividido (evita payload absurdo virar 500 linhas no caixa)
MAX_PARTES = 10

# teto da lista de titulos do mesmo borderô na tela de detalhes
LIMITE_PARCELAS = 100

# rotulo que o recebimento dividido grava no fim da descricao: "(Parte 1/2 · PIX)"
RX_PARTE = re.compile(r'\(Parte (\d+)/\d+(?: · ([^)]+))?\)\s*$')

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
            db.session.flush()   # precisa do id para vincular o lancamento ao cheque
            
           
            if conta_saida and amount > 0:
                desc_tx = f"Empréstimo Cheque #{novo_cheque.number or 'S/N'} - {novo_cheque.issuer_name}"
                transacao = Transaction(
                    date=datetime.now().date(),
                    description=desc_tx[:200],
                    amount=amount,
                    type='saida',
                    origin=conta_saida, 
                    category='Empréstimo Manual',
                    operation_id=nova_operacao.id,
                    check_id=novo_cheque.id
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

            if 'observacao' in data and check.operation:
                op = check.operation
                nova = str(data['observacao'] or '').strip()[:1000]
                atual = notas_limpas(op)
                if nova != atual:
                    marca = RX_MARCA_IMPORT.search(op.notes or '')
                    op.notes = ' | '.join(x for x in (marca.group(0) if marca else '', nova) if x) or None
                    mudancas.append(f"observacao do borderô #{op.id} '{atual}' -> '{nova}'")

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
            status_list = [s for s in status.split(',') if s]
            if status_list and 'Todos' not in status_list:
                hoje = date.today()
                vencido = and_(Check.status == 'Aguardando', Check.due_date < hoje)
                condicoes = []
                for s in status_list:
                    if s == 'Atrasado':
                        condicoes.append(or_(Check.status == 'Atrasado', vencido))
                    elif s == 'Aguardando':
                        condicoes.append(and_(Check.status == 'Aguardando', Check.due_date >= hoje))
                    else:
                        condicoes.append(Check.status == s)
                query = query.filter(or_(*condicoes))

        if date_start:
            query = query.filter(Check.due_date >= date_start)

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
            'current_page': page,
            'resumo': self._resumo(search, status, date_start, date_end, calculo),
        }

    def _resumo(self, search, status, date_start, date_end, calculo):
        """Soma de TODOS os cheques do filtro (nao so da pagina), por status, no banco.
        Aguardando vencido aparece como Atrasado, igual na lista."""
        situacao = case((and_(Check.status == 'Aguardando', Check.due_date < date.today()), 'Atrasado'),
                        else_=Check.status)
        linhas = (self._montar_query(search, status, date_start, date_end, calculo)
                  .with_entities(situacao, Check.fora_do_calculo, func.count(Check.id), func.sum(Check.amount))
                  .group_by(situacao, Check.fora_do_calculo).all())
        por_status = {}
        fora = {'qtd': 0, 'valor': 0.0}
        for st, fora_calc, qtd, valor in linhas:
            s = por_status.setdefault(st, {'status': st, 'qtd': 0, 'valor': 0.0})
            s['qtd'] += qtd
            s['valor'] += valor or 0.0
            if fora_calc:
                fora['qtd'] += qtd
                fora['valor'] += valor or 0.0
        itens = sorted(por_status.values(), key=lambda s: -s['valor'])
        for s in itens:
            s['valor'] = round(s['valor'], 2)
        return {
            'qtd': sum(s['qtd'] for s in itens),
            'valor': round(sum(s['valor'] for s in itens), 2),
            'por_status': itens,
            'fora': {'qtd': fora['qtd'], 'valor': round(fora['valor'], 2)},
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

    def listar_emitentes(self, termo=None, limite=10):
        """Nomes de emitente ja usados, do mais frequente para o menos.

        Serve para o campo do borderô sugerir o nome que ja esta no banco em vez de
        cada um digitar de um jeito - foi esse tipo de divergencia (um nome curto
        que tambem existe numa versao longa) que deu trabalho na importacao da planilha.
        Cheque marcado como historico (fora_do_calculo) ENTRA aqui de proposito: os
        8.254 cheques pagos da planilha sao onde estao quase todos os nomes reais de
        emitente. Agregado no banco e com limite: nunca traz os 653 nomes de uma vez.
        """
        limite = max(1, min(int(limite or 10), 20))
        q = db.session.query(Check.issuer_name).filter(
            Check.issuer_name.isnot(None), Check.issuer_name != '')
        termo = (termo or '').strip()
        if termo:
            q = q.filter(Check.issuer_name.ilike(f'%{termo}%'))
        linhas = q.group_by(Check.issuer_name)\
                  .order_by(func.count(Check.id).desc(), Check.issuer_name.asc())\
                  .limit(limite).all()
        return [nome for (nome,) in linhas]

    def get_portfolio_total(self):
        # Inclui 'Prorrogado' (título renegociado ainda a receber) no total da carteira.
        # Cheque marcado como 'fora do calculo' (historico da planilha) nao entra.
        total = db.session.query(func.sum(Check.amount)).filter(
            Check.status.in_(['Aguardando', 'Atrasado', 'Juridico', 'Prorrogado']),
            Check.fora_do_calculo.is_(False)
        ).scalar()
        return {'total_portfolio': total or 0.0}

    def _validar_partes(self, partes, total, rotulo='valor do título'):
        """Recebimento dividido (parte no dinheiro, parte no banco...).

        Valida tudo ANTES de encostar no caixa e devolve [(conta, forma, valor)].
        A soma das partes tem que fechar com o valor do titulo: se fechasse por menos,
        o cheque ficava Pago com dinheiro que nunca entrou.
        """
        if not isinstance(partes, list) or not partes:
            raise ValueError("Informe as partes do recebimento")
        if len(partes) > MAX_PARTES:
            raise ValueError(f"Máximo de {MAX_PARTES} partes por recebimento")

        limpas = []
        for i, p in enumerate(partes, 1):
            if not isinstance(p, dict):
                raise ValueError(f"Parte {i}: formato inválido")

            conta = str(p.get('conta') or p.get('method') or '').strip()
            if conta not in CONTAS_CAIXA:
                raise ValueError(f"Parte {i}: conta inválida (use {', '.join(CONTAS_CAIXA)})")

            forma = str(p.get('forma') or '').strip()
            if forma and forma not in FORMAS_PAGAMENTO:
                raise ValueError(f"Parte {i}: forma de pagamento inválida")

            try:
                valor = round(float(p.get('valor', p.get('amount', 0))), 2)
            except (TypeError, ValueError):
                raise ValueError(f"Parte {i}: valor inválido")
            if not valor > 0:
                raise ValueError(f"Parte {i}: o valor tem que ser maior que zero")

            limpas.append((conta, forma, valor))

        soma = round(sum(v for _, _, v in limpas), 2)
        total = round(float(total or 0), 2)
        if abs(soma - total) > 0.01:
            raise ValueError(f"A soma das partes (R$ {soma:.2f}) tem que fechar com o "
                             f"{rotulo} (R$ {total:.2f})")
        return limpas

    def _rotulo_imposto(self, calc, imposto):
        """Como o imposto foi calculado, para o caixa e a auditoria. A conta vem da tela
        (calcularImposto em calculoBordero.js): aqui so confere o formato do retrato."""
        if not calc:
            return 'valor à mão'
        if not isinstance(calc, dict):
            raise ValueError("Cálculo do imposto inválido")
        try:
            dias = int(calc.get('dias') or 0)
        except (TypeError, ValueError):
            raise ValueError("Cálculo do imposto inválido")
        taxa = self._valor(calc.get('taxa_mensal') or 0, 'Taxa do imposto')
        sistema = calc.get('calculado')
        sistema = None if sistema in (None, '') else self._valor(sistema, 'Imposto calculado')
        # dias <= 0: titulo em dia na data escolhida (o valor foi digitado)
        if taxa > 100 or abs(dias) > 36500:
            raise ValueError("Cálculo do imposto inválido")
        if dias <= 0 or not sistema:
            return 'valor à mão'
        texto = f"{dias} dias a {f'{taxa:g}'.replace('.', ',')}% a.m." + (" + IOF" if calc.get('iof') else "")
        if abs(sistema - imposto) > 0.005:
            texto += f", à mão (sistema R$ {sistema:.2f})"
        return texto

    def _apagar_lancamentos(self, check, categoria, prefixo):
        """Desfaz no caixa o que a baixa/devolucao criou.

        Um recebimento dividido gera VARIAS linhas, entao apaga todas as que estao
        vinculadas ao cheque (check_id). O `prefixo` desempata dentro da mesma
        categoria - multa de devolucao e taxa de prorrogacao dividem 'Multas e Juros'.
        Lancamento antigo (gravado antes do check_id existir) ainda e' achado pelo
        texto, como era antes: um so, o mais recente que casa.
        """
        base = Transaction.query.filter(Transaction.category == categoria,
                                        Transaction.description.like(f"{prefixo}%"))
        lancamentos = base.filter(Transaction.check_id == check.id).all()

        if not lancamentos:
            legado = base.filter(
                Transaction.check_id.is_(None),
                Transaction.operation_id == check.operation_id,
                Transaction.description.like(
                    f"{prefixo} #{check.number or 'S/N'} - {check.issuer_name or ''}%")
            ).first()
            lancamentos = [legado] if legado else []

        for t in lancamentos:
            db.session.delete(t)
        return len(lancamentos)

    def update_status(self, id, new_status, payment_data=None):
        check = Check.query.get(id)
        if not check: return False

        dados = payment_data if isinstance(payment_data, dict) else {}
        old_status = check.status

        # Valida ANTES de mexer no cheque: pedido invalido nao chega a alterar nada.
        partes = None
        imposto, rotulo_imposto = 0.0, ''
        if new_status == 'Pago' and old_status != 'Pago':
            # imposto so quando ele marca na tela: atrasado ou nao, quem decide e' ele
            imposto = self._valor(dados.get('imposto') or 0, 'Imposto')
            if imposto:
                rotulo_imposto = self._rotulo_imposto(dados.get('imposto_calculo'), imposto)
            if dados.get('partes'):
                partes = self._validar_partes(dados['partes'], round(float(check.amount or 0) + imposto, 2),
                                              'total com imposto' if imposto else 'valor do título')
            else:
                if (dados.get('method') or 'Dinheiro') not in CONTAS_CAIXA:
                    raise ValueError(f"Conta inválida (use {', '.join(CONTAS_CAIXA)})")
                forma_unica = str(dados.get('forma') or '').strip()
                if forma_unica and forma_unica not in FORMAS_PAGAMENTO:
                    raise ValueError("Forma de pagamento inválida")
        taxa_multa = 0.0
        if new_status == 'Devolvido' and old_status != 'Devolvido':
            if (dados.get('method') or 'Dinheiro') not in CONTAS_CAIXA:
                raise ValueError(f"Conta inválida (use {', '.join(CONTAS_CAIXA)})")
            try:
                taxa_multa = float(dados.get('taxa_multa', 2.0) or 0.0)
            except (TypeError, ValueError):
                raise ValueError("Taxa de multa inválida")
            if not 0 <= taxa_multa <= 100:
                raise ValueError("Taxa de multa inválida (use de 0 a 100%)")

        check.status = new_status
        detalhe_partes = ''

        if old_status == 'Pago' and new_status != 'Pago':
            check.payment_date = None
            check.paid_amount = 0.0
            check.payment_method = None
            check.imposto_cobrado = 0.0
            self._apagar_lancamentos(check, 'Recebimento de Cheque', 'Recebimento Cheque')
            self._apagar_lancamentos(check, 'Multas e Juros', 'Imposto Cheque')

        # so DESFAZER a devolucao (voltar a cobrar) tira a multa do caixa. Receber o cheque
        # devolvido ou manda-lo ao Juridico mantem: a multa ja entrou de verdade.
        if old_status == 'Devolvido' and new_status in ('Aguardando', 'Atrasado'):
            check.fine_amount = 0.0
            self._apagar_lancamentos(check, 'Multas e Juros', 'Multa Devolução Cheque')

        # --- LÓGICA DE NOVO PAGAMENTO ---
        if new_status == 'Pago' and old_status != 'Pago':
            hoje = datetime.now().date()
            desc_base = f"Recebimento Cheque #{check.number or 'S/N'} - {check.issuer_name or ''}"
            # o imposto vai numa linha propria ('Multas e Juros'), como os juros da prorrogacao
            desc_imposto = (f"Imposto Cheque #{check.number or 'S/N'} - {check.issuer_name or ''}"[:110]
                            + f" ({rotulo_imposto})")

            def entrada(descricao, valor, conta, categoria):
                db.session.add(Transaction(
                    date=hoje,
                    description=descricao[:200],
                    amount=valor,
                    type='entrada',
                    origin=conta,
                    category=categoria,
                    operation_id=check.operation_id,
                    check_id=check.id
                ))

            if partes:
                total_pago = round(sum(v for _, _, v in partes), 2)
                qtd = len(partes)
                falta_imposto = imposto
                for i, (conta, forma, valor) in enumerate(partes, 1):
                    # "Dinheiro · Dinheiro" e' redundante: forma so aparece se somar info
                    rotulo = f" (Parte {i}/{qtd}" + (f" · {forma}" if forma and forma != conta else "") + ")"
                    # cada parte paga primeiro o que falta do imposto e o resto e' do titulo
                    de_imposto = round(min(valor, falta_imposto), 2)
                    falta_imposto = round(falta_imposto - de_imposto, 2)
                    if de_imposto > 0:
                        entrada(desc_imposto + rotulo, de_imposto, conta, 'Multas e Juros')
                    if round(valor - de_imposto, 2) > 0:
                        entrada(desc_base + rotulo, round(valor - de_imposto, 2), conta, 'Recebimento de Cheque')
                contas = list(dict.fromkeys(c for c, _, _ in partes))
                check.payment_method = f"Múltiplo ({' + '.join(contas)})"[:50]
                detalhe_partes = ' | Partes: ' + ' + '.join(
                    f"{c} R$ {v:.2f}" + (f" ({f})" if f else "") for c, f, v in partes)
            else:
                method = dados.get('method') or 'Dinheiro'
                forma = str(dados.get('forma') or '').strip()
                try:
                    # com imposto o titulo entra pelo valor devido; 'amount' e' do formato antigo
                    principal = round(float((None if imposto else dados.get('amount')) or check.amount or 0), 2)
                except (TypeError, ValueError):
                    raise ValueError("Valor recebido inválido")
                # a forma e' so rotulo ("recebi via PIX"): quem manda no saldo e a conta
                rotulo = f" · {forma}" if forma and forma != method else ""
                check.payment_method = f"{method}{rotulo}"[:50]
                entrada(desc_base + rotulo, principal, method, 'Recebimento de Cheque')
                if imposto:
                    entrada(desc_imposto + rotulo, imposto, method, 'Multas e Juros')
                total_pago = round(principal + imposto, 2)

            check.payment_date = hoje
            check.paid_amount = total_pago
            check.imposto_cobrado = imposto

        # --- LÓGICA DE NOVO CHEQUE DEVOLVIDO ---
        elif new_status == 'Devolvido' and old_status != 'Devolvido':
            method = dados.get('method') or 'Dinheiro'

            multa_calculada = round(float(check.amount or 0.0) * (taxa_multa / 100.0), 2)
            check.fine_amount = multa_calculada

            desc_tx = (f"Multa Devolução Cheque #{check.number or 'S/N'} - "
                       f"{check.issuer_name or ''} ({taxa_multa}%)")
            db.session.add(Transaction(
                date=datetime.now().date(),
                description=desc_tx[:200],
                amount=multa_calculada,
                type='entrada',
                origin=method,
                category='Multas e Juros',
                operation_id=check.operation_id,
                check_id=check.id
            ))

        db.session.commit()

        acao = 'BAIXA' if new_status == 'Pago' else 'UPDATE'
        detalhes = f"Cheque #{check.number} ({check.issuer_name}): {old_status} -> {new_status}"
        if new_status == 'Pago':
            detalhes += f" | Recebido: R$ {check.paid_amount}"
            if imposto:
                detalhes += (f" (título R$ {check.paid_amount - imposto:.2f} + imposto "
                             f"R$ {imposto:.2f}: {rotulo_imposto})")
            detalhes += detalhe_partes
        if new_status == 'Devolvido': detalhes += f" | Multa: R$ {check.fine_amount}"

        self.audit.log_action(self._get_current_user(), acao, 'Cheque', detalhes)

        return check

    def _valor(self, bruto, campo):
        try:
            v = round(float(bruto), 2)
        except (TypeError, ValueError):
            raise ValueError(f"{campo}: valor inválido")
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"{campo}: valor inválido")
        return v

    def prorrogate_check(self, check_id, dados):
        """Prorrogacao do titulo (com ou sem pagamento na hora), numa operacao so.

        1. Juros desta prorrogacao = conta do Borderô de Liquido (Inverso) sobre o saldo
           devido, ate a nova data. Vem da tela (Front-end/src/utils/calculoBordero.js);
           o backend nao repete a formula - pode ter sido ajustada a mao - e so exige >= 0.
        2. O que o cliente paga agora quita primeiro esses juros e o que passar abate o
           saldo. Se nao cobrir os juros, a diferenca soma no valor devido.
        3. Novo valor devido = saldo + juros - pago: vira o `amount` do titulo.
        Sem prorrogar (so pagamento parcial) nao ha juros: o pago abate o saldo direto.
        Caixa, titulo, historico e auditoria gravam juntos ou nada grava.
        """
        check = db.session.get(Check, check_id)
        if not check:
            return None
        if check.status == 'Pago':
            raise ValueError("Título já está pago")

        hoje = date.today()
        total = round(float(check.amount or 0), 2)

        saldo_base = total
        if dados.get('saldo_base') not in (None, ''):
            saldo_base = self._valor(dados['saldo_base'], 'Saldo para o cálculo')
        if saldo_base <= 0:
            raise ValueError("Saldo para o cálculo tem que ser maior que zero")
        ajuste = round(saldo_base - total, 2)

        prorrogar = bool(dados.get('prorrogar'))
        venc_atual = check.due_date
        nova = venc_atual
        juros = 0.0
        calculo = {}
        if prorrogar:
            nova = self._data(dados.get('new_date'), 'Nova data de vencimento')
            data_base = self._data(dados.get('data_base') or venc_atual.isoformat(), 'Data base')
            if nova <= venc_atual:
                raise ValueError(f"A nova data tem que ser depois do vencimento atual "
                                 f"({venc_atual.strftime('%d/%m/%Y')})")
            if nova <= data_base:
                raise ValueError("A nova data tem que ser depois da data base do cálculo")
            juros = self._valor(dados.get('novos_juros') or 0, 'Juros da prorrogação')
            taxa = self._valor(dados.get('taxa_mensal') or 0, 'Taxa')
            if taxa > 100:
                raise ValueError("Taxa: valor inválido")
            try:
                dias_comp = int(dados.get('dias_compensacao') or 0)
            except (TypeError, ValueError):
                raise ValueError("Dias de compensação inválidos")
            if not 0 <= dias_comp <= 30:
                raise ValueError("Dias de compensação inválidos")
            calculo = {
                'data_base': data_base.isoformat(),
                'taxa_mensal': taxa,
                'dias_compensacao': dias_comp,
                'iof': bool(dados.get('iof')),
                'dias': (nova - data_base).days + dias_comp,
                'juros_calculado': (self._valor(dados['juros_calculado'], 'Juros calculado')
                                    if dados.get('juros_calculado') not in (None, '') else None),
            }
        total_com_juros = round(saldo_base + juros, 2)

        # comissao: `comissao` pontos dos `taxa` pontos da taxa, sobre os juros sem o IOF
        # (`comissao_base`, que vem da tela - o backend nao refaz a conta dos juros)
        comissao = self._valor(dados.get('comissao') or 0, 'Comissão')
        comissao_valor, comissao_base, comissao_conta = 0.0, 0.0, None
        if comissao:
            if not prorrogar:
                raise ValueError("Comissão só existe na prorrogação (pagamento parcial não tem juros)")
            if comissao > calculo['taxa_mensal']:
                raise ValueError(f"A comissão tem que ficar entre 0 e a taxa ({calculo['taxa_mensal']:g}%)")
            comissao_base = self._valor(dados.get('comissao_base') or 0, 'Juros da comissão')
            if comissao_base > juros:
                raise ValueError("Os juros da comissão não podem passar dos juros da prorrogação")
            comissao_conta = str(dados.get('comissao_conta') or '').strip()
            if comissao_conta not in CONTAS_CAIXA:
                raise ValueError(f"Conta da comissão inválida (use {', '.join(CONTAS_CAIXA)})")
            comissao_valor = arredondar(comissao_base * comissao / calculo['taxa_mensal'])

        recebido = self._valor(dados.get('valor_recebido') or 0, 'Valor pago')
        if recebido > total_com_juros:
            raise ValueError(f"Pago (R$ {recebido:.2f}) maior que o devido (R$ {total_com_juros:.2f})")
        if recebido == total_com_juros:
            raise ValueError("O valor pago quita o título inteiro: use o botão Receber")
        if not prorrogar and not recebido and not ajuste:
            raise ValueError("Nada para registrar: informe o valor pago ou a nova data")
        conta = None
        partes = []
        data_recebimento = hoje
        if recebido > 0:
            if dados.get('partes'):
                # dividido (parte no dinheiro, parte no banco): mesma regra do Receber
                partes = self._validar_partes(dados['partes'], recebido)
                contas = list(dict.fromkeys(c for c, _, _ in partes))
                conta = contas[0] if len(contas) == 1 else f"Múltiplo ({' + '.join(contas)})"
            else:
                conta = str(dados.get('conta') or '').strip()
                if conta not in CONTAS_CAIXA:
                    raise ValueError(f"Conta inválida (use {', '.join(CONTAS_CAIXA)})")
                partes = [(conta, str(dados.get('forma') or '').strip(), recebido)]
            data_recebimento = self._data(dados.get('data_recebimento') or hoje.isoformat(),
                                          'Data do pagamento')
            if data_recebimento > hoje:
                raise ValueError("Data do pagamento não pode ser no futuro")

        if ajuste:
            # mudar o que o cliente deve sem dinheiro entrar: mesmo 2o fator da edicao
            usuario = self._usuario_logado()
            if not usuario or not check_password_hash(usuario.password_hash, str(dados.get('senha') or '')):
                self.audit.log_action(self._get_current_user(), 'NEGADO', 'Cheque',
                                      f"Senha incorreta ao ajustar o saldo do cheque #{check.number or 'S/N'} "
                                      f"({check.issuer_name}) de R$ {total:.2f} para R$ {saldo_base:.2f}")
                raise PermissionError("Senha incorreta - nada foi gravado")

        juros_pagos = min(recebido, juros)
        abatido = round(recebido - juros_pagos, 2)
        juros_nao_pagos = round(juros - juros_pagos, 2)
        novo_total = round(total_com_juros - recebido, 2)
        # so informativo: quanto do valor devido e' juros que ficou sem pagar
        pendentes = min(max(float(check.juros_pendentes or 0), 0.0), saldo_base)
        juros_pendentes = round(max(pendentes - abatido, 0.0) + juros_nao_pagos, 2)
        numero, emitente = check.number or 'S/N', check.issuer_name or ''

        try:
            # cada parte paga primeiro o que falta dos juros e o resto abate o saldo:
            # o caixa fica certo por conta E por categoria
            falta_juros = juros_pagos
            linhas_caixa = []
            for i, (conta_parte, forma, valor_parte) in enumerate(partes, 1):
                de_juros = round(min(valor_parte, falta_juros), 2)
                falta_juros = round(falta_juros - de_juros, 2)
                rotulo_parte = ([f"Parte {i}/{len(partes)}"] if len(partes) > 1 else []) \
                    + ([forma] if forma and forma != conta_parte else [])
                sufixo = f" ({' · '.join(rotulo_parte)})" if rotulo_parte else ''
                for valor, categoria, rotulo in ((de_juros, 'Multas e Juros', 'Juros de prorrogação'),
                                                 (round(valor_parte - de_juros, 2), 'Recebimento Parcial',
                                                  'Recebimento parcial')):
                    if valor > 0:
                        linhas_caixa.append(Transaction(
                            date=data_recebimento, amount=valor, type='entrada', origin=conta_parte,
                            description=f"{rotulo} - Cheque #{numero} - {emitente}{sufixo}"[:200],
                            category=categoria, operation_id=check.operation_id, check_id=check.id))
            if comissao_valor > 0:
                # juros da prorrogacao = comissao (sai hoje) + o que fica para a empresa (so informativo)
                rotulo = f"prorrogação do cheque #{numero} - {emitente}"
                vinculo = {'operation_id': check.operation_id, 'check_id': check.id}
                agrupar(linhas_caixa + [
                    linha_comissao(hoje, comissao_conta, comissao_valor, rotulo,
                                   f"{comissao / calculo['taxa_mensal'] * 100:.4g}%".replace('.', ','), **vinculo),
                    linha_informativa(hoje, comissao_conta, round(juros - comissao_valor, 2), 'entrada',
                                      f"Juros sem a comissão (informativo) - {rotulo}", **vinculo)])
            else:
                db.session.add_all(linhas_caixa)

            if check.original_amount is None and novo_total != total:
                check.original_amount = total
            check.amount = novo_total
            check.juros_pendentes = juros_pendentes
            if prorrogar:
                if not check.original_due_date:
                    check.original_due_date = venc_atual
                check.due_date = nova
                # prorrogado nao e' mais status: volta a Aguardando (vira Atrasado sozinho se
                # passar da nova data) e a tela marca "prorrogado" pelo historico
                check.status = 'Aguardando'

            detalhe = {
                'prorrogar': prorrogar,
                'valor_anterior': total,
                'ajuste': ajuste,
                'saldo_base': saldo_base,
                'novos_juros': juros,
                'total_com_juros': total_com_juros,
                'valor_recebido': recebido,
                'conta': conta,
                'partes': [{'conta': c, 'forma': f, 'valor': v} for c, f, v in partes] if len(partes) > 1 else None,
                'data_recebimento': data_recebimento.isoformat() if recebido else None,
                'juros_pagos': juros_pagos,
                'principal_abatido': abatido,
                'juros_nao_pagos': juros_nao_pagos,
                'novo_total': novo_total,
                **calculo,
                **({'comissao': comissao, 'comissao_base': comissao_base, 'comissao_valor': comissao_valor,
                    'comissao_conta': comissao_conta} if comissao_valor else {}),
            }
            db.session.add(CheckExtension(
                check_id=check.id, old_due_date=venc_atual, new_due_date=nova,
                days_added=(nova - venc_atual).days, fee_amount=juros,
                notes=sanitize_input(str(dados.get('notes') or ''))[:1000] or None,
                status='PENDENTE' if juros_nao_pagos > 0 else 'PAGO', detalhe=detalhe))
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        texto = f"Cheque #{numero} ({emitente}): devia R$ {total:.2f}"
        if ajuste:
            texto += f" | AJUSTE MANUAL do saldo R$ {total:.2f} -> R$ {saldo_base:.2f}"
        if prorrogar:
            texto += (f" | prorrogado {venc_atual.strftime('%d/%m/%Y')} -> {nova.strftime('%d/%m/%Y')}, "
                      f"{calculo['taxa_mensal']}% a.m., {calculo['dias']} dias, juros R$ {juros:.2f}")
            if calculo['juros_calculado'] is not None and calculo['juros_calculado'] != juros:
                texto += f" (sistema calculou R$ {calculo['juros_calculado']:.2f})"
        if recebido:
            texto += (f" | pagou R$ {recebido:.2f} em {conta} (juros R$ {juros_pagos:.2f} + "
                      f"abatido R$ {abatido:.2f})")
        if comissao_valor:
            texto += (f" | comissão {comissao:g} de {calculo['taxa_mensal']:g} pontos sobre juros R$ {comissao_base:.2f} "
                      f"= R$ {comissao_valor:.2f} (saiu de {comissao_conta})")
        if juros_nao_pagos:
            texto += f" | R$ {juros_nao_pagos:.2f} de juros ficaram no saldo"
        texto += f" | novo valor devido R$ {novo_total:.2f}"
        self.audit.log_action(self._get_current_user(),
                              'PRORROGACAO' if prorrogar else 'RECEBIMENTO PARCIAL', 'Cheque', texto)
        return self._serialize_check(check)

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

        obs_da_operacao = notas_limpas(getattr(c, 'operation', None))

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

        # Quebra do recebimento dividido. So consulta quando o cheque foi recebido em
        # partes (o payment_method marca isso) - assim a listagem de cheques normal
        # continua sem consulta extra por linha.
        partes_pagamento = []
        if (getattr(c, 'payment_method', '') or '').startswith('Múltiplo'):
            # com imposto, uma parte pode ter 2 linhas (imposto + titulo): soma pela parte
            por_parte = {}
            for t in Transaction.query.filter(
                    Transaction.check_id == c.id,
                    Transaction.category.in_(('Recebimento de Cheque', 'Multas e Juros')),
                    or_(Transaction.description.like('Recebimento Cheque%'),
                        Transaction.description.like('Imposto Cheque%'))
                    ).order_by(Transaction.id).all():
                # a forma ("PIX", "Dinheiro"...) fica no rotulo "(Parte 1/2 · PIX)"
                m = RX_PARTE.search(t.description or '')
                p = por_parte.setdefault(int(m.group(1)) if m else -t.id,
                                         {'conta': t.origin, 'forma': (m.group(2) if m else '') or '', 'valor': 0.0})
                p['valor'] = round(p['valor'] + float(t.amount or 0.0), 2)
            partes_pagamento = [por_parte[k] for k in sorted(por_parte)]

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
            'valor_original': float(c.original_amount if c.original_amount is not None else c.amount),
            'juros_pendentes': float(c.juros_pendentes or 0.0),
            'taxa_cliente': (float(c.operation.client.standard_rate)
                             if c.operation and c.operation.client and c.operation.client.standard_rate is not None
                             else None),
            'valor_liquido': float(c.net_amount or 0.0),
            'juros': float(c.interest_amount or 0.0),
            
            'emissao': (getattr(c, 'issue_date', None) or date.today()).strftime('%Y-%m-%d'),
            'vencimento': (getattr(c, 'due_date', None) or date.today()).strftime('%Y-%m-%d'),
            'vencimento_original': (getattr(c, 'original_due_date', None) or getattr(c, 'due_date', None) or date.today()).strftime('%Y-%m-%d'),
            # Check nao tem created_at: antes isto era sempre "hoje". Entrada = data do borderô.
            'data_entrada': (c.operation.operation_date.strftime('%Y-%m-%d')
                             if getattr(c, 'operation', None) and c.operation.operation_date else None),
            'dias': c.days,
            'tipo': c.type,
            'iof_bordero': bool(c.operation and (c.operation.iof_amount or 0) > 0),
            
            'data_pagamento': c.payment_date.strftime('%Y-%m-%d') if getattr(c, 'payment_date', None) else None,
            'valor_pago': float(c.paid_amount or 0.0),
            'imposto_cobrado': float(c.imposto_cobrado or 0.0),
            'multa': float(c.fine_amount or 0.0),
            # quantas vezes o vencimento foi empurrado (marca "prorrogado" na tela)
            'prorrogacoes': sum(1 for e in getattr(c, 'extensions', [])
                                if e.old_due_date and e.new_due_date and e.new_due_date > e.old_due_date),
            'forma_pagamento': getattr(c, 'payment_method', None),
            'partes_pagamento': partes_pagamento,
            'forma_devolucao': forma_devolucao,
            'cliente': c.operation.client.name if c.operation and c.operation.client else 'Sem Cliente',
            'emitente': getattr(c, 'issuer_name', ''),
            'status': getattr(c, 'status', ''),
            'observacao': obs_da_operacao, # Agora a variável está definida!
            'historico_prorrogacao': [self._serialize_extension(e) for e in getattr(c, 'extensions', [])]
        }

    def detalhes(self, id):
        """Tudo que a tela de detalhes mostra: o titulo, o borderô de origem e os outros
        titulos do mesmo borderô. Separado da listagem para ela nao pagar essas consultas."""
        c = db.session.get(Check, id)
        if not c:
            return None
        op = c.operation
        importado = TAG_IMPORT in (op.notes or '')
        notas = notas_limpas(op)
        irmaos = (db.session.query(Check.id, Check.number, Check.due_date, Check.amount, Check.status,
                                   Check.issuer_name)
                  .filter(Check.operation_id == op.id)
                  .order_by(Check.due_date.asc(), Check.id.asc()).limit(LIMITE_PARCELAS).all())
        total_titulos = Check.query.filter_by(operation_id=op.id).count()
        return {
            **self._serialize_check(c),
            'bordero': {
                'id': op.id,
                'data': op.operation_date.strftime('%Y-%m-%d') if op.operation_date else None,
                'lancado_em': op.created_at.strftime('%Y-%m-%d %H:%M') if op.created_at else None,
                'taxa_mensal': float(op.monthly_rate or 0),
                'dias_compensacao': op.compensation_days or 0,
                'conta_saida': None if importado else op.account_source,
                'iof': float(op.iof_amount or 0),
                'comissao': float(op.comissao or 0),
                'comissao_valor': float(op.comissao_valor or 0),
                'valor_total': float(op.total_face_value or 0),
                'juros_total': float(op.total_interest or 0),
                'liquido_entregue': float(op.total_net_value or 0),
                'qtd_titulos': total_titulos,
                'observacao': notas.strip(),
                'importado': importado,
            },
            'titulos_do_bordero': [{
                'id': i, 'numero': n, 'vencimento': v.strftime('%Y-%m-%d') if v else None,
                'valor': float(a or 0), 'status': st, 'emitente': em,
            } for i, n, v, a, st, em in irmaos],
        }

    def _serialize_extension(self, e):
        base = {
            'id': e.id,
            'data_simulacao': e.prorrogation_date.strftime('%Y-%m-%d') if e.prorrogation_date else None,
            'de': e.old_due_date.strftime('%Y-%m-%d') if e.old_due_date else None,
            'para': e.new_due_date.strftime('%Y-%m-%d') if e.new_due_date else None,
            'dias': e.days_added or 0,
            'taxa': float(e.fee_amount or 0.0),
            'observacao': e.notes,
        }
        if e.detalhe:
            return {**base, **e.detalhe, 'forma_pagamento': e.detalhe.get('conta')}

        # prorrogacao do formato antigo: a taxa era paga na hora e a conta so esta no caixa
        tx_prorrog = Transaction.query.filter(
            Transaction.operation_id == e.check.operation_id,
            Transaction.category == 'Multas e Juros',
            Transaction.amount == (e.fee_amount or 0.0),
            Transaction.description.like("Taxa Prorrogação%")
        ).order_by(Transaction.id.desc()).first()
        return {**base, 'forma_pagamento': tx_prorrog.origin if tx_prorrog else 'Dinheiro'}
