import re
from math import log, sqrt, exp, erf
from datetime import datetime, timezone, timedelta

# All the products on Optibook are stocks or functions of stocks:
#   dual listing -> S, the same stock on another book
#   future       -> S * exp(r * tau)
#   ETF          -> C + M * X, where X = sum(w_i * S_i) / divisor is the index
#   option       -> Black-Scholes with one flat volatility
# The names are from the Optibook guide (NVDA, NVDA_DUAL, NVDA_202506_F, 
# NVDA_202506_110C, OB5X_ETF) and everything expires on the third friday of the 
# month at 12:00 UTC. If the exchange gives us the metadata we use that instead.

YEAR = 365 * 24 * 3600.0
FUTURE = re.compile(r'^(.+)_(\d{4})(\d{2})_F$')
OPTION = re.compile(r'^(.+)_(\d{4})(\d{2})_(\d+(?:\.\d+)?)(CALL|PUT|C|P)$', re.IGNORECASE)
ETF_TERMS = (0.25, 2.50) # multiplier and cash of OB5X_ETF from the guide

def normCdf(x):
    return (1 + erf(x / sqrt(2))) / 2

def blackScholes(spot, strike, tau, rate, vol, is_call):
    '''
    Black-Scholes price and delta of a european option. 
    
    Returns
    -------
    price, delta : float, float
    '''
    if tau <= 0 or vol <= 0:
        if is_call:
            return max(0.0, spot - strike), float(spot > strike)
        return max(0.0, strike - spot), -float(spot < strike)
    d1 = (log(spot / strike) + (rate + vol * vol / 2) * tau) / (vol * sqrt(tau))
    n1, n2 = normCdf(d1), normCdf(d1 - vol * sqrt(tau))
    discounted = strike * exp(-rate * tau)
    if is_call:
        return spot * n1 - discounted * n2, n1
    return discounted * (1 - n2) - spot * (1 - n1), n1 - 1

def impliedVol(price, spot, strike, tau, rate, is_call):
    '''
    Finds the volatility that gives `price` with bisection. Returns None if there is 
    none, which usually means the quote is stale
    '''
    low, high = 1e-4, 20.0
    value = lambda vol: blackScholes(spot, strike, tau, rate, vol, is_call)[0]
    if tau <= 0 or not value(low) < price < value(high):
        return None
    for _ in range(60):
        mid = (low + high) / 2
        low, high = (mid, high) if value(mid) < price else (low, mid)
    return (low + high) / 2

def yearsUntil(expiry):
    return 0.0 if expiry is None else max(0.0, (expiry - datetime.now(timezone.utc)).total_seconds() / YEAR)

def thirdFriday(year, month):
    first = datetime(year, month, 1, 12, tzinfo=timezone.utc)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)

class Spec:
    '''
    Everything we need to know about one instrument
    '''

    def __init__(self, iid, tick):
        self.id, self.tick, self.kind = iid, tick, 'stock'
        self.base = self.stock = self.index = self.expiry = self.rate = None
        self.strike = self.is_call = self.mult = self.cash = None

    def __repr__(self):
        return f'{self.id} ({self.kind} on {self.index or self.stock})'

def meta(info, name):
    value = getattr(info, name, None)
    return getattr(value, 'name', value) # some of the fields are enums, we want the name

def describe(iid, info):
    """
    Works out what an instrument is from its name, then from the metadata if the 
    exchange gives it (metadata wins). Returns a Spec
    """
    spec = Spec(iid, float(meta(info, 'tick_size') or 0.1))
    match = OPTION.match(iid) or FUTURE.match(iid)
    if match:
        spec.kind = 'option' if match.re is OPTION else 'future'
        spec.base, spec.expiry = match.group(1), thirdFriday(int(match.group(2)), int(match.group(3)))
        if spec.kind == 'option':
            spec.strike, spec.is_call = match.group(4), match.group(5)[0].upper() == 'C'
    elif iid.endswith('_ETF'):
        spec.kind, spec.index = 'etf', iid[:-4]

    kind = str(meta(info, 'instrument_type') or '').upper()
    spec.kind = next((k.lower() for k in ('OPTION', 'FUTURE', 'ETF') if k in kind), spec.kind)
    for attr, name in (('base', 'base_instrument_id'), ('strike', 'strike'),
                       ('mult', 'etf_multiplier'), ('cash', 'etf_cash_comp')):
        if meta(info, name) is not None:
            setattr(spec, attr, meta(info, name))
    if meta(info, 'option_kind') is not None:
        spec.is_call = str(meta(info, 'option_kind')).upper().startswith('C')
    if spec.kind == 'etf':
        spec.index = meta(info, 'index_id') or spec.index
        spec.mult = float(ETF_TERMS[0] if spec.mult is None else spec.mult)
        spec.cash = float(ETF_TERMS[1] if spec.cash is None else spec.cash)
    expiry = meta(info, 'expiry')
    try:
        expiry = datetime.fromisoformat(expiry) if isinstance(expiry, str) else expiry
    except ValueError:
        expiry = None
    if isinstance(expiry, datetime):
        spec.expiry = expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
    if meta(info, 'interest_rate') is not None:
        rate = float(meta(info, 'interest_rate'))
        spec.rate = rate / 100 if rate > 1 else rate # in case its given in percent
    spec.strike = None if spec.strike is None else float(spec.strike)
    if spec.kind == 'option' and (spec.strike is None or spec.is_call is None):
        spec.kind = 'unknown' # we cant price an option without these
    return spec

def buildUniverse(infos, extra_indices=None):
    '''
    Goes through all the instruments on the exchange.
        
    Returns
    --------
    specs : dict
        {instrument: Spec}
    listings : dict
        {stock: [instruments]}, all the books a stock trades on
    indices : dict
        {index: (weights, divisor)}
    '''
    specs = {i: describe(i, info) for i, info in infos.items()}
    indices = dict(extra_indices or {})
    for i, info in infos.items():
        name = meta(info, 'index_id') or specs[i].index or specs[i].base
        try:
            weights = {k: float(w) for k, w in dict(meta(info, 'index_constituents')).items()}
            if name and name not in indices:
                indices[name] = (weights, float(meta(info, 'index_divisor')))
        except (TypeError, ValueError):
            pass # no index on this one, or only names without weights

    stocks = {i for i, s in specs.items() if s.kind == 'stock'}
    def canonical(i):
        # NVDA_DUAL is just NVDA on a seperate book, same for PHILIPS_A and PHILIPS_B
        if i.endswith('_DUAL'):
            return i[:-5]
        twin = i[:-1] + {'A': 'B', 'B': 'A'}.get(i[-1], '')
        return i[:-2] if i[-2:] in ('_A', '_B') and twin in stocks else i

    listings = {}
    for i in sorted(stocks):
        specs[i].stock = canonical(i)
        listings.setdefault(specs[i].stock, []).append(i)
    indices = {n: ({canonical(k): w for k, w in ws.items()}, d) for n, (ws, d) in indices.items()}
    for s in specs.values():
        if s.kind in ('future', 'option') and s.base:
            s.index, s.stock = (s.base, None) if s.base in indices else (None, canonical(s.base))
    return specs, listings, indices

def underlying(spec, fair, indices):
    '''
    Price of what the instrument is written on (a stock or the index) and how much 
    it moves with every stock. None if we dont have all the prices
    '''
    if spec.index:
        weights, divisor = indices.get(spec.index, ({}, 1.0))
        if not weights or any(s not in fair for s in weights):
            return None
        return sum(w * fair[s] for s, w in weights.items()) / divisor, {s: w / divisor for s, w in weights.items()}
    return (fair[spec.stock], {spec.stock: 1.0}) if spec.stock in fair else None

def value(spec, fair, indices, vols, rate):
    """
    Theoretical value of an instrument and its delta to every stock, i.e. how many 
    lots of each stock one lot is worth. This is what we hedge.
    
    Returns
    -------
    theo, deltas : float, dict
        None if something is missing
    """
    under = underlying(spec, fair, indices)
    if under is None:
        return None
    spot, grads, = under
    if spec.kind == 'stock':
        return spot, grads
    if spec.kind == 'etf':
        theo, scale = spec.cash + spec.mult * spot, spec.mult
    else:
        tau, r = yearsUntil(spec.expiry), rate if spec.rate is None else spec.rate
        if spec.kind == 'future':
            scale = exp(r * tau)
            theo = spot * scale
        elif (spec.index or spec.stock) in vols:
            theo, scale = blackScholes(spot, spec.strike, tau, r, vols[spec.index or spec.stock], spec.is_call)
        else:
            return None
    return theo, {s: scale * g for s, g in grads.items()}
