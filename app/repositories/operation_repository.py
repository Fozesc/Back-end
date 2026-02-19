from app.models.domain import Operation
from app import db
from .base_repository import BaseRepository

class OperationRepository(BaseRepository):
    def __init__(self):
        super().__init__(Operation)

    def create_with_transactions(self, operation, transactions):
        try:
    
            db.session.add(operation)
            db.session.flush() 
            
            for t in transactions:
                t.operation_id = operation.id
                db.session.add(t)
            
            db.session.commit()
            return operation
        except Exception as e:
            db.session.rollback()
            raise e