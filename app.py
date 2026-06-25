import os
import uuid
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import cloudinary
import cloudinary.uploader
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash

ALLOWED_IMAGE = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
ALLOWED_VIDEO = {"mp4", "webm", "mov", "avi"}

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "win95social-dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB max upload

# ---------- Cloudinary config (reads from environment variables) ----------
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET"),
    secure=True,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Render gives URLs starting with postgres://, psycopg2 wants postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)


# ---------- Database helpers ----------

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT DEFAULT '',
            bio TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    # Migration: add password_hash to a users table that existed before this column did
    cur.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT DEFAULT ''
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS communities (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            id SERIAL PRIMARY KEY,
            community_id INTEGER NOT NULL REFERENCES communities (id),
            user_id INTEGER NOT NULL REFERENCES users (id),
            post_type TEXT NOT NULL,
            title TEXT DEFAULT '',
            body TEXT DEFAULT '',
            media_path TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts (id),
            user_id INTEGER NOT NULL REFERENCES users (id),
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS likes (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts (id),
            user_id INTEGER NOT NULL REFERENCES users (id),
            created_at TEXT NOT NULL,
            UNIQUE(post_id, user_id)
        )
    """)
    db.commit()

    cur.execute("SELECT COUNT(*) as c FROM communities")
    existing = cur.fetchone()
    if existing["c"] == 0:
        starter_communities = [
            ("r/RetroTech", "Old computers, ThinkPads, and nostalgia hardware"),
            ("r/Gaming", "Video games, handhelds, and builds"),
            ("r/Aviation", "Plane spotting and aviation talk"),
            ("r/Memes", "Just memes, no rules"),
            ("r/Crafts", "Papercraft, soldering, DIY projects"),
        ]
        now = now_iso()
        for name, desc in starter_communities:
            cur.execute(
                "INSERT INTO communities (name, description, created_at) VALUES (%s, %s, %s)",
                (name, desc, now),
            )
        db.commit()

    cur.close()
    db.close()


# ---------- Auth helpers ----------

def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE id = %s", (session["user_id"],))
    return cur.fetchone()


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
        password = request.form.get("password", "")

        if not username:
            return render_template("login.html", error="Type a username, dummy.")
        if len(username) > 20:
            return render_template("login.html", error="Username too long (max 20 chars).")
        if not password:
            return render_template("login.html", error="Type a password too.")
        if len(password) < 4:
            return render_template("login.html", error="Password needs to be at least 4 characters.")

        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cur.fetchone()

        if user is None:
            # Brand new account — create it with this password
            cur.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (%s, %s, %s) RETURNING *",
                (username, generate_password_hash(password), now_iso()),
            )
            user = cur.fetchone()
            db.commit()
        elif not user["password_hash"]:
            # Existing account from before passwords existed — set their password now
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (generate_password_hash(password), user["id"]),
            )
            db.commit()
        else:
            # Existing account with a password — verify it
            if not check_password_hash(user["password_hash"], password):
                return render_template("login.html", error="Wrong password.")

        session["user_id"] = user["id"]
        return redirect(url_for("communities"))

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/about")
def about():
    return render_template("about.html", user=current_user())


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
    cur = db.cursor()
    cur.execute("""
        SELECT c.*, COUNT(p.id) as post_count
        FROM communities c
        LEFT JOIN posts p ON p.community_id = c.id
        GROUP BY c.id
        ORDER BY c.name ASC
    """)
    comms = cur.fetchall()
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
    cur = db.cursor()
    try:
        cur.execute(
            "INSERT INTO communities (name, description, created_at) VALUES (%s, %s, %s)",
            (name, desc, now_iso()),
        )
        db.commit()
    except psycopg2.IntegrityError:
        db.rollback()  # community already exists, ignore
    return redirect(url_for("communities"))


# ---------- Routes: Feed (TikTok-style scroll) ----------

@app.route("/feed/<int:community_id>")
def feed(community_id):
    redir = login_required_redirect()
    if redir:
        return redir
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM communities WHERE id = %s", (community_id,))
    community = cur.fetchone()
    if community is None:
        return redirect(url_for("communities"))
    return render_template("feed.html", community=community, user=current_user())


@app.route("/api/posts/<int:community_id>")
def api_posts(community_id):
    redir = login_required_redirect()
    if redir:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]
    cur.execute("""
        SELECT p.*, u.username,
            (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) as like_count,
            (SELECT COUNT(*) FROM comments cm WHERE cm.post_id = p.id) as comment_count,
            (SELECT COUNT(*) FROM likes l2 WHERE l2.post_id = p.id AND l2.user_id = %s) as liked_by_me
        FROM posts p
        JOIN users u ON u.id = p.user_id
        WHERE p.community_id = %s
        ORDER BY p.created_at DESC
    """, (uid, community_id))
    rows = cur.fetchall()
    posts = [dict(r) for r in rows]
    for p in posts:
        p["liked_by_me"] = bool(p["liked_by_me"])
        p["like_count"] = int(p["like_count"])
        p["comment_count"] = int(p["comment_count"])
    return jsonify(posts)


# ---------- Routes: Posting ----------

@app.route("/post/new/<int:community_id>", methods=["GET", "POST"])
def new_post(community_id):
    redir = login_required_redirect()
    if redir:
        return redir
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM communities WHERE id = %s", (community_id,))
    community = cur.fetchone()
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
                    try:
                        resource_type = "image" if post_type == "image" else "video"
                        upload_result = cloudinary.uploader.upload(
                            file,
                            resource_type=resource_type,
                            public_id=f"win95social/{uuid.uuid4().hex}",
                        )
                        media_path = upload_result["secure_url"]
                    except Exception as e:
                        return render_template("new_post.html", community=community, user=current_user(),
                                                error=f"Upload failed: {e}")
                else:
                    return render_template("new_post.html", community=community, user=current_user(),
                                            error="That file type isn't allowed for this post type.")
            else:
                return render_template("new_post.html", community=community, user=current_user(),
                                        error="You gotta attach a file for that post type.")

        if not title and not body and not media_path:
            return render_template("new_post.html", community=community, user=current_user(),
                                    error="Post can't be totally empty.")

        cur.execute("""
            INSERT INTO posts (community_id, user_id, post_type, title, body, media_path, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (community_id, session["user_id"], post_type, title, body, media_path, now_iso()))
        db.commit()
        return redirect(url_for("feed", community_id=community_id))

    return render_template("new_post.html", community=community, user=current_user(), error=None)


# ---------- Routes: Likes ----------

@app.route("/api/like/<int:post_id>", methods=["POST"])
def api_like(post_id):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]
    cur.execute("SELECT * FROM likes WHERE post_id = %s AND user_id = %s", (post_id, uid))
    existing = cur.fetchone()
    if existing:
        cur.execute("DELETE FROM likes WHERE id = %s", (existing["id"],))
        db.commit()
        liked = False
    else:
        cur.execute("INSERT INTO likes (post_id, user_id, created_at) VALUES (%s, %s, %s)",
                     (post_id, uid, now_iso()))
        db.commit()
        liked = True
    cur.execute("SELECT COUNT(*) as c FROM likes WHERE post_id = %s", (post_id,))
    count = cur.fetchone()["c"]
    return jsonify({"liked": liked, "like_count": count})


# ---------- Routes: Comments ----------

@app.route("/api/comments/<int:post_id>")
def api_get_comments(post_id):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT cm.*, u.username
        FROM comments cm
        JOIN users u ON u.id = cm.user_id
        WHERE cm.post_id = %s
        ORDER BY cm.created_at ASC
    """, (post_id,))
    rows = cur.fetchall()
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
    cur = db.cursor()
    uid = session["user_id"]
    cur.execute("INSERT INTO comments (post_id, user_id, body, created_at) VALUES (%s, %s, %s, %s)",
                (post_id, uid, body, now_iso()))
    db.commit()
    cur.execute("SELECT COUNT(*) as c FROM comments WHERE post_id = %s", (post_id,))
    count = cur.fetchone()["c"]
    return jsonify({"ok": True, "comment_count": count})


# ---------- Routes: Profile ----------

@app.route("/profile/<username>")
def profile(username):
    redir = login_required_redirect()
    if redir:
        return redir
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    profile_user = cur.fetchone()
    if profile_user is None:
        return redirect(url_for("communities"))
    cur.execute("""
        SELECT p.*, c.name as community_name,
            (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) as like_count,
            (SELECT COUNT(*) FROM comments cm WHERE cm.post_id = p.id) as comment_count
        FROM posts p
        JOIN communities c ON c.id = p.community_id
        WHERE p.user_id = %s
        ORDER BY p.created_at DESC
    """, (profile_user["id"],))
    posts = cur.fetchall()
    return render_template("profile.html", profile_user=profile_user, posts=posts, user=current_user())


@app.route("/profile/<username>/bio", methods=["POST"])
def update_bio(username):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    me = current_user()
    if me["username"] != username:
        return jsonify({"error": "not your profile"}), 403
    data = request.get_json(silent=True) or {}
    bio = (data.get("bio") or "").strip()[:280]
    cur.execute("UPDATE users SET bio = %s WHERE id = %s", (bio, me["id"]))
    db.commit()
    return jsonify({"ok": True, "bio": bio})


# Initialize DB tables on startup (safe to call every boot, uses IF NOT EXISTS)
init_db()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
