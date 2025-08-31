# app.py
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
import random
import time
from datetime import datetime
import json
from collections import deque
import secrets
import threading

app = Flask(__name__)
app.config['SECRET_KEY'] = secrets.token_hex(16)
socketio = SocketIO(app, manage_session=False, cors_allowed_origins="*")


# Game state management
class Player:
    def __init__(self, id, name, sid):
        self.id = id
        self.name = name
        self.sid = sid
        self.role = None
        self.alive = True
        self.votes = 0
        self.vote_target = None
        self.is_admin = False

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'role': self.role,
            'alive': self.alive,
            'votes': self.votes,
            'is_admin': self.is_admin
        }


class Lobby:
    def __init__(self, code, creator):
        self.code = code
        self.players = {creator.id: creator}
        creator.is_admin = True
        self.settings = {
            'doctor': True,
            'detective': False,
            'max_players': 12,
            'min_players': 4,
            'game_time': 120,  # seconds per phase
            'night_chat': False  # Whether mafia can chat during night
        }
        self.game = None
        self.created_at = datetime.now()
        self.messages = deque(maxlen=100)

    def to_dict(self):
        return {
            'code': self.code,
            'players': [player.to_dict() for player in self.players.values()],
            'player_count': len(self.players),
            'settings': self.settings,
            'game_started': self.game is not None,
            'messages': list(self.messages)
        }

    def add_message(self, message, player_name=None):
        if player_name:
            full_message = f"{player_name}: {message}"
        else:
            full_message = message

        timestamp = datetime.now().strftime("%H:%M:%S")
        self.messages.append({
            'timestamp': timestamp,
            'message': full_message
        })

        return {
            'timestamp': timestamp,
            'message': full_message
        }


class Game:
    def __init__(self, lobby):
        self.lobby = lobby
        self.players = lobby.players
        self.phase = "setup"  # setup, night, day, discussion, voting, ended
        self.day_number = 0
        self.time_remaining = 0
        self.votes = {}
        self.night_actions = {}
        self.communications = deque(maxlen=100)
        self.start_time = time.time()

        # Assign roles
        self.assign_roles()

        # Start the first night phase
        self.start_night()

    def assign_roles(self):
        players = list(self.players.values())
        random.shuffle(players)

        # Determine number of mafia based on player count
        num_players = len(players)
        if num_players <= 6:
            num_mafia = 1
        elif num_players <= 9:
            num_mafia = 2
        else:
            num_mafia = 3

        # Assign roles
        for i, player in enumerate(players):
            if i < num_mafia:
                player.role = "mafia"
            else:
                player.role = "townsfolk"

        # Check if doctor is enabled and assign if needed
        if self.lobby.settings['doctor'] and num_players > 4:
            # Replace one townsfolk with doctor
            for player in players:
                if player.role == "townsfolk":
                    player.role = "doctor"
                    break

        # Check if detective is enabled and assign if needed
        if self.lobby.settings['detective'] and num_players > 6:
            # Replace one townsfolk with detective
            for player in players:
                if player.role == "townsfolk":
                    player.role = "detective"
                    break

    def start_night(self):
        self.phase = "night"
        self.day_number += 1
        self.time_remaining = self.lobby.settings['game_time']
        self.night_actions = {}
        self.broadcast_game_state()
        self.add_communication("The night falls. Mafia, choose your target.")

        # Notify mafia members about each other
        mafia_members = [p.name for p in self.players.values() if p.role == "mafia" and p.alive]
        if len(mafia_members) > 1:
            for player in self.players.values():
                if player.role == "mafia" and player.alive:
                    self.send_private_message(player, f"Your mafia teammates are: {', '.join(mafia_members)}")

    def start_day(self):
        self.phase = "day"
        self.time_remaining = self.lobby.settings['game_time']
        self.process_night_actions()
        self.broadcast_game_state()
        self.add_communication("The day begins. Discuss and find the mafia!")

    def start_discussion(self):
        self.phase = "discussion"
        self.time_remaining = self.lobby.settings['game_time'] // 2
        self.broadcast_game_state()
        self.add_communication("Discussion phase begins. Talk about your suspicions!")

    def start_voting(self):
        self.phase = "voting"
        self.time_remaining = self.lobby.settings['game_time'] // 2
        self.votes = {}
        for player in self.players.values():
            player.votes = 0
            player.vote_target = None
        self.broadcast_game_state()
        self.add_communication("Voting phase begins. Vote for who you think is mafia!")

    def process_night_actions(self):
        # Process mafia kill
        mafia_target_id = None
        doctor_target_id = None
        detective_target_id = None

        # Find targets
        for action in self.night_actions.values():
            if action['type'] == 'mafia_kill':
                mafia_target_id = action['target_id']
            elif action['type'] == 'doctor_heal':
                doctor_target_id = action['target_id']
            elif action['type'] == 'detective_investigate':
                detective_target_id = action['target_id']

        # Apply actions
        if mafia_target_id and mafia_target_id in self.players:
            if mafia_target_id != doctor_target_id:  # Doctor saves if they targeted the same person
                self.players[mafia_target_id].alive = False
                self.add_communication(f"{self.players[mafia_target_id].name} was killed by the mafia!")
            else:
                self.add_communication("The doctor saved someone from the mafia's attack!")
        else:
            self.add_communication("The mafia did not kill anyone tonight.")

        # Process detective investigation
        if detective_target_id and detective_target_id in self.players:
            detective_id = None
            for pid, action in self.night_actions.items():
                if action['type'] == 'detective_investigate':
                    detective_id = pid
                    break

            if detective_id:
                target_role = self.players[detective_target_id].role
                role_hint = "suspicious" if target_role == "mafia" else "trustworthy"
                self.send_private_message(
                    self.players[detective_id],
                    f"Your investigation reveals that {self.players[detective_target_id].name} seems {role_hint}."
                )

    def check_game_end(self):
        mafia_count = 0
        town_count = 0

        for player in self.players.values():
            if player.alive:
                if player.role == "mafia":
                    mafia_count += 1
                else:
                    town_count += 1

        if mafia_count == 0:
            self.phase = "ended"
            self.add_communication("The townsfolk have won! All mafia members have been eliminated.")
            return True
        elif mafia_count >= town_count:
            self.phase = "ended"
            self.add_communication("The mafia have won! They outnumber the townsfolk.")
            return True

        return False

    def add_communication(self, message, player_name=None):
        if player_name:
            full_message = f"{player_name}: {message}"
        else:
            full_message = message

        timestamp = datetime.now().strftime("%H:%M:%S")
        self.communications.append({
            'timestamp': timestamp,
            'message': full_message
        })

        # Broadcast to all players in the game
        socketio.emit('new_message', {
            'timestamp': timestamp,
            'message': full_message
        }, room=self.lobby.code)

    def send_private_message(self, player, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        socketio.emit('private_message', {
            'timestamp': timestamp,
            'message': message
        }, room=player.sid)

    def broadcast_game_state(self):
        game_state = self.get_game_state()
        socketio.emit('game_update', game_state, room=self.lobby.code)

    def get_game_state(self):
        players_data = {pid: player.to_dict() for pid, player in self.players.items()}

        return {
            'phase': self.phase,
            'day_number': self.day_number,
            'time_remaining': self.time_remaining,
            'players': players_data,
            'communications': list(self.communications)
        }


# Global state (in production, use a proper database)
lobbies = {}
players = {}


# Helper functions
def generate_lobby_code():
    code = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=6))
    while code in lobbies:
        code = ''.join(random.choices('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', k=6))
    return code


# Routes
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/create', methods=['POST'])
def create_lobby():
    player_name = request.form.get('player_name')
    if not player_name or len(player_name.strip()) < 2:
        return redirect(url_for('index'))

    # Create player
    player_id = secrets.token_hex(8)
    session['player_id'] = player_id
    session['player_name'] = player_name

    # Create lobby
    lobby_code = generate_lobby_code()
    player_obj = Player(player_id, player_name, None)
    lobbies[lobby_code] = Lobby(lobby_code, player_obj)
    players[player_id] = player_obj

    return redirect(url_for('lobby', code=lobby_code))


@app.route('/join', methods=['POST'])
def join_lobby():
    player_name = request.form.get('player_name')
    lobby_code = request.form.get('lobby_code', '').upper().strip()

    if not player_name or len(player_name.strip()) < 2:
        return redirect(url_for('index'))

    if lobby_code not in lobbies:
        return render_template('index.html', error="Lobby not found")

    lobby = lobbies[lobby_code]
    if len(lobby.players) >= lobby.settings['max_players']:
        return render_template('index.html', error="Lobby is full")

    if lobby.game:
        return render_template('index.html', error="Game already in progress")

    # Create player
    player_id = secrets.token_hex(8)
    session['player_id'] = player_id
    session['player_name'] = player_name

    player_obj = Player(player_id, player_name, None)
    lobby.players[player_id] = player_obj
    players[player_id] = player_obj

    # Notify all players in the lobby
    message = lobby.add_message(f"{player_name} joined the lobby")
    socketio.emit('new_message', message, room=lobby_code)
    socketio.emit('lobby_update', lobby.to_dict(), room=lobby_code)

    return redirect(url_for('lobby', code=lobby_code))


@app.route('/lobby/<code>')
def lobby(code):
    if code not in lobbies:
        return redirect(url_for('index'))

    player_id = session.get('player_id')
    if not player_id or player_id not in lobbies[code].players:
        return redirect(url_for('index'))

    return render_template('lobby.html', lobby=lobbies[code].to_dict(), player_id=player_id)


@app.route('/game/<code>')
def game(code):
    if code not in lobbies:
        return redirect(url_for('index'))

    player_id = session.get('player_id')
    if not player_id or player_id not in lobbies[code].players:
        return redirect(url_for('index'))

    if not lobbies[code].game:
        return redirect(url_for('lobby', code=code))

    player = lobbies[code].players[player_id]
    return render_template('game.html', lobby_code=code, player=player.to_dict())


@app.route('/api/lobby/<code>')
def api_lobby(code):
    if code not in lobbies:
        return jsonify({'error': 'Lobby not found'}), 404

    return jsonify(lobbies[code].to_dict())


# Socket events
@socketio.on('connect')
def handle_connect():
    player_id = session.get('player_id')
    if player_id and player_id in players:
        players[player_id].sid = request.sid


@socketio.on('join_lobby')
def handle_join_lobby(data):
    lobby_code = data.get('lobby_code')
    player_id = session.get('player_id')

    if lobby_code in lobbies and player_id in lobbies[lobby_code].players:
        join_room(lobby_code)
        emit('lobby_update', lobbies[lobby_code].to_dict(), room=lobby_code)


@socketio.on('leave_lobby')
def handle_leave_lobby(data):
    lobby_code = data.get('lobby_code')
    player_id = session.get('player_id')

    if lobby_code in lobbies and player_id in lobbies[lobby_code].players:
        player_name = lobbies[lobby_code].players[player_id].name
        leave_room(lobby_code)

        # Remove player from lobby
        del lobbies[lobby_code].players[player_id]

        # If lobby is empty, remove it
        if len(lobbies[lobby_code].players) == 0:
            del lobbies[lobby_code]
        else:
            # Assign new admin if needed
            admin_found = any(player.is_admin for player in lobbies[lobby_code].players.values())
            if not admin_found:
                new_admin = next(iter(lobbies[lobby_code].players.values()))
                new_admin.is_admin = True

            # Notify remaining players
            message = lobbies[lobby_code].add_message(f"{player_name} left the lobby")
            socketio.emit('new_message', message, room=lobby_code)
            socketio.emit('lobby_update', lobbies[lobby_code].to_dict(), room=lobby_code)


@socketio.on('start_game')
def handle_start_game(data):
    lobby_code = data.get('lobby_code')
    player_id = session.get('player_id')

    if (lobby_code in lobbies and
            player_id in lobbies[lobby_code].players and
            not lobbies[lobby_code].game):

        # Check if minimum players requirement is met
        if len(lobbies[lobby_code].players) < lobbies[lobby_code].settings['min_players']:
            emit('error', {'message': f'Need at least {lobbies[lobby_code].settings["min_players"]} players to start'})
            return

        # Only the admin can start the game
        if lobbies[lobby_code].players[player_id].is_admin:
            lobbies[lobby_code].game = Game(lobbies[lobby_code])
            emit('game_started', {'redirect': url_for('game', code=lobby_code)}, room=lobby_code)


@socketio.on('update_settings')
def handle_update_settings(data):
    lobby_code = data.get('lobby_code')
    player_id = session.get('player_id')
    settings = data.get('settings', {})

    if (lobby_code in lobbies and
            player_id in lobbies[lobby_code].players and
            not lobbies[lobby_code].game):

        # Only the admin can change settings
        if lobbies[lobby_code].players[player_id].is_admin:
            for key, value in settings.items():
                if key in lobbies[lobby_code].settings:
                    lobbies[lobby_code].settings[key] = value

            # Ensure min_players is not greater than max_players
            if lobbies[lobby_code].settings['min_players'] > lobbies[lobby_code].settings['max_players']:
                lobbies[lobby_code].settings['min_players'] = lobbies[lobby_code].settings['max_players']

            emit('lobby_update', lobbies[lobby_code].to_dict(), room=lobby_code)


@socketio.on('send_message')
def handle_send_message(data):
    lobby_code = data.get('lobby_code')
    player_id = session.get('player_id')
    message = data.get('message', '').strip()

    if not message or lobby_code not in lobbies or player_id not in lobbies[lobby_code].players:
        return

    player = lobbies[lobby_code].players[player_id]
    game = lobbies[lobby_code].game

    if game:
        # Check if player can speak based on game phase and role
        if game.phase == "night":
            # Only mafia can talk at night if night_chat is enabled
            if player.role == "mafia" and game.lobby.settings['night_chat']:
                game.add_communication(message, player.name)
        else:
            # Everyone can talk during day phases
            game.add_communication(message, player.name)
    else:
        # In lobby, everyone can talk
        message_data = lobbies[lobby_code].add_message(message, player.name)
        socketio.emit('new_message', message_data, room=lobby_code)


@socketio.on('night_action')
def handle_night_action(data):
    lobby_code = data.get('lobby_code')
    player_id = session.get('player_id')
    target_id = data.get('target_id')

    if (lobby_code not in lobbies or
            not lobbies[lobby_code].game or
            player_id not in lobbies[lobby_code].players):
        return

    game = lobbies[lobby_code].game
    player = game.players[player_id]

    if game.phase != "night" or not player.alive:
        return

    # Validate action based on role
    action_type = None
    if player.role == "mafia":
        action_type = "mafia_kill"
    elif player.role == "doctor":
        action_type = "doctor_heal"
    elif player.role == "detective":
        action_type = "detective_investigate"

    if action_type and target_id in game.players:
        game.night_actions[player_id] = {
            'type': action_type,
            'target_id': target_id
        }

        # Notify player
        emit('action_confirmed', {'action': action_type, 'target': target_id})

        # Check if all actions are submitted
        expected_actions = 0
        for p in game.players.values():
            if p.alive and p.role in ["mafia", "doctor", "detective"]:
                expected_actions += 1

        if len(game.night_actions) >= expected_actions:
            # All actions submitted, proceed to day
            socketio.sleep(2)  # Brief delay
            game.start_day()


@socketio.on('cast_vote')
def handle_cast_vote(data):
    lobby_code = data.get('lobby_code')
    player_id = session.get('player_id')
    target_id = data.get('target_id')

    if (lobby_code not in lobbies or
            not lobbies[lobby_code].game or
            player_id not in lobbies[lobby_code].players):
        return

    game = lobbies[lobby_code].game
    player = game.players[player_id]

    if game.phase != "voting" or not player.alive:
        return

    # Record vote
    game.votes[player_id] = target_id
    player.vote_target = target_id

    # Update vote count for target
    if target_id in game.players:
        game.players[target_id].votes += 1

    # Broadcast updated game state
    game.broadcast_game_state()

    # Check if all votes are in
    alive_players = sum(1 for p in game.players.values() if p.alive)
    if len(game.votes) >= alive_players:
        # Process votes
        vote_count = {}
        for voted_id in game.votes.values():
            vote_count[voted_id] = vote_count.get(voted_id, 0) + 1

        # Find player with most votes
        if vote_count:
            max_votes = max(vote_count.values())
            candidates = [pid for pid, votes in vote_count.items() if votes == max_votes]

            if len(candidates) == 1:
                # Eliminate player
                eliminated_id = candidates[0]
                game.players[eliminated_id].alive = False
                game.add_communication(f"{game.players[eliminated_id].name} has been eliminated!")

                # Check if game continues
                if not game.check_game_end():
                    # Start next night
                    socketio.sleep(3)  # Brief delay
                    game.start_night()
            else:
                # Tie vote, no elimination
                game.add_communication("It's a tie! No one is eliminated.")
                socketio.sleep(3)  # Brief delay
                game.start_night()
        else:
            # No votes, proceed to night
            game.add_communication("No votes were cast.")
            socketio.sleep(3)  # Brief delay
            game.start_night()


# Game timer
def game_timer():
    while True:
        socketio.sleep(1)
        for code, lobby in list(lobbies.items()):
            if lobby.game and lobby.game.phase not in ["setup", "ended"]:
                lobby.game.time_remaining -= 1

                if lobby.game.time_remaining <= 0:
                    # Time's up, proceed to next phase
                    if lobby.game.phase == "night":
                        lobby.game.start_day()
                    elif lobby.game.phase == "day":
                        lobby.game.start_discussion()
                    elif lobby.game.phase == "discussion":
                        lobby.game.start_voting()
                    elif lobby.game.phase == "voting":
                        # Auto-process votes if not all are in
                        lobby.game.start_night()


# Start timer thread
timer_thread = threading.Thread(target=game_timer, daemon=True)
timer_thread.start()

if __name__ == '__main__':
    socketio.run(app, debug=True)