from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    total_matches = db.Column(db.Integer, default=0)
    total_wins = db.Column(db.Integer, default=0)
    total_losses = db.Column(db.Integer, default=0)
    total_draws = db.Column(db.Integer, default=0)
    tournaments_played = db.Column(db.Integer, default=0)
    tournament_wins = db.Column(db.Integer, default=0)
    elo_rating = db.Column(db.Integer, default=1200)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'total_matches': self.total_matches,
            'total_wins': self.total_wins,
            'total_losses': self.total_losses,
            'total_draws': self.total_draws,
            'tournaments_played': self.tournaments_played,
            'tournament_wins': self.tournament_wins,
            'elo_rating': self.elo_rating
        }


class Tournament(db.Model):
    __tablename__ = 'tournaments'
    id = db.Column(db.Integer, primary_key=True)
    room_code = db.Column(db.String(8), unique=True, nullable=False)
    admin_username = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default='waiting')  # waiting, active, completed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    participants_json = db.Column(db.Text, default='[]')
    winner_username = db.Column(db.String(50), nullable=True)
    current_round = db.Column(db.String(30), default='')
    rounds_json = db.Column(db.Text, default='[]')

    @property
    def participants(self):
        return json.loads(self.participants_json)

    @participants.setter
    def participants(self, value):
        self.participants_json = json.dumps(value)

    @property
    def rounds(self):
        return json.loads(self.rounds_json)

    @rounds.setter
    def rounds(self, value):
        self.rounds_json = json.dumps(value)

    def to_dict(self):
        return {
            'id': self.id,
            'room_code': self.room_code,
            'admin_username': self.admin_username,
            'status': self.status,
            'created_at': self.created_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
            'participants': self.participants,
            'winner_username': self.winner_username,
            'current_round': self.current_round,
            'rounds': self.rounds
        }


class Match(db.Model):
    __tablename__ = 'matches'
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey('tournaments.id'), nullable=False)
    round_name = db.Column(db.String(30), nullable=False)
    white_player = db.Column(db.String(50), nullable=False)
    black_player = db.Column(db.String(50), nullable=False)
    winner = db.Column(db.String(50), nullable=True)
    result = db.Column(db.String(20), nullable=True)  # checkmate, timeout, draw, resignation
    time_control = db.Column(db.Integer, default=300)  # seconds
    status = db.Column(db.String(20), default='pending')  # pending, active, completed
    pgn = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'tournament_id': self.tournament_id,
            'round_name': self.round_name,
            'white_player': self.white_player,
            'black_player': self.black_player,
            'winner': self.winner,
            'result': self.result,
            'time_control': self.time_control,
            'status': self.status,
            'created_at': self.created_at.isoformat(),
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }
