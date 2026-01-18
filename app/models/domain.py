from app import db
from sqlalchemy.sql import func
from datetime import datetime
from app import db, bcrypt

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())

  
    def set_password(self, password):
        self.password_hash = bcrypt.generate_password_hash(password).decode('utf-8')

 
    def check_password(self, password):
        return bcrypt.check_password_hash(self.password_hash, password)

class Client(db.Model):
    __tablename__ = 'clients'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    document = db.Column(db.String(20))
    phone = db.Column(db.String(20))
    address = db.Column(db.String(200))
    credit_limit = db.Column(db.Float, default=0.0)
    standard_rate = db.Column(db.Float, default=4.00)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    operations = db.relationship('Operation', backref='client', lazy=True)

class Operation(db.Model):
    __tablename__ = 'operations'
    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey('clients.id'), nullable=True)
    client_name_snapshot = db.Column(db.String(100), nullable=False)
    operation_date = db.Column(db.Date, nullable=False)
    total_gross = db.Column(db.Float, default=0.0)
    total_net = db.Column(db.Float, default=0.0)
    applied_rate = db.Column(db.Float)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    checks = db.relationship('Check', backref='operation', lazy=True, cascade="all, delete-orphan")

class Check(db.Model):
    __tablename__ = 'checks'
    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey('operations.id'), nullable=False)
    bank = db.Column(db.String(50))
    number = db.Column(db.String(50))
    due_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default='Aguardando')
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())

#

class AuditLog(db.Model):
    __tablename__ = 'audit_logs'

    id = db.Column(db.Integer, primary_key=True)
    

    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    user_name = db.Column(db.String(100))
    

    action = db.Column(db.String(50), nullable=False) 
    target_type = db.Column(db.String(50)) 
    target_id = db.Column(db.String(50))   
    

    details = db.Column(db.Text) 
    ip_address = db.Column(db.String(50))
    user_agent = db.Column(db.String(200)) 
    
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())