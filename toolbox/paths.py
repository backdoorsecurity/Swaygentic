from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SCREENSHOTS = PROJECT / "screenshots"
DOWNLOADS = PROJECT / "downloads"


def new_screenshot():
	SCREENSHOTS.mkdir(parents=True, exist_ok=True)
	return SCREENSHOTS / "latest.png"
