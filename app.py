# Load cookies from Render Environment Variable (YT_COOKIES) or local cookies.txt
        cookie_path = workdir / "cookies.txt"
        env_cookies = os.environ.get("YT_COOKIES", "").strip()
        if env_cookies:
            cookie_path.write_text(env_cookies, encoding="utf-8")
        elif (APP_DIR / "cookies.txt").is_file():
            shutil.copy(APP_DIR / "cookies.txt", cookie_path)

        ydl_opts = {
            "format": fmt,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["tv", "web"],
                }
            },
        }

        if cookie_path.is_file():
            ydl_opts["cookiefile"] = str(cookie_path)
