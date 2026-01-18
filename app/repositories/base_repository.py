from app import db

class BaseRepository:
    def __init__(self, model):
        self.model = model

    def get_all(self):
        return self.model.query.all()

    def get_by_id(self, id):
        return self.model.query.get(id)

    def create(self, data):
        entity = self.model(**data)
        db.session.add(entity)
        db.session.commit()
        return entity

    def update(self, id, data):
        entity = self.get_by_id(id)
        if not entity:
            return None
        for key, value in data.items():
            setattr(entity, key, value)
        db.session.commit()
        return entity

    def delete(self, id):
        entity = self.get_by_id(id)
        if entity:
            db.session.delete(entity)
            db.session.commit()
            return True
        return False