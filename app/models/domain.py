from app import db
from datetime import datetime

# --- CONFIGURAÇÕES DO SISTEMA ---

class CompanySettings(db.Model):
    __tablename__ = 'company_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # Dados da Empresa (Para relatórios/impressão)
    company_name = db.Column(db.String(100), default="Minha Fatoring")
    cnpj = db.Column(db.String(20))
    phone = db.Column(db.String(20))
    address = db.Column(db.String(200))
    
    # Taxas e Cálculos Padrão
    default_monthly_rate = db.Column(db.Float, default=4.0)       # Taxa Padrão Mensal
    default_compensation_days = db.Column(db.Integer, default=2)  # Dias Comp.
    
    # Configurações Avançadas (Automação)
    iof_rate = db.Column(db.Float, default=0.38)         # IOF Fixo (0.38%)
    iof_daily_rate = db.Column(db.Float, default=0.0082) # IOF Diário
    extension_rate = db.Column(db.Float, default=4.0)    # Juros padrão p/ Prorrogação
    fine_rate = db.Column(db.Float, default=2.0)         # Multa p/ Devolução (2%)
    
    # Saldos Iniciais (Caixa)
    capital_social = db.Column(db.Float, default=0.0)
    saldo_inicial_bb = db.Column(db.Float, default=0.0)
    saldo_inicial_ce = db.Column(db.Float, default=0.0)
    saldo_inicial_caixa = db.Column(db.Float, default=0.0)

# --- USUÁRIOS (AUTH) ---
class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default='Operador') 
    active = db.Column(db.Boolean, default=True)

# --- AUDITORIA ---
class AuditLog(db.Model):
    __tablename__ = 'audit_logs'
    id = db.Column(db.Integer, primary_key=True)
    user_name = db.Column(db.String(100))
    action = db.Column(db.String(50))     
    target = db.Column(db.String(50))     
    description = db.Column(db.Text)      
    timestamp = db.Column(db.DateTime, default=datetime.now)

# --- CLIENTE ---
class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    document = db.Column(db.String(20)) 
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    
    # ### NOVO: Endereço Detalhado ###
    address = db.Column(db.String(200)) # Rua
    neighborhood = db.Column(db.String(100)) # Bairro
    city = db.Column(db.String(100)) # Cidade
    state = db.Column(db.String(2))  # UF
    zip_code = db.Column(db.String(20)) # CEP
    # ----------------------------------

    credit_limit = db.Column(db.Float, default=0.0)
    standard_rate = db.Column(db.Float, default=4.0)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.now)

# --- OPERAÇÃO (BORDERÔ) ---
class Operation(db.Model):
    __tablename__ = 'operations'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=False)
    client_name_snapshot = db.Column(db.String(100))
    operation_date = db.Column(db.Date, nullable=False)
    monthly_rate = db.Column(db.Float, default=0.0) 
    compensation_days = db.Column(db.Integer, default=0)
    # valores totais da operacao

    iof_amount = db.Column(db.Float, default=0.0)
    total_face_value = db.Column(db.Float, default=0.0) 
    total_interest = db.Column(db.Float, default=0.0)
    total_net_value = db.Column(db.Float, default=0.0)
    notes = db.Column(db.Text, nullable=True)
    account_source = db.Column(db.String(50), default='Dinheiro')
    created_at = db.Column(db.DateTime, default=datetime.now)
    #(Aberto, Finalizada, Cancelada)
    status = db.Column(db.String(20), default='Finalizada')
    # cliente para operacao
    client = db.relationship('Client', backref=db.backref('operations', lazy=True))



# --- CHEQUE ---
class Check(db.Model):
    __tablename__ = 'checks'
    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey('operations.id'), nullable=False)
    

    type = db.Column(db.String(20), default='CHEQUE') 


    bank = db.Column(db.String(50))
    number = db.Column(db.String(50))
    
   
    original_due_date = db.Column(db.Date, nullable=True) 
  

    due_date = db.Column(db.Date, nullable=False) # Essa data muda se prorrogar
    
    amount = db.Column(db.Float, nullable=False)        # Valor de Face
    interest_amount = db.Column(db.Float, default=0.0)  # Juros originais
    net_amount = db.Column(db.Float, default=0.0)       # Valor Líquido
    
    days = db.Column(db.Integer, default=0)           
    status = db.Column(db.String(20), default='Aguardando')
    destination_bank = db.Column(db.String(50), default='Carteira')
    issuer_name = db.Column(db.String(100))
    
 
    payment_date = db.Column(db.Date, nullable=True) 
    payment_method = db.Column(db.String(50)) # Ex: 'DINHEIRO', 'PIX'
    paid_amount = db.Column(db.Float, default=0.0) # Quanto pagou de verdade
    fine_amount = db.Column(db.Float, default=0.0) # Multas cobradas (se devolvido)


    operation = db.relationship('Operation', backref=db.backref('checks', lazy=True, cascade="all, delete-orphan"))

# --- TRANSAÇÃO FINANCEIRA (FLUXO DE CAIXA) ---
class Transaction(db.Model):
    __tablename__ = 'transactions'
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    type = db.Column(db.String(10), nullable=False) 
    origin = db.Column(db.String(50)) 
    category = db.Column(db.String(50))
    operation_id = db.Column(db.Integer, db.ForeignKey('operations.id'), nullable=True)


class CheckExtension(db.Model):
    __tablename__ = 'check_extensions'
    
    id = db.Column(db.Integer, primary_key=True)
    check_id = db.Column(db.Integer, db.ForeignKey('checks.id'), nullable=False)
    
    prorrogation_date = db.Column(db.DateTime, default=datetime.now) # Quando prorrogou
    
    old_due_date = db.Column(db.Date, nullable=False) # Data que estava antes
    new_due_date = db.Column(db.Date, nullable=False) # Data nova combinada
    
    days_added = db.Column(db.Integer, default=0) # Quantos dias ganhou
    fee_amount = db.Column(db.Float, default=0.0) # Juros cobrados nessa prorrogação
    
    status = db.Column(db.String(20), default='PENDENTE') # PENDENTE ou PAGO
    notes = db.Column(db.Text)

  
    check = db.relationship('Check', backref=db.backref('extensions', lazy=True, cascade="all, delete-orphan"))

#block de logout
class TokenBlocklist(db.Model):
    __tablename__ = 'token_blocklist'
    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(36), nullable=False, index=True) 
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)