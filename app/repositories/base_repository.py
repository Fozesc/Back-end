from app import db

class BaseRepository:
    def __init__(self, model):
        self.model = model

    def get_all(self):
        return self.model.query.all()

    def get_by_id(self, id):
        return self.model.query.get(id)


    def create(self, data):
    
        if isinstance(data, dict):
            entity = self.model(**data)
        else:
   
            entity = data
        
        db.session.add(entity)
        db.session.commit()
        return entity


    def update(self, entity):

        db.session.commit()
        return entity

    def delete(self, id_or_entity):
        if isinstance(id_or_entity, int):
            entity = self.get_by_id(id_or_entity)
        else:
            entity = id_or_entity
        
        if entity:
            db.session.delete(entity)
            db.session.commit()
            return True
        return False