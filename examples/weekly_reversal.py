"""Survivorship-bias and low-price stocks strategy

- Weekly reversal contrarian strategy (Lo and Mackinlay 1990, and others)
- Structural change with unknown breakpoint
- Implementation slippage

Copyright 2022, Terence Lim

MIT License
"""
import finds.display
def show(df, latex=True, ndigits=4, **kwargs):
    return finds.display.show(df, latex=latex, ndigits=ndigits, **kwargs)
figext = '.jpg'

import os
import glob
import time
import sys
import numpy as np
import pandas as pd
from pandas import DataFrame, Series, to_datetime
import matplotlib.pyplot as plt
from finds.database import SQL, Redis
from finds.busday import BusDay, WeeklyDay, to_date
from finds.structured import CRSP, Benchmarks
from finds.recipes import fractiles, lm
from conf import credentials, VERBOSE, paths

VERBOSE = 1

sql = SQL(**credentials['sql'], verbose=VERBOSE)
rdb = Redis(**credentials['redis'])
bd = BusDay(sql, verbose=VERBOSE)
crsp = CRSP(sql, bd, rdb=rdb, verbose=VERBOSE)
bench = Benchmarks(sql, bd)
imgdir = paths['images']

# Construct weekly reversal
begweek = 19730629     # increased stocks coverage in CRSP around this date
middate = 19850628     # increased stocks traded in CRSP around this date
endweek = 20211231     # is a Friday, so can include last week in 2020
wd = WeeklyDay(sql, 'Fri')   # Generate Friday-end weekly cal

rebaldates = wd.date_range(begweek, endweek)
retdates = wd.date_tuples(rebaldates)

# Retrieve weekly returns, standardize scores, and compute returns and i.c.
june_universe = 0  # to track when reached a June end to update universe
year = 0           # to track new year to retrieve prices in batch
results = []
lagged_exposures = Series(dtype=float)   # for "turnover" of stock weights
tic = time.time()
for rebaldate, pastdates, nextdates in zip(rebaldates[1:-1],
                                           retdates[:-1],
                                           retdates[1:]):
    # screen by June universe
    d = bd.june_universe(rebaldate)
    if d != june_universe:  # need next June's universe
        june_universe = d                        # update universe every June
        univ = crsp.get_universe(june_universe)  # usual CRSP universe screen
        univ = univ[univ['decile'] <= 5]         # drop smallest half stocks

    # retrieve new batch of prices when start each year        
    if bd.begyr(rebaldate) != year:
        year = bd.begyr(rebaldate)
        prc = crsp.get_range(dataset='daily',
                             fields=['bidlo', 'askhi', 'prc', 'retx', 'ret'],
                             date_field='date',
                             beg=year,
                             end=bd.offset(bd.endyr(year), 10),
                             use_cache=True)

    # get past week's returns, require price at start of week
    past_week = prc[prc.index.get_level_values('date') == rebaldate]['prc']\
        .reset_index()\
        .set_index('permno')\
        .join(crsp.get_ret(*pastdates).reindex(univ.index))\
        .dropna()

    # convert past week's contrarian returns to standardized exposures
    exposures = ((past_week['ret'].mean() - past_week['ret']) /
                 (past_week['ret'].std(ddof=0) * len(past_week)))

    # turnover is abs change in stock weight, scaled by total abs weight
    delta = pd.concat([exposures, -lagged_exposures], axis=1)\
              .fillna(0)\
              .sum(axis=1)\
              .abs()\
              .sum()\
              / (exposures.abs().sum() + lagged_exposures.abs().sum())
    lagged_exposures = exposures
    
    # get next week's returns
    next_week = crsp.get_ret(*nextdates).reindex(past_week.index).fillna(0)

    # get next day's prices, to compute one-day delay cost
    next_day = prc[prc.index.get_level_values('date')==bd.offset(rebaldate, 1)]\
        .reset_index()\
        .set_index('permno')\
        .drop(columns='date')\
        .reindex(past_week.index)
    avgprc = next_day[['bidlo', 'askhi', 'prc']].abs().mean(axis=1)

    # if no trade next day, then enter at askhi (long) or bidlo (short)
    bidask = next_day['askhi'].where(exposures > 0, next_day['bidlo']).abs()
    avgprc = next_day['prc'].where(next_day['prc'] > 0, bidask)

    # drift to next's day price => delay cost is -drift * exposure
    drift = (avgprc.div(next_day['prc'].abs())\
             .mul(1 + next_day['ret']) - 1).fillna(0.)

    # accumulate weekly computations
    results.append(DataFrame({'ret': exposures.dot(next_week),
                              'ic': exposures.corr(next_week),
                              'n': len(next_week),
                              'beg': nextdates[0],
                              'end': nextdates[1],
                              'absweight': np.sum(exposures.abs()),
                              'turnover': delta,
                              'vol': next_week.std(ddof=0),
                              'delay': exposures.dot(-drift)},
                             index=[rebaldate]))
    print(rebaldate, *pastdates, *nextdates, len(exposures),
          int(time.time()-tic))

# Combine accumulated weekly computations
df = pd.concat(results, axis=0)
dates = df.index

# Plot returns, i.c. and cross-sectional vol over time
fig, ax = plt.subplots(num=1, clear=True, figsize=(10, 5))
df.index = pd.DatetimeIndex(df.index.astype(str))
df['ret'].cumsum().plot(ax=ax, ls='--', color='r', rot=0)
ax.legend(['cumulative returns'], loc='upper left')
ax.set_ylabel('cumulative returns')
bx = ax.twinx()
df['ic'].plot(ax=bx, ls=':', lw=1, rot=0)
df['vol'].plot(ax=bx, ls=':', lw=1, rot=0)
bx.axhline(df['ic'].mean(), linestyle='-', color='C0', lw=2)
bx.axhline(0, linestyle='-', color='black', lw=1)
bx.legend(['information coefficient', 'cross-sectional vol'], loc='lower right')
ax.set_title('Profitability of Weekly Mean Reversion')
plt.tight_layout(pad=2)
plt.savefig(os.path.join(imgdir, 'weekrev' + figext))
plt.show()

show(pd.concat([df[['ic' ,'vol', 'ret']].mean(axis=0).rename('mean'),
                df[['ic' ,'vol', 'ret']].std(axis=0).rename('std')],
               axis=1),
     caption=f'Alpha, IC and Vol of Weekly Mean Reversion Strategy')

# Structural Break Test with Unknown Date
import rpy2.robjects as ro
from rpy2.robjects.packages import importr
from finds.pyR import PyR
importr('strucchange')   # R package to use

v# Set up data and formulas for R
Y = df['ret']
#dates = to_datetime(df.index.astype(str))
formula = ro.Formula('y ~ 1')
formula.environment['y'] = PyR(Y.values).ro

# Call R strucchange routines to compute breakpoint statistics
fstats_r = ro.r['Fstats'](formula, **{'from': 1})    # Fstats at every break
breakpoints_r = ro.r['breakpoints'](formula)         # candidate breakpoints
confint_r = ro.r['confint'](breakpoints_r, breaks=1) # conf interval for 1 break
sctest_r = ro.r['sctest'](fstats_r, **{'type': 'aveF'})

# Extract output from R results
confint = PyR(confint_r[0]).frame.iloc[0].astype(int) - 1  # R index starts at 1
output = dict(zip(confint.index, df.index[confint]))       # confidence interval
for k,v in zip(sctest_r.slots['names'][:3], sctest_r[:3]): # significance values
    output[k] = PyR(v).values[0]
output['mean(pre)'] = Y[df.index <= output['breakpoints']].mean() 
output['mean(post)'] = Y[df.index > output['breakpoints']].mean()
fstat = [0] + list(PyR(fstats_r[0]).values) + [0, 0]  # pad beyond from and to 

# Plot breakpoint F-stats
fig, ax = plt.subplots(num=2, clear=True, figsize=(10, 5))
ax.plot(df.index, fstat, color='C0')
arg = np.nanargmax(fstat)
ax.axvline(df.index[arg], color='C1')
ax.axvspan(df.index[confint[0]], df.index[confint[2]], alpha=0.3, color='grey')
ax.legend(['F-stat', 'Max-F', 'C.I. of Break Date'])
ax.annotate(df.index[arg].strftime('%Y-%m-%d'), xy=(df.index[arg], fstat[arg]))
ax.set_ylabel('F-statistic at Breakpoints')
ax.set_xlabel('Date of Breakpoints')
ax.set_title('Structural Change with Unknown Breakpoint: '
             'Weekly Mean Reversion Spread Returns')
plt.tight_layout(pad=3)
plt.savefig(os.path.join(imgdir, 'break' + figext))
plt.show()

# Compute gross annualized sharpe ratio and delay slippage
market = bench.get_series(permnos=['Mkt-RF'], field='ret').reset_index()
breakpoint = to_date(output['breakpoints'])
out = dict()
for period, select in enumerate([dates > 0,
                                 dates > breakpoint,
                                 dates <= breakpoint,
                                 (dates > middate) & (dates <= breakpoint),
                                 dates <= middate]):
    res = df[select].copy()
    res.index = dates[select]

    # align market returns and compute market regression beta
    #res['date'] = res.index
    res['mkt'] = [(1 + market[market['date'].between(*dt)]['Mkt-RF']).prod() - 1
                  for dt in res[['beg','end']].itertuples(index=False)]
    model = lm(res['mkt'], res['ret'], flatten=True)
    
    # save df summary
    out[f"Period {period}"] = {
        'start date': min(res.index),
        'end date': max(res.index),
        'Sharpe Ratio': np.sqrt(52)*res['ret'].mean()/res['ret'].std(),
        'Average Return': res['ret'].mean(),
        'Std Dev Return': res['ret'].std(),
        'Market Beta': model.coefficients[1],
        'Jensen Alpha (annualized)': model.coefficients[0] * 52,
        'Appraisal Ratio': np.sqrt(52) * model.coefficients[0] / model.stderr,
        'Information Coefficient': res['ic'].mean(),
        'Cross-sectional Vol': res['vol'].mean(),
        'Delay cost': res['delay'].mean(),
        'Turnover Fraction': res['turnover'].mean(),
        'Abs Weight': res['absweight'].mean(),
        'Num Stocks': int(res['n'].mean()),
    }

# Display as formatted DataFrame
formats = dict.fromkeys(['start date' ,'end date', 'Num Stocks'], '{:.0f}')
show(row_formatted(DataFrame(out), formats=formats, default='{:.4f}'),
     caption="Subperiod Performance of Weekly Mean Reversion Strategy")
