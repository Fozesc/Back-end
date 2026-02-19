import pandas as pd
import os
from datetime import datetime
from app import create_app, db
from app.models.domain import Client, Operation, Check

# --- CONFIGURAÇÃO ---
EXCEL_FILE = "cheques.xlsx"
SHEET_NAME = "Listado de cheques"

# Cache APENAS para Clientes (acelera a busca sem perder dados dos cheques)
clientes_cache = {} 

def parse_money(value):
    if pd.isna(value) or str(value).strip() in ['-', '', 'nan', 'NaT']: return 0.0
    if isinstance(value, (int, float)): return float(value)
    try:
        val = str(value).replace('R$', '').replace(' ', '').replace('.', '').replace(',', '.')
        return float(val)
    except:
        return 0.0

def parse_date(value):
    if pd.isna(value) or str(value).strip() in ['-', '', 'nan', 'NaT']: return None
    if isinstance(value, (datetime, pd.Timestamp)): return value.date()
    text = str(value).strip()
    for fmt in ('%d/%m/%Y', '%m/%d/%Y', '%Y-%m-%d', '%d-%m-%y', '%Y/%m/%d'):
        try: return datetime.strptime(text, fmt).date()
        except ValueError: continue
    return None

def clean_str(value):
    if pd.isna(value) or str(value).lower() == 'nan': return ""
    return str(value).strip()

def get_or_create_client(name):
    # Verifica cache
    if name in clientes_cache:
        return clientes_cache[name]
    
    # Verifica banco
    client = Client.query.filter(Client.name.ilike(name)).first()
    
    # Cria se não existir
    if not client:
        client = Client(name=name, notes="Importado via planilha")
        db.session.add(client)
        db.session.flush()
    
    clientes_cache[name] = client
    return client

def run_import():
    if not os.path.exists(EXCEL_FILE):
        print(f"❌ ARQUIVO NÃO ENCONTRADO: {EXCEL_FILE}")
        return

    app = create_app()
    with app.app_context():
        print(f"📂 Lendo planilha...")
        try:
            df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME)
            df.columns = df.columns.str.strip()
        except Exception as e:
            print(f"❌ Erro ao abrir Excel: {e}")
            return

        sucesso = 0
        relatorio_erros = []

        print(f"🚀 Iniciando processamento de {len(df)} linhas...\n")

        for index, row in df.iterrows():
            linha = index + 2
            try:
                # --- 1. DADOS BÁSICOS ---
                nome_cliente = clean_str(row.get('Cliente'))
                if not nome_cliente: continue

                bruto = parse_money(row.get('v'))
                if bruto <= 0: continue

                dt_venc = parse_date(row.get('Vencimento'))
                if not dt_venc:
                    raise ValueError(f"Data de Vencimento inválida: {row.get('Vencimento')}")

                # --- 2. CÁLCULOS ---
                juros = parse_money(row.get('Juros'))
                liquido = parse_money(row.get('Valor Liquido'))

                if liquido == 0 and bruto > 0:
                    liquido = bruto - juros
                elif juros == 0 and bruto > liquido and liquido > 0:
                    juros = bruto - liquido
                
                if juros < 0: juros = 0

                dt_pgto = parse_date(row.get('Data PGTO'))
                dt_criacao = parse_date(row.get('Dt Operação')) or datetime.now().date()

                # --- 3. STATUS INTELIGENTE ---
                raw_cobrado = clean_str(row.get('cobrado')).lower()
                status = 'Aguardando'
                
                if 'jurídico' in raw_cobrado or 'juridico' in raw_cobrado: status = 'Juridico'
                elif 'devolvido' in raw_cobrado: status = 'Devolvido'
                elif 'cobrado' in raw_cobrado or 'pago' in raw_cobrado: status = 'Pago'
                elif 'pendente' in raw_cobrado:
                    if dt_venc < datetime.now().date(): status = 'Atrasado'
                    else: status = 'Aguardando'
                
                if dt_pgto and status != 'Pago': status = 'Pago'

                # --- 4. RECUPERAÇÃO DAS OBSERVAÇÕES (Restaurado) ---
                infos = []
                forma = clean_str(row.get('Forma'))
                obs_excel = clean_str(row.get('observação'))
                
                if forma: infos.append(f"Forma: {forma}")
                if obs_excel: infos.append(f"Obs: {obs_excel}")
                
                # Validação de divergência matemática (Opcional, mas útil)
                diff = abs((bruto - juros) - liquido)
                if diff > 0.05:
                    infos.append(f"Nota: Divergência cálculo ({bruto}-{juros}!={liquido})")

                obs_final = " | ".join(infos) if infos else "Importado via planilha"

                # --- 5. BANCO DE DADOS ---
                
                # Busca Cliente (Rápido via Cache)
                client = get_or_create_client(nome_cliente)
                
                # Cria UMA Operação POR Cheque (Para poder salvar a observação específica)
                op = Operation(
                    client_id=client.id,
                    operation_date=dt_criacao,
                    notes=obs_final[:500], # <--- AQUI ESTÁ A OBSERVAÇÃO DE VOLTA
                    status='Finalizada',
                    total_face_value=bruto,
                    total_interest=juros,
                    total_net_value=liquido
                )
                db.session.add(op)
                db.session.flush()

                # Cria o Cheque vinculado
                check = Check(
                    operation_id=op.id,
                    amount=bruto,
                    interest_amount=juros,
                    net_amount=liquido,
                    due_date=dt_venc,
                    payment_date=dt_pgto,
                    status=status,
                    bank=clean_str(row.get('Banco'))[:50], # <--- BANCO ESTÁ AQUI
                    issuer_name=clean_str(row.get('Emitente'))[:100],
                    number=clean_str(row.get('Nº Doc'))[:30],
                    destination_bank=clean_str(row.get('Destino'))[:50]
                )
                db.session.add(check)
                sucesso += 1

                if sucesso % 500 == 0:
                    db.session.commit()
                    print(f"   ... {sucesso} registros processados ...")

            except Exception as e:
                # Ignora erros individuais para não parar tudo
                msg_erro = str(e)
                relatorio_erros.append(f"Linha {linha}: {msg_erro}")

        # --- FINALIZAÇÃO ---
        try:
            db.session.commit()
            print("\n" + "="*60)
            print(f"✅ IMPORTAÇÃO FINALIZADA")
            print(f"📥 Cheques importados: {sucesso}")
            print(f"⚠️ Erros ignorados: {len(relatorio_erros)}")
            print("="*60)

        except Exception as e:
            print(f"❌ Erro fatal ao salvar banco: {e}")

if __name__ == "__main__":
    run_import()