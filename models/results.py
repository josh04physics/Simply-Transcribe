# models/results.py
from . import db

class Results(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False, unique=True)
    transcript = db.Column(db.Text, nullable=True)
    summary = db.Column(db.Text, nullable=True)
    outputs = db.Column(db.JSON, nullable=False)
    zip_ready = db.Column(db.Boolean, default=False)
    zip_data = db.Column(db.LargeBinary, nullable=True)