from app import ma
from app.models.domain import Transaction

class TransactionSchema(ma.SQLAlchemyAutoSchema):
    class Meta:
        model = Transaction
        load_instance = True
        include_fk = True

transaction_schema = TransactionSchema()
transactions_schema = TransactionSchema(many=True)