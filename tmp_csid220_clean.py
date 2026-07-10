import numpy as np
import pandas as pd

def evaluate(bars, params):
    if bars.empty or len(bars) < 320:
        return None
