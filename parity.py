from collections import deque
from math import ceil, log
from statistics import median
from time import monotonic, sleep

import logging
logger = logging.getLogger('client')
logger.setLevel('ERROR')

import pricing
import utils
from utils import  roundToTick, headroom

#-----------------------------------------------------------#
#               ,-.   ,.  ,-.  , ,---. .   ,                #
#               |  ) /  \ |  ) |   |    \ /                 #
#               |-'  |--| |-<  |   |     Y                  #
#               |    |  | |  \ |   |     |                  #
#               '    '  ' '  ' '   '     '                  #
#           every product is a stock in disguise            #
#-----------------------------------------------------------#

# PARITY is a delta-neutral market-making algorithm that quotes every product on 
# the Optibook board and hedges what it trades on the liquid stocks.

# Every product on the exchange is basically a stock in disguise. A dual listing is 
# the same stock on another book, a future is the stock plus interest, the ETF is a 
# basket of stocks and an option is a Black-Scholes function of one. So we price 
# everything off the liquid stock books and quote the other products one tick inside 
# the other bots, but never past the *"bound"*, the price where hedging the trade is 
# not profitable anymore. If someone quotes on the wrong side of parity we just take it.

# We add up our delta per stock over everything we hold, so a lot of our fills cancel 
# each other out. We only hedge on the liquid book when the delta of a stock gets too 
# big, which means we pay the spread a lot less often than teams that hedge every trade.
# It also means a fill that brings our delta back towards 0 costs nothing to hedge, so on 
# that side we can quote tighter than the others, exactly when we want the trade.

#--------------------------------------------------------#
#--------------------------------------------------------#
#-----------------------PARAMETERS-----------------------#
#--------------------------------------------------------#
#--------------------------------------------------------#

LIMIT = 95 # exchange limit is 100 per instrument, we keep a bit of room
RATE_LIMIT = 22 #exchange allows 25 inserts/deletes/amends per second
HEDGE_RESERVE = 6 # updates per second quoting is not allowed to use
LOOP_INTERVAL = 0.1

# Edge we want to keep after hedging (in ticks of the instrument), the edge we need 
# to take someone elses quote and the volume we quote with
EDGE = {'stock': 1.0, 'future': 1.0, 'etf': 1.5, 'option': 2.0}
TAKE_EDGE = {'stock': 0.5, 'future': 0.5, 'etf': 1.0, 'option': 1.5}
SIZE = {'stock': 20, 'future': 15, 'etf': 15, 'option': 10}
MIN_SIZE = 3

LEAN, MAX_LEAN = 0.05, 8 # ticks we move our quotes for every lot we hold
DELTA_LEAN = 0.1 # and for every lot of delta, so the market helps us get flat for free
WINDOW, QUANTILE = 30, 0.25 
EMPTY_SIDE = 8
REQUOTE, RELAX = 1, 2

# We dont hedge small deltas, our next fills on other products usually cancel them. 
# Only when a stock goes past HEDGE_THRESHOLD lots we hedge it, and only back to HEDGE_TO
HEDGE_THRESHOLD = 20
HEDGE_TO = 10
SLIPPAGE = 2
HEDGE_BUFFER = 20 # room we keep on the hedge books in case a lot of fills come at once
MAX_DELTA = 25
MIN_OPTION = 3
MIN_EXPIRY = 1 / (365 * 24) # one hour
RECALIBRATE = 300

# Set these if the case gives them, otherwise we get them from the market
VOLATILITY = None
RATE = None
INDEX_WEIGHTS = {} # e.g. {'OB5X': ({'NVDA': w, ...}, 1000.0)}

#--------------------------------------------------------#
#--------------------------------------------------------#
#-------------------------PRICING------------------------#
#--------------------------------------------------------#
#--------------------------------------------------------#

class Parity:
    """
    The bot. Keeps track of the market and our orders and does all the trading.
    
    Parameters
    ----------
    e : Exchange
        Already connected
    clock, wait : func
        Used for the rate limit. A backtest can pass its own to run in simulated time
    verbose : bool
        If True we print what we are doing
    """

    def __init__(self, e, clock=monotonic, wait=None, verbose=True):
        self.e, self.verbose = e, verbose
        self.throttle = utils.Throttle(RATE_LIMIT, clock, wait)
        self.specs, self.listings, self.indices = pricing.buildUniverse(e.get_instruments(), INDEX_WEIGHTS)
        self.hedges = {} # which listing we hedge every stock on
        self.quoted = []
        self.vols = {}
        self.rate = RATE or 0.0
        self.books, self.orders, self.fair, self.theo = {}, {}, {}, {}
        self.delta, self.seen, self.last = {}, {}, {}
        self.loops = self.traded = self.taken = self.hedged = self.moves = 0

    def warmUp(self, pump=None, samples=10):
        '''
        Looks at the market for a few loops before we start. Every stock gets hedged on 
        its listing with the most volume per tick of spread, and we quote everything 
        else we can price and hedge. Then we calibrate.
        
        Parameters
        -----------
        pump : func
            Only for backtesting, moves the market between looks
        '''
        depth = {}
        for _ in range(samples):
            if pump:
                pump()
            for i in sum(self.listings.values(), []):
                b = utils.getBook(self.e, i)
                if b and b.ok:
                    spread = max(b.best_ask - b.best_bid, self.specs[i].tick)
                    depth[i] = depth.get(i, 0) + (b.bids[0].volume + b.asks[0].volume) / spread
        self.hedges = {s: max(ids, key=lambda i: depth.get(i, 0)) for s, ids in self.listings.items()}
        
        for spec in self.specs.values():
            stocks = self.indices.get(spec.index, ({},))[0] if spec.index else [spec.stock]
            if spec.id in self.hedges.values():
                continue
            if spec.kind in EDGE and stocks and all(s in self.hedges for s in stocks) and (spec.expiry or spec.kind in ('stock', 'etf')):
                self.quoted.append(spec)
            elif self.verbose == True:
                print(f'Skipping {spec}, we cant price or hedge it',flush=True)
        self.last = utils.getPositions(self.e) or {}
        self.readMarket()
        self.calibrate()
        if self.verbose == True:
            print(f'Hedging on {sorted(self.hedges.values())}',flush=True)
            print(f'Quoting {sorted(s.id for s in self.quoted)}',flush=True)
            print(f'Rate {self.rate:.4f}, volatility {self.vols}',flush=True)

    def calibrate(self):
        '''
        Gets the numbers the exchange does not give us from the market. The volatility 
        is flat, so every option should imply the same one, and we take the median so a 
        couple of stale quotes dont matter. If the futures have no interest rate we get 
        it from their prices the same way.
        '''
        live = []
        for s in self.quoted:
            b, u, tau = self.books[s.id], pricing.underlying(s, self.fair, self.indices), pricing.yearsUntil(s.expiry)
            if b.ok and u and tau > 0:
                live.append((s, b.mid, u[0], tau))
        rates = [log(mid / spot) / tau for s, mid, spot, tau in live if s.kind == 'future' and s.rate is None]
        self.rate = median(rates) if RATE is None and rates else self.rate
        
        ivs = {}
        for s, mid, spot, tau in live:
            r = self.rate if s.rate is None else s.rate
            # options with less than a tick of time value only give noise
            if s.kind == 'option' and mid - pricing.blackScholes(spot, s.strike, tau, r, 0, s.is_call)[0] > s.tick:
                iv = pricing.impliedVol(mid, spot, s.strike, tau, r, s.is_call)
                if iv:
                    ivs.setdefault(s.index or s.stock, []).append(iv)
        for under, found in ivs.items():
            self.vols[under] = median(found) if under not in self.vols else 0.8 * self.vols[under] + 0.2 * median(found)
        if VOLATILITY != None:
            self.vols = {s.index or s.stock: VOLATILITY for s in self.quoted if s.kind == 'option'}
        self.revalue()

    def readMarket(self):
        '''
        Downloads all the books and our outstanding orders, once per loop. We take our 
        own orders out of the books so we dont compete with ourselves. The fair value 
        of every stock is the microprice of its hedge listing.
        '''
        self.orders = {s.id: utils.getOrders(self.e, s.id) for s in self.quoted}
        for i in set(self.hedges.values()) | set(self.orders):
            book = utils.getBook(self.e, i)
            # if the download failed we keep the book from last loop
            if book is not None:
                self.books[i] = book.without(self.orders[i]) if self.orders.get(i) else book
            self.books.setdefault(i, utils.Book())
        self.fair = {s: self.books[i].micro for s, i in self.hedges.items() if self.books[i].ok}
        self.revalue()

    def revalue(self):
        self.theo = {s.id: pricing.value(s, self.fair, self.indices, self.vols, self.rate) for s in self.quoted}

    def exposure(self, positions):
        '''
        Adds up our delta for every stock over everything we hold.
        
        Returns
        -------
        delta : dict
            Net delta of every stock, in lots
        blind : set
            Stocks where something we hold has no price right now. We dont hedge 
            these until we can see the whole position again
        '''
        delta, blind = {}, set()
        for i, q in positions.items():
            spec = self.specs.get(i)
            if not q or spec == None:
                continue
            priced = (None, {spec.stock: 1.0}) if spec.kind == 'stock' else self.theo.get(i)
            if priced is None:
                blind.update(self.indices.get(spec.index, ({},))[0] if spec.index else [spec.stock])
            for s, d in (priced[1] if priced else {}).items():
                delta[s] = delta.get(s, 0.0) + q * d
        return delta, blind

    def bound(self, spec, priced, lots, edge, side):
        """
        Best price we can quote on one side and still make `edge` ticks after hedging. 
        We look at what hedging `lots` would really cost on the hedge books, but only for 
        the part of the fill that adds to our delta. A fill that cancels delta we already 
        have costs nothing to hedge, so on that side we can quote tighter. The cost goes 
        negative if a hedge book is already past fair, that is the arbitrage so we leave it.
        
        Returns
        ---------
        bound : float
            The price, None if the hedge books cant take the volume
        """
        theo, deltas = priced
        cost = 0.0
        for s, d in deltas.items():
            if abs(d) > 1e-9:
                need = max(1, ceil(abs(d) * lots - 1e-9))
                got, average = self.books[self.hedges[s]].sweep(need, (d < 0) == (side == 'bid'))
                if got < need:
                    return None
                # we only pay for the part of the fill that makes our delta on s bigger, a fill 
                # that brings it back towards 0 does not need a hedge at all
                now = self.delta.get(s, 0)
                after = now + d * lots * (1 if side == 'bid' else -1)
                share = max(0.0, abs(after) - abs(now)) / (abs(d) * lots)
                cost += share * d * (self.fair[s] - average if side == 'bid' else average - self.fair[s])
        return theo - cost - edge * spec.tick if side == 'bid' else theo + cost + edge * spec.tick

    def room(self, spec, side, deltas, positions):
        '''
        Most lots we can trade on one side before we hit our limit, or the limit on one 
        of the books we would hedge on
        '''
        lots = headroom(positions.get(spec.id, 0), side, LIMIT)
        for s, d in deltas.items():
            if abs(d) > 1e-6:
                hedge_side = 'ask' if (d > 0) == (side == 'bid') else 'bid'
                lots = min(lots, int(headroom(positions.get(self.hedges[s], 0), hedge_side, LIMIT - HEDGE_BUFFER) / abs(d)))
        return lots

    def tradeable(self, spec, priced):
        # we dont trade options that are too cheap or anything about to expire
        if priced is None or spec.kind == 'option' and priced[0] < MIN_OPTION * spec.tick:
            return False
        return spec.expiry is None or pricing.yearsUntil(spec.expiry) > MIN_EXPIRY

#--------------------------------------------------------#
#--------------------------------------------------------#
#-------------------------TRADING------------------------#
#--------------------------------------------------------#
#--------------------------------------------------------#

    def takeMispricing(self, positions):
        '''
        Takes any quote on the wrong side of our bound, e.g. a dual bid above what the 
        stock costs, a future offered under its carry, the ETF away from its basket or an 
        option away from theo. hedgeDelta does the hedge straight after. Returns the lots 
        we took
        '''
        taken = 0
        for spec in self.quoted:
            priced, book, mine = self.theo[spec.id], self.books[spec.id], self.orders.get(spec.id) or []
            for side, levels in (('ask', book.bids), ('bid', book.asks)):
                if not levels or not self.tradeable(spec, priced):
                    continue
                price, resting = levels[0].price, sum(o.volume for o in mine if o.side == side)
                lots = min(levels[0].volume, self.room(spec, side, priced[1], positions) - resting)
                bound = self.bound(spec, priced, lots, TAKE_EDGE[spec.kind], side) if lots > 0 else None
                # make sure we dont trade against our own quote
                ours = any(o.side != side and (o.price >= price if side == 'ask' else o.price <= price) for o in mine)
                if bound is None or ours or (price < bound if side == 'ask' else price > bound):
                    continue
                if utils.insertOrder(self.e, spec.id, price, lots, side, 'ioc', self.throttle, HEDGE_RESERVE // 2):
                    taken += lots
                    if self.verbose:
                        print(f'We take {lots} {spec.id} at {price} ({side}), theo is {priced[0]:.2f}',flush=True)
        self.taken += taken
        return taken

    def hedgeDelta(self, positions):
        '''
        Hedges our delta for every stock on its hedge listing, but only when it gets past 
        HEDGE_THRESHOLD lots and then only back to HEDGE_TO. Smaller deltas usually get 
        cancelled by our next fills on the other products, so hedging them would mean 
        paying the spread twice. We use IOC orders so nothing is left on the liquid book, 
        at most SLIPPAGE ticks past the best price.
        '''
        self.delta, blind = self.exposure(positions)
        # biggest delta goes first in case we run out of rate limit
        for s, delta in sorted(self.delta.items(), key=lambda kv: -abs(kv[1])):
            i = self.hedges.get(s)
            if i is None or s in blind or abs(delta) < HEDGE_THRESHOLD:
                continue
            side = 'ask' if delta > 0 else 'bid'
            book = self.books[i]
            tick = self.specs[i].tick
            touch = book.best_bid if side == 'ask' else book.best_ask
            if touch != None:
                limit = roundToTick(touch + (SLIPPAGE if side == 'bid' else -SLIPPAGE) * tick, tick, side)
                want = round(abs(delta) - HEDGE_TO)
                lots = min(want, book.sweep(want, side == 'bid', limit)[0], headroom(positions.get(i, 0), side, LIMIT))
                if utils.insertOrder(self.e, i, limit, lots, side, 'ioc', self.throttle):
                    self.hedged += lots
                    self.delta[s] -= lots if delta > 0 else -lots
                    #print(f'We hedge {lots} {i} on the {side}',flush=True)

    def quotes(self, spec, positions):
        '''
        Works out where we want our bid and ask for one instrument. We go one tick 
        inside where the other bots have been, move both prices against what we hold 
        and never go past the bound. We also quote less on the side that makes our 
        position bigger.
        
        Returns
        -------
        quotes : dict
            {'bid': (price, size, bound), 'ask': (price, size, bound)}, price is None if 
            we dont quote that side. None if we dont quote at all
        '''
        priced = self.theo[spec.id]
        if not self.tradeable(spec, priced) or any(abs(self.delta.get(s, 0)) > MAX_DELTA for s in priced[1]):
            return None
        q, book = positions.get(spec.id, 0), self.books[spec.id]
        lean = LEAN * q + DELTA_LEAN * sum(d * self.delta.get(s, 0) for s, d in priced[1].items())
        lean = max(-MAX_LEAN, min(MAX_LEAN, lean)) * spec.tick
        #lean = LEAN * q * spec.tick
        out = {}
        for side, sign, other in (('bid', -1, book.best_ask), ('ask', 1, book.best_bid)):
            size, bound = min(SIZE[spec.kind], self.room(spec, side, priced[1], positions)), None
            if q and (q > 0) == (side == 'bid'):
                size = int(size * max(0.15, 1 - abs(q) / LIMIT))
            # if the hedge books cant take the size we try again with half
            while size >= MIN_SIZE and bound is None:
                bound = self.bound(spec, priced, size, EDGE[spec.kind], side)
                size = size if bound is not None else size // 2
            if bound == None:
                out[side] = (None, 0, None)
                continue
            target = priced[0] + sign * self.distance(spec, side, priced[0], book) * spec.tick - lean
            price = roundToTick(min(target, bound) if side == 'bid' else max(target, bound), spec.tick, side)
            # we dont want to cross the book, so at most one tick inside the other side
            if other is not None:
                inside = roundToTick(other + sign * spec.tick, spec.tick, side)
                price = min(price, inside) if side == 'bid' else max(price, inside)
            out[side] = (price if price >= spec.tick else None, size, bound)
        if out['bid'][0] and out['ask'][0] and out['bid'][0] >= out['ask'][0]:
            return None
        return out

    def distance(self, spec, side, theo, book):
        '''
        How many ticks away from theo we sit. We remember how far the best competing 
        quote was for the last WINDOW loops and go one tick inside the tight end of that 
        (QUANTILE). If we jumped every time the other bots moved we would waste all our 
        rate limit.
        '''
        best = book.best_bid if side == 'bid' else book.best_ask
        if best is None:
            return EMPTY_SIDE
        seen = self.seen.setdefault((spec.id, side), deque(maxlen=WINDOW))
        seen.append((theo - best if side == 'bid' else best - theo) / spec.tick)
        return sorted(seen)[int(QUANTILE * (len(seen) - 1))] - 1

    def requote(self, positions):
        """
        Moves our quotes to where quotes() wants them. Because of the rate limit we cant 
        always do everything so we sort by urgency:
            4 - pull a quote we cant keep
            3 - replace a quote that lost half its edge
            2 - place a missing quote
            1 - move a quote that is behind the other bots (furthest behind first) or half filled
            0 - move back a quote that is more generous than it needs to be
        Only the deletes of 3 and 4 can use HEDGE_RESERVE, and the rest stop after using 
        half of what is free this loop.
        """
        todo = []
        # start from a different instrument every loop, so when we run out of rate limit 
        # its not always the same ones that get left behind
        k = self.loops % max(1, len(self.quoted))
        for spec in self.quoted[k:] + self.quoted[:k]:
            mine = self.orders.get(spec.id)
            if mine == None: # we couldnt get our orders so we leave it for now
                continue
            want = self.quotes(spec, positions)
            for side in ('bid', 'ask'):
                price, size, bound = want[side] if want else (None, 0, None)
                have = [o for o in mine if o.side == side]
                todo += [(4, spec, side, o, None, 0) for o in have[1:]]
                old = have[0] if have else None
                margin = EDGE[spec.kind] * spec.tick / 2
                losing = old is not None and (bound is None or (old.price > bound + margin if side == 'bid'
                                                                else old.price < bound - margin))
                if price is None:
                    if old is not None:
                        todo.append((4 if want is None or losing else 1, spec, side, old, None, 0))
                elif old is None or losing:
                    todo.append((3 if losing else 2, spec, side, old, price, size))
                else:
                    behind = (price - old.price if side == 'bid' else old.price - price) / spec.tick
                    if behind >= REQUOTE or old.volume < size / 2 or -behind >= RELAX:
                        todo.append((0 if -behind >= RELAX else 1 + min(behind, 9) / 10, spec, side, old, price, size))
        free = self.throttle.free()
        for urgency, spec, side, old, price, size in sorted(todo, key=lambda t: -t[0]):
            if urgency < 3 and free - self.throttle.free() >= max(2, (free - HEDGE_RESERVE) // 2):
                break
            if not self.replace(spec, side, old, price, size, urgency >= 3) and urgency < 3:
                break

    def replace(self, spec, side, old, price, size, urgent):
        '''
        Deletes our old quote and inserts the new one. Returns False if the rate limit 
        does not let us, before anything is sent
        '''
        if not urgent and self.throttle.free() - (old is not None) - (price is not None) < HEDGE_RESERVE:
            return False
        if old is not None and not utils.deleteOrder(self.e, spec.id, old.order_id, self.throttle,
                                                     0 if urgent else HEDGE_RESERVE):
            return False
        if price is not None:
            utils.insertOrder(self.e, spec.id, price, size, side, 'limit', self.throttle, HEDGE_RESERVE)
        self.moves += 1
        return True

    def pullQuotes(self):
        '''
        Deletes all our oustanding quotes, waits for the rate limit if it has to
        '''
        for spec in self.quoted:
            for o in utils.getOrders(self.e, spec.id) or []:
                utils.deleteOrder(self.e, spec.id, o.order_id, self.throttle, block=True)

#--------------------------------------------------------#
#--------------------------------------------------------#
#------------------------MAIN LOOP-----------------------#
#--------------------------------------------------------#
#--------------------------------------------------------#

    def runOnce(self):
        """
        Main function to be looped over. Reads the market, takes mispricings, hedges 
        and then updates our quotes.
        """
        positions = utils.getPositions(self.e)
        if positions == None:
            return
        self.readMarket()
        if self.takeMispricing(positions):
            positions = utils.getPositions(self.e) or positions
        self.hedgeDelta(positions)
        self.requote(positions)
        self.traded += sum(abs(positions.get(s.id, 0) - self.last.get(s.id, 0)) for s in self.quoted)
        self.last = positions
        self.loops+=1
        if self.loops % RECALIBRATE == 0:
            self.calibrate()

    def status(self):
        worst = max(map(abs, self.delta.values()), default=0)
        return (f'[{self.loops}] traded {self.traded}, took {self.taken}, hedged {self.hedged}, '
                f'requotes {self.moves}, worst delta {worst:.1f}')

def main():
    """
    Main function. Connects and looks at the market, then deletes anything left from 
    a previous run before starting our main loop.
    """
    from optibook.synchronous_client import Exchange
    e = Exchange()
    e.connect()
    bot = Parity(e)
    bot.warmUp()
    #Delete any outstanding orders left from the previous run of the bot
    bot.pullQuotes()
    try:
        deadline = monotonic()
        while True:
            bot.runOnce()
            if(bot.loops % 50 == 0): # Occasionally print how we are doing
                print(bot.status(),flush=True)
            deadline = max(deadline + LOOP_INTERVAL, monotonic())
            sleep(max(0.0, deadline - monotonic()))
    except KeyboardInterrupt: # So we can safely stop the loop through Ctrl+C
        pass
    finally:
        # we always pull our quotes when we stop, also if it crashes
        bot.pullQuotes()
        print(bot.status(),flush=True)

if __name__ == '__main__':
    main()
