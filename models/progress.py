from . import db

class Progress(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String, nullable=False)
    message = db.Column(db.String, nullable=False)
    is_done = db.Column(db.Boolean, default=False)
    phase = db.Column(db.String, nullable=False, default="phase1")