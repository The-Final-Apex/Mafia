from flask import Flask, render_template, request, redirect, url_for
from flask_socketio import SocketIO, emit, join_room
import uuid

app = Flask(__name__)
app.config["SECRET_KEY"] = "supersecret"
socketio = SocketIO(app, cors_allowed_origins="*")

# store lobbies
lobbies = {}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/create", methods=["POST"])
def create():
    lobby_id = str(uuid.uuid4())[:5].upper()  # short unique code
    lobbies[lobby_id] = {
        "players": [],
        "admin": None,
        "settings": {
            "min_players": 4,
            "max_players": 10,
            "day_duration": 60,
            "night_duration": 30,
            "mafia_count": 2
        }
    }
    return redirect(url_for("lobby", lobby_id=lobby_id))

@app.route("/lobby/<lobby_id>")
def lobby(lobby_id):
    return render_template("lobby.html", lobby_id=lobby_id)

# === SOCKET EVENTS ===
@socketio.on("join_lobby")
def join_lobby(data):
    lobby_id = data["lobby"]
    player = data["name"]
    join_room(lobby_id)

    if lobby_id not in lobbies:
        return

    if player not in lobbies[lobby_id]["players"]:
        lobbies[lobby_id]["players"].append(player)

    if not lobbies[lobby_id]["admin"]:
        lobbies[lobby_id]["admin"] = player

    # Broadcast update to all
    emit("update_lobby", {
        "players": lobbies[lobby_id]["players"],
        "admin": lobbies[lobby_id]["admin"],
        "settings": lobbies[lobby_id]["settings"]
    }, room=lobby_id)

    # Send full state immediately to new player
    emit("full_state", {
        "players": lobbies[lobby_id]["players"],
        "admin": lobbies[lobby_id]["admin"],
        "settings": lobbies[lobby_id]["settings"]
    })

@socketio.on("chat")
def chat(data):
    lobby_id = data["lobby"]
    player = data["player"]
    msg = data["message"]
    emit("chat", {"player": player, "message": msg}, room=lobby_id)

@socketio.on("update_settings")
def update_settings(data):
    lobby_id = data["lobby"]
    if lobby_id not in lobbies:
        return
    lobbies[lobby_id]["settings"] = data["settings"]
    emit("settings_updated", lobbies[lobby_id]["settings"], room=lobby_id)

@socketio.on("start_game")
def start_game(data):
    lobby_id = data["lobby"]
    emit("game_starting", {"msg": "The game is starting soon!"}, room=lobby_id)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
