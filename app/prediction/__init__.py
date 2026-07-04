"""Autonomous BTC hourly prediction-market trading bot.

moomoo's prediction markets are Kalshi event contracts (moomoo x Kalshi
partnership). The bot discovers and quotes the BTC hourly series via Kalshi's
keyless public market-data API, estimates the probability that each contract
settles YES, and trades when its estimate diverges from the market-implied
probability by a configurable edge. Orders route through the moomoo OpenD
gateway in live mode and through a local paper simulator otherwise.
"""
