import pandas as pd
from app import db
from app.models.domain import Check, Operation, Client, Transaction, CompanySettings
from app.services.history_service import bank_key, NOME_CONTA
from sqlalchemy import func, case
from datetime import datetime, timedelta
import os

# Relatorio gerencial da tela (impressao/PDF). Antes a tela imprimia numeros FIXOS
# escritos no proprio Vue - nenhum valor vinha do banco. Nada aqui recalcula juros:
# tudo e' soma de campo ja gravado, com os MESMOS filtros do Dashboard e do Historico
# Mensal (fora_do_calculo=False, status de inadimplencia, abs() no valor do caixa),
# para os numeros do papel baterem com os da tela.
TIPOS_RESUMO = ('geral', 'inadimplencia', 'lucro')
STATUS_INADIMPLENTE = ('Atrasado', 'Devolvido', 'Juridico')
# O papel nao aguenta mais que isso e a lista pode crescer muito. O TOTAL continua
# vindo agregado do banco, entao o rodape esta certo mesmo com a lista cortada.
LIMITE_ITENS = 500
# Quantas linhas de detalhe (top clientes, categorias) entram no papel.
LIMITE_TOP = 6
# Serie temporal: dia para periodo curto, mes, depois ano. Acima disso o grafico
# viraria um borrao de barras - melhor nao desenhar do que desenhar ilegivel.
MAX_PONTOS = 40
FORMATO_TEMPO = {'dia': 'YYYY-MM-DD', 'mes': 'YYYY-MM', 'ano': 'YYYY'}

VERDE, VERMELHO, AZUL, ROXO, LARANJA, CIANO = (
    '#10b981', '#ef4444', '#6366f1', '#a855f7', '#f97316', '#0ea5e9')
COR_STATUS = {'Atrasado': VERMELHO, 'Devolvido': LARANJA, 'Juridico': ROXO}
COR_CONTA = {'BRASIL': '#3b82f6', 'CAIXA': CIANO, 'DINHEIRO': VERDE}


class ReportService:
    
    def _get_path(self):
        base_dir = os.path.abspath(os.path.dirname(__file__))
        root_dir = os.path.dirname(os.path.dirname(base_dir))
        target_folder = os.path.join(root_dir, "backups")
        if not os.path.exists(target_folder): os.makedirs(target_folder)
        return target_folder

    def _format_date(self, date_obj):
        """Helper para formatar data DD/MM/AAAA para o Excel"""
        if not date_obj:
            return ""
        return date_obj.strftime('%d/%m/%Y')

    # ================================================================== #
    # Resumo gerencial para a tela de Relatorios (impressao / PDF)
    # ================================================================== #
    def _periodo(self, inicio, fim):
        try:
            d1 = datetime.strptime(str(inicio), '%Y-%m-%d').date()
            d2 = datetime.strptime(str(fim), '%Y-%m-%d').date()
        except (ValueError, TypeError):
            raise ValueError('Período inválido (use AAAA-MM-DD)')
        if d1 > d2:
            raise ValueError('A data inicial não pode ser maior que a final')
        return d1, d2

    def _movimento(self, *filtros):
        """(entradas, saidas) do caixa, agregadas no banco. O abs() e o criterio
        'type != entrada' sao os mesmos do Fluxo de Caixa e do Historico Mensal."""
        entradas, saidas = db.session.query(
            func.coalesce(func.sum(case(
                (Transaction.type == 'entrada', func.abs(Transaction.amount)), else_=0.0)), 0.0),
            func.coalesce(func.sum(case(
                (Transaction.type != 'entrada', func.abs(Transaction.amount)), else_=0.0)), 0.0),
        ).filter(*filtros).one()
        return float(entradas or 0), float(saidas or 0)

    def gerar_resumo(self, tipo, inicio, fim):
        if tipo not in TIPOS_RESUMO:
            raise ValueError(f"Tipo de relatório inválido: {tipo}")
        d1, d2 = self._periodo(inicio, fim)

        if tipo == 'geral':
            corpo = self._resumo_geral(d1, d2)
        elif tipo == 'inadimplencia':
            corpo = self._resumo_inadimplencia(d1, d2)
        else:
            corpo = self._resumo_lucro(d1, d2)

        s = CompanySettings.query.first()
        corpo.update({
            'tipo': tipo,
            'empresa': (s.company_name if s and s.company_name else 'Fozesc'),
            'cnpj': (s.cnpj if s else None),
            'inicio': d1.strftime('%Y-%m-%d'),
            'fim': d2.strftime('%Y-%m-%d'),
            'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
        })
        return corpo

    # ------------------------------------------------------------------ #
    # Séries temporais (agregadas no banco, nunca em memória)
    # ------------------------------------------------------------------ #
    def _modo_tempo(self, d1, d2):
        dias = (d2 - d1).days
        if dias <= 45:
            return 'dia'
        if dias <= 1100:
            return 'mes'
        return 'ano'

    def _chaves(self, d1, d2, modo):
        """Todas as fatias do período, inclusive as sem movimento - senão a linha
        do gráfico 'pula' o mês parado e dá impressão de atividade que não houve."""
        chaves = []
        if modo == 'dia':
            d = d1
            while d <= d2:
                chaves.append(d.strftime('%Y-%m-%d'))
                d += timedelta(days=1)
        elif modo == 'mes':
            ano, mes = d1.year, d1.month
            while (ano, mes) <= (d2.year, d2.month):
                chaves.append(f'{ano}-{mes:02d}')
                mes += 1
                if mes > 12:
                    mes, ano = 1, ano + 1
        else:
            chaves = [str(a) for a in range(d1.year, d2.year + 1)]
        return chaves

    def _rotulo(self, chave, modo):
        if modo == 'dia':
            _a, m, d = chave.split('-')
            return f'{d}/{m}'
        if modo == 'mes':
            a, m = chave.split('-')
            return f'{m}/{a}'
        return chave

    def _serie(self, chaves, linhas):
        """Alinha [(chave, valor)] do banco com a lista cheia de chaves."""
        mapa = {str(k): float(v or 0) for k, v in linhas}
        return [round(mapa.get(k, 0.0), 2) for k in chaves]

    def _eixo_tempo(self, d1, d2):
        """(modo, chaves, rotulos) ou None quando daria pontos demais."""
        modo = self._modo_tempo(d1, d2)
        chaves = self._chaves(d1, d2, modo)
        if len(chaves) > MAX_PONTOS:
            return None
        return modo, chaves, [self._rotulo(k, modo) for k in chaves]

    def _barras_de(self, titulo, pares, cor):
        """Gráfico de barras a partir de [(rótulo, valor)]."""
        return {
            'tipo': 'barras',
            'titulo': titulo,
            'labels': [p[0] for p in pares],
            'series': [{'nome': titulo, 'dados': [round(float(p[1] or 0), 2) for p in pares],
                        'cor': cor}],
        }

    # ------------------------------------------------------------------ #
    # 1. Resumo de Caixa e Operações
    # ------------------------------------------------------------------ #
    def _resumo_geral(self, d1, d2):
        ant_e, ant_s = self._movimento(Transaction.date < d1)
        entradas, saidas = self._movimento(Transaction.date >= d1, Transaction.date <= d2)
        saldo_anterior = ant_e - ant_s
        resultado = entradas - saidas

        no_periodo = (Transaction.date >= d1, Transaction.date <= d2)
        entrada_abs = case((Transaction.type == 'entrada', func.abs(Transaction.amount)), else_=0.0)
        saida_abs = case((Transaction.type != 'entrada', func.abs(Transaction.amount)), else_=0.0)

        graficos = []
        eixo = self._eixo_tempo(d1, d2)
        if eixo:
            modo, chaves, rotulos = eixo
            quando = func.to_char(Transaction.date, FORMATO_TEMPO[modo])
            linhas = db.session.query(
                quando,
                func.coalesce(func.sum(entrada_abs), 0.0),
                func.coalesce(func.sum(saida_abs), 0.0),
            ).filter(*no_periodo).group_by(quando).all()
            graficos.append({
                'tipo': 'barras',
                'titulo': 'Entradas e saídas no período',
                'labels': rotulos,
                'series': [
                    {'nome': 'Entradas', 'dados': self._serie(chaves, [(k, e) for k, e, _s in linhas]), 'cor': VERDE},
                    {'nome': 'Saídas', 'dados': self._serie(chaves, [(k, sa) for k, _e, sa in linhas]), 'cor': VERMELHO},
                ],
            })

        # Saldo por conta no fim do período + movimento dentro dele.
        # bank_key é a MESMA regra do Fluxo de Caixa e do Histórico Mensal.
        contas = {k: {'nome': NOME_CONTA[k], 'saldo': 0.0, 'entradas': 0.0, 'saidas': 0.0}
                  for k in ('BRASIL', 'CAIXA', 'DINHEIRO')}
        for origem, val in db.session.query(
                Transaction.origin,
                func.sum(case((Transaction.type == 'entrada', func.abs(Transaction.amount)),
                              else_=-func.abs(Transaction.amount)))
        ).filter(Transaction.date <= d2).group_by(Transaction.origin).all():
            contas[bank_key(origem)]['saldo'] += float(val or 0)
        for origem, ent, sai in db.session.query(
                Transaction.origin,
                func.coalesce(func.sum(entrada_abs), 0.0),
                func.coalesce(func.sum(saida_abs), 0.0),
        ).filter(*no_periodo).group_by(Transaction.origin).all():
            c = contas[bank_key(origem)]
            c['entradas'] += float(ent or 0)
            c['saidas'] += float(sai or 0)

        graficos.append({
            'tipo': 'barras',
            'titulo': 'Saldo por conta no fim do período',
            'labels': [contas[k]['nome'] for k in ('BRASIL', 'CAIXA', 'DINHEIRO')],
            'series': [{'nome': 'Saldo', 'dados': [round(contas[k]['saldo'], 2)
                                                   for k in ('BRASIL', 'CAIXA', 'DINHEIRO')],
                        'cor': [COR_CONTA[k] for k in ('BRASIL', 'CAIXA', 'DINHEIRO')]}],
        })

        categorias = db.session.query(
            func.coalesce(Transaction.category, 'Sem categoria'),
            func.coalesce(func.sum(func.abs(Transaction.amount)), 0.0),
        ).filter(*no_periodo, Transaction.type != 'entrada')\
         .group_by(func.coalesce(Transaction.category, 'Sem categoria'))\
         .order_by(func.sum(func.abs(Transaction.amount)).desc()).limit(LIMITE_TOP).all()
        if categorias:
            graficos.append(self._barras_de('Maiores saídas por categoria', categorias, VERMELHO))

        return {
            'titulo': 'Resumo de Caixa e Operações',
            'linhas': [
                {'label': 'Saldo anterior ao período', 'valor': round(saldo_anterior, 2)},
                {'label': 'Entradas no período', 'valor': round(entradas, 2), 'sinal': 'positivo'},
                {'label': 'Saídas no período', 'valor': round(-saidas, 2), 'sinal': 'negativo'},
                {'label': 'Resultado do período', 'valor': round(resultado, 2), 'destaque': True},
                {'label': 'Saldo final', 'valor': round(saldo_anterior + resultado, 2), 'destaque': True},
            ],
            'destaques': [
                {'label': 'Entradas', 'valor': round(entradas, 2), 'sinal': 'positivo'},
                {'label': 'Saídas', 'valor': round(saidas, 2), 'sinal': 'negativo'},
                {'label': 'Resultado', 'valor': round(resultado, 2)},
                {'label': 'Saldo final', 'valor': round(saldo_anterior + resultado, 2)},
            ],
            'graficos': graficos,
            'tabelas': [{
                'titulo': 'Por conta',
                'colunas': [{'nome': 'Conta', 'tipo': 'texto'},
                            {'nome': 'Entradas', 'tipo': 'moeda'},
                            {'nome': 'Saídas', 'tipo': 'moeda'},
                            {'nome': 'Saldo no fim', 'tipo': 'moeda'}],
                'linhas': [[contas[k]['nome'], round(contas[k]['entradas'], 2),
                            round(contas[k]['saidas'], 2), round(contas[k]['saldo'], 2)]
                           for k in ('BRASIL', 'CAIXA', 'DINHEIRO')],
            }],
        }

    # ------------------------------------------------------------------ #
    # 2. Demonstrativo de Resultados
    # ------------------------------------------------------------------ #
    def _resumo_lucro(self, d1, d2):
        # Juros que ENTROU: cheque pago dentro do periodo. E' o mesmo criterio do KPI
        # "Juros de cheques ja pagos" do Dashboard (decisao do Lucas em 15/09/2026).
        recebido = db.session.query(
            func.coalesce(func.sum(Check.interest_amount), 0.0)
        ).filter(Check.status == 'Pago',
                 Check.fora_do_calculo.is_(False),
                 Check.payment_date >= d1, Check.payment_date <= d2).scalar() or 0.0

        # Juros GERADO: borderos operados no periodo. E' o criterio do Historico Mensal
        # (por data de operacao). Os dois aparecem no papel porque respondem perguntas
        # diferentes - quanto entrou x quanto foi vendido no periodo.
        gerado, operado, qtd = db.session.query(
            func.coalesce(func.sum(Check.interest_amount), 0.0),
            func.coalesce(func.sum(Check.amount), 0.0),
            func.count(func.distinct(Operation.id)),
        ).select_from(Check).join(Operation, Check.operation_id == Operation.id)\
         .filter(Operation.operation_date >= d1, Operation.operation_date <= d2,
                 Check.fora_do_calculo.is_(False)).one()

        ops_com_cheque = db.session.query(Check.operation_id).filter(
            Check.fora_do_calculo.is_(False)).distinct()
        iof = db.session.query(
            func.coalesce(func.sum(Operation.iof_amount), 0.0)
        ).filter(Operation.operation_date >= d1, Operation.operation_date <= d2,
                 Operation.id.in_(ops_com_cheque)).scalar() or 0.0

        graficos = []
        eixo = self._eixo_tempo(d1, d2)
        if eixo:
            modo, chaves, rotulos = eixo
            fmt = FORMATO_TEMPO[modo]

            quando_op = func.to_char(Operation.operation_date, fmt)
            por_operacao = db.session.query(
                quando_op, func.coalesce(func.sum(Check.interest_amount), 0.0)
            ).select_from(Check).join(Operation, Check.operation_id == Operation.id)\
             .filter(Operation.operation_date >= d1, Operation.operation_date <= d2,
                     Check.fora_do_calculo.is_(False)).group_by(quando_op).all()

            quando_pg = func.to_char(Check.payment_date, fmt)
            por_pagamento = db.session.query(
                quando_pg, func.coalesce(func.sum(Check.interest_amount), 0.0)
            ).filter(Check.status == 'Pago', Check.fora_do_calculo.is_(False),
                     Check.payment_date >= d1, Check.payment_date <= d2)\
             .group_by(quando_pg).all()

            graficos.append({
                'tipo': 'linha',
                'titulo': 'Juros no período',
                'labels': rotulos,
                'series': [
                    {'nome': 'Recebido (cheque pago)', 'dados': self._serie(chaves, por_pagamento), 'cor': VERDE},
                    {'nome': 'Gerado (borderô operado)', 'dados': self._serie(chaves, por_operacao), 'cor': AZUL},
                ],
            })

        top_clientes = db.session.query(
            Client.name, func.coalesce(func.sum(Check.interest_amount), 0.0)
        ).select_from(Check).join(Operation, Check.operation_id == Operation.id)\
         .join(Client, Operation.client_id == Client.id)\
         .filter(Operation.operation_date >= d1, Operation.operation_date <= d2,
                 Check.fora_do_calculo.is_(False))\
         .group_by(Client.name)\
         .order_by(func.sum(Check.interest_amount).desc()).limit(LIMITE_TOP).all()
        if top_clientes:
            graficos.append(self._barras_de('Clientes que mais geraram juros', top_clientes, AZUL))

        return {
            'titulo': 'Demonstrativo de Resultados (Lucro)',
            'linhas': [
                {'label': 'Juros de cheques recebidos no período', 'valor': round(float(recebido), 2),
                 'sinal': 'positivo', 'destaque': True},
                {'label': 'Juros gerado em borderôs operados no período', 'valor': round(float(gerado), 2)},
                {'label': 'IOF cobrado nos borderôs do período', 'valor': round(float(iof), 2)},
                {'label': 'Valor de face operado no período', 'valor': round(float(operado), 2)},
                {'label': 'Borderôs operados no período', 'valor': int(qtd), 'formato': 'numero'},
            ],
            'destaques': [
                {'label': 'Juros recebido', 'valor': round(float(recebido), 2), 'sinal': 'positivo'},
                {'label': 'Juros gerado', 'valor': round(float(gerado), 2)},
                {'label': 'Face operado', 'valor': round(float(operado), 2)},
                {'label': 'Borderôs', 'valor': int(qtd), 'formato': 'numero'},
            ],
            'graficos': graficos,
            'tabelas': [{
                'titulo': 'Maiores clientes do período (por juros gerado)',
                'colunas': [{'nome': 'Cliente', 'tipo': 'texto'},
                            {'nome': 'Juros gerado', 'tipo': 'moeda'}],
                'linhas': [[nome, round(float(v or 0), 2)] for nome, v in top_clientes],
            }] if top_clientes else [],
        }

    # ------------------------------------------------------------------ #
    # 3. Cheques em atraso
    # ------------------------------------------------------------------ #
    def _resumo_inadimplencia(self, d1, d2):
        filtros = (Check.status.in_(STATUS_INADIMPLENTE),
                   Check.fora_do_calculo.is_(False),
                   Check.due_date >= d1, Check.due_date <= d2)

        total, qtd = db.session.query(
            func.coalesce(func.sum(Check.amount), 0.0), func.count(Check.id)
        ).filter(*filtros).one()

        por_status = db.session.query(
            Check.status, func.coalesce(func.sum(Check.amount), 0.0)
        ).filter(*filtros).group_by(Check.status).all()
        mapa = {s: float(v or 0) for s, v in por_status}

        # Colunas explicitas (nao SELECT *) e join unico - sem N+1 para pegar o cliente.
        linhas = db.session.query(
            Check.due_date, Check.number, Check.issuer_name, Check.amount,
            Check.status, Client.name
        ).select_from(Check).join(Operation, Check.operation_id == Operation.id)\
         .join(Client, Operation.client_id == Client.id)\
         .filter(*filtros).order_by(Check.due_date.asc(), Check.id.asc())\
         .limit(LIMITE_ITENS).all()

        top_devedores = db.session.query(
            Client.name, func.coalesce(func.sum(Check.amount), 0.0)
        ).select_from(Check).join(Operation, Check.operation_id == Operation.id)\
         .join(Client, Operation.client_id == Client.id).filter(*filtros)\
         .group_by(Client.name)\
         .order_by(func.sum(Check.amount).desc()).limit(LIMITE_TOP).all()

        graficos = []
        presentes = [s for s in STATUS_INADIMPLENTE if mapa.get(s, 0) > 0]
        # pizza de uma fatia so e' um circulo cheio: nao diz nada que o total ja nao diga
        if len(presentes) > 1:
            graficos.append({
                'tipo': 'pizza',
                'titulo': 'Atraso por situação',
                'labels': presentes,
                'series': [{'nome': 'Em atraso',
                            'dados': [round(mapa[s], 2) for s in presentes],
                            'cor': [COR_STATUS[s] for s in presentes]}],
            })
        if top_devedores:
            graficos.append(self._barras_de('Maiores devedores', top_devedores, VERMELHO))

        return {
            'titulo': 'Relatório de Cheques em Atraso',
            'linhas': [
                {'label': s, 'valor': round(mapa.get(s, 0.0), 2)} for s in STATUS_INADIMPLENTE
            ] + [
                {'label': 'Total em atraso', 'valor': round(float(total or 0), 2),
                 'sinal': 'negativo', 'destaque': True},
                {'label': 'Quantidade de cheques', 'valor': int(qtd), 'formato': 'numero'},
            ],
            'destaques': [
                {'label': 'Total em atraso', 'valor': round(float(total or 0), 2), 'sinal': 'negativo'},
                {'label': 'Cheques', 'valor': int(qtd), 'formato': 'numero'},
                {'label': 'Clientes', 'valor': len(top_devedores), 'formato': 'numero'},
                {'label': 'Maior atraso', 'valor': round(float(top_devedores[0][1]), 2)
                 if top_devedores else 0.0},
            ],
            'graficos': graficos,
            'tabelas': [{
                'titulo': 'Cheques do período',
                'colunas': [{'nome': 'Vencimento', 'tipo': 'data'},
                            {'nome': 'Cheque', 'tipo': 'texto'},
                            {'nome': 'Emitente', 'tipo': 'texto'},
                            {'nome': 'Cliente', 'tipo': 'texto'},
                            {'nome': 'Situação', 'tipo': 'texto'},
                            {'nome': 'Valor', 'tipo': 'moeda'}],
                'linhas': [[v.strftime('%Y-%m-%d') if v else '', num or '', emi or '',
                            cli or '', st, float(val or 0)]
                           for v, num, emi, val, st, cli in linhas],
                'total_linhas': int(qtd),
                'truncado': int(qtd) > LIMITE_ITENS,
            }],
        }

    def gerar_relatorio_customizado(self, tipo, start_date=None, end_date=None):
        folder = self._get_path()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Nome do arquivo
        prefix = "BACKUP_COMPLETO" if tipo == 'legacy' else f"Relatorio_{tipo.upper()}"
        filename = f"{prefix}_{timestamp}.xlsx"
        filepath = os.path.join(folder, filename)
        
        writer = pd.ExcelWriter(filepath, engine='openpyxl')
        has_data = False

        # ==============================================================================
        # 1. ABA DE CHEQUES (Presente em 'legacy', 'geral' e 'cheques')
        # ==============================================================================
        if tipo in ['legacy', 'geral', 'cheques']:
            # Query Completa
            query = db.session.query(Check, Operation, Client)\
                .select_from(Check)\
                .join(Operation)\
                .join(Client)
            
            if start_date: query = query.filter(Check.due_date >= start_date)
            if end_date: query = query.filter(Check.due_date <= end_date)
            
            data_cheques = []
            for c, op, cli in query.all():
                
                # Tradução de Status para o padrão da planilha antiga (se for legacy)
                if tipo == 'legacy':
                    status_str = 'Pendente'
                    if c.status == 'Pago': status_str = 'Cobrado'
                    elif c.status == 'Juridico': status_str = 'Jurídico'
                    elif c.status == 'Devolvido': status_str = 'Devolvido'
                    elif c.status == 'Atrasado': status_str = 'Pendente'
                else:
                    status_str = c.status # Usa o status real do sistema nos outros relatórios

                item = {
                    'ID': c.id,
                    'Dt Operação': self._format_date(op.operation_date),
                    'Vencimento': self._format_date(c.due_date),
                    'Data PGTO': self._format_date(c.payment_date),
                    'Cliente': cli.name,
                    'Banco': c.bank,
                    'Emitente': c.issuer_name,
                    'Nº Doc': c.number,
                    'Destino': c.destination_bank,
                    # Valores Numéricos (Sem R$ para permitir soma no Excel)
                    'Valor Bruto (v)': float(c.amount or 0),
                    'Juros': float(c.interest_amount or 0),
                    'Valor Líquido': float(c.net_amount or 0),
                    'Status': status_str,
                    'Observação': op.notes or "",
                    'Forma': '' # Campo legado vazio
                }
                
                # Se for LEGACY, renomeia as chaves para bater EXATAMENTE com o importador
                if tipo == 'legacy':
                    item['v'] = item.pop('Valor Bruto (v)')
                    item['cobrado'] = item.pop('Status')
                    item['observação'] = item.pop('Observação')
                    # Remove ID para não confundir importador antigo se não precisar
                    # Mas se quiser manter ID, deixe. O importador ignora colunas extras.
                
                data_cheques.append(item)
            
            if data_cheques:
                df = pd.DataFrame(data_cheques)
                sheet_name = "Listado de cheques" if tipo == 'legacy' else "Cheques Detalhados"
                df.to_excel(writer, sheet_name=sheet_name, index=False)
                has_data = True

        # ==============================================================================
        # 2. ABA FLUXO DE CAIXA (Presente em 'geral' e 'fluxo')
        # ==============================================================================
        if tipo in ['geral', 'fluxo']:
            query = Transaction.query
            if start_date: query = query.filter(Transaction.date >= start_date)
            if end_date: query = query.filter(Transaction.date <= end_date)

            data_fluxo = []
            for t in query.all():
                data_fluxo.append({
                    'ID': t.id,
                    'Data': self._format_date(t.date),
                    'Descrição': t.description,
                    'Categoria': t.category,
                    'Origem/Conta': t.origin,
                    'Tipo': t.type.upper(),
                    'Entrada': float(t.amount) if t.type == 'entrada' else 0,
                    'Saída': float(t.amount) if t.type == 'saida' else 0,
                    'Valor Total': float(t.amount)
                })

            if data_fluxo:
                df = pd.DataFrame(data_fluxo)
                df.to_excel(writer, sheet_name='Fluxo de Caixa', index=False)
                has_data = True

        # ==============================================================================
        # 3. ABA CLIENTES (Presente em 'geral' e 'clientes')
        # ==============================================================================
        if tipo in ['geral', 'clientes']:
            data_clientes = []
            for cli in Client.query.all():
                # Conta quantos cheques esse cliente tem
                qtd_cheques = len(cli.operations) # Aproximado por operações
                
                data_clientes.append({
                    'ID': cli.id,
                    'Nome': cli.name,
                    'Telefone': cli.phone,
                    'Documento': cli.document if hasattr(cli, 'document') else '',
                    'Observações': cli.notes,
                    'Qtd Operações': qtd_cheques
                })
            
            if data_clientes:
                df = pd.DataFrame(data_clientes)
                df.to_excel(writer, sheet_name='Base Clientes', index=False)
                has_data = True

        if not has_data:
            writer.close()
            return None

        writer.close()
        return filepath