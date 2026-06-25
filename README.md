# Win95Social

A Windows 95-themed social media site. Communities, scrollable feeds (text/image/video posts), likes, comments, and simple profiles — all wrapped in that chunky gray Win95 look.

## How to run it (first time)

1. Make sure you have **Python 3.9+** installed.
2. Open a terminal in this folder.
3. Install Flask:
   ```
   pip install -r requirements.txt
   ```
4. Run the app:
   ```
   python app.py
   ```
5. Open your browser to: **http://127.0.0.1:5000**

That's it. The database (`win95social.db`) gets created automatically the first time you run it, with a few starter communities already in there (r/RetroTech, r/Gaming, r/Aviation, r/Memes, r/Crafts).

## How to run it (every time after)

Just `python app.py` again. Your posts, comments, likes, and accounts are all saved in `win95social.db` — they won't disappear when you stop the server.

## How it works

- **Login:** Just type a username, no password. First time using a username creates the account on the spot.
- **Communities:** Click one to open its feed. Create new ones from the Communities page.
- **Feed:** Scroll up/down through posts in a community, TikTok-style. Each post can be text, an image, or a video.
- **Posting:** Click "+ New Post" inside any community feed. Pick the post type with the tabs at the top.
- **Likes/Comments:** Buttons at the bottom of each post card. Comments open in a popup window.
- **Profile:** Click any username to see their posts and bio. You can edit your own bio.

## File structure

```
win95social/
├── app.py                  <- the whole backend (Flask)
├── requirements.txt
├── win95social.db          <- created automatically (your data lives here)
├── static/
│   ├── css/
│   │   ├── win95.css        <- core Win95 look (buttons, windows, scrollbars)
│   │   └── social.css       <- feed, post cards, profile, comments styling
│   └── uploads/             <- uploaded images/videos land here
└── templates/
    ├── base.html            <- shared layout + taskbar
    ├── login.html
    ├── communities.html
    ├── feed.html             <- the scrolling feed + comments popup
    ├── new_post.html
    └── profile.html
```

## When you're ready to actually host this for real

Right now it runs with Flask's built-in dev server, which is fine for testing on your own machine but isn't meant for the real internet. When you want to put this on a real domain:

1. You'll want a proper host (Render, Railway, PythonAnywhere, a VPS, etc.) — happy to walk through whichever one you pick.
2. Swap the dev server for a production one like `gunicorn`.
3. Change the `app.secret_key` in `app.py` to something random and secret (don't reuse the placeholder one).
4. SQLite is fine for small-scale stuff, but if it ever blows up in users you'd eventually want to move to something like PostgreSQL.

None of this is urgent — just flagging it for later. For now, this runs great locally.

## Known limitations (stuff to add later if you want)

- No way to delete posts/comments yet
- No way to delete or rename communities
- Videos aren't compressed, so big video files = big uploads (200MB cap is set, can be changed in `app.py`)
- Anyone can post as anyone if they know the username (no passwords) — totally fine for a personal/friends project, not fine for a real public launch
