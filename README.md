
               ,-.   ,.  ,-.  , ,---. .   ,
               |  ) /  \ |  ) |   |    \ /
               |-'  |--| |-<  |   |     Y
               |    |  | |  \ |   |     |
               '    '  ' '  ' '   '     '
# The Challenge

This is our bot for the Optiver Optibook trading competition in 2025. The board has
a few stocks (some of them listed twice), futures on the stocks and on the index, an
ETF that tracks the index and a set of options, and the bot trades all of them at once.

# PARITY

**PARITY** is a delta-neutral market-making algorithm: it keeps a bid and an ask on
every product and hedges what it trades on the busy stock order books.

The idea is that every product on the board moves with one or more stocks in a way you
can calculate. A dual listing *is* the stock, a future is the stock plus interest, the
ETF is a basket of the index stocks and an option follows Black-Scholes. So once we
know what the stocks are worth, we know what everything is worth.

We quote each product one tick better than the other bots, but never past our
*"bound"*, the price where trading it and hedging on the stock would stop making money.
If someone else quotes past that price, we simply trade with them.

Every trade leaves us some *"delta"*, the money we make or lose if a stock moves. We add
it up per stock over everything we hold (for TSLA that is TSLA, TSLA_DUAL, its futures and
options and its share of the ETF) and a lot of it cancels out on its own. We only hedge a
stock when its delta gets too big, and then only back halfway, so we pay the spread far
less often than a bot that hedges every trade.

This also means a trade that cancels delta we already have costs us nothing to hedge, so
on that side our bound has no hedging cost in it and we quote tighter than the others,
exactly when the trade is good for us. Our quotes also *"lean"* against what we hold, so
the market brings us back to flat.

Everything runs within the exchange limit of 25 order updates a second, with the most
urgent ones first.

Run `parity.py` from the Optibook environment, the parameters are all at the top of
the file.
