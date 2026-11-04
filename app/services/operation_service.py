from datetime import datetime
import math
import sys
from app import db
from app.utils.validacao import ler_data, ler_texto
from app.models.domain import Operation, Check, Transaction, Client, CompanySettings, Contrato
from flask_jwt_extended import get_jwt
from app.services.audit_service import AuditService
from sqlalchemy import asc, desc, func
from sqlalchemy.orm import joinedload, selectinload

MAX_POR_PAGINA = 100
TIPOS_TITULO = ('CHEQUE', 'PROMISSORIA')
# as duas empresas do topo do borderô. PQ e' a do dia a dia (padrao); a Fozesc faz os contratos.
# Borderô gravado antes da 2.13 (sem empresa) conta como PQ.
EMPRESAS = ('Fozesc', 'PQ')
EMPRESA_PADRAO = 'PQ'


def coluna_empresa():
    return func.coalesce(Operation.empresa, EMPRESA_PADRAO)


def filtrar_empresa(query, empresa):
    """So a empresa pedida (a query precisa ter Operation); vazio ou outra coisa = as duas."""
    return query.filter(coluna_empresa() == empresa) if empresa in EMPRESAS else query
BORDEROS_POR_PAGINA = 10      # ficha do cliente
MAX_BORDEROS_POR_PAGINA = 50

# Conta do borderô: a MESMA da tela (Front-end/src/utils/calculoBordero.js, calcularLinha).
# Antes o servidor recalculava cada cheque com outra formula (liquido = valor / fator) e
# gravava um juros menor que o da tela. Agora a tela e o servidor fazem a mesma conta.

def arredondar(valor):
    """Igual ao arredondar() da tela: Math.round((v + EPSILON) * 100) / 100 (meio centavo sobe)."""
    return math.floor((valor + sys.float_info.epsilon) * 100 + 0.5) / 100


def calcular_linha(valor_face, dias, taxa_mensal, iof_ativo=False, iof_base=0.0, iof_diario=0.0):
    """(juros, iof, liquido) de um titulo, na mesma ordem de contas da tela."""
    valor_face = float(valor_face or 0)
    if not valor_face or dias <= 0:
        return 0.0, 0.0, valor_face
    fator = math.pow(1 + float(taxa_mensal) / 100, dias / 30.0)
    juros = arredondar(valor_face * (fator - 1))
    iof = 0.0
    if iof_ativo:
        iof = arredondar(valor_face * (float(iof_base) / 100) + valor_face * (float(iof_diario) / 100) * dias)
    return juros, iof, arredondar(valor_face - juros - iof)


def reais(valor):
    """1234.5 -> '1.234,50' (texto do caixa)."""
    return f"{valor:,.2f}".replace(',', '_').replace('.', ',').replace('_', '.')


def calcular_comissao(juros_total, comissao, taxa_mensal):
    """Igual ao calcularComissao() da tela: `comissao` pontos dos `taxa_mensal` pontos da
    taxa (2 de 8% = 25% dos juros)."""
    if not comissao or comissao <= 0 or not taxa_mensal or taxa_mensal <= 0:
        return 0.0
    return arredondar(arredondar(juros_total) * comissao / taxa_mensal)


def linha_comissao(data, origem, valor, rotulo, parte, **vinculo):
    """Comissao: sai do caixa no mesmo dia (ao contrario dos juros, que so entram quando o
    cheque e' pago)."""
    return Transaction(date=data, amount=-valor, type='saida', origin=origem, category='Comissão',
                       description=f"Comissão ({parte} dos juros) - {rotulo}"[:200], **vinculo)


def linha_informativa(data, origem, valor, tipo, descricao, **vinculo):
    """Linha so para leitura: amount 0, nao mexe em saldo nem em soma nenhuma."""
    return Transaction(date=data, amount=0.0, valor_informativo=valor, type=tipo, origin=origem,
                       category='Informativo', description=descricao[:200], **vinculo)


def agrupar(linhas):
    """Grava as linhas e liga todas pelo id da primeira (o caixa mostra o bloco junto)."""
    db.session.add_all(linhas)
    db.session.flush()
    grupo = min(t.id for t in linhas)
    for t in linhas:
        t.grupo_id = grupo


def recalcular_total(grupo_id):
    """A linha 'Pgto Borderô' (so informativa) e' o total que sai do banco: saidas - entradas
    das linhas reais do mesmo grupo. Chamar quando uma linha do grupo sai do caixa; sem
    linha real nenhuma, o total sai tambem. Grupo sem essa linha (prorrogacao) nao muda."""
    total = Transaction.query.filter(Transaction.grupo_id == grupo_id, Transaction.type == 'saida',
                                     Transaction.valor_informativo.isnot(None)).first()
    if not total:
        return
    reais = Transaction.query.filter(Transaction.grupo_id == grupo_id, Transaction.valor_informativo.is_(None)).all()
    if not reais:
        db.session.delete(total)
        return
    total.valor_informativo = round(sum(-abs(t.amount) if t.type == 'entrada' else abs(t.amount) for t in reais), 2)


def ler_comissao(data, taxa_mensal):
    try:
        comissao = float(data.get('comissao') or 0)
    except (TypeError, ValueError):
        raise ValueError("Comissão inválida")
    if not 0 <= comissao <= taxa_mensal:
        raise ValueError(f"A comissão tem que ficar entre 0 e a taxa do borderô ({taxa_mensal:g}%)")
    return comissao


class OperationService:
    def __init__(self):
        self.audit = AuditService()

    def create_operation(self, data):
        try:
            client = Client.query.get(data['client_id'])
            if not client:
                raise ValueError("Cliente não encontrado")
            if not data.get('checks'):
                raise ValueError("Borderô sem cheques")

            op_date = ler_data(data.get('operation_date') or datetime.now().date(), 'Data da operação')

            conta_origem = data.get('account_source') or 'Dinheiro'
            if conta_origem not in ('Dinheiro', 'BB', 'Caixa'):
                raise ValueError("Conta de saída inválida (use Dinheiro, BB ou Caixa)")
            origem_sistema = f"Sistema ({conta_origem})"

            try:
                taxa = float(data['taxa_mensal'])
                dias_comp = int(data.get('dias_compensacao', 0))
            except (TypeError, ValueError, KeyError):
                raise ValueError("Taxa ou dias de compensação inválidos")
            if not (math.isfinite(taxa) and 0 <= taxa <= 100) or not 0 <= dias_comp <= 30:
                raise ValueError("Taxa (0 a 100%) ou dias de compensação (0 a 30) inválidos")
            comissao = ler_comissao(data, taxa)
            empresa = data.get('empresa') or EMPRESA_PADRAO
            if empresa not in EMPRESAS:
                raise ValueError("Empresa inválida (use Fozesc ou PQ)")

            new_operation = Operation(
                client_id=data['client_id'],
                client_name_snapshot=client.name,
                operation_date=op_date,
                monthly_rate=taxa,
                compensation_days=dias_comp,
                account_source=conta_origem,
                notes=data.get('notes'),
                comissao=comissao,
                empresa=empresa,
                avalista_nome=ler_texto(data.get('avalista_nome'), 'o avalista', 100),
                avalista_documento=ler_texto(data.get('avalista_documento'), 'o CPF do avalista', 20),
                total_face_value=0.0,
                total_interest=0.0,
                total_net_value=0.0
            )
            
            db.session.add(new_operation)
            db.session.flush()

            acumulado_face = 0.0
            acumulado_juros = 0.0
            acumulado_iof = 0.0
            acumulado_liquido = 0.0
            
            checks_audit_list = []

            # IOF como a tela usou; tela antiga (sem esses campos) cai nas configuracoes
            config = CompanySettings.query.first()
            iof_ativo = bool(data.get('iof_enabled', float(data.get('iof_amount') or 0) > 0))
            iof_base = float(data.get('iof_base', config.iof_rate if config else 0.38) or 0)
            iof_diario = float(data.get('iof_diario', config.iof_daily_rate if config else 0.0041) or 0)
            if not (0 <= iof_base <= 10 and 0 <= iof_diario <= 1):
                raise ValueError("IOF inválido")
            if iof_ativo:
                new_operation.iof_base, new_operation.iof_diario = iof_base, iof_diario

            # 1o a conta de todos os cheques, depois grava (o valor redondo da parcela vem da tela,
            # no gerador de parcelas; juros e liquido sao sempre a conta exata)
            contas = []
            for n, item in enumerate(data['checks'], 1):
                valor_face = float(item['valor'])
                if not valor_face > 0:
                    raise ValueError(f"Cheque {n}: o valor tem que ser maior que zero")
                venc = ler_data(item.get('vencimento'), f'Cheque {n}: vencimento')
                if (item.get('tipo') or 'CHEQUE') not in TIPOS_TITULO:
                    raise ValueError(f"Título {n}: tipo inválido (use cheque ou promissória)")
                dias = (venc - new_operation.operation_date).days + dias_comp
                contas.append([*calcular_linha(valor_face, dias, new_operation.monthly_rate,
                                               iof_ativo, iof_base, iof_diario), valor_face, venc, dias])

            for n, (item, (juros, iof, liquido, valor_face, venc, dias)) in enumerate(zip(data['checks'], contas), 1):
                vencimento = venc
                # A tela manda o que mostrou. Tem que bater com a conta daqui (1 centavo de
                # folga so para o arredondamento de ponto flutuante JS x Python) - e ai grava
                # EXATAMENTE o que a tela mostrou, centavo por centavo.
                if item.get('juros') is not None and item.get('liquido') is not None:
                    j_tela = round(float(item['juros']), 2)
                    i_tela = round(float(item.get('iof') or 0), 2)
                    l_tela = round(float(item['liquido']), 2)
                    if (abs(j_tela - juros) > 0.011 or abs(i_tela - iof) > 0.011
                            or abs(round(valor_face - j_tela - i_tela, 2) - l_tela) > 0.011):
                        raise ValueError(f"Cheque {n}: a conta da tela (juros {j_tela:.2f}, IOF {i_tela:.2f}) "
                                         f"não confere com o servidor (juros {juros:.2f}, IOF {iof:.2f}). "
                                         f"Recarregue a página e gere o borderô de novo.")
                    juros, iof, liquido = j_tela, i_tela, l_tela
                if not liquido > 0:
                    raise ValueError(f"Cheque {n}: juros e IOF passam do valor do cheque (líquido R$ {liquido:.2f}). "
                                     f"Use menos parcelas ou um prazo menor.")

                new_check = Check(
                    operation_id=new_operation.id,
                    type=item.get('tipo') or 'CHEQUE',
                    bank=item.get('banco', ''),
                    number=item.get('num_doc', ''),
                    due_date=datetime.strptime(vencimento, '%Y-%m-%d').date() if isinstance(vencimento, str) else vencimento,
                    amount=valor_face,
                    interest_amount=juros, 
                    net_amount=liquido,    
                    days=dias,
                    status='Aguardando',
                    destination_bank='Carteira',
                    issuer_name=item.get('emitente', '')
                )
                
                db.session.add(new_check)
                
                acumulado_face += valor_face
                acumulado_juros += juros
                acumulado_iof += iof
                acumulado_liquido += liquido
                
                checks_audit_list.append(f"[{'Promissória ' + (new_check.number or '') if new_check.type == 'PROMISSORIA' else new_check.bank} R$ {new_check.amount:.2f}]")

            new_operation.total_face_value = round(acumulado_face, 2)
            new_operation.total_interest = round(acumulado_juros, 2)
            new_operation.comissao_valor = calcular_comissao(new_operation.total_interest, comissao,
                                                             new_operation.monthly_rate)
            # o que sai do caixa = soma dos liquidos dos cheques = o liquido da tela
            # (valor - juros - IOF). Tudo sai dos mesmos numeros: nao sobra centavo.
            liquido_entregue = round(acumulado_liquido, 2)
            new_operation.iof_amount = round(acumulado_iof, 2)
            new_operation.total_net_value = liquido_entregue

            # saida dividida (parte do dinheiro, parte do banco): a mesma validacao do Receber, a
            # soma tem que fechar com o liquido. A conta do borderô vira a da maior parte.
            partes = None
            if data.get('partes'):
                from app.services.check_service import CheckService
                partes = CheckService()._validar_partes(data['partes'], liquido_entregue, 'líquido do borderô')
                conta_origem = max(partes, key=lambda p: p[2])[0]
                if len(partes) == 1:
                    partes = None
                new_operation.account_source = conta_origem
                origem_sistema = f"Sistema ({conta_origem})"

            # comissao descontada de um vale: abate ate o que falta nele e so o resto sai do caixa
            vale, saldo_vale, no_vale = None, 0.0, 0.0
            if data.get('vale_id') not in (None, ''):
                if not new_operation.comissao_valor:
                    raise ValueError("Só dá para abater num vale quando o borderô tem comissão")
                from app.services.vale_service import travar_para_abater
                vale, saldo_vale = travar_para_abater(data['vale_id'])
                no_vale = round(min(new_operation.comissao_valor, saldo_vale), 2)
            comissao_no_caixa = round(new_operation.comissao_valor - no_vale, 2)

            rotulo = f"Borderô #{new_operation.id} - {client.name}"
            inicio = 'Cliente recebe - ' if new_operation.comissao_valor > 0 else 'Pgto '

            def saida_cliente(conta, valor, sufixo=''):
                return Transaction(date=new_operation.operation_date, description=f"{inicio}{rotulo}{sufixo}"[:200],
                                   amount=-valor, type='saida', origin=f"Sistema ({conta})",
                                   category='Compra de Ativos', operation_id=new_operation.id)

            if partes:
                saidas = [saida_cliente(conta, valor, f" (Parte {i}/{len(partes)}" + (f" · {forma}" if forma else '') + ")")
                          for i, (conta, forma, valor) in enumerate(partes, 1)]
            else:
                saidas = [saida_cliente(conta_origem, liquido_entregue)]
            if new_operation.comissao_valor > 0:
                # o cliente recebe o liquido e a comissao e' paga a parte: a 1a linha (so
                # informativa) e' o total que sai do banco = cliente + comissao, as de baixo
                linhas = [
                    linha_informativa(new_operation.operation_date, origem_sistema,
                                      round(liquido_entregue + comissao_no_caixa, 2), 'saida',
                                      f"Pgto {rotulo}", operation_id=new_operation.id),
                    *saidas,
                    linha_comissao(new_operation.operation_date, origem_sistema, new_operation.comissao_valor,
                                   rotulo + (f" · R$ {reais(no_vale)} descontados no vale #{vale.id}" if vale else ''),
                                   f"{comissao / new_operation.monthly_rate * 100:.4g}%".replace('.', ','),
                                   operation_id=new_operation.id),
                ]
                if vale:
                    # a parte da comissao que paga o vale entra de volta na mesma conta
                    from app.services.vale_service import abatimento
                    linhas.append(abatimento(vale, saldo_vale, no_vale, new_operation.operation_date, origem_sistema,
                                             f"comissão do {rotulo}", operation_id=new_operation.id))
                agrupar(linhas)
            elif len(saidas) > 1:
                agrupar(saidas)   # as partes aparecem juntas no Fluxo de Caixa
            else:
                db.session.add(saidas[0])

            db.session.commit()

            try:
                claims = get_jwt()
                user_name = claims.get('name', 'Sistema')
            except:
                user_name = 'Sistema'

            resumo_cheques_str = ", ".join(checks_audit_list)
            detalhes = (
                f"Novo Borderô #{new_operation.id} criado.\n"
                f"Cliente: {client.name}\n"
                f"Taxa: {new_operation.monthly_rate}% | Juros Total: R$ {new_operation.total_interest:.2f}\n"
                + (f"Comissão: {comissao:g} de {new_operation.monthly_rate:g} pontos da taxa "
                   f"({comissao / new_operation.monthly_rate * 100:.1f}% dos juros) = "
                   f"R$ {new_operation.comissao_valor:.2f}"
                   + (f" (R$ {no_vale:.2f} descontados no vale #{vale.id}"
                      + (f", R$ {comissao_no_caixa:.2f} saíram de {conta_origem})" if comissao_no_caixa
                         else ", nada saiu do caixa)") if vale else '')
                   + "\n" if comissao else '') +
                f"Valor Líquido Entregue: R$ {new_operation.total_net_value:.2f}\n"
                + (f"Saiu de: " + " + ".join(f"{conta} R$ {valor:.2f}" + (f" ({forma})" if forma else '')
                                             for conta, forma, valor in partes) + "\n" if partes else '') +
                f"Cheques ({len(data['checks'])}): {resumo_cheques_str}"
            )

            self.audit.log_action(user_name, 'CREATE', 'Borderô', detalhes)
            if vale:
                quitou = vale.status == 'Pago'
                self.audit.log_action(
                    user_name, 'BAIXA' if quitou else 'PAGAMENTO', 'Vale',
                    f"Desconto no vale #{vale.id} com a comissão do Borderô #{new_operation.id} ({client.name}) "
                    f"| R$ {no_vale:.2f} de R$ {saldo_vale:.2f}"
                    + (" | quitado" if quitou else f" | resta R$ {saldo_vale - no_vale:.2f}")
                    + f" | no caixa ({conta_origem}): comissão R$ {new_operation.comissao_valor:.2f} saiu e o desconto entrou"
                    + (f", de verdade saíram R$ {comissao_no_caixa:.2f}" if comissao_no_caixa
                       else ', nada saiu de verdade'))
                
            return new_operation

        except Exception as e:
            db.session.rollback()
            raise e
            
    def get_all(self):
        return Operation.query.all()

    def get_paginated(self, page=1, per_page=20, sort_by='id', sort_order='desc'):
        """
        Lista de borderôs paginada.

        Antes existia só o get_all(), que devolvia TODOS os borderôs com TODOS os
        cheques de uma vez. Com os 6 borderôs de teste isso era instantâneo; depois
        de importar a planilha (4.113 borderôs / 8.665 cheques) virou uma resposta
        de ~7 MB e ~7 s a cada abertura da tela de Borderô.
        A tela (BorderoView.fetchNextId) já lia 'items' e 'total' — só que ninguém
        os enviava. Agora envia. O get_all() acima continua existindo intacto.
        """
        colunas = {
            'id': Operation.id,
            'operation_date': Operation.operation_date,
            'total_face_value': Operation.total_face_value,
        }
        coluna = colunas.get(sort_by, Operation.id)
        ordem = desc(coluna) if str(sort_order).lower() == 'desc' else asc(coluna)

        paginacao = Operation.query\
            .options(joinedload(Operation.checks))\
            .order_by(ordem)\
            .paginate(page=page, per_page=per_page, max_per_page=MAX_POR_PAGINA, error_out=False)

        return {
            'items': paginacao.items,
            'total': paginacao.total,
            'pages': paginacao.pages,
            'current_page': page,
        }

    def get_by_client(self, client_id, page=1, per_page=BORDEROS_POR_PAGINA):
        """Borderôs do cliente em paginas (mais novo primeiro). Antes trazia todos de uma vez, com
        todos os titulos: cliente antigo com centenas de borderôs deixava a ficha pesada."""
        pagina = Operation.query\
            .filter(Operation.client_id == client_id)\
            .options(selectinload(Operation.checks))\
            .order_by(desc(Operation.operation_date), desc(Operation.id))\
            .paginate(page=page, per_page=per_page, max_per_page=MAX_BORDEROS_POR_PAGINA, error_out=False)
        return {'items': [self._serialize_with_checks(op) for op in pagina.items],
                'total': pagina.total, 'pages': pagina.pages, 'page': pagina.page, 'per_page': pagina.per_page}

    def documento(self, op_id):
        """Tudo que o borderô em PDF e as promissorias precisam, como no dia do borderô: valor e
        vencimento de origem (antes de prorrogacao ou pagamento parcial), empresa e avalista gravados."""
        from app.services.check_service import TAG_IMPORT, descrever_saida, notas_limpas
        op = db.session.get(Operation, op_id)
        if not op:
            return None
        s = CompanySettings.query.first() or CompanySettings()
        if (op.empresa or EMPRESA_PADRAO) == 'PQ':
            empresa = {'chave': 'PQ', 'nome': s.pq_nome or 'PQ', 'cnpj': s.pq_cnpj, 'endereco': s.pq_endereco}
        else:
            empresa = {'chave': 'Fozesc', 'nome': s.company_name or 'Fozesc', 'cnpj': s.cnpj, 'endereco': s.address}
        c = op.client
        endereco = ' - '.join(filter(None, [c.address, c.neighborhood, '/'.join(filter(None, [c.city, c.state])),
                                            c.zip_code and f'CEP {c.zip_code}']))
        iso = lambda d: d.strftime('%Y-%m-%d') if d else None
        titulos = []
        for t in sorted(op.checks, key=lambda t: t.id):
            face = float(t.original_amount if t.original_amount is not None else t.amount or 0)
            juros, liquido = float(t.interest_amount or 0), float(t.net_amount or 0)
            titulos.append({'tipo': t.type or 'CHEQUE', 'numero': t.number or '', 'banco': t.bank or '',
                            'emitente': t.issuer_name or '', 'vencimento': iso(t.original_due_date or t.due_date),
                            'dias': t.days or 0, 'valor': round(face, 2), 'juros': round(juros, 2),
                            'iof': round(max(face - juros - liquido, 0), 2), 'liquido': round(liquido, 2)})
        return {
            'id': op.id, 'data': iso(op.operation_date), 'empresa': empresa,
            'cliente': {'nome': c.name, 'documento': c.document or '', 'telefone': c.phone or '', 'endereco': endereco},
            'avalista': {'nome': op.avalista_nome or '', 'documento': op.avalista_documento or ''},
            'taxa_mensal': float(op.monthly_rate or 0), 'dias_compensacao': op.compensation_days or 0,
            'conta_saida': descrever_saida(op), 'observacao': notas_limpas(op),
            'importado': TAG_IMPORT in (op.notes or ''),
            'comissao': float(op.comissao or 0), 'comissao_valor': float(op.comissao_valor or 0),
            'valor_total': float(op.total_face_value or 0), 'juros_total': float(op.total_interest or 0),
            'iof_total': float(op.iof_amount or 0), 'liquido_total': float(op.total_net_value or 0),
            'titulos': titulos,
            'tem_contrato': db.session.query(Contrato.id).filter_by(operation_id=op.id).first() is not None,
        }

    def mudar_empresa(self, op_id, empresa, usuario):
        """Corrige a empresa de um borderô (os antigos, gravados antes da 2.13, contam como PQ)."""
        if empresa not in EMPRESAS:
            raise ValueError('Empresa inválida (use Fozesc ou PQ)')
        op = db.session.get(Operation, op_id)
        if not op:
            return None
        antes = op.empresa or f'{EMPRESA_PADRAO} (padrão)'
        op.empresa = empresa
        db.session.commit()
        self.audit.log_action(usuario, 'UPDATE', 'Borderô', f'Borderô #{op.id}: empresa {antes} -> {empresa}')
        return {'id': op.id, 'empresa': op.empresa}

    def _serialize_with_checks(self, op):
        return {
            'id': op.id,
            'client_id': op.client_id,
            'date': op.operation_date.strftime('%Y-%m-%d'),
            'total_face_value': op.total_face_value,
            'total_net_value': op.total_net_value,
            'total_interest': op.total_interest,
            'iof_amount': op.iof_amount,
            'comissao_valor': op.comissao_valor,
            'monthly_rate': op.monthly_rate,
            'status': op.status,
            'notes': op.notes,
            'cheques': [{
                'id': c.id,
                'type': c.type,
                'due_date': c.due_date.strftime('%Y-%m-%d'),
                'amount': c.amount,
                'interest_amount': c.interest_amount,
                'status': c.status,
                'bank': c.bank,
                'number': c.number,
                'issuer_name': c.issuer_name,
                'payment_date': c.payment_date.strftime('%Y-%m-%d') if c.payment_date else None,
                'paid_amount': c.paid_amount
            } for c in op.checks]
        }
        