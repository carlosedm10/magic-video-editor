"""PyInstaller entry script. PyInstaller needs an actual .py file to analyze
(not a console_scripts reference), so this just calls the real `mve` entry
point (magic_video_editor.app:main -- uvicorn thread + pywebview window)."""

from magic_video_editor.app import main

if __name__ == "__main__":
    main()
