"""Platform registry: one module per platform, same interface for all."""

from grokbot_publisher.platforms.instagram import Instagram
from grokbot_publisher.platforms.linkedin import LinkedIn
from grokbot_publisher.platforms.tiktok import TikTok
from grokbot_publisher.platforms.x import X
from grokbot_publisher.platforms.youtube import YouTube

REGISTRY = {cls.name: cls for cls in (Instagram, X, YouTube, LinkedIn, TikTok)}

__all__ = ["REGISTRY", "Instagram", "X", "YouTube", "LinkedIn", "TikTok"]
