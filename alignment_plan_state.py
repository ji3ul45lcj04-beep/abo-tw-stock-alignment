"""Persistent reference plans; daily OHLC is not an execution record."""
import copy
import datetime as dt
import json
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

FIELDS = ('suggested_buy', 'warning_line', 'stop_loss', 'tp1', 'tp2', 'tp3')

def tick_size(price):
    if price < 10:
        return Decimal('0.01')
    if price < 50:
        return Decimal('0.05')
    if price < 100:
        return Decimal('0.1')
    if price < 500:
        return Decimal('0.5')
    if price < 1000:
        return Decimal('1')
    return Decimal('5')

def floor_tick(price):
    value = Decimal(str(price))
    tick = tick_size(float(value))
    return float((value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)

def normalize_levels(levels):
    return {field: floor_tick(levels[field]) for field in FIELDS}

def widen_tight_active_stop(levels, min_risk_pct):
    entry = levels['suggested_buy']
    old_risk = entry - levels['stop_loss']
    if entry <= 0 or old_risk <= 0 or old_risk / entry * 100 >= min_risk_pct:
        return levels
    ratios = [(levels[f'tp{i}'] - entry) / old_risk for i in (1, 2, 3)]
    levels['stop_loss'] = floor_tick(entry * (1 - min_risk_pct * .01))
    new_risk = entry - levels['stop_loss']
    previous = entry
    for i, ratio in enumerate(ratios, start=1):
        target = floor_tick(entry + new_risk * ratio)
        if target <= previous:
            target = floor_tick(previous + float(tick_size(previous)))
        levels[f'tp{i}'] = target
        previous = target
    return levels

def load(path):
    p = Path(path)
    if not p.exists():
        return {'version': 1, 'plans': {}}
    data = json.loads(p.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('plans'), dict):
        raise ValueError('Invalid plan state; refusing to reset tracking')
    return data

def save(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp = p.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(p)

def update(store, key, row, bars, today=None, min_stop_risk_pct=3.0):
    today = today or dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()
    plans = store['plans']
    plan = plans.get(key)
    bull = row['alignment'] == 'BULL ALIGN'
    if plan and plan.get('rearm') and bull and bars[-1].date.isoformat() > plan['last_seen']:
        store.setdefault('archive', {}).setdefault(key, []).append(copy.deepcopy(plan))
        plan = None
    new_plan = plan is None
    if plan is None:
        if row.get('tp_status', 'WAIT BUY') != 'WAIT BUY' or not bull or not all(isinstance(row.get(f), (float, int)) for f in FIELDS):
            return row
        if not row['stop_loss'] < row['suggested_buy'] < row['tp1'] < row['tp2'] < row['tp3']:
            return row
        # A plan made after observing a daily bar cannot claim that bar's trades.
        start = max(today, bars[-1].date).isoformat()
        plan = {'created_date': start, 'levels': {f: row[f] for f in FIELDS},
                'status': 'WAIT BUY', 'entry_date': None, 'highest_tp': 0,
                'last_seen': start, 'rearm': False}
        plans[key] = plan
    else:
        plan['levels'] = normalize_levels(plan['levels'])
        if plan.get('status') == 'ENTRY TOUCHED':
            plan['levels'] = widen_tight_active_stop(plan['levels'], min_stop_risk_pct)
    levels = plan['levels']
    if plan['status'] == 'CANCELLED / ALIGN LOST' or row['alignment'] == 'NO DATA':
        result = copy.copy(row)
        result.update(levels)
        result['tp_status'] = plan['status']
        return result
    # Replay against frozen levels so repeated scans are idempotent and missing
    # scan days are covered. No pre-plan high/low can trigger an event.
    state = {
        'status': plan.get('baseline_status', 'WAIT BUY'),
        'entry_date': plan.get('baseline_entry_date'),
        'highest_tp': int(plan.get('baseline_highest_tp', 0)),
    }
    for bar in bars:
        date = bar.date.isoformat()
        if date <= plan['created_date']:
            continue
        if state['status'] in ('STOP HIT / WAIT RESET', 'TP3 GET', 'ORDER UNKNOWN'):
            break
        if state['entry_date'] is None:
            if not bar.low <= levels['suggested_buy'] <= bar.high:
                continue
            state['entry_date'] = date
            state['status'] = 'ENTRY TOUCHED'
            if bar.low <= levels['stop_loss'] or bar.high >= levels['tp1']:
                state['status'] = 'ORDER UNKNOWN'
            continue
        stop = bar.low <= levels['stop_loss']
        reached = max([state['highest_tp']] + [i for i in (1, 2, 3) if bar.high >= levels[f'tp{i}']])
        if stop and reached > state['highest_tp']:
            state['status'] = 'ORDER UNKNOWN'
        elif stop:
            state['status'] = 'STOP HIT / WAIT RESET'
        else:
            state['highest_tp'] = reached
            state['status'] = f'TP{reached} GET' if reached else 'ENTRY TOUCHED'

    # First evaluate the latest bar against yesterday's untouched order. Only
    # after that may a still-pending plan adopt today's freshly calculated levels.
    if not new_plan and bull and state['entry_date'] is None:
        if row.get('tp_status', 'WAIT BUY') != 'WAIT BUY':
            store.setdefault('archive', {}).setdefault(key, []).append(copy.deepcopy(plan))
            del plans[key]
            return row
        if all(isinstance(row.get(f), (float, int)) for f in FIELDS):
            fresh = {f: row[f] for f in FIELDS}
            if fresh['stop_loss'] < fresh['suggested_buy'] < fresh['tp1'] < fresh['tp2'] < fresh['tp3']:
                start = max(today, bars[-1].date).isoformat()
                plan.update(created_date=start, levels=fresh, status='WAIT BUY',
                            entry_date=None, highest_tp=0, last_seen=start,
                            rearm=False)
                plan.pop('baseline_status', None)
                plan.pop('baseline_entry_date', None)
                plan.pop('baseline_highest_tp', None)
                result = copy.copy(row)
                result.update(fresh)
                result['tp_status'] = 'WAIT BUY'
                return result
    if plan['status'] == 'CANCELLED / ALIGN LOST':
        state['status'] = plan['status']
    if not bull and state['entry_date'] is None:
        state['status'] = 'CANCELLED / ALIGN LOST'
    plan.update(state)
    plan['last_seen'] = max(plan['last_seen'], bars[-1].date.isoformat())
    if not bull and state['status'] in ('CANCELLED / ALIGN LOST', 'STOP HIT / WAIT RESET', 'TP3 GET'):
        plan['rearm'] = True
    result = copy.copy(row)
    result.update(levels)
    result['tp_status'] = plan['status']
    return result
