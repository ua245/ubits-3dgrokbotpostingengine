"""Grokbot Publisher: posts approved content cards to official platform APIs.

The Publisher is deliberately not an AI. It is told *which* approved card to
post and then runs the same steps in the same order every time:

    safety gate -> prepare per platform -> post -> report -> log
"""

__version__ = "0.1.0"
