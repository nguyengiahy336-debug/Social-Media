import os
import uuid
from datetime import datetime, timezone, timedelta

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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS poll_options (
            id SERIAL PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts (id),
            option_text TEXT NOT NULL,
            option_order INTEGER NOT NULL DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS poll_votes (
            id SERIAL PRIMARY KEY,
            poll_option_id INTEGER NOT NULL REFERENCES poll_options (id),
            user_id INTEGER NOT NULL REFERENCES users (id),
            post_id INTEGER NOT NULL REFERENCES posts (id),
            created_at TEXT NOT NULL,
            UNIQUE(post_id, user_id)
        )
    """)
    # Migration: mark edited posts/comments and allow soft state tracking
    cur.execute("""
        ALTER TABLE posts ADD COLUMN IF NOT EXISTS edited_at TEXT DEFAULT ''
    """)
    cur.execute("""
        ALTER TABLE comments ADD COLUMN IF NOT EXISTS edited_at TEXT DEFAULT ''
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id SERIAL PRIMARY KEY,
            recipient_id INTEGER NOT NULL REFERENCES users (id),
            actor_id INTEGER NOT NULL REFERENCES users (id),
            notif_type TEXT NOT NULL,
            post_id INTEGER REFERENCES posts (id),
            message_preview TEXT DEFAULT '',
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS direct_messages (
            id SERIAL PRIMARY KEY,
            sender_id INTEGER NOT NULL REFERENCES users (id),
            recipient_id INTEGER NOT NULL REFERENCES users (id),
            body TEXT NOT NULL,
            is_read BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS follows (
            id SERIAL PRIMARY KEY,
            follower_id INTEGER NOT NULL REFERENCES users (id),
            followed_id INTEGER NOT NULL REFERENCES users (id),
            created_at TEXT NOT NULL,
            UNIQUE(follower_id, followed_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ads (
            id SERIAL PRIMARY KEY,
            community_id INTEGER NOT NULL REFERENCES communities (id),
            ad_type TEXT NOT NULL DEFAULT 'custom',
            title TEXT DEFAULT '',
            body TEXT DEFAULT '',
            media_path TEXT DEFAULT '',
            link_url TEXT DEFAULT '',
            sponsor_name TEXT DEFAULT '',
            network_slot_html TEXT DEFAULT '',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            expires_at TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    # Migration: add expires_at to an ads table that existed before this column did
    cur.execute("""
        ALTER TABLE ads ADD COLUMN IF NOT EXISTS expires_at TEXT DEFAULT ''
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


def is_admin(user):
    if user is None:
        return False
    admin_username = os.environ.get("ADMIN_USERNAME", "")
    return admin_username != "" and user["username"] == admin_username


def allowed_file(filename, allowed_set):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_set


def create_notification(cur, recipient_id, actor_id, notif_type, post_id=None, message_preview=""):
    # Don't notify yourself about your own actions
    if recipient_id == actor_id:
        return
    cur.execute("""
        INSERT INTO notifications (recipient_id, actor_id, notif_type, post_id, message_preview, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (recipient_id, actor_id, notif_type, post_id, message_preview, now_iso()))


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
    me = current_user()
    return render_template("feed.html", community=community, user=me, is_admin_user=is_admin(me))


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
        p["is_ad"] = False

    cur.execute("""
        SELECT * FROM ads
        WHERE community_id = %s AND is_active = TRUE
          AND (expires_at = '' OR expires_at > %s)
        ORDER BY id ASC LIMIT 3
    """, (community_id, now_iso()))
    ads = [dict(r) for r in cur.fetchall()]
    for a in ads:
        a["is_ad"] = True

    # Sprinkle ads evenly through the feed rather than clustering them at the top
    if ads and posts:
        merged = []
        spacing = max(len(posts) // (len(ads) + 1), 1)
        ad_idx = 0
        for i, post in enumerate(posts):
            merged.append(post)
            if ad_idx < len(ads) and (i + 1) % spacing == 0:
                merged.append(ads[ad_idx])
                ad_idx += 1
        # Any leftover ads (e.g. short feed) get appended at the end
        while ad_idx < len(ads):
            merged.append(ads[ad_idx])
            ad_idx += 1
        return jsonify(merged)
    elif ads and not posts:
        return jsonify(ads)

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
        poll_options_clean = []

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

        if post_type == "poll":
            if not title:
                return render_template("new_post.html", community=community, user=current_user(),
                                        error="Your poll needs a question (use the title field).")
            raw_options = request.form.getlist("poll_option")
            poll_options_clean = [o.strip() for o in raw_options if o.strip()]
            if len(poll_options_clean) < 2:
                return render_template("new_post.html", community=community, user=current_user(),
                                        error="Polls need at least 2 options.")
            if len(poll_options_clean) > 6:
                return render_template("new_post.html", community=community, user=current_user(),
                                        error="Polls can have at most 6 options.")

        if post_type != "poll" and not title and not body and not media_path:
            return render_template("new_post.html", community=community, user=current_user(),
                                    error="Post can't be totally empty.")

        cur.execute("""
            INSERT INTO posts (community_id, user_id, post_type, title, body, media_path, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
        """, (community_id, session["user_id"], post_type, title, body, media_path, now_iso()))
        new_post_id = cur.fetchone()["id"]

        if post_type == "poll":
            for idx, option_text in enumerate(poll_options_clean):
                cur.execute(
                    "INSERT INTO poll_options (post_id, option_text, option_order) VALUES (%s, %s, %s)",
                    (new_post_id, option_text, idx),
                )

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
        cur.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))
        post_owner = cur.fetchone()
        if post_owner:
            create_notification(cur, post_owner["user_id"], uid, "like", post_id=post_id)
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
    cur.execute("SELECT user_id FROM posts WHERE id = %s", (post_id,))
    post_owner = cur.fetchone()
    if post_owner:
        create_notification(cur, post_owner["user_id"], uid, "comment", post_id=post_id,
                             message_preview=body[:80])
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

    cur.execute("SELECT COUNT(*) as c FROM follows WHERE followed_id = %s", (profile_user["id"],))
    follower_count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM follows WHERE follower_id = %s", (profile_user["id"],))
    following_count = cur.fetchone()["c"]

    is_following = False
    me = current_user()
    if me and me["id"] != profile_user["id"]:
        cur.execute("SELECT 1 FROM follows WHERE follower_id = %s AND followed_id = %s",
                    (me["id"], profile_user["id"]))
        is_following = cur.fetchone() is not None

    return render_template("profile.html", profile_user=profile_user, posts=posts, user=me,
                            follower_count=follower_count, following_count=following_count,
                            is_following=is_following)


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


@app.route("/api/follow/<username>", methods=["POST"])
def api_follow(username):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]

    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    target = cur.fetchone()
    if target is None:
        return jsonify({"error": "user not found"}), 404
    if target["id"] == uid:
        return jsonify({"error": "can't follow yourself"}), 400

    cur.execute("SELECT * FROM follows WHERE follower_id = %s AND followed_id = %s", (uid, target["id"]))
    existing = cur.fetchone()

    if existing:
        cur.execute("DELETE FROM follows WHERE id = %s", (existing["id"],))
        db.commit()
        following = False
    else:
        cur.execute("INSERT INTO follows (follower_id, followed_id, created_at) VALUES (%s, %s, %s)",
                     (uid, target["id"], now_iso()))
        create_notification(cur, target["id"], uid, "follow")
        db.commit()
        following = True

    cur.execute("SELECT COUNT(*) as c FROM follows WHERE followed_id = %s", (target["id"],))
    follower_count = cur.fetchone()["c"]
    return jsonify({"following": following, "follower_count": follower_count})


# ---------- Routes: Notifications ----------

@app.route("/notifications")
def notifications_page():
    redir = login_required_redirect()
    if redir:
        return redir
    return render_template("notifications.html", user=current_user())


@app.route("/api/notifications")
def api_notifications():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]
    cur.execute("""
        SELECT n.*, u.username as actor_username
        FROM notifications n
        JOIN users u ON u.id = n.actor_id
        WHERE n.recipient_id = %s
        ORDER BY n.created_at DESC
        LIMIT 50
    """, (uid,))
    rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/notifications/unread_count")
def api_notifications_unread_count():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]
    cur.execute("SELECT COUNT(*) as c FROM notifications WHERE recipient_id = %s AND is_read = FALSE", (uid,))
    count = cur.fetchone()["c"]
    cur.execute("SELECT COUNT(*) as c FROM direct_messages WHERE recipient_id = %s AND is_read = FALSE", (uid,))
    dm_count = cur.fetchone()["c"]
    return jsonify({"unread_notifications": count, "unread_messages": dm_count})


@app.route("/api/notifications/mark_read", methods=["POST"])
def api_notifications_mark_read():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]
    cur.execute("UPDATE notifications SET is_read = TRUE WHERE recipient_id = %s", (uid,))
    db.commit()
    return jsonify({"ok": True})


# ---------- Routes: Direct Messages ----------

@app.route("/messages")
def messages_page():
    redir = login_required_redirect()
    if redir:
        return redir
    return render_template("messages.html", user=current_user(), other_username=None)


@app.route("/messages/<username>")
def messages_thread_page(username):
    redir = login_required_redirect()
    if redir:
        return redir
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    other_user = cur.fetchone()
    if other_user is None:
        return redirect(url_for("messages_page"))
    return render_template("messages.html", user=current_user(), other_username=username)


@app.route("/api/messages/conversations")
def api_conversations():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]
    # Get the most recent message per conversation partner
    cur.execute("""
        SELECT
            other_user_id,
            u.username as other_username,
            (SELECT body FROM direct_messages dm2
             WHERE (dm2.sender_id = %s AND dm2.recipient_id = other_user_id)
                OR (dm2.sender_id = other_user_id AND dm2.recipient_id = %s)
             ORDER BY dm2.created_at DESC LIMIT 1) as last_message,
            (SELECT created_at FROM direct_messages dm3
             WHERE (dm3.sender_id = %s AND dm3.recipient_id = other_user_id)
                OR (dm3.sender_id = other_user_id AND dm3.recipient_id = %s)
             ORDER BY dm3.created_at DESC LIMIT 1) as last_message_at,
            (SELECT COUNT(*) FROM direct_messages dm4
             WHERE dm4.sender_id = other_user_id AND dm4.recipient_id = %s AND dm4.is_read = FALSE) as unread_count
        FROM (
            SELECT recipient_id as other_user_id FROM direct_messages WHERE sender_id = %s
            UNION
            SELECT sender_id as other_user_id FROM direct_messages WHERE recipient_id = %s
        ) AS partners
        JOIN users u ON u.id = partners.other_user_id
        ORDER BY last_message_at DESC
    """, (uid, uid, uid, uid, uid, uid, uid))
    rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/messages/<username>")
def api_get_messages(username):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    other_user = cur.fetchone()
    if other_user is None:
        return jsonify({"error": "user not found"}), 404
    other_id = other_user["id"]

    cur.execute("""
        SELECT dm.*, u.username as sender_username
        FROM direct_messages dm
        JOIN users u ON u.id = dm.sender_id
        WHERE (dm.sender_id = %s AND dm.recipient_id = %s)
           OR (dm.sender_id = %s AND dm.recipient_id = %s)
        ORDER BY dm.created_at ASC
    """, (uid, other_id, other_id, uid))
    rows = cur.fetchall()

    # Mark messages from the other user as read now that we've fetched them
    cur.execute("""
        UPDATE direct_messages SET is_read = TRUE
        WHERE sender_id = %s AND recipient_id = %s AND is_read = FALSE
    """, (other_id, uid))
    db.commit()

    return jsonify([dict(r) for r in rows])


@app.route("/api/messages/<username>", methods=["POST"])
def api_send_message(username):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    data = request.get_json(silent=True) or {}
    body = (data.get("body") or "").strip()
    if not body:
        return jsonify({"error": "empty message"}), 400
    if len(body) > 1000:
        return jsonify({"error": "message too long"}), 400

    db = get_db()
    cur = db.cursor()
    uid = session["user_id"]
    cur.execute("SELECT * FROM users WHERE username = %s", (username,))
    other_user = cur.fetchone()
    if other_user is None:
        return jsonify({"error": "user not found"}), 404
    if other_user["id"] == uid:
        return jsonify({"error": "can't message yourself"}), 400

    cur.execute("""
        INSERT INTO direct_messages (sender_id, recipient_id, body, created_at)
        VALUES (%s, %s, %s, %s) RETURNING *
    """, (uid, other_user["id"], body, now_iso()))
    new_msg = cur.fetchone()
    db.commit()
    return jsonify(dict(new_msg))


# ---------- Routes: Search ----------

@app.route("/search")
def search_page():
    redir = login_required_redirect()
    if redir:
        return redir
    query = request.args.get("q", "").strip()
    return render_template("search.html", user=current_user(), query=query)


@app.route("/api/search")
def api_search():
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"communities": [], "posts": []})

    db = get_db()
    cur = db.cursor()
    like_pattern = f"%{query}%"

    cur.execute("""
        SELECT c.*, COUNT(p.id) as post_count
        FROM communities c
        LEFT JOIN posts p ON p.community_id = c.id
        WHERE c.name ILIKE %s OR c.description ILIKE %s
        GROUP BY c.id
        ORDER BY c.name ASC
        LIMIT 20
    """, (like_pattern, like_pattern))
    communities_result = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT p.*, u.username, c.name as community_name,
            (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) as like_count,
            (SELECT COUNT(*) FROM comments cm WHERE cm.post_id = p.id) as comment_count
        FROM posts p
        JOIN users u ON u.id = p.user_id
        JOIN communities c ON c.id = p.community_id
        WHERE p.title ILIKE %s OR p.body ILIKE %s
        ORDER BY p.created_at DESC
        LIMIT 30
    """, (like_pattern, like_pattern))
    posts_result = [dict(r) for r in cur.fetchall()]

    return jsonify({"communities": communities_result, "posts": posts_result})


# ---------- Routes: Ads ----------

@app.route("/ads/new/<int:community_id>", methods=["GET", "POST"])
def new_ad(community_id):
    redir = login_required_redirect()
    if redir:
        return redir
    if not is_admin(current_user()):
        return redirect(url_for("communities"))
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM communities WHERE id = %s", (community_id,))
    community = cur.fetchone()
    if community is None:
        return redirect(url_for("communities"))

    cur.execute("""
        SELECT COUNT(*) as c FROM ads
        WHERE community_id = %s AND is_active = TRUE AND (expires_at = '' OR expires_at > %s)
    """, (community_id, now_iso()))
    active_count = cur.fetchone()["c"]

    if request.method == "POST":
        if active_count >= 3:
            return render_template("new_ad.html", community=community, user=current_user(),
                                    active_count=active_count, error="This community already has 3 active ads (the max). Deactivate one first.")

        ad_type = request.form.get("ad_type", "custom")
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        link_url = request.form.get("link_url", "").strip()
        sponsor_name = request.form.get("sponsor_name", "").strip()
        network_slot_html = request.form.get("network_slot_html", "").strip()
        expires_in_days = request.form.get("expires_in_days", "").strip()
        media_path = ""
        expires_at = ""

        if expires_in_days:
            try:
                days = int(expires_in_days)
                if days < 1 or days > 365:
                    return render_template("new_ad.html", community=community, user=current_user(),
                                            active_count=active_count, error="Expiry must be between 1 and 365 days.")
                expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
            except ValueError:
                return render_template("new_ad.html", community=community, user=current_user(),
                                        active_count=active_count, error="Expiry days must be a number.")

        if ad_type == "custom":
            if not title:
                return render_template("new_ad.html", community=community, user=current_user(),
                                        active_count=active_count, error="Custom ads need a title.")
            file = request.files.get("media")
            if file and file.filename:
                if allowed_file(file.filename, ALLOWED_IMAGE):
                    try:
                        upload_result = cloudinary.uploader.upload(
                            file, resource_type="image", public_id=f"win95social-ads/{uuid.uuid4().hex}",
                        )
                        media_path = upload_result["secure_url"]
                    except Exception as e:
                        return render_template("new_ad.html", community=community, user=current_user(),
                                                active_count=active_count, error=f"Upload failed: {e}")
                else:
                    return render_template("new_ad.html", community=community, user=current_user(),
                                            active_count=active_count, error="That image type isn't allowed.")
        elif ad_type == "network":
            if not network_slot_html:
                return render_template("new_ad.html", community=community, user=current_user(),
                                        active_count=active_count, error="Paste your ad network's embed code.")
        else:
            return render_template("new_ad.html", community=community, user=current_user(),
                                    active_count=active_count, error="Unknown ad type.")

        cur.execute("""
            INSERT INTO ads (community_id, ad_type, title, body, media_path, link_url, sponsor_name, network_slot_html, expires_at, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (community_id, ad_type, title, body, media_path, link_url, sponsor_name, network_slot_html, expires_at, now_iso()))
        db.commit()
        return redirect(url_for("manage_ads", community_id=community_id))

    return render_template("new_ad.html", community=community, user=current_user(),
                            active_count=active_count, error=None)


@app.route("/ads/manage/<int:community_id>")
def manage_ads(community_id):
    redir = login_required_redirect()
    if redir:
        return redir
    if not is_admin(current_user()):
        return redirect(url_for("communities"))
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM communities WHERE id = %s", (community_id,))
    community = cur.fetchone()
    if community is None:
        return redirect(url_for("communities"))
    cur.execute("SELECT * FROM ads WHERE community_id = %s ORDER BY created_at DESC", (community_id,))
    ads = cur.fetchall()
    return render_template("manage_ads.html", community=community, ads=ads, user=current_user())


@app.route("/ads/<int:ad_id>/toggle", methods=["POST"])
def toggle_ad(ad_id):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "not authorized"}), 403
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM ads WHERE id = %s", (ad_id,))
    ad = cur.fetchone()
    if ad is None:
        return jsonify({"error": "ad not found"}), 404

    new_state = not ad["is_active"]
    if new_state:
        cur.execute("""
            SELECT COUNT(*) as c FROM ads
            WHERE community_id = %s AND is_active = TRUE AND (expires_at = '' OR expires_at > %s)
        """, (ad["community_id"], now_iso()))
        active_count = cur.fetchone()["c"]
        if active_count >= 3:
            return jsonify({"error": "Max 3 active ads per community"}), 400

    cur.execute("UPDATE ads SET is_active = %s WHERE id = %s", (new_state, ad_id))
    db.commit()
    return jsonify({"ok": True, "is_active": new_state})


@app.route("/ads/<int:ad_id>/delete", methods=["POST"])
def delete_ad(ad_id):
    if "user_id" not in session:
        return jsonify({"error": "not logged in"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "not authorized"}), 403
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM ads WHERE id = %s", (ad_id,))
    db.commit()
    return jsonify({"ok": True})


# Initialize DB tables on startup (safe to call every boot, uses IF NOT EXISTS)
init_db()


if __name__ == "__main__":
    app.run(debug=True, port=5000)
