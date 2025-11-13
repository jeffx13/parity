from collections import deque, namedtuple
from math import floor, ceil
from time import monotonic, sleep

# Functions to talk to the exchange. None of these raise, if something fails we 
# return None so one bad reply does not kill the bot

EPS = 1e-9
Level = namedtuple('Level', 'price volume')

def roundToTick(price, tick, side):
    '''
    Rounds a price to the tick size. Bids get rounded down and asks up so we never 
    give away edge by rounding (also needed because 0.1 * 3 != 0.3 with floats)
    '''
    n = price / tick
    return round((floor(n + EPS) if side == 'bid' else ceil(n - EPS)) * tick, 10)

def headroom(position, side, limit):
    '''
    How many lots we can still buy (bid) or sell (ask) before we hit the limit
    '''
    return max(0, limit - position) if side == 'bid' else max(0, limit + position)

class Book:
    """
    Order book of one instrument, best prices first. 
    
    The microprice is the mid weighted by the volume on the other side, so it leans 
    towards the side with less volume, as that is the side that trades next
    """

    def __init__(self, bids=(), asks=()):
        self.bids, self.asks = list(bids), list(asks)
        self.best_bid = self.bids[0].price if self.bids else None
        self.best_ask = self.asks[0].price if self.asks else None
        self.ok = self.best_bid is not None and self.best_ask is not None
        self.mid = self.micro = None
        if self.ok:
            qb, qa = self.bids[0].volume, self.asks[0].volume
            self.mid = (self.best_bid + self.best_ask) / 2
            self.micro = (self.best_bid * qa + self.best_ask * qb) / (qa + qb) if qa + qb else self.mid

    def without(self, orders):
        '''
        Same book but with our own orders taken out
        '''
        def strip(levels, side):
            out = []
            for lv in levels:
                left = lv.volume - sum(o.volume for o in orders if o.side == side and abs(o.price - lv.price) < EPS)
                if left > 0:
                    out.append(Level(lv.price, left))
            return out
        return Book(strip(self.bids, 'bid'), strip(self.asks, 'ask'))

    def sweep(self, lots, buying, limit=None):
        '''
        Checks how many of `lots` we could trade on this book without going past `limit`
        
        Returns
        --------
        got, average : int, float
            Lots we would get and their average price
        '''
        got, cost = 0, 0.0
        for lv in (self.asks if buying else self.bids):
            if got >= lots or limit is not None and (lv.price > limit + EPS if buying else lv.price < limit - EPS):
                break
            take = min(lv.volume, lots - got)
            got, cost = got + take, cost + take * lv.price
        return got, (cost / got if got else None)

class Throttle:
    '''
    Keeps track of how many updates we sent in the last second. The exchange allows 
    25 inserts/deletes/amends per second in total. We can keep some of them in 
    reserve so quoting never uses up what we need for hedging
    '''

    def __init__(self, rate, clock=monotonic, wait=None):
        self.rate, self.clock, self.sent = rate, clock, deque()
        self.wait = wait or (lambda: sleep(0.02))

    def free(self):
        while self.sent and self.sent[0] <= self.clock() - 1.0:
            self.sent.popleft()
        return self.rate - len(self.sent)

    def take(self, reserve=0, block=False):
        # we only send if more than `reserve` is left, if block we wait until it is
        while self.free() <= reserve:
            if not block:
                return False
            self.wait()
        self.sent.append(self.clock())
        return True

def insertOrder(e, iid, price, volume, side, kind, throttle, reserve=0, block=False):
    """
    Inserts an order if the rate limit lets us
    
    Parameters
    ----------
    iid : str
        Instrument id (no assertion is done for perfomance)
    side, kind : str
        'bid' or 'ask', and 'limit' or 'ioc'
    reserve : int
        Updates we have to leave free
    block : bool
        If True we wait for the rate limit instead of giving up
        
    Returns
    -------
    order_id : int
        None if it did not go through
    """
    if volume <= 0 or not throttle.take(reserve, block):
        return None
    try:
        reply = e.insert_order(iid, price=price, volume=int(volume), side=side, order_type=kind)
    except Exception as ex:
        print(f'Could not insert order on {iid}: {ex}',flush=True)
        return None
    if not getattr(reply, 'success', False):
        print(f'Order rejected, {side} {volume} {iid} at {price}: {getattr(reply, "reason", "")}',flush=True)
        return None
    return reply.order_id

def deleteOrder(e, iid, order_id, throttle, reserve=0, block=False):
    '''
    Deletes an order if the rate limit lets us, returns False if it does not
    '''
    if not throttle.take(reserve, block):
        return False
    try:
        result = e.delete_order(iid, order_id=order_id)
    except Exception:
        pass
    return True

def getBook(e, iid):
    '''
    Downloads the order book for an instrument. None if it failed (not the same as 
    an empty book)
    '''
    try:
        raw = e.get_last_price_book(iid)
        return Book(raw.bids, raw.asks) if raw else Book()
    except Exception:
        return None

def getOrders(e, iid):
    '''
    Our outstanding orders on an instrument, None if it failed
    '''
    try:
        return list(e.get_outstanding_orders(iid).values())
    except Exception:
        return None

def getPositions(e):
    try:
        return dict(e.get_positions())
    except Exception:
        return None
