"""Test 6B: read-only SQL extraction plus canonical Python opportunity.

Writes one XLSX, never a permanent database object. Credentials use the existing
OTT_DB_URL / MYSQL_PWD / MYSQL_PWD_FILE / MYSQL_CNF connection helper.
Intermediate frames and the embedded JS workbook builder live in --work-dir
(outside the project by default). --export-only reuses those reviewed frames;
it does not refresh the database. Normal execution always refreshes sources.

Primary consumption = full watch_seconds of qualified events / 60, windowed by
event_start_ts. Raw-event language counts are retained only for reconciliation.
HHI is a mathematical allocation descriptor when qualified minutes > 0; a
single-language distribution has HHI=1. This is not an evidence threshold.
SQL NTILE(5) is over all eligible profiles per window, ordered by the activity
field then profile_id. Watch and active-day comparisons remain separate.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.config import SimCalendar, load_config
from src.mart_transforms.access import profile_access_day_matrix, plan_codes
from src.mart_transforms.opportunity import profile_opportunity, eligibility_cube
from src.mart_transforms.qualification import qualified_start
from analysis.test5_5b1_reachable_window_pool import classify_access_regimes
from load_test5_analysis_tables import connect

KEY = ['profile_id', 'window_label']
LK = KEY + ['audio_language']
WINDOWS = {'BASELINE_90': ('2025-09-01', '2025-11-29', slice(0, 90)),
           'FINAL_90': ('2025-11-30', '2026-02-27', slice(90, 180))}
PROGRAMMES = ['MOVIE', 'WEB_SERIES', 'TV_CATCHUP', 'REALITY', 'DOCUMENTARY_SPECIAL']
OUT = ROOT / 'analysis_outputs/test6b_opportunity_conditioned_language_behavior.xlsx'
CHECKS = []


def check(name, failures, window='ALL', detail='', tolerance='exact'):
    n = int(failures)
    CHECKS.append(dict(check=name, window_label=window, failures=n,
                       status='PASS' if n == 0 else 'FAIL', tolerance=tolerance, detail=detail))
    print('CHECK', window, name, n, flush=True)
    if n:
        raise RuntimeError(f'STOP before interpretation: {window} {name}: {n} failures. {detail}')


def fetch(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        frame = pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])
    return frame


def load_sources(conn):
    profiles = fetch(conn, 'SELECT profile_id,account_id,profile_created_date FROM ott_viewership.profiles ORDER BY profile_id')
    cycles = fetch(conn, '''SELECT account_id,cycle_start_date,cycle_end_date,plan_family,plan_language_group
        FROM ott_viewership.subscription_cycles ORDER BY account_id,cycle_start_date''')
    cat = fetch(conn, '''SELECT content_id,parent_title_id,program_type,runtime_minutes,
        release_date,catalogue_entry_date,catalogue_exit_date FROM ott_viewership.content_catalogue ORDER BY content_id''')
    audio = fetch(conn, 'SELECT parent_title_id,language FROM ott_viewership.content_audio_languages ORDER BY parent_title_id,language')
    pvw = fetch(conn, '''SELECT profile_id,window_label,window_start,window_end,is_main_analytical_eligible,
        entitled_days_in_window,eligible_parent_title_days,mean_daily_eligible_parent_titles,
        watch_minutes,qualified_watch_minutes,qualified_watch_hours,active_days,qualified_active_days,
        qualified_starts,distinct_audio_languages_consumed,top_language,top_language_share,non_top_language_minutes,
        NTILE(5) OVER(PARTITION BY window_label ORDER BY qualified_watch_hours,profile_id) AS watch_hours_quintile,
        NTILE(5) OVER(PARTITION BY window_label ORDER BY active_days,profile_id) AS active_days_quintile
        FROM ott_viewership.profile_viewership_window WHERE is_main_analytical_eligible=1
        AND window_label IN ('BASELINE_90','FINAL_90') ORDER BY profile_id,window_label''')
    old = fetch(conn, '''SELECT profile_id,window_label,entitlement_timing,opportunity_regime,
        reachable_parent_titles_in_window FROM ott_viewership_analysis.profile_opportunity_window''')
    ranks = fetch(conn, '''SELECT profile_id,window_label,watch_hours_quintile,active_days_quintile
        FROM ott_viewership_analysis.profile_mechanism_carryforward_window''')
    schemas = fetch(conn, '''SELECT table_name,column_name,column_type FROM information_schema.columns
        WHERE table_schema='ott_viewership_analysis' ORDER BY table_name,ordinal_position''').rename(columns=str.lower)
    print('SQL aggregating eligible events to profile/window/day/parent/audio', flush=True)
    # Numeric watch_seconds totals avoid SQL division rounding. Convert once in Python.
    events = fetch(conn, '''WITH e AS (
        SELECT p.profile_id,p.window_label,DATE(v.event_start_ts) AS event_date,
               c.parent_title_id,c.program_type,v.audio_language,v.watch_seconds,
               v.watch_seconds >= CASE WHEN v.is_autoplay=1
                   THEN LEAST(480,0.20*c.runtime_minutes*60)
                   ELSE LEAST(300,0.10*c.runtime_minutes*60) END AS qualified,
               (b.parent_title_id IS NULL) AS bad_bridge,
               (DATE(v.event_start_ts)<GREATEST(c.release_date,c.catalogue_entry_date)
                OR (c.catalogue_exit_date IS NOT NULL AND DATE(v.event_start_ts)>c.catalogue_exit_date)) AS bad_asset_date
        FROM ott_viewership.view_events v
        JOIN ott_viewership.viewing_sessions s ON s.session_id=v.session_id
        JOIN ott_viewership.profile_viewership_window p ON p.profile_id=s.profile_id
             AND p.is_main_analytical_eligible=1 AND p.window_label IN ('BASELINE_90','FINAL_90')
             AND v.event_start_ts>=p.window_start AND v.event_start_ts<DATE_ADD(p.window_end,INTERVAL 1 DAY)
        JOIN ott_viewership.content_catalogue c ON c.content_id=v.content_id
        LEFT JOIN ott_viewership.content_audio_languages b ON b.parent_title_id=c.parent_title_id AND b.language=v.audio_language
        ) SELECT profile_id,window_label,event_date,parent_title_id,program_type,audio_language,
            SUM(watch_seconds) AS raw_seconds,SUM(CASE WHEN qualified THEN watch_seconds ELSE 0 END) AS qualified_seconds,
            COUNT(*) AS events,SUM(qualified) AS qualified_starts,SUM(bad_bridge) AS bad_bridge,
            SUM(bad_asset_date) AS bad_asset_date
        FROM e GROUP BY profile_id,window_label,event_date,parent_title_id,program_type,audio_language''')
    for c in ['raw_seconds','qualified_seconds','events','qualified_starts','bad_bridge','bad_asset_date']:
        events[c] = pd.to_numeric(events[c]).astype(np.int64)
    print('EXTRACT', len(events), 'daily parent/language rows;', int(events.events.sum()), 'eligible events', flush=True)
    return profiles, cycles, cat, audio, pvw, old, ranks, schemas, events


def reconstruct(profiles, cycles, cat, audio, pvw, old, events):
    cal = SimCalendar(load_config())
    check('locked_calendar', int(str(cal.start)!='2025-09-01' or str(cal.end)!='2026-02-27' or cal.n_days!=180))
    profiles.profile_created_date = pd.to_datetime(profiles.profile_created_date)
    for col in ['cycle_start_date','cycle_end_date']:cycles[col]=pd.to_datetime(cycles[col]).dt.date
    for col in ['release_date','catalogue_entry_date','catalogue_exit_date']:cat[col]=pd.to_datetime(cat[col])
    for col in ['window_start','window_end']:pvw[col]=pd.to_datetime(pvw[col])
    check('unique_profiles',profiles.profile_id.duplicated().sum())
    check('unique_catalogue_assets',cat.content_id.duplicated().sum())
    check('unique_audio_bridge',audio.duplicated(['parent_title_id','language']).sum())
    check('canonical_programme_type',(~cat.program_type.isin(PROGRAMMES)).sum())
    prev_end=pd.to_datetime(cycles.groupby('account_id').cycle_end_date.shift())
    check('overlapping_subscription_cycles',(pd.to_datetime(cycles.cycle_start_date)<=prev_end).sum())
    codes=plan_codes()
    check('recognized_plan_contexts',sum((f,g) not in codes for f,g in cycles[['plan_family','plan_language_group']].itertuples(index=False,name=None)))
    ids,has,code=profile_access_day_matrix(profiles,cycles,cal)
    parents,cube=eligibility_cube(cat,audio,cal)
    languages=sorted(audio.language.unique())
    track=pd.crosstab(audio.parent_title_id,audio.language).reindex(index=parents,columns=languages,fill_value=0).to_numpy(dtype=np.int32)
    check('6A_parent_title_count',abs(len(parents)-1000))
    check('6A_bridge_pair_count',abs(len(audio)-1904))
    check('6A_multilingual_title_count',abs(int((track.sum(axis=1)>1).sum())-555))
    check('broad_equivalent_vod',(cube[0]!=cube[1]).sum())
    reference=pd.read_csv(ROOT/'analysis_outputs/test6a_window_access_multilinguality.csv')
    for w,(_,_,sl) in WINDOWS.items():
        for r in reference[reference.window_label.eq(w)].itertuples(index=False):
            ci=0 if r.access_regime=='BROAD' else codes.index(('REGIONAL',r.access_regime.removeprefix('REGIONAL_')))
            z=cube[ci,:,sl]
            check('6A_'+r.access_regime+'_reach',abs(int(z.any(axis=1).sum())-r.reachable_parent_titles_at_least_once),w)
            check('6A_'+r.access_regime+'_title_days',abs(int(z.sum())-r.accessible_title_days),w)
    first=(profiles.profile_created_date-pd.Timestamp(cal.start)).dt.days.to_numpy()
    check('opportunity_before_profile_creation',np.count_nonzero(has & (np.arange(180)[None,:]<first[:,None])))
    # Independent interval projection verifies every actual day-plan state.
    independent=np.full(code.shape,-1,dtype=np.int16)
    account_profiles=profiles.groupby('account_id').indices
    for r in cycles.itertuples(index=False):
        ix=account_profiles.get(r.account_id)
        if ix is None:continue
        start=max(cal.day_index(r.cycle_start_date),0);end=min(cal.day_index(r.cycle_end_date),179)
        if start>end:continue
        days=np.arange(start,end+1)
        valid=days[None,:]>=first[ix,None]
        block=independent[np.ix_(ix,days)]
        block[valid]=codes.index((r.plan_family,r.plan_language_group))
        independent[np.ix_(ix,days)]=block
    check('historical_day_plan_state_no_backward_leak',np.count_nonzero(independent!=code))
    check('opportunity_outside_actual_entitled_dates',np.count_nonzero(has!=(independent>=0)))
    regimes=classify_access_regimes(pvw,profiles,cycles)
    base=pvw[KEY+['entitled_days_in_window']].merge(old,on=KEY,validate='one_to_one',how='left').merge(
        regimes,on=KEY,validate='one_to_one',suffixes=('','_reconstructed'))
    check('missing_Test5_regime',base.opportunity_regime.isna().sum())
    check('historical_regime_vs_Test5',base.opportunity_regime.ne(base.opportunity_regime_reconstructed).sum())
    check('historical_context_days',base.entitled_days_in_window.ne(base.reconstructed_context_days).sum())
    check('entitlement_timing_vs_Test5',base.entitlement_timing.ne(np.where(base.entitled_days_in_window.eq(90),'FULL_90','PARTIAL')).sum())
    # Vectorised event-day entitlement lookup, independent of any window union.
    ep=ids.get_indexer(events.profile_id);et=parents.get_indexer(events.parent_title_id)
    ed=(pd.to_datetime(events.event_date)-pd.Timestamp(cal.start)).dt.days.to_numpy()
    check('event_keys_and_days',(ep<0).sum()+(et<0).sum()+((ed<0)|(ed>=180)).sum())
    ec=code[ep,ed];valid=ec>=0
    allowed=np.zeros(len(events),bool);allowed[valid]=cube[ec[valid],et[valid],ed[valid]]
    check('events_outside_historical_title_entitlement',events.loc[~allowed,'events'].sum())
    check('events_outside_listed_audio_bridge',events.bad_bridge.sum())
    check('events_outside_playable_asset_availability',events.bad_asset_date.sum())
    # Code/day/language count cube; track sets inherit title availability.
    daily_language=np.einsum('ctd,tl->cdl',cube.astype(np.int32),track,optimize=True)
    opps=[];longs=[]
    for w,(start,end,sl) in WINDOWS.items():
        eligible_ids=pvw.loc[pvw.window_label.eq(w),'profile_id']
        subset=profiles[profiles.profile_id.isin(eligible_ids)].reset_index(drop=True)
        (pids,tids,exposure),opp=profile_opportunity(subset,cycles,cat,audio,cal,pd.Timestamp(start),pd.Timestamp(end))
        check('canonical_exposure_parent_order',int(not tids.equals(parents)),w)
        opp['window_label']=w
        counts=(exposure>0).astype(np.int32)@track
        days=exposure.astype(np.int64)@track
        positions=ids.get_indexer(pids)
        eff=code[positions,sl]
        valid=eff>=0
        dayidx=np.arange(sl.start,sl.stop)[None,:]
        dl=daily_language[np.maximum(eff,0),dayidx,:]*valid[:,:,None]
        reachdays=(dl>0).sum(axis=1)
        check('language_title_days_daily_vs_exposure',np.count_nonzero(dl.sum(axis=1)!=days),w)
        opp['reachable_parent_titles_in_window']=(exposure>0).sum(axis=1)
        opp['distinct_reachable_audio_languages']=(counts>0).sum(axis=1)
        opp['reachable_title_language_pairs_in_window']=counts.sum(axis=1)
        opp['total_reachable_title_language_days']=days.sum(axis=1)
        opp['mean_daily_reachable_audio_languages']=reachdays.sum(axis=1)/opp.entitled_days_in_window.to_numpy()
        pairshares=counts/counts.sum(axis=1)[:,None]
        dayshares=days/days.sum(axis=1)[:,None]
        opp['opportunity_top_language_pair_share']=pairshares.max(axis=1)
        opp['opportunity_top_language_day_share']=dayshares.max(axis=1)
        opp['opportunity_language_hhi_pairs']=(pairshares**2).sum(axis=1)
        opp['opportunity_language_hhi_days']=(dayshares**2).sum(axis=1)
        opp['dominant_opportunity_language_pairs']=np.array(languages)[pairshares.argmax(axis=1)]
        opp['dominant_opportunity_language_days']=np.array(languages)[dayshares.argmax(axis=1)]
        i,j=np.where(counts>0)
        lang=pd.DataFrame({'profile_id':pids.to_numpy()[i],'window_label':w,'audio_language':np.array(languages)[j],
            'reachable_parent_titles_with_language':counts[i,j],'reachable_title_language_days':days[i,j],
            'entitled_days_in_window':opp.entitled_days_in_window.to_numpy()[i],
            'days_language_reachable':reachdays[i,j],
            'mean_daily_reachable_titles_with_language':days[i,j]/opp.entitled_days_in_window.to_numpy()[i],
            'opportunity_pair_share':pairshares[i,j],'opportunity_day_share':dayshares[i,j]})
        ref=pvw[pvw.window_label.eq(w)].merge(opp,on=KEY,suffixes=('_stored','_new'),validate='one_to_one')
        for col in ['entitled_days_in_window','eligible_parent_title_days','mean_daily_eligible_parent_titles']:
            check('canonical_'+col,(~np.isclose(ref[col+'_stored'].astype(float),ref[col+'_new'].astype(float),rtol=0,atol=1e-8)).sum(),w,tolerance='absolute 1e-8')
        rr=opp.merge(old[KEY+['reachable_parent_titles_in_window']],on=KEY,suffixes=('','_old'),validate='one_to_one')
        check('Test5_reachable_title_count',rr.reachable_parent_titles_in_window.ne(rr.reachable_parent_titles_in_window_old).sum(),w)
        check('zero_reachable_titles',opp.reachable_parent_titles_in_window.le(0).sum(),w)
        check('language_days_exceed_entitled_days',(lang.days_language_reachable>lang.entitled_days_in_window).sum(),w)
        opps.append(opp);longs.append(lang)
    labels=base[KEY+['entitlement_timing','opportunity_regime']]
    op=pd.concat(opps,ignore_index=True).merge(labels,on=KEY,validate='one_to_one')
    ol=pd.concat(longs,ignore_index=True).merge(labels,on=KEY,validate='many_to_one')
    return op,ol


def consumption(events,pvw):
    e=events.copy()
    e['consumed_watch_minutes']=e.raw_seconds/60.
    e['qualified_consumed_watch_minutes']=e.qualified_seconds/60.
    raw=e.groupby(LK,as_index=False).agg(consumed_watch_minutes=('consumed_watch_minutes','sum'),
        all_event_active_days_with_language=('event_date','nunique'),
        all_event_parent_titles_consumed=('parent_title_id','nunique'))
    q=e[e.qualified_starts>0].copy()
    cl=q.groupby(LK,as_index=False).agg(qualified_consumed_watch_minutes=('qualified_consumed_watch_minutes','sum'),
        active_days_with_language=('event_date','nunique'),distinct_parent_titles_consumed_in_language=('parent_title_id','nunique'),
        qualified_starts_in_language=('qualified_starts','sum'))
    # Keep all-event-only rows with a zero meaningful-language signal.
    cl=raw.merge(cl,on=LK,how='left',validate='one_to_one').fillna(0)
    cl['is_meaningful_consumed_language']=(cl.qualified_starts_in_language>0).astype(int)
    totals=cl.groupby(KEY).qualified_consumed_watch_minutes.transform('sum')
    cl['consumed_watch_share']=cl.qualified_consumed_watch_minutes/totals
    rawtot=cl.groupby(KEY).consumed_watch_minutes.transform('sum')
    cl['all_event_watch_share']=cl.consumed_watch_minutes/rawtot
    cp=cl.groupby(KEY,as_index=False).agg(all_event_distinct_consumed_audio_languages=('audio_language','size'),
        distinct_consumed_audio_languages=('is_meaningful_consumed_language','sum'),
        consumed_watch_minutes=('consumed_watch_minutes','sum'),qualified_consumed_watch_minutes=('qualified_consumed_watch_minutes','sum'),
        top_consumed_language_watch_share=('consumed_watch_share','max'),
        consumed_title_language_pairs=('distinct_parent_titles_consumed_in_language','sum'))
    hhi=cl.assign(sq=cl.consumed_watch_share**2).groupby(KEY,as_index=False).sq.sum().rename(columns={'sq':'consumed_language_hhi'})
    cp=cp.merge(hhi,on=KEY,validate='one_to_one')
    top=cl.sort_values(KEY+['qualified_consumed_watch_minutes','audio_language'],ascending=[True,True,False,True]).drop_duplicates(KEY)
    cp=cp.merge(top[KEY+['audio_language']].rename(columns={'audio_language':'dominant_consumed_audio_language'}),on=KEY,validate='one_to_one')
    cp['consumed_language_hhi_is_valid']=cp.qualified_consumed_watch_minutes.gt(0).astype(int)
    cp['non_top_consumed_language_minutes']=cp.qualified_consumed_watch_minutes*(1-cp.top_consumed_language_watch_share)
    program=q.groupby(KEY+['program_type'],as_index=False).qualified_consumed_watch_minutes.sum()
    program['program_qualified_watch_share']=program.qualified_consumed_watch_minutes/program.groupby(KEY).qualified_consumed_watch_minutes.transform('sum')
    dominant=program.sort_values(KEY+['qualified_consumed_watch_minutes','program_type'],ascending=[True,True,False,True]).drop_duplicates(KEY)
    cp=cp.merge(dominant[KEY+['program_type']].rename(columns={'program_type':'dominant_program_type_by_watch_minutes'}),on=KEY,validate='one_to_one')
    wide=program.pivot(index=KEY,columns='program_type',values='program_qualified_watch_share').reindex(columns=PROGRAMMES).fillna(0)
    wide.columns=['qualified_watch_share_'+c.lower() for c in wide]
    cp=cp.merge(wide.reset_index(),on=KEY,validate='one_to_one')
    cp['library_on_demand_qualified_share']=cp.qualified_watch_share_movie+cp.qualified_watch_share_web_series+cp.qualified_watch_share_documentary_special
    cp['recurring_broadcast_qualified_share']=cp.qualified_watch_share_tv_catchup+cp.qualified_watch_share_reality
    cp['dominant_programme_structure']=np.where(cp.library_on_demand_qualified_share>cp.recurring_broadcast_qualified_share,
        'LIBRARY_ON_DEMAND',np.where(cp.library_on_demand_qualified_share<cp.recurring_broadcast_qualified_share,'RECURRING_BROADCAST','TIED'))
    activity=e.groupby(KEY,as_index=False).event_date.nunique().rename(columns={'event_date':'reconstructed_active_days'})
    qactivity=q.groupby(KEY,as_index=False).agg(reconstructed_qualified_active_days=('event_date','nunique'),reconstructed_qualified_starts=('qualified_starts','sum'))
    cp=cp.merge(activity,on=KEY).merge(qactivity,on=KEY)
    recon=cp.merge(pvw,on=KEY,validate='one_to_one')
    numeric=[('consumed_watch_minutes','watch_minutes'),('qualified_consumed_watch_minutes','qualified_watch_minutes'),
        ('top_consumed_language_watch_share','top_language_share'),('non_top_consumed_language_minutes','non_top_language_minutes'),
        ('all_event_distinct_consumed_audio_languages','distinct_audio_languages_consumed'),('reconstructed_active_days','active_days'),
        ('reconstructed_qualified_active_days','qualified_active_days'),('reconstructed_qualified_starts','qualified_starts')]
    for a,b in numeric:
        for w,g in recon.groupby('window_label'):
            atol=1e-7 if 'minutes' in a else 1e-10
            check('PVW_'+b,(~np.isclose(g[a].astype(float),g[b].astype(float),atol=atol,rtol=1e-12)).sum(),w,tolerance=f'absolute {atol}; relative 1e-12')
    check('PVW_dominant_language',recon.dominant_consumed_audio_language.ne(recon.top_language).sum())
    check('positive_qualified_consumption',cp.qualified_consumed_watch_minutes.le(0).sum())
    return cp,cl


def summarise(d,keys):
    rows=[]
    metrics=['distinct_reachable_audio_languages','distinct_consumed_audio_languages','consumed_languages_per_reachable_language',
        'top_consumed_language_watch_share','opportunity_top_language_pair_share','opportunity_top_language_day_share',
        'consumed_language_hhi','opportunity_language_hhi_pairs','opportunity_language_hhi_days','qualified_watch_hours','active_days',
        'reachable_parent_titles_in_window','reachable_title_language_pairs_in_window','mean_daily_reachable_audio_languages',
        'consumed_minus_opportunity_top_day_share_pp','consumed_minus_opportunity_hhi_days',
        'library_on_demand_qualified_share']
    for key,g in d.groupby(keys,sort=True,dropna=False):
        if not isinstance(key,tuple):key=(key,)
        r=dict(zip(keys,key));r['profiles']=len(g)
        for m in metrics:r['avg_'+m]=g[m].mean()
        for m in ['distinct_reachable_audio_languages','distinct_consumed_audio_languages','consumed_languages_per_reachable_language',
                  'top_consumed_language_watch_share','qualified_watch_hours','active_days']:
            r['median_'+m]=g[m].median()
        for m in ['distinct_consumed_audio_languages','top_consumed_language_watch_share']:
            r['p25_'+m]=g[m].quantile(.25);r['p75_'+m]=g[m].quantile(.75)
            r['min_'+m]=g[m].min();r['max_'+m]=g[m].max();r['std_'+m]=g[m].std(ddof=0)
        r['min_qualified_watch_hours']=g.qualified_watch_hours.min();r['max_qualified_watch_hours']=g.qualified_watch_hours.max()
        r['min_active_days']=g.active_days.min();r['max_active_days']=g.active_days.max()
        r['profiles_consumption_more_concentrated_days']=(g.consumed_language_hhi>g.opportunity_language_hhi_days).sum()
        r['hhi_valid_profiles']=g.consumed_language_hhi_is_valid.sum()
        for n in range(0,11):r[f'profiles_consuming_{n}_languages']=g.distinct_consumed_audio_languages.eq(n).sum()
        rows.append(r)
    return pd.DataFrame(rows)


def assemble(op,ol,cp,cl,pvw,schemas):
    for name,d,keys in [('opp_profile',op,KEY),('opp_language',ol,LK),('cons_profile',cp,KEY),('cons_language',cl,LK)]:
        check('duplicate_'+name,d.duplicated(keys).sum());check('null_key_'+name,d[keys].isna().sum().sum())
    for s in ['opportunity_pair_share','opportunity_day_share']:
        check(s+'_sums_to_one',(~np.isclose(ol.groupby(KEY)[s].sum(),1,atol=1e-12)).sum())
    check('consumed_share_sums_to_one',(~np.isclose(cl.groupby(KEY).consumed_watch_share.sum(),1,atol=1e-12)).sum())
    for name,d,cols in [('opportunity',ol,['opportunity_pair_share','opportunity_day_share']),
                       ('consumption',cl,['consumed_watch_share'])]:
        values=d[cols].to_numpy(float)
        check(name+'_shares_in_0_1',np.count_nonzero(~np.isfinite(values)|(values < -1e-12)|(values > 1+1e-12)))
    alloc=ol.merge(cl,on=LK,how='outer',validate='one_to_one',indicator=True)
    check('consumed_language_without_reachable_opportunity',alloc._merge.eq('right_only').sum())
    alloc=alloc.drop(columns='_merge')
    consumption_columns=[c for c in cl if c not in LK]
    alloc[consumption_columns]=alloc[consumption_columns].fillna(0)
    alloc['consumption_minus_opportunity_pair_share_pp']=100*(alloc.consumed_watch_share-alloc.opportunity_pair_share)
    alloc['consumption_minus_opportunity_day_share_pp']=100*(alloc.consumed_watch_share-alloc.opportunity_day_share)
    # Attach pair/day opportunity to consumed rows; zero-qualified raw-only rows remain explicit.
    cl=cl.merge(ol[LK+['opportunity_pair_share','opportunity_day_share']],on=LK,validate='one_to_one')
    cl['consumption_minus_opportunity_pair_share_pp']=100*(cl.consumed_watch_share-cl.opportunity_pair_share)
    cl['consumption_minus_opportunity_day_share_pp']=100*(cl.consumed_watch_share-cl.opportunity_day_share)
    merged=op.merge(cp,on=KEY,validate='one_to_one').merge(pvw[KEY+['qualified_watch_hours','active_days','watch_hours_quintile','active_days_quintile']],on=KEY,validate='one_to_one')
    merged.insert(2,'main_eligibility',1)
    merged['opportunity_family']=np.select([merged.opportunity_regime.eq('STABLE_BROAD'),merged.opportunity_regime.str.startswith('STABLE_REGIONAL_')],['BROAD','REGIONAL'],default='MIXED_ACCESS')
    check('consumed_breadth_exceeds_reachable',(merged.distinct_consumed_audio_languages>merged.distinct_reachable_audio_languages).sum())
    check('consumed_title_language_pairs_exceed_reachable',(merged.consumed_title_language_pairs>merged.reachable_title_language_pairs_in_window).sum())
    check('qualified_programme_shares_sum_to_one',(~np.isclose(merged[['qualified_watch_share_'+p.lower() for p in PROGRAMMES]].sum(axis=1),1,atol=1e-12)).sum())
    merged['consumed_languages_per_reachable_language']=merged.distinct_consumed_audio_languages/merged.distinct_reachable_audio_languages
    merged['consumed_title_language_pairs_per_reachable_pair']=merged.consumed_title_language_pairs/merged.reachable_title_language_pairs_in_window
    merged['consumed_minus_opportunity_top_day_share_pp']=100*(merged.top_consumed_language_watch_share-merged.opportunity_top_language_day_share)
    merged['consumed_minus_opportunity_hhi_days']=merged.consumed_language_hhi-merged.opportunity_language_hhi_days
    for w,n in [('BASELINE_90',8761),('FINAL_90',9762)]:check('merged_eligible_count',abs(merged.window_label.eq(w).sum()-n),w)
    check('coverage_ratio_redundant',0,detail='Every consumed language is reachable; duplicate consumed_language_pair_coverage measure omitted.')
    access=summarise(merged,['window_label','opportunity_regime'])
    watch=summarise(merged,['window_label','opportunity_regime','watch_hours_quintile'])
    active=summarise(merged,['window_label','opportunity_regime','active_days_quintile'])
    program=summarise(merged,['window_label','opportunity_regime','dominant_program_type_by_watch_minutes'])
    program=program.rename(columns={'dominant_program_type_by_watch_minutes':'program_context'})
    program.insert(0,'context_basis','FIVE_PROGRAMME_TYPES')
    binary=summarise(merged,['window_label','opportunity_regime','dominant_programme_structure']).rename(columns={'dominant_programme_structure':'program_context'})
    binary.insert(0,'context_basis','SECONDARY_PROGRAMME_STRUCTURE')
    program=pd.concat([program,binary],ignore_index=True)
    allocation=[]
    for (w,reg,lang),g in alloc.groupby(['window_label','opportunity_regime','audio_language'],sort=True):
        q=g[g.is_meaningful_consumed_language.eq(1)]
        r=dict(window_label=w,opportunity_regime=reg,audio_language=lang,profiles_with_opportunity=len(g),
               profiles_with_qualified_consumption=len(q),share_profiles_with_qualified_consumption=len(q)/len(g))
        for col in ['opportunity_pair_share','opportunity_day_share','consumed_watch_share',
                    'consumption_minus_opportunity_pair_share_pp','consumption_minus_opportunity_day_share_pp']:
            r['avg_'+col+'_all_opportunity_profiles']=g[col].mean()
            r['avg_'+col+'_consumers_only']=q[col].mean()
        allocation.append(r)
    distribution=[]
    for w,g in merged.groupby('window_label'):
        for metric in ['distinct_reachable_audio_languages','distinct_consumed_audio_languages','all_event_distinct_consumed_audio_languages']:
            for value,n in g[metric].value_counts().sort_index().items():
                distribution.append(dict(window_label=w,measure=metric,language_count=value,profiles=n,share_profiles=n/len(g)))
    persistence=pd.DataFrame([
        ['profile_language_opportunity_window','profile x window','profile_id, window_label','01_opp_profile',
         'Reachable language breadth, pair/day composition and opportunity HHI for later SQL joins.',
         'Existing profile_opportunity_window covers title opportunity only; access fields are repeated context.','RECOMMEND PERSIST AFTER REVIEW'],
        ['profile_language_opportunity_long','profile x window x language','profile_id, window_label, audio_language','02_opp_language',
         'Reusable per-language opportunity denominators and days for allocation comparisons.',
         'No language opportunity long table exists.','RECOMMEND PERSIST AFTER REVIEW'],
        ['profile_language_behavior_window','profile x window','profile_id, window_label','03_cons_profile',
         'Qualified language breadth, HHI, parent-language pairs and programme mix.',
         'PVW already holds qualified dominant language/share and raw-event breadth; qualified breadth/HHI/mix add information.','RECOMMEND PERSIST AFTER REVIEW'],
        ['profile_language_behavior_long','profile x window x language','profile_id, window_label, audio_language','04_cons_language',
         'Reusable raw and qualified minutes, support, title-language counts and allocation shares.',
         'No equivalent persisted long table; opportunity-share columns can be supplied by a SQL join.','RECOMMEND PERSIST AFTER REVIEW'],
        ['profile_language_measurement_join','profile x window','profile_id, window_label','05_merged_profile',
         'Convenient analysis join of the reusable sidecars and canonical mart context.',
         'Redundant physical copy; use a SQL view or explicit query if later authorized.','DO NOT PERSIST AS TABLE'],
        ['test6b_grouped_diagnostics','grouped evidence','varies','06 through 14',
         'Review evidence and validation, retained in workbook.',
         'Reproducible from the profile and language tables.','DO NOT PERSIST']
    ],columns=['candidate_table_name','grain','key','source_sheet','why_reusable','redundancy','recommendation'])
    existing=sorted(schemas.table_name.unique())
    methods=[
        ('Authority','MySQL read-only consistent snapshot; canonical opportunity/access functions. Test-6A prose is supporting context.'),
        ('Population','All eligible profile-windows: 8761 BASELINE_90 and 9762 FINAL_90, including PARTIAL and MIXED_ACCESS.'),
        ('Qualification','Manual watch_seconds >= min(300, 0.10 * runtime_seconds); autoplay >= min(480, 0.20 * runtime_seconds). Full watch time of qualified events is counted.'),
        ('Window assignment','event_start_ts within each window; upper bound exclusive. Calendar timestamps are project IST. No cross-window first-start asset ownership for these viewing allocations.'),
        ('Primary language breadth','Languages with at least one qualified event. Raw-event breadth retained separately for PVW reconciliation; raw-only rows have is_meaningful_consumed_language=0.'),
        ('Raw mart distinction','PVW distinct_audio_languages_consumed is all-event breadth; top_language and top_language_share use qualified minutes.'),
        ('HHI rule','Sum of squared language shares; valid if qualified minutes >0. Monolingual HHI=1. Mathematical descriptor only, no minimum evidence threshold.'),
        ('Opportunity shares','Pair and title-day shares are structural composition, not a probability of choosing a language. Each sums to one within profile-window.'),
        ('Regional entitlement','Pack-language track admits a title; all listed tracks on the entitled title are selectable. Audio bridge has no independent track dates.'),
        ('Track timing caveat','Tracks inherit title availability. This is a bounded source-model limitation, not uncertainty about implemented entitlement.'),
        ('Regime convention','Exact Test-5 history classification retained. A change between distinct ALL_ACCESS contexts remains MIXED_ACCESS under that established classifier even though VOD reach is equivalent.'),
        ('Quintiles','SQL NTILE(5), partition window_label, ordered by qualified_watch_hours or active_days then profile_id. Full eligible population. Reconciled to existing carryforward ranks; no mechanism labels read.'),
        ('Context interpretation','Separate coarse watch-hours and active-days strata, never crossed. These are descriptive comparisons, not causal adjustment or proof of independent effects. Ties can span quintiles.'),
        ('Programme basis','Exact dominant programme by qualified watch minutes; lexical tie-break. Five qualified shares retained. Library/on-demand is movie+web series+documentary; secondary sensitivity only.'),
        ('Aggregated means','Equal weight per profile within each reported group. No pooled regional result. Allocation reports all reachable profiles and consumers-only means separately.'),
        ('Diagnostic ratios','consumed_languages_per_reachable_language and consumed_title_language_pairs_per_reachable_pair are diagnostics, not utilisation, conversion or openness scores.'),
        ('Consumption pair grain','Distinct parent title x consumed audio language with a qualified event; summed across language rows.'),
        ('Concentration differences','Top-share difference compares each distribution maximum; maxima can belong to different languages. Matched-language differences are in 04_cons_language and 10_lang_alloc.'),
        ('Consumed minutes by language','Stored in 04_cons_language rather than repeated as an embedded JSON map in 03_cons_profile.'),
        ('Quantiles','Pandas linear interpolation. Display rounding only; full numeric values retained.'),
        ('Existing analysis tables','; '.join(existing)),
        ('Canonical references','src/marts.py build_profile_viewership_window; src/mart_transforms/qualification.py; src/mart_transforms/opportunity.py; src/mart_transforms/access.py; src/behavior.py _pick_audio; analysis/test5_5b1_reachable_window_pool.py classify_access_regimes; sql/diagnostics/test4_4d3_canonical_separate_activity_controls.sql'),
        ('Scope','No mechanism-state merge, native-language inference, longitudinal comparison, openness score, permanent database tables, commits or pushes.')]
    return {'01_opp_profile':op,'02_opp_language':ol,'03_cons_profile':cp,'04_cons_language':cl,
        '05_merged_profile':merged,'06_access_summary':access,'07_watch_context':watch,
        '08_active_context':active,'09_program_context':program,'10_lang_alloc':pd.DataFrame(allocation),
        '11_validation':pd.DataFrame(CHECKS),'12_persistence_rec':persistence,
        '13_breadth_distributions':pd.DataFrame(distribution),'14_methods':pd.DataFrame(methods,columns=['topic','definition_or_caveat'])}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--work-dir',type=Path,default=Path(tempfile.gettempdir())/'test6b_language_audit')
    parser.add_argument('--export-only',action='store_true')
    parser.add_argument('--compute-only',action='store_true')
    args=parser.parse_args();args.work_dir.mkdir(parents=True,exist_ok=True)
    start=time.perf_counter()
    if not args.export_only:
        conn=connect()
        try:
            with conn.cursor() as cur:
                cur.execute('SET TRANSACTION READ ONLY');cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
            sources=load_sources(conn);conn.rollback()
        finally:conn.close()
        profiles,cycles,cat,audio,pvw,old,ranks,schemas,events=sources
        check('unique_eligible_profile_windows',pvw.duplicated(KEY).sum())
        for w,n in [('BASELINE_90',8761),('FINAL_90',9762)]:check('eligible_population',abs(pvw.window_label.eq(w).sum()-n),w)
        ranked=pvw.merge(ranks,on=KEY,how='left',suffixes=('','_stored'),validate='one_to_one')
        for col in ['watch_hours_quintile','active_days_quintile']:check('canonical_'+col,ranked[col].ne(ranked[col+'_stored']).sum())
        # SQL predicate mirrors the canonical vectorised function. Check thresholds
        # around every catalogue runtime before accepting SQL aggregation semantics.
        rt=np.repeat(cat.runtime_minutes.to_numpy(float)*60,6)
        auto=np.tile([0,0,0,1,1,1],len(cat))
        threshold=np.where(auto==1,np.minimum(480,.20*rt),np.minimum(300,.10*rt))
        watch=np.floor(threshold)+np.tile([-1,0,1,-1,0,1],len(cat))
        expected=qualified_start(pd.DataFrame({'watch_seconds':watch,'is_autoplay':auto}),rt)
        check('SQL_qualification_vs_canonical_boundaries',np.count_nonzero(expected!=(watch>=threshold)))
        op,ol=reconstruct(profiles,cycles,cat,audio,pvw,old,events)
        cp,cl=consumption(events,pvw)
        frames=assemble(op,ol,cp,cl,pvw,schemas)
        elapsed=time.perf_counter()-start
        frames['14_methods']=pd.concat([frames['14_methods'],pd.DataFrame([{'topic':'Calculation runtime seconds','definition_or_caveat':f'{elapsed:.3f} excluding workbook export'}])],ignore_index=True)
        inventory=[]
        for name,d in frames.items():
            if np.isinf(d.select_dtypes(include='number').to_numpy()).any():
                raise RuntimeError('STOP: infinite numeric result in '+name)
            d.to_pickle(args.work_dir/(name+'.pkl'))
            d.to_json(args.work_dir/(name+'.json'),orient='split',index=False,double_precision=15)
            inventory.append(dict(sheet=name,rows=len(d),columns=len(d.columns)))
        (args.work_dir/'inventory.json').write_text(json.dumps(inventory),encoding='utf-8')
        print('COMPUTATION_SECONDS',elapsed,flush=True)
        print(frames['13_breadth_distributions'].to_string(index=False),flush=True)
        print('ACCESS\n',frames['06_access_summary'][['window_label','opportunity_regime','profiles',
            'avg_distinct_reachable_audio_languages','avg_distinct_consumed_audio_languages','avg_top_consumed_language_watch_share',
            'avg_opportunity_top_language_day_share','avg_consumed_language_hhi','avg_opportunity_language_hhi_days']].to_string(index=False),flush=True)
        print('INVENTORY',inventory,flush=True)
    if not args.compute_only:
        export_workbook(args.work_dir)
    print('TOTAL_SECONDS',time.perf_counter()-start,flush=True)


def export_workbook(workdir):
    deps=Path(os.environ.get('CODEX_DEPENDENCIES',str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies')))
    node=deps/'node/bin/node.exe' if os.name=='nt' else deps/'node/bin/node'
    modules=deps/'node/node_modules'
    if not node.exists() or not modules.exists():
        raise RuntimeError('Bundled artifact-tool runtime missing; set CODEX_DEPENDENCIES to the installed runtime dependencies directory.')
    link=workdir/'node_modules'
    if not link.exists():
        if os.name=='nt':subprocess.run(['cmd','/c','mklink','/J',str(link),str(modules)],check=True,capture_output=True)
        else:link.symlink_to(modules,target_is_directory=True)
    builder=workdir/'export.mjs'
    builder.write_text(EXPORT_JS,encoding='utf-8')
    # Artifact tool authors layout, styles, small tables and representative rows.
    # Stream the large raw tables into OOXML: its in-memory renderer need not
    # lay out millions of raw cells. No calculation or formula is rewritten.
    template=workdir/'workbook_template.xlsx'
    subprocess.run([str(node),'--max-old-space-size=6000',str(builder),str(workdir),str(template)],check=True)
    # Read-only saved-file comparison checks every populated value, not just counts.
    python=deps/'python/python.exe' if os.name=='nt' else deps/'python/bin/python'
    streaming=workdir/'stream_rows.py'
    streaming.write_text(STREAM_PY,encoding='utf-8')
    staged=workdir/OUT.name
    subprocess.run([str(python),str(streaming),str(workdir),str(template),str(staged)],check=True)
    verifier=workdir/'verify_export.py'
    verifier.write_text(VERIFY_PY,encoding='utf-8')
    subprocess.run([str(python),str(verifier),str(workdir),str(staged)],check=True)
    import shutil
    shutil.copyfile(staged,OUT)
    print('WORKBOOK',OUT,flush=True)


EXPORT_JS = r'''
import fs from 'node:fs/promises';
import path from 'node:path';
import {Workbook,SpreadsheetFile} from '@oai/artifact-tool';
const [dir,out]=process.argv.slice(2);
const manifest=JSON.parse(await fs.readFile(path.join(dir,'inventory.json'),'utf8'));
const wb=Workbook.create();
const contents=wb.worksheets.add('00_contents');
const descriptions={
 '01_opp_profile':['profile x window','Historical language opportunity summary','profile_id, window_label','Pair and day shares describe supply; not choice probabilities.'],
 '02_opp_language':['profile x window x reachable language','Language-specific title counts and exposure days','profile_id, window_label, audio_language','Includes languages never meaningfully consumed.'],
 '03_cons_profile':['profile x window','Qualified repertoire, concentration and programme mix','profile_id, window_label','Primary language breadth uses qualified events; raw breadth is explicitly separate.'],
 '04_cons_language':['profile x window x any consumed language','Raw and qualified watch allocation with opportunity shares','profile_id, window_label, audio_language','Raw-only languages remain with is_meaningful_consumed_language=0.'],
 '05_merged_profile':['profile x window','Reusable analysis base with separate activity quintiles','profile_id, window_label','No Test-4 or Test-5 mechanism labels. Ratios are diagnostic only.'],
 '06_access_summary':['window x access regime','Per-profile means, medians, support and count distributions','window_label, opportunity_regime','Each regional pack separate; mixed access retained.'],
 '07_watch_context':['window x access regime x watch quintile','Separate watch-volume context','window_label, opportunity_regime, watch_hours_quintile','Coarse descriptive strata; not causal adjustment.'],
 '08_active_context':['window x access regime x active-day quintile','Separate activity-frequency context','window_label, opportunity_regime, active_days_quintile','Not crossed with watch-hours quintiles.'],
 '09_program_context':['window x access regime x programme context','Exact dominant programme and secondary structural sensitivity','context_basis, window_label, opportunity_regime, program_context','Programme shares and dominance use qualified watch minutes.'],
 '10_lang_alloc':['window x access regime x language','Opportunity versus consumption allocation','window_label, opportunity_regime, audio_language','All-opportunity and consumers-only means have different denominators.'],
 '11_validation':['check x window','Integrity and canonical reconciliation results','check, window_label','All foundational checks must pass before interpretation.'],
 '12_persistence_rec':['candidate sidecar','Complete persistence recommendation','candidate_table_name','Recommendations only; no database tables created.'],
 '13_breadth_distributions':['window x measure x language count','Full observed opportunity, qualified and raw breadth distributions','window_label, measure, language_count','Shares are fractions. Raw breadth is a reconciliation reference.'],
 '14_methods':['definition','Sources, semantics and limitations','topic','Audio tracks inherit title dates; no independent track timing.']
};
for(const item of manifest){
 const obj=JSON.parse(await fs.readFile(path.join(dir,item.sheet+'.json'),'utf8'));
 const sh=wb.worksheets.add(item.sheet);sh.showGridLines=false;
 const nc=obj.columns.length,actualRows=obj.data.length,nr=actualRows>1000?25:actualRows;
 sh.getRangeByIndexes(0,0,1,nc).values=[obj.columns];
 sh.getRangeByIndexes(1,0,nr,nc).values=obj.data.slice(0,nr);
 const head=sh.getRangeByIndexes(0,0,1,nc);
 head.format={fill:'#24374D',font:{name:'Arial',bold:true,color:'#FFFFFF',size:10},wrapText:true,rowHeight:110};
 // Bound formatting to actual data, keeping wide raw tables easy to filter.
 for(let j=0;j<nc;j++){
  const col=sh.getRangeByIndexes(0,j,nr+1,1);col.format.columnWidth=25;
  const body=sh.getRangeByIndexes(1,j,nr,1);
  const header=obj.columns[j];
  if(['profile_id','window_label','audio_language'].includes(header))col.format.columnWidth=17;
  if(['definition_or_caveat','why_reusable','redundancy','detail'].includes(header)){col.format.columnWidth=95;body.format.wrapText=true;}
  if(['key','grain','recommendation','topic','candidate_table_name','check'].includes(header)){col.format.columnWidth=44;body.format.wrapText=true;}
  const sample=obj.data.map(r=>r[j]).find(v=>v!==null);
  if(typeof sample==='number'){
   const integers=obj.data.every(r=>r[j]===null||Number.isInteger(r[j]));
   body.setNumberFormat(integers?'#,##0':header.endsWith('_pp')||header.includes('_pp_')?'0.00':header.includes('share')||header.includes('hhi')||header==='consumed_languages_per_reachable_language'?'0.0000':'0.000');
  }
 }
 sh.freezePanes.freezeRows(1);
 if(actualRows<1000){
  sh.tables.add(sh.getRangeByIndexes(0,0,nr+1,nc),true,'T6B_'+item.sheet.replace(/^[0-9]+_/,''));
  if(item.sheet==='14_methods')sh.getRangeByIndexes(1,0,nr,nc).format.rowHeight=64;
  if(item.sheet==='12_persistence_rec')sh.getRangeByIndexes(1,0,nr,nc).format.rowHeight=76;
 }
 const range=item.sheet==='14_methods'?'A1:B5':item.sheet==='12_persistence_rec'?'A1:D4':'A1:D4';
 const png=await wb.render({sheetName:item.sheet,range,scale:1,format:'png'});
 await fs.writeFile(path.join(dir,item.sheet+'.png'),new Uint8Array(await png.arrayBuffer()));
 console.log('SHEET_LAYOUT',item.sheet,actualRows,nc);
}
contents.showGridLines=false;
contents.getRange('A1').values=[['Test 6B Opportunity conditioned consumed language behaviour']];
contents.getRange('A1').format.font={name:'Arial',size:15,bold:true};
contents.getRange('A3:F3').merge();contents.getRange('A3').values=[['Qualified-event language behaviour versus historically entitled title/audio opportunity. All 18,523 eligible profile-windows retained.']];
contents.getRange('A4:F4').merge();contents.getRange('A4').values=[['Shares and HHI are stored as fractions; columns ending pp are percentage-point differences. Display rounding does not alter stored values.']];
contents.getRange('A5:F5').merge();contents.getRange('A5').values=[['Read 14_methods before interpretation. Partial and mixed access are resolved day by day. No openness score or permanent database table is created.']];
const rows=manifest.map(x=>[x.sheet,descriptions[x.sheet][0],x.rows,descriptions[x.sheet][1],descriptions[x.sheet][2],descriptions[x.sheet][3]]);
contents.getRangeByIndexes(7,0,rows.length+1,6).values=[['Sheet name','Grain','Data rows','Purpose','Key fields','Caveat'],...rows];
contents.getRange('A8:F8').format={fill:'#24374D',font:{bold:true,color:'#FFFFFF'},rowHeight:25};
contents.getRangeByIndexes(8,0,rows.length,6).format.wrapText=true;
contents.getRangeByIndexes(8,0,rows.length,6).format.rowHeight=60;
for(const [col,width] of [['A',29],['B',35],['C',12],['D',55],['E',48],['F',67]])contents.getRange(col+':'+col).format.columnWidth=width;
const cover=await wb.render({sheetName:'00_contents',range:'A1:D12',scale:1,format:'png'});
await fs.writeFile(path.join(dir,'00_contents.png'),new Uint8Array(await cover.arrayBuffer()));
console.log((await wb.inspect({kind:'table',range:"'13_breadth_distributions'!A1:E5",tableMaxRows:5,tableMaxCols:5,maxChars:1500})).ndjson);
console.log('EXPORT_STARTED');
await (await SpreadsheetFile.exportXlsx(wb)).save(out);
console.log('SAVED',out);
process.exit(0);
'''

VERIFY_PY = r'''
import json,math,sys
from pathlib import Path
from openpyxl import load_workbook
root=Path(sys.argv[1]);path=Path(sys.argv[2])
manifest=json.loads((root/'inventory.json').read_text())
w=load_workbook(path,read_only=True,data_only=False)
assert len(w.sheetnames)==len(manifest)+1
n=0
for item in manifest:
    obj=json.loads((root/(item['sheet']+'.json')).read_text())
    it=w[item['sheet']].iter_rows(values_only=True)
    assert list(next(it))==obj['columns']
    for source in obj['data']:
        actual=next(it)
        assert len(actual)==len(source),(item['sheet'],'columns')
        for a,b in zip(actual,source):
            n+=1
            if b is None or b=='':assert a in (None,'')
            elif isinstance(b,(int,float)):assert math.isclose(a,b,rel_tol=1e-13,abs_tol=1e-13),(item['sheet'],a,b)
            else:assert a==b,(item['sheet'],a,b)
    assert next(it,None) is None,(item['sheet'],'extra rows')
w.close()
print('VERIFIED saved workbook values',n,'cells;',len(manifest)+1,'sheets;',path.stat().st_size,'bytes')
'''

STREAM_PY = r'''
import json,re,sys,zipfile,posixpath
from pathlib import Path
from xml.sax.saxutils import escape
from lxml import etree as ET
rootdir,template,out=map(Path,sys.argv[1:])
manifest=json.loads((rootdir/'inventory.json').read_text())
ns={'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
uri=ns['m'];ruri='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
def col(n):
    s=''
    while n:
        n,r=divmod(n-1,26);s=chr(65+r)+s
    return s
with zipfile.ZipFile(template) as zi:
    wb=ET.fromstring(zi.read('xl/workbook.xml'))
    rels=ET.fromstring(zi.read('xl/_rels/workbook.xml.rels'))
    targets={r.get('Id'):r.get('Target') for r in rels}
    sheetpaths={s.get('name'):posixpath.normpath(posixpath.join('xl',targets[s.get('{'+ruri+'}id')])).lstrip('/')
        if not targets[s.get('{'+ruri+'}id')].startswith('/') else targets[s.get('{'+ruri+'}id')].lstrip('/')
        for s in wb.find('m:sheets',ns)}
    large={sheetpaths[x['sheet']]:x for x in manifest if x['rows']>1000}
    with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as zo:
        for info in zi.infolist():
            name=info.filename.replace('\\','/')
            if name not in large:
                zo.writestr(info,zi.read(info));continue
            item=large[name];obj=json.loads((rootdir/(item['sheet']+'.json')).read_text())
            xml=ET.fromstring(zi.read(info));sd=xml.find('m:sheetData',ns)
            first=sd.find('m:row',ns)
            header=ET.tostring(first,encoding='utf-8')
            styles={}
            for row in list(sd)[1:]:
                for c in row:
                    letter=re.sub(r'\d','',c.get('r'))
                    if c.get('s') is not None:styles.setdefault(letter,c.get('s'))
            end=f'{col(item["columns"])}{item["rows"]+1}'
            dim=xml.find('m:dimension',ns)
            if dim is not None:dim.set('ref','A1:'+end)
            af=xml.find('m:autoFilter',ns)
            if af is None:
                af=ET.Element('{'+uri+'}autoFilter');xml.insert(list(xml).index(sd)+1,af)
            af.set('ref','A1:'+end)
            sd.clear()
            skeleton=ET.tostring(xml,xml_declaration=True,encoding='UTF-8',standalone=True)
            match=re.search(rb'<(?:\w+:)?sheetData\b[^>]*/>',skeleton)
            assert match is not None
            letters=[col(j+1) for j in range(item['columns'])]
            with zo.open(name,'w',force_zip64=True) as dest:
                dest.write(skeleton[:match.start()]+('<sheetData xmlns="'+uri+'">').encode('utf-8')+header)
                buf=[]
                for i,row in enumerate(obj['data'],2):
                    cells=[]
                    for letter,v in zip(letters,row):
                        style=f' s="{styles[letter]}"' if letter in styles else ''
                        prefix=f'<c r="{letter}{i}"{style}'
                        if v is None:cells.append(prefix+'/>')
                        elif isinstance(v,bool):cells.append(prefix+f' t="b"><v>{int(v)}</v></c>')
                        elif isinstance(v,(int,float)):cells.append(prefix+f'><v>{repr(v)}</v></c>')
                        else:cells.append(prefix+' t="inlineStr"><is><t xml:space="preserve">'+escape(str(v))+'</t></is></c>')
                    buf.append(f'<row r="{i}">'+''.join(cells)+'</row>')
                    if len(buf)>=1000:dest.write(''.join(buf).encode('utf-8'));buf=[]
                if buf:dest.write(''.join(buf).encode('utf-8'))
                dest.write(b'</sheetData>'+skeleton[match.end():])
            print('STREAMED',item['sheet'],item['rows'],flush=True)
print('STREAMED_WORKBOOK',out,flush=True)
'''


if __name__=='__main__':main()
