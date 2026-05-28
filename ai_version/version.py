"""
Single source of truth for the app version. Bumped by build.ps1
on every release. Imported by app_ai.py for the in-app version
check and by LocationScraperAI.iss for the installer metadata.
"""

__version__ = "1.1.0"
GITHUB_REPO = "corbandavidson/indatel-location-scraper"
