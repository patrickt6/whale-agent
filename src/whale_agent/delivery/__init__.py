"""Delivery channels: email (SMTP or Resend), Telegram, SMS, Slack.

Every sender takes an explicit `Settings` and returns a `DeliveryResult` rather than
raising, so a failed channel degrades to a logged warning instead of losing the digest
that has already been computed.
"""
