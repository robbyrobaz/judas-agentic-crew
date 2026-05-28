# judas_native Runtime Parameter Keys — Ground Truth

These are the EXACT keys portfolio_runtime.py reads. Any other key name is silently ignored.

## judas_native execution engine

```json
{
  "execution_engine": "judas_native",
  "symbol": "MGC",
  "strategy_name": "my_strategy",
  "strategy_family": "judas_1h",
  "timeframe": "1H",
  "target_r": 2.0,
  "stop_buffer_ticks": 2,
  "min_sweep_ticks": 3,
  "confirmation_bars": 4,
  "pivot_length": 2,
  "detector_lookback_bars": 120,
  "min_displacement_strength": 1.5,
  "min_displacement_body_ratio": 0.5,
  "max_sweep_age_bars": 4
}
```

### WRONG keys (silently ignored by runtime — do NOT use):
- `displacement` → correct key is `min_displacement_strength`
- `body_ratio` → correct key is `min_displacement_body_ratio`
- `body_ratio_thr` → correct key is `min_displacement_body_ratio`
- `disp` → correct key is `min_displacement_strength`
- `sweep_age` → correct key is `max_sweep_age_bars`

## buffet_zoo execution engine

### RSI subtype
```json
{
  "execution_engine": "buffet_zoo",
  "strategy_type": "rsi",
  "period": 14,
  "lo_thr": 25.0,
  "hi_thr": 75.0,
  "target_r": 1.5,
  "stop_atr_mult": 1.0
}
```

### Bollinger subtype
```json
{
  "execution_engine": "buffet_zoo",
  "strategy_type": "bollinger",
  "period": 20,
  "n_std": 2.0,
  "target_r": 1.5,
  "stop_atr_mult": 1.0
}
```

### MA cross subtype
```json
{
  "execution_engine": "buffet_zoo",
  "strategy_type": "ma_cross",
  "fast": 9,
  "slow": 21,
  "target_r": 2.0,
  "stop_atr_mult": 1.5
}
```
