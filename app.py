import os
import json
import random
import string
import math
from datetime import datetime
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from models import db, User, Tournament, Match

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'chess-tournament-secret-key-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=1e6,
    logger=False,
    engineio_logger=False,
    transports=['websocket', 'polling']  # fallback to polling if websocket blocked
)

# In-memory state for active rooms and matches
rooms = {}
# rooms[room_code] = {
#   'tournament_id': int,
#   'admin': str,
#   'players': {username: sid},
#   'match_requests': {(requester, opponent): time_control},
#   'active_matches': {match_id: {...match state...}},
#   'bracket': [...],
#   'status': 'waiting'|'active'|'completed'
# }

active_matches = {}
# active_matches[match_id] = {
#   'room_code': str,
#   'white': str, 'black': str,
#   'white_time': int, 'black_time': int,
#   'turn': 'w'|'b',
#   'fen': str,
#   'timer_task': ...,
#   'status': 'active'
# }


def generate_room_code():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))


def get_round_name(num_players):
    if num_players <= 2:
        return 'Final'
    elif num_players <= 4:
        return 'Semi Final'
    elif num_players <= 8:
        return 'Quarter Final'
    else:
        return 'Round of 16'


def generate_bracket(players):
    """Generate bracket pairings from list of players"""
    random.shuffle(players)
    pairs = []
    for i in range(0, len(players) - 1, 2):
        pairs.append((players[i], players[i+1]))
    if len(players) % 2 == 1:
        # Bye for last player
        pairs.append((players[-1], 'BYE'))
    return pairs


def calculate_elo(winner_rating, loser_rating, k=32):
    expected_winner = 1 / (1 + 10 ** ((loser_rating - winner_rating) / 400))
    expected_loser = 1 - expected_winner
    new_winner = round(winner_rating + k * (1 - expected_winner))
    new_loser = round(loser_rating + k * (0 - expected_loser))
    return new_winner, new_loser


def emit_room_update(room_code):
    room = rooms.get(room_code)
    if not room:
        return
    tournament = Tournament.query.get(room['tournament_id'])
    players_info = []
    for username in room['players']:
        user = User.query.filter_by(username=username).first()
        if user:
            players_info.append(user.to_dict())
    
    data = {
        'players': players_info,
        'admin': room['admin'],
        'status': room['status'],
        'bracket': room.get('bracket', []),
        'current_round': tournament.current_round if tournament else '',
        'tournament_id': room['tournament_id']
    }
    socketio.emit('room_update', data, room=room_code)


def emit_leaderboard(room_code):
    room = rooms.get(room_code)
    if not room:
        return
    tournament = Tournament.query.get(room['tournament_id'])
    if not tournament:
        return
    
    matches = Match.query.filter_by(tournament_id=room['tournament_id']).all()
    leaderboard = {
        'current_round': tournament.current_round,
        'matches': [m.to_dict() for m in matches],
        'bracket': room.get('bracket', []),
        'status': tournament.status,
        'winner': tournament.winner_username
    }
    socketio.emit('leaderboard_update', leaderboard, room=room_code)


def check_round_complete(room_code):
    """Check if current round is complete and advance if so"""
    room = rooms.get(room_code)
    if not room:
        return
    
    tournament = Tournament.query.get(room['tournament_id'])
    if not tournament or tournament.status != 'active':
        return
    
    current_round_matches = Match.query.filter_by(
        tournament_id=room['tournament_id'],
        round_name=tournament.current_round
    ).all()
    
    # Check if all matches in current round are complete
    all_complete = all(m.status == 'completed' for m in current_round_matches)
    if not all_complete:
        return
    
    # Collect winners
    winners = []
    for m in current_round_matches:
        if m.winner and m.winner != 'BYE':
            winners.append(m.winner)
    
    if len(winners) == 1:
        # Tournament complete
        tournament.status = 'completed'
        tournament.winner_username = winners[0]
        tournament.completed_at = datetime.utcnow()
        room['status'] = 'completed'
        
        winner_user = User.query.filter_by(username=winners[0]).first()
        if winner_user:
            winner_user.tournament_wins += 1
        
        for username in room['players']:
            user = User.query.filter_by(username=username).first()
            if user:
                user.tournaments_played += 1
        
        db.session.commit()
        
        socketio.emit('tournament_complete', {
            'winner': winners[0],
            'message': f'🏆 {winners[0]} wins the tournament!'
        }, room=room_code)
        emit_leaderboard(room_code)
        return
    
    # Start next round
    round_name = get_round_name(len(winners))
    tournament.current_round = round_name
    
    pairs = generate_bracket(winners)
    bracket = []
    
    for white, black in pairs:
        if black == 'BYE':
            # Auto-advance
            bracket.append({
                'white': white, 'black': 'BYE',
                'winner': white, 'status': 'completed',
                'match_id': None
            })
            continue
        
        match = Match(
            tournament_id=room['tournament_id'],
            round_name=round_name,
            white_player=white,
            black_player=black,
            time_control=room.get('default_time', 300),
            status='pending'
        )
        db.session.add(match)
        db.session.flush()
        
        bracket.append({
            'white': white, 'black': black,
            'winner': None, 'status': 'pending',
            'match_id': match.id
        })
    
    db.session.commit()
    room['bracket'] = bracket
    rounds = tournament.rounds
    rounds.append({'round': round_name, 'pairs': [(p[0], p[1]) for p in pairs]})
    tournament.rounds = rounds
    db.session.commit()
    
    socketio.emit('new_round', {
        'round_name': round_name,
        'bracket': bracket,
        'message': f'🎯 {round_name} begins!'
    }, room=room_code)
    emit_leaderboard(room_code)


# =================== ROUTES ===================

@app.route('/')
def index():
    if 'username' in session:
        return redirect(url_for('lobby'))
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        if not username or len(username) < 2 or len(username) > 30:
            return render_template('login.html', error='Username must be 2-30 characters')
        
        # Sanitize
        username = ''.join(c for c in username if c.isalnum() or c in '_-')
        if not username:
            return render_template('login.html', error='Invalid username')
        
        user = User.query.filter_by(username=username).first()
        if not user:
            user = User(username=username)
            db.session.add(user)
            db.session.commit()
        
        session['username'] = username
        session['user_id'] = user.id
        return redirect(url_for('lobby'))
    
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/lobby')
def lobby():
    if 'username' not in session:
        return redirect(url_for('login'))
    user = User.query.filter_by(username=session['username']).first()
    return render_template('lobby.html', user=user)


@app.route('/room/<room_code>')
def room(room_code):
    if 'username' not in session:
        return redirect(url_for('login'))
    if room_code not in rooms:
        return redirect(url_for('lobby'))
    return render_template('room.html', room_code=room_code, username=session['username'])


@app.route('/match/<int:match_id>')
def chess_match(match_id):
    if 'username' not in session:
        return redirect(url_for('login'))
    match = Match.query.get_or_404(match_id)
    username = session['username']
    if username not in [match.white_player, match.black_player]:
        return redirect(url_for('lobby'))
    return render_template('chess_match.html', match=match, username=username)


@app.route('/leaderboard')
def leaderboard():
    users = User.query.order_by(User.elo_rating.desc()).limit(50).all()
    tournaments = Tournament.query.filter_by(status='completed').order_by(Tournament.completed_at.desc()).limit(20).all()
    return render_template('leaderboard.html', users=users, tournaments=tournaments)


@app.route('/api/create_room', methods=['POST'])
def create_room():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    room_code = generate_room_code()
    while room_code in rooms:
        room_code = generate_room_code()
    
    tournament = Tournament(
        room_code=room_code,
        admin_username=session['username'],
        status='waiting'
    )
    db.session.add(tournament)
    db.session.commit()
    
    rooms[room_code] = {
        'tournament_id': tournament.id,
        'admin': session['username'],
        'players': {},
        'match_requests': {},
        'active_matches': {},
        'bracket': [],
        'status': 'waiting',
        'default_time': 300
    }
    
    return jsonify({'room_code': room_code})


@app.route('/api/join_room', methods=['POST'])
def join_room_api():
    if 'username' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    room_code = request.json.get('room_code', '').strip().upper()
    if room_code not in rooms:
        return jsonify({'error': 'Room not found'}), 404
    
    room = rooms[room_code]
    if room['status'] != 'waiting':
        return jsonify({'error': 'Tournament already started'}), 400
    
    if len(room['players']) >= 10:
        return jsonify({'error': 'Room is full (max 10 players)'}), 400
    
    return jsonify({'room_code': room_code})


@app.route('/api/tournaments')
def get_tournaments():
    tournaments = Tournament.query.filter_by(status='completed').order_by(Tournament.completed_at.desc()).limit(20).all()
    return jsonify([t.to_dict() for t in tournaments])


# =================== SOCKET EVENTS ===================

@socketio.on('connect')
def on_connect():
    pass


@socketio.on('disconnect')
def on_disconnect():
    username = session.get('username')
    if not username:
        return
    
    # Remove from any rooms
    for room_code, room in list(rooms.items()):
        if username in room['players']:
            del room['players'][username]
            leave_room(room_code)
            
            if room['status'] == 'waiting':
                # Update tournament participants
                tournament = Tournament.query.get(room['tournament_id'])
                if tournament:
                    parts = tournament.participants
                    if username in parts:
                        parts.remove(username)
                    tournament.participants = parts
                    db.session.commit()
            
            emit_room_update(room_code)
            socketio.emit('player_left', {'username': username}, room=room_code)
            break


@socketio.on('join_room_socket')
def on_join_room(data):
    room_code = data.get('room_code', '').upper()
    username = session.get('username')
    
    if not username or room_code not in rooms:
        emit('error', {'message': 'Invalid room or not logged in'})
        return
    
    room = rooms[room_code]
    
    if room['status'] != 'waiting' and username not in room['players']:
        emit('error', {'message': 'Tournament already in progress'})
        return
    
    if len(room['players']) >= 10 and username not in room['players']:
        emit('error', {'message': 'Room is full'})
        return
    
    room['players'][username] = request.sid
    join_room(room_code)
    
    # Update tournament participants
    tournament = Tournament.query.get(room['tournament_id'])
    if tournament:
        parts = tournament.participants
        if username not in parts:
            parts.append(username)
        tournament.participants = parts
        db.session.commit()
    
    emit('joined_room', {'room_code': room_code, 'username': username, 'is_admin': username == room['admin']})
    emit_room_update(room_code)


@socketio.on('send_match_request')
def on_match_request(data):
    room_code = data.get('room_code', '').upper()
    opponent = data.get('opponent')
    time_control = int(data.get('time_control', 300))
    username = session.get('username')
    
    if not username or room_code not in rooms:
        return
    
    room = rooms[room_code]
    if username not in room['players'] or opponent not in room['players']:
        return
    
    if time_control not in [60, 180, 300, 600] and not (60 <= time_control <= 3600):
        time_control = 300
    
    room['match_requests'][(username, opponent)] = time_control
    
    # Emit to opponent
    opponent_sid = room['players'].get(opponent)
    if opponent_sid:
        socketio.emit('match_request_received', {
            'from': username,
            'time_control': time_control,
            'room_code': room_code
        }, to=opponent_sid)


@socketio.on('respond_match_request')
def on_match_response(data):
    room_code = data.get('room_code', '').upper()
    requester = data.get('requester')
    accepted = data.get('accepted', False)
    username = session.get('username')
    
    if not username or room_code not in rooms:
        return
    
    room = rooms[room_code]
    request_key = (requester, username)
    
    if request_key not in room['match_requests']:
        emit('error', {'message': 'Match request not found'})
        return
    
    time_control = room['match_requests'].pop(request_key)
    
    if not accepted:
        requester_sid = room['players'].get(requester)
        if requester_sid:
            socketio.emit('match_request_declined', {'by': username}, to=requester_sid)
        return
    
    # Create match
    match = Match(
        tournament_id=room['tournament_id'],
        round_name='Friendly',
        white_player=requester,
        black_player=username,
        time_control=time_control,
        status='active'
    )
    db.session.add(match)
    db.session.commit()
    
    match_state = {
        'room_code': room_code,
        'white': requester,
        'black': username,
        'white_time': time_control,
        'black_time': time_control,
        'turn': 'w',
        'fen': 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
        'status': 'active',
        'match_id': match.id
    }
    active_matches[match.id] = match_state
    
    # Notify both players
    for player in [requester, username]:
        player_sid = room['players'].get(player)
        if player_sid:
            color = 'white' if player == requester else 'black'
            socketio.emit('match_started', {
                'match_id': match.id,
                'white': requester,
                'black': username,
                'color': color,
                'time_control': time_control
            }, to=player_sid)


@socketio.on('start_tournament')
def on_start_tournament(data):
    room_code = data.get('room_code', '').upper()
    time_control = int(data.get('time_control', 300))
    username = session.get('username')
    
    if not username or room_code not in rooms:
        return
    
    room = rooms[room_code]
    if username != room['admin']:
        emit('error', {'message': 'Only admin can start tournament'})
        return
    
    players = list(room['players'].keys())
    if len(players) < 2:
        emit('error', {'message': 'Need at least 2 players to start'})
        return
    
    if len(players) > 10:
        emit('error', {'message': 'Maximum 10 players allowed'})
        return
    
    room['status'] = 'active'
    room['default_time'] = time_control
    
    tournament = Tournament.query.get(room['tournament_id'])
    tournament.status = 'active'
    tournament.participants = players
    
    round_name = get_round_name(len(players))
    tournament.current_round = round_name
    
    pairs = generate_bracket(players)
    bracket = []
    
    for white, black in pairs:
        if black == 'BYE':
            bracket.append({
                'white': white, 'black': 'BYE',
                'winner': white, 'status': 'completed',
                'match_id': None
            })
            continue
        
        match = Match(
            tournament_id=room['tournament_id'],
            round_name=round_name,
            white_player=white,
            black_player=black,
            time_control=time_control,
            status='pending'
        )
        db.session.add(match)
        db.session.flush()
        
        bracket.append({
            'white': white, 'black': black,
            'winner': None, 'status': 'pending',
            'match_id': match.id
        })
    
    rounds = [{'round': round_name, 'pairs': [(p[0], p[1]) for p in pairs]}]
    tournament.rounds = rounds
    db.session.commit()
    
    room['bracket'] = bracket
    
    socketio.emit('tournament_started', {
        'round_name': round_name,
        'bracket': bracket,
        'message': f'🏆 Tournament started! {round_name} begins!'
    }, room=room_code)
    
    emit_leaderboard(room_code)
    
    # Notify matched players
    for entry in bracket:
        if entry['status'] == 'completed':
            continue
        white = entry['white']
        black = entry['black']
        mid = entry['match_id']
        
        # Init match state
        match_state = {
            'room_code': room_code,
            'white': white,
            'black': black,
            'white_time': time_control,
            'black_time': time_control,
            'turn': 'w',
            'fen': 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
            'status': 'active',
            'match_id': mid
        }
        active_matches[mid] = match_state
        
        # Update match status
        m = Match.query.get(mid)
        if m:
            m.status = 'active'
            db.session.commit()
        
        for player, color in [(white, 'white'), (black, 'black')]:
            player_sid = room['players'].get(player)
            if player_sid:
                socketio.emit('match_started', {
                    'match_id': mid,
                    'white': white,
                    'black': black,
                    'color': color,
                    'time_control': time_control
                }, to=player_sid)


@socketio.on('join_match')
def on_join_match(data):
    match_id = int(data.get('match_id'))
    username = session.get('username')
    
    match = Match.query.get(match_id)
    if not match or username not in [match.white_player, match.black_player]:
        emit('error', {'message': 'Invalid match'})
        return
    
    join_room(f'match_{match_id}')
    
    state = active_matches.get(match_id)
    if state:
        emit('match_state', {
            'fen': state['fen'],
            'turn': state['turn'],
            'white_time': state['white_time'],
            'black_time': state['black_time'],
            'white': state['white'],
            'black': state['black'],
            'status': state['status']
        })


@socketio.on('make_move')
def on_make_move(data):
    match_id = int(data.get('match_id'))
    move = data.get('move')  # {from, to, promotion}
    fen_after = data.get('fen')
    username = session.get('username')
    
    state = active_matches.get(match_id)
    if not state or state['status'] != 'active':
        emit('error', {'message': 'Match not active'})
        return
    
    # Validate it's the player's turn
    if state['turn'] == 'w' and username != state['white']:
        emit('error', {'message': 'Not your turn'})
        return
    if state['turn'] == 'b' and username != state['black']:
        emit('error', {'message': 'Not your turn'})
        return
    
    # Basic move validation - ensure move has from/to
    if not move or not move.get('from') or not move.get('to'):
        emit('error', {'message': 'Invalid move format'})
        return
    
    # Update state
    state['fen'] = fen_after
    state['turn'] = 'b' if state['turn'] == 'w' else 'w'
    
    # Emit move to both players in match room
    socketio.emit('move_made', {
        'move': move,
        'fen': fen_after,
        'turn': state['turn'],
        'white_time': state['white_time'],
        'black_time': state['black_time'],
        'by': username
    }, room=f'match_{match_id}')


@socketio.on('update_timer')
def on_update_timer(data):
    match_id = int(data.get('match_id'))
    white_time = data.get('white_time')
    black_time = data.get('black_time')
    
    state = active_matches.get(match_id)
    if not state or state['status'] != 'active':
        return
    
    username = session.get('username')
    # Only white player sends timer updates (authoritative)
    if username != state['white']:
        return
    
    state['white_time'] = white_time
    state['black_time'] = black_time
    
    # Check for timeout
    if white_time <= 0:
        handle_match_end(match_id, state['black'], 'timeout')
    elif black_time <= 0:
        handle_match_end(match_id, state['white'], 'timeout')
    else:
        socketio.emit('timer_update', {
            'white_time': white_time,
            'black_time': black_time
        }, room=f'match_{match_id}')


@socketio.on('game_over')
def on_game_over(data):
    match_id = int(data.get('match_id'))
    result = data.get('result')  # 'checkmate', 'draw', 'stalemate'
    winner = data.get('winner')  # username or None for draw
    
    state = active_matches.get(match_id)
    if not state or state['status'] != 'active':
        return
    
    username = session.get('username')
    if username not in [state['white'], state['black']]:
        return
    
    handle_match_end(match_id, winner, result)


@socketio.on('resign')
def on_resign(data):
    match_id = int(data.get('match_id'))
    username = session.get('username')
    
    state = active_matches.get(match_id)
    if not state or state['status'] != 'active':
        return
    
    if username not in [state['white'], state['black']]:
        return
    
    winner = state['black'] if username == state['white'] else state['white']
    handle_match_end(match_id, winner, 'resignation')


def handle_match_end(match_id, winner, result):
    state = active_matches.get(match_id)
    if not state or state['status'] != 'active':
        return
    
    state['status'] = 'completed'
    
    match = Match.query.get(match_id)
    if not match:
        return
    
    match.status = 'completed'
    match.winner = winner
    match.result = result
    match.completed_at = datetime.utcnow()
    
    # Update player stats
    white_user = User.query.filter_by(username=match.white_player).first()
    black_user = User.query.filter_by(username=match.black_player).first()
    
    if white_user and black_user:
        white_user.total_matches += 1
        black_user.total_matches += 1
        
        if result == 'draw':
            white_user.total_draws += 1
            black_user.total_draws += 1
        elif winner == match.white_player:
            white_user.total_wins += 1
            black_user.total_losses += 1
            new_w, new_b = calculate_elo(white_user.elo_rating, black_user.elo_rating)
            white_user.elo_rating = new_w
            black_user.elo_rating = new_b
        elif winner == match.black_player:
            black_user.total_wins += 1
            white_user.total_losses += 1
            new_b, new_w = calculate_elo(black_user.elo_rating, white_user.elo_rating)
            black_user.elo_rating = new_b
            white_user.elo_rating = new_w
    
    db.session.commit()
    
    # Emit game over to match room
    socketio.emit('game_ended', {
        'winner': winner,
        'result': result,
        'white_player': match.white_player,
        'black_player': match.black_player
    }, room=f'match_{match_id}')
    
    # Update bracket in room
    room_code = state.get('room_code')
    if room_code and room_code in rooms:
        room = rooms[room_code]
        for entry in room.get('bracket', []):
            if entry.get('match_id') == match_id:
                entry['winner'] = winner
                entry['status'] = 'completed'
                break
        
        # Notify room of match result
        socketio.emit('match_result', {
            'match_id': match_id,
            'winner': winner,
            'result': result,
            'white': match.white_player,
            'black': match.black_player
        }, room=room_code)
        
        emit_leaderboard(room_code)
        
        # Check if round is complete
        if match.round_name != 'Friendly':
            check_round_complete(room_code)
    
    # Clean up
    del active_matches[match_id]


@socketio.on('admin_remove_player')
def on_remove_player(data):
    room_code = data.get('room_code', '').upper()
    target = data.get('username')
    username = session.get('username')
    
    if not username or room_code not in rooms:
        return
    
    room = rooms[room_code]
    if username != room['admin']:
        emit('error', {'message': 'Only admin can remove players'})
        return
    
    if target in room['players']:
        target_sid = room['players'].pop(target)
        socketio.emit('kicked', {'message': 'You were removed by the admin'}, to=target_sid)
        emit_room_update(room_code)


@socketio.on('admin_force_next_round')
def on_force_next_round(data):
    room_code = data.get('room_code', '').upper()
    username = session.get('username')
    
    if not username or room_code not in rooms:
        return
    
    room = rooms[room_code]
    if username != room['admin']:
        emit('error', {'message': 'Only admin can force next round'})
        return
    
    check_round_complete(room_code)


@socketio.on('chat_message')
def on_chat(data):
    room_code = data.get('room_code', '').upper()
    message = data.get('message', '').strip()[:200]
    username = session.get('username')
    
    if not username or room_code not in rooms or not message:
        return
    
    if username not in rooms[room_code]['players']:
        return
    
    socketio.emit('chat_message', {
        'username': username,
        'message': message,
        'timestamp': datetime.utcnow().isoformat()
    }, room=room_code)


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        print("\n✅ Database ready.")
    port = int(os.environ.get('PORT', 5000))
    print(f"🚀 ChessArena running on http://0.0.0.0:{port}")
    print(f"   Share your public URL with friends to play online!\n")
    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False)
