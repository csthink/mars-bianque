"""The minimal identity mechanism: demo accounts defined in the settings, and sessions.

Accounts come from ``BIANQUE_DEMO_USERS`` (username to bcrypt hash, ``passwords``); a
successful login returns a signed session token (``sessions``). ``authenticator`` puts
the two together. The package does not import FastAPI: the API layer reads the token
from the session cookie and asks the authenticator who it belongs to.

Service clients, the MCP and A2A clients, are not accounts: each holds a bearer token
whose hash is configured, checked in front of its endpoint (``bearer``).
"""
