import os
import sqlite3
import uuid
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g, send_from_directory
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "win95social.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_IMAGE = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
ALLOWED_VIDEO = {"mp4", "webm", "mov", "avi"}

app = Flask(__name__)
app.secret_key = "win95social-dev-secret-change-me"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB max upload

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---------- Database helpers ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            bio TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            icon TEXT DEFAULT '02_computer-4.png',
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            post_type TEXT NOT NULL,  -- text, image, video
            title TEXT DEFAULT '',
            body TEXT DEFAULT '',
            media_path TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (community_id) REFERENCES communities (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (post_id) REFERENCES posts (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(post_id, user_id),
            FOREIGN KEY (post_id) REFERENCES posts (id),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    """)

    # Seed some starter communities if none exist
    existing = db.execute("SELECT COUNT(*) as c FROM communities").fetchone()
    if existing["c"] == 0:
        starter_communities = [
            ("r/RetroTech", "Old computers, ThinkPads, and nostalgia hardware"),
            ("r/Gaming", "Video games, handhelds, and builds"),
            ("r/Aviation", "Plane spotting and aviation talk"),
            ("r/Memes", "Just memes, no rules"),
            ("r/Crafts", "Papercraft, soldering, DIY projects"),
        ]
        now = datetime.utcnow().isoformat()
        for name, desc in starter_communities:
            db.execute(
                "INSERT INTO communities (name, description, created_at) VALUES (?, ?, ?)",
                (name, desc, now),
            )
        db.commit()

    db.close()


# ---------- Auth helpers ----------

def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def login_required_redirect():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return None


def allowed_file(filename, allowed_set):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_set


# ---------- Routes: Auth ----------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        if not username:
            return render_template("login.html", error="Type a username, dummy.")
        if len(username) > 20:
            return render_template("login.html", error="Username too long (max 20 chars).")

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None:
            now = datetime.utcnow().isoformat()
            db.execute(
                "INSERT INTO users (username, created_at) VALUES (?, ?)",
                (username, now),
            )
            db.commit()
            user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()

        session["user_id"] = user["id"]
        return redirect(url_for("communities"))

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- Routes: Communities ----------

@app.route("/")
def index():
    if "user_id" not in session:
        return redirect(url_for("login"))
    return redirect(url_for("communities"))


@app.route("/communities")
def communities():
    redir = login_required_redirect()
    if redir:
        return redir
    db = get_db()
    comms = db.execute("""
        SELECT c.*, COUNT(p.id) as post_count
        FROM communities c
        LEFT JOIN posts p ON p.community_id = c.id
        GROUP BY c.id
        ORDER BY c.name ASC
    """).fetchall()
    return render_template("communities.html", communities=comms, user=current_user())


@app.route("/communities/new", methods=["POST"])
def new_community():
    redir = login_required_redirect()
    if redir:
        return redir
    name = request.form.get("name", "").strip()
    desc = request.form.get("description", "").strip()
    if not name:
        return redirect(url_for("communities"))
    if not name.startswith("r/"):
        name = "r/" + name
    db = get_db()
    try:
        db.execute(
            "INSERT INTO communities (name, description, created_at) VALUES (?, ?, ?)",
            (name, desc, datetime.utcnow().isoformat()),
        )
        db.commit()
    except sqlite3.IntegrityError:
        pass  # community already exists, ignore
    return redirect(url_for("communities"))


# ---------- Routes: Feed (TikTok-style scroll) ----------

@app.route("/feed/<int:community_id>")
def feed(community_id):
    redir = login_required_redirect()
    if redir:
        return redir
    db = get_db()
    community = db.execute("SELECT * FROM communities WHERE id = ?", (community_id,)).fetchone()
    if community is None:
        return redirect(url_for("communities"))
    return render_template("feed.html", community=community, user=current_user())


@app.route("/api/posts/<int:community_id>")
def api_posts(community_id):
    redir = login_required_redirect()
    if redir:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    uid = session["user_id"]
    rows = db.execute("""
        SELECT p.*, u.username,
            (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) as like_count,
            (SELECT COUNT(*) FROM comments cm WHERE cm.post_id = p.id) as comment_count,
            (SELECT COUNT(*) FROM likes l2 WHERE l2.post_id = p.id AND l2.user_id = ?) as liked_by_me
        FROM posts p
        JOIN users u ON u.id = p.user_id
        WHERE p.community_id = ?
        ORDER BY p.created_at DESC
    """, (uid, community_id)).fetchall()
    posts = [dict(r) for r in rows]
    for p in posts:
        p["liked_by_me"] = bool(p["liked_by_me"])
    return jsonify(posts)


# ---------- Routes: Posting ----------

@app.route("/post/new/<int:community_id>", methods=["GET", "POST"])
def new_post(community_id):
    redir = login_required_redirect()
    if redir:
        return redir
    db = get_db()
    community = db.execute("SELECT * FROM communities WHERE id = ?", (community_id,)).fetchone()
    if community is None:
        return redirect(url_for("communities"))

    if request.method == "POST":
        post_type = request.form.get("post_type", "text")
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        media_path = ""

        if post_type in ("image", "video"):
            file = request.files.get("media")
            if file and file.filename:
                allowed = ALLOWED_IMAGE if post_type == "image" else ALLOWED_VIDEO
                if allowed_file(file.filename, allowed):
                    ext = file.filename.rsplit(".", 1)[1].lower()
                    new_name = f"{uuid.uuid4().hex}.{ext}"
                    filepath = os.path.join(app.config["UPLOAD_FOLDER"], new_name)
                    file.save(filepath)
                    media_path = new_name
                else:
                    return render_template("new_post.html", community=community, user=current_user(),
                                            error="That file type isn't allowed for this post type.")
            else:
                return render_template("new_post.html", community=community, user=current_user(),
                                        error="You gotta attach a file for that post type.")

        if not title and not body and not media_path:
            return render_template("new_post.html", community=community, user=current_user(),
                                    error="Post can't be totally empty.")

        db.execute("""
            INSERT INTO posts (community_id, user_id, post_type, title, body, media_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (community_id, session["user_id"], post_type, title, body, media_path, datetime.utcnow().isoformat()))
        db.commit()
        return redirect(url_for("feed", community_id=community_id))

    return render_template("new_post.html", community=community, user=current_user(), error=None)


# ---------- Routes: Likes ----------

@app.route("/api/like/<int:post_id>", methods=["POST"])
def api_like(post_id):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    uid = session["user_id"]
    existing = db.execute("SELECT * FROM likes WHERE post_id = ? AND user_id = ?", (post_id, uid)).fetchone()
    if existing:
        db.execute("DELETE FROM likes WHERE id = ?", (existing["id"],))
        db.commit()
        liked = False
    else:
        db.execute("INSERT INTO likes (post_id, user_id, created_at) VALUES (?, ?, ?)",
                   (post_id, uid, datetime.utcnow().isoformat()))
        db.commit()
        liked = True
    count = db.execute("SELECT COUNT(*) as c FROM likes WHERE post_id = ?", (post_id,)).fetchone()["c"]
    return jsonify({"liked": liked, "like_count": count})


# ---------- Routes: Comments ----------

@app.route("/api/comments/<int:post_id>")
def api_get_comments(post_id):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    rows = db.execute("""
        SELECT cm.*, u.username
        FROM comments cm
        JOIN users u ON u.id = cm.user_id
        WHERE cm.post_id = ?
        ORDER BY cm.created_at ASC
    """, (post_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/comments/<int:post_id>", methods=["POST"])
def api_post_comment(post_id):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "empty comment"}), 400
    db = get_db()
    uid = session["user_id"]
    db.execute("INSERT INTO comments (post_id, user_id, body, created_at) VALUES (?, ?, ?, ?)",
               (post_id, uid, body, datetime.utcnow().isoformat()))
    db.commit()
    count = db.execute("SELECT COUNT(*) as c FROM comments WHERE post_id = ?", (post_id,)).fetchone()["c"]
    return jsonify({"ok": True, "comment_count": count})


# ---------- Routes: Profile ----------

@app.route("/profile/<username>")
def profile(username):
    redir = login_required_redirect()
    if redir:
        return redir
    db = get_db()
    profile_user = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if profile_user is None:
        return redirect(url_for("communities"))
    posts = db.execute("""
        SELECT p.*, c.name as community_name,
            (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) as like_count,
            (SELECT COUNT(*) FROM comments cm WHERE cm.post_id = p.id) as comment_count
        FROM posts p
        JOIN communities c ON c.id = p.community_id
        WHERE p.user_id = ?
        ORDER BY p.created_at DESC
    """, (profile_user["id"],)).fetchall()
    return render_template("profile.html", profile_user=profile_user, posts=posts, user=current_user())


@app.route("/profile/<username>/bio", methods=["POST"])
def update_bio(username):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    me = current_user()
    if me["username"] != username:
        return jsonify({"error": "not your profile"}), 403
    data = request.get_json(silent=True) or {}
    bio = (data.get("bio") or "").strip()[:280]
    db.execute("UPDATE users SET bio = ? WHERE id = ?", (bio, me["id"]))
    db.commit()
    return jsonify({"ok": True, "bio": bio})


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
