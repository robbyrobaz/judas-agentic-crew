import json

with open('_restore_ledger/classified_full.json') as f:
    rows = json.load(f)

# For each BLOCKED row, look at active occupants to understand the conflict
print('BLOCKED-ON-SLOT-CONFLICT rows with active occupancy:')
print('='*120)
for r in rows:
    if r['verdict'] != 'BLOCKED-ON-SLOT-CONFLICT':
        continue
    csid_self = r['csid']
    same_csid_occupant = None
    diff_csid_occupants = []
    for o in r['occupants']:
        if csid_self and o['csid'] == csid_self:
            same_csid_occupant = o
        else:
            diff_csid_occupants.append(o)
    # Determine "different-timeframe sibling" bug
    diff_tf_occupants = [o for o in diff_csid_occupants if o.get('params', {}).get('timeframe') != r['tf']]
    same_tf_occupants = [o for o in diff_csid_occupants if o.get('params', {}).get('timeframe') == r['tf']]
    if csid_self:
        kind = "STRICT-BETTER-CHECK"
        if diff_tf_occupants and not same_tf_occupants:
            kind = "TIMEFRAME-BLIND-BUG-CONFLICT"
        elif same_tf_occupants and not diff_tf_occupants:
            kind = "SAME-TF-STRICT-BETTER-CHECK"
        elif same_tf_occupants and diff_tf_occupants:
            kind = "MIXED"
        if same_csid_occupant:
            kind = kind + "+SAME-CSID"
    else:
        if diff_tf_occupants and not same_tf_occupants:
            kind = "TIMEFRAME-BLIND-BUG-CONFLICT"
        elif same_tf_occupants and not diff_tf_occupants:
            kind = "SAME-TF-STRICT-BETTER-CHECK"
        else:
            kind = "MIXED"
    r['conflict_kind'] = kind
    r['diff_tf_occupants'] = diff_tf_occupants
    r['same_tf_occupants'] = same_tf_occupants
    pf_str = (f"{r['BT_PF']:.2f}" if r['BT_PF'] is not None else "  ?")
    n_str = (f"{int(r['BT_n']):3d}" if r['BT_n'] is not None else "  ?")
    er_str = (f"{r['BT_$']:9.0f}" if r['BT_$'] is not None else "       ?")
    print(f"#{r['id']:5} {r['sym']:4} {r['fam'][:40]:40} tf={str(r['tf'])[:4]:4} csid={(csid_self or '?'):>4} PF={pf_str} n={n_str} ${er_str} :: {kind}")
    for o in r['occupants']:
        o_tf = o.get('params', {}).get('timeframe', '')
        o_pf = o.get('params', {}).get('profit_factor', '?')
        print(f"      ↳ active #{o['active_id']} fam={o['family'][:35]:35} tf={o_tf[:4]:4} csid={o['csid']}")

# Tally the kinds
from collections import Counter
cnt = Counter(r.get('conflict_kind', '?') for r in rows if r['verdict'] == 'BLOCKED-ON-SLOT-CONFLICT')
print()
print('Conflict-kind tally:')
for k, c in sorted(cnt.items(), key=lambda x: -x[1]):
    print(f'  {k:55s} {c}')

# Save the extended view
with open('_restore_ledger/classified_full.json', 'w') as f:
    json.dump(rows, f, indent=2, default=str)
